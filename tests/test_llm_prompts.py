"""受控生成 prompt 模板单测（Story 4.2，AC 第 3 条）。

覆盖：固定三段（事实卡片/对话上下文/生成约束）恒在；fact_card=None 走
「暂无事实」口径且不虚构房源信息；卡片字段按原值渲染；输入仅消费
resolved_message + fact_card + intent。
"""

from datetime import UTC, datetime
from typing import cast

from weiliao_agent.llm.prompts import (
    CONSTRAINT_SECTION_HEADER,
    DIALOGUE_SECTION_HEADER,
    FACT_SECTION_HEADER,
    NO_FACTS_COPY,
    build_generation_prompt,
)
from weiliao_agent.models.fact_card import FactCard

RESOLVED_MESSAGE = "这套房子现在什么价位？"


def _fact_card(**overrides: object) -> FactCard:
    values: dict[str, object] = {
        "house_id": "house-001",
        "status": "active",
        "price_bucket": 12,
        "fact_version": 3,
        "as_of": datetime(2026, 11, 6, 10, 0, tzinfo=UTC),
        "dialogue_summary": ["买家上次问过朝向", "已回复南向"],
    }
    values.update(overrides)
    return FactCard.model_validate(values)


def _rendered_text(*, fact_card: FactCard | None, intent: str | None) -> str:
    from weiliao_agent.models.intent import IntentTag

    messages = build_generation_prompt(
        resolved_message=RESOLVED_MESSAGE,
        fact_card=fact_card,
        intent=cast(IntentTag, intent),
    )
    assert len(messages) == 2
    return "\n".join(str(m.content) for m in messages)


def test_prompt_always_contains_three_fixed_sections() -> None:
    text = _rendered_text(fact_card=_fact_card(), intent="PRICE_INQUIRY")

    assert FACT_SECTION_HEADER in text
    assert DIALOGUE_SECTION_HEADER in text
    assert CONSTRAINT_SECTION_HEADER in text


def test_fact_card_fields_rendered_verbatim() -> None:
    text = _rendered_text(fact_card=_fact_card(), intent="PRICE_INQUIRY")

    assert "house-001" in text
    assert "active" in text
    assert "价格分桶（10 万元/档）：12" in text
    assert "对口学校：实验小学" not in text  # 未提供 school 不得渲染
    assert RESOLVED_MESSAGE in text
    assert "价格询问" in text


def test_optional_school_rendered_only_when_present() -> None:
    text = _rendered_text(fact_card=_fact_card(school="实验小学"), intent="LOCATION")

    assert "对口学校：实验小学" in text


def test_dialogue_summary_rendered_as_numbered_context() -> None:
    text = _rendered_text(fact_card=_fact_card(), intent="PRICE_INQUIRY")

    assert "买家上次问过朝向" in text
    assert "已回复南向" in text


def test_empty_dialogue_summary_states_no_context() -> None:
    text = _rendered_text(fact_card=_fact_card(dialogue_summary=[]), intent="GREETING")

    assert "（暂无对话上下文）" in text


def test_missing_fact_card_uses_no_facts_copy_without_fabrication() -> None:
    text = _rendered_text(fact_card=None, intent="PRICE_INQUIRY")

    assert NO_FACTS_COPY in text
    assert FACT_SECTION_HEADER in text  # 三段结构不变
    assert "（暂无对话上下文）" in text
    # 不虚构口径：事实段不得出现任何房源字段值。
    assert "房源编号：" not in text
    assert "价格分桶" not in text


def test_generation_constraints_carry_compliance_rules() -> None:
    text = _rendered_text(fact_card=None, intent=None)

    assert "不得虚构价格" in text
    assert "学区名额" in text
    # Story 4.3 强化：违禁词与自称经纪人禁令须明示在生成约束段。
    assert "价格可谈" in text
    assert "「最」「第一」" in text
    assert "我是这套房的经纪人" in text
    assert "AI 助手" in text
