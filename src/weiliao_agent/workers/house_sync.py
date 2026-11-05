"""``house.sync.trigger`` 异步消费者（Story 3.2，AD-6 事实卡片唯一写入路径）。

事件消费是事实卡片的唯一写入路径：本消费者直写 L0 缓存、不碰 DB。
处理链：反序列化（Pydantic 契约校验）→ 超期判定（延迟 >500ms 自
``occurred_at`` 起算，超期事件不写入内容、一律按失效处理 + 告警）→
upsert 构建 ``FactCard`` 写全部 9 类 intent / invalidate 写墓碑 →
**L0 写成功后才提交 offset**（at-least-once：Redis 故障不提交可重放，
重放幂等由 Story 3.1 fact_version 三 Lua 守卫保证；解析失败的毒消息
跳过并提交，不阻塞分区）。

Fail-safe（冻结区）：解析失败、Redis 故障、Kafka 断连、提交失败均不得
使消费者崩溃或影响 API 路径——未知异常一律经指数退避重连消化；观测
一律走 Langfuse fail-safe 模式（``record_house_sync_event``）+ 结构化
stderr 日志。Redis 客户端为同步 redis-py，经 ``asyncio.to_thread`` 卸载。

版本冲突口径：L0 ``set`` 返回 False 有两种成因——守卫拒写（重放/乱序/
墓碑）或 Redis 故障。消费者两段诊断区分：``get`` 读到版本不低于事件
版本的事实卡片即守卫拒写（offset 正常提交、冲突计入 consumed 事件
metadata）；``get`` 返回 None 时以 ``invalidate`` 作健康探针（墓碑守卫
拒写下现存墓碑版本必 ≥ 事件版本，探针幂等不重：生效版本取最大者），
探针失败即 Redis 故障（不提交 offset、记 failure）；探针成功而重试 set
仍被拒即确认守卫拒写（提交 offset）。探针副作用与作废事件同语义：
已有墓碑时仅刷新其 TTL，无现存墓碑时立新墓碑（版本即事件版本）——
fail-safe 优先级（「永不错误返回旧卡片」）不受损。

消费者为独立 asyncio task（组合根 lifespan 托管），与 API 请求路径
零共享状态；启动门控 ``WEILIAO_KAFKA_BROKERS`` 非空（main.py）。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Protocol, cast, get_args

# aiokafka 未随包发布 py.typed（0.15.0 实测无标记），mypy strict 下按
# import-untyped 最小化豁免：本模块仅以 Any 触碰其 API，不做精细类型假设。
from aiokafka import AIOKafkaConsumer  # type: ignore[import-untyped]
from aiokafka.structs import TopicPartition  # type: ignore[import-untyped]

from weiliao_agent.cache.l0_client import L0CacheClient
from weiliao_agent.models.house_event import HouseSyncEvent
from weiliao_agent.models.intent import IntentTag
from weiliao_agent.observability import tracing

logger = logging.getLogger("weiliao_agent.workers.house_sync")

# 消费延迟预算：自事件 occurred_at 起算至 L0 写完成，>500ms 即超期
# （超期口径权威定义见 spec Design Notes：「永不错误返回旧卡片」优先）。
STALE_AFTER = timedelta(milliseconds=500)

# 断连重连退避：初值 1s、上限 30s，指数翻倍。
_INITIAL_BACKOFF = timedelta(seconds=1)
_MAX_BACKOFF = timedelta(seconds=30)

# upsert 事件写入全部 9 类 intent（验收标准：各 intent get 返回同版本卡片）。
ALL_INTENTS: tuple[IntentTag, ...] = get_args(IntentTag)


class Message:
    """单条待消费消息：原始 payload + 处理成功后的 offset 提交钩子。"""

    def __init__(
        self,
        payload: bytes,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        self.payload = payload
        self._commit = commit

    async def commit(self) -> None:
        """提交 offset。

        两种合法时机：L0 写成功后提交（正常路径）；毒消息跳过时也须
        提交（解析失败不设重试点，防阻塞分区）。L0 写失败路径不得提交。
        """
        await self._commit()


class MessageSource(Protocol):
    """消息源抽象：生产实现为 aiokafka；测试注入内存队列替身。"""

    async def start(self) -> None:
        """建立连接（不可达时抛错，由 run 退避重连）。"""

    async def stop(self) -> None:
        """关闭连接（幂等；运行中调用可打断迭代）。"""

    def messages(self) -> AsyncIterator[Message]:
        """消息异步迭代流。"""


class KafkaMessageSource:
    """aiokafka 消息源：手动提交（enable_auto_commit=False）保 at-least-once。

    提交粒度为单条 record（TopicPartition → offset+1）：顺序处理下位置
    提交会把「已拉取未处理」的消息一并标记，崩丢失 at-least-once 承诺，
    故逐 record 精确提交。
    """

    def __init__(self, *, brokers: str, topic: str, group_id: str) -> None:
        self._consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=brokers,
            group_id=group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    async def start(self) -> None:
        await self._consumer.start()

    async def stop(self) -> None:
        await self._consumer.stop()

    async def messages(self) -> AsyncIterator[Message]:
        async for record in self._consumer:
            yield Message(
                payload=cast("bytes", record.value),
                commit=partial(self._commit_record, record),
            )

    async def _commit_record(self, record: Any) -> None:
        # aiokafka 无类型标记（import-untyped），record 以 Any 承接：
        # 仅经其公共属性组装 TopicPartition 精确提交单条 offset。
        topic = record.topic
        partition = record.partition
        offset = record.offset
        tp = TopicPartition(topic, partition)
        await self._consumer.commit({tp: offset + 1})


class HouseSyncConsumer:
    """``house.sync.trigger`` 消费者：反序列化 → 超期判定 → L0 写 → 提交。"""

    def __init__(
        self,
        l0: L0CacheClient,
        *,
        source: MessageSource,
        stale_after: timedelta = STALE_AFTER,
        initial_backoff: timedelta = _INITIAL_BACKOFF,
        max_backoff: timedelta = _MAX_BACKOFF,
    ) -> None:
        self._l0 = l0
        self._source = source
        self._stale_after = stale_after
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._stopped = False

    async def run(self) -> None:
        """消费主循环：断连/未知异常一律指数退避重连，永不崩溃（fail-safe）。"""
        backoff = self._initial_backoff
        while not self._stopped:
            try:
                await self._source.start()
                backoff = self._initial_backoff
                async for message in self._source.messages():
                    if self._stopped:
                        break
                    await self._handle_message(message)
            except Exception as exc:  # fail-safe 冻结区口径：未知异常也不崩溃
                if not self._stopped:
                    self._record_failure(
                        "consume_loop_error",
                        trace_id=tracing.new_trace_id(),
                        error_type=type(exc).__name__,
                        backoff_seconds=backoff.total_seconds(),
                    )
            finally:
                with suppress(Exception):  # 关闭失败也不抛出（fail-safe）
                    await self._source.stop()
            if self._stopped:
                break
            await asyncio.sleep(backoff.total_seconds())
            backoff = min(backoff * 2, self._max_backoff)

    async def stop(self) -> None:
        """请求关停：置停止位并关闭消息源（打断在途迭代，幂等）。"""
        self._stopped = True
        with suppress(Exception):  # 关停路径绝不抛出（fail-safe）
            await self._source.stop()

    async def _handle_message(self, message: Message) -> None:
        """单消息处理：毒消息跳过并提交；L0 写失败不提交（可重放）。"""
        trace_id = tracing.new_trace_id()
        event = self._deserialize(message.payload, trace_id)
        if event is None:
            await self._commit_safely(message, trace_id)
            return
        # 入口延迟仅用于超期判定：保守口径，处理策略在消息入口一次定
        # 夺，不因处理中途的时钟推进而半途翻转。consumed 事件所记录的
        # latency_ms 是另一口径——由各 handler 在 L0 写完成前经
        # _refresh_latency 重算（occurred_at→写完成，AC 权威口径）。
        latency = datetime.now(UTC) - event.occurred_at
        metadata: dict[str, object] = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "house_id": event.house_id,
            "fact_version": event.fact_version,
            "latency_ms": latency.total_seconds() * 1000,
        }
        if latency > self._stale_after:
            await self._handle_stale(event, metadata, message, trace_id)
        elif event.event_type == "house_upsert":
            await self._handle_upsert(event, metadata, message, trace_id)
        else:
            await self._handle_invalidate(event, metadata, message, trace_id)

    def _deserialize(self, payload: bytes, trace_id: str) -> HouseSyncEvent | None:
        """JSON + schema 双段校验：损坏一律按毒消息（corrupted 事件）。

        ``TypeError`` 覆盖 Kafka tombstone 记录（record.value=None）：
        按毒消息跳过并提交 offset，不阻塞分区。
        """
        try:
            decoded = json.loads(payload)
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            self._record_corrupted(trace_id, reason=f"json_decode_error:{type(exc).__name__}")
            return None
        try:
            return HouseSyncEvent.model_validate(decoded)
        except ValueError as exc:
            self._record_corrupted(trace_id, reason=f"schema_validation_error:{type(exc).__name__}")
            return None

    async def _handle_upsert(
        self,
        event: HouseSyncEvent,
        metadata: dict[str, object],
        message: Message,
        trace_id: str,
    ) -> None:
        """upsert：FactCard 写全部 intent；守卫拒写提交、Redis 故障不提交。

        set 返回 False 的两段诊断（守卫拒写 vs Redis 故障）见模块
        docstring：get 裁决索引守卫，invalidate 健康探针 + set 重试裁决
        墓碑守卫（探针成功后的重试同时兜住 Redis 瞬断恢复的补写窗口）。
        """
        card = event.to_fact_card()
        version_conflict = False
        probe_retry = False
        for intent in ALL_INTENTS:
            written = await asyncio.to_thread(
                self._l0.set, intent, card, trace_id=trace_id
            )
            if written:
                continue
            existing = await asyncio.to_thread(
                self._l0.get, intent, event.house_id, trace_id=trace_id
            )
            if existing is not None and existing.fact_version >= event.fact_version:
                # Story 3.1 守卫拒写（迟到的低版本/同版本重放）：offset 正常
                # 提交，冲突计入事件 metadata——无旧事实复活。
                version_conflict = True
                continue
            # set False 且 get 为 None：墓碑守卫拒写或 Redis 故障——
            # invalidate 健康探针区分（墓碑守卫下现存墓碑版本必 ≥ 事件
            # 版本，探针幂等不重；Redis 故障下探针同样失败）。
            probed = await asyncio.to_thread(
                self._l0.invalidate,
                event.house_id,
                event.fact_version,
                trace_id=trace_id,
            )
            if not probed:
                self._record_failure(
                    "l0_set_failed",
                    trace_id=trace_id,
                    intent=intent,
                    house_id=event.house_id,
                    fact_version=event.fact_version,
                )
                return  # Redis 故障：不提交 offset，事件可重放
            # 探针成功（Redis 健康）而 set 仍被拒：确认守卫拒写。
            retried = await asyncio.to_thread(
                self._l0.set, intent, card, trace_id=trace_id
            )
            if retried:
                probe_retry = True
                continue
            version_conflict = True
            break  # 探针已立墓碑 ≥ 事件版本：其余 intent 必同判定，短路
        if probe_retry:
            metadata["probe_retry"] = True
        metadata["version_conflict"] = version_conflict
        self._refresh_latency(event, metadata)
        tracing.record_house_sync_event(trace_id, operation="consumed", metadata=metadata)
        await self._commit_safely(message, trace_id)

    async def _handle_invalidate(
        self,
        event: HouseSyncEvent,
        metadata: dict[str, object],
        message: Message,
        trace_id: str,
    ) -> None:
        """invalidate：全 intent 墓碑作废；Redis 故障不提交（可重放）。"""
        ok = await asyncio.to_thread(
            self._l0.invalidate,
            event.house_id,
            event.fact_version,
            trace_id=trace_id,
        )
        if not ok:
            self._record_failure(
                "l0_invalidate_failed",
                trace_id=trace_id,
                house_id=event.house_id,
                fact_version=event.fact_version,
            )
            return
        self._refresh_latency(event, metadata)
        tracing.record_house_sync_event(trace_id, operation="consumed", metadata=metadata)
        await self._commit_safely(message, trace_id)

    async def _handle_stale(
        self,
        event: HouseSyncEvent,
        metadata: dict[str, object],
        message: Message,
        trace_id: str,
    ) -> None:
        """超期事件：内容不写入（防旧事实落地），按失效处理 + 告警（ERROR）。

        告警在 invalidate 成功之后发出：失败路径 offset 不提交、事件将经
        at-least-once 重放，提前告警会对同一 event_id 重复告警。
        """
        ok = await asyncio.to_thread(
            self._l0.invalidate,
            event.house_id,
            event.fact_version,
            trace_id=trace_id,
        )
        if not ok:
            self._record_failure(
                "stale_invalidate_failed",
                trace_id=trace_id,
                house_id=event.house_id,
                fact_version=event.fact_version,
            )
            return
        tracing.record_house_sync_event(trace_id, operation="stale_alert", metadata=metadata)
        logger.error(
            "house_sync_stale_alert event_id=%s house_id=%s fact_version=%s latency_ms=%.1f",
            event.event_id,
            event.house_id,
            event.fact_version,
            cast("float", metadata["latency_ms"]),
        )
        metadata["stale"] = True
        self._refresh_latency(event, metadata)
        tracing.record_house_sync_event(trace_id, operation="consumed", metadata=metadata)
        await self._commit_safely(message, trace_id)

    @staticmethod
    def _refresh_latency(event: HouseSyncEvent, metadata: dict[str, object]) -> None:
        """按 L0 写完成时刻重算 latency_ms（occurred_at→写完成，AC 口径）。"""
        latency = datetime.now(UTC) - event.occurred_at
        metadata["latency_ms"] = latency.total_seconds() * 1000

    async def _commit_safely(self, message: Message, trace_id: str) -> None:
        """提交 offset；提交失败不崩溃——offset 未提交，重启后重放（at-least-once）。"""
        try:
            await message.commit()
        except Exception as exc:  # Kafka 提交异常族不抛出（fail-safe）
            self._record_failure(
                "offset_commit_failed",
                trace_id=trace_id,
                error_type=type(exc).__name__,
            )

    @staticmethod
    def _record_corrupted(trace_id: str, *, reason: str) -> None:
        tracing.record_house_sync_event(
            trace_id, operation="corrupted", metadata={"reason": reason}
        )
        logger.error("house_sync_corrupted reason=%s", reason)

    @staticmethod
    def _record_failure(reason: str, **metadata: object) -> None:
        # 消费循环异常无 trace_id 可挂（消息未反序列化）：传 None 时
        # record_house_sync_event 经 _recordable 收敛为 no-op，结构化日志兜底。
        trace_id = metadata.pop("trace_id", None)
        tracing.record_house_sync_event(
            cast("str | None", trace_id),
            operation="failure",
            metadata={"reason": reason, **metadata},
        )
        logger.error("house_sync_failure reason=%s metadata=%s", reason, metadata)
