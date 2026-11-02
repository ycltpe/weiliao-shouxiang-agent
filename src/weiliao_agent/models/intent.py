"""意图标签与分类结果模型（Story 2.1）—— 数据格式约定的唯一权威定义。

9 类意图标签为单一 ``Literal`` 类型枚举（架构一致性约定「数据格式」），
全链路经 ``model_validate`` 校验：未知值校验拒绝即 fail-closed，由端点层
既有 ``ValidationError`` 兜底承载为模板兜底应答。

``IntentClassification`` 为分类器输出的统一口径：单一标签 + 置信度
（AD-3 单意图路由；置信度不进信封——六字段契约不变）。
"""

from typing import Literal

from pydantic import BaseModel, Field

# 9 类意图固定枚举（顺序按 Epic 2 AC）：全链路唯一权威定义。
IntentTag = Literal[
    "GREETING",
    "PRICE_INQUIRY",
    "AVAILABILITY",
    "LOCATION",
    "PHOTO_REQUEST",
    "NEGOTIATION",
    "LOAN_POLICY",
    "COMPLAINT",
    "UNKNOWN",
]


class IntentClassification(BaseModel):
    """单条意图分类结果：单一标签 + 置信度（0.0–1.0，含边界）。"""

    tag: IntentTag
    confidence: float = Field(ge=0.0, le=1.0)
