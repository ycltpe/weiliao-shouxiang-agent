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

Story 3.3：可选注入 ``recent_intent_writer`` / ``followup_snapshot_writer``
（AD-5 组合根注入 ``L1CacheClient.set_recent_intent`` /
``set_followup_count`` 方法引用，过滤器不持有 L1 客户端）：分类成功
且 ``turn`` 非 None 时写最近意图快照；``record_followup`` 成功返回后
写会话追问计数快照（会话总数）。两者均 try 包裹静默 fail-safe——
与 intent_writer 的非对称设计：L1 是进程内优化层，写失败只可能是
程序错误，不记 trace（避免热路径噪音）、不应答降级，信封结果不变。

Story 5.1：可选注入 ``handoff_assessor``（AD-12 单一评估点——四类
硬触发条件集中判定：红线意图 / 非文本媒体 / 追问达阈值 / 负面词）
与 ``handoff_marker``（AD-4 单写者——组合根注入
``SessionManager.mark_handoff`` 方法引用，过滤器不持有 Redis 客户端）。
分类与追问记录完成后调用评估模块，结果（触发标记 + 命中条件清单）
写信封 ``handoff`` 字段并记 trace（未命中也记，支撑漏判审计）；
追问记录失败 fail-safe 路径（2.3 语义）传追问序号 0 继续评估——
Redis 故障时追问条件不触发，但红线 / 媒体 / 负面条件照常判定。
评估代码自身异常 fail-closed：按触发处理（``conditions=
["assessment_error"]``），绝不放行到生成链路，并记 trace 告警；
触发时调用 marker（try 包裹 fail-safe，写失败经
``tracing.record_handoff_mark_failed`` 专用事件观测，不复用分类失败
信号）。分类异常 fail-closed 路径与空消息路径同口径：以 UNKNOWN /
追问序号 0 照常评估——媒体与负面词条件不依赖分类器输出，高风险
消息绝不在分类失败时漏入 cache/LLM。判定结果由编排器消费
（``triggered is True`` 短路下游走转人工出口分支）。

