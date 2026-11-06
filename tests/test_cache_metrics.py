"""缓存命中率指标口径单测（Story 3.4）。

覆盖口径全场景（spec Boundaries & Constraints）：
hit_rate = 分子/分母计算、分母 0 返回 None（含仅 bypass 时）、
bypass 不计入分子/分母、并发计数精确（ThreadPoolExecutor 同热路径）、
重置隔离、模块级单例注入重置、定时结构化日志 JSON 可解析。
"""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.observability.metrics import (
    CacheMetricsCollector,
    get_cache_metrics,
    reset_cache_metrics,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    # 模块级单例跨测试共享：每个用例前后清零，隔离口径断言。
    reset_cache_metrics()
    yield
    reset_cache_metrics()


def test_snapshot_zero_denominator_hit_rate_none() -> None:
    # 分母 0（无任何实际查询）时命中率无定义：None 而非 0/0。
    snapshot = CacheMetricsCollector().snapshot()
    assert snapshot["lookups"] == 0
    assert snapshot["hits"] == 0
    assert snapshot["bypassed"] == 0
    assert snapshot["l1_hit"] == 0  # 预留并入位：当前无 L1 读消费者
    assert snapshot["hit_rate"] is None


def test_hit_rate_is_hits_over_lookups() -> None:
    # 口径：分子 = L0 命中，分母 = 非追问实际查询数（hit + miss）。
    collector = CacheMetricsCollector()
    collector.record_lookup(hit=True)
    collector.record_lookup(hit=True)
    collector.record_lookup(hit=False)

    snapshot = collector.snapshot()
    assert snapshot["l0_hit"] == 2
    assert snapshot["l0_miss"] == 1
    assert snapshot["hits"] == 2
    assert snapshot["lookups"] == 3
    assert snapshot["hit_rate"] == pytest.approx(2 / 3)


def test_bypass_not_in_numerator_or_denominator() -> None:
    # 口径：被 bypass 的追问完全不计入分子也不计入分母。
    collector = CacheMetricsCollector()
    collector.record_bypass()
    collector.record_bypass()

    snapshot = collector.snapshot()
    assert snapshot["bypassed"] == 2
    assert snapshot["lookups"] == 0
    assert snapshot["hit_rate"] is None


def test_mixed_bypass_and_lookups_hit_rate_excludes_bypass() -> None:
    collector = CacheMetricsCollector()
    collector.record_lookup(hit=True)
    collector.record_lookup(hit=False)
    collector.record_bypass()

    snapshot = collector.snapshot()
    assert snapshot["bypassed"] == 1
    assert snapshot["hit_rate"] == pytest.approx(0.5)  # 1/2，与 bypass 无关


def test_reset_returns_counters_to_zero() -> None:
    collector = CacheMetricsCollector()
    collector.record_lookup(hit=True)
    collector.record_bypass()

    collector.reset()

    snapshot = collector.snapshot()
    assert snapshot["lookups"] == 0
    assert snapshot["bypassed"] == 0
    assert snapshot["hit_rate"] is None


def test_concurrent_recordings_are_exact() -> None:
    # 线程安全：过滤器在 ThreadPoolExecutor 线程内执行，计数须精确。
    collector = CacheMetricsCollector()
    workers = 32
    per_worker = 50

    def work(i: int) -> None:
        for j in range(per_worker):
            if (i + j) % 4 == 0:
                collector.record_bypass()
            else:
                collector.record_lookup(hit=(i + j) % 2 == 0)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))

    total = workers * per_worker
    expected_bypassed = sum(
        1
        for i in range(workers)
        for j in range(per_worker)
        if (i + j) % 4 == 0
    )
    expected_hits = sum(
        1
        for i in range(workers)
        for j in range(per_worker)
        if (i + j) % 4 != 0 and (i + j) % 2 == 0
    )
    snapshot = collector.snapshot()
    assert snapshot["bypassed"] == expected_bypassed
    assert snapshot["lookups"] == total - expected_bypassed
    assert snapshot["hits"] == expected_hits
    assert snapshot["hit_rate"] == pytest.approx(expected_hits / (total - expected_bypassed))


def test_module_singleton_and_reset_isolation() -> None:
    # 单例注入重置：cache_lookup 热路径与读数入口共享同一计数器，
    # reset 后口径断言不相互污染。
    collector = get_cache_metrics()
    collector.record_lookup(hit=True)
    assert get_cache_metrics().snapshot()["l0_hit"] == 1

    reset = reset_cache_metrics()

    assert reset is collector  # 重置同一单例（读数入口引用不变）
    assert collector.snapshot()["lookups"] == 0
    assert collector.snapshot()["hit_rate"] is None


def test_emit_cache_metrics_log_is_structured_json(caplog: pytest.LogCaptureFixture) -> None:
    # 定时日志（A 方案第二读数）：单行 JSON 可解析，含分子/分母/命中率。
    get_cache_metrics().record_lookup(hit=True)
    with caplog.at_level(logging.INFO, logger="weiliao_agent.observability.metrics"):
        metrics_module.emit_cache_metrics_log()

    record = next(r for r in caplog.records if r.name == "weiliao_agent.observability.metrics")
    payload = json.loads(record.message)  # 消息体即纯 JSON，无文本前缀
    assert payload["event"] == "cache_metrics"
    assert payload["l0_hit"] == 1
    assert payload["lookups"] == 1
    assert payload["hits"] == 1
    assert payload["hit_rate"] == 1.0
