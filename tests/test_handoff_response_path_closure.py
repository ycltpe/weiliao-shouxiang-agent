"""编排器级终态闸门端到端测试（Story 5.5，fakeredis 驱动）。

覆盖 spec I/O & Edge-Case Matrix 全场景：编排器 ``run()`` 直接调用
入口（绕过 5.3 http 适配器闸门）下，已转接会话新消息经 SessionManager
实读 Redis 权威 ``handoff_status`` 后短路——全过滤器零执行、零 LLM、
零模板兜底，回包与 5.3 闸门文案逐字节一致（共享 helper 单一字节来源），
发送器零调用（无二次转交指令），留痕 already_handed_off / ai.disclosure
两事件与 WARNING 日志（无 pipeline.delivery 事件）；``"none"`` / None
（会话过期）放行正常管道；未知状态值与 Redis 读取失败 fail-closed
（degraded 模板兜底 + 原 trace_id，零过滤器执行）；trace_id 缺失回填
与携带透传；未注入 reader 时闸门禁用（行为与 5.5 前逐字节一致）。
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.pipeline import (
    HANDOFF_REPLY_TEXT,
    HandoffStatusReader,
    PipelineOrchestrator,
)
from weiliao_agent.session import SessionManager

TS_1 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
SESSION_ID = "sess-001"
TRACE_ID = "c" * 32  # 32-hex：Langfuse trace id 口径


class _CountingFilter(Filter):
    """管道替身过滤器：记录执行次数，透传信封（零副作用）。"""

    def __init__(self) -> None:
        self.name = "counting_filter"
        self.calls = 0

    def process(self, envelope: Envelope) -> Envelope:
        self.calls += 1
        return envelope


class _RecordingSender:
    """HandoffCommandSender 替身：记录调用，绝不抛出。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Envelope, str]] = []

    def send_command(self, envelope: Envelope, *, reply: str) -> None:
        self.calls.append((envelope, reply))


class _StaticReader:
    """HandoffStatusReader 替身：固定返回值（未知值/None 场景）。"""

    def __init__(self, status: str | None) -> None:
        self._status = status

    def read_handoff_status(self, session_id: str) -> str | None:
        return self._status


class _FailingReader:
    """HandoffStatusReader 替身：读取抛 redis.RedisError。"""

    def read_handoff_status(self, session_id: str) -> str | None:
        raise redis.RedisError("redis boom")


@pytest.fixture
def manager() -> SessionManager:
    return SessionManager(fakeredis.FakeRedis())


def _counting_filter() -> _CountingFilter:
    return _CountingFilter()


def _orchestrator(
    counting: _CountingFilter,
    sender: _RecordingSender,
    reader: HandoffStatusReader | None,
) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        filters=[counting],
        handoff_sender=sender,
        handoff_status_reader=reader,
    )


def _envelope(trace_id: str | None = TRACE_ID) -> Envelope:
    payload: dict[str, Any] = {
        "session_id": SESSION_ID,
        "house_id": "house-001",
        "timestamp": TS_1.isoformat(),
        "message": "房子还有吗",
    }
    if trace_id is not None:
        payload["trace_id"] = trace_id
    return Envelope.model_validate(payload)


