"""受控生成子图单测（Story 4.2，AC 第 1/4 条）。

替身客户端覆盖：进程内 ``.invoke()`` 同步返回生成文本（不依赖
LangGraph Runtime）；生成成功经 ``tracing.record_generation`` 记录模型
名与 token 用量；空消息 fail-fast（reason=invalid_input）不触达模型；
fact_card=None 照常生成。
"""

from datetime import UTC, datetime
from typing import Any

import pytest

import weiliao_agent.llm as llm_module
from weiliao_agent.llm import (
    LlmGenerationError,
    LlmGenerationSubgraph,
    ModelRouter,
    OpenAiGenerationClient,
)
from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.observability import tracing
from weiliao_agent.observability.metrics import ModelRoutingMetricsCollector

TRACE_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


class _StubChat:
    """返回固定响应的 chat 替身；记录收到的 messages。"""

    def __init__(self, text: str = "您好，这是受控生成的应答。") -> None:
        self.calls = 0
        self.seen_messages: list[Any] = []
        self._text = text

    def invoke(self, messages: Any) -> Any:
        self.calls += 1
        self.seen_messages.append(messages)

        class _Response:
            content = self._text
            usage_metadata = USAGE

        return _Response()


def _fact_card() -> FactCard:
    return FactCard.model_validate(
        {
            "house_id": "house-001",
            "status": "active",
            "price_bucket": 12,
            "fact_version": 3,
            "as_of": datetime(2026, 11, 6, 10, 0, tzinfo=UTC),
            "dialogue_summary": ["买家上次问过朝向"],
        }
    )


def _subgraph(chat: _StubChat) -> LlmGenerationSubgraph:
    return LlmGenerationSubgraph(OpenAiGenerationClient(chat_model=chat))


def test_generate_returns_text_in_process_without_runtime() -> None:
    chat = _StubChat()

    text = _subgraph(chat).generate(
        "这套房子什么价位？", fact_card=_fact_card(), intent="PRICE_INQUIRY"
    )

    assert text == "您好，这是受控生成的应答。"
    assert chat.calls == 1


def test_success_records_model_and_usage_via_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []

    def spy(
        trace_id: str | None,
        *,
        model: str,
        usage: Any,
        cost: Any,
        name: str = "model.generation",
    ) -> None:
        recorded.append(
            {
                "trace_id": trace_id,
                "model": model,
                "usage": dict(usage),
                "cost": dict(cost),
            }
        )

    monkeypatch.setattr(tracing, "record_generation", spy)
    chat = _StubChat()

    _subgraph(chat).generate(
        "这套房子什么价位？",
        fact_card=_fact_card(),
        intent="PRICE_INQUIRY",
        trace_id=TRACE_ID,
    )

    assert recorded == [
        {
            "trace_id": TRACE_ID,
            "model": "gpt-4o",
            "usage": USAGE,
            "cost": {},
        }
    ]


def test_generate_only_consumes_resolved_message_fact_card_and_intent() -> None:
    chat = _StubChat()

    _subgraph(chat).generate(
        "这套房子什么价位？", fact_card=_fact_card(), intent="PRICE_INQUIRY"
    )

    (messages,) = chat.seen_messages
    combined = "\n".join(str(m.content) for m in messages)
    assert "这套房子什么价位？" in combined  # 只消费 resolved_message
    assert "house-001" in combined  # fact_card 三段渲染进 prompt
    assert "价格询问" in combined


@pytest.mark.parametrize("empty", [None, "", "  \n\t "])
def test_empty_resolved_message_fails_fast_without_calling_model(
    empty: str | None,
) -> None:
    chat = _StubChat()

    with pytest.raises(LlmGenerationError) as exc_info:
        _subgraph(chat).generate(empty, fact_card=_fact_card())  # type: ignore[arg-type]

    assert exc_info.value.reason == "invalid_input"
    assert chat.calls == 0


def test_missing_fact_card_still_generates() -> None:
    chat = _StubChat()

    text = _subgraph(chat).generate("你好，还在租吗？", fact_card=None, intent="GREETING")

    assert text == "您好，这是受控生成的应答。"
    assert chat.calls == 1


def test_client_failure_propagates_unified_error() -> None:
    class _FailingChat:
        def invoke(self, messages: Any) -> Any:
            msg = "boom"
            raise RuntimeError(msg)

    subgraph = LlmGenerationSubgraph(OpenAiGenerationClient(chat_model=_FailingChat()))

    with pytest.raises(LlmGenerationError) as exc_info:
        subgraph.generate("这套房子什么价位？")

    assert exc_info.value.reason == "RuntimeError"


