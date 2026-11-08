"""LLM 熔断器状态机单测（Story 4.5，AC 第 1/2/3 条）。

fake clock 注入控制冷却与步进；覆盖三态全分支：closed 放行/跳闸、
open 冷却期拒绝（零子图调用前提）、冷却满转 half-open、确定性取模
放量（无随机）、试探成功递增至 100% 闭合、试探失败回跳并重启冷却、
确定性失败不跳闸、构造 fail-fast、线程安全计数。另含组合根 env
fail-fast 与合法值子进程导入回读（仿 test_risk_review_composition_root
先例，擦洗继承环境，零网络）。
"""

import inspect
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from weiliao_agent.llm import client as client_module
from weiliao_agent.llm.circuit_breaker import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    LlmCircuitBreaker,
    is_transient_reason,
)
from weiliao_agent.observability.metrics import (
    LlmBreakerMetricsCollector,
    get_llm_breaker_metrics,
    reset_llm_breaker_metrics,
)


class _FakeClock:
    """可控单调时钟：advance 推进，注入 breaker 控制冷却边界。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    # 模块级单例跨测试共享：每个用例前后清零，隔离口径断言。
    reset_llm_breaker_metrics()
    yield
    reset_llm_breaker_metrics()


# reason 词表与 llm/client.py _classify_error 产出逐项对齐。
@pytest.mark.parametrize(
    "reason",
    ["rate_limit", "server_500", "server_503", "timeout", "attempt_timeout", "network"],
)
def test_transient_reasons_trip_breaker(reason: str) -> None:
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, clock=clock)

    assert breaker.before_request() is True
    assert breaker.on_failure(reason) is True

    assert breaker.state == STATE_OPEN
    # 后续请求在冷却期内被拒绝（调用方据此零子图调用）。
    assert breaker.before_request() is False
    assert breaker.before_request() is False


# 确定性失败（400 系 / 凭据类型名 / invalid_output / 未知类型名）不跳闸。
@pytest.mark.parametrize(
    "reason",
    ["http_400", "http_422", "invalid_output", "invalid_api_key", "ValueError"],
)
def test_deterministic_failures_never_trip(reason: str) -> None:
    metrics = LlmBreakerMetricsCollector()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, metrics=metrics)

    assert breaker.on_failure(reason) is False
    assert breaker.state == STATE_CLOSED
    assert breaker.before_request() is True  # 放行不受影响
    snapshot = metrics.snapshot()
    assert snapshot["trips"] == 0
    assert snapshot["requests"] == 1


def test_is_transient_reason_vocab() -> None:
    assert is_transient_reason("rate_limit")
    assert is_transient_reason("server_500")
    assert not is_transient_reason("http_429_like_but_4xx")
    assert not is_transient_reason("invalid_output")


def test_trip_vocab_matches_classifier_contract() -> None:
    """防漂移：跳闸词表是 llm/client.py _classify_error 可重试产出的
    手工镜像——两处漂移即故障判定静默错位。钉住 client 源码包含全部
    跳闸词产出，且每个词经 is_transient_reason 判真。"""
    source = inspect.getsource(client_module)
    for token in ('"rate_limit"', '"timeout"', '"attempt_timeout"', '"network"'):
        assert token in source, f"client _classify_error 不再产出 {token}"
    # 5xx 前缀产出：f"server_{exc.status_code}"（拼写漂移即红）。
    assert 'f"server_{exc.status_code}"' in source
    for reason in ("rate_limit", "timeout", "attempt_timeout", "network", "server_500"):
        assert is_transient_reason(reason)


def test_constructor_fails_fast_on_invalid_config() -> None:
    with pytest.raises(ValueError, match="cooldown_seconds"):
        LlmCircuitBreaker(cooldown_seconds=0)
    with pytest.raises(ValueError, match="cooldown_seconds"):
        LlmCircuitBreaker(cooldown_seconds=-1)
    for step in (0.0, -0.1, 1.01):
        with pytest.raises(ValueError, match="recovery_step"):
            LlmCircuitBreaker(recovery_step=step)


def test_closed_state_success_is_stats_only() -> None:
    metrics = LlmBreakerMetricsCollector()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, metrics=metrics)

    assert breaker.before_request() is True
    assert breaker.on_success() is False  # closed 态不推进恢复
    assert breaker.state == STATE_CLOSED
    assert metrics.snapshot()["requests"] == 1
    assert metrics.snapshot()["recoveries"] == 0


def test_open_denies_during_cooldown_then_half_opens() -> None:
    clock = _FakeClock()
    metrics = LlmBreakerMetricsCollector()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, metrics=metrics, clock=clock)
    breaker.on_failure("rate_limit")

    clock.advance(9.9)  # 冷却未满：仍拒绝
    assert breaker.before_request() is False

    clock.advance(0.2)  # 冷却已满：转入 half-open 并放行首个试探
    assert breaker.before_request() is True
    assert breaker.state == STATE_HALF_OPEN
    snapshot = metrics.snapshot()
    assert snapshot["skipped"] == 1
    assert snapshot["requests"] == 1


def test_half_open_admission_is_deterministic_modulo() -> None:
    # step=0.1 → N=10：k=1 时取模模式恒为 [放行, 拒×9, 放行, 拒×9, ...]。
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, recovery_step=0.1, clock=clock)
    breaker.on_failure("server_500")
    clock.advance(10)

    decisions = [breaker.before_request() for _ in range(20)]

    expected = [i % 10 == 0 for i in range(20)]
    assert decisions == expected


def test_probe_successes_admit_wider_then_close() -> None:
    clock = _FakeClock()
    metrics = LlmBreakerMetricsCollector()
    breaker = LlmCircuitBreaker(
        cooldown_seconds=10, recovery_step=0.1, metrics=metrics, clock=clock
    )
    breaker.on_failure("rate_limit")
    clock.advance(10)

    recovered: list[bool] = []
    for _ in range(9):  # 10% 起步、每次成功 +10%：第 9 次成功达 100% 闭合
        assert breaker.before_request() is True  # 每次试探须先被放量
        recovered.append(breaker.on_success())

    assert recovered[-1] is True  # 第 9 次成功令熔断器闭合
    assert all(r is False for r in recovered[:-1])
    assert breaker.state == STATE_CLOSED
    assert breaker.before_request() is True  # 闭合后全量放行
    snapshot = metrics.snapshot()
    assert snapshot["recoveries"] == 1
    assert snapshot["trips"] == 1


def test_probe_failure_reopens_and_restarts_cooldown() -> None:
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, recovery_step=0.1, clock=clock)
    breaker.on_failure("rate_limit")
    clock.advance(10)

    assert breaker.before_request() is True  # 首个试探被放量
    assert breaker.on_failure("timeout") is True  # 试探失败：回 open
    assert breaker.state == STATE_OPEN

    clock.advance(9.9)  # 冷却重启：未满仍拒绝
    assert breaker.before_request() is False
    clock.advance(0.2)
    assert breaker.before_request() is True  # 冷却再满：重新试探


def test_late_failure_while_open_does_not_extend_cooldown() -> None:
    # 并发迟到失败（请求放行后熔断器已 open）不得重启冷却窗口。
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, clock=clock)
    breaker.on_failure("rate_limit")
    clock.advance(5)
    assert breaker.on_failure("network") is False
    clock.advance(5)  # t=10：若冷却被重启则此处仍拒绝
    assert breaker.before_request() is True


def test_deterministic_failure_on_probe_does_not_reopen() -> None:
    # 试探请求的确定性失败（如凭据错误）不回跳：非 OpenAI 侧故障。
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, recovery_step=0.1, clock=clock)
    breaker.on_failure("rate_limit")
    clock.advance(10)

    assert breaker.before_request() is True
    assert breaker.on_failure("invalid_api_key") is False
    assert breaker.state == STATE_HALF_OPEN


def test_recovery_step_one_closes_after_single_probe() -> None:
    # step=1.0 → N=1：half-open 全量试探，一次成功即闭合。
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, recovery_step=1.0, clock=clock)
    breaker.on_failure("rate_limit")
    clock.advance(10)

    assert breaker.before_request() is True
    assert breaker.on_success() is True
    assert breaker.state == STATE_CLOSED


def test_concurrent_trips_count_exactly_once() -> None:
    # 线程安全：并发故障爆发只允许一次 closed→open 迁移与一次 trip 计数。
    metrics = LlmBreakerMetricsCollector()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, metrics=metrics)

    def fail(_: int) -> bool:
        return breaker.on_failure("rate_limit")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(fail, range(64)))

    assert sum(results) == 1
    assert metrics.snapshot()["trips"] == 1


def test_concurrent_consults_are_exact() -> None:
    # 线程安全：并发咨询的放行/拒绝计数须精确（过滤器在 executor 线程执行）。
    metrics = LlmBreakerMetricsCollector()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, metrics=metrics)

    def consult(_: int) -> bool:
        return breaker.before_request()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(consult, range(100)))

    assert all(results)  # closed 态全部放行
    assert metrics.snapshot()["requests"] == 100


def test_metrics_injection_optional_and_fail_safe() -> None:
    breaker = LlmCircuitBreaker(cooldown_seconds=10)  # None=不上报
    breaker.on_failure("rate_limit")
    assert breaker.before_request() is False

    class _RaisingMetrics:
        def record_request(self) -> None:
            raise RuntimeError("boom")

        def record_skipped(self) -> None:
            raise RuntimeError("boom")

        def record_trip(self) -> None:
            raise RuntimeError("boom")

        def record_recovery(self) -> None:
            raise RuntimeError("boom")

        def set_state(self, state: str) -> None:
            raise RuntimeError("boom")

    flaky = LlmCircuitBreaker(cooldown_seconds=10, metrics=_RaisingMetrics())  # type: ignore[arg-type]
    assert flaky.on_failure("rate_limit") is True  # 埋点异常不得影响状态机
    assert flaky.state == STATE_OPEN


def test_metrics_singleton_receives_breaker_signals() -> None:
    # 组合根接线口径：指标单例由 breaker 迁移点回写（/health 即读单例）。
    breaker = LlmCircuitBreaker(cooldown_seconds=10, metrics=get_llm_breaker_metrics())
    breaker.before_request()
    breaker.on_failure("server_503")
    breaker.before_request()  # skipped

    snapshot = get_llm_breaker_metrics().snapshot()
    assert snapshot["state"] == STATE_OPEN
    assert snapshot["requests"] == 1
    assert snapshot["skipped"] == 1
    assert snapshot["trips"] == 1
    assert snapshot["recoveries"] == 0


# ---- 组合根 env fail-fast 与合法值回读（子进程导入，零网络）----

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
_SCRUBBED = (
    "WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS",
    "WEILIAO_LLM_BREAKER_RECOVERY_STEP",
)


def _import_main(
    env_overrides: dict[str, str], program: str = "import weiliao_agent.main"
) -> subprocess.CompletedProcess[bytes]:
    # 擦洗继承环境：开发机/CI 全局导出的受测变量会改变子进程导入行为。
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED}
    env.update({"PYTHONPATH": str(_SRC), **env_overrides})
    return subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )


def test_invalid_cooldown_fails_startup_with_variable_name() -> None:
    for raw in ("0", "-1", "not-a-number", "nan", "inf"):
        result = _import_main({"WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS": raw})
        assert result.returncode != 0
        assert b"WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS" in result.stderr


def test_invalid_recovery_step_fails_startup_with_variable_name() -> None:
    for raw in ("0", "-0.5", "1.5", "not-a-number"):
        result = _import_main({"WEILIAO_LLM_BREAKER_RECOVERY_STEP": raw})
        assert result.returncode != 0
        assert b"WEILIAO_LLM_BREAKER_RECOVERY_STEP" in result.stderr


def test_valid_breaker_env_reaches_breaker_and_metrics_singleton() -> None:
    """合法 env 值须真的落到注入熔断器与指标单例（只测拒绝方向会让
    接线成死配置面）。子进程导入后回读接线点实际值并打印，主进程断言。"""
    result = _import_main(
        {
            "WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS": "7",
            "WEILIAO_LLM_BREAKER_RECOVERY_STEP": "0.5",
        },
        program=(
            "import weiliao_agent.main as main_module; "
            "from weiliao_agent.observability import metrics; "
            "breaker = main_module._llm_breaker; "
            "print(breaker._cooldown_seconds); "
            "print(breaker._recovery_step); "
            "print(breaker._steps_total); "
            "print(metrics.get_llm_breaker_metrics().snapshot()['state'])"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().split() == ["7.0", "0.5", "2", "closed"]


def test_breaker_wired_into_llm_generate_filter() -> None:
    """接线守护：删掉 main.py 的 breaker=_llm_breaker 注入，本测试即红。

    子进程遍历生产 app 装配链：/v1/messages 端点闭包持有编排器，链内
    LlmGenerateFilter 的 _breaker 必须与组合根构造的实例同一对象。
    """
    result = _import_main(
        {},
        program=(
            "import weiliao_agent.main as m; "
            "from weiliao_agent.filters.llm_generate import LlmGenerateFilter; "
            "from weiliao_agent.pipeline import PipelineOrchestrator; "
            "route = next(r for r in m.app.routes "
            "             if getattr(r, 'path', '') == '/v1/messages'); "
            "cells = route.endpoint.__closure__ or (); "
            "orch = next(c.cell_contents for c in cells "
            "            if isinstance(c.cell_contents, PipelineOrchestrator)); "
            "filt = next(f for f in orch._filters "
            "            if isinstance(f, LlmGenerateFilter)); "
            "print(filt._breaker is m._llm_breaker)"
        )
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().strip() == "True"
