"""应用入口：装配编排器、SessionManager、意图分类器与 HTTP 端点。

启动方式：``uvicorn weiliao_agent.main:app --reload``。
``/health`` 由 create_app 装配（Story 1.4，NFR-7）。

Story 2.2 组合根：``WEILIAO_INTENT_CLASSIFIER`` 选分类器实现
（默认 ``llm_zero_shot``；``none`` 保留占位行为），非法值由工厂
fail-fast——导入期抛 ``ValueError``，服务拒绝启动。意图写回经
``SessionManager.write_intent`` 方法引用注入（AD-4 单写者），路由
链路代码零改动。

Story 2.3 组合根：追问识别经 ``SessionManager.record_followup``
方法引用注入（AD-4 单写者），过滤器不持有 Redis 客户端；路由链路
其余装配不动。

Story 3.2 组合根：``house.sync.trigger`` 消费者任务由 lifespan 托管。
``WEILIAO_KAFKA_BROKERS`` 非空才创建消费者（缺省不启动，本地开发与
测试零 broker 零噪音）；复用下方 composition root 的 Redis 客户端构
建 L0CacheClient（消费者与 API 路径共享同一 redis-py 客户端实例——
线程安全，零进程内状态共享）。关停时先停拉取、等待在途消息处理，
超时强退不悬挂进程；消费者健康不挂 /health（fail-closed 逐消息降级
先例，Story 3.1 口径不变）。

Story 3.3 组合根：``L1CacheClient``（``WEILIAO_L1_TTL_SECONDS`` 缺省
30，AD-5 硬上限 30 超限 fail-fast）方法引用注入 intent_router
（``set_recent_intent`` / ``set_followup_count``——AD-4 权威写仍在
SessionManager，L1 仅为读优化快照）；模块级另建 ``L0CacheClient``
实例注入 cache_lookup（与 lifespan 内消费者实例相互独立，共享同一
redis-py 客户端）。

Story 4.3 组合根：``LlmGenerateFilter`` 插入 cache_lookup 与
risk_review 之间（影子接入决策 A）；注入预算 0.9s 的子图
（``LLM_GENERATE_BUDGET_SECONDS``，慢调用在过滤器内超时归一，防
拖过编排器剩余跳预算导致误降级）——装配期不触达 OpenAI 凭据，
生成草稿只挂信封 ``generated_reply``，买家可见应答仍 100% 模板兜底。

Story 4.4 组合根：``RiskReviewFilter`` 注入真实审核客户端（双审核
第二道，AD-8）；``WEILIAO_RISK_REVIEW_TIMEOUT_MS`` 缺省 500、越界
300–800 fail-fast 带变量名，``WEILIAO_RISK_REVIEW_ENDPOINT`` 未配置
则运行期 fail-closed（任何草稿视为不通过，绝不默认放行）；
``WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD`` 缺省 0.95、非 0–1
fail-fast 带变量名。``/health`` 就绪响应附审核通过率快照，lifespan
定时日志增发 ``risk_review_metrics``（跌破阈值时 ``risk_review_alert``
ERROR 告警）。

Story 4.5 组合根：LLM 熔断器（``llm.circuit_breaker.LlmCircuitBreaker``，
三态 + 确定性步进恢复）注入 LlmGenerateFilter——open 期间请求不调子图、
立即回模板兜底；``WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS`` 缺省 10、
非正 fail-fast 带变量名；``WEILIAO_LLM_BREAKER_RECOVERY_STEP`` 缺省
0.1、须 0<x<=1 越界 fail-fast 带变量名；指标经模块级单例归集，
``/health`` 附熔断器快照，lifespan 定时日志增发 ``llm_breaker_metrics``
（非 closed 态追加 ERROR 级 ``llm_breaker_alert`` 告警）。

Story 4.6 组合根：模型路由与 token 预算护栏（``llm.model_router.ModelRouter``，
按意图复杂度在 gpt-4o/gpt-4o-mini 间选择 + 每分钟滑窗累计 total_tokens
≥ 阈值即收紧、全部强制 mini、回落自动恢复）注入 LlmGenerationSubgraph——
路由层为每个 alias 各持一个 client 实例（client 单模型语义冻结不动）；
``WEILIAO_LLM_MINI_MODEL`` 缺省 gpt-4o-mini、strip 后为空 fail-fast
带变量名；``WEILIAO_LLM_ROUTING_SIMPLE_INTENTS`` 缺省由
DEFAULT_SIMPLE_INTENTS 派生、解析为空或含未知意图标签 fail-fast 带
变量名；``WEILIAO_LLM_ROUTING_BUDGET_TOKENS`` 缺省 50000、非正整数
fail-fast 带变量名；``WEILIAO_LLM_ROUTING_WINDOW_SECONDS`` 缺省 60、
非正或非有限（nan/inf）fail-fast 带变量名；主/mini alias 相同时
ERROR 告警留痕（不 fail-fast）。指标经模块级单例归集，``/health`` 附
模型路由快照，lifespan 定时日志增发 ``model_routing_metrics``（收紧态
追加 ERROR 级 ``model_routing_alert`` 告警）。

Story 3.4 组合根（A 方案）：cache_lookup 的追问禁走与命中率计数经
``observability.metrics`` 模块级单例归集；``/health`` 就绪响应附
metrics snapshot（压测 curl 即读）；lifespan 托管定时结构化 stderr
日志任务（间隔 ``WEILIAO_METRICS_LOG_SECONDS`` 缺省 60，非正整数
fail-fast 带变量名）。

Story 5.1 组合根：硬触发评估模块（``handoff.trigger.HardTriggerAssessor``，
AD-12 单一评估点——红线意图 / 非文本媒体 / 追问达阈值 / 负面词四类
条件集中判定）注入 intent_router，命中即由编排器短路下游走转人工
出口分支回固定文案；``handoff_marker`` 注入 ``SessionManager.mark_handoff``
（AD-4 单写者）。``WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD`` 缺省 3、非正
整数 fail-fast 带变量名；``WEILIAO_HANDOFF_REDLINE_INTENTS`` 缺省
NEGOTIATION,LOAN_POLICY,COMPLAINT、解析为空或含未知意图标签 fail-fast
带变量名；``WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH`` 缺省仓库内置词表
（``handoff/negative_words.txt``，风控中台产出发布），文件不可读或
无词条 fail-fast 带变量名（加载失败启动即败，不静默放行）。

Story 5.2 组合根：转人工单向指令发送器（``handoff.command.
HandoffCommandSender``，AD-7 先指令后通知——编排器出口分支先发送
转交指令、后返回安抚文案）注入编排器。复用 ``WEILIAO_KAFKA_BROKERS``
门控（与消费者同口径）：非空才装配发送器（本地开发/测试零 broker 时
注入 None，出口分支跳过发送、文案语义不变）。``WEILIAO_HANDOFF_KAFKA_TOPIC``
缺省 handoff.commands、``WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC`` 缺省
handoff.commands.dlq，strip 后为空或与指令 topic 相同 fail-fast 带
变量名（后者防死信写进 live topic）。发送器后台事件循环线程与
producer start/stop 归 lifespan 托管（装配期零网络触达）；producer
启动失败不拖垮服务——ERROR 告警 + 禁用发送器。
"""

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import redis

