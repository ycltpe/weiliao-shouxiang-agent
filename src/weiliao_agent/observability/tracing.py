"""Langfuse 全链路追踪脚手架（Story 1.5）。

职责（架构一致性约定「可观测」）：
- trace_id 由编排器入口权威生成（``new_trace_id``，32 位小写十六进制，
  与 Langfuse trace id 同口径，响应 id 可直接在 Langfuse 检索）；
- 每消息记录三文本字段：买家原文 / 消解后消息 / 最终下发文本
  （后两者当前恒等于原文，coref（Story 2）与 LLM 生成（Epic 4）
  就位后渐进分化）；
- 降级事件、过滤器 span 写入消息 trace；成本追踪经 generation 观测
  记录 token 用量与费用（真实模型调用由 Epic 4 接入本封装）。

Fail-safe 约定（spec Boundaries）：本模块所有公开入口对 Langfuse SDK
的任何异常一律吞掉——追踪缺失绝不二次影响买家应答，也不得超出管道
时间预算；SDK 自身 flush 走后台线程，不阻塞请求路径。未配置 Langfuse
（无 ``LANGFUSE_PUBLIC_KEY`` 等环境变量）时 SDK 进入 disabled 态，全部
记录 no-op，管道行为与 Story 1.4 逐字节一致（仅多一条禁用 warning）。

意图分类事件（Story 2.2）：编排器入口 ``trace_message`` 的 intent 是
preset 值（分类尚未发生），分类结果只能经 ``record_classification`` /
``record_classification_failure`` 事件补挂同一 trace_id——含分类失败
与写回失败两类失败事件（2.1 评审 defer 的分类失败观测挂点）。

记录一律在主线程编排器侧、以 ``TraceContext(trace_id=...)`` 显式挂到
消息 trace：过滤器在 ThreadPoolExecutor 线程内执行，不做 OTel 跨线程
context 传播（spec Never 约束）。

追问识别事件（Story 2.3）：分类成功后 intent_router 经注入的会话单写者
回调记录 (session, intent) 计数，``record_followup`` 补挂追问事件
（tag / per_intent_count / session_followup_count / is_followup）；
会话追问总数达 3 时 ``record_followup_hard_trigger`` 产出 hard trigger
信号事件（命中条件=追问≥3，判定与转人工归 Epic 5 的 AD-12 模块）。
记录失败经 ``record_classification_failure``（reason=
``followup_record_failed``）观测，fail-safe 口径不变。

硬触发判定事件（Story 5.1）：intent_router 内 AD-12 单一评估模块
产出判定结果后 ``record_handoff_assessment`` 补挂事件——命中与未
命中全量记录（未命中 DEFAULT 级，支撑漏判审计）；触发为 WARNING 级
（设计内转人工，非故障）；判定代码自身异常 fail-closed 按触发处理，
ERROR 级并携带 error 归因。
"""

import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from typing import Any, Literal

from langfuse import Langfuse, get_client, propagate_attributes
from langfuse.types import TraceContext

# SDK 自定义 trace id 的合法口径（Langfuse._is_valid_trace_id 同规则）：
# 非 32 位小写十六进制的 id 会让 SDK 告警并抛 ValueError，故本层提前
# 收敛为跳过记录，避免异常驱动的控制流与日志噪音。
_VALID_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")

# 状态消息长度上限（降级归因等，防异常文本膨胀 trace）。
_STATUS_MESSAGE_LIMIT = 500

# ``house.sync.trigger`` 消费事件 operation 词表（Story 3.2 评审补丁）：
# 四值 Literal 权威定义，拼错即静态报错，防 ERROR 词被静默降级 DEFAULT。
HouseSyncOperation = Literal["consumed", "stale_alert", "corrupted", "failure"]

# 惰性单例缓存（get_client 内部按 public_key 单例）；测试可经
# ``set_langfuse_client`` 注入替身或重置回 None 走惰性初始化。
_client: Langfuse | None = None


def get_langfuse() -> Langfuse:
    """返回惰性单例 client；首次调用才初始化（未配置则 disabled 单例）。"""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = get_client()
    return _client


def set_langfuse_client(client: Langfuse | None) -> None:
    """注入/替换 client（测试替身）；传 None 恢复惰性单例路径。"""
    global _client  # noqa: PLW0603
    _client = client


def new_trace_id() -> str:
    """生成服务端权威 trace_id：32 位小写十六进制（Langfuse trace id 口径）。"""
    return uuid.uuid4().hex


