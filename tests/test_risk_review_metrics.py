"""审核通过率指标单测（Story 4.4，AC 第 3/4/5 条）。

覆盖口径全场景（spec Boundaries & Constraints）：pass_rate = passed/total
（分母含超时/错误）、分母 0 返回 None、并发计数精确（ThreadPoolExecutor
同热路径）、重置隔离、模块级单例注入重置、定时结构化日志 JSON 可解析、
样本 <20 不告警防抖动、跌破阈值（默认 0.95）发射 ERROR 级
``risk_review_alert``、阈值越界 fail-fast。
"""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.observability.metrics import (
    MIN_RISK_REVIEW_ALERT_SAMPLES,
    RiskReviewMetricsCollector,
    get_risk_review_metrics,
    reset_risk_review_metrics,
)

_LOGGER = "weiliao_agent.observability.metrics"


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    # 模块级单例跨测试共享：每个用例前后清零，隔离口径断言。
    reset_risk_review_metrics()
    yield
    reset_risk_review_metrics()


def test_snapshot_zero_denominator_pass_rate_none() -> None:
    snapshot = RiskReviewMetricsCollector().snapshot()
    assert snapshot["total"] == 0
    assert snapshot["passed"] == 0
    assert snapshot["rejected"] == 0
    assert snapshot["timeout"] == 0
    assert snapshot["error"] == 0
    assert snapshot["pass_rate"] is None


def test_pass_rate_counts_timeouts_and_errors_as_failures() -> None:
    # 口径：分母 = 实际审核调用数（含超时/错误），分子 = passed。
    collector = RiskReviewMetricsCollector()
    collector.record("passed")
    collector.record("passed")
    collector.record("rejected")
    collector.record("timeout")
    collector.record("error")

    snapshot = collector.snapshot()
    assert snapshot["total"] == 5
    assert snapshot["passed"] == 2
    assert snapshot["rejected"] == 1
    assert snapshot["timeout"] == 1
    assert snapshot["error"] == 1
    assert snapshot["pass_rate"] == pytest.approx(0.4)


def test_reset_returns_counters_to_zero_and_restores_default_threshold() -> None:
    collector = RiskReviewMetricsCollector()
    collector.record("passed")
    collector.set_pass_rate_threshold(0.8)

    collector.reset()

    snapshot = collector.snapshot()
    assert snapshot["total"] == 0
    assert snapshot["pass_rate"] is None
    assert collector.pass_rate_threshold == 0.95


def test_concurrent_recordings_are_exact() -> None:
    # 线程安全：过滤器在 ThreadPoolExecutor 线程内执行，计数须精确。
    collector = RiskReviewMetricsCollector()
    workers = 16
    per_worker = 50
    outcomes = ("passed", "rejected", "timeout", "error")

    def work(i: int) -> None:
        for j in range(per_worker):
            collector.record(outcomes[(i + j) % 4])  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))

    total = workers * per_worker
    per_outcome = total // 4
    snapshot = collector.snapshot()
    assert snapshot["total"] == total
    assert snapshot["passed"] == per_outcome
    assert snapshot["rejected"] == per_outcome
    assert snapshot["timeout"] == per_outcome
    assert snapshot["error"] == per_outcome
    assert snapshot["pass_rate"] == pytest.approx(0.25)


def test_module_singleton_and_reset_isolation() -> None:
    collector = get_risk_review_metrics()
    collector.record("passed")
    assert get_risk_review_metrics().snapshot()["passed"] == 1

    reset = reset_risk_review_metrics()

    assert reset is collector  # 重置同一单例（读数入口引用不变）
    assert collector.snapshot()["total"] == 0


def test_set_threshold_out_of_range_fails_fast() -> None:
    collector = RiskReviewMetricsCollector()
    for value in (-0.1, 1.01):
        with pytest.raises(ValueError, match="pass_rate_threshold"):
            collector.set_pass_rate_threshold(value)
    # 越界写入不生效。
    assert collector.pass_rate_threshold == 0.95


def test_emit_metrics_log_is_structured_json(caplog: pytest.LogCaptureFixture) -> None:
    get_risk_review_metrics().record("passed")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_risk_review_metrics_log()

    record = next(r for r in caplog.records if r.name == _LOGGER)
    payload = json.loads(record.message)  # 消息体即纯 JSON，无文本前缀
    assert payload["event"] == "risk_review_metrics"
    assert payload["total"] == 1
    assert payload["passed"] == 1
    assert payload["pass_rate"] == 1.0
    assert payload["pass_rate_threshold"] == 0.95


def test_no_alert_when_below_min_samples(caplog: pytest.LogCaptureFixture) -> None:
    # 样本 <20 不告警防抖动：19 个样本全失败也不发射告警。
    collector = get_risk_review_metrics()
    for _ in range(MIN_RISK_REVIEW_ALERT_SAMPLES - 1):
        collector.record("rejected")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_risk_review_metrics_log()

    assert [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR] == []


def test_alert_fired_when_pass_rate_below_threshold(caplog: pytest.LogCaptureFixture) -> None:
    # AC 第 4 条：样本 ≥20 且通过率跌破阈值（默认 0.95）→ ERROR 告警。
    collector = get_risk_review_metrics()
    passed = MIN_RISK_REVIEW_ALERT_SAMPLES - 2  # 18/20 = 0.9 < 0.95
    for _ in range(passed):
        collector.record("passed")
    collector.record("rejected")
    collector.record("timeout")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_risk_review_metrics_log()

    alerts = [
        r
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.ERROR and "risk_review_alert" in r.message
    ]
    assert len(alerts) == 1
    payload = json.loads(alerts[0].message)
    assert payload["event"] == "risk_review_alert"
    assert payload["pass_rate"] == pytest.approx(0.9)
    assert payload["pass_rate_threshold"] == 0.95
    assert payload["total"] == MIN_RISK_REVIEW_ALERT_SAMPLES


def test_no_alert_when_pass_rate_at_or_above_threshold(caplog: pytest.LogCaptureFixture) -> None:
    # 19/20 = 0.95 = 阈值：不告警（跌破才告警，非跌破等于）。
    collector = get_risk_review_metrics()
    for _ in range(MIN_RISK_REVIEW_ALERT_SAMPLES - 1):
        collector.record("passed")
    collector.record("rejected")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_risk_review_metrics_log()

    errors = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR]
    assert errors == []


def test_alert_respects_custom_threshold(caplog: pytest.LogCaptureFixture) -> None:
    collector = get_risk_review_metrics()
    collector.set_pass_rate_threshold(0.5)
    for _ in range(12):  # 12/20 = 0.6 >= 自定义阈值 0.5：不告警
        collector.record("passed")
    for _ in range(8):
        collector.record("rejected")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_risk_review_metrics_log()

    errors = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR]
    assert errors == []  # 0.6 >= 0.5：不告警

    collector.set_pass_rate_threshold(0.8)  # 0.6 < 0.8：告警
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        metrics_module.emit_risk_review_metrics_log()

    alerts = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "risk_review_alert" in r.message
    ]
    assert len(alerts) == 1