def test_gate_short_circuits_all_filters_after_handoff(
    manager: SessionManager, fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    counting = _counting_filter()
    sender = _RecordingSender()
    orchestrator = _orchestrator(counting, sender, manager)
    envelope = _envelope()
    # Story 7.2（D3）：信封 frozen——原地写改 model_copy 整信封替换
    #（轮次语义不变：透传进闸门事件与 WARNING 日志）。
    envelope = envelope.model_copy(update={"turn": 7})

    with caplog.at_level(logging.WARNING):
        result = orchestrator.run(envelope)

    # 与 5.3 适配器闸门共享 helper：文案逐字节一致，设计内终态分支。
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert result.degraded is False
    assert result.failed_filter is None
    assert result.trace_id == TRACE_ID  # 携带值透传
    assert counting.calls == 0  # 全部过滤器零执行
    assert sender.calls == []  # 无二次转交指令
    names = [e["name"] for e in fake_langfuse.events]
    assert "handoff.already_handed_off" in names
    assert "ai.disclosure" in names
    assert "pipeline.delivery" not in names  # 消息未进管道
    (event,) = [e for e in fake_langfuse.events if e["name"] == "handoff.already_handed_off"]
    assert event["level"] == "WARNING"
    assert event["metadata"]["session_id"] == SESSION_ID
    assert event["metadata"]["turn"] == 7  # 信封 turn 透传进事件
    (disclosure,) = [e for e in fake_langfuse.events if e["name"] == "ai.disclosure"]
    assert disclosure["metadata"]["disclosed"] is True
    assert any(
        "session already handed off" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )
    assert any(
        "turn=7" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


def test_gate_passes_normal_pipeline_when_status_none(
    manager: SessionManager,
) -> None:
    # begin_turn 初始化 handoff_status="none"（Lua 初始化口径）。
    manager.begin_turn(SESSION_ID, TS_1)
    counting = _counting_filter()
    sender = _RecordingSender()
    orchestrator = _orchestrator(counting, sender, manager)

    result = orchestrator.run(_envelope())

    assert counting.calls == 1  # 正常管道：过滤器执行
    assert result.degraded is False
    assert result.reply == render_fallback(None)  # 替身链无组装产出，防御回退
    assert sender.calls == []


def test_gate_passes_when_session_expired(manager: SessionManager) -> None:
    # 会话不存在（TTL 过期后从零初始化）：reader 返回 None，放行管道。
    counting = _counting_filter()
    orchestrator = _orchestrator(counting, _RecordingSender(), manager)

    result = orchestrator.run(_envelope())

    assert counting.calls == 1
    assert result.degraded is False


def test_gate_fail_closed_on_unknown_status() -> None:
    counting = _counting_filter()
    orchestrator = _orchestrator(counting, _RecordingSender(), _StaticReader("corrupt"))

    result = orchestrator.run(_envelope())

    # fail-closed：degraded 模板兜底 + 原 trace_id，绝不放行 AI 管道。
    assert counting.calls == 0
    assert result.degraded is True
    assert result.failed_filter == "handoff_gate"
    assert result.reply == render_fallback(None)
    assert result.trace_id == TRACE_ID  # 原 trace_id 透传


def test_gate_fail_closed_on_redis_error() -> None:
    counting = _counting_filter()
    orchestrator = _orchestrator(counting, _RecordingSender(), _FailingReader())

    result = orchestrator.run(_envelope())

    # 与未知状态值同一兜底口径：绝不人机混答。
    assert counting.calls == 0
    assert result.degraded is True
    assert result.failed_filter == "handoff_gate"
    assert result.reply == render_fallback(None)
    assert result.trace_id == TRACE_ID


def test_gate_fail_closed_on_non_redis_error() -> None:
    # Epic 5 code review 补丁：5.5 闸门 catch-Exception 口径的回归锁定——
    # 非 Redis 异常（编程错误等）同样 fail-closed 兜底（spec 5.5 评审
    # 补丁 #1 的拍板行为进网；若未来收窄捕获，本条驱动实现同步改）。
    class _ProgrammingErrorReader:
        def read_handoff_status(self, session_id: str) -> str | None:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")

    counting = _counting_filter()
    orchestrator = _orchestrator(counting, _RecordingSender(), _ProgrammingErrorReader())

    result = orchestrator.run(_envelope())

    assert counting.calls == 0
    assert result.degraded is True
    assert result.failed_filter == "handoff_gate"


def test_gate_backfills_trace_id_when_missing(
    manager: SessionManager, fake_langfuse: Any
) -> None:
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    orchestrator = _orchestrator(_counting_filter(), _RecordingSender(), manager)

    result = orchestrator.run(_envelope(trace_id=None))

    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert re.fullmatch(r"[0-9a-f]{32}", result.trace_id)  # 权威回填口径
    (event,) = [e for e in fake_langfuse.events if e["name"] == "handoff.already_handed_off"]
    assert event["trace_context"]["trace_id"] == result.trace_id


def test_gate_event_record_failure_keeps_reply(
    manager: SessionManager, fake_langfuse: Any
) -> None:
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    fake_langfuse.raise_on_event = True
    counting = _counting_filter()
    orchestrator = _orchestrator(counting, _RecordingSender(), manager)

    result = orchestrator.run(_envelope())

    # 事件记录失败 fail-safe：应答逐字节不变，编排器不抛。
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert result.degraded is False
    assert counting.calls == 0


def test_gate_disabled_without_reader(manager: SessionManager) -> None:
    # 未注入 reader：闸门禁用，即使 Redis 为 triggered 也走正常管道
    # （与 5.5 之前逐字节一致的行为口径）。
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    counting = _counting_filter()
    orchestrator = _orchestrator(counting, _RecordingSender(), None)

    result = orchestrator.run(_envelope())

    assert counting.calls == 1
    assert result.degraded is False
    assert result.reply == render_fallback(None)


def _payload(trace_id: str | None = TRACE_ID) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": SESSION_ID,
        "house_id": "house-001",
        "timestamp": TS_1.isoformat(),
        "message": "房子还有吗",
    }
    if trace_id is not None:
        payload["trace_id"] = trace_id
    return payload


def test_http_two_gates_single_gate_events(
    manager: SessionManager, fake_langfuse: Any
) -> None:
    """HTTP 级双闸门交互（生产接线口径）：适配器闸门先拦截，编排器
    reader 已武装但不再触发——事件零重复、无 handoff_gate 降级、应答
    与 5.3 文案逐字节一致。"""
    manager.mark_handoff(SESSION_ID, conditions=["redline_intent"])
    counting = _counting_filter()
    orchestrator = _orchestrator(counting, _RecordingSender(), manager)
    app = create_app(orchestrator, manager)

    response = TestClient(app).post("/v1/messages", json=_payload())

    body = response.json()
    assert response.status_code == 200
    assert body["reply"] == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert body["degraded"] is False
    assert body["failed_filter"] is None
    assert counting.calls == 0  # 编排器零执行：适配器闸门先行拦截
    already = [
        e for e in fake_langfuse.events if e["name"] == "handoff.already_handed_off"
    ]
    assert len(already) == 1  # 双闸门同消息：事件零重复
    assert not [
        e for e in fake_langfuse.events if e["name"] == "pipeline.delivery"
    ]  # 消息未进管道
    assert not [
        e
        for e in fake_langfuse.events
        if e["name"] == "pipeline.degradation"
        and e["metadata"].get("failed_filter") == "handoff_gate"
    ]  # 无 handoff_gate 降级：适配器闸门非 fail-closed 路径
