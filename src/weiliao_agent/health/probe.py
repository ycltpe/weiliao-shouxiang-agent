"""就绪探针（Story 1.4）。

判定口径即 AC 的 Given：服务已启动且管道编排完成。架构约定健康检查
"挂在响应组装过滤器"，字面落点为过滤器链完整且含 ``response_assembler``
过滤器。就绪判定不纳入 Redis 连通性：Redis 故障按既有 fail-closed 设计
逐消息降级应答，服务仍可受理流量，探针不应因此摘除全部实例。
"""

from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.pipeline import PipelineOrchestrator

# 就绪所依赖的末位过滤器（架构：端点挂在响应组装过滤器）；
# 直接引用过滤器类属性，避免过滤器重命名时判定键失同步。
READY_FILTER_NAME = ResponseAssemblerFilter.name


def is_ready(orchestrator: PipelineOrchestrator) -> bool:
    """编排器过滤器链完整（含 response_assembler）即视为就绪。"""
    return READY_FILTER_NAME in orchestrator.filter_names