Story 6.3：链首 coreference 过滤器（真实 resolver，Story 6.1/6.2）
就位后 ``resolved_message`` 在生产链上必然有值：本过滤器决策输入
收窄为纯 ``resolved_message``——``None``/空白走既有 fail-closed
UNKNOWN 路径（分类器零调用），买家原文（``envelope.message``）在
任何决策路径零读取（FR-1/FR-11 终态口径）。追问计数键同步扩为
per-(tag, anchor)：anchor 由信封 ``coref`` 列表机械导出（排序拼接
``(entity_type, entity_id)``，空列表 → 哨兵值），随 tag 传入
recorder——意图标签仍为追问判定唯一依据，coref 只做计数键锚定、
绝不参与文本相似度比对（FR-2 口径）。硬触发评估与分类器共用同一
输入变量，收窄后文本口径自动一致，无需独立改动。
"""

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, Protocol

from weiliao_agent.filters.base import Filter
from weiliao_agent.handoff.trigger import (
    CONDITION_ASSESSMENT_ERROR,
    HardTriggerAssessor,
)
from weiliao_agent.models.coref import CorefRef
from weiliao_agent.models.envelope import Envelope, HandoffAssessment
from weiliao_agent.models.intent import IntentClassification, IntentTag
from weiliao_agent.observability import tracing

logger = logging.getLogger(__name__)

# 转人工状态写重试口径（2026-11-11 Epic 5 code review 裁决 A）：marker
# 单写者失败意味着会话未入终态——后续消息仍走 AI 管道（人机混答窗口），
# 必须终态尽力达且失败可观测。有界重试（Redis 瞬时故障窗口），总耗
# 时上限 ≈0.15s，远在过滤器链预算内；最终失败仅告警，应答口径不变。
_MARK_HANDOFF_MAX_ATTEMPTS = 3
_MARK_HANDOFF_RETRY_BACKOFF_SECONDS = 0.05


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
        followup_recorder: (
            Callable[[str, IntentTag, str], tuple[int, int, int]] | None
        ) = None,
        recent_intent_writer: Callable[[str, IntentTag], None] | None = None,
        followup_snapshot_writer: Callable[[str, int], None] | None = None,
        handoff_assessor: HardTriggerAssessor | None = None,
        handoff_marker: Callable[[str, list[str]], None] | None = None,
    ) -> None:
        self._classifier = classifier
        self._intent_writer = intent_writer
        self._followup_recorder = followup_recorder
        self._recent_intent_writer = recent_intent_writer
        self._followup_snapshot_writer = followup_snapshot_writer
        self._handoff_assessor = handoff_assessor
        self._handoff_marker = handoff_marker

    def process(self, envelope: Envelope) -> Envelope:
        if self._classifier is None:
            # 占位行为（Story 1.2，既有测试锁定）：缺失意图置 UNKNOWN。
            if envelope.intent is None:
                return envelope.model_copy(update={"intent": "UNKNOWN"})
            return envelope
        # 决策输入唯一来源为消解后消息（AD-11，Story 6.3 终态口径）：
        # 链首 coreference 过滤器 fail-safe 恒产出 resolved_message
        # （透传场景逐字节等于原文），买家原文在任何决策路径零读取。
        message = envelope.resolved_message
        if message is None or not message.strip():
            # 分类器契约要求非空消息文本：None、空串与纯空白一律
            # fail-closed UNKNOWN，不调用分类器（Story 6.3 空白口径）。
            # is_followup 一并复位：过滤器为追问判定唯一权威，payload
            # 预设值不允许透传（2026-11-10 评审补丁）。
            unknown = envelope.model_copy(
                update={"intent": "UNKNOWN", "is_followup": False}
            )
            # 硬触发评估不依赖分类器输出（媒体条件照常判定）：空消息
            # 路径以 UNKNOWN / 追问序号 0 评估，追问条件天然不触发。
            return self._assess_handoff(unknown, "UNKNOWN", 0, message)
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
                {
                    **envelope.model_dump(),
                    "intent": classification.tag,
                    # is_followup 复位为判定起点 False：追问命中仅在出口
                    # 单向置位（2026-11-10 评审补丁，payload 预设不透传）。
                    "is_followup": False,
                }
            )
            # confidence 提取同置 try 内（2.1 防御口径）：非一致性分类器
            # （有 .tag 无 .confidence）的 AttributeError 同样 fail-closed，
            # 不得逃逸 process()。
            confidence = classification.confidence
            if confidence != confidence:  # NaN 守卫（评审补丁）：duck-type
                # 实现绕过 IntentClassification 边界校验时，NaN metadata
                # 会致 Langfuse 序列化抛错被吞、分类成功事件静默丢失。
                raise ValueError("nan confidence from classifier")
        except Exception as exc:
            # 分类异常 fail-closed：UNKNOWN，过滤器自身不向上抛。
            # 硬触发评估照常执行（UNKNOWN / 追问序号 0，同空消息路径
            # 口径）：媒体与负面词条件不依赖分类器输出，绝不让高风险
            # 消息在分类失败时流入 cache/LLM。
            tracing.record_classification_failure(
                envelope.trace_id, reason=_failure_reason(exc)
            )
            unknown = envelope.model_copy(
                update={"intent": "UNKNOWN", "is_followup": False}
            )
            return self._assess_handoff(unknown, "UNKNOWN", 0, message)
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
        if (
            envelope.turn is not None
            # UNKNOWN 是降级信号而非真实意图：不落「最近意图」快照，
            # 防误导 Epic 4 的 LLM 上下文消费方（2026-11-11 评审裁决）。
            and classification.tag != "UNKNOWN"
            and self._recent_intent_writer is not None
        ):
            # L1 写失败静默 fail-safe（Story 3.3 设计笔记）：优化层
            # 不记 trace、不应答降级，信封保留分类结果。
            with suppress(Exception):
                self._recent_intent_writer(envelope.session_id, classification.tag)
        is_followup, _, per_tag_count = self._record_followup(
            envelope, classification.tag
        )
        assessed = self._assess_handoff(
            classified, classification.tag, per_tag_count, message
        )
        if not is_followup:
            return assessed
        return assessed.model_copy(update={"is_followup": True})

    def _record_followup(
        self, envelope: Envelope, tag: IntentTag
    ) -> tuple[bool, int, int]:
        """追问识别落点（Story 2.3）：原子计数、信封标记、trace 挂点。

        返回 ``(是否追问命中, per_(tag, anchor) 计数, per-tag 聚合
        计数)``——追问命中为 per-(tag, anchor) ≥2（is_followup 口径，
        Story 6.3 FR-2 锚定）；per-tag 聚合计数供 Story 5.1 硬触发
        追问条件消费（2026-11-11 Epic 5 code review 裁决 B：阈值判定
        回到「同一意图 ≥3」冻结口径，per-(tag, anchor) 细键会稀释
        阈值，勿复用 is_followup 的锚定口径）。anchor 为 FR-2 锚定
        计数键：由信封 ``coref`` 机械导出（排序拼接
        ``(entity_type, entity_id)``，空列表/None → 哨兵值），不读
        Redis 审计键、不回溯历史——锚定的唯一真相源是信封。前置条件
        不满足（turn 为 None、未注入 recorder、tag 为 UNKNOWN）一律
        跳过：不调 recorder、不累积计数、计数返回 0（追问条件不触发）、
        信封 ``is_followup`` 保持默认 False。
        recorder 异常 fail-safe 收敛在本方法内，不外抛。
        """
        if (
            envelope.turn is None
            or self._followup_recorder is None
            or tag == "UNKNOWN"
        ):
            return False, 0, 0
        anchor = _followup_anchor(envelope.coref)
        try:
            per_anchor_count, session_followup_count, per_tag_count = (
                self._followup_recorder(envelope.session_id, tag, anchor)
            )
        except Exception:
            # 记录失败 fail-safe：应答不降级、信封保留分类结果、
            # is_followup 保持默认 False、追问序号按 0 处理（追问条件
            # 不触发，2.3 语义沿用），trace 记失败事件。
            tracing.record_classification_failure(
                envelope.trace_id, reason="followup_record_failed"
            )
            return False, 0, 0
        is_followup = per_anchor_count >= 2
        if self._followup_snapshot_writer is not None:
            # 同 recent_intent_writer：L1 快照写失败静默吞，不记 trace。
            with suppress(Exception):
                self._followup_snapshot_writer(
                    envelope.session_id, session_followup_count
                )
        tracing.record_followup(
            envelope.trace_id,
            tag=tag,
            per_tag_anchor_count=per_anchor_count,
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
        return is_followup, per_anchor_count, per_tag_count

    def _assess_handoff(
        self,
        envelope: Envelope,
        tag: IntentTag,
        per_tag_count: int,
        message: str | None,
    ) -> Envelope:
        """硬触发评估落点（Story 5.1，AD-12）：集中判定、写信封、记 trace。

        未注入 assessor 时透传（判定集中挂载点缺失即不评估，信封
        ``handoff`` 保持 None）。评估未命中也记 trace（支撑漏判审计）；
        评估代码自身异常 fail-closed：按触发处理（conditions=
        [assessment_error]）并记 ERROR 级 trace 告警 + ERROR 级本地
        日志（Epic 5 code review 补丁：该路径持续故障会把全量消息
        误判触发，无本地留痕时仅 Langfuse 可见），消息绝不放行到
        生成链路。触发时经 marker 单写者落 handoff 状态（有界重试 +
        本地告警，2026-11-11 裁决 A：写失败意味着会话未入终态，必须
        尽力达且可观测；应答口径不变）；信封条件清单含内部诊断哨兵
        assessment_error（审计归因），但传给 marker 的清单过滤该
        哨兵——5.2 的转交载荷消费 handoff_conditions，只携带真实
        命中条件。
        """
        if self._handoff_assessor is None:
            return envelope
        try:
            assessment = self._handoff_assessor.assess(
                intent=tag,
                media_type=envelope.media_type,
                per_intent_followup_count=per_tag_count,
                message=message,
            )
        except Exception as exc:
            logger.error(
                "handoff assessment failed; fail-closed trigger "
                "session_id=%s trace_id=%s error=%s",
                envelope.session_id,
                envelope.trace_id,
                _failure_reason(exc),
                exc_info=True,
            )
            tracing.record_handoff_assessment(
                envelope.trace_id,
                triggered=True,
                conditions=[CONDITION_ASSESSMENT_ERROR],
                error=_failure_reason(exc),
            )
            assessment = HandoffAssessment(
                triggered=True, conditions=[CONDITION_ASSESSMENT_ERROR]
            )
        else:
            tracing.record_handoff_assessment(
                envelope.trace_id,
                triggered=assessment.triggered,
                conditions=assessment.conditions,
            )
        if assessment.triggered and self._handoff_marker is not None:
            # 内部诊断哨兵不入 Redis：handoff_conditions 供 5.2 转交
            # 载荷消费，只携带真实命中条件；信封清单保持不变。
            mark_conditions = [
                condition
                for condition in assessment.conditions
                if condition != CONDITION_ASSESSMENT_ERROR
            ]
            last_error: Exception | None = None
            for attempt in range(_MARK_HANDOFF_MAX_ATTEMPTS):
                try:
                    self._handoff_marker(envelope.session_id, mark_conditions)
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < _MARK_HANDOFF_MAX_ATTEMPTS:
                        time.sleep(
                            _MARK_HANDOFF_RETRY_BACKOFF_SECONDS * (attempt + 1)
                        )
                else:
                    break
            else:
                # 状态写失败 fail-safe：判定结果已挂信封，应答不降级；
                # 有界重试耗尽——会话未入终态（后续消息仍走 AI 管道，
                # 人机混答窗口），ERROR 本地告警（可检索可告警）+
                # trace 专用失败事件，绝不二次影响买家应答。
                logger.error(
                    "handoff mark failed after %d attempts; session not "
                    "terminal session_id=%s trace_id=%s conditions=%s "
                    "error=%s",
                    _MARK_HANDOFF_MAX_ATTEMPTS,
                    envelope.session_id,
                    envelope.trace_id,
                    mark_conditions,
                    type(last_error).__name__ if last_error else "unknown",
                    exc_info=last_error is not None,
                )
                tracing.record_handoff_mark_failed(envelope.trace_id)
        return envelope.model_copy(update={"handoff": assessment})


# 空 coref 哨兵锚（Story 6.3）：等价于旧 per-tag 键语义演进，向后兼容。
_NO_COREF_ANCHOR = "none"


def _followup_anchor(coref: list[CorefRef] | None) -> str:
    """FR-2 锚定导出：信封 coref 列表机械映射为计数锚点字符串。

    通用口径为 ``(entity_type, entity_id)`` 序列（MVP 仅 house 类型
    产实体锚点；非 house 类型 entity_id 可为 None，序列化为空段），
    按 ``(entity_type, entity_id)`` 排序后以 ``|`` 拼接——同一引用
    集合恒导出同一锚点，与列表顺序无关。空列表/None（无指代消息
    或过滤器未执行）走哨兵值，计数行为与既有 per-tag 口径一致。
    """
    if not coref:
        return _NO_COREF_ANCHOR
    refs = sorted(coref, key=lambda ref: (ref.entity_type, ref.entity_id or ""))
    return "|".join(
        f"{ref.entity_type}:{ref.entity_id or ''}" for ref in refs
    )


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
