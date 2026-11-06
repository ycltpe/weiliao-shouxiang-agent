"""缓存查找过滤器 I/O 矩阵单测（Story 3.3，fakeredis 驱动）。

覆盖 spec I/O & Edge-Case Matrix 查找侧全部场景与验收标准：
未注入 L0 逐字节透传（既有占位行为零回归）、intent 缺失跳过查询、
L0 命中挂信封 ``fact_card``、miss 保持 None 并记 ``miss`` 事件、
Redis 故障经 L0 客户端 fail-safe 视 miss 不抛。
"""

from datetime import UTC, datetime
from typing import Any, cast
from unittest import mock

import fakeredis
import pytest
import redis
from conftest import _FakeLangfuse

from weiliao_agent.cache.l0_client import L0CacheClient
from weiliao_agent.filters.cache_lookup import CacheLookupFilter
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.fact_card import FactCard

TRACE_ID = "ab" * 16  # 32 位小写十六进制（Langfuse 可记录口径）
HOUSE_A = "house-001"
PRICE = "PRICE_INQUIRY"
AS_OF = datetime(2026, 11, 5, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def l0(redis_client: fakeredis.FakeRedis) -> L0CacheClient:
    return L0CacheClient(redis_client)


def _card(house_id: str = HOUSE_A, version: int = 1) -> FactCard:
    return FactCard.model_validate(
        {
            "house_id": house_id,
            "status": "active",
            "price_bucket": 12,
            "school": "实验小学",
            "dialogue_summary": ["第 1 轮"],
            "fact_version": version,
            "as_of": AS_OF,
        }
    )


def _envelope(intent: Any = PRICE) -> Envelope:
    return Envelope.model_validate(
        {
            "session_id": "sess-001",
            "house_id": HOUSE_A,
            "timestamp": "2026-11-05T10:00:00+08:00",
            "trace_id": TRACE_ID,
            "intent": intent,
        }
    )


def _operations(fake: _FakeLangfuse) -> list[str]:
    return [
        cast("str", event["metadata"]["operation"]) for event in fake.events
    ]


# ---- 透传行为 ----


def test_without_l0_client_passes_through_byte_identical() -> None:
    # 既有占位行为（Story 1.2）零回归：无注入逐字节透传。
    filter_ = CacheLookupFilter()
    envelope = _envelope()

    result = filter_.process(envelope)

    assert result is envelope
    assert result.fact_card is None


def test_missing_intent_skips_lookup_and_passes_through(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    # intent=None：L0 key 首段缺失不可寻址，跳过查询直接透传、零事件。
    filter_ = CacheLookupFilter(l0_client=l0)
    envelope = _envelope(intent=None)

    result = filter_.process(envelope)

    assert result.fact_card is None
    assert fake_langfuse.events == []
    assert redis_client.keys() == []  # 未触发任何 Redis 访问


# ---- 命中 / miss ----


def test_hit_attaches_fact_card_to_envelope(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    card = _card()
    assert l0.set(PRICE, card, trace_id=TRACE_ID) is True
    filter_ = CacheLookupFilter(l0_client=l0)
    envelope = _envelope()

    result = filter_.process(envelope)

    assert result.fact_card == card
    assert "hit" in _operations(fake_langfuse)
    assert "miss" not in _operations(fake_langfuse)


def test_miss_leaves_fact_card_none_and_records_miss_event(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    # L0 无匹配卡片：fact_card=None 留给 Epic 4 的 LLM 层，miss 事件
    # 可观测（与 hit 成对，3.4 命中率口径前置）。
    filter_ = CacheLookupFilter(l0_client=l0)
    envelope = _envelope()

    result = filter_.process(envelope)

    assert result.fact_card is None
    miss = next(e for e in fake_langfuse.events if e["metadata"]["operation"] == "miss")
    assert miss["level"] == "DEFAULT"  # 仅 corrupted/failure 为 ERROR
    assert miss["metadata"]["stage"] == "get"
    assert miss["metadata"]["metadata"]["intent"] == PRICE
    assert miss["metadata"]["metadata"]["house_id"] == HOUSE_A


def test_hit_preserves_other_envelope_fields(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    card = _card()
    l0.set(PRICE, card, trace_id=TRACE_ID)
    filter_ = CacheLookupFilter(l0_client=l0)
    envelope = _envelope()

    result = filter_.process(envelope)

    assert result.session_id == envelope.session_id
    assert result.intent == PRICE
    assert result.is_followup is False
    assert result.trace_id == TRACE_ID


# ---- Redis 故障 fail-safe ----


def test_redis_failure_treated_as_miss_without_raising(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    # Redis 故障经 L0 客户端 fail-safe 归并为 miss：failure（ERROR）+
    # miss（DEFAULT）事件成对，过滤器不抛、信封透传。
    filter_ = CacheLookupFilter(l0_client=l0)
    envelope = _envelope()

    with mock.patch.object(
        l0, "_get_script", side_effect=redis.RedisError("connection boom")
    ):
        result = filter_.process(envelope)

    assert result.fact_card is None
    operations = _operations(fake_langfuse)
    assert "failure" in operations
    assert "miss" in operations
    failure = next(
        e for e in fake_langfuse.events if e["metadata"]["operation"] == "failure"
    )
    assert failure["level"] == "ERROR"
