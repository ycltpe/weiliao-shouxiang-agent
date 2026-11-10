"""指代消解过滤器（Story 6.1：接口边界与链首透传接入，AD-11）。

链首执行（intent_router 之前，FR-11）：intent_router 已按 AD-11 读
``resolved_message`` 回落 ``message``，本过滤器就位后 ``resolved_message``
即分类器唯一输入，天然兼容、下游零改动。

接口边界三件套（可插拔替换，Story 6.2 / OQ-5 选型落地时组合根只替换
resolver，过滤器本体与信封契约不动）：
- 可注入 ``resolver``：``Envelope -> (resolved, refs)``——输入上游信封，
  输出消解后文本 + ``CorefRef`` 引用清单（模型校验权威定义在
  models.coref）；未注入时默认透传（恒等消解）：``resolved_message``
  为原文拷贝（``message`` 可能为 None）、``coref`` 为 []。空消息与
  非文本媒体同口径透传，绝不调用 resolver。
- 信封两字段：``resolved_message`` / ``coref`` 唯一写入点，经
  ``model_copy(update=...)`` 产出下游信封；信封 ``coref`` 为 None 保留
  「过滤器未执行」语义。
- 审计回调 ``coref_writer``（AD-4 单写者——组合根注入
  ``SessionManager.write_coref`` 方法引用，过滤器不持有 Redis 客户端）：
  per-turn 审计键 ``weiliao:session:{session_id}:coref:{turn}`` 写入
  JSON，仅作留痕，任何下游禁止反读 Redis 重建 coref。

Fail-safe 口径（过滤器契约，见 filters/base.py）：resolver 异常或其
产出复核失败（``model_copy(update=...)`` 不触发 pydantic 校验，
``model_construct`` 构造的非法 ``CorefRef`` 经逐个 ``model_validate``
复核拒绝）一律回落透传（resolved_message 为原文、coref 为 []，
ref_count=0、resolver_error=True），process 永不因 resolver 产出而
抛错；resolver_error 与「本就无指代」的恒等透传（ref_count=0、
resolver_error=False）区分——Story 6.2 注入真实 resolver 后运行侧
据 True 持续出现发现「消解持续失败」。审计写 try 包裹——写失败
应答不降级、不外抛、不阻塞，仅经 ``tracing.record_coref`` 事件观测
（audit_written=False、audit_error=True）；``turn`` 为 None 时跳过
审计写（同 intent_router ``_record_followup`` 先例，
audit_written=False、audit_error=False）。过滤器自身延迟为纯字段
拷贝 + 可选单次同步 Redis SET（有界阻塞，计入 FR-1 端到端预算）：
写失败「即弃」指 fail-safe 观测口径（trace 事件，绝不重试阻塞），
非异步 fire-and-forget 调用形态。
"""

import time
from collections.abc import Callable, Mapping
from typing import Any

from weiliao_agent.filters.base import Filter
from weiliao_agent.models.coref import CorefRef
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import tracing

# 消解钩子接口边界：输入上游信封，输出 (消解后文本, 引用清单)。
CorefResolver = Callable[[Envelope], tuple[str, list[CorefRef]]]
# AD-4 单写者回调口径：SessionManager.write_coref 方法引用。
CorefWriter = Callable[[str, int, Mapping[str, Any]], None]


class CoreferenceFilter(Filter):
    """指代消解：默认透传原文（恒等消解），可注入 resolver 替换实现。"""

    name = "coreference"

    def __init__(
        self,
        resolver: CorefResolver | None = None,
        coref_writer: CorefWriter | None = None,
    ) -> None:
        self._resolver = resolver
        self._coref_writer = coref_writer

    def process(self, envelope: Envelope) -> Envelope:
        started = time.monotonic()
        resolved: str | None
        refs: list[CorefRef]
        resolver_error = False
        if self._resolver is None or not (
            envelope.message and envelope.message.strip()
        ):
            # 默认透传（恒等消解）/ 空消息、纯空白与非文本媒体：原文
            # 拷贝（可能为 None）、coref 空列表；resolver 契约要求
            # 非空白文本，守卫同 intent_router 空消息先例（纯空白
            # message 为 truthy，必须 strip 后判定）。
            resolved, refs = envelope.message, []
        else:
            try:
                resolved, raw_refs = self._resolver(envelope)
                # refs 复核与 resolver 调用同置 try 内：model_copy
                # (update=...) 不触发 pydantic 校验，非一致性 resolver
                # （如以 model_construct 绕过 house 必填约束）在此拒绝；
                # 复核失败回落透传，process 永不因 resolver 产出而抛错。
                refs = [CorefRef.model_validate(ref) for ref in raw_refs]
            except Exception:
                # resolver_error：回落归因观测——仅 resolver 调用/复核
                # 抛异常时为 True，与恒等透传（本就无指代）区分。
                resolver_error = True
                # resolver 异常回落透传：买家应答零影响（下游按原文
                # 消费），仅 trace 侧经 record_coref 事件可观测
                # （ref_count=0 + resolver_error=True）。
                resolved, refs = envelope.message, []
        # 计时含同步审计写（有界阻塞 SET 计入过滤器自身延迟预算），
        # 故 latency_ms 在审计写完成后停止。
        audit_written, audit_error = self._write_audit(envelope, resolved, refs)
        latency_ms = (time.monotonic() - started) * 1000
        tracing.record_coref(
            envelope.trace_id,
            latency_ms=latency_ms,
            ref_count=len(refs),
            audit_written=audit_written,
            audit_error=audit_error,
            resolver_error=resolver_error,
        )
        return envelope.model_copy(
            update={"resolved_message": resolved, "coref": refs}
        )

    def _write_audit(
        self,
        envelope: Envelope,
        resolved: str | None,
        refs: list[CorefRef],
    ) -> tuple[bool, bool]:
        """审计单写者回调（AD-4）：turn 非 None 且 writer 注入时落盘。

        返回 ``(是否写入成功, 写是否出错)``——turn 为 None（同
        ``_record_followup`` 先例）或未注入 writer 时跳过（False,
        False：设计内跳过，非故障）；writer 异常 fail-safe：不外抛、
        不阻塞，返回 (False, True) 经 trace 事件观测（audit_error
        区分故障与设计内跳过）。审计键仅作留痕，下游禁止反读
        Redis 重建 coref（spec Boundaries）。
        """
        if envelope.turn is None or self._coref_writer is None:
            return False, False
        payload: Mapping[str, Any] = {
            "trace_id": envelope.trace_id,  # None 也直写：审计键缺 trace 上下文
            "resolved_message": resolved,
            "coref": [ref.model_dump() for ref in refs],
        }
        try:
            self._coref_writer(envelope.session_id, envelope.turn, payload)
        except Exception:
            return False, True
        return True, False
