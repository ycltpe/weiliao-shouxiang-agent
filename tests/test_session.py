"""会话状态单写者 I/O 矩阵单测（Story 1.3，fakeredis 驱动）。

覆盖 spec I/O & Edge-Case Matrix：
首条消息（初始化 turn=1）/ 连续消息（自增 + TTL 刷新）/ 并发 begin_turn
（turn 唯一）/ 过期重开（从零初始化）/ 并发 increment_followup（计数精确）
/ Redis 故障（端点层兜底，非 5xx）。
"""

import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX
from weiliao_agent.session import SessionManager, SessionSnapshot
from weiliao_agent.session.manager import DEFAULT_TTL_SECONDS, KEY_PREFIX

TS_1 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
TS_2 = datetime(2026, 10, 31, 10, 1, 0, tzinfo=UTC)
SESSION_ID = "sess-001"


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def manager(client: fakeredis.FakeRedis) -> SessionManager:
    return SessionManager(client)


def _valid_payload() -> dict[str, Any]:
    return {
        "session_id": SESSION_ID,
        "house_id": "house-001",
        "timestamp": TS_1.isoformat(),
        "trace_id": "trace-001",
    }


def test_first_message_initializes_state_with_turn_one(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    snapshot = manager.begin_turn(SESSION_ID, TS_1)

    assert snapshot == SessionSnapshot(
        turn=1, followup_count=0, handoff_status="none", last_message_at=TS_1
    )
    assert client.ttl(f"{KEY_PREFIX}{SESSION_ID}") == DEFAULT_TTL_SECONDS


def test_consecutive_messages_increment_turn_and_refresh_ttl(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    manager.begin_turn(SESSION_ID, TS_1)
    client.expire(f"{KEY_PREFIX}{SESSION_ID}", 60)  # 模拟中途 TTL 被压短

    snapshot = manager.begin_turn(SESSION_ID, TS_2)

    assert snapshot.turn == 2
    assert snapshot.followup_count == 0
    assert snapshot.last_message_at == TS_2
    assert client.ttl(f"{KEY_PREFIX}{SESSION_ID}") == DEFAULT_TTL_SECONDS  # TTL 刷新


def test_expired_session_reinitializes_from_zero(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    first = manager.begin_turn(SESSION_ID, TS_1)
    manager.increment_followup(SESSION_ID)
    client.delete(f"{KEY_PREFIX}{SESSION_ID}")  # TTL 到期，key 被清除

    second = manager.begin_turn(SESSION_ID, TS_2)

    assert first.turn == 1
    assert second.turn == 1  # 从零重新开始，无残留
    assert second.followup_count == 0


def test_read_snapshot_returns_none_for_unknown_session(manager: SessionManager) -> None:
    assert manager.read_snapshot("no-such-session") is None


def test_read_snapshot_reflects_written_state(manager: SessionManager) -> None:
    manager.begin_turn(SESSION_ID, TS_1)
    manager.increment_followup(SESSION_ID)

    snapshot = manager.read_snapshot(SESSION_ID)

    assert snapshot is not None
    assert snapshot.turn == 1
    assert snapshot.followup_count == 1
    assert snapshot.handoff_status == "none"
    assert snapshot.last_message_at == TS_1


def test_concurrent_begin_turn_yields_unique_turns(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # fakeredis 服务端语义串行执行 Lua，并发提交验证 turn 唯一且不丢序。
    threads = 25
    with ThreadPoolExecutor(max_workers=threads) as pool:
        turns = list(
            pool.map(
                lambda i: manager.begin_turn(SESSION_ID, TS_1 + timedelta(seconds=i)).turn,
                range(threads),
            )
        )

    assert sorted(turns) == list(range(1, threads + 1))
    snapshot = manager.read_snapshot(SESSION_ID)
    assert snapshot is not None
    assert snapshot.turn == threads


def test_concurrent_increment_followup_counts_exactly(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    manager.begin_turn(SESSION_ID, TS_1)
    client.expire(f"{KEY_PREFIX}{SESSION_ID}", 60)  # 验证每次自增都刷新 TTL
    threads = 50
    with ThreadPoolExecutor(max_workers=threads) as pool:
        counts = list(pool.map(lambda _i: manager.increment_followup(SESSION_ID), range(threads)))

    assert sorted(counts) == list(range(1, threads + 1))  # 各次返回值互不重复
    snapshot = manager.read_snapshot(SESSION_ID)
    assert snapshot is not None
    assert snapshot.followup_count == threads
    assert client.ttl(f"{KEY_PREFIX}{SESSION_ID}") == DEFAULT_TTL_SECONDS


def test_increment_before_begin_turn_preserves_followup_count(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # increment_followup 先执行：hash 已存在但无 turn 字段。begin_turn
    # 不得误判为新会话而把已有计数清零。
    manager.increment_followup(SESSION_ID)
    manager.increment_followup(SESSION_ID)

    snapshot = manager.begin_turn(SESSION_ID, TS_1)

    assert snapshot.turn == 1
    assert snapshot.followup_count == 2


def test_begin_turn_backfills_only_missing_fields(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # 不完整 hash（有 turn / followup_count，缺 handoff_status /
    # last_message_at）：仅补缺失字段，已有值保留。
    key = f"{KEY_PREFIX}{SESSION_ID}"
    client.hset(key, mapping={"turn": 5, "followup_count": 3})

    snapshot = manager.begin_turn(SESSION_ID, TS_1)

    assert snapshot.turn == 6
    assert snapshot.followup_count == 3
    assert snapshot.handoff_status == "none"
    assert snapshot.last_message_at == TS_1


def test_read_snapshot_returns_none_for_incomplete_hash(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # 缺 turn / last_message_at 的 hash 不是合法会话快照：返回 None。
    client.hset(f"{KEY_PREFIX}{SESSION_ID}", "followup_count", 3)

    assert manager.read_snapshot(SESSION_ID) is None


class _FailingSessionManager:
    """begin_turn 抛 redis 连接异常的替身，模拟 Redis 故障 fail-closed。"""

    def begin_turn(self, session_id: str, timestamp: datetime) -> SessionSnapshot:
        import redis

        msg = "Connection refused"
        raise redis.ConnectionError(msg)


def test_endpoint_falls_back_when_redis_is_down() -> None:
    app = create_app(session_manager=_FailingSessionManager())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/v1/messages", json=_valid_payload())

    assert response.status_code == 200  # 非 5xx
    body = response.json()
    assert body["degraded"] is True
    assert body["trace_id"] == "trace-001"  # 兜底仍携带原 trace_id
    assert body["reply"].startswith(AI_DISCLOSURE_PREFIX)


def test_endpoint_backfills_turn_before_pipeline(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    from weiliao_agent.models.envelope import Envelope
    from weiliao_agent.pipeline import PipelineOrchestrator

    seen_turns: list[int] = []

    class _TurnProbe:
        """透传过滤器，记录入管道时的 turn（入管道前赋值落点）。"""

        name = "turn_probe"

        def process(self, envelope: Envelope) -> Envelope:
            seen_turns.append(envelope.turn)
            return envelope

    app = create_app(
        orchestrator=PipelineOrchestrator(filters=[_TurnProbe()]),
        session_manager=manager,
    )
    http = TestClient(app)

    first = http.post("/v1/messages", json=_valid_payload())
    second = http.post(
        "/v1/messages",
        json={**_valid_payload(), "timestamp": TS_2.isoformat(), "turn": 99},
    )

    assert first.status_code == 200
    assert first.json()["degraded"] is False
    assert second.status_code == 200
    assert second.json()["degraded"] is False
    # 两条消息入管道前均获服务端快照回填：turn 单调唯一，客户端自带
    # 值（99）被覆盖而非透传。
    assert seen_turns == [1, 2]


def test_main_composition_root_wires_session_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    # 组合根冒烟：main.py 是生产唯一注入 SessionManager 的位置。patch
    # Redis.from_url 为 fakeredis 后导入 main，端点须完成 turn 回填。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    import weiliao_agent.main as main_module

    importlib.reload(main_module)

    http = TestClient(main_module.app)
    response = http.post("/v1/messages", json=_valid_payload())

    assert response.status_code == 200
    assert response.json()["degraded"] is False
    stored_turn = fake.hget(f"{KEY_PREFIX}{SESSION_ID}", "turn")
    assert int(cast("bytes", stored_turn)) == 1  # turn 已由组合根的 SessionManager 写入


def test_in_flight_message_continues_with_snapshot_after_key_expiry(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    """矩阵行「过期瞬间在途」：入管道后 key 过期，按入管道快照继续，不中断。"""
    from weiliao_agent.models.envelope import Envelope
    from weiliao_agent.pipeline import PipelineOrchestrator

    snapshot = manager.begin_turn(SESSION_ID, TS_1)
    client.delete(f"{KEY_PREFIX}{SESSION_ID}")  # 模拟处理中途 key 被 TTL 清除

    envelope = Envelope.model_validate(_valid_payload())
    envelope.turn = snapshot.turn
    result = PipelineOrchestrator().run(envelope)

    assert envelope.turn == 1  # 在途消息持有入管道时快照，不随 key 过期而失效
    assert result.degraded is False  # 处理不中断，正常出管道
