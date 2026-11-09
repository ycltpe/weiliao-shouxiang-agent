"""转人工单向指令发送器 I/O 矩阵单测（Story 5.2，AD-7 先指令后通知）。

覆盖 spec I/O & Edge-Case Matrix 与评审轮次 1 补丁：正常触发送达
（handoff_id/version=1 契约、key=session_id、条件过滤哨兵）/ 瞬时
故障退避重试恢复（睡前重算剩余预算）/ 重试 3 次全败写 DLQ + ERROR
告警 / DLQ 写失败仅告警 / 无历史转写仅当前轮 / Redis 读失败降级照发
+ trace / 阻塞预算耗尽按重试耗尽归一 / producer 未配置跳过（编排器
None 注入）/ topic == DLQ topic 构造拒绝；trace 事件（sent / failed /
dlq）经共享 fake_langfuse 夹具断言。
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import redis

from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX
from weiliao_agent.handoff import command as command_module
from weiliao_agent.handoff.command import (
    PAYLOAD_FIELD_CHAR_CAP,
    PAYLOAD_RECENT_TURNS,
    HandoffCommand,
    HandoffCommandSender,
    HandoffTransport,
)
from weiliao_agent.models.envelope import Envelope, HandoffAssessment
from weiliao_agent.pipeline import HANDOFF_REPLY_TEXT, PipelineOrchestrator
from weiliao_agent.session import TranscriptTurn

TS = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
TRACE_ID = "a" * 32  # 32-hex：Langfuse trace id 口径，可挂事件
TOPIC = "handoff.commands"
DLQ_TOPIC = "handoff.commands.dlq"


class _FakeTransport:
    """HandoffTransport 替身：按 topic 记录发送（含 key），可预置失败次数。"""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.sent: list[tuple[str, bytes, str, float]] = []
        self._failures = dict(failures or {})
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def send(self, *, topic: str, value: bytes, key: str, timeout: float) -> None:
        if self._failures.get(topic, 0) > 0:
            self._failures[topic] -= 1
            raise RuntimeError("kafka boom")
        self.sent.append((topic, value, key, timeout))

    def close(self) -> None:
        self.closed = True


class _NoTranscript:
    """转写读取替身：固定返回空历史。"""

    def __call__(self, session_id: str, limit: int) -> list[TranscriptTurn]:
        return []


_NO_TRANSCRIPT = _NoTranscript()  # 模块级单例：B008（默认参数禁止调用）


def _sender(
    transport: HandoffTransport,
    *,
    transcript_reader: Any = _NO_TRANSCRIPT,
    sleeper: Any = lambda seconds: None,
    clock: Any = None,
) -> HandoffCommandSender:
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return HandoffCommandSender(
        topic=TOPIC,
        dlq_topic=DLQ_TOPIC,
        transport=transport,
        transcript_reader=transcript_reader,
        sleeper=sleeper,
        **kwargs,
    )


def _envelope(**updates: Any) -> Envelope:
    base: dict[str, Any] = {
        "session_id": "sess-001",
        "house_id": "house-9",
        "timestamp": TS,
        "trace_id": TRACE_ID,
        "turn": 3,
        "message": "你们这是骗子",
        "handoff": {
            "triggered": True,
            "conditions": ["骗子", "assessment_error"],
        },
    }
    base.update(updates)
    return Envelope.model_validate(base)


def _payload(transport: _FakeTransport, topic: str = TOPIC) -> HandoffCommand:
    ((sent_topic, value, _, _),) = [
        entry for entry in transport.sent if entry[0] == topic
    ]
    assert sent_topic == topic
    return HandoffCommand.model_validate(json.loads(value.decode("utf-8")))


def _events(fake_langfuse: Any, name: str) -> list[dict[str, Any]]:
    return [event for event in fake_langfuse.events if event["name"] == name]


def test_send_success_delivers_command_with_current_turn(
    fake_langfuse: Any,
) -> None:
    transport = _FakeTransport()
    sender = _sender(transport)

    sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    assert len(transport.sent) == 1
    (sent_topic, _, key, _) = transport.sent[0]
    assert sent_topic == TOPIC
    assert key == "sess-001"  # message key=session_id：同会话分区保序
    command = _payload(transport)
    # 跨进程契约（评审轮次 1 补全）：handoff_id（UUID4）+ version=1。
    uuid.UUID(command.handoff_id)  # 合法 UUID4 字符串（非法即抛）
    assert command.version == 1
    assert command.session_id == "sess-001"
    assert command.house_id == "house-9"
    assert command.trace_id == TRACE_ID
    # assessment_error 内部哨兵过滤：诊断词不外发。
    assert command.conditions == ["骗子"]
    # 无历史转写：上下文仅当前触发轮（买家原文 + 固定出口文案）。
    assert len(command.recent_turns) == 1
    current = command.recent_turns[0]
    assert current.turn == 3
    assert current.buyer_message == "你们这是骗子"
    assert current.agent_reply == HANDOFF_REPLY_TEXT
    assert current.timestamp is not None
    (sent,) = _events(fake_langfuse, "handoff.command.sent")
    assert sent["level"] == "DEFAULT"
    assert sent["metadata"]["attempts"] == 1
    assert sent["metadata"]["metadata"]["handoff_id"] == command.handoff_id
    assert sent["metadata"]["metadata"]["transcript_degraded"] is False


def test_handoff_id_unique_per_trigger(fake_langfuse: Any) -> None:
    transport = _FakeTransport()
    sender = _sender(transport)

    sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)
    sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    commands = [
        HandoffCommand.model_validate(json.loads(entry[1].decode("utf-8")))
        for entry in transport.sent
    ]
    first, second = commands
    assert first.handoff_id != second.handoff_id


def test_payload_model_independent_contract() -> None:
    # 跨进程契约：字段集固定，不随信封模型漂移。
    command = HandoffCommand(
        handoff_id=str(uuid.uuid4()),
        session_id="s",
        house_id="h",
        trace_id=TRACE_ID,
        triggered_at=TS,
        conditions=["redline_intent"],
        recent_turns=[],
    )
    dumped = json.loads(command.model_dump_json())
    assert set(dumped) == {
        "handoff_id",
        "version",
        "session_id",
        "house_id",
        "trace_id",
        "triggered_at",
        "conditions",
        "recent_turns",
    }


def test_transient_failure_recovers_with_bounded_backoff(
    fake_langfuse: Any,
) -> None:
    transport = _FakeTransport(failures={TOPIC: 1})
    slept: list[float] = []
    sender = _sender(transport, sleeper=slept.append)

    sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    assert len(transport.sent) == 1  # 第 2 次尝试送达
    assert len(slept) == 1  # 一次有界退避
    assert 0 < slept[0] <= 0.2
    (sent,) = _events(fake_langfuse, "handoff.command.sent")
    assert sent["metadata"]["attempts"] == 2  # 重试计数写 trace
    assert _events(fake_langfuse, "handoff.command.failed") == []


def test_backoff_recomputes_remaining_budget_before_sleeping(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """评审轮次 1 补丁：退避睡前重算剩余预算——第 1 次发送耗掉 0.95s
    后，第 2 次退避必须按重算后的剩余预算（0.01s）封顶，而非发送前
    的陈旧值，否则总阻塞可超 SEND_BUDGET_SECONDS 上限。"""

    class _ManualClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = _ManualClock()
    sleeps: list[float] = []
    calls = {"n": 0}

    def slow_failing_send(*, topic: str, value: bytes, key: str, timeout: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            clock.now = 0.95  # 第 1 次发送耗掉预算的 95%
        elif calls["n"] == 2:
            clock.now = 0.99
        raise RuntimeError("kafka boom")

    class _SlowTransport(_FakeTransport):
        def send(
            self, *, topic: str, value: bytes, key: str, timeout: float
        ) -> None:
            if topic == TOPIC:
                slow_failing_send(topic=topic, value=value, key=key, timeout=timeout)
                return
            super().send(topic=topic, value=value, key=key, timeout=timeout)

    transport = _SlowTransport()
    sender = _sender(transport, sleeper=sleeps.append, clock=clock)

    with caplog.at_level(logging.ERROR):
        sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    # 第 2 次退避 0.01s（重算后剩余预算）而非陈旧值 0.04s；若用陈旧
    # 预算，0.99+0.04 > 1.0 即超总预算上限。
    assert sleeps == pytest.approx([0.05, 0.01])
    # 重试耗尽归一：DLQ 写入 + ERROR 告警（预算在 _exhausted 时仍剩 0.01s）。
    assert [entry[0] for entry in transport.sent] == [DLQ_TOPIC]
    assert any(
        "handoff_command_alert" in record.message for record in caplog.records
    )


def test_retry_exhausted_writes_dlq_and_alerts(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    transport = _FakeTransport(failures={TOPIC: 3})
    sender = _sender(transport)

    with caplog.at_level(logging.ERROR):
        sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    # 3 次尝试全败 → DLQ topic 写入同一份 payload（key 一致）。
    assert len(transport.sent) == 1
    (dlq_topic, _, key, _) = transport.sent[0]
    assert dlq_topic == DLQ_TOPIC
    assert key == "sess-001"
    command = _payload(transport, DLQ_TOPIC)
    assert command.session_id == "sess-001"
    uuid.UUID(command.handoff_id)
    # ERROR 级告警日志（仓库惯例 event=<name>_alert）。
    alerts = [
        record for record in caplog.records if "handoff_command_alert" in record.message
    ]
    assert len(alerts) == 1
    assert alerts[0].levelno == logging.ERROR
    # trace：failed + dlq 两事件。
    (failed,) = _events(fake_langfuse, "handoff.command.failed")
    assert failed["level"] == "ERROR"
    assert failed["metadata"]["attempts"] == 3
    assert failed["metadata"]["metadata"]["error"] == "RuntimeError"
    (dlq,) = _events(fake_langfuse, "handoff.command.dlq")
    assert dlq["level"] == "ERROR"
    assert dlq["metadata"]["metadata"]["dlq_topic"] == DLQ_TOPIC


def test_dlq_write_failure_only_alerts(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    transport = _FakeTransport(failures={TOPIC: 3, DLQ_TOPIC: 1})
    sender = _sender(transport)

    with caplog.at_level(logging.ERROR):
        sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)  # 绝不抛出

    assert len(transport.sent) == 0  # 主 topic 与 DLQ 均未送达
    alerts = [
        record for record in caplog.records if "handoff_command_alert" in record.message
    ]
    assert len(alerts) == 1
    assert alerts[0].levelno == logging.ERROR
    assert "dlq_written=False" in alerts[0].message
    # failed 事件照记；dlq 事件仅在写入成功时记。
    assert len(_events(fake_langfuse, "handoff.command.failed")) == 1
    assert _events(fake_langfuse, "handoff.command.dlq") == []


def test_budget_exhaustion_treated_as_retry_exhausted(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # 时钟每次调用跳 10s：首次预算检查即耗尽——按重试耗尽归一（DLQ+告警），
    # 不拖垮编排器 deadline，主 topic 一次都不尝试。
    ticks = iter(range(0, 100, 10))
    transport = _FakeTransport()
    sender = _sender(transport, clock=lambda: float(next(ticks)))

    with caplog.at_level(logging.ERROR):
        sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    assert transport.sent == []
    alerts = [
        record for record in caplog.records if "handoff_command_alert" in record.message
    ]
    assert len(alerts) == 1
    (failed,) = _events(fake_langfuse, "handoff.command.failed")
    assert failed["metadata"]["metadata"]["error"] == "budget_exhausted"


def test_transcript_read_failure_degrades_to_current_turn(
    fake_langfuse: Any,
) -> None:
    def broken_reader(session_id: str, limit: int) -> list[TranscriptTurn]:
        raise redis.RedisError("redis down")

    transport = _FakeTransport()
    sender = _sender(transport, transcript_reader=broken_reader)

    sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)  # 指令照发

    command = _payload(transport)
    assert len(command.recent_turns) == 1  # 空 + 当前轮
    (sent,) = _events(fake_langfuse, "handoff.command.sent")
    assert sent["metadata"]["metadata"]["transcript_degraded"] is True


def test_history_prepended_and_capped_at_three_turns(
    fake_langfuse: Any,
) -> None:
    def reader(session_id: str, limit: int) -> list[TranscriptTurn]:
        assert limit == PAYLOAD_RECENT_TURNS - 1  # 历史只读 2 条
        return [
            TranscriptTurn(1, "msg-1", "reply-1", TS),
            TranscriptTurn(2, "msg-2", "reply-2", TS),
            TranscriptTurn(99, "msg-99", "reply-99", TS),  # 超出读取窗口的历史
        ]

    transport = _FakeTransport()
    sender = _sender(transport, transcript_reader=reader)

    sender.send_command(_envelope(turn=3), reply=HANDOFF_REPLY_TEXT)

    command = _payload(transport)
    assert [turn.turn for turn in command.recent_turns] == [2, 99, 3]
    assert command.recent_turns[-1].buyer_message == "你们这是骗子"
    assert command.recent_turns[-1].agent_reply == HANDOFF_REPLY_TEXT


def test_disabled_sender_skips_with_per_hit_warning(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    transport = _FakeTransport()
    sender = _sender(transport)
    sender.disable()

    with caplog.at_level(logging.WARNING):
        sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)
        sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    assert transport.sent == []
    assert fake_langfuse.events == []
    # 禁用后红线转交不得静默消失：每次跳过逐条 WARNING（session_id +
    # handoff_id，可检索可计数）。
    warnings = [
        record for record in caplog.records if "sender disabled" in record.message
    ]
    assert len(warnings) == 2
    assert warnings[0].levelno == logging.WARNING
    assert "sess-001" in warnings[0].message
    assert "handoff_id=" in warnings[0].message


def test_successful_send_logs_info_with_handoff_id(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # Langfuse 未配置时事件 no-op——本地 INFO 日志是「指令确实发出过」
    # 的独立证据。
    transport = _FakeTransport()
    sender = _sender(transport)

    with caplog.at_level(logging.INFO):
        sender.send_command(_envelope(), reply=HANDOFF_REPLY_TEXT)

    infos = [record for record in caplog.records if "handoff command sent" in record.message]
    assert len(infos) == 1
    assert infos[0].levelno == logging.INFO
    assert "sess-001" in infos[0].message
    assert TOPIC in infos[0].message
    assert _payload(transport).handoff_id in infos[0].message


def test_oversized_message_fields_truncated_and_delivered(
    fake_langfuse: Any,
) -> None:
    # 超大 buyer_message/agent_reply 截断至 PAYLOAD_FIELD_CHAR_CAP：
    # payload 体积有界，送达不受影响（不截断会顶破 max_request_size，
    # 逐次尝试与 DLQ 全败、指令丢失）。
    transport = _FakeTransport()
    sender = _sender(transport)
    huge = "巨" * 5000

    sender.send_command(_envelope(message=huge), reply="回" * 5000)

    assert len(transport.sent) == 1
    command = _payload(transport)
    current = command.recent_turns[0]
    assert len(current.buyer_message) == PAYLOAD_FIELD_CHAR_CAP
    assert current.buyer_message == "巨" * PAYLOAD_FIELD_CHAR_CAP
    assert len(current.agent_reply) == PAYLOAD_FIELD_CHAR_CAP
    assert current.agent_reply == "回" * PAYLOAD_FIELD_CHAR_CAP


def test_sender_start_and_close_delegate_to_transport() -> None:
    # 接线守护：sender.start()/close() 必须真的触达 transport——
    # start() 变 no-op 时全套件仍绿而生产发送全丢。
    transport = _FakeTransport()
    sender = _sender(transport)

    assert transport.started is False
    sender.start()
    assert transport.started is True
    sender.close()
    assert transport.closed is True


def test_constructor_rejects_empty_or_colliding_topics() -> None:
    transport = _FakeTransport()
    with pytest.raises(ValueError, match="topic"):
        HandoffCommandSender(
            topic="",
            dlq_topic=DLQ_TOPIC,
            transport=transport,
            transcript_reader=_NO_TRANSCRIPT,
        )
    with pytest.raises(ValueError, match="DLQ"):
        HandoffCommandSender(
            topic=TOPIC,
            dlq_topic="",
            transport=transport,
            transcript_reader=_NO_TRANSCRIPT,
        )
    # 评审轮次 1 补丁：topic == dlq_topic 构造拒绝（死信写进 live topic
    # 且告警误导）。
    with pytest.raises(ValueError, match="differ"):
        HandoffCommandSender(
            topic="same.topic",
            dlq_topic="same.topic",
            transport=transport,
            transcript_reader=_NO_TRANSCRIPT,
        )


def test_constructor_rejects_negative_backoff() -> None:
    # 负退避会让 time.sleep(negative) 从 send_command 抛出，违反其
    # 绝不抛出契约——构造即拒（同 sibling 参数口径）。
    transport = _FakeTransport()
    with pytest.raises(ValueError, match="backoff"):
        HandoffCommandSender(
            topic=TOPIC,
            dlq_topic=DLQ_TOPIC,
            transport=transport,
            transcript_reader=_NO_TRANSCRIPT,
            backoff_base_seconds=-0.1,
        )
    with pytest.raises(ValueError, match="backoff"):
        HandoffCommandSender(
            topic=TOPIC,
            dlq_topic=DLQ_TOPIC,
            transport=transport,
            transcript_reader=_NO_TRANSCRIPT,
            backoff_cap_seconds=-0.1,
        )


# ---- 编排器出口分支集成（AD-7 严格先指令后通知；producer 未配置跳过） ----


class _HandoffFilter(Filter):
    """首跳即挂硬触发判定结果的信封透传过滤器。"""

    def __init__(self) -> None:
        self.name = "intent_router"

    def process(self, envelope: Envelope) -> Envelope:
        return envelope.model_copy(
            update={
                "handoff": HandoffAssessment(
                    triggered=True, conditions=["redline_intent"]
                )
            }
        )


class _RecordingSender:
    """HandoffCommandSender 协议替身：记录调用，可预置抛错。"""

    def __init__(self, *, raise_on_send: bool = False) -> None:
        self.calls: list[tuple[Envelope, str]] = []
        self._raise = raise_on_send

    def send_command(self, envelope: Envelope, *, reply: str) -> None:
        if self._raise:
            raise RuntimeError("sender boom")
        self.calls.append((envelope, reply))


def test_orchestrator_sends_command_before_reply(fake_langfuse: Any) -> None:
    sender = _RecordingSender()
    orchestrator = PipelineOrchestrator(
        filters=[_HandoffFilter()], handoff_sender=sender
    )

    result = orchestrator.run(_envelope())

    # 严格先指令后通知：send_command 同步返回后应答才出管道。
    assert len(sender.calls) == 1
    (envelope, reply) = sender.calls[0]
    assert envelope.session_id == "sess-001"
    assert reply == HANDOFF_REPLY_TEXT
    # 出口文案语义不变：固定文案、非降级、绝不兜底。
    # 出口文案语义不变（5.4 决策 Q1-B 携标）：固定文案 + 前缀，非降级。
    assert HANDOFF_REPLY_TEXT == "已为您转接经纪人，请稍候"
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert result.degraded is False
    assert result.failed_filter is None


def test_orchestrator_sender_failure_never_changes_reply(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    orchestrator = PipelineOrchestrator(
        filters=[_HandoffFilter()],
        handoff_sender=_RecordingSender(raise_on_send=True),
    )

    with caplog.at_level(logging.WARNING):
        result = orchestrator.run(_envelope())

    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert result.degraded is False  # 发送失败不改变买家应答，非降级
    assert result.failed_filter is None


def test_orchestrator_without_sender_skips_send(fake_langfuse: Any) -> None:
    orchestrator = PipelineOrchestrator(filters=[_HandoffFilter()])

    result = orchestrator.run(_envelope())

    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert result.degraded is False


# ---- _KafkaTransport 生命周期（零 broker，producer 工厂注入替身） ----


class _FakeProducer:
    """aiokafka producer 替身：start/stop/send_and_wait 记录调用。"""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started = False
        self.stopped = False
        self.sent: list[tuple[str, bytes, str]] = []

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("connect boom")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(self, topic: str, value: bytes, key: str) -> object:
        self.sent.append((topic, value, key))
        return object()


def test_transport_lifecycle_start_send_close_without_broker() -> None:
    producer = _FakeProducer()
    transport = command_module._KafkaTransport(
        brokers="localhost:9092",
        producer_factory=lambda: producer,
    )

    transport.start()
    assert producer.started
    thread = transport._thread
    assert thread is not None and thread.is_alive()

    transport.send(topic=TOPIC, value=b"{}", key="sess-001", timeout=1.0)
    assert producer.sent == [(TOPIC, b"{}", "sess-001")]

    transport.close()
    assert producer.stopped
    assert transport._thread is None
    assert transport._loop is None
    transport.close()  # 幂等：二次关闭 no-op 不抛


def test_transport_send_before_start_raises() -> None:
    transport = command_module._KafkaTransport(
        brokers="localhost:9092",
        producer_factory=lambda: _FakeProducer(),
    )
    with pytest.raises(RuntimeError, match="not started"):
        transport.send(topic=TOPIC, value=b"{}", key="k", timeout=1.0)
    transport.close()  # 未 start 关闭安全


def test_transport_start_failure_cleans_up_thread() -> None:
    transport = command_module._KafkaTransport(
        brokers="localhost:9092",
        producer_factory=lambda: _FakeProducer(fail_start=True),
    )
    with pytest.raises(RuntimeError, match="connect boom"):
        transport.start()
    # 失败分支清理：线程回收、内部引用清空，不悬挂。
    assert transport._thread is None
    assert transport._loop is None
    assert transport._producer is None
    transport.close()  # 幂等


def test_transport_ready_timeout_cleans_up_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """评审轮次 1 补丁：ready 超时分支与 producer 失败分支清理对称——
    raise 前先回收线程。线程替身不运行 target，ready 永不置位。"""

    class _NoopThread:
        def __init__(self, target: Any = None, name: Any = None, daemon: Any = None):
            self._target = target

        def start(self) -> None:
            pass  # 不运行 target：事件循环永不启动

        def join(self, timeout: float | None = None) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(command_module.threading, "Thread", _NoopThread)
    transport = command_module._KafkaTransport(
        brokers="localhost:9092",
        start_timeout_seconds=0.05,
        producer_factory=lambda: _FakeProducer(),
    )
    with pytest.raises(RuntimeError, match="did not start"):
        transport.start()
    assert transport._thread is None
    assert transport._loop is None
