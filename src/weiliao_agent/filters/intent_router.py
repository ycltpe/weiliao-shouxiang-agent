"""意图路由过滤器（占位实现，Story 1.2）。

真实意图分类与硬触发评估（AD-3/AD-12）由后续 Epic 填充。本 Story
最小行为：意图缺失时按 fail-closed 口径置为 UNKNOWN，其余透传。
"""

from weiliao_agent.filters.base import Filter
from weiliao_agent.models.envelope import Envelope


class IntentRouterFilter(Filter):
    """占位意图路由：缺失意图默认 UNKNOWN，信封其余字段透传。"""

    name = "intent_router"

    def process(self, envelope: Envelope) -> Envelope:
        if envelope.intent is None:
            return envelope.model_copy(update={"intent": "UNKNOWN"})
        return envelope