from weiliao_agent.adapters.http import create_app
from weiliao_agent.cache.l0_client import L0CacheClient
from weiliao_agent.cache.l1_cache import L1CacheClient
from weiliao_agent.classifiers import create_classifier
from weiliao_agent.filters.cache_lookup import CacheLookupFilter
from weiliao_agent.filters.coreference import CoreferenceFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.llm_generate import LlmGenerateFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import (
    MAX_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    RiskReviewClient,
    RiskReviewFilter,
)
from weiliao_agent.handoff.command import HandoffCommandSender
from weiliao_agent.handoff.trigger import HardTriggerAssessor, load_negative_words
from weiliao_agent.llm.circuit_breaker import LlmCircuitBreaker
from weiliao_agent.llm.client import OpenAiGenerationClient
from weiliao_agent.llm.model_router import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_MINI_MODEL_ALIAS,
    DEFAULT_SIMPLE_INTENTS,
    DEFAULT_WINDOW_SECONDS,
    KNOWN_INTENTS,
    ModelRouter,
)
from weiliao_agent.llm.subgraph import LlmGenerationSubgraph
from weiliao_agent.observability import metrics
from weiliao_agent.pipeline import LLM_GENERATE_BUDGET_SECONDS, PipelineOrchestrator
from weiliao_agent.session import SessionManager
from weiliao_agent.workers import HouseSyncConsumer, KafkaMessageSource

