"""转人工原子终态闸门单测（Story 5.3，fakeredis 驱动）。

覆盖 spec I/O & Edge-Case Matrix 全场景：转接后再消息（编排器零
执行、回 HANDOFF_REPLY_TEXT、不落转写、trace 事件 + WARNING 日志）/
trace_id 缺失回填与透传 / 事件记录失败吞掉应答不变 / 触发轮回归
（先指令后通知 + HANDOFF_REPLY_TEXT + 转写落盘，连续红线消息零
二次指令——收口 5.2 评审 #18）/ Redis begin_turn 失败既有兜底语义 /
无会话管理器无闸门（原管道路径）/ 新会话 handoff_status="none"
正常管道 / 未知 handoff_status 值（Redis 损坏）fail-closed 兜底。
编排器替身经 run 调用计数断言装配级拦截（闸门在真实编排器之前）。
"""

import logging
from datetime import UTC, datetime
from typing import Any
from unittest import mock

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import render_fallback
from weiliao_agent.handoff.command import HandoffCommandSender
from weiliao_agent.models.envelope import Envelope, HandoffAssessment
from weiliao_agent.pipeline import HANDOFF_REPLY_TEXT, PipelineOrchestrator, PipelineResult
from weiliao_agent.session import SessionManager
from weiliao_agent.session.manager import KEY_PREFIX

TS_1 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
TS_2 = datetime(2026, 11, 9, 10, 1, 0, tzinfo=UTC)
SESSION_ID = "sess-001"


class _CountingFilter(Filter):
    """管道替身过滤器：记录执行次数，透传信封。"""

    def __init__(self) -> None:
        self.name = "intent_router"
        self.calls = 0

    def process(self, envelope: Envelope) -> Envelope:
        self.calls += 1
        return envelope


class _CountingOrchestrator(PipelineOrchestrator):
    """编排器替身：run 调用计数，供装配级「闸门在编排器前拦截」断言。"""

    def __init__(
        self,
        filters: list[Filter],
        handoff_sender: Any = None,
    ) -> None:
        super().__init__(filters=filters, handoff_sender=handoff_sender)
        self.run_calls = 0

    def run(self, envelope: Envelope) -> PipelineResult:
        self.run_calls += 1
        return super().run(envelope)


class _FakeTransport:
    """HandoffTransport 替身：按 topic 记录发送（含 key），可预置失败次数。"""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.sent: list[tuple[str, bytes, str, float]] = []
        self._failures = dict(failures or {})

    def start(self) -> None:
        pass

    def send(self, *, topic: str, value: bytes, key: str, timeout: float) -> None:
        if self._failures.get(topic, 0) > 0:
            self._failures[topic] -= 1
            raise RuntimeError("kafka boom")
        self.sent.append((topic, value, key, timeout))

    def close(self) -> None:
        pass


@pytest.fixture
def manager() -> SessionManager:
    return SessionManager(fakeredis.FakeRedis())


def _payload(timestamp: datetime = TS_1, trace_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": SESSION_ID,
        "house_id": "house-001",
        "timestamp": timestamp.isoformat(),
        "message": "房子还有吗",
    }
    if trace_id is not None:
        payload["trace_id"] = trace_id
    return payload


