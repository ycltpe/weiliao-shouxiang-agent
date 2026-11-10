"""受控生成过滤器单测（Story 4.3，影子接入 I/O 矩阵全分支）。

替身子图（真实 ``LlmGenerationSubgraph`` + 替身 chat，不触达 OpenAI）
覆盖：卡片就绪正常生成、L0 miss 照常生成、纯 resolved_message 生成
输入（Story 6.3：原文零接触、缺失/空白跳过）、红线意图/非文本/空
消息跳过、生成异常吞掉记 trace、grounding 校验未过丢草稿记 trace；
另有完整管道走查（草稿挂信封、买家可见应答仍是模板兜底、
degraded=False）与 OpenAI 不可用（模块级入口异常）fail-safe 走查。
全部无网络。

Story 4.5 扩展：熔断器注入矩阵——open 期间跳过（零子图调用、信封
透传、DEFAULT 级 skip 事件）、OpenAI 故障跳闸（ERROR 级 tripped 事件）
且后续请求不再触达子图、成功信号推进步进恢复（recovered 事件）、
确定性失败（凭据/http_4xx）与 grounding 未过不跳闸。
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest

import weiliao_agent.filters.llm_generate as filter_module
from weiliao_agent.filters.llm_generate import LlmGenerateFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import RiskReviewFilter, RiskReviewVerdict
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.llm import (
    LlmGenerationError,
    LlmGenerationSubgraph,
    OpenAiGenerationClient,
)
from weiliao_agent.llm.circuit_breaker import LlmCircuitBreaker
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.models.intent import IntentTag
from weiliao_agent.observability import tracing
from weiliao_agent.pipeline import LLM_GENERATE_BUDGET_SECONDS, PipelineOrchestrator

TRACE_ID = "ab" * 16
DRAFT = "您好，这套房源目前在售，价格以页面展示为准。"


class _AlwaysPassClient:
    """审核客户端替身：恒放行（4.4 过审路径走查）。"""

    def review(
        self, *, text: str, intent: str | None, trace_id: str | None, house_id: str
    ) -> RiskReviewVerdict:
        return RiskReviewVerdict(passed=True, outcome="passed", latency_ms=1.0)


class _StubChat:
    """返回固定文本（或可抛错）的 chat 替身；记录收到的 messages。"""

    def __init__(self, text: str = DRAFT, error: Exception | None = None) -> None:
        self.calls = 0
        self.seen_messages: list[Any] = []
        self._text = text
        self._error = error

    def invoke(self, messages: Any) -> Any:
        self.calls += 1
        self.seen_messages.append(messages)
        if self._error is not None:
            raise self._error

        class _Response:
            content = self._text
            usage_metadata: dict[str, int] = {}

        return _Response()


def _subgraph(chat: _StubChat) -> LlmGenerationSubgraph:
    return LlmGenerationSubgraph(OpenAiGenerationClient(chat_model=chat))


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


def _envelope(**overrides: Any) -> Envelope:
    values: dict[str, Any] = {
        "session_id": "sess-001",
        "house_id": "house-001",
        "timestamp": "2026-11-06T10:00:00+08:00",
        "trace_id": TRACE_ID,
        "intent": "PRICE_INQUIRY",
        "message": "这套房子现在什么价位？",
        # Story 6.3 夹具口径：standalone 装配显式携带消解后消息。
        "resolved_message": "这套房子现在什么价位？",
    }
    values.update(overrides)
    return Envelope.model_validate(values)


class _FakeLangfuse:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@pytest.fixture
def fake_langfuse() -> Iterator[_FakeLangfuse]:
    """换入 _FakeLangfuse；teardown 恢复先前 client（快照式）。"""
    previous = tracing._client_or_none()
    client = _FakeLangfuse()
    tracing.set_langfuse_client(client)
    yield client
    tracing.set_langfuse_client(previous)


def _generate_events(client: _FakeLangfuse) -> list[dict[str, Any]]:
    return [e for e in client.events if e["name"] == "llm.generate_event"]


def _breaker_events(client: _FakeLangfuse) -> list[dict[str, Any]]:
    return [e for e in client.events if e["name"] == "llm.breaker_event"]


class _FakeClock:
    """可控单调时钟：注入熔断器控制冷却边界（Story 4.5 恢复矩阵）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_draft_attached_to_envelope_when_card_ready() -> None:
    chat = _StubChat()
    envelope = _envelope(fact_card=_fact_card())

    result = LlmGenerateFilter(subgraph=_subgraph(chat)).process(envelope)

    assert chat.calls == 1
    assert result.generated_reply == DRAFT
    assert result.fact_card is not None  # 其余信封字段不动
    assert envelope.generated_reply is None  # 原信封不被改写（model_copy）


