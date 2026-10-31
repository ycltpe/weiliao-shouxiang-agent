"""消息信封 —— 管道-过滤器架构全链路唯一通信模型。

信封随管道逐跳填充：每个过滤器仅消费上游信封、产出下游信封。
任意来源的 dict 必须经 ``Envelope.model_validate`` 校验后方可进入管道；
缺失必填字段、类型错误一律 fail-closed（抛 ``pydantic.ValidationError``），
由调用方兜底。

字段口径（架构一致性约定）：
- ``session_id`` / ``house_id`` / ``timestamp`` / ``trace_id`` 必填；
- ``turn``（会话内绝对消息序号，由会话管理在入管道前赋值）与
  ``intent``（由分类器填写）允许为 None，但非 None 时类型必须正确；
- ``intent`` 本 Story 为 ``str | None``，9 类 Literal 枚举在 Story 2.1
  定义后收窄，未知值届时 fail-closed 到模板兜底；
- ``message`` / ``resolved_message`` / ``coref`` / ``media_type`` 为架构
  预留字段，本 Story 仅定义、不消费，默认 None。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Envelope(BaseModel):
    """管道内传递的标准化消息信封。"""

    session_id: str = Field(min_length=1)
    house_id: str = Field(min_length=1)
    timestamp: datetime
    trace_id: str = Field(min_length=1)
    turn: int | None = None
    intent: str | None = None
    # 以下四字段为架构预留，本 Story 不参与验收；coref 将在 CorefRef
    # 模型（models/，AD-11）定义后收窄为 list[CorefRef]。
    message: str | None = None
    resolved_message: str | None = None
    coref: list[Any] | None = None
    media_type: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _timestamp_must_be_tz_aware(cls, value: datetime) -> datetime:
        """结算 deferred-work：时间戳必须带时区（naive datetime 拒绝）。"""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            msg = "timestamp must be timezone-aware"
            raise ValueError(msg)
        return value
