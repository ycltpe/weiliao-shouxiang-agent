"""响应组装过滤器（占位实现，Story 1.2）。

事实卡片 + 模板/LLM 组装与 AI 身份披露校验（AD-10）由后续 Epic 填充，
本 Story 透传。
"""

from weiliao_agent.filters.base import Filter
from weiliao_agent.models.envelope import Envelope


class ResponseAssemblerFilter(Filter):
    """占位响应组装：直接透传上游信封。"""

    name = "response_assembler"

    def process(self, envelope: Envelope) -> Envelope:
        return envelope
