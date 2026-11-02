"""意图路由过滤器（Story 1.2 占位行为；Story 2.1 分类器接口边界）。

占位行为（未注入分类器）：意图缺失时按 fail-closed 口径置为 UNKNOWN，
其余透传（既有测试逐字节锁定）。

Story 2.1：定义 ``IntentClassifier`` Protocol（AD-3 单意图路由——输入
消息文本 + 会话上下文，输出单一标签 + 置信度）并经构造参数注入。实现
选型（规则引擎 / LLM zero-shot / 微调模型，Story 2.2）保持任意继承
结构即可替换，路由链路代码零感知。分类异常 fail-closed 为 UNKNOWN，
过滤器自身不向上抛（过滤器契约，见 filters/base.py）。
"""

from collections.abc import Mapping
from typing import Any, Protocol

from weiliao_agent.filters.base import Filter
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentClassification


class IntentClassifier(Protocol):
    """意图分类器接口边界（AD-3 单意图路由）。

    接口仅承诺「消息文本 + 会话上下文 → 单一标签 + 置信度」；context
    具体形态由实现自定（本过滤器当前供给 session_id / house_id / turn /
    trace_id 的只读映射），实现方按组合根装配时的约定消费，切换实现
    无需改动路由链路代码（AC 第 4 条）。
    """

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        """对单条消息输出单一意图标签与置信度。"""
        ...


class IntentRouterFilter(Filter):
    """意图路由：注入分类器时写回分类标签，未注入时保留占位行为。"""

    name = "intent_router"

    def __init__(self, classifier: IntentClassifier | None = None) -> None:
        self._classifier = classifier

    def process(self, envelope: Envelope) -> Envelope:
        if self._classifier is None:
            # 占位行为（Story 1.2，既有测试锁定）：缺失意图置 UNKNOWN。
            if envelope.intent is None:
                return envelope.model_copy(update={"intent": "UNKNOWN"})
            return envelope
        # 消解后消息（resolved_message）就位后即为分类器唯一输入（AD-11）；
        # 指代消解过滤器交付前，退化为买家原文（message）。
        message = (
            envelope.resolved_message
            if envelope.resolved_message is not None
            else envelope.message
        )
        if not message:
            # 分类器契约要求非空消息文本：None 与空串一律 fail-closed
            # UNKNOWN，不调用分类器。
            return envelope.model_copy(update={"intent": "UNKNOWN"})
        context: Mapping[str, Any] = {
            "session_id": envelope.session_id,
            "house_id": envelope.house_id,
            "turn": envelope.turn,
            "trace_id": envelope.trace_id,
        }
        try:
            classification = self._classifier.classify(message, context)
            # 写回经 model_validate 全量校验（非 model_copy 旁路）：
            # 非一致性分类器（如以 model_construct 绕过校验返回枚举外
            # tag）在此拒绝；tag 提取同置 try 内，返回 None 或无 .tag
            # 对象的 AttributeError 同样降级，过滤器自身不向上抛。
            return Envelope.model_validate(
                {**envelope.model_dump(), "intent": classification.tag}
            )
        except Exception:
            # 分类异常 fail-closed：UNKNOWN，过滤器自身不向上抛。
            return envelope.model_copy(update={"intent": "UNKNOWN"})
