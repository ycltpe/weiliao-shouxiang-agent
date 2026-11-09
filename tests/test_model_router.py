"""模型路由与预算护栏单测（Story 4.6，AC 第 1/2/3/4 条）。

fake clock + 双替身 chat 客户端覆盖 spec I/O & Edge-Case Matrix 全分支：
名单内简单意图走 mini、名单外/intent=None 走主模型、滑窗累计 ≥ 阈值
收紧（forced_mini）、窗口回落自动恢复（recovered）、usage 缺失按 0
计数、判定不计数（account 才记账）、构造 fail-fast（非正/非有限预算或
窗口、名单含未知意图标签、alias 空串）、指标回调与线程安全。零网络。
"""

import math
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from weiliao_agent.llm.client import OpenAiGenerationClient
from weiliao_agent.llm.model_router import (
    KNOWN_INTENTS,
    OUTCOME_FORCED_MINI,
    OUTCOME_MINI,
    OUTCOME_PRIMARY,
    OUTCOME_RECOVERED,
    ModelRouter,
)
from weiliao_agent.observability.metrics import (
    ModelRoutingMetricsCollector,
    reset_model_routing_metrics,
)


class _FakeClock:
    """可控单调时钟：advance 推进，注入 router 控制滑窗边界。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubChat:
    """返回固定文本与 usage 的 chat 替身。"""

    def __init__(self, text: str, usage: dict[str, int] | None) -> None:
        self.calls = 0
        self._text = text
        self._usage = usage

    def invoke(self, messages: Any) -> Any:
        self.calls += 1
        text, usage = self._text, self._usage

        class _Response:
            content = text
            usage_metadata = usage

        return _Response()


def _clients(
    usage: dict[str, int] | None = None,
) -> tuple[OpenAiGenerationClient, OpenAiGenerationClient, _StubChat, _StubChat]:
    primary_chat = _StubChat("主模型应答", usage)
    mini_chat = _StubChat("mini 应答", usage)
    return (
        OpenAiGenerationClient(model="gpt-4o", chat_model=primary_chat),
        OpenAiGenerationClient(model="gpt-4o-mini", chat_model=mini_chat),
        primary_chat,
        mini_chat,
    )


def _router(
    *,
    clock: _FakeClock | None = None,
    budget_tokens: int = 100,
    window_seconds: float = 60.0,
    simple_intents: frozenset[str] = frozenset({"GREETING", "LOCATION"}),
    metrics: ModelRoutingMetricsCollector | None = None,
) -> ModelRouter:
    primary, mini, _, _ = _clients()
    return ModelRouter(
        primary,
        mini,
        simple_intents=simple_intents,
        budget_tokens=budget_tokens,
        window_seconds=window_seconds,
        metrics=metrics,
        clock=clock or _FakeClock(),
    )


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    # 模块级指标单例跨测试共享：每个用例前后清零，隔离口径断言。
    reset_model_routing_metrics()
    yield
    reset_model_routing_metrics()


def test_simple_intent_routes_to_mini() -> None:
    primary, mini, primary_chat, mini_chat = _clients()
    router = ModelRouter(primary, mini, clock=_FakeClock())

    decision = router.select("GREETING")

    assert decision.client is mini
    assert decision.outcome == OUTCOME_MINI
    assert decision.transition is None
    assert mini_chat.calls == 0 and primary_chat.calls == 0  # select 零调用


def test_complex_intent_and_none_route_to_primary() -> None:
    primary, mini, _, _ = _clients()
    router = ModelRouter(primary, mini, clock=_FakeClock())

    complex_decision = router.select("PRICE_INQUIRY")
    none_decision = router.select(None)

    assert complex_decision.client is primary
    assert complex_decision.outcome == OUTCOME_PRIMARY
    assert none_decision.client is primary
    assert none_decision.outcome == OUTCOME_PRIMARY


def test_select_does_not_account_usage() -> None:
    # 判定不计数：只有 account（成功生成后调用）才向滑窗记账。
    router = _router(clock=_FakeClock())

    router.select("GREETING")

    assert router.snapshot() == {"tightened": False, "window_tokens": 0}


def test_account_accumulates_tokens_into_window() -> None:
    clock = _FakeClock()
    router = _router(clock=clock)

    decision = router.select("GREETING")
    router.account(decision, {"total_tokens": 40, "input_tokens": 30})

    assert router.snapshot()["window_tokens"] == 40


def test_usage_missing_counts_zero() -> None:
    # 响应无 usage_metadata（替身）：按 0 token 计数，预算不受影响。
    clock = _FakeClock()
    router = _router(clock=clock, budget_tokens=10)

    decision = router.select("GREETING")
    router.account(decision, {})

    assert router.snapshot() == {"tightened": False, "window_tokens": 0}


def test_budget_exceeded_forces_mini_regardless_of_intent() -> None:
    # 滑窗累计 ≥ 阈值：后续请求（无论意图）强制 mini（AC 第 2 条）。
    clock = _FakeClock()
    router = _router(clock=clock, budget_tokens=100)
    decision = router.select("PRICE_INQUIRY")  # 名单外意图先走主模型
    router.account(decision, {"total_tokens": 100})

    forced = router.select("PRICE_INQUIRY")

    assert forced.outcome == OUTCOME_FORCED_MINI
    assert forced.client.model == "gpt-4o-mini"
    assert router.tightened is True
    # 收紧期间的常规名单意图同样强制 mini。
    assert router.select("GREETING").outcome == OUTCOME_FORCED_MINI


def test_forced_mini_recovers_after_window_slide() -> None:
    # 窗口消耗回落到阈值以下：自动恢复常规路由（AC 第 3 条）。
    clock = _FakeClock()
    router = _router(clock=clock, budget_tokens=100)
    router.account(router.select("PRICE_INQUIRY"), {"total_tokens": 100})
    assert router.select("PRICE_INQUIRY").outcome == OUTCOME_FORCED_MINI

    clock.advance(61.0)  # 条目全部滑出窗口
    recovered = router.select("PRICE_INQUIRY")

    assert recovered.outcome == OUTCOME_PRIMARY
    assert recovered.transition == OUTCOME_RECOVERED
    assert router.tightened is False
    # 恢复后名单意图恢复 mini 常规路由。
    assert router.select("GREETING").outcome == OUTCOME_MINI


def test_tighten_counts_over_limit_once_per_episode() -> None:
    # 收紧进入只记一次超限：持续收紧期反复 select 不得重复计数。
    metrics = ModelRoutingMetricsCollector()
    clock = _FakeClock()
    router = _router(clock=clock, budget_tokens=10, metrics=metrics)
    router.account(router.select("PRICE_INQUIRY"), {"total_tokens": 10})

    router.select("GREETING")
    router.select("LOCATION")
    router.select("PRICE_INQUIRY")

    snapshot = metrics.snapshot()
    assert snapshot["over_limits"] == 1
    assert snapshot["tightened"] is True


def test_metrics_recorded_per_successful_call() -> None:
    metrics = ModelRoutingMetricsCollector()
    clock = _FakeClock()
    router = _router(clock=clock, budget_tokens=1000, metrics=metrics)

    router.account(router.select("PRICE_INQUIRY"), {"total_tokens": 60})
    router.account(router.select("GREETING"), {"total_tokens": 10})
    router.account(router.select("LOCATION"), {"total_tokens": 5})

    snapshot = metrics.snapshot()
    assert snapshot["primary_calls"] == 1
    assert snapshot["primary_tokens"] == 60
    assert snapshot["mini_calls"] == 2
    assert snapshot["mini_tokens"] == 15


def test_metrics_fail_safe_when_collector_raises() -> None:
    class _BoomCollector:
        def __getattr__(self, name: str) -> Any:
            msg = "metrics boom"
            raise RuntimeError(msg)

    clock = _FakeClock()
    router = _router(clock=clock, metrics=_BoomCollector())  # type: ignore[arg-type]
    router.account(router.select("GREETING"), {"total_tokens": 10_000})
    # 埋点异常吞掉：判定与记账照常，绝不阻断生成路径。
    assert router.select("GREETING").outcome == OUTCOME_FORCED_MINI


@pytest.mark.parametrize("budget_tokens", [0, -1, 1.5, True])
def test_invalid_budget_fails_fast(budget_tokens: Any) -> None:
    with pytest.raises(ValueError, match="budget_tokens"):
        _router(budget_tokens=budget_tokens)


@pytest.mark.parametrize("window_seconds", [0.0, -1.0, math.inf, math.nan])
def test_invalid_window_fails_fast(window_seconds: float) -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        _router(window_seconds=window_seconds)


def test_unknown_simple_intent_label_fails_fast() -> None:
    with pytest.raises(ValueError, match="simple_intents"):
        _router(simple_intents=frozenset({"GREETING", "BOGUS"}))


def test_empty_client_alias_fails_fast() -> None:
    primary, mini, _, _ = _clients()

    class _EmptyAliasClient:
        model = ""

        def generate(self, messages: Any) -> Any:
            msg = "should not be called"
            raise AssertionError(msg)

    with pytest.raises(ValueError, match="mini_client"):
        ModelRouter(primary, _EmptyAliasClient())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="primary_client"):
        ModelRouter(_EmptyAliasClient(), mini)  # type: ignore[arg-type]


def test_known_intents_covers_full_tag_vocab() -> None:
    # 名单校验词表与 models/intent.py IntentTag 枚举对齐（防两处漂移）。
    assert frozenset(
        {
            "GREETING",
            "PRICE_INQUIRY",
            "AVAILABILITY",
            "LOCATION",
            "PHOTO_REQUEST",
            "NEGOTIATION",
            "LOAN_POLICY",
            "COMPLAINT",
            "UNKNOWN",
        }
    ) == KNOWN_INTENTS


def test_concurrent_select_and_account_are_exact() -> None:
    # 线程安全：子图 executor 线程内并发判定/记账，计数须精确。
    metrics = ModelRoutingMetricsCollector()
    router = _router(budget_tokens=10**9, metrics=metrics)
    workers = 16
    per_worker = 50

    def work(i: int) -> None:
        for _ in range(per_worker):
            decision = router.select("GREETING" if i % 2 else "PRICE_INQUIRY")
            router.account(decision, {"total_tokens": 1})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))

    total = workers * per_worker
    snapshot = metrics.snapshot()
    assert snapshot["primary_calls"] + snapshot["mini_calls"] == total
    assert snapshot["primary_tokens"] + snapshot["mini_tokens"] == total
    assert router.snapshot()["window_tokens"] == total
