"""健康检查端点（挂在响应组装过滤器）。"""

from weiliao_agent.health.probe import is_ready

__all__ = ["is_ready"]
