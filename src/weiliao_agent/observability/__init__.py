"""Langfuse trace、监控指标、成本追踪。

Story 1.5 追踪脚手架公共接口面（对编排器与后续 Epic 稳定）：
trace_id 权威生成、消息级 trace 上下文、过滤器 span、降级/下发事件、
成本追踪封装（Epic 4 消费）。全部入口 fail-safe，未配置 Langfuse 时
no-op。详见 ``tracing`` 模块 docstring。
"""

from weiliao_agent.observability.tracing import (
    filter_span,
    get_langfuse,
    new_trace_id,
    record_cache_event,
    record_degradation,
    record_delivery,
    record_generation,
    set_langfuse_client,
    trace_message,
)

__all__ = [
    "record_cache_event",
    "filter_span",
    "get_langfuse",
    "new_trace_id",
    "record_degradation",
    "record_delivery",
    "record_generation",
    "set_langfuse_client",
    "trace_message",
]
