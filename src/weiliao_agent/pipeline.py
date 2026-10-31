"""管道编排器（Story 1.2 核心交付）。

手写纯 Python 编排（AD-2，无 LangGraph Runtime 依赖）：顺序调用
intent_router → cache_lookup → risk_review → response_assembler，
过滤器间仅经信封传递。

统一错误兜底：
- 任一过滤器抛异常 → 捕获并降级模板兜底，跳过剩余过滤器；
- 端到端 deadline（默认内部预算 2.5s，为 NFR-1 P99 < 3s 留出口余量，
  可配置）逐跳检查剩余预算，每跳以 ``future.result(timeout=剩余预算)``
  执行，超时立即兜底、不傻等全量超时，执行线程结果丢弃（占位过滤器
  无副作用，可安全丢弃）。

买家侧永不暴露 5xx：所有路径返回 ``PipelineResult``，错误应答携带
trace_id 且 ``degraded=True``。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass

from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.cache_lookup import CacheLookupFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import RiskReviewFilter
from weiliao_agent.filters.templates import render_placeholder
from weiliao_agent.models.envelope import Envelope

# 端到端内部预算：NFR-1 P99 < 3s 的出口余量口径，可配置。
DEFAULT_BUDGET_SECONDS = 2.5


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
    """顺序编排四过滤器链，统一异常捕获与逐跳 deadline 预算。"""

    def __init__(
        self,
        filters: list[Filter] | None = None,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    ) -> None:
        self._filters: list[Filter] = (
            filters
            if filters is not None
            else [
                IntentRouterFilter(),
                CacheLookupFilter(),
                RiskReviewFilter(),
                ResponseAssemblerFilter(),
            ]
        )
        self._budget_seconds = budget_seconds

    def run(self, envelope: Envelope) -> PipelineResult:
        """执行过滤器链；异常/超时一律降级模板兜底，不向上抛。"""
        deadline = time.monotonic() + self._budget_seconds
        executor = ThreadPoolExecutor(max_workers=max(1, len(self._filters)))
        last_filter: Filter | None = None
        try:
            for filter_ in self._filters:
                last_filter = filter_
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 剩余预算耗尽：立即兜底，不傻等。
                    return self._degraded(envelope, slow_filter=filter_.name)
                future = executor.submit(filter_.process, envelope)
                try:
                    envelope = future.result(timeout=remaining)
                except FutureTimeoutError:
                    return self._degraded(envelope, slow_filter=filter_.name)
                except Exception:
                    return self._degraded(envelope, failed_filter=filter_.name)
        except Exception:
            # future.result 之外的逃逸异常（如过滤器未定义 name 触发
            # AttributeError）：统一兜底，异常不向上抛（I/O 矩阵约定）。
            if last_filter is None:
                culprit = "pipeline"
            else:
                culprit = getattr(last_filter, "name", None) or type(last_filter).__name__
            return self._degraded(envelope, failed_filter=culprit)
        finally:
            # 超时跳的执行线程结果已丢弃，等待其完成无意义（不傻等）。
            executor.shutdown(wait=False)
        return PipelineResult(
            reply=render_placeholder(envelope.intent),
            trace_id=envelope.trace_id,
            degraded=False,
            failed_filter=None,
        )

    @staticmethod
    def _degraded(
        envelope: Envelope,
        *,
        failed_filter: str | None = None,
        slow_filter: str | None = None,
    ) -> PipelineResult:
        """构造兜底应答：跳过剩余过滤器，携带 trace_id 与降级归因。"""
        culprit = failed_filter if failed_filter is not None else slow_filter
        return PipelineResult(
            reply=render_placeholder(envelope.intent),
            trace_id=envelope.trace_id,
            degraded=True,
            failed_filter=culprit,
        )
