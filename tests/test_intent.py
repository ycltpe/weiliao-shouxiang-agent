"""意图标签模型与分类器接口 I/O 矩阵单测（Story 2.1）。

覆盖 spec I/O & Edge-Case Matrix 六场景：
合法标签入信封（9 类逐个）/ 未知意图值拒绝（模型层 + 端点层 fail-closed）/
注入分类器写回 / 分类异常降级 UNKNOWN / 未注入默认占位行为 / 置信度越界
拒绝；另覆盖分类器输入口径（Story 6.3：纯 resolved_message、原文
零接触、缺失/空白 fail-closed）与追问锚定矩阵（FR-2 per-(tag, anchor)
计数、哨兵锚向后兼容、recorder 异常 fail-safe）。
"""

from collections.abc import Iterator, Mapping
from typing import Any, get_args

import pytest
import redis
from fastapi.testclient import TestClient
from pydantic import ValidationError

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.models.coref import CorefRef
from weiliao_agent.models.envelope import Envelope, HandoffAssessment
from weiliao_agent.models.intent import IntentClassification, IntentTag
from weiliao_agent.observability import tracing

# 与 models/intent.py 的 Literal 权威定义同源派生，防手抄漂移。
ALL_TAGS: tuple[IntentTag, ...] = get_args(IntentTag)


def _valid_payload(intent: Any = "PRICE_INQUIRY") -> dict[str, Any]:
    return {
        "session_id": "sess-001",
        "house_id": "house-001",
        "turn": 1,
        "intent": intent,
        "timestamp": "2026-10-31T10:00:00+08:00",
        "trace_id": "trace-001",
        "message": "你好，这套房多少钱？",
        # Story 6.3 夹具口径：standalone 装配的信封显式携带消解后
        # 消息（生产链上由链首 coreference 过滤器恒产出），使"决策
        # 输入唯一"成为可测契约而非隐含假设。
        "resolved_message": "你好，这套房多少钱？",
    }


@pytest.mark.parametrize("tag", ALL_TAGS)
def test_envelope_accepts_each_of_nine_intent_tags(tag: IntentTag) -> None:
    envelope = Envelope.model_validate(_valid_payload(intent=tag))

    assert envelope.intent == tag


def test_envelope_rejects_unknown_intent_value() -> None:
    with pytest.raises(ValidationError):
        Envelope.model_validate(_valid_payload(intent="DISCOUNT"))


def test_endpoint_unknown_intent_fails_closed_with_generic_fallback() -> None:
    client = TestClient(create_app())

    response = client.post("/v1/messages", json=_valid_payload(intent="DISCOUNT"))

    assert response.status_code == 200  # 非 5xx，不进入管道
    body = response.json()
    assert body["degraded"] is True
    assert body["failed_filter"] is None
    assert body["reply"] == render_fallback(None)  # 通用兜底文案
    assert body["reply"].startswith(AI_DISCLOSURE_PREFIX)
    assert body["trace_id"]  # 新生成 trace_id
    assert body["trace_id"] != "trace-001"


class _StubClassifier:
    """返回固定分类结果的测试替身，记录收到的消息文本与会话上下文。"""

    def __init__(self, classification: IntentClassification) -> None:
        self._classification = classification
        self.received_message: str | None = None
        self.received_context: Mapping[str, Any] | None = None

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        self.received_message = message
        self.received_context = context
        return self._classification


class _RaisingClassifier:
    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        msg = "classifier boom"
        raise RuntimeError(msg)


class _RecordingAssessor:
    """记录收到的 message 实参的硬触发评估替身（决策输入锁定用）。"""

    def __init__(self) -> None:
        self.messages: list[str | None] = []

    def assess(
        self,
        *,
        intent: Any,
        media_type: str | None,
        per_intent_followup_count: int,
        message: str | None,
    ) -> HandoffAssessment:
        self.messages.append(message)
        return HandoffAssessment(triggered=False, conditions=[])