def _recordable(trace_id: str | None) -> bool:
    """trace_id 是否可挂到 Langfuse trace（None 或非 32-hex 口径直接跳过）。"""
    return trace_id is not None and _VALID_TRACE_ID.fullmatch(trace_id) is not None


def _client_or_none() -> Langfuse | None:
    """惰性取 client；初始化本身异常也收敛为 None（全链路 no-op）。"""
    try:
        return get_langfuse()
    except Exception:
        return None


@contextmanager
def trace_message(
    *,
    trace_id: str | None,
    session_id: str,
    house_id: str,
    message: str | None,
    turn: int | None = None,
    intent: str | None = None,
) -> Iterator[None]:
    """消息级 trace 上下文：根 span 记买家原文与三文本字段，session_id 贯穿。

    当前口径：消解后消息恒等于买家原文（coref 过滤器属 Story 2），最终
    下发文本由编排器在管道完成后经 ``record_delivery`` 补记到 trace。
    上下文体的异常原样上抛（编排器自身兜底语义不变），SDK 侧异常吞掉。
    """
    if not _recordable(trace_id):
        yield
        return
    client = _client_or_none()
    if client is None:
        yield
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    # SDK 的同步/异步双模 CM 以 Any 承接：只依赖 __enter__/__exit__
    # 协议，避免引入 SDK 私有类型。enter 分步进行：任一步失败须退出
    # 已 enter 的前序 CM（评审补丁：防部分 enter 泄漏根 span）。
    root: Any = None
    attrs: Any = None
    try:
        root = client.start_as_current_observation(
            trace_context=TraceContext(trace_id=trace_id),
            name="pipeline.message",
            as_type="span",
            input={"session_id": session_id, "house_id": house_id, "message": message},
            metadata={
                "session_id": session_id,
                "house_id": house_id,
                "turn": turn,
                "intent": intent,
                "message": message,
                "resolved_message": message,
                "delivered_text": None,
            },
        )
        root.__enter__()
    except Exception:
        yield
        return
    try:
        attrs = propagate_attributes(session_id=session_id)
        attrs.__enter__()
    except Exception:
        with suppress(Exception):
            root.__exit__(None, None, None)
        yield
        return
    try:
        yield
    finally:
        # 逆序退出；SDK __exit__ 异常吞掉，不得影响编排器。
        with suppress(Exception):
            if attrs is not None:
                attrs.__exit__(None, None, None)
        with suppress(Exception):
            if root is not None:
                root.__exit__(None, None, None)


@contextmanager
def filter_span(trace_id: str | None, name: str) -> Iterator[None]:
    """过滤器执行 span（主线程侧记录；过滤器线程内不做 OTel 传播）。

    体异常（过滤器抛错/超时）照常上抛——编排器据此降级，语义不变；
    异常同时尽力以 ERROR 级标记到 span，便于 Langfuse 检索失败跳。
    """
    if not _recordable(trace_id):
        yield
        return
    client = _client_or_none()
    if client is None:
        yield
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    manager: Any = None
    span: Any = None
    try:
        manager = client.start_as_current_observation(
            trace_context=TraceContext(trace_id=trace_id),
            name=f"filter:{name}",
            as_type="span",
        )
    except Exception:
        yield
        return
    try:
        span = manager.__enter__()
    except Exception:
        with suppress(Exception):
            manager.__exit__(None, None, None)
        yield
        return
    try:
        yield
    except Exception as exc:
        with suppress(Exception):
            span.update(level="ERROR", status_message=str(exc)[:_STATUS_MESSAGE_LIMIT])
        raise
    finally:
        with suppress(Exception):
            manager.__exit__(None, None, None)


