"""后置风控审核过滤器（Story 4.4，双审核第二道，AD-8）。

管道固定链 ``intent_router → cache_lookup → llm_generate → risk_review →
response_assembler`` 的第四跳：消费 4.3 grounding 校验通过的 LLM 草稿
（信封 ``generated_reply``），调风控中台审核客户端放行或作废：

- 草稿非 None 且非空：调中台审核，通过则草稿保留信封（买家可见组装发生在
  编排器最终返回点，本过滤器不产出买家可见文本）；不通过/超时/异常/
  未配置 endpoint 一律 ``model_copy`` 清回 None + 模板兜底（编排器口径），
  审核拒绝属设计内行为：``degraded`` 保持 False，质量信号走 trace
  事件与通过率指标；
- 草稿空串/纯空白（Story 4.7 拦截）：按无草稿同等处理——不调中台、不计
  通过率分母，清回 None 并记 draft_empty 事件，与审核不通过清稿口径一致；
- 草稿为 None（红线意图/非文本/生成失败/校验未过）：原样透传，不调
  中台、不计通过率分母；
- 全路径 fail-safe：客户端逃逸异常也归一为「不通过」，绝不向上抛
  影响编排器；trace 记录与指标计数自身同样不抛。
"""

from threading import Lock

from weiliao_agent.filters.base import Filter, is_blank_draft
from weiliao_agent.filters.risk_review.client import RiskReviewClient, RiskReviewVerdict
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import metrics, tracing

# 模块级惰性默认客户端（同 3.3 cache_lookup / 4.3 llm_generate 缺省注入
# 模式）：装配期不触达中台 endpoint；仅 endpoint 缺省读
# WEILIAO_RISK_REVIEW_ENDPOINT，超时取构造缺省 500ms——env 校验与
# fail-fast 归组合根（main.py）负责，非法配置在启动期拒绝。
# 双检锁防并发首用重复构造（同 llm/__init__.py 惰性入口口径）。
_default_client: RiskReviewClient | None = None
_default_client_lock = Lock()


def _get_default_client() -> RiskReviewClient:
    global _default_client  # noqa: PLW0603
    if _default_client is None:
        with _default_client_lock:
            if _default_client is None:
                _default_client = RiskReviewClient()
    return _default_client


class RiskReviewFilter(Filter):
    """后置审核：通过保留草稿，不通过清草稿回模板兜底（fail-closed）。"""

    name = "risk_review"

    def __init__(self, client: RiskReviewClient | None = None) -> None:
        # 组合根注入经 env 校验的客户端；缺省 None 走模块级惰性入口。
        self._client = client

    def process(self, envelope: Envelope) -> Envelope:
        draft = envelope.generated_reply
        if draft is None:
            # 无草稿：不调中台、不计通过率分母，信封原样透传。
            return envelope
        if is_blank_draft(draft):
            # 空草稿拦截（Story 4.7）：与无草稿同等处理——不调中台、不计
            # 通过率分母，草稿作废清回 None（口径同审核不通过），买家应答由
            # 下游组装器走模板兜底；trace 记 draft_empty 事件（stage 归因）。
            tracing.record_draft_empty_event(envelope.trace_id, stage="risk_review")
            return envelope.model_copy(update={"generated_reply": None})
        verdict = self._review(draft, envelope)
        collector = metrics.get_risk_review_metrics()
        collector.record(verdict.outcome)
        tracing.record_risk_review_event(
            envelope.trace_id,
            outcome=verdict.outcome,
            latency_ms=verdict.latency_ms,
            reason=verdict.reason,
        )
        if verdict.passed:
            return envelope
        # 审核不通过（含超时/异常/未配置 endpoint）：草稿作废，信封清回
        # None，买家应答由编排器最终返回点回模板兜底。
        return envelope.model_copy(update={"generated_reply": None})

    def _review(self, draft: str, envelope: Envelope) -> RiskReviewVerdict:
        try:
            client = self._client if self._client is not None else _get_default_client()
            return client.review(
                text=draft,
                intent=envelope.intent,
                trace_id=envelope.trace_id,
                house_id=envelope.house_id,
            )
        except Exception as exc:
            # 绝对 fail-safe：客户端构造/逃逸异常归一为「不通过」，绝不
            # 向上抛影响编排器降级语义。
            return RiskReviewVerdict(
                passed=False,
                outcome="error",
                latency_ms=0.0,
                reason=type(exc).__name__,
            )