class _ConstructBypassClassifier:
    """非一致性替身：经 model_construct 绕过校验返回枚举外 tag。"""

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        return IntentClassification.model_construct(tag="DISCOUNT", confidence=0.9)


def test_injected_classifier_writes_back_tag_and_passes_through_envelope() -> None:
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    filter_ = IntentRouterFilter(classifier=stub)
    envelope = Envelope.model_validate(_valid_payload(intent=None))

    result = filter_.process(envelope)

    assert result.intent == "GREETING"
    # 信封其余字段透传。
    assert result.session_id == envelope.session_id
    assert result.house_id == envelope.house_id
    assert result.turn == envelope.turn
    assert result.trace_id == envelope.trace_id
    assert result.timestamp == envelope.timestamp
    # 分类器收到的输入：消解后消息（Story 6.3 决策输入唯一来源）+
    # 会话上下文。
    assert stub.received_message == "你好，这套房多少钱？"
    assert stub.received_context is not None
    assert stub.received_context["session_id"] == "sess-001"
    assert stub.received_context["house_id"] == "house-001"
    assert stub.received_context["turn"] == 1
    assert stub.received_context["trace_id"] == "trace-001"


def test_injected_classifier_overrides_preset_intent() -> None:
    # 分类器为权威：注入后一律以分类结果覆盖信封既有 intent。
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    filter_ = IntentRouterFilter(classifier=stub)
    envelope = Envelope.model_validate(_valid_payload(intent="PRICE_INQUIRY"))

    result = filter_.process(envelope)

    assert result.intent == "GREETING"


def test_classifier_sees_only_resolved_message_byte_for_byte() -> None:
    # Story 6.3：resolved 与原文并存时，分类器收到的文本逐字节等于
    # resolved_message；原文（message）在任何决策路径零读取。
    stub = _StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.8))
    filter_ = IntentRouterFilter(classifier=stub)
    envelope = Envelope.model_validate(_valid_payload())
    envelope = envelope.model_copy(update={"resolved_message": "这套房（house_id: h1）多少钱"})

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    # AD-11/Story 6.3：消解后消息即分类器唯一输入（逐字节，原文零接触）。
    assert stub.received_message == "这套房（house_id: h1）多少钱"


def test_classifier_not_called_when_resolved_message_missing() -> None:
    # Story 6.3：resolved_message 缺失（原文在场也不读取）→ fail-closed
    # UNKNOWN，分类器零调用；硬触发评估与分类器共用同一输入变量——
    # assessor 收到的 message 实参为 None 而非 envelope.message。
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    assessor = _RecordingAssessor()
    filter_ = IntentRouterFilter(classifier=stub, handoff_assessor=assessor)
    payload = _valid_payload(intent=None)
    payload["resolved_message"] = None
    envelope = Envelope.model_validate(payload)

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert stub.received_message is None  # 决策路径零读取原文
    assert assessor.messages == [None]  # 评估输入同为 resolved_message


def test_classifier_exception_degrades_to_unknown_without_raising() -> None:
    filter_ = IntentRouterFilter(classifier=_RaisingClassifier())
    envelope = Envelope.model_validate(_valid_payload(intent=None))

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    # 信封其余字段透传（fail-closed 仅作用于 intent），与 happy path 口径一致。
    assert result.session_id == envelope.session_id
    assert result.house_id == envelope.house_id
    assert result.turn == envelope.turn
    assert result.trace_id == envelope.trace_id
    assert result.timestamp == envelope.timestamp


def test_nonconforming_classifier_tag_fails_closed_to_unknown() -> None:
    # 非一致性分类器经 model_construct 写入枚举外 tag：写回前的
    # model_validate 全量校验拒绝，fail-closed UNKNOWN。
    filter_ = IntentRouterFilter(classifier=_ConstructBypassClassifier())
    envelope = Envelope.model_validate(_valid_payload(intent=None))

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"