def record_degradation(
    trace_id: str | None,
    *,
    failed_filter: str | None = None,
    slow_filter: str | None = None,
    reason: str | None = None,
) -> None:
    """降级事件写入消息 trace（含归因过滤器名）。

    降级事件记录本身再抛异常也吞掉（I/O 矩阵：绝不二次影响应答）。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="pipeline.degradation",
            level="ERROR",
            metadata={
                "event": "pipeline_degradation",
                "failed_filter": failed_filter,
                "slow_filter": slow_filter,
                "reason": reason,
            },
        )


def record_delivery(
    trace_id: str | None,
    *,
    reply: str,
    degraded: bool,
    failed_filter: str | None = None,
) -> None:
    """最终下发文本写入消息 trace（三文本字段之三，SM-5/SM-6 口径）。"""
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="pipeline.delivery",
            output=reply,
            metadata={
                "event": "pipeline_delivery",
                "degraded": degraded,
                "failed_filter": failed_filter,
                "delivered_text": reply,
            },
        )
    # 评审补丁：根 span metadata 预置的 delivered_text=None 属过期占位，
    # 同步最终文本到当前 span（调用方位于 trace_message 上下文内）。
    with suppress(Exception):
        client.update_current_span(metadata={"delivered_text": reply})


def record_classification(
    trace_id: str | None,
    *,
    tag: str,
    confidence: float,
    source: str,
    latency_ms: float,
) -> None:
    """意图分类成功事件：标签/置信度/实现来源/延迟补挂消息 trace。

    fail-safe 口径同 ``record_degradation``：记录本身再抛异常也吞掉。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="intent.classification",
            metadata={
                "event": "intent_classification",
                "tag": tag,
                "confidence": confidence,
                "source": source,
                "latency_ms": round(latency_ms, 3),
            },
        )


def record_classification_failure(trace_id: str | None, *, reason: str) -> None:
    """意图分类/写回失败事件（reason=timeout / intent_write_failed 等）。

    失败事件记录本身再抛异常也吞掉（绝不二次影响买家应答）。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="intent.classification_failure",
            level="ERROR",
            metadata={
                "event": "intent_classification_failure",
                "reason": reason[:_STATUS_MESSAGE_LIMIT],
            },
        )


def record_followup(
    trace_id: str | None,
    *,
    tag: str,
    per_intent_count: int,
    session_followup_count: int,
    is_followup: bool,
) -> None:
    """追问记录事件（Story 2.3）：per-intent / 会话总数 / 标记补挂消息 trace。

    fail-safe 口径同 ``record_classification``：记录本身再抛异常也吞掉。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="intent.followup",
            metadata={
                "event": "intent_followup",
                "tag": tag,
                "per_intent_count": per_intent_count,
                "session_followup_count": session_followup_count,
                "is_followup": is_followup,
            },
        )


def record_followup_hard_trigger(trace_id: str | None, *, count: int) -> None:
    """追问 ≥3 hard trigger 信号事件（Story 2.3）：判定归 Epic 5（AD-12）。

    本事件仅为触发信号（Langfuse 可检索），不写信封 handoff 字段；
    判定方读 Redis 权威值而非任何快照。fail-safe 口径同上。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="intent.followup_hard_trigger",
            level="WARNING",
            metadata={
                "event": "followup_hard_trigger",
                "hit_condition": "followup>=3",
                "count": count,
            },
        )


def record_generation(
    trace_id: str | None,
    *,
    model: str,
    usage: Mapping[str, int],
    cost: Mapping[str, float],
    name: str = "model.generation",
) -> None:
    """成本追踪封装：token 用量与费用挂到当前消息 trace（Epic 4 消费）。

    脚手架级 API：真实模型调用接入时由 LLM 过滤器在拿到 usage/cost 后
    调用本封装；内部走 SDK generation 观测（usage_details/cost_details）。
    局限（评审补丁）：空上下文体意味着时长恒 ~0 且不记录 input/output，
    Epic 4 接入真实调用时须改 start/end 或 update 语义补全。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    try:
        with client.start_as_current_observation(
            trace_context=TraceContext(trace_id=trace_id),
            name=name,
            as_type="generation",
            model=model,
            usage_details=dict(usage),
            cost_details=dict(cost),
        ):
            pass
    except Exception:
        pass


def record_handoff_assessment(
    trace_id: str | None,
    *,
    triggered: bool,
    conditions: list[str],
    error: str | None = None,
) -> None:
    """硬触发判定事件（Story 5.1，AD-12）：命中与否全量记录，可审计漏判。

    事件分级：未命中为 DEFAULT 级（漏判审计基线）；触发为 WARNING 级
    （设计内转人工出口，非链路故障）；判定代码自身异常（fail-closed
    按触发处理）为 ERROR 级并携带 error 归因。fail-safe 口径同
    ``record_followup_hard_trigger``：记录本身再抛异常也吞掉。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    level: Literal["DEFAULT", "WARNING", "ERROR"]
    if error is not None:
        level = "ERROR"
    elif triggered:
        level = "WARNING"
    else:
        level = "DEFAULT"
    metadata: dict[str, Any] = {
        "event": "handoff_assessment",
        "triggered": triggered,
        "conditions": list(conditions),
        "error": error[:_STATUS_MESSAGE_LIMIT] if error else None,
    }
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="handoff.assessment",
            level=level,
            metadata=metadata,
        )


def record_handoff_mark_failed(trace_id: str | None) -> None:
    """handoff 状态写失败事件（Story 5.1）：marker 单写者落盘失败的
    专用观测锚点。

    不复用 ``record_classification_failure``：handoff 标记失败与意图
    分类失败是两条独立信号，混用会污染分类失败归因（评审补丁）。
    ERROR 级，fail-safe 口径同 ``record_handoff_assessment``：记录
    本身再抛异常也吞掉，绝不二次影响买家应答。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="handoff.mark_failed",
            level="ERROR",
            metadata={
                "event": "handoff_mark_failed",
                "reason": "handoff_mark_failed",
            },
        )


