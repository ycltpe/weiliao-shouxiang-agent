"""转人工单向指令发送（Story 5.2，AD-7 先指令后通知）。

编排器出口分支命中硬触发后，本模块把转交指令发到 Kafka topic
（SpringBoot 经纪人工作台消费）：payload 携带会话上下文（最近 3 轮
对话原文 + 命中条件清单，条件过滤 ``assessment_error`` 内部哨兵——
诊断词不外发）。指令只出不进：本 Story 不新增消费该 topic 的消费者。

跨进程契约（评审轮次 1 补全）：payload 恒携带 ``handoff_id``（UUID4，
单次触发唯一——编排器出口分支每次触发生成，工作台消费者据此去重）
与 ``version: int = 1``（契约版本，字段演进协商基础）；producer 发送
带 ``key=session_id``（同会话指令分区保序）与 ``acks="all"``（显式
指定，合规关键指令不依赖 broker 默认）。投递语义 = at-least-once：
``run_coroutine_threadsafe`` 超时不取消在途发送，首次尝试可能实际
送达而重试再发一条——重复指令由工作台消费者按 ``handoff_id`` 去重
（消费侧实现归工作台 Story，本服务只保证去重键随 payload 送达）。

发送语义（单向出口，fire-and-forget 但有界阻塞）：
- 发送器自持有后台事件循环线程（aiokafka producer 为 async，管道
  为同步执行），对外暴露同步 ``send_command()``
  （``run_coroutine_threadsafe(...).result(timeout)``）；
- 重试 3 次（首次发送即第 1 次，有界指数退避且睡前重算剩余预算），
  总阻塞预算 ``SEND_BUDGET_SECONDS``（远小于编排器 2.5s 内部预算，
  为 NFR-1 P99 < 3s 留余量）；预算耗尽按重试耗尽归一；
- 耗尽写 DLQ topic（独立 Kafka topic）+ ERROR 级
  ``handoff_command_alert`` 告警日志（沿用仓库惯例，无外部 paging
  系统）；DLQ 写失败仅告警日志；
- topic 与 DLQ topic 相同即构造拒绝（否则死信写进 live topic 且
  告警误导）；
- 全程 fail-safe：任何失败绝不抛出、绝不改变买家应答（出口文案恒为
  固定 ``HANDOFF_REPLY_TEXT``，绝不降级为模板兜底）；发送全过程记
  Langfuse trace（sent / failed / dlq，no-op 模式）。

payload 为跨进程契约（工作台消费），独立 Pydantic 模型定义在本模块，
不引用信封模型——管道内部结构不得外泄。
"""

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol

# aiokafka 未随包发布 py.typed（0.15.0 实测无标记），mypy strict 下按
# import-untyped 最小化豁免：本模块仅以 Any 触碰其 API，不做精细类型假设。
from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from weiliao_agent.handoff.trigger import CONDITION_ASSESSMENT_ERROR
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import tracing
from weiliao_agent.session.manager import TranscriptTurn

logger = logging.getLogger("weiliao_agent.handoff.command")

# 发送尝试次数：首次发送即第 1 次，共 3 次（spec 钉死）。
MAX_SEND_ATTEMPTS = 3
# 单次指令发送的总阻塞预算（秒）：远小于编排器 2.5s 内部预算（NFR-1
# P99 < 3s 出口余量口径），超预算按重试耗尽归一（DLQ+告警），不等
# Kafka。
SEND_BUDGET_SECONDS = 1.0
# 有界指数退避：0.05s → 0.1s（第 1/2 次失败后），封顶 0.2s；睡前重算
# 剩余预算， sleep = min(退避, 剩余预算)。
BACKOFF_BASE_SECONDS = 0.05
BACKOFF_CAP_SECONDS = 0.2
# payload 上下文轮数（Story 5.2 决策 Q1-A：原文最近 3 轮）：历史读取
# 只取最近 PAYLOAD_RECENT_TURNS-1 条，与当前信封轮拼成 3 轮。
PAYLOAD_RECENT_TURNS = 3
# payload 单字段字符上限：buyer_message / agent_reply 截断——一条超大
# 消息可能顶破 Kafka max_request_size，导致每次尝试与 DLQ 写全部失败、
# 指令整体丢失；截断后 payload 体积有界。
PAYLOAD_FIELD_CHAR_CAP = 2000


