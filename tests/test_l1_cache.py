"""L1 进程内缓存客户端 I/O 矩阵单测（Story 3.3）。

覆盖 spec I/O & Edge-Case Matrix 全部 L1 场景与验收标准：
三类会话快照写读（最近意图 / 追问计数 / handoff 状态）、TTL 过期
（假时钟驱动）、容量 LRU 逐出、handoff 作废立即生效、ttl>30
fail-fast（AD-5 硬上限）、FactCard 等错误类型拒绝、多线程并发读写
无竞态损坏。
"""

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from weiliao_agent.cache.l1_cache import (
    L1_DEFAULT_MAXSIZE,
    L1_DEFAULT_TTL_SECONDS,
    L1_MAX_TTL_SECONDS,
    L1CacheClient,
)
from weiliao_agent.models.fact_card import FactCard

SESSION_A = "sess-001"
SESSION_B = "sess-002"


def _make_clock(start: float = 1_000.0) -> tuple[list[float], Any]:
    """假时钟：可变时间戳列表 + timer 引用（TTL 过期断言驱动）。"""
    now = [start]
    return now, lambda: now[0]


def _card() -> FactCard:
    return FactCard.model_validate(
        {
            "house_id": "house-001",
            "status": "active",
            "price_bucket": 12,
            "fact_version": 1,
            "as_of": datetime(2026, 11, 5, 10, 0, 0, tzinfo=UTC),
        }
    )


# ---- 基本写读（三类快照）----


def test_three_kinds_of_snapshots_round_trip_within_ttl() -> None:
    l1 = L1CacheClient()

    l1.set_recent_intent(SESSION_A, "PRICE_INQUIRY")
    l1.set_followup_count(SESSION_A, 3)
    l1.set_handoff_status(SESSION_A, "pending")

    assert l1.get_recent_intent(SESSION_A) == "PRICE_INQUIRY"
    assert l1.get_followup_count(SESSION_A) == 3
    assert l1.get_handoff_status(SESSION_A) == "pending"


def test_default_ttl_is_30s_and_maxsize_is_4096() -> None:
    assert L1_DEFAULT_TTL_SECONDS == 30
    assert L1_MAX_TTL_SECONDS == 30
    assert L1_DEFAULT_MAXSIZE == 4096
    # 边界 30 允许（fail-fast 仅针对 >30）。
    l1 = L1CacheClient(ttl_seconds=30)

    l1.set_recent_intent(SESSION_A, "GREETING")

    assert l1.get_recent_intent(SESSION_A) == "GREETING"


def test_miss_returns_none_for_all_kinds() -> None:
    l1 = L1CacheClient()

    assert l1.get_recent_intent(SESSION_A) is None
    assert l1.get_followup_count(SESSION_A) is None
    assert l1.get_handoff_status(SESSION_A) is None


def test_sessions_are_keyed_independently() -> None:
    l1 = L1CacheClient()

    l1.set_recent_intent(SESSION_A, "PRICE_INQUIRY")
    l1.set_recent_intent(SESSION_B, "AVAILABILITY")

    assert l1.get_recent_intent(SESSION_A) == "PRICE_INQUIRY"
    assert l1.get_recent_intent(SESSION_B) == "AVAILABILITY"


# ---- TTL 过期（假时钟）----


def test_entry_expires_after_ttl_and_returns_none() -> None:
    now, timer = _make_clock()
    l1 = L1CacheClient(ttl_seconds=30, timer=timer)
    l1.set_recent_intent(SESSION_A, "PRICE_INQUIRY")
    l1.set_followup_count(SESSION_A, 3)

    now[0] += 29.9
    assert l1.get_recent_intent(SESSION_A) == "PRICE_INQUIRY"
    assert l1.get_followup_count(SESSION_A) == 3

    now[0] += 0.2  # 越过 30s 上限：过期即 miss（调用方回落 Redis 权威值）
    assert l1.get_recent_intent(SESSION_A) is None
    assert l1.get_followup_count(SESSION_A) is None


# ---- 容量 LRU 逐出 ----