# Redis socket 超时须远小于 2.5s 内部预算（NFR-1 P99 < 3s 出口余量口径）。
REDIS_SOCKET_TIMEOUT_SECONDS = 0.5
# 关停消费者任务等待上限：超时强退（cancel 回收），不悬挂进程。
CONSUMER_SHUTDOWN_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger(__name__)

_redis_client = redis.Redis.from_url(
    os.environ.get("WEILIAO_REDIS_URL", "redis://127.0.0.1:6379/0"),
    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
)

_session_manager = SessionManager(_redis_client)
# 空串等空值回落默认实现（文档承诺），仅显式非法值 fail-fast。
_classifier = create_classifier(os.environ.get("WEILIAO_INTENT_CLASSIFIER") or "llm_zero_shot")
# L1 进程内缓存（AD-5）：TTL 缺省 30s、硬上限 30s（超限 fail-fast，
# 配置错误拒绝启动）；仅存会话热点快照，事实卡片严禁入 L1。
_L1_TTL_RAW = os.environ.get("WEILIAO_L1_TTL_SECONDS", "30")
try:
    _L1_TTL_SECONDS = int(_L1_TTL_RAW)
except ValueError as exc:
    msg = f"WEILIAO_L1_TTL_SECONDS must be an integer, got {_L1_TTL_RAW!r}"
    raise ValueError(msg) from exc
_l1_cache = L1CacheClient(ttl_seconds=_L1_TTL_SECONDS)
# 模块级 L0 实例注入 cache_lookup（与 lifespan 消费者实例独立，共享
# 同一 redis-py 客户端——线程安全，零进程内状态共享）。
_l0_cache = L0CacheClient(_redis_client)

# 命中率定时日志间隔（Story 3.4，A 方案）：非正整数配置错误拒绝启动
# （与 WEILIAO_L1_TTL_SECONDS 同口径，错误消息带变量名）。
_METRICS_LOG_RAW = os.environ.get("WEILIAO_METRICS_LOG_SECONDS", "60")
try:
    _METRICS_LOG_SECONDS = int(_METRICS_LOG_RAW)
except ValueError as exc:
    msg = f"WEILIAO_METRICS_LOG_SECONDS must be an integer, got {_METRICS_LOG_RAW!r}"
    raise ValueError(msg) from exc
if _METRICS_LOG_SECONDS <= 0:
    msg = f"WEILIAO_METRICS_LOG_SECONDS must be a positive integer, got {_METRICS_LOG_SECONDS}"
    raise ValueError(msg)

# 审核超时（Story 4.4）：缺省 500ms，可调 300–800；解析错误与越界均
# fail-fast 拒绝启动（错误消息带变量名）。endpoint 未配置不 fail-fast：
# 运行期 fail-closed（任何草稿视为不通过，本地开发/测试零中台可跑）。
_RISK_TIMEOUT_RAW = os.environ.get("WEILIAO_RISK_REVIEW_TIMEOUT_MS", "500")
try:
    _RISK_REVIEW_TIMEOUT_MS = int(_RISK_TIMEOUT_RAW)
except ValueError as exc:
    msg = f"WEILIAO_RISK_REVIEW_TIMEOUT_MS must be an integer, got {_RISK_TIMEOUT_RAW!r}"
    raise ValueError(msg) from exc
if not MIN_TIMEOUT_MS <= _RISK_REVIEW_TIMEOUT_MS <= MAX_TIMEOUT_MS:
    msg = (
        f"WEILIAO_RISK_REVIEW_TIMEOUT_MS must be within "
        f"{MIN_TIMEOUT_MS}-{MAX_TIMEOUT_MS}, got {_RISK_REVIEW_TIMEOUT_MS}"
    )
    raise ValueError(msg)

