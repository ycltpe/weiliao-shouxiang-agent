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

Story 3.4 组合根（A 方案）：cache_lookup 的追问禁走与命中率计数经
``observability.metrics`` 模块级单例归集；``/health`` 就绪响应附
metrics snapshot（压测 curl 即读）；lifespan 托管定时结构化 stderr
日志任务（间隔 ``WEILIAO_METRICS_LOG_SECONDS`` 缺省 60，非正整数
fail-fast 带变量名）。
"""

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import redis

from weiliao_agent.adapters.http import create_app
from weiliao_agent.cache.l0_client import L0CacheClient
from weiliao_agent.cache.l1_cache import L1CacheClient
from weiliao_agent.classifiers import create_classifier
from weiliao_agent.filters.cache_lookup import CacheLookupFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.llm_generate import LlmGenerateFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import (
    MAX_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    RiskReviewClient,
    RiskReviewFilter,
)
from weiliao_agent.llm.circuit_breaker import LlmCircuitBreaker
from weiliao_agent.llm.client import OpenAiGenerationClient
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


async def _metrics_log_loop() -> None:
    """定时发射缓存命中率与审核通过率结构化日志（first 发射在首个
    间隔之后）；跌破审核阈值时同循环追加 ERROR 级告警日志。"""
    while True:
        await asyncio.sleep(_METRICS_LOG_SECONDS)
        metrics.emit_cache_metrics_log()
        metrics.emit_risk_review_metrics_log()
        metrics.emit_llm_breaker_metrics_log()


@asynccontextmanager
async def _lifespan(app: object) -> AsyncIterator[None]:
    """托管 Story 3.2 消费者任务；broker 未配置时消费者不创建。"""
    consumer: HouseSyncConsumer | None = None
    consumer_task: asyncio.Task[None] | None = None
    metrics_task: asyncio.Task[None] | None = None
    brokers = os.environ.get("WEILIAO_KAFKA_BROKERS")
    if brokers:
        consumer = HouseSyncConsumer(
            L0CacheClient(_redis_client),
            source=KafkaMessageSource(
                brokers=brokers,
                topic=os.environ.get("WEILIAO_KAFKA_TOPIC", "house.sync.trigger"),
                group_id=os.environ.get("WEILIAO_KAFKA_GROUP_ID", "weiliao-house-sync"),
            ),
        )
        consumer_task = asyncio.create_task(consumer.run())
    metrics_task = asyncio.create_task(_metrics_log_loop())
    yield
    metrics_task.cancel()
    with suppress(asyncio.CancelledError):
        await metrics_task
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
            IntentRouterFilter(
                classifier=_classifier,
                intent_writer=_session_manager.write_intent,
                followup_recorder=_session_manager.record_followup,
                recent_intent_writer=_l1_cache.set_recent_intent,
                followup_snapshot_writer=_l1_cache.set_followup_count,
            ),
            CacheLookupFilter(l0_client=_l0_cache),
            LlmGenerateFilter(
                subgraph=LlmGenerationSubgraph(
                    OpenAiGenerationClient(budget_seconds=LLM_GENERATE_BUDGET_SECONDS)
                ),
                breaker=_llm_breaker,
            ),
            RiskReviewFilter(client=_risk_review_client),
            ResponseAssemblerFilter(),
        ]
    ),
    _session_manager,
)
# HTTP 适配层冻结（spec Never 约束），lifespan 以 router 覆盖方式接入。
app.router.lifespan_context = _lifespan