def test_module_level_generate_lazily_builds_and_reuses_default_subgraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecordingSubgraph:
        """记录 generate 调用的替身；实例数即装配次数。"""

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def generate(self, resolved_message: str, **kwargs: Any) -> str:
            self.calls.append({"resolved_message": resolved_message, **kwargs})
            return "应答"

    built: list[_RecordingSubgraph] = []

    def _factory() -> _RecordingSubgraph:
        instance = _RecordingSubgraph()
        built.append(instance)
        return instance

    monkeypatch.setattr(llm_module, "_default_subgraph", None)
    monkeypatch.setattr(llm_module, "LlmGenerationSubgraph", _factory)

    first = llm_module.generate(
        "第一条", fact_card=None, intent="GREETING", trace_id=TRACE_ID
    )
    second = llm_module.generate("第二条")

    assert first == second == "应答"
    assert len(built) == 1  # 惰性装配恰好一次，并发首调用复用同一实例
    (stub,) = built
    assert stub.calls == [
        {
            "resolved_message": "第一条",
            "fact_card": None,
            "intent": "GREETING",
            "trace_id": TRACE_ID,
        },
        {
            "resolved_message": "第二条",
            "fact_card": None,
            "intent": None,
            "trace_id": None,
        },
    ]


# ---- Story 4.6：可选 router 注入的生成节点调度（I/O 矩阵全分支）----