# 通过率告警阈值：缺省 0.95，须为 0–1 闭区间（越界 fail-fast 带变量名）；
# 跌破判定在 lifespan 定时日志（样本 ≥20 防抖动）。
_RISK_THRESHOLD_RAW = os.environ.get("WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD", "0.95")
try:
    _RISK_REVIEW_PASS_RATE_THRESHOLD = float(_RISK_THRESHOLD_RAW)
except ValueError as exc:
    msg = (
        f"WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD must be a number, "
        f"got {_RISK_THRESHOLD_RAW!r}"
    )
    raise ValueError(msg) from exc
if not 0.0 <= _RISK_REVIEW_PASS_RATE_THRESHOLD <= 1.0:
    msg = (
        f"WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD must be within 0-1, "
        f"got {_RISK_REVIEW_PASS_RATE_THRESHOLD}"
    )
    raise ValueError(msg)
metrics.get_risk_review_metrics().set_pass_rate_threshold(_RISK_REVIEW_PASS_RATE_THRESHOLD)
_risk_review_client = RiskReviewClient(timeout_ms=_RISK_REVIEW_TIMEOUT_MS)

# LLM 熔断器冷却期（Story 4.5）：缺省 10s；解析错误、非正与非有限
# （nan/inf：nan 令 open 立即抖回 half-open、inf 令熔断永不恢复）均
# fail-fast 拒绝启动（错误消息带变量名，同 WEILIAO_L1_TTL_SECONDS 口径）。
_LLM_BREAKER_COOLDOWN_RAW = os.environ.get("WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS", "10")
try:
    _LLM_BREAKER_COOLDOWN_SECONDS = float(_LLM_BREAKER_COOLDOWN_RAW)
except ValueError as exc:
    msg = (
        f"WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS must be a number, "
        f"got {_LLM_BREAKER_COOLDOWN_RAW!r}"
    )
    raise ValueError(msg) from exc
if (
    not math.isfinite(_LLM_BREAKER_COOLDOWN_SECONDS)
    or _LLM_BREAKER_COOLDOWN_SECONDS <= 0
):
    msg = (
        f"WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS must be a positive finite number, "
        f"got {_LLM_BREAKER_COOLDOWN_SECONDS}"
    )
    raise ValueError(msg)

# 恢复步进：缺省 0.1（half-open 按 10% 起步、试探成功 +10%、达 100%
# 闭合）；须 0<x<=1，越界 fail-fast 拒绝启动（错误消息带变量名）。
_LLM_BREAKER_STEP_RAW = os.environ.get("WEILIAO_LLM_BREAKER_RECOVERY_STEP", "0.1")
try:
    _LLM_BREAKER_RECOVERY_STEP = float(_LLM_BREAKER_STEP_RAW)
except ValueError as exc:
    msg = (
        f"WEILIAO_LLM_BREAKER_RECOVERY_STEP must be a number, "
        f"got {_LLM_BREAKER_STEP_RAW!r}"
    )
    raise ValueError(msg) from exc
if not 0.0 < _LLM_BREAKER_RECOVERY_STEP <= 1.0:
    msg = (
        f"WEILIAO_LLM_BREAKER_RECOVERY_STEP must be within (0, 1], "
        f"got {_LLM_BREAKER_RECOVERY_STEP}"
    )
    raise ValueError(msg)
# 组合根构造：指标单例注入（状态/计数归集与 /health、定时日志共用）；
# 纯进程内状态，装配期不触达 OpenAI 凭据。
_llm_breaker = LlmCircuitBreaker(
    cooldown_seconds=_LLM_BREAKER_COOLDOWN_SECONDS,
    recovery_step=_LLM_BREAKER_RECOVERY_STEP,
    metrics=metrics.get_llm_breaker_metrics(),
)

# 模型路由 mini alias（Story 4.6）：缺省 gpt-4o-mini；strip 一次后全程
# 使用 strip 值（带空白 alias 会直达 API）；strip 后为空（空串/纯空白）
# 为配置错误，fail-fast 拒绝启动（错误消息带变量名）。
_MINI_MODEL_RAW = os.environ.get("WEILIAO_LLM_MINI_MODEL", DEFAULT_MINI_MODEL_ALIAS)
_MINI_MODEL_ALIAS = _MINI_MODEL_RAW.strip()
if not _MINI_MODEL_ALIAS:
    msg = (
        f"WEILIAO_LLM_MINI_MODEL must be a non-empty model alias, "
        f"got {_MINI_MODEL_RAW!r}"
    )
    raise ValueError(msg)

