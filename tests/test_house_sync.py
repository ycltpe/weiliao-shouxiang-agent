"""``house.sync.trigger`` 消费者 I/O 矩阵单测（Story 3.2）。

覆盖 spec I/O & Edge-Case Matrix 全场景：upsert/invalidate 正常路径
（含 offset 提交/不提交断言）、超期转失效 + 告警、毒消息跳过并提交、
乱序重放守卫拒写、Redis 故障不提交、Kafka 断连指数退避重连、提交失败
fail-safe、lifespan 启动门控。消息源为内存队列替身（不连真 Kafka），
L0 为 fakeredis 直注，观测为 FakeLangfuse 替身——AC 可验证。
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import fakeredis
import pytest
from aiokafka.errors import KafkaConnectionError
from conftest import _FakeLangfuse
from fastapi.testclient import TestClient

import weiliao_agent.main as main_module
import weiliao_agent.workers.house_sync as house_sync_module
from weiliao_agent.cache import L0CacheClient
from weiliao_agent.models import FactCard
from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.workers import HouseSyncConsumer, KafkaMessageSource
from weiliao_agent.workers.house_sync import ALL_INTENTS, Message

HOUSE_A = "house-001"
HOUSE_B = "house-002"


class _RecordedMessage:
    """内存消息：记录提交次数/结果，可注入提交异常。"""

    def __init__(self, payload: bytes, *, commit_error: Exception | None = None) -> None:
        self.payload = payload
        self.commit_error = commit_error
        self.commit_calls = 0
        self.committed = False


class InMemorySource:
    """消息源替身：可注入前 N 次 start 失败（断连重连场景）；消息仅一遍
    语义（ drains 后重连为空流——已提交 offset 不会被重复拉取）。"""

    def __init__(
        self,
        messages: list[_RecordedMessage],
        *,
        fail_first_starts: int = 0,
    ) -> None:
        self._pending = list(messages)
        self.fail_first_starts = fail_first_starts
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_calls <= self.fail_first_starts:
            msg = "broker unreachable"
            raise KafkaConnectionError(msg)

    async def stop(self) -> None:
        self.stop_calls += 1

    async def messages(self) -> AsyncIterator[Message]:
        pending, self._pending = self._pending, []
        for recorded in pending:
            yield Message(recorded.payload, self._commit_once(recorded))

    @staticmethod
    def _commit_once(recorded: _RecordedMessage) -> Any:
        async def commit() -> None:
            recorded.commit_calls += 1
            if recorded.commit_error is not None:
                raise recorded.commit_error
            recorded.committed = True

        return commit




class _FlakyL0:
    """L0 故障替身：set/invalidate 返回 False（Redis 故障 fail-safe 语义），
    get 返回 None（miss 语义）、tombstone_version 返回 None（故障归并）
    ——驱动「重试仍失败 → 不提交 offset + failure 事件」分支。"""

    def set(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def get(self, *args: Any, **kwargs: Any) -> FactCard | None:
        return None

    def invalidate(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def tombstone_version(self, house_id: str) -> float | None:
        return None


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def l0(redis_client: fakeredis.FakeRedis) -> L0CacheClient:
    return L0CacheClient(redis_client)


def _now_payload(event_type: str, house_id: str, fact_version: int, **extra: Any) -> bytes:
    payload: dict[str, Any] = {
        "event_id": f"evt-{house_id}-{fact_version}",
        "event_type": event_type,
        "house_id": house_id,
        "fact_version": fact_version,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    payload.update(extra)
    return json.dumps(payload).encode()


def _upsert(
    house_id: str,
    fact_version: int,
    *,
    stale: bool = False,
    age_ms: int = 0,
    occurred_at: datetime | None = None,
) -> _RecordedMessage:
    if occurred_at is None:
        age = timedelta(seconds=10) if stale else timedelta(milliseconds=age_ms)
        occurred_at = datetime.now(UTC) - age
    return _RecordedMessage(
        json.dumps(
            {
                "event_id": f"evt-{house_id}-{fact_version}",
                "event_type": "house_upsert",
                "house_id": house_id,
                "fact_version": fact_version,
                "occurred_at": occurred_at.isoformat(),
                "status": "active",
                "price_yuan": 1_250_000,
                "school": "实验小学",
                "dialogue_summary": ["第 1 轮"],
            }
        ).encode()
    )


def _invalidate(house_id: str, fact_version: int) -> _RecordedMessage:
    return _RecordedMessage(_now_payload("house_invalidate", house_id, fact_version))


def _sync_events(fake: _FakeLangfuse) -> list[dict[str, Any]]:
    return [e for e in fake.events if e["metadata"]["event"] == "house_sync_event"]


def _sync_metadata(event: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", event["metadata"]["metadata"])


def _operations(fake: _FakeLangfuse) -> list[str]:
    return [cast("str", e["metadata"]["operation"]) for e in _sync_events(fake)]


async def _consume_until(consumer: HouseSyncConsumer, predicate: Any) -> None:
    """启动消费者任务，轮询直至谓词满足（或超时 fail），随后关停回收。"""
    task = asyncio.create_task(consumer.run())
    try:
        deadline = asyncio.get_running_loop().time() + 3.0
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("timed out waiting for consumption")
            await asyncio.sleep(0.01)
    finally:
        await consumer.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _consumer(l0: Any, source: InMemorySource) -> HouseSyncConsumer:
    return HouseSyncConsumer(
        l0,
        source=source,
        initial_backoff=timedelta(milliseconds=5),
        max_backoff=timedelta(milliseconds=20),
    )


def test_upsert_writes_all_intents_and_commits(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    # occurred_at 固定为构造前 100ms：与消费耗时解耦，高负载 CI 下不会
    # 翻进 stale 路径（断言 0<=latency_ms<=500 语义不变）。
    message = _upsert(HOUSE_A, 5, age_ms=100)
    source = InMemorySource([message])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: message.committed)

    asyncio.run(scenario())

    for intent in ALL_INTENTS:
        card = l0.get(intent, HOUSE_A)
        assert card is not None and card.fact_version == 5
        assert card.price_bucket == 12
        assert card.school == "实验小学"
    # L0 各 intent 落一张卡片 + 二级索引（9 类 intent 全部写入）。
    assert message.committed is True
    consumed = [e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "consumed"]
    assert len(consumed) == 1
    metadata = _sync_metadata(consumed[0])
    assert metadata["fact_version"] == 5
    assert 0 <= cast("float", metadata["latency_ms"]) <= 500  # 验收：延迟 ≤500ms 记录
    assert metadata["version_conflict"] is False
    assert all(e["level"] != "ERROR" for e in fake_langfuse.events)


def test_invalidate_tombstones_all_intents(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    upsert_v1 = _upsert(HOUSE_A, 1)
    invalidate_v2 = _invalidate(HOUSE_A, 2)
    source = InMemorySource([upsert_v1, invalidate_v2])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: invalidate_v2.committed)

    asyncio.run(scenario())

    assert upsert_v1.committed is True
    assert invalidate_v2.committed is True
    for intent in ALL_INTENTS:
        assert l0.get(intent, HOUSE_A) is None  # 全部 intent 读取返回 None
    consumed = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "consumed"
    ]
    assert len(consumed) == 2
    assert _sync_metadata(consumed[1])["event_type"] == "house_invalidate"
    # L0 客户端已记录 invalidated 事件（operation 词表经 record_cache_event）。
    invalidated = [
        e
        for e in fake_langfuse.events
        if e["metadata"]["event"] == "cache_event"
        and e["metadata"]["operation"] == "invalidated"
    ]
    assert invalidated


def test_stale_upsert_invalidate_only_with_alert(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    stale = _upsert(HOUSE_A, 3, stale=True)
    source = InMemorySource([stale])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: stale.committed)

    asyncio.run(scenario())

    assert stale.committed is True
    alerts = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "stale_alert"
    ]
    assert len(alerts) == 1
    assert alerts[0]["level"] == "ERROR"
    assert _sync_metadata(alerts[0])["stale"] is True  # 评审补丁：告警落点自带 stale 标记
    assert cast("float", _sync_metadata(alerts[0])["latency_ms"]) > 500
    for intent in ALL_INTENTS:
        assert l0.get(intent, HOUSE_A) is None  # 事件内容未写入，按失效处理
    # 失效生效：后续低版本事实不得复活（墓碑守卫）。
    stale_low = _upsert(HOUSE_A, 2)
    source2 = InMemorySource([stale_low])

    async def scenario2() -> None:
        await _consume_until(_consumer(l0, source2), lambda: stale_low.committed)

    asyncio.run(scenario2())
    for intent in ALL_INTENTS:
        assert l0.get(intent, HOUSE_A) is None


def test_corrupted_messages_skipped_committed_and_do_not_block(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    bad_json = _RecordedMessage(b"not-json{")
    schema_bad = _RecordedMessage(
        json.dumps(
            {
                "event_id": "evt-bad",
                "event_type": "house_upsert",
                "house_id": HOUSE_A,
                "fact_version": 1,
                "occurred_at": datetime.now(UTC).isoformat(),
                # 缺 status/price_yuan：schema 校验失败
            }
        ).encode()
    )
    healthy = _upsert(HOUSE_B, 1)
    source = InMemorySource([bad_json, schema_bad, healthy])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: healthy.committed)

    asyncio.run(scenario())

    assert bad_json.committed is True  # 毒消息提交，不阻塞分区
    assert schema_bad.committed is True
    assert healthy.committed is True  # 后续消息正常消费
    assert l0.get("PRICE_INQUIRY", HOUSE_B) is not None
    corrupted = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "corrupted"
    ]
    assert len(corrupted) == 2
    assert all(e["level"] == "ERROR" for e in corrupted)


def test_late_version_replay_guard_rejects_but_commits(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    v5 = _upsert(HOUSE_A, 5)
    late_v4 = _upsert(HOUSE_A, 4)
    source = InMemorySource([v5, late_v4])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: late_v4.committed)

    asyncio.run(scenario())

    assert late_v4.committed is True  # 守卫拒写仍提交 offset（验收标准）
    for intent in ALL_INTENTS:
        card = l0.get(intent, HOUSE_A)
        assert card is not None and card.fact_version == 5  # 无旧事实复活
    conflicted = [
        e
        for e in _sync_events(fake_langfuse)
        if e["metadata"]["operation"] == "consumed"
        and _sync_metadata(e)["fact_version"] == 4
    ]
    assert len(conflicted) == 1
    assert _sync_metadata(conflicted[0])["version_conflict"] is True


def test_redis_failure_blocks_commit_and_records_failure(
    fake_langfuse: _FakeLangfuse
) -> None:
    message = _upsert(HOUSE_A, 5)
    source = InMemorySource([message])
    consumer = _consumer(_FlakyL0(), source)

    async def scenario() -> None:
        consumer_task = asyncio.create_task(consumer.run())
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while not any(
                e["metadata"]["operation"] == "failure" for e in _sync_events(fake_langfuse)
            ):
                if asyncio.get_running_loop().time() > deadline:
                    pytest.fail("timed out waiting for failure event")
                await asyncio.sleep(0.01)
        finally:
            await consumer.stop()
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task

    asyncio.run(scenario())

    assert message.committed is False  # offset 不提交：事件可重放
    failures = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "failure"
    ]
    assert failures and all(e["level"] == "ERROR" for e in failures)
    assert any(
        _sync_metadata(e)["reason"] == "l0_set_failed" for e in failures
    )


def test_invalidate_redis_failure_blocks_commit(fake_langfuse: _FakeLangfuse) -> None:
    message = _invalidate(HOUSE_A, 2)
    source = InMemorySource([message])
    consumer = _consumer(_FlakyL0(), source)

    async def scenario() -> None:
        consumer_task = asyncio.create_task(consumer.run())
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while not any(
                _sync_metadata(e).get("reason") == "l0_invalidate_failed"
                for e in _sync_events(fake_langfuse)
            ):
                if asyncio.get_running_loop().time() > deadline:
                    pytest.fail("timed out waiting for invalidate attempt")
                await asyncio.sleep(0.01)
        finally:
            await consumer.stop()
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task

    asyncio.run(scenario())

    assert message.committed is False
    failures = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "failure"
    ]
    assert any(_sync_metadata(e)["reason"] == "l0_invalidate_failed" for e in failures)


def test_kafka_disconnect_retries_with_backoff(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    message = _upsert(HOUSE_A, 5)
    source = InMemorySource([message], fail_first_starts=2)
    consumer = _consumer(l0, source)

    async def scenario() -> None:
        consumer_task = asyncio.create_task(consumer.run())
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while not message.committed:
                if asyncio.get_running_loop().time() > deadline:
                    pytest.fail("timed out waiting for reconnect consumption")
                await asyncio.sleep(0.01)
        finally:
            await consumer.stop()
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task

    asyncio.run(scenario())

    assert source.start_calls >= 3  # 断连后指数退避重连成功
    assert message.committed is True
    assert l0.get("PRICE_INQUIRY", HOUSE_A) is not None
    failures = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "failure"
    ]
    assert any(
        _sync_metadata(e)["reason"] == "consume_loop_error" for e in failures
    )


def test_commit_failure_is_swallowed_and_recorded(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    message = _upsert(HOUSE_A, 5)
    message.commit_error = RuntimeError("commit boom")
    source = InMemorySource([message])
    consumer = _consumer(l0, source)

    async def scenario() -> None:
        consumer_task = asyncio.create_task(consumer.run())
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while not any(
                _sync_metadata(e).get("reason") == "offset_commit_failed"
                for e in _sync_events(fake_langfuse)
            ):
                if asyncio.get_running_loop().time() > deadline:
                    pytest.fail("timed out waiting for commit failure event")
                await asyncio.sleep(0.01)
        finally:
            await consumer.stop()
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task

    asyncio.run(scenario())

    assert message.commit_calls >= 1
    assert message.committed is False  # 提交失败：重启后重放（at-least-once）
    assert l0.get("PRICE_INQUIRY", HOUSE_A) is not None  # 写入已完成


def test_stale_event_redis_invalidate_failure_blocks_commit(
    fake_langfuse: _FakeLangfuse
) -> None:
    stale = _upsert(HOUSE_A, 3, stale=True)
    source = InMemorySource([stale])
    consumer = _consumer(_FlakyL0(), source)

    async def scenario() -> None:
        consumer_task = asyncio.create_task(consumer.run())
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while not any(
                _sync_metadata(e).get("reason") == "stale_invalidate_failed"
                for e in _sync_events(fake_langfuse)
            ):
                if asyncio.get_running_loop().time() > deadline:
                    pytest.fail("timed out waiting for stale handling")
                await asyncio.sleep(0.01)
        finally:
            await consumer.stop()
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task

    asyncio.run(scenario())

    assert stale.committed is False
    # 告警仅在 invalidate 成功后发出（失败路径重放会重复告警，评审补丁）。
    assert "stale_alert" not in _operations(fake_langfuse)
    failures = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "failure"
    ]
    assert any(
        _sync_metadata(e)["reason"] == "stale_invalidate_failed" for e in failures
    )


# ---- 组合根 lifespan 门控（验收：WEILIAO_KAFKA_BROKERS 未设置不创建消费者） ----


class _SpyConsumer:
    """HouseSyncConsumer 替身：记录创建/运行/关停，不连 Kafka。"""

    created: list["_SpyConsumer"] = []

    def __init__(self, l0: Any, *, source: Any) -> None:
        self.source = source
        self.task_started = False
        self.stopped = False
        _SpyConsumer.created.append(self)

    async def run(self) -> None:
        self.task_started = True
        while not self.stopped:
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self.stopped = True


class _SpySource:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_lifespan_without_brokers_starts_no_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    _SpyConsumer.created.clear()
    monkeypatch.delenv("WEILIAO_KAFKA_BROKERS", raising=False)
    monkeypatch.setattr(main_module, "HouseSyncConsumer", _SpyConsumer)
    monkeypatch.setattr(main_module, "KafkaMessageSource", _SpySource)
    with TestClient(main_module.app) as client:
        assert client.get("/health").status_code == 200  # API 正常就绪
    assert _SpyConsumer.created == []  # 消费者任务不创建


def test_lifespan_with_brokers_runs_and_stops_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SpyConsumer.created.clear()
    monkeypatch.setenv("WEILIAO_KAFKA_BROKERS", "localhost:9092")
    monkeypatch.setattr(main_module, "HouseSyncConsumer", _SpyConsumer)
    monkeypatch.setattr(main_module, "KafkaMessageSource", _SpySource)
    with TestClient(main_module.app) as client:
        assert client.get("/health").status_code == 200
        assert len(_SpyConsumer.created) == 1
        spy = _SpyConsumer.created[0]
        assert spy.task_started  # 消费任务随 lifespan 启动
        assert isinstance(spy.source, _SpySource)
        assert spy.source.kwargs == {  # 组合根 topic/group 缺省值
            "brokers": "localhost:9092",
            "topic": "house.sync.trigger",
            "group_id": "weiliao-house-sync",
        }
    assert _SpyConsumer.created[0].stopped  # 优雅关停：停止拉取


def test_lifespan_kafka_topic_and_group_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SpyConsumer.created.clear()
    monkeypatch.setenv("WEILIAO_KAFKA_BROKERS", "localhost:9092")
    monkeypatch.setenv("WEILIAO_KAFKA_TOPIC", "custom.topic")
    monkeypatch.setenv("WEILIAO_KAFKA_GROUP_ID", "custom-group")
    monkeypatch.setattr(main_module, "HouseSyncConsumer", _SpyConsumer)
    monkeypatch.setattr(main_module, "KafkaMessageSource", _SpySource)
    with TestClient(main_module.app):
        pass
    assert _SpyConsumer.created[0].source.kwargs["topic"] == "custom.topic"
    assert _SpyConsumer.created[0].source.kwargs["group_id"] == "custom-group"


class _BlockingConsumer:
    """run() 不响应 stop() 的消费者替身：关停只能走 wait_for 超时强退。"""

    created: list["_BlockingConsumer"] = []

    def __init__(self, l0: Any, *, source: Any) -> None:
        self.stopped = False
        self.cancelled = False
        _BlockingConsumer.created.append(self)

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def stop(self) -> None:
        self.stopped = True


def test_lifespan_forces_cancel_on_blocking_consumer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _BlockingConsumer.created.clear()
    monkeypatch.setenv("WEILIAO_KAFKA_BROKERS", "localhost:9092")
    monkeypatch.setattr(main_module, "HouseSyncConsumer", _BlockingConsumer)
    monkeypatch.setattr(main_module, "KafkaMessageSource", _SpySource)
    monkeypatch.setattr(main_module, "CONSUMER_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
    with (
        caplog.at_level(logging.WARNING, logger="weiliao_agent.main"),
        TestClient(main_module.app) as client,
    ):
        assert client.get("/health").status_code == 200
        assert len(_BlockingConsumer.created) == 1
    spy = _BlockingConsumer.created[0]
    assert spy.stopped is True
    assert spy.cancelled is True  # 任务被取消回收，无异常逸出
    assert any("forcing cancel" in record.message for record in caplog.records)


# ---- Story 5.2：转人工指令发送器 lifespan（启动失败禁用 / 关停关闭） ----


class _SpyHandoffSender:
    """HandoffCommandSender 替身：记录 start/close/disable 调用。"""

    def __init__(self, *, start_error: Exception | None = None) -> None:
        self.start_error = start_error
        self.started = False
        self.closed = False
        self.disabled = False

    def start(self) -> None:
        self.started = True
        if self.start_error is not None:
            raise self.start_error

    def close(self) -> None:
        self.closed = True

    def disable(self) -> None:
        self.disabled = True

    def send_command(self, envelope: Any, *, reply: str) -> None:
        pass  # lifespan 测试不触发出口分支


def test_lifespan_sender_start_failure_disables_sender(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """验证缺口修补（评审轮次 1）：producer start 失败不得拖垮服务——
    ERROR 告警 + sender.disable()（出口分支跳过发送，文案语义不变）。
    删掉 disable 一行本测试即红。"""
    sender = _SpyHandoffSender(start_error=RuntimeError("connect boom"))
    monkeypatch.setattr(main_module, "_handoff_command_sender", sender)
    with (
        caplog.at_level(logging.ERROR, logger="weiliao_agent.main"),
        TestClient(main_module.app),
    ):
        assert sender.started
        assert sender.disabled is True
    alerts = [
        record
        for record in caplog.records
        if "handoff_command_alert" in record.message
    ]
    assert len(alerts) == 1
    assert alerts[0].levelno == logging.ERROR


def test_lifespan_closes_sender_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证缺口修补（评审轮次 1）：lifespan 退出须关闭发送器
    （后台事件循环线程 + producer 回收，不悬挂进程）。"""
    sender = _SpyHandoffSender()
    monkeypatch.setattr(main_module, "_handoff_command_sender", sender)
    with TestClient(main_module.app):
        assert sender.started
        assert sender.closed is False
    assert sender.closed is True


