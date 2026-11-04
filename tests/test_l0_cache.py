"""L0 缓存客户端 I/O 矩阵单测（Story 3.1，fakeredis 驱动）。

覆盖 spec I/O & Edge-Case Matrix 全部场景与验收标准：
写入/命中/未命中/逻辑作废后读/新版本覆盖/Redis 故障；评审循环 1-3
补充：读路径原子单 roundtrip、乱序守卫（低版本 set/invalidate、同版本
重放）、高版本卡片不被低版本作废遮蔽、跨 intent 写入不清墓碑、跨房源
墓碑隔离、未缓存 intent 负控、写路径 fail-open 三分支、读路径损坏墓碑
不遮蔽、非法 UTF-8 字节、索引存活卡片过期、invalidate 默认时间戳路径
与缺版本抛错、record_cache_event 直测。
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import fakeredis
import pytest
import redis

from weiliao_agent.cache import (
    DEFAULT_TTL_SECONDS,
    INDEX_KEY_PREFIX,
    KEY_PREFIX,
    TOMBSTONE_KEY_PREFIX,
    L0CacheClient,
)
from weiliao_agent.models import FactCard
from weiliao_agent.observability import tracing

AS_OF = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)
INVALIDATED_AT = datetime(2026, 11, 3, 10, 5, 0, tzinfo=UTC)
TRACE_ID = "ab" * 16
HOUSE_A = "house-001"
HOUSE_B = "house-002"
PRICE = "PRICE_INQUIRY"
AVAIL = "AVAILABILITY"


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def l0(redis_client: fakeredis.FakeRedis) -> L0CacheClient:
    return L0CacheClient(redis_client)


class _FakeLangfuse:
    """Langfuse client 替身：记录全部事件调用，可按需抛错。"""

    def __init__(self, *, raise_on_event: bool = False) -> None:
        self.raise_on_event = raise_on_event
        self.events: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> None:
        if self.raise_on_event:
            msg = "sdk create_event boom"
            raise RuntimeError(msg)
        self.events.append(kwargs)


@pytest.fixture
def fake_langfuse() -> Iterator[_FakeLangfuse]:
    client = _FakeLangfuse()
    tracing.set_langfuse_client(client)
    yield client
    tracing.set_langfuse_client(None)


def _card(house_id: str, version: int, **overrides: Any) -> FactCard:
    payload: dict[str, Any] = {
        "house_id": house_id,
        "status": "active",
        "price_bucket": 12,
        "school": "实验小学",
        "dialogue_summary": ["第 1 轮"],
        "fact_version": version,
        "as_of": AS_OF,
    }
    payload.update(overrides)
    return FactCard.model_validate(payload)


def _idx(intent: str, house_id: str) -> str:
    return f"{INDEX_KEY_PREFIX}{intent}:{house_id}"


def _tomb(house_id: str) -> str:
    return f"{TOMBSTONE_KEY_PREFIX}{house_id}"


def _card_key(intent: str, card: FactCard) -> str:
    return f"{KEY_PREFIX}{intent}:{card.house_id}:{card.status}:{card.price_bucket}"


def _last_event(fake: _FakeLangfuse) -> dict[str, Any]:
    assert fake.events, "expected at least one Langfuse event"
    return fake.events[-1]


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", event["metadata"])


# ---- 基本读写 / TTL / 未命中 ----


def test_set_then_get_round_trips_with_300s_ttl_on_both_keys(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    card = _card(HOUSE_A, 1)

    assert l0.set(PRICE, card, trace_id=TRACE_ID) is True
    got = l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID)

    assert got == card
    assert redis_client.ttl(_idx(PRICE, HOUSE_A)) == DEFAULT_TTL_SECONDS
    assert redis_client.ttl(_card_key(PRICE, card)) == DEFAULT_TTL_SECONDS
    # set 成功补记 write 事件（KEEP 指令④），hit 记录命中版本。
    assert _metadata(_last_event(fake_langfuse))["operation"] == "hit"
    write_events = [
        e for e in fake_langfuse.events if _metadata(e)["operation"] == "write"
    ]
    assert len(write_events) == 1
    assert _metadata(write_events[0])["stage"] == "set"
    # happy path 零 ERROR 事件：hit/write 均为 DEFAULT 级。
    assert all(e["level"] != "ERROR" for e in fake_langfuse.events)


def test_get_miss_returns_none_without_event(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    assert l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is None
    assert fake_langfuse.events == []  # miss 无事件：命中率口径不依赖 miss 事件


def test_get_uncached_intent_negative_control(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis
) -> None:
    # 已缓存 intent 的卡片不得被其他 intent 读到（负控，KEEP 指令⑤）。
    l0.set(PRICE, _card(HOUSE_A, 1))
    assert redis_client.get(_idx(PRICE, HOUSE_A)) is not None

    assert l0.get(AVAIL, HOUSE_A) is None
    assert l0.get("LOCATION", HOUSE_A) is None


def test_overwrite_higher_version_switches_index_and_keeps_old_card_key(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis
) -> None:
    v1 = _card(HOUSE_A, 1)
    v2 = _card(HOUSE_A, 2, status="sold", price_bucket=13)
    assert l0.set(PRICE, v1) is True

    assert l0.set(PRICE, v2) is True

    got = l0.get(PRICE, HOUSE_A)
    assert got == v2  # 索引指向新值，读取返回新卡片
    assert redis_client.get(_card_key(PRICE, v1)) is not None  # 旧 key 留盘可审计
    assert redis_client.get(_idx(PRICE, HOUSE_A)) == _card_key(PRICE, v2).encode()


# ---- 逻辑作废 ----


def test_invalidated_read_returns_none_but_old_card_value_remains(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    card = _card(HOUSE_A, 1)
    l0.set(PRICE, card)

    assert l0.invalidate(HOUSE_A, 2, invalidated_at=INVALIDATED_AT, trace_id=TRACE_ID)
    assert l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is None
    # 逻辑作废非物理删除：旧卡片值仍在 Redis 可审计。
    assert redis_client.get(_card_key(PRICE, card)) is not None
    # 墓碑 JSON 可解析且 fact_version / invalidated_at 正确。
    tomb = json.loads(cast("bytes", redis_client.get(_tomb(HOUSE_A))).decode())
    assert tomb == {"fact_version": 2, "invalidated_at": INVALIDATED_AT.isoformat()}
    assert redis_client.ttl(_tomb(HOUSE_A)) == DEFAULT_TTL_SECONDS
    # 读路径拦截记 invalidated 事件（stage=get）。
    assert _metadata(_last_event(fake_langfuse))["operation"] == "invalidated"
    assert _metadata(_last_event(fake_langfuse))["stage"] == "get"


def test_invalidate_without_timestamp_uses_current_time(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis
) -> None:
    # 生产默认调用形态（不传 invalidated_at）：墓碑 JSON 可解析且版本正确。
    assert l0.invalidate(HOUSE_A, 3) is True

    tomb = json.loads(cast("bytes", redis_client.get(_tomb(HOUSE_A))).decode())
    assert tomb["fact_version"] == 3
    assert datetime.fromisoformat(tomb["invalidated_at"]).tzinfo is not None


def test_tombstone_isolation_per_house(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    # 已缓存 house A 且已 invalidate house B：A 正常命中，不受他房墓碑影响。
    l0.set(PRICE, _card(HOUSE_A, 1))
    l0.invalidate(HOUSE_B, 5)

    assert l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is not None
    assert _metadata(_last_event(fake_langfuse))["operation"] == "hit"


def test_invalidate_without_fact_version_raises_and_writes_nothing(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis
) -> None:
    with pytest.raises(ValueError, match="fact_version"):
        l0.invalidate(HOUSE_A)  # 默认调用形态：缺版本快速失败

    assert redis_client.keys(f"{TOMBSTONE_KEY_PREFIX}*") == []  # 不写任何键


# ---- 乱序守卫（评审循环 2 权威语义） ----


def test_late_lower_version_set_after_invalidate_is_rejected(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    l0.invalidate(HOUSE_A, 2)

    assert l0.set(PRICE, _card(HOUSE_A, 1), trace_id=TRACE_ID) is False

    assert l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is None  # 旧事件不得复活


def test_same_version_replay_set_does_not_revoke_invalidate(
    l0: L0CacheClient,
) -> None:
    l0.set(PRICE, _card(HOUSE_A, 2))
    l0.invalidate(HOUSE_A, 2)

    assert l0.set(PRICE, _card(HOUSE_A, 2, price_bucket=13)) is False

    assert l0.get(PRICE, HOUSE_A) is None


def test_lower_version_set_does_not_repoint_index(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis
) -> None:
    v2 = _card(HOUSE_A, 2, price_bucket=13)
    assert l0.set(PRICE, v2) is True

    assert l0.set(PRICE, _card(HOUSE_A, 1)) is False  # 守卫 (b)：不重指索引

    got = l0.get(PRICE, HOUSE_A)
    assert got is not None and got.fact_version == 2  # 仍返回 v2 卡片
    assert redis_client.get(_idx(PRICE, HOUSE_A)) == _card_key(PRICE, v2).encode()


def test_late_lower_version_invalidate_does_not_mask_newer_card(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    l0.set(PRICE, _card(HOUSE_A, 3))

    assert l0.invalidate(HOUSE_A, 2, trace_id=TRACE_ID) is True

    got = l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID)
    assert got is not None and got.fact_version == 3  # 作废不遮蔽更新事实
    # 事件记录 Lua 返回的实际生效版本（无既有墓碑：生效版本=传入 2，
    # 而非卡片的 3）——显式选取 invalidate 阶段事件，而非其后 get 的
    # hit 事件。
    invalidate_events = [
        e
        for e in fake_langfuse.events
        if _metadata(e)["operation"] == "invalidated" and _metadata(e)["stage"] == "invalidate"
    ]
    assert len(invalidate_events) == 1
    assert _metadata(invalidate_events[0])["metadata"]["fact_version"] == 2


def test_invalidate_keeps_higher_existing_tombstone_version(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis
) -> None:
    l0.invalidate(HOUSE_A, 5)

    assert l0.invalidate(HOUSE_A, 2) is True  # 迟到低版本不得拉低墓碑

    tomb = json.loads(cast("bytes", redis_client.get(_tomb(HOUSE_A))).decode())
    assert tomb["fact_version"] == 5


# ---- 跨 intent 墓碑保留（评审循环 3 回归） ----


def test_set_does_not_clear_tombstone_for_other_intents(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    # 评审循环 3 实测复现回归：单 intent 高版本 set 不得清整房墓碑。
    l0.set(PRICE, _card(HOUSE_A, 1))
    l0.set(AVAIL, _card(HOUSE_A, 1))
    l0.invalidate(HOUSE_A, 2)
    assert l0.get(PRICE, HOUSE_A) is None

    assert l0.set(PRICE, _card(HOUSE_A, 3)) is True  # 高版本本 intent 可写

    got = l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID)
    assert got is not None and got.fact_version == 3  # 高版本不被低墓碑遮蔽
    assert l0.get(AVAIL, HOUSE_A) is None  # 其他 intent 仍被墓碑拦截


# ---- 损坏数据（fail-open / corrupted） ----


def test_corrupted_card_payload_is_miss_with_corrupted_event(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    card = _card(HOUSE_A, 1)
    l0.set(PRICE, card)
    redis_client.set(_card_key(PRICE, card), "{not valid json")

    assert l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is None
    event = _last_event(fake_langfuse)
    assert _metadata(event)["operation"] == "corrupted"
    assert event["level"] == "ERROR"


def test_invalid_utf8_card_bytes_are_corrupted_not_raise(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    card = _card(HOUSE_A, 1)
    l0.set(PRICE, card)
    redis_client.set(_card_key(PRICE, card), b"\xff\xfe\x00bad-bytes")

    assert l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is None  # 不抛异常
    assert _metadata(_last_event(fake_langfuse))["operation"] == "corrupted"


def test_corrupted_tombstone_does_not_mask_valid_card(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    # 读路径损坏墓碑按「无版本信息」fail-open：不遮蔽合法卡片。
    l0.set(PRICE, _card(HOUSE_A, 1))
    redis_client.set(_tomb(HOUSE_A), "{corrupted")

    got = l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID)
    assert got is not None and got.fact_version == 1
    assert _metadata(_last_event(fake_langfuse))["operation"] == "hit"


@pytest.mark.parametrize("tomb_payload", ["{}", '{"fact_version": "abc"}', '"str"'])
def test_corrupted_tombstone_fails_open_on_write_paths(
    l0: L0CacheClient,
    redis_client: fakeredis.FakeRedis,
    fake_langfuse: _FakeLangfuse,
    tomb_payload: str,
) -> None:
    # 写路径 fail-open 三分支之 (a)(c)：损坏墓碑（合法 JSON 但版本缺失/
    # 非数值）不阻塞 set / invalidate，不抛错（Lua tonumber nil 检查）。
    redis_client.set(_tomb(HOUSE_A), tomb_payload)

    assert l0.set(PRICE, _card(HOUSE_A, 4), trace_id=TRACE_ID) is True
    got = l0.get(PRICE, HOUSE_A)
    assert got is not None and got.fact_version == 4

    assert l0.invalidate(HOUSE_A, 6, trace_id=TRACE_ID) is True
    assert l0.get(PRICE, HOUSE_A) is None  # invalidate 已生效（版本 6 ≥ 4）


@pytest.mark.parametrize(
    "existing_payload",
    ['{"fact_version": null}', "{not valid json"],  # 版本缺失 / pcall decode 失败
)
def test_corrupted_existing_card_fails_open_on_guard_b(
    l0: L0CacheClient,
    redis_client: fakeredis.FakeRedis,
    existing_payload: str,
) -> None:
    # 写路径 fail-open 三分支之 (b)：guard-(b) 现存卡片损坏（无版本信息）
    # 不阻塞 set——旧事件不得回指旧事实的守卫对无版本数据 fail-open。
    card = _card(HOUSE_A, 1)
    l0.set(PRICE, card)
    card_key = _card_key(PRICE, card)
    redis_client.set(card_key, existing_payload)

    assert l0.set(PRICE, _card(HOUSE_A, 1, price_bucket=13)) is True  # 不重指被拒

    got = l0.get(PRICE, HOUSE_A)
    assert got is not None and got.price_bucket == 13


def test_index_surviving_expired_card_is_miss(
    l0: L0CacheClient, redis_client: fakeredis.FakeRedis
) -> None:
    # 索引存活卡片过期中途态：无卡片视为未命中，不抛异常。
    card = _card(HOUSE_A, 1)
    l0.set(PRICE, card)
    redis_client.delete(_card_key(PRICE, card))

    assert l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is None


# ---- Redis 故障 fail-safe ----


class _FailingScript:
    """脚本替身：调用即抛 RedisError（连接故障注入）。"""

    def __call__(self, keys: list[str], args: list[Any]) -> Any:
        msg = "Connection refused"
        raise redis.ConnectionError(msg)


class _FailingRedis:
    """Redis 替身：register_script 返回故障脚本。"""

    def register_script(self, script: str) -> _FailingScript:
        return _FailingScript()


@pytest.fixture
def failing_l0() -> L0CacheClient:
    return L0CacheClient(_FailingRedis())  # type: ignore[arg-type]


def test_redis_failure_get_is_miss_with_failure_event(
    failing_l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    assert failing_l0.get(PRICE, HOUSE_A, trace_id=TRACE_ID) is None
    event = _last_event(fake_langfuse)
    assert _metadata(event)["operation"] == "failure"
    assert event["level"] == "ERROR"
    assert _metadata(event)["stage"] == "get"


def test_redis_failure_set_silently_fails_with_failure_event(
    failing_l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    assert failing_l0.set(PRICE, _card(HOUSE_A, 1), trace_id=TRACE_ID) is False
    assert _metadata(_last_event(fake_langfuse))["stage"] == "set"


def test_redis_failure_invalidate_silently_fails_with_failure_event(
    failing_l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    assert failing_l0.invalidate(HOUSE_A, 2, trace_id=TRACE_ID) is False
    assert _metadata(_last_event(fake_langfuse))["stage"] == "invalidate"


# ---- record_cache_event 直测 ----


def test_record_cache_event_level_mapping_and_nested_metadata(
    fake_langfuse: _FakeLangfuse,
) -> None:
    tracing.record_cache_event(
        TRACE_ID, operation="corrupted", stage="get", metadata={"reason": "bad"}
    )
    tracing.record_cache_event(
        TRACE_ID,
        operation="failure",
        stage="set",
        metadata={"operation": "hacked", "error_type": "ConnectionError"},
    )
    tracing.record_cache_event(
        TRACE_ID,
        operation="hit",
        stage="get",
        metadata={"operation": "hacked", "intent": PRICE},
    )

    corrupted, failure, hit = fake_langfuse.events
    assert corrupted["level"] == "ERROR"
    assert failure["level"] == "ERROR"
    assert hit["level"] == "DEFAULT"
    # 保留键（event/operation/stage）不得被调用方 metadata 覆盖；
    # 调用方 metadata 一律挂于独立子键（评审循环 3）。
    hit_meta = _metadata(hit)
    assert hit_meta["operation"] == "hit"
    assert hit_meta["stage"] == "get"
    assert hit_meta["event"] == "cache_event"
    assert hit_meta["metadata"] == {"operation": "hacked", "intent": PRICE}
    assert _metadata(corrupted)["metadata"] == {"reason": "bad"}


def test_record_cache_event_skips_unrecordable_trace_id(
    fake_langfuse: _FakeLangfuse,
) -> None:
    tracing.record_cache_event(None, operation="hit", stage="get")
    tracing.record_cache_event("not-a-hex-trace", operation="hit", stage="get")
    assert fake_langfuse.events == []


def test_record_cache_event_swallows_sdk_errors() -> None:
    tracing.set_langfuse_client(_FakeLangfuse(raise_on_event=True))
    try:
        tracing.record_cache_event(TRACE_ID, operation="failure", stage="get")
    finally:
        tracing.set_langfuse_client(None)