# 简单意图名单：逗号分隔（可含空白），空条目容忍跳过；缺省值由
# model_router.DEFAULT_SIMPLE_INTENTS 派生（排序后逗号拼接），防两处
# 静默漂移。解析为空（显式置空/全是空白与逗号条目）令 mini 常规路由
# 静默失效，fail-fast 带变量名；含未知意图标签（不在 models/intent.py
# IntentTag 枚举内）同口径 fail-fast。
_SIMPLE_INTENTS_DEFAULT = ",".join(sorted(DEFAULT_SIMPLE_INTENTS))
_SIMPLE_INTENTS_RAW = os.environ.get(
    "WEILIAO_LLM_ROUTING_SIMPLE_INTENTS", _SIMPLE_INTENTS_DEFAULT
)
_SIMPLE_INTENT_LABELS = [
    label.strip() for label in _SIMPLE_INTENTS_RAW.split(",") if label.strip()
]
if not _SIMPLE_INTENT_LABELS:
    msg = (
        f"WEILIAO_LLM_ROUTING_SIMPLE_INTENTS must contain at least one "
        f"intent label, got {_SIMPLE_INTENTS_RAW!r}"
    )
    raise ValueError(msg)
_UNKNOWN_INTENT_LABELS = sorted(set(_SIMPLE_INTENT_LABELS) - KNOWN_INTENTS)
if _UNKNOWN_INTENT_LABELS:
    msg = (
        f"WEILIAO_LLM_ROUTING_SIMPLE_INTENTS contains unknown intent labels: "
        f"{_UNKNOWN_INTENT_LABELS!r} (valid: {sorted(KNOWN_INTENTS)!r})"
    )
    raise ValueError(msg)

# 滑窗 token 预算阈值：缺省 50000；非正整数（含 0/负数，nan/inf 无法
# 解析为 int 同归解析失败）fail-fast 拒绝启动（错误消息带变量名）。
_ROUTING_BUDGET_RAW = os.environ.get(
    "WEILIAO_LLM_ROUTING_BUDGET_TOKENS", str(DEFAULT_BUDGET_TOKENS)
)
try:
    _ROUTING_BUDGET_TOKENS = int(_ROUTING_BUDGET_RAW)
except ValueError as exc:
    msg = (
        f"WEILIAO_LLM_ROUTING_BUDGET_TOKENS must be an integer, "
        f"got {_ROUTING_BUDGET_RAW!r}"
    )
    raise ValueError(msg) from exc
if _ROUTING_BUDGET_TOKENS <= 0:
    msg = (
        f"WEILIAO_LLM_ROUTING_BUDGET_TOKENS must be a positive integer, "
        f"got {_ROUTING_BUDGET_TOKENS}"
    )
    raise ValueError(msg)

# 滑窗时长秒数：缺省 60（spec 决策：每分钟滑动窗口）；非正与非有限
# （nan/inf）fail-fast 拒绝启动（错误消息带变量名，同熔断器冷却口径）。
_ROUTING_WINDOW_RAW = os.environ.get(
    "WEILIAO_LLM_ROUTING_WINDOW_SECONDS", str(int(DEFAULT_WINDOW_SECONDS))
)
try:
    _ROUTING_WINDOW_SECONDS = float(_ROUTING_WINDOW_RAW)
except ValueError as exc:
    msg = (
        f"WEILIAO_LLM_ROUTING_WINDOW_SECONDS must be a number, "
        f"got {_ROUTING_WINDOW_RAW!r}"
    )
    raise ValueError(msg) from exc
if not math.isfinite(_ROUTING_WINDOW_SECONDS) or _ROUTING_WINDOW_SECONDS <= 0:
    msg = (
        f"WEILIAO_LLM_ROUTING_WINDOW_SECONDS must be a positive finite number, "
        f"got {_ROUTING_WINDOW_SECONDS}"
    )
    raise ValueError(msg)

