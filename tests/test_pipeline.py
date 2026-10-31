"""管道编排 I/O 矩阵单测（Story 1.2）。

覆盖 spec I/O & Edge-Case Matrix 四场景：
HAPPY_PATH / FILTER_RAISES / DEADLINE_EXCEEDED / INVALID_INPUT，
外加占位模板集选择键（9 类意图 / 缺失 / 未知值）与 AI 身份披露前缀。
"""

import time
from typing import Any

from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_placeholder
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.pipeline import PipelineOrchestrator


def _valid_payload(intent: str | None = "PRICE_INQUIRY") -> dict[str, Any]:
    return {
        "session_id": "sess-001",
        "house_id": "house-001",
        "turn": 1,
        "intent": intent,
        "timestamp": "2026-10-31T10:00:00+08:00",
        "trace_id": "trace-001",
    }


class _RecordingFilter(Filter):
    """透传过滤器，记录执行顺序，用于断言剩余过滤器被跳过。"""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    def process(self, envelope: Envelope) -> Envelope:
        self._log.append(self.name)
        return envelope


class _RaisingFilter(Filter):
    def __init__(self, name: str = "intent_router") -> None:
        self.name = name

    def process(self, envelope: Envelope) -> Envelope:
        msg = "boom"
        raise RuntimeError(msg)


class _SlowFilter(Filter):
    def __init__(self, name: str = "cache_lookup", delay_seconds: float = 1.0) -> None:
        self.name = name
        self._delay_seconds = delay_seconds

    def process(self, envelope: Envelope) -> Envelope:
        time.sleep(self._delay_seconds)
        return envelope


def test_happy_path_runs_full_chain_and_returns_placeholder_reply() -> None:
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            _RecordingFilter("intent_router", log),
            _RecordingFilter("cache_lookup", log),
            _RecordingFilter("risk_review", log),
            _RecordingFilter("response_assembler", log),
        ]
    )
    envelope = Envelope.model_validate(_valid_payload())

    result = orchestrator.run(envelope)

    assert log == ["intent_router", "cache_lookup", "risk_review", "response_assembler"]
    assert result.degraded is False
    assert result.failed_filter is None
    assert result.trace_id == "trace-001"
    assert result.reply.startswith(AI_DISCLOSURE_PREFIX)
    assert result.reply != render_placeholder(None)  # 命中意图文案而非通用兜底


def test_filter_exception_degrades_and_skips_remaining_filters() -> None:
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            _RecordingFilter("intent_router", log),
            _RaisingFilter("cache_lookup"),
            _RecordingFilter("risk_review", log),
            _RecordingFilter("response_assembler", log),
        ]
    )
    envelope = Envelope.model_validate(_valid_payload())

    result = orchestrator.run(envelope)

    assert result.degraded is True
    assert result.failed_filter == "cache_lookup"
    assert result.trace_id == "trace-001"
    assert result.reply.startswith(AI_DISCLOSURE_PREFIX)
    assert log == ["intent_router"]  # 剩余过滤器未执行


def test_slow_filter_times_out_immediately_without_waiting() -> None:
    log: list[str] = []
    budget = 0.2
    orchestrator = PipelineOrchestrator(
        filters=[
            _RecordingFilter("intent_router", log),
            _SlowFilter("cache_lookup", delay_seconds=5.0),
            _RecordingFilter("risk_review", log),
        ],
        budget_seconds=budget,
    )
    envelope = Envelope.model_validate(_valid_payload())

    started = time.monotonic()
    result = orchestrator.run(envelope)
    elapsed = time.monotonic() - started

    assert result.degraded is True
    assert result.failed_filter == "cache_lookup"
    assert result.trace_id == "trace-001"
    assert result.reply.startswith(AI_DISCLOSURE_PREFIX)
    assert log == ["intent_router"]
    # 不傻等：总耗时逼近内部预算，远小于慢过滤器的 5s。
    assert elapsed < budget + 0.5


def test_invalid_input_fails_closed_with_fallback_and_fresh_trace_id() -> None:
    client = TestClient(create_app())

    # 请求自带 trace_id 但缺其他必填字段：断言返回的是新生成 trace_id，
    # 而非原样带回客户端传入值。
    response = client.post("/v1/messages", json={"session_id": "sess-001", "trace_id": "trace-001"})

    assert response.status_code == 200  # 非 5xx
    body = response.json()
    assert body["degraded"] is True
    assert body["trace_id"]  # 新生成 trace_id
    assert body["trace_id"] != "trace-001"
    assert body["reply"].startswith(AI_DISCLOSURE_PREFIX)
    assert body["failed_filter"] is None


def test_malformed_json_fails_closed_with_fallback() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/v1/messages",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200  # 非 5xx
    body = response.json()
    assert body["degraded"] is True
    assert body["trace_id"]  # 新生成 trace_id
    assert body["reply"].startswith(AI_DISCLOSURE_PREFIX)


def test_invalid_input_bad_type_fails_closed() -> None:
    client = TestClient(create_app())
    payload = _valid_payload()
    payload["trace_id"] = {"not": "a string"}

    response = client.post("/v1/messages", json=payload)

    assert response.status_code == 200
    assert response.json()["degraded"] is True


def test_valid_input_via_endpoint_executes_pipeline() -> None:
    client = TestClient(create_app())

    response = client.post("/v1/messages", json=_valid_payload("GREETING"))

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is False
    assert body["failed_filter"] is None
    assert body["trace_id"] == "trace-001"
    assert body["reply"] == render_placeholder("GREETING")


def test_default_chain_intent_router_defaults_missing_intent_to_unknown() -> None:
    orchestrator = PipelineOrchestrator()
    envelope = Envelope.model_validate(_valid_payload(intent=None))

    result = orchestrator.run(envelope)

    assert result.degraded is False
    assert result.reply == render_placeholder("UNKNOWN")


def test_placeholder_template_selection_keys() -> None:
    # 9 类意图各一条文案，全部携带 AI 身份披露前缀。
    bodies: set[str] = set()
    for intent in (
        "GREETING",
        "PRICE_INQUIRY",
        "AVAILABILITY",
        "LOCATION",
        "PHOTO_REQUEST",
        "NEGOTIATION",
        "LOAN_POLICY",
        "COMPLAINT",
        "UNKNOWN",
    ):
        text = render_placeholder(intent)
        assert text.startswith(AI_DISCLOSURE_PREFIX)
        assert text != render_placeholder(None)
        # 正文非空，且各意图文案互不重复（防空文案/静默复制）。
        assert len(text) > len(AI_DISCLOSURE_PREFIX)
        bodies.add(text)
    assert len(bodies) == 9

    # intent 缺失或不在 9 类枚举内：一律通用兜底。
    assert render_placeholder(None) == render_placeholder("NOT_AN_INTENT")