def test_draft_still_produced_when_fact_card_missing() -> None:
    chat = _StubChat()

    result = LlmGenerateFilter(subgraph=_subgraph(chat)).process(_envelope())

    assert chat.calls == 1
    assert result.generated_reply == DRAFT


def test_generation_consumes_only_resolved_message() -> None:
    # Story 6.3 纯消解输入：resolved 非空白即唯一生成输入（逐字节），
    # 原文字段在场也不读取；resolved 缺失/空白跳过生成（零模型调用），
    # 即使原文有值。
    chat = _StubChat()
    subgraph = _subgraph(chat)

    # resolved 在场：prompt 逐字节消费 resolved，原文零接触。
    resolved = _envelope(
        resolved_message="消解后的自包含消息",
        message="原始的含指代消息",
    )
    LlmGenerateFilter(subgraph=subgraph).process(resolved)
    assert chat.calls == 1
    assert "消解后的自包含消息" in str(chat.seen_messages[-1][-1].content)
    assert "原始的含指代消息" not in str(chat.seen_messages[-1][-1].content)

    # resolved 缺失：跳过生成，原文不被读取（FR-6 纯消解输入）。
    fallback = _envelope(resolved_message=None)
    LlmGenerateFilter(subgraph=subgraph).process(fallback)
    assert chat.calls == 1  # 零新增模型调用

    # resolved 纯空白：同样跳过生成。
    blank_resolved = _envelope(
        resolved_message="   ",
        message="空白消解结果不回落到这条",
    )
    LlmGenerateFilter(subgraph=subgraph).process(blank_resolved)
    assert chat.calls == 1  # 零新增模型调用


@pytest.mark.parametrize(
    "redline_intent",
    ["NEGOTIATION", "LOAN_POLICY", "COMPLAINT"],
)
def test_redline_intents_skip_generation(redline_intent: str) -> None:
    chat = _StubChat()
    envelope = _envelope(intent=redline_intent)

    result = LlmGenerateFilter(subgraph=_subgraph(chat)).process(envelope)

    assert chat.calls == 0  # 不调模型
    assert result.generated_reply is None  # 不产生草稿


@pytest.mark.parametrize("media_type", ["other", "image", "voice"])
def test_non_text_media_skips_generation(media_type: str) -> None:
    chat = _StubChat()
    envelope = _envelope(media_type=media_type)

    result = LlmGenerateFilter(subgraph=_subgraph(chat)).process(envelope)

    assert chat.calls == 0
    assert result.generated_reply is None


def test_empty_message_skips_generation() -> None:
    chat = _StubChat()

    blank = LlmGenerateFilter(subgraph=_subgraph(chat)).process(
        _envelope(message="   ", resolved_message=None)
    )
    both_missing = LlmGenerateFilter(subgraph=_subgraph(chat)).process(
        _envelope(message=None, resolved_message=None)
    )

    assert chat.calls == 0
    assert blank.generated_reply is None
    assert both_missing.generated_reply is None


def test_generation_error_swallowed_and_traced(fake_langfuse: _FakeLangfuse) -> None:
    chat = _StubChat(error=LlmGenerationError("invalid_api_key", "no key"))
    envelope = _envelope(fact_card=_fact_card())

    result = LlmGenerateFilter(subgraph=_subgraph(chat)).process(envelope)

    assert result is envelope  # 信封原样透传
    events = _generate_events(fake_langfuse)
    assert len(events) == 1
    assert events[0]["metadata"]["outcome"] == "generation_error"
    assert events[0]["metadata"]["reason"] == "invalid_api_key"


