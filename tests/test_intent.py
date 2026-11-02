"""意图标签模型与分类器接口 I/O 矩阵单测（Story 2.1）。

覆盖 spec I/O & Edge-Case Matrix 六场景：
合法标签入信封（9 类逐个）/ 未知意图值拒绝（模型层 + 端点层 fail-closed）/
注入分类器写回 / 分类异常降级 UNKNOWN / 未注入默认占位行为 / 置信度越界
拒绝；另覆盖分类器输入口径（resolved_message 优先、无文本 fail-closed）。
"""

from collections.abc import Mapping
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_placeholder
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentClassification, IntentTag

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
    assert body["reply"] == render_placeholder(None)  # 通用兜底文案
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
    # 分类器收到的输入：消息文本（原文，消解后消息未就位）+ 会话上下文。
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


def test_injected_classifier_prefers_resolved_message_over_raw_message() -> None:
    stub = _StubClassifier(IntentClassification(tag="PRICE_INQUIRY", confidence=0.8))
    filter_ = IntentRouterFilter(classifier=stub)
    envelope = Envelope.model_validate(_valid_payload())
    envelope = envelope.model_copy(update={"resolved_message": "这套房（house_id: h1）多少钱"})

    result = filter_.process(envelope)

    assert result.intent == "PRICE_INQUIRY"
    # AD-11：消解后消息就位后即分类器唯一输入。
    assert stub.received_message == "这套房（house_id: h1）多少钱"


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
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    filter_ = IntentRouterFilter(classifier=stub)
    payload = _valid_payload(intent=None)
    del payload["message"]
    envelope = Envelope.model_validate(payload)

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert stub.received_message is None  # 无文本不调分类器


def test_empty_message_text_fails_closed_without_calling_classifier() -> None:
    # 空串与 None 同口径：信封允许空串（无 min_length），守卫不得被绕过。
    stub = _StubClassifier(IntentClassification(tag="GREETING", confidence=0.9))
    filter_ = IntentRouterFilter(classifier=stub)
    payload = _valid_payload(intent=None)
    payload["message"] = ""
    envelope = Envelope.model_validate(payload)

    result = filter_.process(envelope)

    assert result.intent == "UNKNOWN"
    assert stub.received_message is None  # 空文本不调分类器


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
