"""消息信封 —— 管道-过滤器架构全链路唯一通信模型。

信封随管道逐跳填充：每个过滤器仅消费上游信封、产出下游信封。
任意来源的 dict 必须经 ``Envelope.model_validate`` 校验后方可进入管道；
缺失必填字段、类型错误一律 fail-closed（抛 ``pydantic.ValidationError``），
由调用方兜底。

字段口径（架构一致性约定）：
- ``session_id`` / ``house_id`` / ``timestamp`` 必填；
- ``trace_id`` 缺省为 None：由服务在编排器入口生成并回填信封
  （Story 1.5，32 位小写十六进制，即 Langfuse trace id 口径）。
  回填语义为「缺失即生成、携带值透传」（既有测试锁定透传）：
  payload 携带值原样流经管道与响应，但仅 32-hex 值可作为 Langfuse
  trace id 被消费，非 32-hex 值跳过追踪（见 observability.tracing）；
  空串仍拒绝；
- ``turn``（会话内绝对消息序号，由会话管理在入管道前赋值）允许为 None，
  但非 None 时类型必须正确；
- ``intent`` 已收窄为 ``IntentTag | None``（Story 2.1，9 类 Literal 枚举，
  权威定义在 models.intent，由分类器填写）：未知值经 ``model_validate``
  拒绝即 fail-closed 到模板兜底；
- ``is_followup``（Story 2.3，重复追问标记）：同一 session 内同一意图第
  2 次起为 True，由 intent_router 经注入的会话单写者回调判定后写回；
  默认 False（含分类 fail-closed / 记录失败 fail-safe 路径）。Epic 3 的
  cache_lookup 据此禁走 L0 缓存；
- ``message`` / ``resolved_message`` / ``coref`` / ``media_type`` 为架构
  预留字段，本 Story 仅定义、不消费，默认 None。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from weiliao_agent.models.intent import IntentTag


class Envelope(BaseModel):
    """管道内传递的标准化消息信封。"""

    session_id: str = Field(min_length=1)
    house_id: str = Field(min_length=1)
    timestamp: datetime
    # Story 1.5：trace_id 放宽为可选——服务端权威生成（编排器入口
    # 回填），其余校验规则不动（空串仍 min_length=1 拒绝）。
    trace_id: str | None = Field(default=None, min_length=1)
    turn: int | None = None
    intent: IntentTag | None = None
    # Story 2.3：Follow-up Query 标记（同意图第 2 次起为 True，Epic 3
    # cache_lookup 消费）；默认 False——分类 fail-closed 与追问记录失败
    # fail-safe 路径均不置位。
    is_followup: bool = False
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
