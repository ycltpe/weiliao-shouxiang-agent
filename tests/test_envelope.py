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
    # 四字段默认 None（Story 6.1：coref 为 None 即「过滤器未执行」）。
    assert envelope.message is None
    assert envelope.resolved_message is None
    assert envelope.coref is None
    assert envelope.media_type is None


# ---- Story 6.1：coref 收窄为 list[CorefRef]（HTTP 入口 model_validate 口径） ----


def test_coref_accepts_valid_coref_ref_dict_list() -> None:
    # 生产唯一执行点：合法 CorefRef dict 列表经 model_validate 全量校验。
    payload = _valid_payload()
    payload["coref"] = [
        {"entity_type": "house", "entity_id": "house-001"},
        {"entity_type": "price"},
    ]

    envelope = Envelope.model_validate(payload)

    assert envelope.coref is not None
    assert len(envelope.coref) == 2
    assert envelope.coref[0].entity_type == "house"
    assert envelope.coref[0].entity_id == "house-001"
    assert envelope.coref[1].entity_type == "price"
    assert envelope.coref[1].entity_id is None


@pytest.mark.parametrize(
    "coref",
    [
        ["junk"],  # 非 dict 条目
        [{"entity_type": "bogus"}],  # 未知实体类型
        [{"entity_type": "house"}],  # house 缺 entity_id（模型约束）
    ],
)
def test_coref_rejects_invalid_values_fails_closed(coref: object) -> None:
    # 非法 coref 经入口校验拒绝即 fail-closed（不进入管道）。
    payload = _valid_payload()
    payload["coref"] = coref

    with pytest.raises(ValidationError):
        Envelope.model_validate(payload)


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