def test_injected_classifier_without_any_message_text_fails_closed() -> None:
    # 原文与消解后消息同时缺失：fail-closed UNKNOWN，分类器零调用。
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    filter_ = IntentRouterFilter(classifier=stub)
    payload = _valid_payload(intent=None)
    payload["resolved_message"] = None
    del payload["message"]
    envelope = Envelope.model_validate(payload)

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert stub.received_message is None  # 无文本不调分类器


def test_empty_message_text_fails_closed_without_calling_classifier() -> None:
    # 空串/纯空白 resolved_message 与 None 同口径：守卫不得被绕过，
    # 原文在场也不读取。
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    filter_ = IntentRouterFilter(classifier=stub)
    payload = _valid_payload(intent=None)
    payload["resolved_message"] = ""
    payload["message"] = "原文在场也不读"
    envelope = Envelope.model_validate(payload)

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert stub.received_message is None  # 空文本不调分类器


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_resolved_message_fails_closed_without_calling_classifier(
    blank: str,
) -> None:
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    assessor = _RecordingAssessor()
    filter_ = IntentRouterFilter(classifier=stub, handoff_assessor=assessor)
    payload = _valid_payload(intent=None)
    payload["resolved_message"] = blank
    envelope = Envelope.model_validate(payload)

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert stub.received_message is None
    # assessor 收到空白 resolved 值（原文在场也不读取）。
    assert assessor.messages == [blank]


def test_default_filter_preserves_placeholder_behavior() -> None:
    filter_ = IntentRouterFilter()

    missing = Envelope.model_validate(_valid_payload(intent=None))
    assert filter_.process(missing).intent == "UNKNOWN"

    preset = Envelope.model_validate(_valid_payload(intent="PRICE_INQUIRY"))
    assert filter_.process(preset).intent == "PRICE_INQUIRY"


