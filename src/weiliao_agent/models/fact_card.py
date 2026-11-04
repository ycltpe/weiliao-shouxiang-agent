"""事实卡片 —— L0 缓存的唯一存储单元（Story 3.1，Epic 3 地基）。

事实卡片为单一 Pydantic 模型（架构一致性约定「事实卡片 schema」）：
房源事实的结构化、可失效缓存单元。字段口径：

- ``house_id`` / ``status``（active/sold/offline 三态 Literal）必填；
- ``price_bucket`` 为 10 万分桶（非负 int），入 L0 key 前由
  ``price_to_bucket`` 计算（向下取整，如 1,250,000 → 12）；
- ``school`` 可选（对口学校）；
- ``dialogue_summary`` 为最近 3 轮对话摘要，上限 3 条（架构钉死）；
- ``fact_version`` 为 int，随 Kafka 房源事件单调递增，是 L0 缓存
  乐观失效的唯一依据（AD-6/AD-5：逻辑作废 + 版本比较，无物理删除）；
- ``as_of`` 为事实生成时间戳，须带时区（与信封 ``timestamp`` 同款
  tz-aware 校验约定；naive datetime 拒绝）。

全链路经 ``model_validate`` 校验：未知/损坏数据一律拒绝，由缓存客户端
归并为缓存未命中（fail-safe 到 LLM 链路）。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 房源状态三态枚举（架构一致性约定「事实卡片 schema」）：全链路唯一
# 权威定义；未知值经 ``model_validate`` 拒绝即 fail-closed。
HouseStatus = Literal["active", "sold", "offline"]

# 对话摘要条数上限（架构钉死：最近 3 轮）。
MAX_DIALOGUE_SUMMARY_ITEMS = 3


class FactCard(BaseModel):
    """L0 缓存事实卡片：房源事实的唯一 schema（AD-5）。"""

    house_id: str = Field(min_length=1)
    status: HouseStatus
    price_bucket: int = Field(ge=0)
    school: str | None = None
    dialogue_summary: list[str] = Field(
        default_factory=list, max_length=MAX_DIALOGUE_SUMMARY_ITEMS
    )
    fact_version: int
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def _as_of_must_be_tz_aware(cls, value: datetime) -> datetime:
        """时间戳必须带时区（与信封 timestamp 同款约定）。"""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            msg = "as_of must be timezone-aware"
            raise ValueError(msg)
        return value


def price_to_bucket(price_yuan: int) -> int:
    """挂牌价（元）→ 10 万分桶：向下取整（1,250,000 → 12）。

    前置条件：入参须为非负价格（元）；负数不属本函数职责——负值结果
    会被 ``FactCard.price_bucket`` 的 ``ge=0`` 校验拒绝（fail-fast）。
    """
    return price_yuan // 100_000
