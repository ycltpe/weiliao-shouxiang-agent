"""受控生成过滤器（Story 4.3，影子接入决策 A）。

管道固定链 cache_lookup 之后、risk_review 之前的 LLM 生成落点：输入
归一（Story 6.3 终态：``resolved_message`` 为唯一生成输入，``None``/
空白跳过生成、不产生草稿，买家原文 ``message`` 在任何路径零读取，
FR-6 纯消解输入——链首 coreference 过滤器 fail-safe 恒产出
resolved_message）→ 红线意图（NEGOTIATION / LOAN_POLICY /
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

Story 4.5：可选注入熔断器（``llm.circuit_breaker.LlmCircuitBreaker``，
None=不熔断，保持既有测试与惰性默认路径语义）。请求前 ``before_request()``
咨询：False 即跳过生成——不调子图、零 I/O、信封原样透传（切换 <100ms
的前提是 open 期间零子图调用），并记 DEFAULT 级 skip 事件；生成成功
``on_success()``（grounding 未过属内容问题、中性不信号，成功口径=
子图未抛 OpenAI 故障）；生成异常按 ``_failure_reason`` 归一后
``on_failure(reason)``，仅 OpenAI 侧故障词表跳闸（tripped 事件 ERROR 级），
确定性失败不跳闸。熔断咨询/信号自身全部 fail-safe（异常吞掉、按放行
处理），绝不让请求路径因熔断逻辑抛错。
"""

from weiliao_agent.filters.base import Filter
from weiliao_agent.llm import LlmGenerationError
from weiliao_agent.llm import generate as _module_generate
from weiliao_agent.llm.circuit_breaker import LlmCircuitBreaker
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

    def __init__(
        self,
        subgraph: LlmGenerationSubgraph | None = None,
        breaker: LlmCircuitBreaker | None = None,
    ) -> None:
        # 组合根注入定制子图；缺省 None 走 llm 模块级惰性入口（装配期
        # 不触达 OpenAI 凭据，同 3.3 cache_lookup 注入模式）。
        # breaker 可选注入（Story 4.5）：None=不熔断，装配期不构造亦可。
        self._subgraph = subgraph
        self._breaker = breaker

    def process(self, envelope: Envelope) -> Envelope:
        if envelope.intent in _REDLINE_INTENTS:
            # 红线意图优先于 LLM 链路：不调模型、不产生草稿。
            return envelope
        if envelope.media_type is not None and envelope.media_type != "text":
            # 非文本输入（图片/语音等）不进生成链路。
            return envelope
        message = envelope.resolved_message
        if message is None or not message.strip():
            # 消解结果缺失/空白（FR-11 终态口径）：跳过生成、不产生
            # 草稿，信封原样透传、不触达模型；买家原文不做决策性读取。
            return envelope
        breaker = self._breaker
        if breaker is not None and not self._consult_breaker(breaker, envelope):
            # 熔断拒绝：open/half-open 未放量，不调子图、零 I/O、透传。
            return envelope
        try:
            text = self._generate(message, envelope)
        except Exception as exc:
            # 全路径 fail-safe：凭据缺失/超时/网络错误一律吞掉，草稿
            # 为 None；仅记可观测事件，买家应答与 degraded 语义不动。
            reason = _failure_reason(exc)
            tracing.record_llm_generate_event(
                envelope.trace_id,
                outcome="generation_error",
                reason=reason,
            )
            if breaker is not None:
                self._signal_breaker_failure(breaker, envelope, reason)
            return envelope
        if breaker is not None:
            # 成功信号口径：子图未抛 OpenAI 故障即算成功（grounding
            # 校验属内容问题，中性，不阻断恢复推进）。
            self._signal_breaker_success(breaker, envelope)
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

    def _consult_breaker(self, breaker: LlmCircuitBreaker, envelope: Envelope) -> bool:
        """熔断咨询：False 即跳过生成（不调子图、信封原样透传）。

        咨询自身 fail-safe：breaker 异常吞掉并按放行处理（熔断器是
        优化层，故障时退化回既有傻等预算路径，绝不反过来阻断请求）；
        拒绝路径记 DEFAULT 级 skip 事件（open 期跳过记 ERROR 会成噪音）。
        """
        try:
            admitted = breaker.before_request()
        except Exception:
            return True
        if admitted:
            return True
        tracing.record_llm_breaker_event(envelope.trace_id, outcome="skip")
        return False

    def _signal_breaker_failure(
        self, breaker: LlmCircuitBreaker, envelope: Envelope, reason: str
    ) -> None:
        """故障信号 fail-safe：仅故障词表跳闸时记 ERROR 级 tripped 事件。"""
        try:
            tripped = breaker.on_failure(reason)
        except Exception:
            return
        if tripped:
            tracing.record_llm_breaker_event(
                envelope.trace_id, outcome="tripped", reason=reason
            )

    def _signal_breaker_success(
        self, breaker: LlmCircuitBreaker, envelope: Envelope
    ) -> None:
        """成功信号 fail-safe：推进恢复至 100% 闭合时记 recovered 事件。"""
        try:
            recovered = breaker.on_success()
        except Exception:
            return
        if recovered:
            tracing.record_llm_breaker_event(envelope.trace_id, outcome="recovered")


def _failure_reason(exc: Exception) -> str:
    """归一异常为 trace reason：统一错误自报 reason，其余用类型名。"""
    if isinstance(exc, LlmGenerationError):
        return exc.reason
    return type(exc).__name__
