"""模型路由指标单测（Story 4.6，AC 第 2/5 条）。

覆盖口径全场景：计数器快照（按模型调用数/token 累计/收紧状态/超限
次数/恢复次数）、forced_mini 归并 mini 口径、并发计数精确
（ThreadPoolExecutor 同热路径）、重置隔离、模块级单例注入重置、
定时结构化日志 JSON 可解析、收紧态发射 ERROR 级 ``model_routing_alert``、
非收紧态不告警、发射自身 fail-safe。
"""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.observability.metrics import (
    ModelRoutingMetricsCollector,
    get_model_routing_metrics,
    reset_model_routing_metrics,
)

_LOGGER = "weiliao_agent.observability.metrics"


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    # 模块级单例跨测试共享：每个用例前后清零，隔离口径断言。
    reset_model_routing_metrics()
    yield
    reset_model_routing_metrics()


def test_snapshot_zero_counters_and_untightened() -> None:
    snapshot = ModelRoutingMetricsCollector().snapshot()
    assert snapshot == {
        "primary_calls": 0,
        "primary_tokens": 0,
        "mini_calls": 0,
        "mini_tokens": 0,
        "tightened": False,
        "over_limits": 0,
        "recoveries": 0,
    }


def test_calls_and_tokens_recorded_per_model() -> None:
    collector = ModelRoutingMetricsCollector()
    collector.record_call("primary", 60)
    collector.record_call("mini", 10)
    collector.record_call("forced_mini", 5)  # 收紧期调用归并 mini 口径

    snapshot = collector.snapshot()
    assert snapshot["primary_calls"] == 1
    assert snapshot["primary_tokens"] == 60
    assert snapshot["mini_calls"] == 2
    assert snapshot["mini_tokens"] == 15


def test_tighten_and_recovery_transitions() -> None:
    collector = ModelRoutingMetricsCollector()
    collector.set_tightened(True)
    collector.record_over_limit()
    collector.record_over_limit()

    snapshot = collector.snapshot()
    assert snapshot["tightened"] is True
    assert snapshot["over_limits"] == 2

    collector.set_tightened(False)
    collector.record_recovery()
    assert collector.snapshot()["tightened"] is False
    assert collector.snapshot()["recoveries"] == 1


def test_reset_returns_counters_to_zero() -> None:
    collector = ModelRoutingMetricsCollector()
    collector.record_call("mini", 10)
    collector.set_tightened(True)
    collector.record_over_limit()

    collector.reset()

    assert collector.snapshot() == ModelRoutingMetricsCollector().snapshot()


def test_concurrent_recordings_are_exact() -> None:
    # 线程安全：路由在过滤器 executor 线程内回写，计数须精确。
    collector = ModelRoutingMetricsCollector()
    workers = 16
    per_worker = 50

    def work(i: int) -> None:
        for _ in range(per_worker):
            if i % 2:
                collector.record_call("primary", 2)
            else:
                collector.record_call("mini", 1)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))

    snapshot = collector.snapshot()
    assert snapshot["primary_calls"] == workers // 2 * per_worker
    assert snapshot["primary_tokens"] == workers // 2 * per_worker * 2
    assert snapshot["mini_calls"] == workers // 2 * per_worker
    assert snapshot["mini_tokens"] == workers // 2 * per_worker


def test_module_singleton_and_reset_isolation() -> None:
    collector = get_model_routing_metrics()
    collector.record_call("mini", 5)
    assert get_model_routing_metrics().snapshot()["mini_calls"] == 1

    reset = reset_model_routing_metrics()

    assert reset is collector  # 重置同一单例（读数入口引用不变）
    assert collector.snapshot()["mini_calls"] == 0


def test_emit_metrics_log_is_structured_json(caplog: pytest.LogCaptureFixture) -> None:
    collector = get_model_routing_metrics()
    collector.record_call("primary", 60)
    collector.record_call("forced_mini", 5)
    collector.set_tightened(True)
    collector.record_over_limit()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_model_routing_metrics_log()

    records = [r for r in caplog.records if r.name == _LOGGER]
    payload = json.loads(records[0].message)  # 消息体即纯 JSON，无文本前缀
    assert payload["event"] == "model_routing_metrics"
    assert payload["primary_calls"] == 1
    assert payload["mini_calls"] == 1  # forced_mini 归并 mini
    assert payload["mini_tokens"] == 5
    assert payload["tightened"] is True
    assert payload["over_limits"] == 1
    # 收紧态：除 INFO 指标行外追加 ERROR 级告警。
    alerts = [r for r in records if r.levelno == logging.ERROR]
    assert len(alerts) == 1
    alert = json.loads(alerts[0].message)
    assert alert["event"] == "model_routing_alert"
    assert alert["tightened"] is True


def test_no_alert_when_not_tightened(caplog: pytest.LogCaptureFixture) -> None:
    # 非收紧态（无成本异常进行中）：只发 INFO 指标行，不告警。
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_model_routing_metrics_log()

    errors = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR]
    assert errors == []


def test_emit_log_is_fail_safe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 发射自身 fail-safe：取数异常吞掉，不得向调用方（定时循环）上抛。
    def _boom() -> ModelRoutingMetricsCollector:
        raise RuntimeError("snapshot boom")

    monkeypatch.setattr(metrics_module, "get_model_routing_metrics", _boom)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_model_routing_metrics_log()  # 不抛即通过
