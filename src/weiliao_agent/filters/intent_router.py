"""意图路由过滤器（Story 1.2 占位行为；Story 2.1 分类器接口边界；Story 2.2
写回钩子与分类 trace 事件）。

占位行为（未注入分类器）：意图缺失时按 fail-closed 口径置为 UNKNOWN，
其余透传（既有测试逐字节锁定）。

Story 2.1：定义 ``IntentClassifier`` Protocol（AD-3 单意图路由——输入
消息文本 + 会话上下文，输出单一标签 + 置信度）并经构造参数注入。实现
选型（规则引擎 / LLM zero-shot / 微调模型，Story 2.2）保持任意继承
结构即可替换，路由链路代码零感知。分类异常 fail-closed 为 UNKNOWN，
过滤器自身不向上抛（过滤器契约，见 filters/base.py）。

Story 2.2：可选注入 ``intent_writer``（AD-4 单写者——组合根注入
``SessionManager.write_intent`` 方法引用，过滤器不持有 Redis 客户端）：
分类成功、``turn`` 非 None 且 writer 存在时调用，try 包裹 fail-safe——
写失败应答不降级、信封保留分类结果、trace 记
reason=intent_write_failed 失败事件。分类成功/失败各补 trace 事件
（标签/置信度/来源/延迟；失败含 reason），失败观测不区分实现来源。

Story 2.3：可选注入 ``followup_recorder``（AD-4 单写者——组合根注入
``SessionManager.record_followup`` 方法引用）：分类成功、``turn`` 非
None、recorder 存在且 tag 非 UNKNOWN 时调用，对 (session, intent) 计数
做 Redis 原子自增（Lua 单 roundtrip，过滤器不持有 Redis 客户端）；
per-intent 计数 ≥2 时信封 ``is_followup=True``（Epic 3 cache_lookup
消费），会话追问总数达 3 时经 ``record_followup_hard_trigger`` 产出
hard trigger 信号事件（判定与转人工归 Epic 5 的 AD-12 模块）。
UNKNOWN 不参与追问判定（计数与标记均跳过，降级路径零副作用；AD-11：
追问判定只以分类器输出的意图标签为准，禁止对消息文本做相似度比对）。
recorder 异常 try 包裹 fail-safe：应答不降级、信封保留分类结果、
``is_followup`` 保持默认 False、trace 记 reason=followup_record_failed
失败事件，过滤器自身不向上抛。
"""

import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from weiliao_agent.filters.base import Filter
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentClassification, IntentTag
from weiliao_agent.observability import tracing


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

    def __init__(
        self,
        classifier: IntentClassifier | None = None,
        intent_writer: Callable[[str, int, IntentClassification], None] | None = None,
        followup_recorder: Callable[[str, IntentTag], tuple[int, int]] | None = None,
    ) -> None:
        self._classifier = classifier
        self._intent_writer = intent_writer
        self._followup_recorder = followup_recorder

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
        started = time.monotonic()
        try:
            classification = self._classifier.classify(message, context)
            # 写回经 model_validate 全量校验（非 model_copy 旁路）：
            # 非一致性分类器（如以 model_construct 绕过校验返回枚举外
            # tag）在此拒绝；tag 提取同置 try 内，返回 None 或无 .tag
            # 对象的 AttributeError 同样降级，过滤器自身不向上抛。
            classified = Envelope.model_validate(
                {**envelope.model_dump(), "intent": classification.tag}
            )
            # confidence 提取同置 try 内（2.1 防御口径）：非一致性分类器
            # （有 .tag 无 .confidence）的 AttributeError 同样 fail-closed，
            # 不得逃逸 process()。
            confidence = classification.confidence
        except Exception as exc:
            # 分类异常 fail-closed：UNKNOWN，过滤器自身不向上抛。
            tracing.record_classification_failure(
                envelope.trace_id, reason=_failure_reason(exc)
            )
            return envelope.model_copy(update={"intent": "UNKNOWN"})
        # 分类成功：trace 事件（来源取实现自报 source，缺省类名）。
        latency_ms = (time.monotonic() - started) * 1000
        tracing.record_classification(
            envelope.trace_id,
            tag=classification.tag,
            confidence=confidence,
            source=_classifier_source(self._classifier),
            latency_ms=latency_ms,
        )
        if envelope.turn is not None and self._intent_writer is not None:
            try:
                self._intent_writer(envelope.session_id, envelope.turn, classification)
            except Exception:
                # 写回失败 fail-safe：应答不降级、信封保留分类结果，
                # trace 记失败事件（绝不二次影响买家应答）。
                tracing.record_classification_failure(
                    envelope.trace_id, reason="intent_write_failed"
                )
        is_followup = self._record_followup(envelope, classification.tag)
        if not is_followup:
            return classified
        return classified.model_copy(update={"is_followup": True})

    def _record_followup(self, envelope: Envelope, tag: IntentTag) -> bool:
        """追问识别落点（Story 2.3）：原子计数、信封标记、trace 挂点。

        返回是否追问命中（per-intent ≥2）。前置条件不满足（turn 为
        None、未注入 recorder、tag 为 UNKNOWN）一律跳过：不调 recorder、
        不累积计数、信封 ``is_followup`` 保持默认 False。recorder 异常
        fail-safe 收敛在本方法内，不外抛。
        """
        if (
            envelope.turn is None
            or self._followup_recorder is None
            or tag == "UNKNOWN"
        ):
            return False
        try:
            per_intent_count, session_followup_count = self._followup_recorder(
                envelope.session_id, tag
            )
        except Exception:
            # 记录失败 fail-safe：应答不降级、信封保留分类结果、
            # is_followup 保持默认 False，trace 记失败事件。
            tracing.record_classification_failure(
                envelope.trace_id, reason="followup_record_failed"
            )
            return False
        is_followup = per_intent_count >= 2
        tracing.record_followup(
            envelope.trace_id,
            tag=tag,
            per_intent_count=per_intent_count,
            session_followup_count=session_followup_count,
            is_followup=is_followup,
        )
        if is_followup and session_followup_count == 3:
            # 仅产触发信号（trace 事件 + Redis 权威值）：判定与转人工
            # 归 Epic 5 的 AD-12 评估模块，本 Story 不写信封 handoff 字段。
            # == 3 而非 >= 3：HINCRBY 步长恒为 1，恰在穿越 3 的本次
            # 追问命中发一次信号；并发下恰一条消息得 3，不重复触发。
            tracing.record_followup_hard_trigger(
                envelope.trace_id, count=session_followup_count
            )
        return is_followup


def _failure_reason(exc: Exception) -> str:
    """trace 失败归因：实现自报 reason（如 timeout）优先，缺省异常类型名。"""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return type(exc).__name__


def _classifier_source(classifier: IntentClassifier) -> str:
    """trace 实现来源：分类器自报 source 属性优先，缺省类名。"""
    source = getattr(classifier, "source", None)
    if isinstance(source, str) and source:
        return source
    return type(classifier).__name__