class HandoffTurn(BaseModel):
    """转交指令 payload 的单轮上下文（跨进程契约，独立定义）。"""

    turn: int | None = None
    buyer_message: str | None = None
    agent_reply: str | None = None
    timestamp: datetime | None = None


class HandoffCommand(BaseModel):
    """转人工转交指令 payload（跨进程契约，SpringBoot 工作台消费）。

    独立 Pydantic 模型，不引用信封模型；``conditions`` 已过滤
    ``assessment_error`` 内部哨兵（诊断词不外发）。``handoff_id``
    （UUID4，单次触发唯一）为工作台去重键；``version`` 为契约版本。
    """

    handoff_id: str
    version: int = 1
    session_id: str
    house_id: str
    trace_id: str | None = None
    triggered_at: datetime
    conditions: list[str] = Field(default_factory=list)
    recent_turns: list[HandoffTurn] = Field(default_factory=list)


class HandoffTransport(Protocol):
    """指令传输抽象：生产实现为 aiokafka；测试注入内存替身。

    ``send`` 为同步阻塞调用（内部经 run_coroutine_threadsafe 桥接
    后台事件循环），超时/失败一律抛异常（由发送器重试归一）。
    """

    def start(self) -> None:
        """建立 producer（不可达时抛错，由组合根禁用发送器）。"""

    def send(self, *, topic: str, value: bytes, key: str, timeout: float) -> None:
        """单条发送（key=session_id 分区保序）；``timeout`` 为本次
        调用的阻塞上限（秒）。key 经传输层序列化为 utf-8 bytes——
        aiokafka 无 key_serializer 时 record builder 只接受 bytes
        （Epic 5 code review 补丁：str key 会使真实发送 100% 抛
        TypeError）。"""

    def close(self) -> None:
        """关闭 producer（幂等；未 start 时 no-op）。"""


