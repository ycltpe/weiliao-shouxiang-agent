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
"""

import asyncio
import logging
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
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import RiskReviewFilter
from weiliao_agent.pipeline import PipelineOrchestrator
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


@asynccontextmanager
async def _lifespan(app: object) -> AsyncIterator[None]:
    """托管 Story 3.2 消费者任务；broker 未配置时消费者不创建。"""
    consumer: HouseSyncConsumer | None = None
    consumer_task: asyncio.Task[None] | None = None
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
    yield
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
            RiskReviewFilter(),
            ResponseAssemblerFilter(),
        ]
    ),
    _session_manager,
)
# HTTP 适配层冻结（spec Never 约束），lifespan 以 router 覆盖方式接入。
app.router.lifespan_context = _lifespan