class _FakeClock:
    """可控单调时钟：注入 router 控制滑窗边界。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubChatWithUsage:
    """返回固定文本与 usage 的 chat 替身（主/mini 各一）。"""

    def __init__(self, text: str, usage: dict[str, int]) -> None:
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


class _RoutingFixture:
    """主/mini 双 client + router + 子图装配（fake clock，零网络）。"""

    def __init__(self, *, budget_tokens: int = 50) -> None:
        self.clock = _FakeClock()
        self.primary_chat = _StubChatWithUsage("主模型应答", {"total_tokens": 60})
        self.mini_chat = _StubChatWithUsage("mini 应答", {"total_tokens": 10})
        self.primary = OpenAiGenerationClient(
            model="gpt-4o", chat_model=self.primary_chat
        )
        self.mini = OpenAiGenerationClient(model="gpt-4o-mini", chat_model=self.mini_chat)
        self.router = ModelRouter(
            self.primary,
            self.mini,
            simple_intents=frozenset({"GREETING", "LOCATION"}),
            budget_tokens=budget_tokens,
            window_seconds=60.0,
            clock=self.clock,
        )
        self.subgraph = LlmGenerationSubgraph(self.primary, router=self.router)


def test_router_simple_intent_generates_with_mini_and_records_real_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 第 1 条：名单内简单意图用 mini alias 生成，generation 观测记
    # 真实模型名与 token 用量（既有 cost={} 口径不动）。
    fixture = _RoutingFixture()
    recorded: list[dict[str, Any]] = []

    def spy(
        trace_id: str | None,
        *,
        model: str,
        usage: Any,
        cost: Any,
        name: str = "model.generation",
    ) -> None:
        recorded.append({"model": model, "usage": dict(usage), "cost": dict(cost)})

    monkeypatch.setattr(tracing, "record_generation", spy)

    text = fixture.subgraph.generate("你好", intent="GREETING", trace_id=TRACE_ID)

    assert text == "mini 应答"
    assert fixture.mini_chat.calls == 1
    assert fixture.primary_chat.calls == 0
    assert recorded == [{"model": "gpt-4o-mini", "usage": {"total_tokens": 10}, "cost": {}}]


def test_router_complex_intent_generates_with_primary() -> None:
    fixture = _RoutingFixture()

    text = fixture.subgraph.generate("这套房子什么价位？", intent="PRICE_INQUIRY")

    assert text == "主模型应答"
    assert fixture.primary_chat.calls == 1
    assert fixture.mini_chat.calls == 0


def test_router_budget_exceeded_forces_mini_and_traces_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 第 2 条：滑窗累计 ≥ 阈值后，后续请求（无论意图）强制 mini，
    # trace 留 forced_mini 事件。
    fixture = _RoutingFixture(budget_tokens=50)
    events: list[dict[str, Any]] = []

    def spy(trace_id: str | None, *, outcome: Any, intent: Any) -> None:
        events.append({"outcome": outcome, "intent": intent})

    monkeypatch.setattr(tracing, "record_model_routing_event", spy)

    first = fixture.subgraph.generate("这套房子什么价位？", intent="PRICE_INQUIRY")
    second = fixture.subgraph.generate("还在租吗？", intent="AVAILABILITY")

    assert first == "主模型应答"
    assert second == "mini 应答"  # 收紧期名单外意图同样强制 mini
    assert fixture.primary_chat.calls == 1
    assert fixture.mini_chat.calls == 1
    assert events == [
        {"outcome": "primary", "intent": "PRICE_INQUIRY"},
        {"outcome": "forced_mini", "intent": "AVAILABILITY"},
        # code review Epic 4 补丁：进入收紧的迁移瞬间补记 tightened 事件
        # （ERROR 级成本异常锚点，仿 recovered 迁移口径）。
        {"outcome": "tightened", "intent": "AVAILABILITY"},
    ]


def test_router_recovers_after_window_slide_and_traces_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 第 3 条：滑窗消耗回落到阈值以下自动恢复常规路由，trace 留
    # recovered 事件。
    fixture = _RoutingFixture(budget_tokens=50)
    events: list[dict[str, Any]] = []

    def spy(trace_id: str | None, *, outcome: Any, intent: Any) -> None:
        events.append({"outcome": outcome, "intent": intent})

    monkeypatch.setattr(tracing, "record_model_routing_event", spy)

    fixture.subgraph.generate("这套房子什么价位？", intent="PRICE_INQUIRY")  # 60 tokens
    fixture.subgraph.generate("还在租吗？", intent="AVAILABILITY")  # 收紧期 forced mini
    fixture.clock.advance(61.0)  # 旧条目滑出窗口
    text = fixture.subgraph.generate("你好", intent="GREETING")

    assert text == "mini 应答"  # 恢复后名单意图恢复 mini 常规路由
    assert events == [
        {"outcome": "primary", "intent": "PRICE_INQUIRY"},
        {"outcome": "forced_mini", "intent": "AVAILABILITY"},
        {"outcome": "tightened", "intent": "AVAILABILITY"},
        {"outcome": "mini", "intent": "GREETING"},
        {"outcome": "recovered", "intent": "GREETING"},
    ]


def test_router_select_failure_falls_back_to_primary_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 护栏判定自身异常：吞掉并按常规路由（主模型）放行，绝不阻断生成。
    fixture = _RoutingFixture()

    class _BoomRouter:
        def select(self, intent: Any) -> Any:
            msg = "guardrail boom"
            raise RuntimeError(msg)

        def account(self, decision: Any, usage: Any) -> None:
            msg = "guardrail boom"
            raise RuntimeError(msg)

    subgraph = LlmGenerationSubgraph(fixture.primary, router=_BoomRouter())  # type: ignore[arg-type]
    text = subgraph.generate("你好", intent="GREETING")

    assert text == "主模型应答"
    assert fixture.primary_chat.calls == 1
    assert fixture.mini_chat.calls == 0


def test_router_generation_failure_does_not_consume_budget() -> None:
    # 生成失败不消耗预算：account 只在 generate 成功后触发——若被回归
    # 挪到 generate 之前，失败请求会静默双计且全库仍绿（防线测试）。
    class _FailingChat:
        def invoke(self, messages: Any) -> Any:
            msg = "model boom"
            raise RuntimeError(msg)

    clock = _FakeClock()
    collector = ModelRoutingMetricsCollector()
    primary = OpenAiGenerationClient(model="gpt-4o", chat_model=_FailingChat())
    mini = OpenAiGenerationClient(
        model="gpt-4o-mini",
        chat_model=_StubChatWithUsage("mini 应答", {"total_tokens": 10}),
    )
    router = ModelRouter(
        primary,
        mini,
        simple_intents=frozenset({"GREETING"}),
        budget_tokens=50,
        window_seconds=60.0,
        metrics=collector,
        clock=clock,
    )
    subgraph = LlmGenerationSubgraph(primary, router=router)

    with pytest.raises(LlmGenerationError):
        subgraph.generate("这套房子什么价位？", intent="PRICE_INQUIRY")  # 主模型路径失败

    assert router.snapshot() == {"tightened": False, "window_tokens": 0}
    snapshot = collector.snapshot()
    assert snapshot["primary_calls"] == 0
    assert snapshot["mini_calls"] == 0
    assert snapshot["primary_tokens"] == 0
    assert snapshot["mini_tokens"] == 0