# 组合根构造：主/mini 各持一个 client 实例（client 单模型语义冻结，
# 惰性构造——装配期不触达 OpenAI 凭据）；指标单例注入（计数/收紧态
# 归集与 /health、定时日志共用）；纯进程内状态。
_llm_primary_client = OpenAiGenerationClient(budget_seconds=LLM_GENERATE_BUDGET_SECONDS)
_llm_mini_client = OpenAiGenerationClient(
    model=_MINI_MODEL_ALIAS,
    budget_seconds=LLM_GENERATE_BUDGET_SECONDS,
)
_model_router = ModelRouter(
    _llm_primary_client,
    _llm_mini_client,
    simple_intents=frozenset(_SIMPLE_INTENT_LABELS),
    budget_tokens=_ROUTING_BUDGET_TOKENS,
    window_seconds=_ROUTING_WINDOW_SECONDS,
    metrics=metrics.get_model_routing_metrics(),
)
if _model_router.primary_alias == _model_router.mini_alias:
    # 双 alias 相同：路由故事静默失效（无省钱、无模型信号、forced mini
    # 退化为同模型）——不 fail-fast（默认装配 primary=WEILIAO_LLM_MODEL
    # 缺省 gpt-4o 与 mini 缺省不同，碰撞属显式配置选择），ERROR 告警留痕。
    logger.error(
        "model routing degenerate: primary and mini model aliases are "
        "identical (%r); routing saves nothing and forced-mini falls back "
        "to the same model",
        _model_router.primary_alias,
    )


# ---- Story 5.1：硬触发评估（AD-12 单一评估点）组合根配置 ----

# 追问触发阈值：缺省 3（FR 值，权威在 handoff.trigger 模块常量）；
# 非正整数（含 0/负数）fail-fast 拒绝启动（错误消息带变量名）。
_HANDOFF_THRESHOLD_RAW = os.environ.get("WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD", "3")
try:
    _HANDOFF_FOLLOWUP_THRESHOLD = int(_HANDOFF_THRESHOLD_RAW)
except ValueError as exc:
    msg = (
        f"WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD must be an integer, "
        f"got {_HANDOFF_THRESHOLD_RAW!r}"
    )
    raise ValueError(msg) from exc
if _HANDOFF_FOLLOWUP_THRESHOLD <= 0:
    msg = (
        f"WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD must be a positive integer, "
        f"got {_HANDOFF_FOLLOWUP_THRESHOLD}"
    )
    raise ValueError(msg)

# 红线意图集合：逗号分隔（可含空白），空条目容忍跳过；缺省
# NEGOTIATION,LOAN_POLICY,COMPLAINT（FR 值）；解析为空（显式置空会
# 令红线触发静默失效）或含未知意图标签（不在 models/intent.py
# IntentTag 枚举内）fail-fast 带变量名（同简单意图名单口径）。
_HANDOFF_REDLINE_RAW = os.environ.get(
    "WEILIAO_HANDOFF_REDLINE_INTENTS", "NEGOTIATION,LOAN_POLICY,COMPLAINT"
)
_HANDOFF_REDLINE_LABELS = [
    label.strip() for label in _HANDOFF_REDLINE_RAW.split(",") if label.strip()
]
if not _HANDOFF_REDLINE_LABELS:
    msg = (
        f"WEILIAO_HANDOFF_REDLINE_INTENTS must contain at least one "
        f"intent label, got {_HANDOFF_REDLINE_RAW!r}"
    )
    raise ValueError(msg)
_UNKNOWN_HANDOFF_LABELS = sorted(set(_HANDOFF_REDLINE_LABELS) - KNOWN_INTENTS)
if _UNKNOWN_HANDOFF_LABELS:
    msg = (
        f"WEILIAO_HANDOFF_REDLINE_INTENTS contains unknown intent labels: "
        f"{_UNKNOWN_HANDOFF_LABELS!r} (valid: {sorted(KNOWN_INTENTS)!r})"
    )
    raise ValueError(msg)

