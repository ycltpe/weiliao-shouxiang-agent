"""受控生成过滤器单测（Story 4.3，影子接入 I/O 矩阵全分支）。

替身子图（真实 ``LlmGenerationSubgraph`` + 替身 chat，不触达 OpenAI）
覆盖：卡片就绪正常生成、L0 miss 照常生成、resolved_message 回落
message、红线意图/非文本/空消息跳过、生成异常吞掉记 trace、grounding
校验未过丢草稿记 trace；另有完整管道走查（草稿挂信封、买家可见应答
仍是模板兜底、degraded=False）与 OpenAI 不可用（模块级入口异常）
fail-safe 走查。全部无网络。
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


def test_resolved_message_takes_priority_and_falls_back_to_message() -> None:
    chat = _StubChat()
    subgraph = _subgraph(chat)

    # 优先级：resolved_message 非空白时直接消费，不看 message。
    resolved = _envelope(
        resolved_message="消解后的自包含消息",
        message="原始的含指代消息",
    )
    LlmGenerateFilter(subgraph=subgraph).process(resolved)
    assert chat.calls == 1
    assert "消解后的自包含消息" in str(chat.seen_messages[-1][-1].content)
    assert "原始的含指代消息" not in str(chat.seen_messages[-1][-1].content)

    # 回落：resolved_message 为 None 或空白串时回落 message。
    fallback = _envelope(resolved_message=None)
    LlmGenerateFilter(subgraph=subgraph).process(fallback)
    assert chat.calls == 2
    assert "这套房子现在什么价位？" in str(chat.seen_messages[-1][-1].content)

    blank_resolved = _envelope(
        resolved_message="   ",
        message="空白消解结果回落到这条",
    )
    LlmGenerateFilter(subgraph=subgraph).process(blank_resolved)
    assert chat.calls == 3
    assert "空白消解结果回落到这条" in str(chat.seen_messages[-1][-1].content)


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