def record_cache_event(
    trace_id: str | None,
    *,
    operation: str,
    stage: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """缓存事件写入消息 trace（Story 3.1，L0 客户端复用）。

    事件 taxonomy（operation）：``hit``（读命中）/ ``miss``（L0 未命中，
    Story 3.3 起 cache_lookup 成对记录 hit/miss，3.4 命中率口径前置）/
    ``bypass``（追问禁走 L0，Story 3.4——不查不挂卡，DEFAULT 级）/
    ``write``（写入成功）/ ``invalidated``（逻辑作废，含读路径按墓碑
    拦截——同一 operation 兼作废动作与读拦截两语义）/ ``corrupted``
    （损坏数据，视为 miss）/ ``failure``（Redis 故障）。仅 ``corrupted``
    / ``failure`` 为 ERROR 级；其余 DEFAULT 级。stage 词表：
    ``set`` / ``get`` / ``invalidate``。

    保留键（``event`` / ``operation`` / ``stage``）由本函数权威写入，
    调用方 ``metadata`` 一律挂于独立 ``metadata`` 子键，不得覆盖保留键
    （嵌套或过滤，评审循环 3）。fail-safe 口径同 ``record_degradation``：
    记录本身再抛异常也吞掉，绝不二次影响买家应答。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    level: Literal["DEFAULT", "ERROR"] = (
        "ERROR" if operation in ("corrupted", "failure") else "DEFAULT"
    )
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="cache.event",
            level=level,
            metadata={
                "event": "cache_event",
                "operation": operation,
                "stage": stage,
                "metadata": dict(metadata) if metadata else {},
            },
        )


def record_llm_generate_event(
    trace_id: str | None,
    *,
    outcome: Literal["grounding_failed", "generation_error"],
    reason: str,
) -> None:
    """LLM 受控生成失败事件（Story 4.3）：仅可观测，不影响买家应答。

    事件 taxonomy（outcome）：``grounding_failed``（确定性校验未过，
    草稿被丢弃）/ ``generation_error``（模型调用异常/凭据缺失，无草稿）。
    两值均为 ERROR 级。影子接入口径：本事件只记录生成链路故障，
    ``degraded`` 语义不动（买家可见应答始终是模板兜底，AC 第 4 条
    「行为与 4.2 前逐字节一致」经此与编排器降级事件区分开）。
    fail-safe 口径同 ``record_degradation``：记录本身再抛异常也吞掉。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="llm.generate_event",
            level="ERROR",
            metadata={
                "event": "llm_generate_event",
                "outcome": outcome,
                "reason": reason[:_STATUS_MESSAGE_LIMIT],
            },
        )


