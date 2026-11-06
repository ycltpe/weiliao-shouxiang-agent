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
    OpenAiGenerationClient,
)
from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.observability import tracing

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