@pytest.mark.parametrize("confidence", [1.5, -0.1])
def test_classification_confidence_out_of_bounds_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        IntentClassification(tag="GREETING", confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_classification_confidence_boundary_accepted(confidence: float) -> None:
    classification = IntentClassification(tag="GREETING", confidence=confidence)

    assert classification.confidence == confidence


# ---------------------------------------------------------------------------
# Story 2.3 追问识别矩阵：recorder 注入、信封标记写回、fail-safe、
# UNKNOWN / turn=None 跳过、达阈值 hard trigger trace 事件。
# ---------------------------------------------------------------------------

_TRACE_ID_HEX = "ab" * 16  # 32 位小写十六进制（Langfuse 可记录口径）


class _FakeLangfuse:
    """最小 Langfuse 替身：只记录 create_event 事件（trace 断言用）。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@pytest.fixture
def fake_langfuse() -> Iterator[_FakeLangfuse]:
    previous = tracing._client_or_none()  # 快照先前 client，teardown 恢复
    client = _FakeLangfuse()
    tracing.set_langfuse_client(client)
    yield client
    tracing.set_langfuse_client(previous)


def _followup_payload(**overrides: Any) -> dict[str, Any]:
    payload = _valid_payload(intent=None)
    payload["trace_id"] = _TRACE_ID_HEX
    payload.update(overrides)
    return payload


class _StubFollowupRecorder:
    """固定双计数返回的 recorder 替身，记录收到的 (session_id, tag, anchor)。"""

    def __init__(self, result: tuple[int, int]) -> None:
        self._result = result
        self.calls: list[tuple[str, IntentTag, str]] = []

    def __call__(self, session_id: str, tag: IntentTag, anchor: str) -> tuple[int, int]:
        self.calls.append((session_id, tag, anchor))
        return self._result


class _RaisingFollowupRecorder:
    def __call__(self, session_id: str, tag: IntentTag, anchor: str) -> tuple[int, int]:
        msg = "redis boom"
        raise redis.RedisError(msg)


class _CountingFollowupRecorder:
    """按 (tag, anchor) 真实累计的 recorder 替身（Redis 语义内存版）。

    锚定矩阵用：同 (tag, anchor) 第 2 次起返回追问命中（≥2），会话
    总数只计命中——与 SessionManager.record_followup 语义对齐。
    """

    def __init__(self) -> None:
        self.counts: dict[tuple[IntentTag, str], int] = {}
        self.session_total = 0
        self.calls: list[tuple[str, IntentTag, str]] = []

    def __call__(self, session_id: str, tag: IntentTag, anchor: str) -> tuple[int, int]:
        self.calls.append((session_id, tag, anchor))
        key = (tag, anchor)
        per_intent = self.counts.get(key, 0) + 1
        self.counts[key] = per_intent
        if per_intent >= 2:
            self.session_total += 1
        return per_intent, self.session_total


def _event_names(fake: _FakeLangfuse) -> list[str]:
    return [event["name"] for event in fake.events]


def test_followup_first_occurrence_keeps_envelope_flag_false(
    fake_langfuse: _FakeLangfuse,
) -> None:
    recorder = _StubFollowupRecorder((1, 0))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"  # 信封保留分类结果
    assert result.is_followup is False  # per-intent=1：首次非追问
    assert recorder.calls == [("sess-001", "PRICE_INQUIRY", "none")]
    assert "intent.followup" in _event_names(fake_langfuse)
    event = next(e for e in fake_langfuse.events if e["name"] == "intent.followup")
    assert event["metadata"]["per_tag_anchor_count"] == 1  # Story 6.3 锚定键名
    assert event["metadata"]["session_followup_count"] == 0
    assert event["metadata"]["is_followup"] is False


def test_followup_second_occurrence_marks_envelope(
    fake_langfuse: _FakeLangfuse,
) -> None:
    recorder = _StubFollowupRecorder((2, 1))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert result.is_followup is True  # per-intent ≥2：追问命中
    # 信封其余字段透传。
    assert result.session_id == envelope.session_id
    assert result.turn == envelope.turn
    assert "intent.followup_hard_trigger" not in _event_names(fake_langfuse)


def test_followup_threshold_emits_hard_trigger_event(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 会话追问总数达 3：信号事件含命中条件与计数（判定归 Epic 5）。
    recorder = _StubFollowupRecorder((2, 3))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="LOCATION", confidence=0.8)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.is_followup is True
    event = next(
        e for e in fake_langfuse.events if e["name"] == "intent.followup_hard_trigger"
    )
    assert event["level"] == "WARNING"
    assert event["metadata"]["hit_condition"] == "followup>=3"
    assert event["metadata"]["count"] == 3


def test_followup_past_threshold_does_not_reemit_hard_trigger(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 负边界（锁评审 E1）：计数已过 3 的追问命中不重发信号——
    # 若实现退化为 >= 3，本条即红。
    recorder = _StubFollowupRecorder((3, 4))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.is_followup is True
    assert "intent.followup_hard_trigger" not in _event_names(fake_langfuse)


def test_followup_first_occurrence_after_threshold_emits_nothing(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 负边界（锁评审 E2）：会话计数已达标时，新意图首次出现（非追问）
    # 不发信号——若丢失 is_followup 守卫，本条即红。
    recorder = _StubFollowupRecorder((1, 3))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="GREETING", confidence=0.9)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.is_followup is False
    assert "intent.followup_hard_trigger" not in _event_names(fake_langfuse)


def test_followup_recorder_failure_fails_safe(fake_langfuse: _FakeLangfuse) -> None:
    # recorder 抛 redis.RedisError：不应答降级、信封保留分类结果、
    # is_followup 保持默认 False、trace 记失败事件、过滤器不向上抛。
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=_RaisingFollowupRecorder(),
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert result.is_followup is False
    failure = next(
        e
        for e in fake_langfuse.events
        if e["name"] == "intent.classification_failure"
    )
    assert failure["metadata"]["reason"] == "followup_record_failed"


def test_followup_skipped_for_unknown_tag(fake_langfuse: _FakeLangfuse) -> None:
    # UNKNOWN（含占位降级）不参与追问判定：recorder 零调用、零计数副作用。
    recorder = _StubFollowupRecorder((2, 1))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="UNKNOWN", confidence=0.5)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert result.is_followup is False
    assert recorder.calls == []
    assert "intent.followup" not in _event_names(fake_langfuse)


def test_followup_skipped_when_turn_is_none(fake_langfuse: _FakeLangfuse) -> None:
    recorder = _StubFollowupRecorder((2, 1))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload(turn=None))

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert result.is_followup is False
    assert recorder.calls == []


def test_followup_skipped_without_recorder(fake_langfuse: _FakeLangfuse) -> None:
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert result.is_followup is False  # 默认 False，无 recorder 不报错
    assert "intent.followup" not in _event_names(fake_langfuse)


def test_followup_not_recorded_when_classification_fails(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 分类 fail-closed（异常路径）：不调 recorder、不累积追问计数。
    recorder = _StubFollowupRecorder((2, 1))
    filter_ = IntentRouterFilter(
        classifier=_RaisingClassifier(),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert result.is_followup is False
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Story 6.3 追问锚定矩阵（FR-2）：per-(tag, anchor) 计数——同锚同 tag 二现
# 计为同一意图、异锚同 tag 独立计数、空 coref 哨兵锚向后兼容。
# ---------------------------------------------------------------------------

HOUSE_H1 = CorefRef(entity_type="house", entity_id="h1")
HOUSE_H2 = CorefRef(entity_type="house", entity_id="h2")


def _anchored_payload(turn: int, message: str, coref: list[Any]) -> dict[str, Any]:
    payload = _followup_payload(turn=turn, message=message)
    # 经链首 coreference 后的信封口径：resolved 为消解文本、coref 挂引用。
    payload["resolved_message"] = message
    payload["coref"] = [ref.model_dump() for ref in coref]
    return payload


def test_followup_same_anchor_same_tag_second_turn_is_followup(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # FR-2 指代对：轮1"它多少钱"（anchor=h1）后轮2"这套房多少钱"
    # （anchor=h1），分类 tag 相同——两消息计为同一意图，轮2 追问命中。
    recorder = _CountingFollowupRecorder()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=recorder,
    )
    turn1 = Envelope.model_validate(
        _anchored_payload(1, "它多少钱", [HOUSE_H1])
    )
    turn2 = Envelope.model_validate(
        _anchored_payload(2, "这套房多少钱", [HOUSE_H1])
    )

    first = filter_.process(turn1)
    second = filter_.process(turn2)

    assert first.is_followup is False
    assert second.is_followup is True  # per-(tag, anchor) = 2
    assert recorder.calls == [
        ("sess-001", "PRICE_INQUIRY", "house:h1"),
        ("sess-001", "PRICE_INQUIRY", "house:h1"),
    ]


def test_followup_different_anchors_same_tag_count_independently(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 异锚同 tag：coref 锚定 h1 vs h2 各自独立计数，互不计为追问。
    recorder = _CountingFollowupRecorder()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=recorder,
    )
    turn1 = Envelope.model_validate(
        _anchored_payload(1, "它多少钱", [HOUSE_H1])
    )
    turn2 = Envelope.model_validate(
        _anchored_payload(2, "那套多少钱", [HOUSE_H2])
    )

    first = filter_.process(turn1)
    second = filter_.process(turn2)

    assert first.is_followup is False
    assert second.is_followup is False  # h2 首次出现：独立计数
    assert recorder.calls == [
        ("sess-001", "PRICE_INQUIRY", "house:h1"),
        ("sess-001", "PRICE_INQUIRY", "house:h2"),
    ]


def test_followup_empty_coref_uses_sentinel_anchor(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 无指代消息（coref 空）走哨兵锚：计数行为与既有 per-tag 口径一致。
    recorder = _CountingFollowupRecorder()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="GREETING", confidence=0.9)),
        followup_recorder=recorder,
    )
    payload = _followup_payload()
    payload["coref"] = []
    envelope = Envelope.model_validate(payload)

    result = filter_.process(envelope)

    assert result.is_followup is False
    assert recorder.calls == [("sess-001", "GREETING", "none")]


def test_followup_coref_none_uses_sentinel_anchor(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # coref 为 None（coreference 过滤器未执行）与空列表同口径：哨兵锚。
    recorder = _CountingFollowupRecorder()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="GREETING", confidence=0.9)),
        followup_recorder=recorder,
    )
    envelope = Envelope.model_validate(_followup_payload())
    assert envelope.coref is None

    result = filter_.process(envelope)

    assert result.is_followup is False
    assert recorder.calls == [("sess-001", "GREETING", "none")]


def test_followup_recorder_failure_fails_safe_anchored(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 锚定口径下 recorder 异常 fail-safe 原样（2.3 语义）：不应答降级、
    # is_followup=False、trace 记 followup_record_failed。
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=_RaisingFollowupRecorder(),
    )
    envelope = Envelope.model_validate(
        _anchored_payload(1, "它多少钱", [HOUSE_H1])
    )

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert result.is_followup is False
    failure = next(
        e
        for e in fake_langfuse.events
        if e["name"] == "intent.classification_failure"
    )
    assert failure["metadata"]["reason"] == "followup_record_failed"


# ---------------------------------------------------------------------------
# Story 3.3 L1 写挂点：recent_intent_writer / followup_snapshot_writer 注入、
# 触发时机与静默 fail-safe（L1 优化层写失败不记 trace、不应答降级）。
# ---------------------------------------------------------------------------


class _RecordingL1Writer:
    """L1 writer 替身：记录收到的 (session_id, value)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, session_id: str, value: Any) -> None:
        self.calls.append((session_id, value))