def test_lifespan_runs_and_cancels_metrics_log_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Story 3.4（A 方案）/4.5 接线守护：metrics 定时日志任务随 lifespan
    # 启动、退出后被 cancel 回收——删掉 create_task 一行本测试即红，防
    # stderr 读数入口静默脱落。间隔改小（循环体每轮读模块全局，patch 即生效）。
    monkeypatch.delenv("WEILIAO_KAFKA_BROKERS", raising=False)
    monkeypatch.setattr(main_module, "_METRICS_LOG_SECONDS", 0.05)
    metrics_module.reset_cache_metrics()
    metrics_module.reset_llm_breaker_metrics()
    metrics_module.reset_model_routing_metrics()
    created_tasks: list[asyncio.Task[Any]] = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = real_create_task(coro, **kwargs)
        if getattr(coro, "__name__", "") == "_metrics_log_loop":
            created_tasks.append(task)
        return task

    monkeypatch.setattr(main_module.asyncio, "create_task", _spy_create_task)
    with (
        caplog.at_level(logging.INFO, logger="weiliao_agent.observability.metrics"),
        TestClient(main_module.app) as client,
    ):
        assert client.get("/health").status_code == 200
        deadline = time.monotonic() + 5  # 等首个间隔后的发射（~0.05s）
        while time.monotonic() < deadline and not {
            json.loads(r.message)["event"]
            for r in caplog.records
            if r.name == "weiliao_agent.observability.metrics"
        } >= {
            "cache_metrics",
            "risk_review_metrics",
            "llm_breaker_metrics",
            "model_routing_metrics",
        }:
            time.sleep(0.01)
    records = [
        json.loads(r.message)
        for r in caplog.records
        if r.name == "weiliao_agent.observability.metrics"
    ]
    cache_payload = next(p for p in records if p["event"] == "cache_metrics")
    assert cache_payload["lookups"] == 0
    assert cache_payload["hit_rate"] is None
    # Story 4.4：循环内新增的审核通过率发射须被观察——删掉 main.py 中
    # emit_risk_review_metrics_log() 一行，本断言即红（定时日志与跌破
    # 阈值告警入口防静默脱落）。
    risk_payload = next(p for p in records if p["event"] == "risk_review_metrics")
    assert risk_payload["total"] == 0
    assert risk_payload["pass_rate"] is None
    # Story 4.5：循环内新增的熔断器发射须被观察——删掉 main.py 中
    # emit_llm_breaker_metrics_log() 一行，本断言即红（定时日志与
    # 非 closed 态告警入口防静默脱落）。
    breaker_payload = next(p for p in records if p["event"] == "llm_breaker_metrics")
    assert breaker_payload["state"] == "closed"
    assert breaker_payload["requests"] == 0
    # Story 4.6：循环内新增的路由发射须被观察——删掉 main.py 中
    # emit_model_routing_metrics_log() 一行，本断言即红（定时日志与
    # 收紧态告警入口防静默脱落）。
    routing_payload = next(p for p in records if p["event"] == "model_routing_metrics")
    assert routing_payload["tightened"] is False
    assert routing_payload["over_limits"] == 0
    assert len(created_tasks) == 1  # 循环任务随 lifespan 创建
    assert created_tasks[0].cancelled()  # 退出上下文后 cancel 回收，无悬挂