def test_capacity_evicts_oldest_entry_without_raising() -> None:
    l1 = L1CacheClient(maxsize=2)

    l1.set_recent_intent(SESSION_A, "GREETING")
    l1.set_recent_intent(SESSION_B, "LOCATION")
    l1.set_recent_intent("sess-003", "COMPLAINT")  # 超出 maxsize=2

    assert l1.get_recent_intent(SESSION_A) is None  # 最旧条目被逐出
    assert l1.get_recent_intent(SESSION_B) == "LOCATION"
    assert l1.get_recent_intent("sess-003") == "COMPLAINT"


# ---- handoff 作废语义 ----


def test_invalidate_handoff_removes_snapshot_immediately() -> None:
    l1 = L1CacheClient()
    l1.set_handoff_status(SESSION_A, "escalated")

    l1.invalidate_handoff(SESSION_A)

    assert l1.get_handoff_status(SESSION_A) is None


def test_invalidate_handoff_is_noop_for_missing_session() -> None:
    l1 = L1CacheClient()

    l1.invalidate_handoff(SESSION_A)  # 不抛、无副作用

    assert l1.get_handoff_status(SESSION_A) is None


def test_invalidate_handoff_does_not_touch_other_kinds() -> None:
    l1 = L1CacheClient()
    l1.set_recent_intent(SESSION_A, "PRICE_INQUIRY")
    l1.set_handoff_status(SESSION_A, "escalated")

    l1.invalidate_handoff(SESSION_A)

    assert l1.get_handoff_status(SESSION_A) is None
    assert l1.get_recent_intent(SESSION_A) == "PRICE_INQUIRY"


# ---- fail-fast 与类型拒绝 ----


def test_ttl_above_30_seconds_fails_fast() -> None:
    with pytest.raises(ValueError, match="hard limit"):
        L1CacheClient(ttl_seconds=31)


@pytest.mark.parametrize("ttl_seconds", [0, -5])
def test_ttl_at_or_below_zero_fails_fast(ttl_seconds: int) -> None:
    # 防御：TTLCache(ttl=0) 不报错但写入即过期（永不命中的死缓存）。
    with pytest.raises(ValueError, match="positive"):
        L1CacheClient(ttl_seconds=ttl_seconds)


@pytest.mark.parametrize("maxsize", [0, -1])
def test_maxsize_below_one_fails_fast(maxsize: int) -> None:
    # 防御：cachetools 延迟到首次写入才抛内部 ValueError（"value too
    # large"），构造期 fail-fast 给出明确口径。
    with pytest.raises(ValueError, match="at least 1"):
        L1CacheClient(maxsize=maxsize)


def test_recent_intent_rejects_fact_card_and_unknown_values() -> None:
    l1 = L1CacheClient()

    with pytest.raises(TypeError, match="IntentTag"):
        l1.set_recent_intent(SESSION_A, _card())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="IntentTag"):
        l1.set_recent_intent(SESSION_A, "DISCOUNT")  # type: ignore[arg-type]


def test_followup_count_rejects_non_int() -> None:
    l1 = L1CacheClient()

    with pytest.raises(TypeError, match="int"):
        l1.set_followup_count(SESSION_A, _card())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int"):
        l1.set_followup_count(SESSION_A, "3")  # type: ignore[arg-type]
    # bool 是 int 子类：True/False 不得穿透类型拒绝口径。
    with pytest.raises(TypeError, match="int"):
        l1.set_followup_count(SESSION_A, True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int"):
        l1.set_followup_count(SESSION_A, False)  # type: ignore[arg-type]


def test_handoff_status_rejects_non_str() -> None:
    l1 = L1CacheClient()

    with pytest.raises(TypeError, match="str"):
        l1.set_handoff_status(SESSION_A, _card())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="str"):
        l1.set_handoff_status(SESSION_A, 1)  # type: ignore[arg-type]


# ---- 并发读写（锁保护）----


def test_concurrent_writes_and_reads_stay_consistent() -> None:
    l1 = L1CacheClient()
    sessions = [f"sess-{i:03d}" for i in range(16)]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        session_id = sessions[index % len(sessions)]
        try:
            for n in range(200):
                l1.set_followup_count(session_id, n)
                l1.set_recent_intent(session_id, "PRICE_INQUIRY")
                assert l1.get_followup_count(session_id) is not None
                assert l1.get_recent_intent(session_id) == "PRICE_INQUIRY"
        except BaseException as exc:  # noqa: BLE001 —— 竞态损坏即失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
