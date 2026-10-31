"""风控审核过滤器（占位实现，Story 1.2）。

真实风控审核（AD-8 超时视为不通过）由后续 Epic 填充。占位过滤器输出
视为已审核（占位文案为预审核安全文案），本 Story 透传。
"""

from weiliao_agent.filters.base import Filter
from weiliao_agent.models.envelope import Envelope


class RiskReviewFilter(Filter):
    """占位风控审核：直接透传上游信封。"""

    name = "risk_review"

    def process(self, envelope: Envelope) -> Envelope:
        return envelope
