"""LLM 熔断器指标单测（Story 4.5，AC 第 4/5 条）。

覆盖口径全场景：计数器快照（state/requests/skipped/trips/recoveries）、
状态同步、并发计数精确（ThreadPoolExecutor 同热路径）、重置隔离、
模块级单例注入重置、定时结构化日志 JSON 可解析、非 closed 态发射
ERROR 级 ``llm_breaker_alert``、closed 态不告警、发射自身 fail-safe。
"""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.observability.metrics import (
    LlmBreakerMetricsCollector,
    get_llm_breaker_metrics,
    reset_llm_breaker_metrics,
)

_LOGGER = "weiliao_agent.observability.metrics"


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    # 模块级单例跨测试共享：每个用例前后清零，隔离口径断言。
    reset_llm_breaker_metrics()
    yield
    reset_llm_breaker_metrics()


def test_snapshot_zero_counters_and_closed_state() -> None:
    snapshot = LlmBreakerMetricsCollector().snapshot()
    assert snapshot == {
        "state": "closed",
        "requests": 0,
        "skipped": 0,
        "trips": 0,
        "recoveries": 0,
    }


def test_counters_and_state_recorded() -> None:
    collector = LlmBreakerMetricsCollector()
    collector.record_request()
    collector.record_request()
    collector.record_skipped()
    collector.record_trip()
    collector.set_state("open")

    snapshot = collector.snapshot()
    assert snapshot["state"] == "open"
    assert snapshot["requests"] == 2
    assert snapshot["skipped"] == 1
    assert snapshot["trips"] == 1
    assert snapshot["recoveries"] == 0

    collector.record_recovery()
    collector.set_state("closed")
    assert collector.snapshot()["recoveries"] == 1
    assert collector.snapshot()["state"] == "closed"


def test_reset_returns_counters_to_zero_and_closed_state() -> None:
    collector = LlmBreakerMetricsCollector()
    collector.record_trip()
    collector.set_state("half_open")

    collector.reset()

    assert collector.snapshot() == {
        "state": "closed",
        "requests": 0,
        "skipped": 0,
        "trips": 0,
        "recoveries": 0,
    }


def test_concurrent_recordings_are_exact() -> None:
    # 线程安全：熔断器在过滤器 executor 线程内回写，计数须精确。
    collector = LlmBreakerMetricsCollector()
    workers = 16
    per_worker = 50

    def work(i: int) -> None:
        for _ in range(per_worker):
            collector.record_request()
            collector.record_skipped()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))

    total = workers * per_worker
    snapshot = collector.snapshot()
    assert snapshot["requests"] == total
    assert snapshot["skipped"] == total


def test_module_singleton_and_reset_isolation() -> None:
    collector = get_llm_breaker_metrics()
    collector.record_trip()
    assert get_llm_breaker_metrics().snapshot()["trips"] == 1

    reset = reset_llm_breaker_metrics()

    assert reset is collector  # 重置同一单例（读数入口引用不变）
    assert collector.snapshot()["trips"] == 0


def test_emit_metrics_log_is_structured_json(caplog: pytest.LogCaptureFixture) -> None:
    collector = get_llm_breaker_metrics()
    collector.record_request()
    collector.record_trip()
    collector.set_state("open")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_llm_breaker_metrics_log()

    records = [r for r in caplog.records if r.name == _LOGGER]
    payload = json.loads(records[0].message)  # 消息体即纯 JSON，无文本前缀
    assert payload["event"] == "llm_breaker_metrics"
    assert payload["state"] == "open"
    assert payload["requests"] == 1
    assert payload["trips"] == 1
    # open 态：除 INFO 指标行外追加 ERROR 级告警。
    alerts = [r for r in records if r.levelno == logging.ERROR]
    assert len(alerts) == 1
    alert = json.loads(alerts[0].message)
    assert alert["event"] == "llm_breaker_alert"
    assert alert["state"] == "open"


def test_no_alert_when_closed(caplog: pytest.LogCaptureFixture) -> None:
    # closed 态（无降级进行中）：只发 INFO 指标行，不告警。
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_llm_breaker_metrics_log()

    errors = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR]
    assert errors == []


def test_alert_carries_half_open_state(caplog: pytest.LogCaptureFixture) -> None:
    get_llm_breaker_metrics().set_state("half_open")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_llm_breaker_metrics_log()

    alerts = [
        r
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.ERROR and "llm_breaker_alert" in r.message
    ]
    assert len(alerts) == 1
    assert json.loads(alerts[0].message)["state"] == "half_open"


def test_emit_log_is_fail_safe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 发射自身 fail-safe：序列化异常吞掉，不得向调用方（定时循环）上抛。
    def _boom() -> LlmBreakerMetricsCollector:
        raise RuntimeError("snapshot boom")

    monkeypatch.setattr(metrics_module, "get_llm_breaker_metrics", _boom)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_llm_breaker_metrics_log()  # 不抛即通过
