"""缓存查找过滤器（占位实现，Story 1.2）。

L1/L0 缓存分层与事实卡片模型（AD-5）由后续 Epic 填充，本 Story 透传。
"""

from weiliao_agent.filters.base import Filter
from weiliao_agent.models.envelope import Envelope


class CacheLookupFilter(Filter):
    """占位缓存查找：直接透传上游信封。"""

    name = "cache_lookup"

    def process(self, envelope: Envelope) -> Envelope:
        return envelope
