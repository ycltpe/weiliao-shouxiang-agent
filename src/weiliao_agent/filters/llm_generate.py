"""受控生成过滤器（Story 4.3，影子接入决策 A）。

管道固定链 cache_lookup 之后、risk_review 之前的 LLM 生成落点：输入
归一（``resolved_message`` 为 None 时回落 ``message``；指代消解属
FR-11 前置，本 Story 不实现）→ 红线意图（NEGOTIATION / LOAN_POLICY /
COMPLAINT，红线优先于 LLM 链路）与 ``media_type`` 非 text、空消息跳过
→ 调生成子图（trace 埋点在子图内，Story 4.2 既有）→ 确定性 grounding
校验（``llm.grounding.verify_grounding``，fail-closed）→ 通过则草稿
经 ``model_copy`` 写入信封 ``generated_reply``。

影子接入口径（本 Story 全链路 fail-safe，绝不向上抛）：
- 未注入子图：走 llm 模块级惰性入口（首次调用才装配默认子图，
  同 3.3 cache_lookup 缺省注入模式）；
- 生成异常（含凭据缺失、超时、网络错误）：吞异常，草稿为 None；
- 校验未过：丢弃草稿；
- 以上故障仅记 ``tracing.record_llm_generate_event`` 可观测事件，
  买家可见应答、``degraded`` 语义一律不动——4.4 审核就位前 LLM
  内容在任何路径都不得未经审核触达买家，本过滤器不消费也不产出
  买家可见文本。
"""

from weiliao_agent.filters.base import Filter
from weiliao_agent.llm import LlmGenerationError
from weiliao_agent.llm import generate as _module_generate
from weiliao_agent.llm.grounding import verify_grounding
from weiliao_agent.llm.subgraph import LlmGenerationSubgraph
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentTag
from weiliao_agent.observability import tracing

# 红线意图：Epic 5 硬触发落地前的先行把守，一律跳过生成、不产生草稿。
_REDLINE_INTENTS: frozenset[IntentTag] = frozenset(
    {"NEGOTIATION", "LOAN_POLICY", "COMPLAINT"}
)


class LlmGenerateFilter(Filter):
    """受控生成：草稿挂信封 generated_reply；买家可见语义不动。"""

    name = "llm_generate"

    def __init__(self, subgraph: LlmGenerationSubgraph | None = None) -> None:
        # 组合根注入定制子图；缺省 None 走 llm 模块级惰性入口（装配期
        # 不触达 OpenAI 凭据，同 3.3 cache_lookup 注入模式）。
        self._subgraph = subgraph

    def process(self, envelope: Envelope) -> Envelope:
        if envelope.intent in _REDLINE_INTENTS:
            # 红线意图优先于 LLM 链路：不调模型、不产生草稿。
            return envelope
        if envelope.media_type is not None and envelope.media_type != "text":
            # 非文本输入（图片/语音等）不进生成链路。
            return envelope
        message = envelope.resolved_message
        if message is None or not message.strip():
            # 指代消解过滤器（FR-11）未就位（或消解结果为空串/纯空白）：
            # 回落买家原始消息。
            message = envelope.message
        if message is None or not message.strip():
            # 空/空白消息：跳过生成，信封原样透传，不触达模型。
            return envelope
        try:
            text = self._generate(message, envelope)
        except Exception as exc:
            # 全路径 fail-safe：凭据缺失/超时/网络错误一律吞掉，草稿
            # 为 None；仅记可观测事件，买家应答与 degraded 语义不动。
            tracing.record_llm_generate_event(
                envelope.trace_id,
                outcome="generation_error",
                reason=_failure_reason(exc),
            )
            return envelope
        problem = verify_grounding(text)
        if problem is not None:
            # fail-closed：违禁表述/标注泄漏/链接命中即丢弃草稿。
            tracing.record_llm_generate_event(
                envelope.trace_id,
                outcome="grounding_failed",
                reason=problem,
            )
            return envelope
        return envelope.model_copy(update={"generated_reply": text})

    def _generate(self, message: str, envelope: Envelope) -> str:
        if self._subgraph is not None:
            return self._subgraph.generate(
                message,
                fact_card=envelope.fact_card,
                intent=envelope.intent,
                trace_id=envelope.trace_id,
            )
        return _module_generate(
            message,
            fact_card=envelope.fact_card,
            intent=envelope.intent,
            trace_id=envelope.trace_id,
        )


def _failure_reason(exc: Exception) -> str:
    """归一异常为 trace reason：统一错误自报 reason，其余用类型名。"""
    if isinstance(exc, LlmGenerationError):
        return exc.reason
    return type(exc).__name__
