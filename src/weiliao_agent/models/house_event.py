"""Kafka ``house.sync.trigger`` 事件契约（Story 3.2，AD-6 唯一写入路径）。

事实卡片的唯一写入来源：上游以**全量快照** payload 直接携带事实全部
字段，消费端薄转换（``to_fact_card``）为 ``FactCard`` 经 L0 客户端写入，
消费端无 DB 依赖。字段口径：

- ``event_id`` / ``event_type`` / ``house_id`` / ``fact_version`` /
  ``occurred_at`` 为所有事件的必填公共字段；
- ``event_type`` 为两态 Literal（``house_upsert`` / ``house_invalidate``），
  未知值经 ``model_validate`` 拒绝即 fail-closed；
- ``occurred_at`` 为事件发生时间戳，须带时区（与信封 ``timestamp`` /
  ``FactCard.as_of`` 同款 tz-aware 校验约定）；消费延迟预算 500ms 自
  本字段起算（超期转失效 + 告警，见 workers/house_sync.py）；
- upsert 专属字段：``status``（复用 ``HouseStatus`` 三态）、
  ``price_yuan``（挂牌价元，非负；入 L0 前经 ``price_to_bucket`` 转桶）、
  ``school`` 可选、``dialogue_summary`` 缺省空列表上限 3 条——upsert
  缺 ``status`` / ``price_yuan`` 一律拒绝（全量快照契约，防字段漂移）；
- invalidate 仅需公共字段：携带任何 upsert 专属字段即拒绝（契约严格
  形状，拒漂移）。

重放幂等不在本模型职责：乱序/重复事件由 L0 三 Lua 守卫
（fact_version 墓碑/索引/乱序比较）裁决，本契约只保证形状合法。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from weiliao_agent.models.fact_card import (
    MAX_DIALOGUE_SUMMARY_ITEMS,
    FactCard,
    HouseStatus,
    price_to_bucket,
)

# 事件类型两态枚举（Story 3.2 契约唯一权威定义）。
EventType = Literal["house_upsert", "house_invalidate"]


class HouseSyncEvent(BaseModel):
    """``house.sync.trigger`` 全量快照事件：事实卡片的唯一写入契约。"""

    event_id: str = Field(min_length=1)
    event_type: EventType
    house_id: str = Field(min_length=1)
    fact_version: int
    occurred_at: datetime
    # 以下四字段为 upsert 专属（全量快照）；invalidate 事件须全部缺省。
    status: HouseStatus | None = None
    price_yuan: int | None = Field(default=None, ge=0)
    school: str | None = None
    dialogue_summary: list[str] = Field(
        default_factory=list, max_length=MAX_DIALOGUE_SUMMARY_ITEMS
    )

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        """时间戳必须带时区（与信封 timestamp 同款约定，延迟口径前提）。"""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            msg = "occurred_at must be timezone-aware"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _shape_matches_event_type(self) -> "HouseSyncEvent":
        """upsert 必须携带全量事实字段；invalidate 不得携带专属字段。"""
        if self.event_type == "house_upsert":
            if self.status is None or self.price_yuan is None:
                msg = "house_upsert requires status and price_yuan (full snapshot)"
                raise ValueError(msg)
        elif (
            self.status is not None
            or self.price_yuan is not None
            or self.school is not None
            or self.dialogue_summary
        ):
            msg = "house_invalidate must not carry upsert-only fields"
            raise ValueError(msg)
        return self

    def to_fact_card(self) -> FactCard:
        """薄转换：upsert 事件 → 事实卡片（price 转桶、as_of=occurred_at）。

        前置条件：仅 ``house_upsert`` 可调（契约校验已保证专属字段齐备），
        非 upsert 调用属编程错误，fail-fast 抛 ``ValueError``。
        """
        if self.event_type != "house_upsert" or self.status is None or self.price_yuan is None:
            msg = "to_fact_card is only valid for house_upsert events"
            raise ValueError(msg)
        return FactCard(
            house_id=self.house_id,
            status=self.status,
            price_bucket=price_to_bucket(self.price_yuan),
            school=self.school,
            dialogue_summary=list(self.dialogue_summary),
            fact_version=self.fact_version,
            as_of=self.occurred_at,
        )
