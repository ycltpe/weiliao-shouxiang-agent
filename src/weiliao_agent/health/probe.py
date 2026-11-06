"""就绪探针（Story 1.4）。

判定口径即 AC 的 Given：服务已启动且管道编排完成。架构约定健康检查
"挂在响应组装过滤器"，字面落点为过滤器链完整且含 ``response_assembler``
过滤器。就绪判定不纳入 Redis 连通性：Redis 故障按既有 fail-closed 设计
逐消息降级应答，服务仍可受理流量，探针不应因此摘除全部实例。

Story 3.4（A 方案）：``cache_metrics_payload`` 提供缓存命中率快照的
/health 响应挂点——压测 ``curl /health`` 即读，分子/分母/``hit_rate``
口径权威定义在 ``observability.metrics``。
"""

from typing import Any

from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.observability.metrics import (
    get_cache_metrics,
    get_risk_review_metrics,
)
from weiliao_agent.pipeline import PipelineOrchestrator

# 就绪所依赖的末位过滤器（架构：端点挂在响应组装过滤器）；
# 直接引用过滤器类属性，避免过滤器重命名时判定键失同步。
READY_FILTER_NAME = ResponseAssemblerFilter.name


def is_ready(orchestrator: PipelineOrchestrator) -> bool:
    """编排器过滤器链完整（含 response_assembler）即视为就绪。"""
    return READY_FILTER_NAME in orchestrator.filter_names


def cache_metrics_payload() -> dict[str, Any]:
    """命中率快照响应载荷（A 方案读数入口）合并进 /health 就绪响应。"""
    return {"cache_metrics": get_cache_metrics().snapshot()}


def risk_review_metrics_payload() -> dict[str, Any]:
    """审核通过率快照响应载荷（Story 4.4）合并进 /health 就绪响应。"""
    return {"risk_review_metrics": get_risk_review_metrics().snapshot()}