class _RaisingL1Writer:
    def __call__(self, session_id: str, value: Any) -> None:
        msg = "l1 cache boom"
        raise RuntimeError(msg)


def test_recent_intent_writer_called_once_on_classification_success(
    fake_langfuse: _FakeLangfuse,
) -> None:
    writer = _RecordingL1Writer()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        recent_intent_writer=writer,
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert writer.calls == [("sess-001", "PRICE_INQUIRY")]


def test_recent_intent_writer_skipped_when_turn_is_none(
    fake_langfuse: _FakeLangfuse,
) -> None:
    writer = _RecordingL1Writer()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        recent_intent_writer=writer,
    )
    envelope = Envelope.model_validate(_followup_payload(turn=None))

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert writer.calls == []


def test_followup_snapshot_writer_called_with_session_total_on_success(
    fake_langfuse: _FakeLangfuse,
) -> None:
    writer = _RecordingL1Writer()
    recorder = _StubFollowupRecorder((1, 3))
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=recorder,
        followup_snapshot_writer=writer,
    )
    envelope = Envelope.model_validate(_followup_payload())

    filter_.process(envelope)

    assert writer.calls == [("sess-001", 3)]  # 会话追问总数快照


def test_l1_writer_failure_fails_silently_without_trace_failure(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # L1 写失败静默 fail-safe：应答与信封结果不变、无失败 trace 事件
    # （与 intent_writer 的非对称设计——优化层不记 trace）。
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.9)),
        followup_recorder=_StubFollowupRecorder((1, 0)),
        recent_intent_writer=_RaisingL1Writer(),
        followup_snapshot_writer=_RaisingL1Writer(),
    )
    envelope = Envelope.model_validate(_followup_payload())

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    assert result.is_followup is False
    assert "intent.classification_failure" not in _event_names(fake_langfuse)
