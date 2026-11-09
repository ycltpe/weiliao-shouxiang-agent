"""转人工（Epic 5）：硬触发评估、单向指令发送与出口分支。

Story 5.1：``trigger`` 模块为 AD-12 单一评估点——四类高风险信号
（红线意图 / 非文本媒体 / 追问达阈值 / 负面词）集中判定，命中任一
即由编排器短路下游并回固定转人工文案。

Story 5.2：``command`` 模块为 Kafka 转交指令发送器（AD-7 先指令
后通知）——payload 携带 handoff_id（UUID4 去重键）/version=1 契约
版本、命中条件（过滤 assessment_error 哨兵）与最近 3 轮对话原文
上下文；发送 key=session_id、acks=all，重试 3 次耗尽写 DLQ topic
+ ERROR 告警。
"""

from weiliao_agent.handoff.command import (
    PAYLOAD_RECENT_TURNS,
    HandoffCommand,
    HandoffCommandSender,
    HandoffTransport,
    HandoffTurn,
)
from weiliao_agent.handoff.trigger import (
    CONDITION_ASSESSMENT_ERROR,
    CONDITION_FOLLOWUP_THRESHOLD,
    CONDITION_NON_TEXT_MEDIA,
    CONDITION_REDLINE_INTENT,
    DEFAULT_FOLLOWUP_THRESHOLD,
    DEFAULT_REDLINE_INTENTS,
    HardTriggerAssessor,
    load_negative_words,
)

__all__ = [
    "CONDITION_ASSESSMENT_ERROR",
    "CONDITION_FOLLOWUP_THRESHOLD",
    "CONDITION_NON_TEXT_MEDIA",
    "CONDITION_REDLINE_INTENT",
    "DEFAULT_FOLLOWUP_THRESHOLD",
    "DEFAULT_REDLINE_INTENTS",
    "HandoffCommand",
    "HandoffCommandSender",
    "HandoffTransport",
    "HandoffTurn",
    "HardTriggerAssessor",
    "PAYLOAD_RECENT_TURNS",
    "load_negative_words",
]
