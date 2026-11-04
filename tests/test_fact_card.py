"""事实卡片 schema 边界单测（Story 3.1）。

覆盖 spec Tasks：status 枚举拒绝非法值、school 可缺省、
dialogue_summary 上限 3 轮、as_of 须带时区、bucket 计算——防 schema 漂移。
"""

from datetime import UTC, datetime

import pytest

from weiliao_agent.models import FactCard, price_to_bucket

AS_OF = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "house_id": "house-001",
        "status": "active",
        "price_bucket": 12,
        "fact_version": 1,
        "as_of": AS_OF,
    }
    payload.update(overrides)
    return payload


def test_valid_card_round_trips() -> None:
    card = FactCard.model_validate(
        {
            **_valid_payload(),
            "school": "实验小学",
            "dialogue_summary": ["第 1 轮", "第 2 轮", "第 3 轮"],
        }
    )

    assert card.house_id == "house-001"
    assert card.status == "active"
    assert card.price_bucket == 12
    assert card.school == "实验小学"
    assert card.dialogue_summary == ["第 1 轮", "第 2 轮", "第 3 轮"]
    assert card.fact_version == 1
    assert card.as_of == AS_OF
    # 序列化口径：L0 缓存经 model_dump_json 落盘、model_validate_json 回读。
    assert FactCard.model_validate_json(card.model_dump_json()) == card


@pytest.mark.parametrize("status", ["active", "sold", "offline"])
def test_status_enum_accepts_three_states(status: str) -> None:
    card = FactCard.model_validate(_valid_payload(status=status))
    assert card.status == status


@pytest.mark.parametrize("status", ["pending", "ACTIVE", "", "deleted", 1])
def test_status_enum_rejects_unknown_values(status: object) -> None:
    with pytest.raises(ValueError):
        FactCard.model_validate(_valid_payload(status=status))


def test_school_defaults_to_none() -> None:
    card = FactCard.model_validate(_valid_payload())
    assert card.school is None


def test_dialogue_summary_defaults_to_empty() -> None:
    card = FactCard.model_validate(_valid_payload())
    assert card.dialogue_summary == []


def test_dialogue_summary_accepts_up_to_three_turns() -> None:
    card = FactCard.model_validate(
        _valid_payload(dialogue_summary=["a", "b", "c"]),
    )
    assert len(card.dialogue_summary) == 3


def test_dialogue_summary_rejects_fourth_turn() -> None:
    with pytest.raises(ValueError):
        FactCard.model_validate(_valid_payload(dialogue_summary=["a", "b", "c", "d"]))


def test_as_of_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FactCard.model_validate(_valid_payload(as_of=datetime(2026, 11, 3, 10, 0, 0)))


@pytest.mark.parametrize(
    ("price_yuan", "expected"),
    [
        (0, 0),
        (99_999, 0),
        (100_000, 1),
        (1_250_000, 12),
        (1_299_999, 12),
        (1_300_000, 13),
    ],
)
def test_price_to_bucket_floors_to_100k(price_yuan: int, expected: int) -> None:
    assert price_to_bucket(price_yuan) == expected


def test_price_bucket_rejects_negative_value() -> None:
    # price_to_bucket 负数产出经 FactCard 校验拒绝（fail-fast 落点）。
    with pytest.raises(ValueError):
        FactCard.model_validate(_valid_payload(price_bucket=price_to_bucket(-1)))


def test_fact_version_and_house_id_required() -> None:
    for field in ("fact_version", "house_id"):
        payload = _valid_payload()
        payload.pop(field)
        with pytest.raises(ValueError):
            FactCard.model_validate(payload)
