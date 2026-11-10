"""后置风控审核过滤器单测（Story 4.4，AC 第 1/2/3 条 + I/O 矩阵；
Story 4.7 空草稿拦截扩）。

替身客户端（恒放行 / 剧本归一 / 抛错）覆盖矩阵全分支：草稿过审保留、
拒绝/超时/异常清草稿回模板兜底、无草稿不调中台不计分母原样透传、
客户端逃逸异常绝对 fail-safe；空串/纯空白草稿按无草稿拦截（不调中台、
不计分母、清回 None、记 draft_empty 事件，标准管道单事件）；trace 事件
（outcome/latency_ms/reason/级别）与通过率指标计数经共享替身断言。全部无网络。
"""

from collections.abc import Iterator
from typing import Any

import pytest
from conftest import draft_empty_events

from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import RiskReviewFilter, RiskReviewVerdict
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.pipeline import PipelineOrchestrator

DRAFT = "您好，这套房源目前在售，价格以页面展示为准。"


class _StubClient:
    """审核客户端替身：剧本化 verdict 或可抛错；记录收到的调用。"""

    def __init__(
        self,
        verdict: RiskReviewVerdict | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict
        self._error = error

    def review(
        self, *, text: str, intent: str | None, trace_id: str | None, house_id: str
    ) -> RiskReviewVerdict:
        self.calls.append(
            {"text": text, "intent": intent, "trace_id": trace_id, "house_id": house_id}
        )
        if self._error is not None:
            raise self._error
        assert self._verdict is not None
        return self._verdict


def _envelope(generated_reply: str | None = DRAFT) -> Envelope:
    return Envelope.model_validate(
        {
            "session_id": "sess-001",
            "house_id": "house-001",
            "timestamp": "2026-11-06T10:00:00+08:00",
            "trace_id": "ab" * 16,
            "intent": "PRICE_INQUIRY",
            "message": "这套房多少钱？",
            "generated_reply": generated_reply,
        }
    )


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    metrics_module.reset_risk_review_metrics()
    yield
    metrics_module.reset_risk_review_metrics()


def test_passed_draft_kept_on_envelope() -> None:
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))
    envelope = _envelope()

    result = RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    assert result.generated_reply == DRAFT  # 草稿保留，待编排器最终组装
    assert client.calls[0]["text"] == DRAFT
    assert client.calls[0]["intent"] == "PRICE_INQUIRY"
    assert client.calls[0]["trace_id"] == "ab" * 16
    assert client.calls[0]["house_id"] == "house-001"
    snapshot = metrics_module.get_risk_review_metrics().snapshot()
    assert snapshot["total"] == 1
    assert snapshot["passed"] == 1
    assert snapshot["pass_rate"] == 1.0


@pytest.mark.parametrize(
    "verdict",
    [
        RiskReviewVerdict(passed=False, outcome="rejected", latency_ms=1.0, reason="违禁表述"),
        RiskReviewVerdict(
            passed=False, outcome="timeout", latency_ms=500.0, reason="review exceeded 500ms"
        ),
        RiskReviewVerdict(passed=False, outcome="error", latency_ms=1.0, reason="network"),
    ],
    ids=["rejected", "timeout", "error"],
)
def test_not_passed_draft_cleared_to_none(verdict: RiskReviewVerdict) -> None:
    client = _StubClient(verdict)
    envelope = _envelope()

    result = RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    # 草稿作废：信封清回 None，买家应答由编排器回模板兜底；degraded 语义
    # 不动（审核拒绝属设计内行为）。
    assert result.generated_reply is None
    assert envelope.generated_reply == DRAFT  # 原信封不被就地改写
    snapshot = metrics_module.get_risk_review_metrics().snapshot()
    assert snapshot["total"] == 1
    assert snapshot["passed"] == 0
    assert snapshot[verdict.outcome] == 1  # 拒绝/超时/错误计入失败


def test_no_draft_passthrough_without_calling_client() -> None:
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))
    envelope = _envelope(generated_reply=None)

    result = RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    assert result is envelope  # 原样透传
    assert client.calls == []  # 不调中台
    snapshot = metrics_module.get_risk_review_metrics().snapshot()
    assert snapshot["total"] == 0  # 不计通过率分母


@pytest.mark.parametrize("blank", ["", " ", "  \n\t ", "\t\n  "])
def test_blank_draft_intercepted_without_calling_client(
    blank: str, fake_langfuse: Any
) -> None:
    # 空草稿拦截：不调中台、不计通过率分母、清回 None、记 draft_empty 事件
    # （stage=risk_review）；与审核不通过清稿口径一致（model_copy 新信封）。
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))
    envelope = _envelope(generated_reply=blank)

    result = RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    assert result is not envelope
    assert result.generated_reply is None  # 草稿作废清回 None
    assert envelope.generated_reply == blank  # 原信封不被就地改写
    assert client.calls == []  # 不调中台
    snapshot = metrics_module.get_risk_review_metrics().snapshot()
    assert snapshot["total"] == 0  # 不计通过率分母
    events = draft_empty_events(fake_langfuse)
    assert len(events) == 1
    assert events[0]["level"] == "DEFAULT"
    assert events[0]["metadata"]["event"] == "draft_empty_event"
    assert events[0]["metadata"]["stage"] == "risk_review"