# 负面词表文件路径：缺省仓库内置词表（handoff/negative_words.txt，
# 风控中台产出发布）；词表加载失败（文件不可读/无词条）fail-fast 拒绝
# 启动（错误消息带变量名——不静默放行，决策 Q2-A）。
_DEFAULT_NEGATIVE_WORDS_PATH = (
    Path(__file__).resolve().parent / "handoff" / "negative_words.txt"
)
_HANDOFF_WORDS_PATH_RAW = os.environ.get(
    "WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH", str(_DEFAULT_NEGATIVE_WORDS_PATH)
)
try:
    _HANDOFF_NEGATIVE_WORDS = load_negative_words(_HANDOFF_WORDS_PATH_RAW)
except ValueError as exc:
    msg = (
        f"WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH is not loadable, "
        f"got {_HANDOFF_WORDS_PATH_RAW!r}: {exc}"
    )
    raise ValueError(msg) from exc

# 组合根构造：纯进程内状态，装配期只读词表文件（无网络/凭据触达）；
# 注入 intent_router（评估 + 命中后单写者落 handoff 状态）。
_handoff_assessor = HardTriggerAssessor(
    followup_threshold=_HANDOFF_FOLLOWUP_THRESHOLD,
    redline_intents=_HANDOFF_REDLINE_LABELS,
    negative_words=_HANDOFF_NEGATIVE_WORDS,
)


# ---- Story 5.2：转人工单向指令（Kafka producer 装配） ----

# 指令 topic：缺省 handoff.commands；strip 后为空 fail-fast（错误消息
# 带变量名）——空 topic 会让 producer 发送即败，配置错误拒绝启动。
_HANDOFF_TOPIC_RAW = os.environ.get("WEILIAO_HANDOFF_KAFKA_TOPIC", "handoff.commands")
_HANDOFF_KAFKA_TOPIC = _HANDOFF_TOPIC_RAW.strip()
if not _HANDOFF_KAFKA_TOPIC:
    msg = (
        f"WEILIAO_HANDOFF_KAFKA_TOPIC must be a non-empty topic, "
        f"got {_HANDOFF_TOPIC_RAW!r}"
    )
    raise ValueError(msg)

# DLQ topic：缺省 handoff.commands.dlq（独立死信队列）；strip 后为空
# fail-fast；与指令 topic 相同 fail-fast（构造器同口径拦截，死信写进
# live topic 且告警误导——评审轮次 1 补丁）。
_HANDOFF_DLQ_TOPIC_RAW = os.environ.get(
    "WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC", "handoff.commands.dlq"
)
_HANDOFF_KAFKA_DLQ_TOPIC = _HANDOFF_DLQ_TOPIC_RAW.strip()
if not _HANDOFF_KAFKA_DLQ_TOPIC:
    msg = (
        f"WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC must be a non-empty topic, "
        f"got {_HANDOFF_DLQ_TOPIC_RAW!r}"
    )
    raise ValueError(msg)
if _HANDOFF_KAFKA_DLQ_TOPIC == _HANDOFF_KAFKA_TOPIC:
    msg = (
        f"WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC must differ from "
        f"WEILIAO_HANDOFF_KAFKA_TOPIC, both are {_HANDOFF_KAFKA_DLQ_TOPIC!r}"
    )
    raise ValueError(msg)

# 发送器装配：复用 WEILIAO_KAFKA_BROKERS 门控（与消费者同口径）——
# broker 未配置（本地开发/测试）不创建发送器，编排器注入 None，出口
# 分支跳过发送、出口文案语义不变。装配期零网络触达：后台事件循环
# 线程与 producer start/stop 归 lifespan 托管。
_KAFKA_BROKERS = os.environ.get("WEILIAO_KAFKA_BROKERS")
_handoff_command_sender: HandoffCommandSender | None = None
if _KAFKA_BROKERS:
    _handoff_command_sender = HandoffCommandSender.kafka(
        brokers=_KAFKA_BROKERS,
        topic=_HANDOFF_KAFKA_TOPIC,
        dlq_topic=_HANDOFF_KAFKA_DLQ_TOPIC,
        transcript_reader=_session_manager.read_transcript,
    )