def test_gate_short_circuits_pipeline_after_handoff(
    manager: SessionManager, fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    orchestrator = _CountingOrchestrator(filters=[_CountingFilter()])
    app = create_app(orchestrator, manager)
    trace_id = "c" * 32  # 32-hex：Langfuse trace id 口径

    with caplog.at_level(logging.WARNING):
        response = TestClient(app).post(
            "/v1/messages", json=_payload(trace_id=trace_id)
        )

    body = response.json()
    assert response.status_code == 200
    assert body["reply"] == HANDOFF_REPLY_TEXT  # Q1-A：与触发轮出口同一文案
    assert body["degraded"] is False  # 设计内终态分支，非故障降级
    assert body["failed_filter"] is None
    assert body["trace_id"] == trace_id  # 携带值透传
    assert orchestrator.run_calls == 0  # 编排器零调用：闸门在管道之前
    assert manager.read_transcript(SESSION_ID, 10) == []  # 闸门轮不落转写
    (event,) = fake_langfuse.events
    assert event["name"] == "handoff.already_handed_off"
    assert event["level"] == "WARNING"
    assert event["metadata"]["session_id"] == SESSION_ID
    assert any(
        "session already handed off" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


def test_gate_backfills_trace_id_when_missing(
    manager: SessionManager, fake_langfuse: Any
) -> None:
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    orchestrator = _CountingOrchestrator(filters=[_CountingFilter()])
    app = create_app(orchestrator, manager)

    response = TestClient(app).post("/v1/messages", json=_payload())

    assert response.status_code == 200
    assert response.json()["reply"] == HANDOFF_REPLY_TEXT  # 闸门文案口径
    assert response.json()["degraded"] is False
    trace_id = response.json()["trace_id"]
    assert trace_id is not None and len(trace_id) == 32  # 权威回填口径
    assert orchestrator.run_calls == 0  # 回填路径同样不进编排器
    (event,) = fake_langfuse.events
    assert event["trace_context"]["trace_id"] == trace_id


def test_gate_event_record_failure_keeps_reply(
    manager: SessionManager, fake_langfuse: Any
) -> None:
    """I/O 矩阵：事件记录失败吞掉，应答不变（fail-safe）。"""
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    fake_langfuse.raise_on_event = True
    app = create_app(_CountingOrchestrator(filters=[_CountingFilter()]), manager)

    response = TestClient(app).post("/v1/messages", json=_payload())

    assert response.status_code == 200
    assert response.json()["reply"] == HANDOFF_REPLY_TEXT
    assert response.json()["degraded"] is False


def test_gate_never_returns_template_fallback(manager: SessionManager) -> None:
    # 闸门路径绝不回模板兜底文案（Never 约束）：文案逐字节等于出口常量。
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    app = create_app(_CountingOrchestrator(filters=[_CountingFilter()]), manager)

    response = TestClient(app).post("/v1/messages", json=_payload())

    assert response.json()["reply"] != render_fallback(None)
    assert response.json()["reply"] == HANDOFF_REPLY_TEXT


def test_trigger_turn_then_gate_no_second_command(
    manager: SessionManager, fake_langfuse: Any
) -> None:
    """触发轮回归 + 评审 #18 收口：首条红线消息走 5.1/5.2 触发轮
    （先指令后通知 + HANDOFF_REPLY_TEXT + 转写落盘）；转接后再发
    红线消息被闸门拦截——编排器零执行，不可能二次触发、零二次指令。"""
    transport = _FakeTransport()
    sender = HandoffCommandSender(
        topic="handoff.commands",
        dlq_topic="handoff.commands.dlq",
        transport=transport,
        transcript_reader=manager.read_transcript,
    )
    class _TriggeringFilter(Filter):
        """首跳硬触发替身：挂判定结果并经会话单写者落盘 handoff_status
        （生产侧 intent_router 的 marker 挂点，AD-4）。"""

        def __init__(self) -> None:
            self.name = "intent_router"

        def process(self, envelope: Envelope) -> Envelope:
            manager.mark_handoff(
                envelope.session_id, conditions=["redline_intent"]
            )
            return envelope.model_copy(
                update={
                    "handoff": HandoffAssessment(
                        triggered=True, conditions=["redline_intent"]
                    )
                }
            )

    orchestrator = _CountingOrchestrator(
        filters=[_TriggeringFilter()], handoff_sender=sender
    )
    app = create_app(orchestrator, manager)
    client = TestClient(app)

    first = client.post(
        "/v1/messages", json=_payload(timestamp=TS_1, trace_id="d" * 32)
    )
    assert first.json()["reply"] == HANDOFF_REPLY_TEXT
    assert first.json()["degraded"] is False
    assert len(transport.sent) == 1  # 严格先指令后通知：触发轮恰一条指令
    assert orchestrator.run_calls == 1
    assert len(manager.read_transcript(SESSION_ID, 10)) == 1  # 触发轮落转写

    # handoff_status 已由触发轮经会话单写者落盘（5.1 marker 挂点），
    # 此处直接验证终态：第二条红线消息被闸门拦截。
    assert manager.begin_turn(SESSION_ID, TS_2).handoff_status == "triggered"
    second = client.post(
        "/v1/messages", json=_payload(timestamp=TS_2, trace_id="e" * 32)
    )
    assert second.json()["reply"] == HANDOFF_REPLY_TEXT
    assert second.json()["degraded"] is False
    assert orchestrator.run_calls == 1  # 零二次执行
    assert len(transport.sent) == 1  # 零二次指令
    assert len(manager.read_transcript(SESSION_ID, 10)) == 1  # 闸门轮不落转写


def test_begin_turn_redis_failure_keeps_existing_fallback(
    manager: SessionManager,
) -> None:
    """I/O 矩阵：Redis 故障 fail-closed 语义不变——模板兜底（非
    HANDOFF_REPLY_TEXT）、degraded=True、原 trace_id、HTTP 非 5xx。"""
    orchestrator = _CountingOrchestrator(filters=[_CountingFilter()])
    app = create_app(orchestrator, manager)
    with mock.patch.object(
        manager, "begin_turn", side_effect=redis.RedisError("boom")
    ):
        response = TestClient(app).post(
            "/v1/messages", json=_payload(trace_id="f" * 32)
        )

    body = response.json()
    assert response.status_code == 200
    assert body["degraded"] is True
    assert body["trace_id"] == "f" * 32
    assert body["reply"] == render_fallback(None)  # 既有兜底口径，非闸门文案
    assert orchestrator.run_calls == 0  # 兜底路径不进编排器


def test_unknown_handoff_status_fails_closed() -> None:
    """未知 handoff_status（Redis 损坏值，非 none/triggered）fail-closed：
    与 begin_turn 失败同一兜底口径——模板兜底、degraded=True、原
    trace_id、不落转写、编排器零调用（拿不准绝不人机混答）。"""
    client = fakeredis.FakeRedis()
    client.hset(f"{KEY_PREFIX}{SESSION_ID}", "handoff_status", "corrupted")
    manager = SessionManager(client)
    orchestrator = _CountingOrchestrator(filters=[_CountingFilter()])
    app = create_app(orchestrator, manager)

    response = TestClient(app).post(
        "/v1/messages", json=_payload(trace_id="e" * 32)
    )

    body = response.json()
    assert response.status_code == 200  # HTTP 非 5xx
    assert body["degraded"] is True
    assert body["trace_id"] == "e" * 32  # 原 trace_id 透传
    assert body["reply"] == render_fallback(None)  # 既有兜底，非闸门文案
    assert orchestrator.run_calls == 0  # 不进编排器
    assert manager.read_transcript(SESSION_ID, 10) == []  # 不落转写


def test_without_session_manager_no_gate() -> None:
    """I/O 矩阵：无会话管理器（测试替身装配）即无权威终态状态——
    无闸门，走原管道路径。"""
    orchestrator = _CountingOrchestrator(filters=[_CountingFilter()])
    app = create_app(orchestrator)

    response = TestClient(app).post("/v1/messages", json=_payload())

    assert response.status_code == 200
    assert orchestrator.run_calls == 1  # 原管道路径


def test_new_session_first_message_uses_normal_pipeline(
    manager: SessionManager,
) -> None:
    """I/O 矩阵：handoff_status="none" 新会话首消息正常进管道。"""
    orchestrator = _CountingOrchestrator(filters=[_CountingFilter()])
    app = create_app(orchestrator, manager)

    response = TestClient(app).post("/v1/messages", json=_payload())

    assert response.status_code == 200
    assert orchestrator.run_calls == 1
    assert len(manager.read_transcript(SESSION_ID, 10)) == 1  # 正常轮落转写