def test_none_draft_records_no_draft_empty_event(fake_langfuse: Any) -> None:
    # None 不算拦截：零新增行为、不记 draft_empty 事件。
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))

    RiskReviewFilter(client=client).process(_envelope(generated_reply=None))  # type: ignore[arg-type]

    assert draft_empty_events(fake_langfuse) == []


def test_blank_draft_event_record_failure_does_not_break_intercept(fake_langfuse: Any) -> None:
    # fail-safe：事件记录自身再抛也吞掉，拦截（清回 None）照常。
    fake_langfuse.raise_on_event = True
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))

    result = RiskReviewFilter(client=client).process(_envelope(generated_reply=""))  # type: ignore[arg-type]

    assert result.generated_reply is None
    assert client.calls == []


def test_client_escape_normalized_to_not_passed() -> None:
    client = _StubClient(error=RuntimeError("client boom"))
    envelope = _envelope()

    result = RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    # 绝对 fail-safe：逃逸异常归一为不通过，绝不向上抛。
    assert result.generated_reply is None
    snapshot = metrics_module.get_risk_review_metrics().snapshot()
    assert snapshot["error"] == 1


def test_trace_event_recorded_with_outcome_latency_and_reason(fake_langfuse: Any) -> None:
    client = _StubClient(
        RiskReviewVerdict(passed=False, outcome="rejected", latency_ms=42.0, reason="违禁表述")
    )
    envelope = _envelope()

    RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    events = [e for e in fake_langfuse.events if e["name"] == "risk_review.event"]
    assert len(events) == 1
    assert events[0]["level"] == "ERROR"
    assert events[0]["metadata"]["event"] == "risk_review_event"
    assert events[0]["metadata"]["outcome"] == "rejected"
    assert events[0]["metadata"]["latency_ms"] == 42.0
    assert events[0]["metadata"]["reason"] == "违禁表述"


def test_passed_trace_event_is_default_level(fake_langfuse: Any) -> None:
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))
    envelope = _envelope()

    RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    events = [e for e in fake_langfuse.events if e["name"] == "risk_review.event"]
    assert len(events) == 1
    assert events[0]["level"] == "DEFAULT"
    assert events[0]["metadata"]["reason"] is None


def test_trace_record_failure_does_not_affect_verdict(fake_langfuse: Any) -> None:
    fake_langfuse.raise_on_event = True  # 记录本身抛错也须吞掉（fail-safe）
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))
    envelope = _envelope()

    result = RiskReviewFilter(client=client).process(envelope)  # type: ignore[arg-type]

    assert result.generated_reply == DRAFT


def test_full_pipeline_passed_draft_delivered_with_ai_prefix(fake_langfuse: Any) -> None:
    """AC 第 1 条端到端：过审草稿经编排器最终组装点携带 AI 前缀下发。"""
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=3.0))
    orchestrator = PipelineOrchestrator(
        filters=[RiskReviewFilter(client=client), ResponseAssemblerFilter()]  # type: ignore[list-item]
    )

    result = orchestrator.run(_envelope())

    assert result.degraded is False
    assert result.failed_filter is None
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{DRAFT}"
    # trace 留有审核 passed 事件（含 latency_ms）。
    events = [e for e in fake_langfuse.events if e["name"] == "risk_review.event"]
    assert len(events) == 1
    assert events[0]["metadata"]["outcome"] == "passed"
    assert events[0]["metadata"]["latency_ms"] == 3.0


def test_full_pipeline_rejected_draft_falls_back_to_template(fake_langfuse: Any) -> None:
    """AC 第 2 条端到端：审核拒绝 → 买家模板兜底、degraded=False、trace 失败事件。"""
    client = _StubClient(
        RiskReviewVerdict(passed=False, outcome="rejected", latency_ms=7.0, reason="违禁表述")
    )
    orchestrator = PipelineOrchestrator(
        filters=[RiskReviewFilter(client=client), ResponseAssemblerFilter()]  # type: ignore[list-item]
    )

    result = orchestrator.run(_envelope())

    assert result.degraded is False  # 审核拒绝属设计内行为，非服务故障
    assert result.failed_filter is None
    assert result.reply == render_fallback("PRICE_INQUIRY")  # 草稿不触达买家
    events = [e for e in fake_langfuse.events if e["name"] == "risk_review.event"]
    assert len(events) == 1
    assert events[0]["level"] == "ERROR"
    assert events[0]["metadata"]["outcome"] == "rejected"
    assert events[0]["metadata"]["reason"] == "违禁表述"


def test_full_pipeline_blank_draft_single_event_and_template_fallback(fake_langfuse: Any) -> None:
    """AC 4 端到端：空草稿 risk_review 拦截后组装器不再二次记录——全程仅一条
    draft_empty 事件；买家收到完整模板兜底应答（含前缀），degraded=False。"""
    client = _StubClient(RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0))
    orchestrator = PipelineOrchestrator(
        filters=[RiskReviewFilter(client=client), ResponseAssemblerFilter()]  # type: ignore[list-item]
    )

    result = orchestrator.run(_envelope(generated_reply="  \n "))

    assert result.degraded is False
    assert result.failed_filter is None
    assert result.reply == render_fallback("PRICE_INQUIRY")
    assert client.calls == []  # 中台零调用
    assert metrics_module.get_risk_review_metrics().snapshot()["total"] == 0  # 零分母
    events = draft_empty_events(fake_langfuse)
    assert len(events) == 1  # 标准管道该消息全程仅一条 draft_empty 事件
    assert events[0]["metadata"]["stage"] == "risk_review"
