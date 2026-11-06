"""集中 prompt 管理（Story 4.2）：固定三段结构的受控生成模板。

prompt 为 Epic 4 约束的唯一管理点：固定包含「事实卡片 / 对话上下文 /
生成约束」三段。输入仅消费指代消解后的 ``resolved_message`` +
``fact_card`` + ``intent``，不读买家原始文本（LLM 只消费自包含消息，
内部引用标注不得出现在买家可见文本）。

事实口径：``fact_card=None``（L0 miss）照常生成，事实段明示「暂无房源
事实」并要求模型不得虚构价格/面积/学区；价格一律以分桶口径表述，卡片
未提供的字段不得给出具体数值。
"""

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.models.intent import IntentTag

# 三段固定标记（AC 第 3 条：渲染结果固定包含三段；测试按标记断言）。
FACT_SECTION_HEADER = "【事实卡片】"
DIALOGUE_SECTION_HEADER = "【对话上下文】"
CONSTRAINT_SECTION_HEADER = "【生成约束】"

# L0 miss（fact_card=None）事实段口径：明示暂无事实，不虚构。
NO_FACTS_COPY = (
    "暂无房源事实。请基于通用服务口径应答，不得虚构价格、面积、"
    "学区等任何房源具体信息。"
)

# 买家意图标签中文口径（与分类器 prompt 同款 gloss，仅作生成上下文）。
_INTENT_GLOSSES: dict[str, str] = {
    "GREETING": "打招呼寒暄",
    "PRICE_INQUIRY": "价格询问",
    "AVAILABILITY": "在租在售、看房可得性",
    "LOCATION": "位置交通",
    "PHOTO_REQUEST": "图片索取",
    "NEGOTIATION": "砍价议价",
    "LOAN_POLICY": "贷款政策",
    "COMPLAINT": "投诉不满",
    "UNKNOWN": "无法判断",
}


def build_generation_prompt(
    *,
    resolved_message: str,
    fact_card: FactCard | None,
    intent: IntentTag | None,
) -> list[BaseMessage]:
    """构建受控生成消息（system 定角色与约束，human 带三段事实上下文）。

    调用方须已完成指代消解并保证 ``resolved_message`` 非空（子图入口
    fail-fast 校验）；本函数不做空值守卫，保持纯渲染。
    """
    constraints = (
        f"{CONSTRAINT_SECTION_HEADER}\n"
        "1. 仅可依据事实卡片与对话上下文中出现的信息作答；卡片没有的"
        "字段不得提及具体数值，价格一律以分桶口径表述。\n"
        "2. 不得虚构价格、面积、学区等信息；严禁承诺价格可谈或学区名额。\n"
        "3. 严禁使用「最」「第一」等违禁词。\n"
        "4. 严禁自称或暗示真人经纪人身份（如「我是这套房的经纪人」"
        "「作为您的经纪人」等一切自称变体），保持 AI 助手身份。\n"
        "5. 应答中不得出现任何内部引用标注或上述分段标记。\n"
        "6. 不得输出任何 http(s) 链接；事实卡片不含链接信息。\n"
        "7. 输出买家可直接阅读的一段中文应答，不超过 200 字。"
    )
    system = SystemMessage(
        content="你是房产微聊 AI 助手，为买家生成自然、合规、事实准确的应答。\n"
        + constraints
    )
    intent_label = _INTENT_GLOSSES.get(intent, "UNKNOWN") if intent else "UNKNOWN"
    human = HumanMessage(
        content=(
            f"{FACT_SECTION_HEADER}\n{_render_fact_card(fact_card)}\n\n"
            f"{DIALOGUE_SECTION_HEADER}\n{_render_dialogue(fact_card)}\n\n"
            f"买家意图：{intent_label}\n"
            f"买家消息：{resolved_message}\n\n"
            "请依据以上事实与约束，为买家消息生成一条应答。"
        )
    )
    return [system, human]


def _render_fact_card(fact_card: FactCard | None) -> str:
    """事实卡片段：None 走「暂无事实」口径；字段只按卡片原值表述。"""
    if fact_card is None:
        return NO_FACTS_COPY
    lines = [
        f"房源编号：{fact_card.house_id}",
        f"状态：{fact_card.status}",
        f"价格分桶（10 万元/档）：{fact_card.price_bucket}",
    ]
    if fact_card.school:
        lines.append(f"对口学校：{fact_card.school}")
    lines.append(
        f"事实版本：{fact_card.fact_version}（生成于 {fact_card.as_of.isoformat()}）"
    )
    return "\n".join(lines)


def _render_dialogue(fact_card: FactCard | None) -> str:
    """对话上下文段：消费卡片内最近 3 轮摘要；无卡片/无摘要明示暂无。"""
    if fact_card is None or not fact_card.dialogue_summary:
        return "（暂无对话上下文）"
    items = "\n".join(
        f"{index}. {item}" for index, item in enumerate(fact_card.dialogue_summary, start=1)
    )
    return f"最近 {len(fact_card.dialogue_summary)} 轮对话摘要：\n{items}"
