"""转人工（Epic 5）：硬触发评估与出口分支。

Story 5.1：``trigger`` 模块为 AD-12 单一评估点——四类高风险信号
（红线意图 / 非文本媒体 / 追问达阈值 / 负面词）集中判定，命中任一
即由编排器短路下游并回固定转人工文案。
"""

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
    "HardTriggerAssessor",
    "load_negative_words",
]
