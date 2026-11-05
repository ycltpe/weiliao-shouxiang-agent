"""``house.sync.trigger`` 事件契约边界单测（Story 3.2）。

覆盖 spec Tasks：event_type 枚举拒绝、occurred_at 须带时区、upsert 缺
status/price 拒绝、dialogue_summary 上限、invalidate 严格形状、price
转桶薄转换——防契约漂移（事件 schema 为唯一权威定义）。
"""

from datetime import UTC, datetime

import pytest

from weiliao_agent.models import FactCard, HouseSyncEvent, price_to_bucket

OCCURRED_AT = datetime(2026, 11, 5, 10, 0, 0, tzinfo=UTC)


def _valid_upsert(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_id": "evt-001",
        "event_type": "house_upsert",
        "house_id": "house-001",
        "fact_version": 5,
        "occurred_at": OCCURRED_AT,
        "status": "active",
        "price_yuan": 1_250_000,
        "school": "实验小学",
        "dialogue_summary": ["第 1 轮"],
    }
    payload.update(overrides)
    return payload


def _valid_invalidate(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_id": "evt-002",
        "event_type": "house_invalidate",
        "house_id": "house-001",
        "fact_version": 2,
        "occurred_at": OCCURRED_AT,
    }
    payload.update(overrides)
    return payload


def test_upsert_round_trips_full_snapshot() -> None:
    event = HouseSyncEvent.model_validate(_valid_upsert())

    assert event.event_id == "evt-001"
    assert event.event_type == "house_upsert"
    assert event.house_id == "house-001"
    assert event.fact_version == 5
    assert event.occurred_at == OCCURRED_AT
    assert event.status == "active"
    assert event.price_yuan == 1_250_000
    assert event.school == "实验小学"
    assert event.dialogue_summary == ["第 1 轮"]
    assert HouseSyncEvent.model_validate_json(event.model_dump_json()) == event


def test_upsert_optional_fields_default() -> None:
    payload = _valid_upsert(school=None)
    payload.pop("dialogue_summary")
    event = HouseSyncEvent.model_validate(payload)
    assert event.school is None
    assert event.dialogue_summary == []


@pytest.mark.parametrize("event_type", ["house_update", "HOUSE_UPSERT", "", 1])
def test_event_type_rejects_unknown_values(event_type: object) -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(_valid_upsert(event_type=event_type))


def test_occurred_at_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(
            _valid_upsert(occurred_at=datetime(2026, 11, 5, 10, 0, 0))
        )


@pytest.mark.parametrize("missing", ["status", "price_yuan"])
def test_upsert_requires_status_and_price(missing: str) -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(_valid_upsert(**{missing: None}))


@pytest.mark.parametrize("status", ["pending", "ACTIVE", "", 1])
def test_upsert_rejects_unknown_status(status: object) -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(_valid_upsert(status=status))


def test_upsert_rejects_negative_price() -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(_valid_upsert(price_yuan=-1))


def test_dialogue_summary_capped_at_three() -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(
            _valid_upsert(dialogue_summary=["1", "2", "3", "4"])
        )


def test_invalidate_accepts_minimal_shape() -> None:
    event = HouseSyncEvent.model_validate(_valid_invalidate())

    assert event.event_type == "house_invalidate"
    assert event.fact_version == 2
    assert event.status is None
    assert event.price_yuan is None
    assert event.school is None
    assert event.dialogue_summary == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "active"),
        ("price_yuan", 1_250_000),
        ("school", "实验小学"),
        ("dialogue_summary", ["第 1 轮"]),
    ],
)
def test_invalidate_rejects_upsert_only_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(_valid_invalidate(**{field: value}))


def test_invalidate_still_requires_common_fields() -> None:
    with pytest.raises(ValueError):
        HouseSyncEvent.model_validate(_valid_invalidate(fact_version=None))


def test_to_fact_card_converts_price_to_bucket() -> None:
    event = HouseSyncEvent.model_validate(_valid_upsert())

    card = event.to_fact_card()

    assert card == FactCard(
        house_id="house-001",
        status="active",
        price_bucket=price_to_bucket(1_250_000),
        school="实验小学",
        dialogue_summary=["第 1 轮"],
        fact_version=5,
        as_of=OCCURRED_AT,
    )
    assert card.price_bucket == 12


def test_to_fact_card_rejects_invalidate() -> None:
    event = HouseSyncEvent.model_validate(_valid_invalidate())
    with pytest.raises(ValueError):
        event.to_fact_card()
