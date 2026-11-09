"""响应组装过滤器（Story 5.4 真实组装器，AD-10）。

AI 身份披露前缀的唯一注入载体（Epic 技术决策）：risk_review 已放行的
草稿（generated_reply 非 None）加前缀下发，草稿自身已带前缀则跳过防
双标；无草稿（未生成/审核未过）走模板兜底（render_fallback 内部已带
前缀，产出一律原样透传，不二次加注）。组装产出写入
envelope.assembled_reply（权威挂点），编排器最终返回点只读消费。
通过 risk_review 的草稿到达本过滤器后除加前缀外不得再被改写
（4.4 冻结语义）。纯同步、无副作用。
"""

from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.models.envelope import Envelope


class ResponseAssemblerFilter(Filter):
    """真实响应组装：草稿加前缀 / 无草稿模板兜底，产出挂 assembled_reply。"""

    name = "response_assembler"

    def process(self, envelope: Envelope) -> Envelope:
        draft = envelope.generated_reply
        if draft is None:
            assembled = render_fallback(envelope.intent)
        elif draft.startswith(AI_DISCLOSURE_PREFIX):
            assembled = draft  # 草稿已携标：跳过加前缀，防双标
        else:
            assembled = f"{AI_DISCLOSURE_PREFIX}{draft}"
        return envelope.model_copy(update={"assembled_reply": assembled})
