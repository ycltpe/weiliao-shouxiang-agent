"""会话转写原语与 HTTP 转写挂点单测（Story 5.2，fakeredis 驱动）。

覆盖 ``SessionManager.record_transcript`` / ``read_transcript``
（AD-4 单写者）：per-turn JSON 右进左出截断、TTL 与会话一致、
limit 只读尾部、损坏条目防御、key 不存在为空——转交指令 payload
上下文权威来源（spec「最近 3 轮对话原文」决策 Q1-A）。HTTP 挂点
覆盖正常路径、编排器逃逸兜底路径（评审轮次 1 补丁：降级轮次同样
落转写）、写失败仅告警、无会话管理器跳过。
"""

import json
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
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX
from weiliao_agent.handoff.command import (
    HandoffCommand,
    HandoffCommandSender,
)
from weiliao_agent.models.envelope import Envelope, HandoffAssessment
from weiliao_agent.pipeline import HANDOFF_REPLY_TEXT, PipelineOrchestrator, PipelineResult
from weiliao_agent.session import SessionManager, TranscriptTurn
from weiliao_agent.session.manager import (
    DEFAULT_TTL_SECONDS,
    KEY_PREFIX,
    TRANSCRIPT_KEEP_TURNS,
)

TS_1 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
TS_2 = datetime(2026, 11, 9, 10, 1, 0, tzinfo=UTC)
SESSION_ID = "sess-001"
TRANSCRIPT_KEY = f"{KEY_PREFIX}{SESSION_ID}:transcript"