def record_risk_review_event(
    trace_id: str | None,
    *,
    outcome: Literal["passed", "rejected", "timeout", "error"],
    latency_ms: float,
    reason: str | None = None,
) -> None:
    """后置审核结果事件（Story 4.4）：仅可观测，不影响买家应答。

    事件 taxonomy（outcome）：``passed``（中台放行，DEFAULT 级）/
    ``rejected``（中台显式拒绝）/ ``timeout``（超时视为不通过）/
    ``error``（网络错/非 200/坏响应/endpoint 未配置）。后三者为 ERROR
    级并携带 reason 归因。审核拒绝/超时属设计内行为：``degraded`` 语义
    不动，质量信号经本事件与通过率指标表达。fail-safe 口径同
    ``record_llm_generate_event``：记录本身再抛异常也吞掉。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    level: Literal["DEFAULT", "ERROR"] = "ERROR" if outcome != "passed" else "DEFAULT"
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="risk_review.event",
            level=level,
            metadata={
                "event": "risk_review_event",
                "outcome": outcome,
                "latency_ms": round(latency_ms, 3),
                "reason": reason[:_STATUS_MESSAGE_LIMIT] if reason else None,
            },
        )


def record_llm_breaker_event(
    trace_id: str | None,
    *,
    outcome: Literal["tripped", "skip", "recovered"],
    reason: str | None = None,
) -> None:
    """LLM 熔断器事件（Story 4.5）：降级/恢复全程可检索锚点。

    事件 taxonomy（outcome）：``tripped``（OpenAI 侧故障致熔断器跳闸
    open，ERROR 级，携带 reason 归因）/ ``skip``（open/half-open 期间
    请求被跳过、零子图调用回模板兜底，DEFAULT 级——open 期每次跳过都
    记 ERROR 会造成噪音洪流，ERROR 只留给跳闸与告警日志）/
    ``recovered``（步进试探成功、放行比例达 100% 闭合 closed，DEFAULT
    级）。买家可见语义逐字节不动（degraded=False），本事件仅质量信号。
    fail-safe 口径同 ``record_risk_review_event``：记录本身再抛异常也吞掉。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    level: Literal["DEFAULT", "ERROR"] = "ERROR" if outcome == "tripped" else "DEFAULT"
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="llm.breaker_event",
            level=level,
            metadata={
                "event": "llm_breaker_event",
                "outcome": outcome,
                "reason": reason[:_STATUS_MESSAGE_LIMIT] if reason else None,
            },
        )


def record_model_routing_event(
    trace_id: str | None,
    *,
    outcome: Literal["primary", "mini", "forced_mini", "recovered"],
    intent: str | None = None,
) -> None:
    """模型路由事件（Story 4.6）：单条消息可复盘路由判定与收紧/恢复。

    事件 taxonomy（outcome）：``primary``（名单外意图或 intent=None 走
    主模型，DEFAULT 级）/ ``mini``（名单内简单意图走 mini 模型，DEFAULT
    级）/ ``forced_mini``（滑窗预算收紧期强制 mini，ERROR 级——成本异常
    信号锚点）/ ``recovered``（窗口消耗回落阈值以下、自动恢复常规路由，
    DEFAULT 级）。买家可见语义逐字节不动（路由只决定用哪个模型生成），
    本事件仅成本与质量复盘信号；ERROR 级 ``model_routing_alert`` 告警
    日志由指标定时日志通道发射（收紧态追加）。
    fail-safe 口径同 ``record_llm_breaker_event``：记录本身再抛异常也吞掉。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    level: Literal["DEFAULT", "ERROR"] = "ERROR" if outcome == "forced_mini" else "DEFAULT"
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="llm.model_routing_event",
            level=level,
            metadata={
                "event": "model_routing_event",
                "outcome": outcome,
                "intent": intent,
            },
        )


def record_house_sync_event(
    trace_id: str | None,
    *,
    operation: HouseSyncOperation,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """``house.sync.trigger`` 消费事件写入独立 trace（Story 3.2）。

    事件 taxonomy（operation）：``consumed``（消息处理完成：upsert 写入、
    invalidate 生效或超期转失效——含延迟 metadata）/ ``stale_alert``
    （超期 >500ms 事件按失效处理 + 告警）/ ``corrupted``（JSON 损坏 /
    schema 校验失败，毒消息跳过并提交 offset）/ ``failure``（L0 写失败
    未提交 offset 可重放、Kafka 断连重连）。仅 ``stale_alert`` /
    ``corrupted`` / ``failure`` 为 ERROR 级；``consumed`` 为 DEFAULT 级。

    与 ``record_cache_event`` 同口径：保留键（``event`` / ``operation``）
    由本函数权威写入，调用方 ``metadata`` 一律挂于独立 ``metadata`` 子键；
    每事件独立 trace_id（消费者侧经 ``new_trace_id`` 生成，与消息 trace
    解耦——消费链路无买家消息 trace 可挂）。fail-safe：记录本身再抛异常
    也吞掉，绝不令消费者崩溃或影响 API 路径。
    """
    if not _recordable(trace_id):
        return
    client = _client_or_none()
    if client is None:
        return
    assert trace_id is not None  # _recordable 已排除 None，供 mypy 收窄
    level: Literal["DEFAULT", "ERROR"] = (
        "ERROR" if operation in ("stale_alert", "corrupted", "failure") else "DEFAULT"
    )
    with suppress(Exception):
        client.create_event(
            trace_context=TraceContext(trace_id=trace_id),
            name="house_sync.event",
            level=level,
            metadata={
                "event": "house_sync_event",
                "operation": operation,
                "metadata": dict(metadata) if metadata else {},
            },
        )