async def _metrics_log_loop() -> None:
    """定时发射缓存/审核/熔断器/模型路由结构化日志（first 发射在首个
    间隔之后）；跌破审核阈值、熔断器非 closed、路由收紧态时同循环追加
    ERROR 级告警日志。"""
    while True:
        await asyncio.sleep(_METRICS_LOG_SECONDS)
        metrics.emit_cache_metrics_log()
        metrics.emit_risk_review_metrics_log()
        metrics.emit_llm_breaker_metrics_log()
        metrics.emit_model_routing_metrics_log()


@asynccontextmanager
async def _lifespan(app: object) -> AsyncIterator[None]:
    """托管 Story 3.2 消费者任务与 Story 5.2 指令发送器生命周期。

    broker 未配置时消费者不创建、发送器不 start；发送器 producer
    启动失败不拖垮服务——ERROR 告警 + 禁用（出口分支跳过发送，
    出口文案语义不变）。
    """
    consumer: HouseSyncConsumer | None = None
    consumer_task: asyncio.Task[None] | None = None
    metrics_task: asyncio.Task[None] | None = None
    # 消费者门控运行期读 env（既有测试 monkeypatch env 的口径）；发送器
    # 装配期门控见模块级 _KAFKA_BROKERS（import 时定版）。
    if os.environ.get("WEILIAO_KAFKA_BROKERS"):
        consumer = HouseSyncConsumer(
            L0CacheClient(_redis_client),
            source=KafkaMessageSource(
                brokers=os.environ["WEILIAO_KAFKA_BROKERS"],
                topic=os.environ.get("WEILIAO_KAFKA_TOPIC", "house.sync.trigger"),
                group_id=os.environ.get("WEILIAO_KAFKA_GROUP_ID", "weiliao-house-sync"),
            ),
        )
        consumer_task = asyncio.create_task(consumer.run())
    if _handoff_command_sender is not None:
        try:
            await asyncio.to_thread(_handoff_command_sender.start)
        except Exception as exc:  # 启动失败不拖垮服务：禁用 + ERROR 告警
            logger.error(
                "handoff_command_alert producer start failed; sender disabled: %s",
                exc,
            )
            _handoff_command_sender.disable()
    metrics_task = asyncio.create_task(_metrics_log_loop())
    yield
    metrics_task.cancel()
    with suppress(asyncio.CancelledError):
        await metrics_task
    if _handoff_command_sender is not None:
        await asyncio.to_thread(_handoff_command_sender.close)
    if consumer is not None and consumer_task is not None:
        await consumer.stop()
        try:
            await asyncio.wait_for(consumer_task, timeout=CONSUMER_SHUTDOWN_TIMEOUT_SECONDS)
        except Exception as exc:  # 关停超时/任务异常强退：取消任务并回收
            logger.warning(
                "house sync consumer task did not stop cleanly; forcing cancel",
                exc_info=exc,
            )
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task


app = create_app(
    PipelineOrchestrator(
        filters=[
            # Story 6.1（AD-11）：指代消解过滤器占据链首——默认透传
            # （恒等消解，resolved_message = 原文拷贝），真实消解归
            # Story 6.2 / OQ-5 选型后组合根注入 resolver；审计回调为
            # AD-4 单写者方法引用（同 intent_writer 先例）。
            CoreferenceFilter(coref_writer=_session_manager.write_coref),
            IntentRouterFilter(
                classifier=_classifier,
                intent_writer=_session_manager.write_intent,
                followup_recorder=_session_manager.record_followup,
                recent_intent_writer=_l1_cache.set_recent_intent,
                followup_snapshot_writer=_l1_cache.set_followup_count,
                handoff_assessor=_handoff_assessor,
                handoff_marker=_session_manager.mark_handoff,
            ),
            CacheLookupFilter(l0_client=_l0_cache),
            LlmGenerateFilter(
                subgraph=LlmGenerationSubgraph(
                    _llm_primary_client,
                    router=_model_router,
                ),
                breaker=_llm_breaker,
            ),
            RiskReviewFilter(client=_risk_review_client),
            ResponseAssemblerFilter(),
        ],
        # Story 5.2（AD-7）：None（broker 未配置）时出口分支跳过发送。
        handoff_sender=_handoff_command_sender,
    ),
    _session_manager,
)
# HTTP 适配层冻结（spec Never 约束），lifespan 以 router 覆盖方式接入。
app.router.lifespan_context = _lifespan
