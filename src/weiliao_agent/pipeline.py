"""管道编排器（Story 1.2 核心交付）。

手写纯 Python 编排（AD-2，无 LangGraph Runtime 依赖）：顺序调用
intent_router → cache_lookup → llm_generate → risk_review →
response_assembler，过滤器间仅经信封传递。

Story 5.1 转人工短路：信封经 intent_router 挂 ``handoff`` 判定结果后，
若 ``triggered is True`` 编排器立即跳过剩余过滤器（cache/llm/risk/
assembler 零执行，红线意图 100% 无生成回答），走转人工出口分支回复
固定文案 ``HANDOFF_REPLY_TEXT``（决策 Q1-A：转人工专用出口文案，不走
模板兜底库）；短路非降级（degraded=False，设计内分支）。

Story 1.5 追踪挂点：``run()`` 入口为 trace_id 单一权威生成点（信封
缺失即回填；携带值透传——既有测试锁定该语义，非 32-hex 值仅跳过
Langfuse 追踪）；消息级 trace 上下文包裹整条链；每过滤器执行在
主线程侧包 span；降级路径写降级事件；管道完成后补记最终下发文本。
全部追踪 fail-safe，未配置 Langfuse 时 no-op，编排与兜底语义不变。

统一错误兜底：
- 任一过滤器抛异常 → 捕获并降级模板兜底，跳过剩余过滤器；
- 端到端 deadline（默认内部预算 2.5s，为 NFR-1 P99 < 3s 留出口余量，
  可配置）逐跳检查剩余预算，每跳以 ``future.result(timeout=剩余预算)``
  执行，超时立即兜底、不傻等全量超时，执行线程结果丢弃（占位过滤器
  无副作用，可安全丢弃）。

买家侧永不暴露 5xx：所有路径返回 ``PipelineResult``，错误应答携带
trace_id 且 ``degraded=True``。
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass
from typing import Protocol

from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.cache_lookup import CacheLookupFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.llm_generate import LlmGenerateFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import RiskReviewFilter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.llm.client import OpenAiGenerationClient
from weiliao_agent.llm.subgraph import LlmGenerationSubgraph
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import tracing

# 端到端内部预算：NFR-1 P99 < 3s 的出口余量口径，可配置。
DEFAULT_BUDGET_SECONDS = 2.5
# LLM 生成子图预算（Story 4.3）：必须显著小于编排器内部预算——客户端
# 默认 1.8s 的慢调用会拖过剩余跳预算，使编排器以 slow_filter=llm_generate
# 降级（degraded=True），违反影子模式「生成链路故障对买家零感知」；
# 0.9s 使慢调用在过滤器内超时归一为生成失败（仅丢草稿 + trace 事件，
# 买家应答与 degraded 语义逐字节保持 4.2 前口径）。装配期不触达凭据。
LLM_GENERATE_BUDGET_SECONDS = 0.9
# 转人工固定出口文案（Story 5.1 决策 Q1-A）：硬触发命中后由编排器
# 出口分支直接回复；转人工专用文案不走模板兜底库。常量本身保持纯
# 文案不变，AI 披露前缀由出口处统一注入（Story 5.4 决策 Q1-B：转人工
# 文案同样携标——买家可见文本 100% 携带），编排器出口与 http 终态
# 闸门两处逐字节一致。
HANDOFF_REPLY_TEXT = "已为您转接经纪人，请稍候"

logger = logging.getLogger(__name__)


class HandoffCommandSender(Protocol):
    """Story 5.2 转交指令发送器协议（AD-7）：编排器只依赖本协议，组合根
    注入 ``handoff.command.HandoffCommandSender``（或测试替身）。

    ``send_command`` 为同步有界阻塞调用：返回即「发送完成或重试耗尽
    归一 DLQ」——编排器据此保证严格先指令后通知。实现须 fail-safe：
    任何发送失败不得抛出、不得改变买家应答。
    """

    def send_command(self, envelope: Envelope, *, reply: str) -> None:
        """发送转交指令（reply 为当前轮 Agent 回复原文，供上下文拼轮）。"""


@dataclass(frozen=True)
class PipelineResult:
    """管道执行结果：买家侧应答与降级元数据。"""

    reply: str
    trace_id: str
    degraded: bool
    failed_filter: str | None  # degraded=False 时为 None

    def to_dict(self) -> dict[str, object]:
        """序列化为端点响应体。"""
        return asdict(self)


class PipelineOrchestrator:
    """顺序编排五过滤器链，统一异常捕获与逐跳 deadline 预算。"""

    def __init__(
        self,
        filters: list[Filter] | None = None,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        handoff_sender: HandoffCommandSender | None = None,
    ) -> None:
        self._filters: list[Filter] = (
            filters
            if filters is not None
            else [
                IntentRouterFilter(),
                CacheLookupFilter(),
                LlmGenerateFilter(
                    subgraph=LlmGenerationSubgraph(
                        OpenAiGenerationClient(
                            budget_seconds=LLM_GENERATE_BUDGET_SECONDS
                        )
                    )
                ),
                RiskReviewFilter(),
                ResponseAssemblerFilter(),
            ]
        )
        self._budget_seconds = budget_seconds
        # Story 5.2（AD-7）：None（broker 未配置）时出口分支跳过发送，
        # 出口文案语义不变（与消费者门控同口径）。
        self._handoff_sender = handoff_sender

    @property
    def filter_names(self) -> tuple[str, ...]:
        """过滤器链名单（只读），供健康检查探针等链外只读消费。"""
        return tuple(filter_.name for filter_ in self._filters)

    def run(self, envelope: Envelope) -> PipelineResult:
        """执行过滤器链；异常/超时一律降级模板兜底，不向上抛。"""
        # trace_id 单一权威生成点：缺失即由服务生成回填（32 位小写
        # 十六进制，Langfuse trace id 口径）；payload 携带值透传。
        if envelope.trace_id is None:
            envelope.trace_id = tracing.new_trace_id()
        with tracing.trace_message(
            trace_id=envelope.trace_id,
            session_id=envelope.session_id,
            house_id=envelope.house_id,
            turn=envelope.turn,
            intent=envelope.intent,
            message=envelope.message,
        ):
            result = self._run_chain(envelope)
            tracing.record_delivery(
                trace_id=envelope.trace_id,
                reply=result.reply,
                degraded=result.degraded,
                failed_filter=result.failed_filter,
            )
            return result

    def _run_chain(self, envelope: Envelope) -> PipelineResult:
        """deadline 预算内的过滤器链执行（含过滤器 span 记录）。"""
        # run() 入口已权威回填 trace_id（单一生成点），过滤器透传信封
        # 不改写；assert 仅作 mypy 类型收窄（评审补丁：原 or 分支为
        # 假防御——若真触发会与 run() 打开的 trace 分裂成两个 id）。
        assert envelope.trace_id is not None
        trace_id = envelope.trace_id
        deadline = time.monotonic() + self._budget_seconds
        executor = ThreadPoolExecutor(max_workers=max(1, len(self._filters)))
        last_filter: Filter | None = None
        try:
            for filter_ in self._filters:
                last_filter = filter_
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 剩余预算耗尽：立即兜底，不傻等。
                    return self._degraded(envelope, trace_id, slow_filter=filter_.name)
                future = executor.submit(filter_.process, envelope)
                try:
                    # span 在主线程侧包 future.result()：过滤器跑在执行
                    # 线程，不做 OTel 跨线程传播（Story 1.5 约定）。
                    with tracing.filter_span(trace_id, filter_.name):
                        envelope = future.result(timeout=remaining)
                    if envelope.handoff is not None and envelope.handoff.triggered:
                        # 硬触发命中（Story 5.1，AD-12）：短路剩余过滤
                        # 器——cache/llm/risk/assembler 零执行，红线意图
                        # 100% 无生成回答；走转人工出口分支回固定文案。
                        return self._handoff_result(envelope)
                except FutureTimeoutError:
                    return self._degraded(envelope, trace_id, slow_filter=filter_.name)
                except Exception:
                    return self._degraded(envelope, trace_id, failed_filter=filter_.name)
        except Exception:
            # future.result 之外的逃逸异常（如过滤器未定义 name 触发
            # AttributeError）：统一兜底，异常不向上抛（I/O 矩阵约定）。
            if last_filter is None:
                culprit = "pipeline"
            else:
                culprit = getattr(last_filter, "name", None) or type(last_filter).__name__
            return self._degraded(envelope, trace_id, failed_filter=culprit)
        finally:
            # 超时跳的执行线程结果已丢弃，等待其完成无意义（不傻等）。
            executor.shutdown(wait=False)
        # 最终返回点（Story 5.4）：只读消费 response_assembler 的权威
        # 产出 assembled_reply（AI 披露前缀的唯一注入载体，4.4 冻结
        # 语义——通过审核的草稿除加前缀外不得再被改写）；组装产出缺失
        # （替身过滤器透传等）防御性回退模板兜底（render_fallback 自带
        # 前缀），正常链输出逐字节不变。
        reply = envelope.assembled_reply
        if reply is None:
            reply = render_fallback(envelope.intent)
        tracing.record_ai_disclosure(trace_id, disclosed=True)
        return PipelineResult(
            reply=reply,
            trace_id=trace_id,
            degraded=False,
            failed_filter=None,
        )

    def _handoff_result(self, envelope: Envelope) -> PipelineResult:
        """转人工出口分支（Story 5.1 决策 Q1-A）：固定专用文案直接下发。

        非降级（设计内分支，failed_filter 为 None）：降级事件属故障
        归因，转人工是正常合规出口；判定过程已由 intent_router 记
        ``handoff.assessment`` trace 事件，最终下发文本仍由 run() 的
        ``record_delivery`` 补记。

        Story 5.2（AD-7 严格先指令后通知）：发送器注入时先构建并发送
        转交指令（conditions 过滤 assessment_error 哨兵由发送器内收口），
        发送完成（或重试耗尽归一 DLQ）后才返回安抚文案；发送器为 None
        跳过发送。发送器自身 fail-safe，本分支再包一层防御：任何逃逸
        异常不得改变应答（绝不降级为模板兜底）。
        """
        if self._handoff_sender is not None:
            try:
                self._handoff_sender.send_command(envelope, reply=HANDOFF_REPLY_TEXT)
            except Exception:
                # 防御层（发送器契约已承诺不抛）：指令发送失败绝不影响
                # 买家应答，出口文案语义逐字节不变。
                logger.warning(
                    "handoff command sender raised unexpectedly; reply unchanged",
                    exc_info=True,
                )
        trace_id = envelope.trace_id
        assert trace_id is not None  # _run_chain 已权威回填，供 mypy 收窄
        # Story 5.4 决策 Q1-B：转人工文案同样携标（买家可见文本 100%
        # 携带）——前缀在出口处注入，常量保持纯文案；与 http 终态
        # 闸门回包逐字节一致，「转人工后标识消失」由 5.3 闸门拦截保证。
        tracing.record_ai_disclosure(trace_id, disclosed=True)
        return PipelineResult(
            reply=f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}",
            trace_id=trace_id,
            degraded=False,
            failed_filter=None,
        )

    @staticmethod
    def _degraded(
        envelope: Envelope,
        trace_id: str,
        *,
        failed_filter: str | None = None,
        slow_filter: str | None = None,
    ) -> PipelineResult:
        """构造兜底应答：跳过剩余过滤器，携带 trace_id 与降级归因。"""
        # 降级事件挂点：归因过滤器名入 trace；记录本身 fail-safe，
        # 再抛异常也吞掉，绝不二次影响应答。
        tracing.record_degradation(
            trace_id,
            failed_filter=failed_filter,
            slow_filter=slow_filter,
        )
        # Story 5.4：降级兜底文案经 render_fallback 携标下发，披露事件
        # 与降级归因同点记录；记录本身 fail-safe，绝不二次影响应答。
        tracing.record_ai_disclosure(trace_id, disclosed=True)
        culprit = failed_filter if failed_filter is not None else slow_filter
        return PipelineResult(
            reply=render_fallback(envelope.intent),
            trace_id=trace_id,
            degraded=True,
            failed_filter=culprit,
        )