# ---- Kafka tombstone 记录（record.value=None）与消息源构造/提交映射 ----


def test_kafka_tombstone_record_skipped_and_committed(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    # Kafka tombstone 记录：record.value 为 None（如压缩主题保留删除标记）。
    tombstone = _RecordedMessage(cast("bytes", None))
    healthy = _upsert(HOUSE_B, 1)
    source = InMemorySource([tombstone, healthy])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: healthy.committed)

    asyncio.run(scenario())

    assert tombstone.committed is True  # 毒消息提交，分区不卡死
    assert healthy.committed is True
    corrupted = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "corrupted"
    ]
    assert len(corrupted) == 1
    assert "TypeError" in cast("str", _sync_metadata(corrupted[0])["reason"])


class _StubKafkaConsumer:
    """AIOKafkaConsumer 替身：记录构造参数与 commit 调用（不连真 broker）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.commit_calls: list[dict[Any, int]] = []
        self.records: list[Any] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commit_calls.append(offsets)

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for record in self.records:
            yield record


def test_kafka_source_construction_and_commit_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(house_sync_module, "AIOKafkaConsumer", _StubKafkaConsumer)
    source = KafkaMessageSource(
        brokers="localhost:9092",
        topic="house.sync.trigger",
        group_id="weiliao-house-sync",
    )
    consumer = source._consumer
    # 构造参数映射：topic 位置传入、手动提交保 at-least-once。
    assert consumer.args == ("house.sync.trigger",)
    assert consumer.kwargs["bootstrap_servers"] == "localhost:9092"
    assert consumer.kwargs["group_id"] == "weiliao-house-sync"
    assert consumer.kwargs["enable_auto_commit"] is False

    async def scenario() -> None:
        await source._commit_record(
            SimpleNamespace(topic="house.sync.trigger", partition=3, offset=41)
        )

    asyncio.run(scenario())

    (offsets,) = consumer.commit_calls
    (tp, position), = offsets.items()
    assert (tp.topic, tp.partition) == ("house.sync.trigger", 3)
    assert position == 42  # {TopicPartition: offset+1} 精确提交单条


def test_kafka_source_messages_iterates_records_with_payload_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 评审补丁：``messages()`` 真实异步生成器曾无任何测试执行（旧 stub
    # 无 __aiter__）——record 属性读取/Message 组装/commit 映射在真实
    # 迭代形状下全部未执行，属性名笔误会在生产首条消息即炸。
    monkeypatch.setattr(house_sync_module, "AIOKafkaConsumer", _StubKafkaConsumer)
    source = KafkaMessageSource(
        brokers="localhost:9092",
        topic="house.sync.trigger",
        group_id="weiliao-house-sync",
    )
    source._consumer.records = [
        SimpleNamespace(value=b"payload-1", topic="house.sync.trigger", partition=0, offset=7),
        SimpleNamespace(value=b"payload-2", topic="house.sync.trigger", partition=1, offset=3),
    ]

    async def scenario() -> list[Message]:
        received: list[Message] = []
        async for message in source.messages():
            received.append(message)
            await message.commit()
        return received

    received = asyncio.run(scenario())

    assert [m.payload for m in received] == [b"payload-1", b"payload-2"]
    assert len(source._consumer.commit_calls) == 2
    (first, second) = source._consumer.commit_calls
    (tp1, pos1), = first.items()
    (tp2, pos2), = second.items()
    assert (tp1.topic, tp1.partition, pos1) == ("house.sync.trigger", 0, 8)
    assert (tp2.topic, tp2.partition, pos2) == ("house.sync.trigger", 1, 4)


# ---- set=False 三段诊断（索引守卫 / 墓碑守卫 / Redis 故障瞬断恢复） ----


class _GuardRejectionL0:
    """墓碑守卫拒写场景 L0 假件：set 恒 False（守卫拒写）、get 恒 None
    （墓碑遮蔽）、tombstone_version 返回遮蔽版本（≥ 事件版本）——驱动
    「守卫拒写确认、提交 offset」分支（其余 intent 同判定短路）。"""

    def __init__(self, tomb_version: float) -> None:
        self._tomb_version = tomb_version

    def set(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def get(self, *args: Any, **kwargs: Any) -> FactCard | None:
        return None

    def tombstone_version(self, house_id: str) -> float | None:
        return self._tomb_version


class _TransientSetFailL0:
    """瞬断恢复场景 L0 假件：set 首试恒 False（模拟 RedisError 被 fail-safe
    吞为 False——与守卫拒写在类型上不可区分），重试委托真实
    L0CacheClient——评审 P1 回归：只读探针不得新建墓碑反噬重试写入
    （旧副作用式 invalidate 探针实现下本场景静默丢事实、假墓碑遮蔽
    该房源全部 intent 读取直至 TTL 过期）。"""

    def __init__(self, real: L0CacheClient) -> None:
        self._real = real
        self._first_attempt_failed = False

    def set(self, intent: Any, card: Any, **kwargs: Any) -> bool:
        if not self._first_attempt_failed:
            self._first_attempt_failed = True
            return False
        return self._real.set(intent, card, **kwargs)

    def get(self, intent: Any, house_id: str, **kwargs: Any) -> FactCard | None:
        return self._real.get(intent, house_id, **kwargs)

    def tombstone_version(self, house_id: str) -> float | None:
        return self._real.tombstone_version(house_id)


def test_transient_set_failure_recovery_writes_card_and_commits(
    redis_client: fakeredis.FakeRedis, fake_langfuse: _FakeLangfuse
) -> None:
    # 评审 P1 回归：set 首试瞬断（False）→ get None → 只读探针确认无
    # 现存墓碑 → 重试 set 成功。事实必须落 L0、offset 提交、且 Redis
    # 不得留下假墓碑（旧实现探针新立版本=事件版本的墓碑、守卫反噬
    # 重试、事实丢失而 offset 已提交）。
    message = _upsert(HOUSE_A, 5)
    source = InMemorySource([message])

    async def scenario() -> None:
        await _consume_until(
            _consumer(_TransientSetFailL0(L0CacheClient(redis_client)), source),
            lambda: message.committed,
        )

    asyncio.run(scenario())

    assert message.committed is True
    real = L0CacheClient(redis_client)
    for intent in ALL_INTENTS:
        card = real.get(intent, HOUSE_A)
        assert card is not None and card.fact_version == 5
    assert real.tombstone_version(HOUSE_A) is None  # 无假墓碑残留
    consumed = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "consumed"
    ]
    assert len(consumed) == 1
    assert _sync_metadata(consumed[0])["probe_retry"] is True
    assert _sync_metadata(consumed[0])["version_conflict"] is False


def test_tombstone_guard_rejection_commits_as_version_conflict(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 现存墓碑版本（6）≥ 事件版本（5）：只读探针确认守卫拒写，
    # offset 正常提交、不重试 set（重试必被同一守卫拒绝）。
    message = _upsert(HOUSE_A, 5)
    source = InMemorySource([message])

    async def scenario() -> None:
        await _consume_until(
            _consumer(_GuardRejectionL0(tomb_version=6), source),
            lambda: message.committed,
        )

    asyncio.run(scenario())

    assert message.committed is True  # 守卫拒写仍提交 offset
    consumed = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "consumed"
    ]
    assert len(consumed) == 1
    assert _sync_metadata(consumed[0])["version_conflict"] is True
    assert "probe_retry" not in _sync_metadata(consumed[0])


def test_real_tombstone_guard_rejection_short_circuits(
    l0: L0CacheClient, fake_langfuse: _FakeLangfuse
) -> None:
    # 真实 L0（fakeredis）端到端：预立 v6 墓碑后消费 v5 upsert——
    # 只读探针读到遮蔽版本即短路提交；卡片不写入、墓碑不被拉低
    # （tombstone_version 走真实 Lua 写入的 JSON 墓碑解析路径）。
    l0.invalidate(HOUSE_A, 6)
    message = _upsert(HOUSE_A, 5)
    source = InMemorySource([message])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: message.committed)

    asyncio.run(scenario())

    assert message.committed is True
    assert l0.get("PRICE_INQUIRY", HOUSE_A) is None
    assert l0.tombstone_version(HOUSE_A) == 6
    consumed = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "consumed"
    ]
    assert _sync_metadata(consumed[0])["version_conflict"] is True


@pytest.mark.parametrize("age_ms,expect_stale", [(499, False), (501, True)])
def test_stale_gate_boundary_pins_500ms_budget(
    monkeypatch: pytest.MonkeyPatch,
    l0: L0CacheClient,
    fake_langfuse: _FakeLangfuse,
    age_ms: int,
    expect_stale: bool,
) -> None:
    # 评审补丁：500ms 预算旧唯一断言是 now-100ms 构造下的近恒真式，
    # 判定边界两侧（499 放行 / 501 转失效）无用例。冻结消费时刻时钟、
    # occurred_at 精确压线（无冻结时处理耗时会令 499ms 用例在慢 CI
    # 上翻界成 flaky）。
    occurred_at = datetime.now(UTC) - timedelta(milliseconds=age_ms)
    frozen_at = occurred_at + timedelta(milliseconds=age_ms)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return frozen_at

    monkeypatch.setattr(house_sync_module, "datetime", _FrozenDateTime)
    message = _upsert(HOUSE_A, 5, occurred_at=occurred_at)
    source = InMemorySource([message])

    async def scenario() -> None:
        await _consume_until(_consumer(l0, source), lambda: message.committed)

    asyncio.run(scenario())

    assert message.committed is True
    alerts = [
        e for e in _sync_events(fake_langfuse) if e["metadata"]["operation"] == "stale_alert"
    ]
    if expect_stale:
        assert len(alerts) == 1
        assert alerts[0]["level"] == "ERROR"
        assert _sync_metadata(alerts[0])["stale"] is True
        assert l0.get("PRICE_INQUIRY", HOUSE_A) is None  # 内容不写入
    else:
        assert alerts == []
        assert l0.get("PRICE_INQUIRY", HOUSE_A) is not None  # 正常 upsert