def test_unexpected_exception_also_swallowed(fake_langfuse: _FakeLangfuse) -> None:
    chat = _StubChat(error=RuntimeError("boom"))

    result = LlmGenerateFilter(subgraph=_subgraph(chat)).process(_envelope())

    assert result.generated_reply is None
    events = _generate_events(fake_langfuse)
    assert events[0]["metadata"]["reason"] == "RuntimeError"


# reason 期望与 tests/test_llm_grounding.py 的参数化口径逐项一致。
@pytest.mark.parametrize(
    ("bad_draft", "expected_reason"),
    [
        ("我是这套房的经纪人，随时带您看房。", "forbidden_expression:我是这套房的经纪人"),
        ("这套房性价比第一，错过不再有。", "forbidden_expression:第一"),
        ("【事实卡片】房源编号 house-001。", "section_marker_leak:【事实卡片】"),
        ("详情见 https://example.com/house/1", "forbidden_link"),
        ("详情见 example.com/path", "forbidden_link"),
        ("我是这套房子的经纪人，随时看房。", "forbidden_expression:self_claim_regex"),
        ("x" * 1001, "overlong_output"),
    ],
)
def test_grounding_failure_drops_draft_and_traces(
    fake_langfuse: _FakeLangfuse, bad_draft: str, expected_reason: str
) -> None:
    chat = _StubChat(text=bad_draft)

    result = LlmGenerateFilter(subgraph=_subgraph(chat)).process(_envelope())

    assert result.generated_reply is None
    events = _generate_events(fake_langfuse)
    assert len(events) == 1
    assert events[0]["metadata"]["outcome"] == "grounding_failed"
    assert events[0]["metadata"]["reason"] == expected_reason


def test_module_entry_failure_is_fail_safe(
    monkeypatch: pytest.MonkeyPatch, fake_langfuse: _FakeLangfuse
) -> None:
    """AC 第 4 条：OpenAI 不可用（凭据缺失）时无草稿、透传、不抛。"""

    def _boom(
        resolved_message: str,
        *,
        fact_card: FactCard | None = None,
        intent: IntentTag | None = None,
        trace_id: str | None = None,
    ) -> str:
        raise LlmGenerationError("invalid_api_key", "no key")

    monkeypatch.setattr(filter_module, "_module_generate", _boom)
    envelope = _envelope(fact_card=_fact_card())

    result = LlmGenerateFilter().process(envelope)  # 未注入子图：模块级入口

    assert result is envelope
    assert result.generated_reply is None
    assert len(_generate_events(fake_langfuse)) == 1


def test_full_pipeline_approved_draft_reaches_buyer_with_ai_prefix() -> None:
    """4.4 AC 第 1 条：草稿过审后买家可见 reply = AI 前缀+草稿（degraded=False）。

    4.3 影子口径（买家应答 100% 模板兜底）随 4.4 最终组装点落地而作废：
    编排器最终返回点消费已过审草稿。
    """
    seen: list[Envelope] = []

    class _Recorder:
        name = "recorder"

        def process(self, envelope: Envelope) -> Envelope:
            seen.append(envelope)
            return envelope

    orchestrator = PipelineOrchestrator(
        filters=[
            LlmGenerateFilter(subgraph=_subgraph(_StubChat())),
            RiskReviewFilter(client=_AlwaysPassClient()),
            cast(Any, _Recorder()),
            ResponseAssemblerFilter(),
        ]
    )
    envelope = _envelope(fact_card=_fact_card())

    result = orchestrator.run(envelope)

    assert result.degraded is False
    assert result.failed_filter is None
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{DRAFT}"
    assert seen[0].generated_reply == DRAFT  # 过审草稿经信封到达最终组装点


class _SlowChat:
    """慢生成替身：invoke 休眠后返回（调用侧预算守卫须先行返回）。"""

    def __init__(self, delay_seconds: float) -> None:
        self.calls = 0
        self._delay_seconds = delay_seconds

    def invoke(self, messages: Any) -> Any:
        self.calls += 1
        time.sleep(self._delay_seconds)

        class _Response:
            content = DRAFT
            usage_metadata: dict[str, int] = {}

        return _Response()


