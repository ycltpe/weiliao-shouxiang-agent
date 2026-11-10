"""指代消解引用模型（Story 6.1）—— 数据格式约定的唯一权威定义。

五类实体类型为单一 ``Literal`` 类型枚举（架构一致性约定「数据格式」），
全链路经 ``model_validate`` 校验：未知值校验拒绝即 fail-closed。

真实消解算法归 Story 6.2 / OQ-5 选型后落地；本模型只锁定引用元数据
的接口边界——实现可插拔替换，信封契约与本模型不动。
"""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

# 五类实体固定枚举（架构一致性约定）：全链路唯一权威定义。
CorefEntityType = Literal["house", "price", "floor", "location", "other"]


class CorefRef(BaseModel):
    """单条指代引用：实体类型 + 可选实体锚点。

    ``entity_id`` 为房源库实体锚点：house 类型必填（未知房源不得虚构
    锚点），其余类型可缺省（价格 / 楼层 / 位置等可为泛指表达）；
    非 None 时拒绝空串（min_length=1）。
    """

    entity_type: CorefEntityType
    entity_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _house_requires_entity_id(self) -> Self:
        """house 引用必填实体锚点：缺省或空串一律校验拒绝。"""
        if self.entity_type == "house" and not self.entity_id:
            msg = "entity_id is required for house references"
            raise ValueError(msg)
        return self
