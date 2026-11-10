"""管道编排器（Story 1.2 核心交付）。

手写纯 Python 编排（AD-2，无 LangGraph Runtime 依赖）：顺序调用
coreference → intent_router → cache_lookup → llm_generate →
risk_review → response_assembler，过滤器间仅经信封传递。

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

Story 5.5 编排器级终态闸门：``run()`` 入口在 trace_id 回填后、打开
trace 上下文之前，经注入的 ``HandoffStatusReader`` 实读 Redis 权威
``handoff_status``——``"triggered"`` 即短路（零过滤器执行、零 LLM、
零模板兜底），应答/事件/日志与 5.3 http 适配器闸门共享同一构建
来源（``handoff_gate_result``）；未知值与读取失败 fail-closed
既有兜底（degraded 模板兜底 + 原 trace_id），绝不人机混答；未注入
reader 时闸门禁用，行为与 5.5 前逐字节一致。5.3 适配器闸门保留
不动（HTTP 主流径），编排器闸门为覆盖全部直接调用入口的权威关闭点。

统一错误兜底：
- 任一过滤器抛异常 → 捕获并降级模板兜底，跳过剩余过滤器；
- 端到端 deadline（默认内部预算 2.5s，为 NFR-1 P99 < 3s 留出口余量，
  可配置）逐跳检查剩余预算，每跳以 ``future.result(timeout=剩余预算)``
  执行，超时立即兜底、不傻等全量超时，执行线程结果丢弃（占位过滤器
  无副作用，可安全丢弃）。

Story 7.2（D1 决策落地，收口 1.2 挂账）：执行器生命周期重审——压测
实测（scripts/loadtest_timeout_storm.py，24 并发 × 4 请求超时风暴）
每请求新建 ``ThreadPoolExecutor`` 形态风暴期存活线程峰值 100（基线 1），
随并发线性膨胀；落地为编排器级共享池（大小 ``max(1, len(filters))``，
随编排器生命周期共存，不随请求生灭）：超时丢弃的跳不再泄漏执行线程，
风暴期线程数不随并发增长（重测峰值回落到预算内），逐跳超时/异常兜底、
应答字节与 degraded 语义逐字节不变。D2 取消机制决策「不做」（Python
无法强杀运行中线程，cooperative cancel 需过滤器契约变更；共享池已使
线程有界，理由详见 spec-7.2 Design Notes D2）。

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
from weiliao_agent.filters.coreference import CoreferenceFilter
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


class HandoffStatusReader(Protocol):
    """Story 5.5 终态闸门状态读取协议：编排器只依赖本窄协议（同
    ``HandoffCommandSender`` 先例），组合根注入 ``SessionManager``
    （或测试替身）。

    ``read_handoff_status`` 为 Redis 权威实读（不缓存、不读 L1 快照）：
    返回 ``"triggered"`` / ``"none"`` 等状态值，key 或字段缺失返回
    None（会话过期语义）。实现不吞异常：读取失败由编排器闸门
    fail-closed 收口。
    """

    def read_handoff_status(self, session_id: str) -> str | None:
        """实读会话 ``handoff_status`` 权威值；缺失返回 None。"""


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


def handoff_gate_result(
    trace_id: str,
    *,
    session_id: str,
    turn: int | None,
) -> PipelineResult:
    """终态闸门共享出口（Story 5.5）：构建闸门 ``PipelineResult`` 并留痕。

    编排器闸门与 5.3 http 适配器闸门两处同字节出口的公开共享契约
    （跨模块导入，防漂移）：reply 逐字节为 ``前缀+HANDOFF_REPLY_TEXT``，
    degraded=False（设计内终态分支）；记 ``already_handed_off`` 与
    ``ai.disclosure``（disclosed=True）事件 + WARNING 日志（可审计，
    记录本身 fail-safe，绝不改变应答）。无 ``record_delivery`` 事件
    （消息未进管道）。
    """
    tracing.record_already_handed_off(
        trace_id,
        session_id=session_id,
        turn=turn,
    )
    tracing.record_ai_disclosure(trace_id, disclosed=True)
    logger.warning(
        "session already handed off; gate short-circuits pipeline "
        "session_id=%s trace_id=%s turn=%s",
        session_id,
        trace_id,
        turn,
    )
    return PipelineResult(
        reply=f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}",
        trace_id=trace_id,
        degraded=False,
        failed_filter=None,
    )


class PipelineOrchestrator:
    """顺序编排五过滤器链，统一异常捕获与逐跳 deadline 预算。"""

    def __init__(
        self,
        filters: list[Filter] | None = None,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        handoff_sender: HandoffCommandSender | None = None,
        handoff_status_reader: HandoffStatusReader | None = None,
    ) -> None:
        self._filters: list[Filter] = (
            filters
            if filters is not None
            else [
                # Story 6.1（AD-11）：指代消解过滤器占据链首——默认
                # 透传（恒等消解），resolved_message 为 intent_router
                # 唯一分类输入；编排器逐跳超时/异常兜底不变。
                CoreferenceFilter(),
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
        # Story 7.2（D1）：编排器级共享执行池——替代每请求新建
        # ThreadPoolExecutor + shutdown(wait=False)（压测实证线程随
        # 并发线性膨胀，见模块 docstring）。池大小 = max(1, len(filters))：
        # 顺序跳提交同一时刻每请求至多占用一个 worker（慢调用由
        # llm 客户端自有有界池承接，不占本池等待），进程内线程数有界；
        # 池随编排器生命周期共存（atexit 回收空闲 worker，进程退出
        # 不悬挂），空闲零开销（ThreadPoolExecutor 按需起线程）。
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, len(self._filters)),
            thread_name_prefix="weiliao-pipeline",
        )
        # Story 5.2（AD-7）：None（broker 未配置）时出口分支跳过发送，
        # 出口文案语义不变（与消费者门控同口径）。
        self._handoff_sender = handoff_sender
        # Story 5.5：终态闸门状态读取引用。None（未注入，本地/测试
        # 组装）时闸门禁用——行为与 5.5 之前逐字节一致。
        self._handoff_status_reader = handoff_status_reader

    @property
    def filter_names(self) -> tuple[str, ...]:
        """过滤器链名单（只读），供健康检查探针等链外只读消费。"""
        return tuple(filter_.name for filter_ in self._filters)

    def run(self, envelope: Envelope) -> PipelineResult:
        """执行过滤器链；异常/超时一律降级模板兜底，不向上抛。"""
        # trace_id 单一权威生成点：缺失即由服务生成回填（32 位小写
        # 十六进制，Langfuse trace id 口径）；payload 携带值透传。
        # Story 7.2（D3）：frozen 信封禁止原地写——回填经 model_copy
        # 整信封替换（局部变量重新绑定，下游行为逐字节不变）。
        if envelope.trace_id is None:
            envelope = envelope.model_copy(update={"trace_id": tracing.new_trace_id()})
        assert envelope.trace_id is not None  # mypy 收窄：上方已权威回填
        trace_id = envelope.trace_id
        # Story 5.5 编排器级终态闸门：对直接调用 run() 的入口（绕过
        # 5.3 http 适配器闸门）实读 Redis 权威 handoff_status——
        # 已转接会话绝不进 AI 管道（防人机混答，FR-12 红线）。判定
        # 只认 Redis 实读（不缓存、不读 L1 快照）；未知值与读取失败
        # 一律 fail-closed 既有兜底（绝不放行管道）。
        if self._handoff_status_reader is not None:
            try:
                handoff_status = self._handoff_status_reader.read_handoff_status(
                    envelope.session_id
                )
            except Exception:
                # reader 读取失败（Redis 故障、解码错误等任何异常——
                # HandoffStatusReader 契约：读取失败由编排器闸门
                # fail-closed 收口）与未知状态值同一兜底口径（degraded
                # 模板兜底 + 原 trace_id），绝不人机混答、绝不向上抛。
                return self._degraded(
                    envelope, trace_id, failed_filter="handoff_gate"
                )
            if handoff_status == "triggered":
                # 短路：不打开 trace_message、不进 _run_chain——全部
                # 过滤器零执行、零 LLM、零模板兜底，无二次转交指令；
                # 应答与事件/日志经共享 helper 与 5.3 闸门逐字节一致。
                return handoff_gate_result(
                    trace_id,
                    session_id=envelope.session_id,
                    turn=envelope.turn,
                )
            if handoff_status is not None and handoff_status != "none":
                # 未知状态值（Redis 损坏值）：fail-closed 与读取失败
                # 同一兜底口径，绝不放行 AI 管道。
                logger.warning(
                    "unknown handoff_status=%r; fail-closed fallback "
                    "session_id=%s trace_id=%s turn=%s",
                    handoff_status,
                    envelope.session_id,
                    trace_id,
                    envelope.turn,
                )
                return self._degraded(
                    envelope, trace_id, failed_filter="handoff_gate"
                )
            # "none" / None（会话过期）：放行正常管道，行为与 5.5 前
            # 逐字节一致（编排器不做 begin_turn/会话写，AD-4 单写者
            # 仍归入口适配器）。
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
        last_filter: Filter | None = None
        try:
            for filter_ in self._filters:
                last_filter = filter_
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 剩余预算耗尽：立即兜底，不傻等。
                    return self._degraded(envelope, trace_id, slow_filter=filter_.name)
                future = self._executor.submit(filter_.process, envelope)
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