def test_slow_generation_times_out_inside_filter_budget(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """慢调用在过滤器内超时归一：预算内返回、信封不变、不触发编排器降级。"""
    chat = _SlowChat(delay_seconds=2.0)  # 远超 LLM_GENERATE_BUDGET_SECONDS
    subgraph = LlmGenerationSubgraph(
        OpenAiGenerationClient(
            chat_model=chat,
            budget_seconds=LLM_GENERATE_BUDGET_SECONDS,
        )
    )
    envelope = _envelope(fact_card=_fact_card())

    started = time.monotonic()
    result = LlmGenerateFilter(subgraph=subgraph).process(envelope)
    elapsed = time.monotonic() - started

    # 调用侧在子图预算内返回（不傻等执行线程），信封原样、无草稿。
    assert elapsed < LLM_GENERATE_BUDGET_SECONDS + 0.6
    assert result is envelope
    assert result.generated_reply is None
    # 失败仅以可观测事件留痕（reason=timeout），不向上抛、不降级。
    events = _generate_events(fake_langfuse)
    assert len(events) == 1
    assert events[0]["metadata"]["outcome"] == "generation_error"
    assert events[0]["metadata"]["reason"] == "timeout"


class _FakeChatOpenAI:
    """ChatOpenAI 类替身：默认五过滤器链整链走查的模型拦截点。"""

    calls = 0
    error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def invoke(self, messages: Any) -> Any:
        type(self).calls += 1
        if type(self).error is not None:
            raise type(self).error

        class _Response:
            content = DRAFT
            usage_metadata: dict[str, int] = {}

        return _Response()


def test_default_chain_success_path_generates_but_replies_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认五过滤器链：模型被调用、草稿进信封链路，买家应答仍模板兜底。"""
    _FakeChatOpenAI.calls = 0
    _FakeChatOpenAI.error = None
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChatOpenAI)
    orchestrator = PipelineOrchestrator()

    result = orchestrator.run(_envelope(intent="PRICE_INQUIRY"))

    assert _FakeChatOpenAI.calls == 1  # 确实调了模型
    assert result.degraded is False
    assert result.failed_filter is None
    assert result.reply == render_fallback("PRICE_INQUIRY")  # 模板兜底


def test_default_chain_generation_error_keeps_buyer_reply_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认五过滤器链：模型抛错 → 吞掉、degraded=False、模板兜底不变。"""
    _FakeChatOpenAI.calls = 0
    _FakeChatOpenAI.error = RuntimeError("boom")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChatOpenAI)
    orchestrator = PipelineOrchestrator()

    result = orchestrator.run(_envelope(intent="PRICE_INQUIRY"))

    assert _FakeChatOpenAI.calls == 1
    assert result.degraded is False  # 影子模式：生成故障不降级
    assert result.failed_filter is None
    assert result.reply == render_fallback("PRICE_INQUIRY")


# ---- Story 4.5：熔断器注入矩阵（open 跳过零子图调用 / 跳闸 / 恢复）----


