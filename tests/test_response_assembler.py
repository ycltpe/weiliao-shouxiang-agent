"""响应组装过滤器单测（Story 5.4，AD-10；Story 4.7 空草稿拦截扩）。

覆盖 spec I/O & Edge-Case Matrix 组装器侧场景：
草稿过审加前缀 / 无草稿模板兜底 / 草稿已带前缀防双标 / 兜底产出一律
原样透传（内部已带前缀，不二次加注）/ 产出挂 envelope.assembled_reply
（权威挂点，编排器只读消费）/ 输入信封不被改写（4.4 冻结语义）。
空草稿（空串/纯空白）走完整模板兜底并记 draft_empty 事件（stage=
response_assembler）；None 草稿是常态无草稿，不算拦截、不记事件。
"""

from typing import Any

from conftest import draft_empty_events

from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.models.envelope import Envelope


def _envelope(**overrides: object) -> Envelope:
    payload: dict[str, object] = {
        "session_id": "sess-001",
        "house_id": "house-001",
        "timestamp": "2026-11-09T10:00:00+08:00",
        "trace_id": "ab" * 16,
        "intent": "PRICE_INQUIRY",
    }
    payload.update(overrides)
    return Envelope.model_validate(payload)


def test_approved_draft_gets_prefix_and_hangs_on_envelope() -> None:
    # 正常链-草稿过审：产出 = 前缀+草稿，逐字节下发。
    assembler = ResponseAssemblerFilter()
    envelope = _envelope(generated_reply="这套房源目前在售。")

    assembled = assembler.process(envelope)

    assert assembled.assembled_reply == f"{AI_DISCLOSURE_PREFIX}这套房源目前在售。"
    assert assembled.assembled_reply is not None
    assert assembled.assembled_reply.startswith(AI_DISCLOSURE_PREFIX)


def test_no_draft_falls_back_to_template_with_prefix() -> None:
    # 正常链-无草稿（未生成/审核未过）：模板兜底，自带前缀。
    assembler = ResponseAssemblerFilter()
    envelope = _envelope()

    assembled = assembler.process(envelope)

    assert assembled.assembled_reply == render_fallback("PRICE_INQUIRY")


def test_no_draft_and_no_intent_uses_generic_fallback() -> None:
    assembler = ResponseAssemblerFilter()
    envelope = _envelope(intent=None)

    assembled = assembler.process(envelope)

    assert assembled.assembled_reply == render_fallback(None)


def test_draft_already_prefixed_is_not_double_prefixed() -> None:
    # 草稿已带前缀：跳过加前缀，防双标（一次性 startswith 守卫）。
    assembler = ResponseAssemblerFilter()
    envelope = _envelope(
        generated_reply=f"{AI_DISCLOSURE_PREFIX}这套房源目前在售。"
    )

    assembled = assembler.process(envelope)

    assert assembled.assembled_reply == f"{AI_DISCLOSURE_PREFIX}这套房源目前在售。"


def test_fallback_output_passes_through_without_second_prefix() -> None:
    # render_fallback 产出内部已带前缀：组装器原样透传，绝不二次加注。
    assembler = ResponseAssemblerFilter()
    envelope = _envelope()

    assembled = assembler.process(envelope)

    assert assembled.assembled_reply is not None
    assert assembled.assembled_reply.count(AI_DISCLOSURE_PREFIX) == 1


def test_input_envelope_is_not_mutated() -> None:
    # 4.4 冻结语义：组装器经 model_copy 产出新信封，输入信封不被改写。
    assembler = ResponseAssemblerFilter()
    envelope = _envelope(generated_reply="这套房源目前在售。")

    assembled = assembler.process(envelope)

    assert envelope.assembled_reply is None
    assert envelope.generated_reply == "这套房源目前在售。"
    assert assembled is not envelope


def test_empty_draft_falls_back_to_template_and_records_event(fake_langfuse: Any) -> None:
    # 空串草稿：不携标组装，走完整模板兜底，trace 记 draft_empty 事件。
    assembler = ResponseAssemblerFilter()
    envelope = _envelope(generated_reply="")

    assembled = assembler.process(envelope)

    assert assembled.assembled_reply == render_fallback("PRICE_INQUIRY")
    assert assembled.assembled_reply is not None
    assert assembled.assembled_reply.startswith(AI_DISCLOSURE_PREFIX)
    events = draft_empty_events(fake_langfuse)
    assert len(events) == 1
    assert events[0]["level"] == "DEFAULT"
    assert events[0]["metadata"]["event"] == "draft_empty_event"
    assert events[0]["metadata"]["stage"] == "response_assembler"
    assert events[0]["trace_context"]["trace_id"] == "ab" * 16


def test_whitespace_only_draft_treated_same_as_empty(fake_langfuse: Any) -> None:
    # 纯空白草稿（空格/换行/制表符任意组合）：strip 后判空，与空串同等。
    assembler = ResponseAssemblerFilter()

    for blank in (" ", "  \n\t ", "\t\n  "):
        assembled = assembler.process(_envelope(generated_reply=blank))
        assert assembled.assembled_reply == render_fallback("PRICE_INQUIRY")

    events = draft_empty_events(fake_langfuse)
    assert len(events) == 3
    assert all(e["metadata"]["stage"] == "response_assembler" for e in events)


def test_nonempty_draft_records_no_draft_empty_event(fake_langfuse: Any) -> None:
    # 非空草稿（含已携标）：行为逐字节同现状，不产生 draft_empty 事件。
    assembler = ResponseAssemblerFilter()
    assembler.process(_envelope(generated_reply="这套房源目前在售。"))
    assembler.process(_envelope(generated_reply=f"{AI_DISCLOSURE_PREFIX}在售。"))

    assert draft_empty_events(fake_langfuse) == []


def test_none_draft_records_no_draft_empty_event(fake_langfuse: Any) -> None:
    # None 是常态无草稿（未生成/红线/熔断/审核不通过）：不算拦截、不记事件。
    assembler = ResponseAssemblerFilter()
    assembler.process(_envelope())

    assert draft_empty_events(fake_langfuse) == []


def test_draft_empty_event_record_failure_does_not_break_fallback(fake_langfuse: Any) -> None:
    # fail-safe：事件记录自身再抛也吞掉，兜底照常产出。
    fake_langfuse.raise_on_event = True
    assembler = ResponseAssemblerFilter()
    envelope = _envelope(generated_reply="")

    assembled = assembler.process(envelope)

    assert assembled.assembled_reply == render_fallback("PRICE_INQUIRY")