class _FakeTransport:
    """HandoffTransport 替身：记录发送（本文件内联，消除跨测试文件
    私有导入——spec 5.3 Review Triage 已裁 patch，Epic 5 code review
    兑现）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, str, float]] = []
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def send(self, *, topic: str, value: bytes, key: str, timeout: float) -> None:
        self.sent.append((topic, value, key, timeout))

    def close(self) -> None:
        self.closed = True


class _HandoffFilter(Filter):
    """首跳即挂硬触发判定结果的信封透传过滤器（内联替身）。"""

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


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def manager(client: fakeredis.FakeRedis) -> SessionManager:
    return SessionManager(client)


def test_record_and_read_roundtrip(manager: SessionManager) -> None:
    manager.record_transcript(
        SESSION_ID,
        turn=1,
        buyer_message="房子还有吗",
        agent_reply="还有的",
        timestamp=TS_1,
    )

    turns = manager.read_transcript(SESSION_ID, 3)

    assert turns == [
        TranscriptTurn(
            turn=1,
            buyer_message="房子还有吗",
            agent_reply="还有的",
            timestamp=TS_1,
        )
    ]


def test_read_limit_returns_most_recent_in_order(
    manager: SessionManager,
) -> None:
    for turn in range(1, 5):
        manager.record_transcript(
            SESSION_ID,
            turn=turn,
            buyer_message=f"msg-{turn}",
            agent_reply=f"reply-{turn}",
            timestamp=TS_1,
        )

    # 旧→新序，只取尾部 limit 条。
    assert manager.read_transcript(SESSION_ID, 2) == [
        TranscriptTurn(turn=3, buyer_message="msg-3", agent_reply="reply-3", timestamp=TS_1),
        TranscriptTurn(turn=4, buyer_message="msg-4", agent_reply="reply-4", timestamp=TS_1),
    ]
    assert len(manager.read_transcript(SESSION_ID, 10)) == 4


def test_list_trimmed_to_keep_limit(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    for turn in range(1, TRANSCRIPT_KEEP_TURNS + 6):
        manager.record_transcript(
            SESSION_ID,
            turn=turn,
            buyer_message=f"msg-{turn}",
            agent_reply="reply",
            timestamp=TS_1,
        )

    assert client.llen(TRANSCRIPT_KEY) == TRANSCRIPT_KEEP_TURNS
    turns = manager.read_transcript(SESSION_ID, TRANSCRIPT_KEEP_TURNS)
    assert [turn.turn for turn in turns] == list(range(6, TRANSCRIPT_KEEP_TURNS + 6))


def test_record_refreshes_ttl_matching_session(
        manager: SessionManager, client: fakeredis.FakeRedis
    ) -> None:
    manager.record_transcript(
        SESSION_ID,
        turn=1,
        buyer_message="m",
        agent_reply="r",
        timestamp=TS_1,
    )
    assert client.ttl(TRANSCRIPT_KEY) == DEFAULT_TTL_SECONDS

    client.expire(TRANSCRIPT_KEY, 60)  # 模拟中途 TTL 被压短
    manager.record_transcript(
        SESSION_ID,
        turn=2,
        buyer_message="m",
        agent_reply="r",
        timestamp=TS_2,
    )
    assert client.ttl(TRANSCRIPT_KEY) == DEFAULT_TTL_SECONDS


def test_missing_key_reads_empty(manager: SessionManager) -> None:
    assert manager.read_transcript("no-such-session", 3) == []


def test_non_positive_limit_reads_empty(manager: SessionManager) -> None:
    manager.record_transcript(
        SESSION_ID, turn=1, buyer_message="m", agent_reply="r", timestamp=TS_1
    )
    assert manager.read_transcript(SESSION_ID, 0) == []


def test_corrupt_entry_defended_as_blank_turn(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    client.rpush(TRANSCRIPT_KEY, "not-json{{{")
    client.rpush(TRANSCRIPT_KEY, json.dumps(["not", "a", "dict"]))
    client.rpush(TRANSCRIPT_KEY, json.dumps({"turn": "oops", "timestamp": 123}))

    turns = manager.read_transcript(SESSION_ID, 3)

    # 损坏条目一律归一为空白 TranscriptTurn，绝不抛错（防御非本模块写入）。
    assert turns == [TranscriptTurn(None, None, None, None)] * 3


def test_turn_none_and_null_fields_roundtrip(manager: SessionManager) -> None:
    # 无会话管理器的测试替身路径（turn=None、message=None）同样可落盘。
    manager.record_transcript(
        SESSION_ID,
        turn=None,
        buyer_message=None,
        agent_reply="已为您转接经纪人，请稍候",
        timestamp=TS_1,
    )

    (turn,) = manager.read_transcript(SESSION_ID, 1)
    assert turn.turn is None
    assert turn.buyer_message is None
    assert turn.agent_reply == "已为您转接经纪人，请稍候"


# ---- HTTP 入口转写挂点（Story 5.2：回复落盘唯一可得点） ----


class _PassThroughFilter(Filter):
    def __init__(self) -> None:
        self.name = "intent_router"

    def process(self, envelope: Envelope) -> Envelope:
        return envelope


class _ExplodingOrchestrator(PipelineOrchestrator):
    """run() 逃逸异常的编排器替身：触发端点层兜底分支。"""

    def run(self, envelope: Envelope) -> PipelineResult:
        raise RuntimeError("orchestrator boom")


def _message_payload() -> dict[str, str]:
    return {
        "session_id": SESSION_ID,
        "house_id": "house-001",
        "timestamp": TS_1.isoformat(),
        "message": "房子还有吗",
    }


def test_endpoint_records_transcript_after_pipeline(
    manager: SessionManager,
) -> None:
    app = create_app(
        PipelineOrchestrator(filters=[_PassThroughFilter()]),
        manager,
    )

    response = TestClient(app).post("/v1/messages", json=_message_payload())

    assert response.status_code == 200
    (turn,) = manager.read_transcript(SESSION_ID, 1)
    assert turn.turn == 1
    assert turn.buyer_message == "房子还有吗"
    assert turn.agent_reply == response.json()["reply"]  # 回复原文落盘


def test_endpoint_records_transcript_on_orchestrator_escape(
    manager: SessionManager,
) -> None:
    """评审轮次 1 补丁：编排器逃逸兜底路径的降级轮次同样落转写——
    后续 handoff 上下文不缺轮。"""
    app = create_app(_ExplodingOrchestrator(filters=[]), manager)

    response = TestClient(app).post("/v1/messages", json=_message_payload())

    body = response.json()
    assert response.status_code == 200
    assert body["degraded"] is True  # 端点兜底应答
    (turn,) = manager.read_transcript(SESSION_ID, 1)
    assert turn.turn == 1
    assert turn.buyer_message == "房子还有吗"
    assert turn.agent_reply == body["reply"]  # 兜底回复原文同样落盘


def test_endpoint_transcript_record_failure_only_warns(
    manager: SessionManager, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(
        PipelineOrchestrator(filters=[_PassThroughFilter()]),
        manager,
    )
    with mock.patch.object(
        manager, "record_transcript", side_effect=redis.RedisError("boom")
    ):
        response = TestClient(app).post("/v1/messages", json=_message_payload())

    assert response.status_code == 200  # 应答不受影响
    assert any(
        "transcript record failed" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


def test_endpoint_without_session_manager_skips_transcript() -> None:
    app = create_app(PipelineOrchestrator(filters=[_PassThroughFilter()]))

    response = TestClient(app).post("/v1/messages", json=_message_payload())

    assert response.status_code == 200


# ---- 发送器 transcript_reader 绑定真实 SessionManager（接线守护） ----


def _trigger_envelope() -> Envelope:
    return Envelope.model_validate(
        {
            "session_id": SESSION_ID,
            "house_id": "house-001",
            "timestamp": TS_2.isoformat(),
            "trace_id": "b" * 32,
            "turn": 2,
            "message": "你们这是骗子",
            "handoff": {"triggered": True, "conditions": ["骗子"]},
        }
    )


def _real_sender(
    manager: SessionManager, transport: _FakeTransport
) -> HandoffCommandSender:
    return HandoffCommandSender(
        topic="handoff.commands",
        dlq_topic="handoff.commands.dlq",
        transport=transport,
        transcript_reader=manager.read_transcript,
    )


def test_sender_transcript_reader_reads_turn_recorded_via_endpoint(
    fake_langfuse: Any,
) -> None:
    """接线守护：transcript_reader 绑定真实 SessionManager——换成
    ``lambda sid, limit: []`` 本测试即红（生产侧 handoff 历史静默
    丢失）。前一轮经真实 HTTP 端点落盘，发送器读回进 payload。"""
    manager = SessionManager(fakeredis.FakeRedis())
    app = create_app(
        PipelineOrchestrator(filters=[_PassThroughFilter()]),
        manager,
    )
    response = TestClient(app).post("/v1/messages", json=_message_payload())
    assert response.status_code == 200

    transport = _FakeTransport()
    sender = _real_sender(manager, transport)
    sender.send_command(_trigger_envelope(), reply=HANDOFF_REPLY_TEXT)

    ((_, value, _, _),) = transport.sent
    command = HandoffCommand.model_validate(json.loads(value.decode("utf-8")))
    assert [turn.turn for turn in command.recent_turns] == [1, 2]
    # 历史轮来自 Redis（端点落盘），当前轮来自信封。
    assert command.recent_turns[0].buyer_message == "房子还有吗"
    assert command.recent_turns[0].agent_reply == response.json()["reply"]
    assert command.recent_turns[1].buyer_message == "你们这是骗子"
    assert command.recent_turns[1].agent_reply == HANDOFF_REPLY_TEXT


def test_real_orchestrator_with_real_sender_builds_payload_from_redis(
    fake_langfuse: Any,
) -> None:
    """端到端组合：真实编排器 + 真实发送器（fakeredis 转写 + 内存
    transport）——出口分支先指令后通知，payload 上下文权威来源为
    SessionManager（AD-4）。"""
    manager = SessionManager(fakeredis.FakeRedis())
    app = create_app(
        PipelineOrchestrator(filters=[_PassThroughFilter()]),
        manager,
    )
    TestClient(app).post("/v1/messages", json=_message_payload())

    transport = _FakeTransport()
    orchestrator = PipelineOrchestrator(
        filters=[_HandoffFilter()],
        handoff_sender=_real_sender(manager, transport),
    )
    result = orchestrator.run(_trigger_envelope())

    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert result.degraded is False
    assert len(transport.sent) == 1  # 指令先于应答发出
    ((_, value, key, _),) = transport.sent
    assert key == SESSION_ID
    command = HandoffCommand.model_validate(json.loads(value.decode("utf-8")))
    assert [turn.turn for turn in command.recent_turns] == [1, 2]
