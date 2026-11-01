"""信封模型 I/O 矩阵单测（Story 1.1）。

覆盖 spec I/O & Edge-Case Matrix 四场景：
HAPPY_PATH / MISSING_REQUIRED / UNKNOWN_FIELD / BAD_TYPE。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from weiliao_agent.models.envelope import Envelope


def _valid_payload() -> dict[str, object]:
    return {
        "session_id": "sess-001",
        "house_id": "house-001",
        "turn": 1,
        "intent": "PRICE_INQUIRY",
        "timestamp": "2026-10-31T10:00:00+08:00",
        "trace_id": "trace-001",
    }


def test_happy_path_valid_dict_produces_envelope() -> None:
    envelope = Envelope.model_validate(_valid_payload())

    assert envelope.session_id == "sess-001"
    assert envelope.house_id == "house-001"
    assert envelope.turn == 1
    assert envelope.intent == "PRICE_INQUIRY"
    assert isinstance(envelope.timestamp, datetime)
    assert envelope.trace_id == "trace-001"
    # 预留四字段默认 None，不参与本 Story 验收。
    assert envelope.message is None
    assert envelope.resolved_message is None
    assert envelope.coref is None
    assert envelope.media_type is None


@pytest.mark.parametrize("missing_field", ["session_id", "house_id", "timestamp"])
def test_missing_required_field_fails_closed(missing_field: str) -> None:
    payload = _valid_payload()
    del payload[missing_field]

    with pytest.raises(ValidationError):
        Envelope.model_validate(payload)


def test_missing_trace_id_defaults_to_none() -> None:
    """Story 1.5 契约变更：trace_id 由服务端权威生成，payload 可缺省。"""
    payload = _valid_payload()
    del payload["trace_id"]

    envelope = Envelope.model_validate(payload)

    assert envelope.trace_id is None


def test_unknown_field_is_ignored_by_default() -> None:
    payload = _valid_payload()
    payload["future_field"] = "not-defined-yet"

    envelope = Envelope.model_validate(payload)

    assert envelope.session_id == "sess-001"
    assert "future_field" not in envelope.model_fields_set


def test_bad_type_fails_closed() -> None:
    payload = _valid_payload()
    payload["trace_id"] = {"not": "a string"}

    with pytest.raises(ValidationError):
        Envelope.model_validate(payload)


def test_naive_timestamp_without_timezone_is_rejected() -> None:
    """结算 deferred-work：naive datetime（无时区）fail-closed。"""
    payload = _valid_payload()
    payload["timestamp"] = datetime(2026, 10, 31, 10, 0, 0)  # 无时区

    with pytest.raises(ValidationError):
        Envelope.model_validate(payload)


@pytest.mark.parametrize("blank_field", ["session_id", "house_id", "trace_id"])
def test_blank_identifier_is_rejected(blank_field: str) -> None:
    """结算 deferred-work：三项标识符非空（min_length=1）。"""
    payload = _valid_payload()
    payload[blank_field] = ""

    with pytest.raises(ValidationError):
        Envelope.model_validate(payload)


def test_optional_turn_and_intent_accept_none() -> None:
    payload = _valid_payload()
    payload["turn"] = None
    payload["intent"] = None
    payload["timestamp"] = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)

    envelope = Envelope.model_validate(payload)

    assert envelope.turn is None
    assert envelope.intent is None