class _KafkaTransport:
    """aiokafka producer 包装：独立后台事件循环线程 + 同步 send。

    producer 的创建与 start/stop 均在该后台线程的事件循环内完成
    （aiokafka 绑定创建时所在 loop）；对外 ``send`` 经
    ``run_coroutine_threadsafe(...).result(timeout)`` 桥接，与编排器
    现有同步语义零侵入。producer 显式 ``acks="all"``（合规关键指令
    不依赖 broker 默认）。两个启动失败分支（后台线程 ready 超时 /
    producer start 失败）均先清理再抛，不悬挂线程。
    """

    def __init__(
        self,
        *,
        brokers: str,
        start_timeout_seconds: float = 5.0,
        close_timeout_seconds: float = 5.0,
        producer_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._brokers = brokers
        self._start_timeout_seconds = start_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        # producer 工厂可注入（测试替身零 broker 验证生命周期）；
        # 缺省生产实现显式 acks="all"。
        self._producer_factory = producer_factory or (
            lambda: AIOKafkaProducer(bootstrap_servers=self._brokers, acks="all")
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._producer: Any = None

    def start(self) -> None:
        """启动后台事件循环线程并在其中创建 + start producer。"""
        if self._loop is not None:
            return
        ready = threading.Event()

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run_loop,
            name="handoff-command-producer",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        if not ready.wait(timeout=self._start_timeout_seconds):
            # 评审轮次 1 补丁：ready 超时分支与 producer 失败分支清理
            # 对称——先回收线程再抛，不悬挂。
            self.close()
            msg = "handoff command producer event loop did not start"
            raise RuntimeError(msg)
        loop = self._loop
        assert loop is not None  # ready 已置位，供 mypy 收窄
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._create_and_start_producer(), loop
            )
            self._producer = future.result(timeout=self._start_timeout_seconds)
        except Exception:
            self.close()  # 半成品状态回收：停线程，不悬挂
            raise

    async def _create_and_start_producer(self) -> Any:
        # aiokafka 无类型标记（import-untyped），producer 以 Any 承接：
        # 构造与 start 均绑定后台线程事件循环。
        producer = self._producer_factory()
        await producer.start()
        return producer

    def send(self, *, topic: str, value: bytes, key: str, timeout: float) -> None:
        loop = self._loop
        producer = self._producer
        if loop is None or producer is None:
            msg = "handoff command transport is not started"
            raise RuntimeError(msg)
        future = asyncio.run_coroutine_threadsafe(
            producer.send_and_wait(topic, value=value, key=key.encode("utf-8")), loop
        )
        future.result(timeout=timeout)

    def close(self) -> None:
        """关闭 producer、停后台循环、回收线程（全程幂等，绝不抛出）。

        线程 join 超时仍未退出仅告警（跨线程 close 运行中 loop 会抛），
        loop 对象留待进程退出回收；线程正常退出后 close loop。
        """
        loop = self._loop
        self._loop = None
        producer = self._producer
        self._producer = None
        if loop is not None:
            if producer is not None:
                future = asyncio.run_coroutine_threadsafe(producer.stop(), loop)
                with suppress(Exception):  # 关停路径绝不抛出（fail-safe）
                    future.result(timeout=self._close_timeout_seconds)
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        thread.join(timeout=self._close_timeout_seconds)
        if thread.is_alive():
            logger.warning(
                "handoff command producer thread did not exit within %.1fs; "
                "event loop left open",
                self._close_timeout_seconds,
            )
            return
        if loop is not None:
            with suppress(Exception):  # loop.close 残留回调异常不抛出
                loop.close()


class HandoffCommandSender:
    """转交指令发送器：payload 构建 + 有界重试发送 + DLQ 归一。

    构造参数即全部发送配置；实例仅持有 transport 引用与开关位，
    线程安全（发送状态全在单次调用内）。发送 fail-safe 口径：
    ``send_command`` 绝不抛出——重试耗尽归一 DLQ + ERROR 告警日志，
    调用方（编排器出口分支）的买家应答语义绝不因发送失败改变。
    """

    def __init__(
        self,
        *,
        topic: str,
        dlq_topic: str,
        transport: HandoffTransport,
        transcript_reader: Callable[[str, int], list[TranscriptTurn]],
        budget_seconds: float = SEND_BUDGET_SECONDS,
        max_attempts: int = MAX_SEND_ATTEMPTS,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = BACKOFF_CAP_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not topic:
            msg = "handoff command topic must be non-empty"
            raise ValueError(msg)
        if not dlq_topic:
            msg = "handoff command DLQ topic must be non-empty"
            raise ValueError(msg)
        if topic == dlq_topic:
            # 评审轮次 1 补丁：topic 与 DLQ topic 相同会让死信写进
            # live topic 且告警误导——配置错误构造即拒。
            msg = (
                f"handoff command DLQ topic must differ from the live "
                f"topic, both are {topic!r}"
            )
            raise ValueError(msg)
        if max_attempts <= 0:
            msg = f"max_attempts must be a positive integer, got {max_attempts}"
            raise ValueError(msg)
        if budget_seconds <= 0:
            msg = f"budget_seconds must be positive, got {budget_seconds}"
            raise ValueError(msg)
        if backoff_base_seconds < 0 or backoff_cap_seconds < 0:
            # 负退避会让 time.sleep(negative) 从 send_command 抛出，
            # 违反其绝不抛出契约——构造即拒（同 sibling 参数口径）。
            msg = (
                f"backoff seconds must be non-negative, got "
                f"base={backoff_base_seconds} cap={backoff_cap_seconds}"
            )
            raise ValueError(msg)
        self._topic = topic
        self._dlq_topic = dlq_topic
        self._transport = transport
        self._transcript_reader = transcript_reader
        self._budget_seconds = budget_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_cap_seconds = backoff_cap_seconds
        self._sleeper = sleeper
        self._clock = clock
        self._disabled = False

    @classmethod
    def kafka(
        cls,
        *,
        brokers: str,
        topic: str,
        dlq_topic: str,
        transcript_reader: Callable[[str, int], list[TranscriptTurn]],
    ) -> "HandoffCommandSender":
        """组合根入口：aiokafka transport（装配期零网络触达，start 归 lifespan）。"""
        return cls(
            topic=topic,
            dlq_topic=dlq_topic,
            transport=_KafkaTransport(brokers=brokers),
            transcript_reader=transcript_reader,
        )

    @property
    def topic(self) -> str:
        """指令 topic（只读，组合根 env 回读验证用）。"""
        return self._topic

    @property
    def dlq_topic(self) -> str:
        """DLQ topic（只读，组合根 env 回读验证用）。"""
        return self._dlq_topic

    def start(self) -> None:
        """启动 transport（组合根 lifespan 调用；失败抛错由调用方禁用）。"""
        self._transport.start()

    def close(self) -> None:
        """关闭 transport（幂等，绝不抛出）。"""
        with suppress(Exception):  # 关停路径绝不抛出（fail-safe）
            self._transport.close()

    def disable(self) -> None:
        """禁用发送器（producer 启动失败等场景）：send_command 恒 no-op。"""
        self._disabled = True

    def send_command(self, envelope: Envelope, *, reply: str) -> None:
        """构建并发送转交指令（AD-7：先于买家通知返回，有界阻塞）。

        严格同步：本方法返回即「发送完成或重试耗尽归一」，编排器随后
        才返回安抚文案。任何失败在本方法内消化（重试 → DLQ → 告警
        日志），绝不向上抛出，绝不改变买家应答。
        """
        command, transcript_degraded = self._build_command(envelope, reply=reply)
        if self._disabled:
            # producer 启动失败禁用后，红线转交不得静默消失：每次跳过
            # 逐条 WARNING 留痕（session_id + handoff_id，可检索可计数）。
            logger.warning(
                "handoff command skipped: sender disabled session_id=%s "
                "handoff_id=%s",
                envelope.session_id,
                command.handoff_id,
            )
            return
        payload = command.model_dump_json().encode("utf-8")
        started_at = self._clock()
        attempts = 0
        last_error: Exception | None = None
        while attempts < self._max_attempts:
            remaining = self._budget_seconds - (self._clock() - started_at)
            if remaining <= 0:
                # 阻塞预算耗尽：按重试耗尽归一（DLQ+告警），不等 Kafka。
                break
            attempts += 1
            try:
                self._transport.send(
                    topic=self._topic,
                    value=payload,
                    key=envelope.session_id,
                    timeout=remaining,
                )
            except Exception as exc:  # fail-safe：单次失败归一为重试
                last_error = exc
                if attempts < self._max_attempts:
                    self._sleep_before_retry(attempts, started_at)
                continue
            logger.info(
                "handoff command sent session_id=%s handoff_id=%s topic=%s "
                "attempts=%d",
                envelope.session_id,
                command.handoff_id,
                self._topic,
                attempts,
            )
            tracing.record_handoff_command(
                envelope.trace_id,
                operation="sent",
                attempts=attempts,
                metadata={
                    "topic": self._topic,
                    "handoff_id": command.handoff_id,
                    "conditions": list(command.conditions),
                    "context_turns": len(command.recent_turns),
                    "transcript_degraded": transcript_degraded,
                },
            )
            return
        self._exhausted(
            envelope,
            payload,
            handoff_id=command.handoff_id,
            attempts=attempts,
            started_at=started_at,
            transcript_degraded=transcript_degraded,
            last_error=last_error,
        )

    def _build_command(
        self, envelope: Envelope, *, reply: str
    ) -> tuple[HandoffCommand, bool]:
        """构建 payload：条件过滤哨兵 + 上下文拼轮（历史 + 当前轮）。

        ``handoff_id`` 每次触发生成（UUID4，单次触发唯一——工作台去重
        键）。返回 ``(command, transcript_degraded)``：历史转写读取
        失败时上下文降级为空 + 当前轮（指令照发），降级标记供 trace 记录。
        """
        conditions = [
            condition
            for condition in (
                envelope.handoff.conditions if envelope.handoff is not None else []
            )
            if condition != CONDITION_ASSESSMENT_ERROR
        ]
        now = datetime.now(UTC)
        history, transcript_degraded = self._read_history(envelope.session_id)
        current = HandoffTurn(
            turn=envelope.turn,
            buyer_message=self._cap_field(envelope.message),
            agent_reply=self._cap_field(reply),
            timestamp=now,
        )
        recent_turns = (history + [current])[-PAYLOAD_RECENT_TURNS:]
        return (
            HandoffCommand(
                handoff_id=str(uuid.uuid4()),
                session_id=envelope.session_id,
                house_id=envelope.house_id,
                trace_id=envelope.trace_id,
                triggered_at=now,
                conditions=conditions,
                recent_turns=recent_turns,
            ),
            transcript_degraded,
        )

    def _read_history(self, session_id: str) -> tuple[list[HandoffTurn], bool]:
        """读最近 ``PAYLOAD_RECENT_TURNS - 1`` 条历史转写；Redis 读失败
        降级为空清单 + True（调用方记 trace，指令照发）。"""
        try:
            turns = self._transcript_reader(session_id, PAYLOAD_RECENT_TURNS - 1)
        except Exception:
            return [], True
        history = [
            HandoffTurn(
                turn=turn.turn,
                buyer_message=self._cap_field(turn.buyer_message),
                agent_reply=self._cap_field(turn.agent_reply),
                timestamp=turn.timestamp,
            )
            for turn in turns
        ]
        return history, False

    @staticmethod
    def _cap_field(value: str | None) -> str | None:
        """payload 文本字段截断至 PAYLOAD_FIELD_CHAR_CAP：超大消息不得
        顶破 Kafka max_request_size（否则逐次尝试与 DLQ 全败、指令丢失）。"""
        if value is None:
            return None
        return value[:PAYLOAD_FIELD_CHAR_CAP]

    def _sleep_before_retry(self, attempts: int, started_at: float) -> None:
        """有界指数退避，睡前重算剩余预算（评审轮次 1 补丁：不得用发送
        前陈旧的剩余预算，否则总阻塞可超预算上限）。"""
        remaining = self._budget_seconds - (self._clock() - started_at)
        if remaining <= 0:
            return  # 预算已耗尽：不睡，循环头部归一退出
        backoff = min(
            self._backoff_base_seconds * (2 ** (attempts - 1)),
            self._backoff_cap_seconds,
        )
        self._sleeper(min(backoff, remaining))

    def _exhausted(
        self,
        envelope: Envelope,
        payload: bytes,
        *,
        handoff_id: str,
        attempts: int,
        started_at: float,
        transcript_degraded: bool,
        last_error: Exception | None,
    ) -> None:
        """重试耗尽归一：failed trace 事件 → 写 DLQ（预算内尽力）→
        ERROR 级告警日志。DLQ 写失败仅告警日志，回复不变。"""
        error_name = (
            type(last_error).__name__ if last_error is not None else "budget_exhausted"
        )
        tracing.record_handoff_command(
            envelope.trace_id,
            operation="failed",
            attempts=attempts,
            metadata={
                "topic": self._topic,
                "handoff_id": handoff_id,
                "error": error_name,
                "transcript_degraded": transcript_degraded,
            },
        )
        dlq_written = False
        remaining = self._budget_seconds - (self._clock() - started_at)
        if remaining > 0:
            try:
                self._transport.send(
                    topic=self._dlq_topic,
                    value=payload,
                    key=envelope.session_id,
                    timeout=remaining,
                )
                dlq_written = True
            except Exception:  # DLQ 写失败仅告警日志（fail-safe）
                dlq_written = False
        if dlq_written:
            tracing.record_handoff_command(
                envelope.trace_id,
                operation="dlq",
                attempts=attempts,
                metadata={"dlq_topic": self._dlq_topic, "handoff_id": handoff_id},
            )
        logger.error(
            "handoff_command_alert session_id=%s trace_id=%s handoff_id=%s "
            "topic=%s attempts=%d error=%s dlq_topic=%s dlq_written=%s",
            envelope.session_id,
            envelope.trace_id,
            handoff_id,
            self._topic,
            attempts,
            error_name,
            self._dlq_topic,
            dlq_written,
        )