def test_open_breaker_skips_generation_without_subgraph_call(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """open 期间请求：不调子图（<100ms 切换前提=零子图调用）、信封透传。"""
    chat = _StubChat()
    breaker = LlmCircuitBreaker(cooldown_seconds=10)
    breaker.on_failure("rate_limit")  # 预置 open 态
    envelope = _envelope(fact_card=_fact_card())

    result = LlmGenerateFilter(subgraph=_subgraph(chat), breaker=breaker).process(envelope)

    assert chat.calls == 0  # 零子图调用（纯内存路径）
    assert result is envelope  # 信封原样透传
    assert result.generated_reply is None
    events = _breaker_events(fake_langfuse)
    assert len(events) == 1
    assert events[0]["level"] == "DEFAULT"  # skip 事件 DEFAULT 级（防噪音洪流）
    assert events[0]["metadata"]["outcome"] == "skip"


def test_open_breaker_preserves_redline_and_non_text_early_return(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """红线/非文本早退在熔断咨询之前：open 态也不产生 skip 事件（不污染统计）。"""
    chat = _StubChat()
    breaker = LlmCircuitBreaker(cooldown_seconds=10)
    breaker.on_failure("rate_limit")
    filter_ = LlmGenerateFilter(subgraph=_subgraph(chat), breaker=breaker)

    redline = filter_.process(_envelope(intent="NEGOTIATION"))
    media = filter_.process(_envelope(media_type="image"))

    assert chat.calls == 0
    assert redline.generated_reply is None
    assert media.generated_reply is None
    assert _breaker_events(fake_langfuse) == []


def test_transient_failure_trips_and_followups_skip_subgraph(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """AC 第 1 条：首条故障请求吞错跳闸；后续请求零子图调用回模板兜底。"""
    chat = _StubChat(error=LlmGenerationError("rate_limit", "429"))
    breaker = LlmCircuitBreaker(cooldown_seconds=10)
    filter_ = LlmGenerateFilter(subgraph=_subgraph(chat), breaker=breaker)
    first = _envelope(fact_card=_fact_card())
    second = _envelope(fact_card=_fact_card())

    result_first = filter_.process(first)
    result_second = filter_.process(second)

    assert result_first.generated_reply is None  # 既有吞错路径不变
    assert result_second.generated_reply is None
    assert chat.calls == 1  # 第二条被熔断跳过：不再触达子图
    events = _breaker_events(fake_langfuse)
    tripped = [e for e in events if e["metadata"]["outcome"] == "tripped"]
    skipped = [e for e in events if e["metadata"]["outcome"] == "skip"]
    assert len(tripped) == 1
    assert tripped[0]["level"] == "ERROR"  # 跳闸事件 ERROR 级
    assert tripped[0]["metadata"]["reason"] == "rate_limit"
    assert len(skipped) == 1
    assert _generate_events(fake_langfuse)[0]["metadata"]["reason"] == "rate_limit"


def test_deterministic_failure_does_not_trip_breaker(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """AC 第 3 条：凭据/400 系失败不跳闸，后续请求照常触达子图。"""
    chat = _StubChat(error=LlmGenerationError("invalid_api_key", "no key"))
    breaker = LlmCircuitBreaker(cooldown_seconds=10)
    filter_ = LlmGenerateFilter(subgraph=_subgraph(chat), breaker=breaker)

    filter_.process(_envelope(fact_card=_fact_card()))
    filter_.process(_envelope(fact_card=_fact_card()))

    assert chat.calls == 2  # 未跳闸：每次请求仍调子图
    assert breaker.state == "closed"
    assert _breaker_events(fake_langfuse) == []


def test_grounding_failure_does_not_trip_breaker(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """grounding 未过属内容问题（非 OpenAI 故障）：成功信号中性，不跳闸。"""
    chat = _StubChat(text="我是这套房的经纪人，随时带您看房。")
    breaker = LlmCircuitBreaker(cooldown_seconds=10)

    result = LlmGenerateFilter(subgraph=_subgraph(chat), breaker=breaker).process(
        _envelope(fact_card=_fact_card())
    )

    assert result.generated_reply is None  # 既有丢草稿路径不变
    assert breaker.state == "closed"
    assert _breaker_events(fake_langfuse) == []


def test_breaker_stepwise_recovery_via_filter(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """AC 第 2 条全链路：跳闸 → 冷却期跳过 → 步进试探成功递增至闭合。"""
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, recovery_step=0.1, clock=clock)
    filter_ = LlmGenerateFilter(
        subgraph=_subgraph(_StubChat()), breaker=breaker
    )

    # 故障跳闸（不注入故障替身——直接信号，保持本走查聚焦恢复段）。
    breaker.on_failure("server_500")
    skipped = filter_.process(_envelope())
    assert skipped.generated_reply is None
    assert breaker.state == "open"

    # 冷却满：首个试探放行并成功，放行比例按步进递增直至 100% 闭合。
    clock.advance(10)
    for _ in range(10):
        result = filter_.process(_envelope())
        assert result.generated_reply == DRAFT  # 试探请求照常生成

    assert breaker.state == "closed"
    outcomes = [e["metadata"]["outcome"] for e in _breaker_events(fake_langfuse)]
    assert outcomes.count("skip") == 1
    assert outcomes.count("recovered") == 1  # 闭合瞬间记 recovered 事件
    recovered = [
        e for e in _breaker_events(fake_langfuse) if e["metadata"]["outcome"] == "recovered"
    ]
    assert recovered[0]["level"] == "DEFAULT"

    # 闭合后全量放行，无新事件。
    assert filter_.process(_envelope()).generated_reply == DRAFT
    assert len(_breaker_events(fake_langfuse)) == 2


def test_probe_failure_via_filter_reopens_breaker(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """AC 第 2 条：试探失败立即回 open，冷却重启，后续请求重新跳过。"""
    clock = _FakeClock()
    breaker = LlmCircuitBreaker(cooldown_seconds=10, recovery_step=0.1, clock=clock)
    breaker.on_failure("rate_limit")
    clock.advance(10)

    failing = _StubChat(error=LlmGenerationError("timeout", "budget exhausted"))
    filter_ = LlmGenerateFilter(subgraph=_subgraph(failing), breaker=breaker)

    probe = filter_.process(_envelope())  # 首个试探被放行，失败回 open
    assert probe.generated_reply is None
    assert breaker.state == "open"
    after = filter_.process(_envelope())  # 冷却重启：重新跳过
    assert after.generated_reply is None
    assert failing.calls == 1
    outcomes = [e["metadata"]["outcome"] for e in _breaker_events(fake_langfuse)]
    assert outcomes == ["tripped", "skip"]  # 试探失败 tripped + 冷却重启后 skip


class _FlakyBreaker:
    """方法抛错的熔断器替身：验证过滤器咨询/信号路径自身 fail-safe。"""

    def __init__(self, *, fail_before: bool = False, fail_signal: bool = False) -> None:
        self.before_calls = 0
        self.failure_calls = 0
        self.success_calls = 0
        self._fail_before = fail_before
        self._fail_signal = fail_signal

    def before_request(self) -> bool:
        self.before_calls += 1
        if self._fail_before:
            msg = "breaker consult boom"
            raise RuntimeError(msg)
        return True

    def on_failure(self, reason: str) -> bool:
        self.failure_calls += 1
        if self._fail_signal:
            msg = "breaker signal boom"
            raise RuntimeError(msg)
        return False

    def on_success(self) -> bool:
        self.success_calls += 1
        if self._fail_signal:
            msg = "breaker signal boom"
            raise RuntimeError(msg)
        return False


def test_breaker_consult_error_falls_open_to_generation() -> None:
    """咨询抛错按放行处理：过滤器照常调子图、草稿正常写入、不记 skip。"""
    chat = _StubChat()
    breaker = _FlakyBreaker(fail_before=True)

    result = LlmGenerateFilter(
        subgraph=_subgraph(chat), breaker=cast(Any, breaker)
    ).process(_envelope(fact_card=_fact_card()))

    assert breaker.before_calls == 1
    assert chat.calls == 1  # 放行：照常触达子图
    assert result.generated_reply == DRAFT


def test_breaker_signal_error_is_swallowed_on_failure_path(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """on_failure 抛错：过滤器不抛、既有吞错与 trace 事件照常。"""
    chat = _StubChat(error=LlmGenerationError("rate_limit", "429"))
    breaker = _FlakyBreaker(fail_signal=True)

    result = LlmGenerateFilter(
        subgraph=_subgraph(chat), breaker=cast(Any, breaker)
    ).process(_envelope(fact_card=_fact_card()))

    assert breaker.failure_calls == 1
    assert result.generated_reply is None
    assert _breaker_events(fake_langfuse) == []  # tripped 事件未产生（未跳闸）
    events = _generate_events(fake_langfuse)
    assert events[0]["metadata"]["outcome"] == "generation_error"  # 照常


def test_breaker_signal_error_is_swallowed_on_success_path() -> None:
    """on_success 抛错：过滤器不抛、草稿照常写入。"""
    chat = _StubChat()
    breaker = _FlakyBreaker(fail_signal=True)

    result = LlmGenerateFilter(
        subgraph=_subgraph(chat), breaker=cast(Any, breaker)
    ).process(_envelope(fact_card=_fact_card()))

    assert breaker.success_calls == 1
    assert result.generated_reply == DRAFT
