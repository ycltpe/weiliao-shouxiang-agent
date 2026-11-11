"""管道编排 I/O 矩阵单测（Story 1.2）。

覆盖 spec I/O & Edge-Case Matrix 四场景：
HAPPY_PATH / FILTER_RAISES / DEADLINE_EXCEEDED / INVALID_INPUT，
外加模板集选择键（9 类意图 / 缺失 / 未知值）与 AI 身份披露前缀。
"""

import time
from typing import Any

import fakeredis
from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.coref.rule_based import RuleBasedCorefResolver
from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.coreference import CoreferenceFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentClassification
from weiliao_agent.pipeline import PipelineOrchestrator
from weiliao_agent.session import SessionManager


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


def test_happy_path_runs_full_chain_and_returns_fallback_reply() -> None:
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
    assert result.reply != render_fallback(None)  # 命中意图文案而非通用兜底


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
    assert body["reply"] == render_fallback("GREETING")


def test_default_chain_intent_router_defaults_missing_intent_to_unknown() -> None:
    orchestrator = PipelineOrchestrator()
    envelope = Envelope.model_validate(_valid_payload(intent=None))

    result = orchestrator.run(envelope)

    # Story 6.1：默认链六过滤器口径，顺序固定——coreference 占据链首
    # （AD-11，默认透传），intent_router 之前；llm_generate 影子接入，
    # cache_lookup 与 risk_review 之间。
    assert orchestrator.filter_names == (
        "coreference",
        "intent_router",
        "cache_lookup",
        "llm_generate",
        "risk_review",
        "response_assembler",
    )
    assert result.degraded is False
    assert result.reply == render_fallback("UNKNOWN")


def test_final_assembly_consumes_approved_draft_with_ai_prefix() -> None:
    # Story 5.4：真实 response_assembler 是前缀唯一注入载体——已过审
    # 草稿（generated_reply 非 None）产出 前缀+草稿 挂 assembled_reply，
    # 编排器最终返回点只读消费；无草稿回模板兜底。
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            _RecordingFilter("intent_router", log),
            _RecordingFilter("risk_review", log),
            ResponseAssemblerFilter(),
        ]
    )
    envelope = Envelope.model_validate(
        {**_valid_payload(), "generated_reply": "这套房源目前在售。"}
    )

    result = orchestrator.run(envelope)

    assert result.degraded is False
    assert result.failed_filter is None
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}这套房源目前在售。"


def test_final_assembly_does_not_duplicate_existing_ai_prefix() -> None:
    # code review Epic 4 补丁：LLM 草稿自带【AI 助手】前缀时（prompt 要求
    # 保持 AI 身份，grounding/审核均不拦截此前缀），编排器最终返回点不得
    # 盲加前缀产出双标文案——前缀在全链恰好出现一次。
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            _RecordingFilter("intent_router", log),
            _RecordingFilter("risk_review", log),
            ResponseAssemblerFilter(),
        ]
    )
    envelope = Envelope.model_validate(
        {
            **_valid_payload(),
            "generated_reply": f"{AI_DISCLOSURE_PREFIX}这套房源目前在售。",
        }
    )

    result = orchestrator.run(envelope)

    assert result.degraded is False
    assert result.reply.count(AI_DISCLOSURE_PREFIX) == 1
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}这套房源目前在售。"


def test_final_assembly_falls_back_when_no_approved_draft() -> None:
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            _RecordingFilter("intent_router", log),
            _RecordingFilter("risk_review", log),
            _RecordingFilter("response_assembler", log),
        ]
    )
    envelope = Envelope.model_validate(_valid_payload())

    result = orchestrator.run(envelope)

    assert result.degraded is False
    assert result.failed_filter is None
    assert result.reply == render_fallback("PRICE_INQUIRY")


def test_fallback_template_selection_keys() -> None:
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
        text = render_fallback(intent)
        assert text.startswith(AI_DISCLOSURE_PREFIX)
        assert text != render_fallback(None)
        # 正文非空，且各意图文案互不重复（防空文案/静默复制）。
        assert len(text) > len(AI_DISCLOSURE_PREFIX)
        bodies.add(text)
    assert len(bodies) == 9

    # intent 缺失或不在 9 类枚举内：一律通用兜底。
    assert render_fallback(None) == render_fallback("NOT_AN_INTENT")


# ---------------------------------------------------------------------------
# Story 6.3 管道级端到端：真实 CoreferenceFilter（规则 resolver）+ stub
# 分类器 + fakeredis SessionManager 装配 orchestrator——PRD 小王场景与
# FR-2 指代对的端到端验收面。
# ---------------------------------------------------------------------------


class _StubClassifier:
    """固定标签的 stub 分类器（记录收到的输入文本）。"""

    def __init__(self, tag: str, received: list[str]) -> None:
        self._tag = tag
        self._received = received

    def classify(self, message: str, context: Any) -> IntentClassification:
        self._received.append(message)
        return IntentClassification(tag=self._tag, confidence=0.9)  # type: ignore[arg-type]


class _EnvelopeRecorder(Filter):
    """链尾前的信封捕获替身：记录流经的最后一个信封。"""

    name = "recorder"

    def __init__(self, seen: list[Envelope]) -> None:
        self._seen = seen

    def process(self, envelope: Envelope) -> Envelope:
        self._seen.append(envelope)
        return envelope


def _coref_orchestrator(
    received: list[str],
) -> tuple[PipelineOrchestrator, list[Envelope], fakeredis.FakeRedis]:
    redis_client = fakeredis.FakeRedis()
    manager = SessionManager(redis_client)
    seen: list[Envelope] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            CoreferenceFilter(
                resolver=RuleBasedCorefResolver(),
                coref_writer=manager.write_coref,
            ),
            IntentRouterFilter(
                classifier=_StubClassifier("PRICE_INQUIRY", received),
                followup_recorder=manager.record_followup,
            ),
            _EnvelopeRecorder(seen),
            ResponseAssemblerFilter(),
        ]
    )
    return orchestrator, seen, redis_client


def _message_payload(turn: int, message: str) -> dict[str, Any]:
    return {
        "session_id": "sess-001",
        "house_id": "house-001",
        "turn": turn,
        "timestamp": "2026-11-10T10:00:00+08:00",
        "trace_id": "ab" * 16,
        "message": message,
    }


def test_xiaowang_scenario_pipeline_end_to_end() -> None:
    # PRD 小王场景管道级验收：买家原文 "它采光怎么样" 经真实规则
    # resolver 产出含 house 显式引用的消解文本，stub 分类器只见
    # resolved（原文零接触），应答组装成功。
    received: list[str] = []
    orchestrator, seen, redis_client = _coref_orchestrator(received)
    envelope = Envelope.model_validate(_message_payload(1, "它采光怎么样"))

    result = orchestrator.run(envelope)

    expected_resolved = "这套房（house_id: house-001）采光怎么样"
    assert result.degraded is False
    assert result.failed_filter is None
    assert result.reply.startswith(AI_DISCLOSURE_PREFIX)
    # 分类器决策输入逐字节等于 resolved_message（原文零读取）。
    assert received == [expected_resolved]
    assert seen[0].resolved_message == expected_resolved
    assert seen[0].intent == "PRICE_INQUIRY"
    assert seen[0].coref is not None
    assert [(ref.entity_type, ref.entity_id) for ref in seen[0].coref] == [
        ("house", "house-001")
    ]
    # 追问锚定 Redis 权威值：per-(tag, anchor)=1（首次非追问）。
    key = "weiliao:session:sess-001"
    assert redis_client.hget(key, "followup:PRICE_INQUIRY:house:house-001") == b"1"


def test_fr2_coreferent_pair_second_turn_is_followup_end_to_end() -> None:
    # FR-2 指代对端到端：轮1"它多少钱"（anchor=house-001）后轮2
    # "这套房多少钱"（同锚），分类 tag 相同——轮2 is_followup=True，
    # Redis followup:{TAG}:{anchor}=2：两消息计为同一意图。
    received: list[str] = []
    orchestrator, seen, redis_client = _coref_orchestrator(received)
    turn1 = Envelope.model_validate(_message_payload(1, "它多少钱"))
    turn2 = Envelope.model_validate(
        {**_message_payload(2, "这套房多少钱"), "timestamp": "2026-11-10T10:01:00+08:00"}
    )

    first = orchestrator.run(turn1)
    second = orchestrator.run(turn2)

    assert first.degraded is False
    assert second.degraded is False
    assert seen[0].is_followup is False  # 轮1：首次出现
    assert seen[1].is_followup is True  # 轮2：同锚同 tag 二现 → 追问命中
    key = "weiliao:session:sess-001"
    assert redis_client.hget(key, "followup:PRICE_INQUIRY:house:house-001") == b"2"
    assert redis_client.hget(key, "followup_count") == b"1"
