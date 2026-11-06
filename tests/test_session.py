"""会话状态单写者 I/O 矩阵单测（Story 1.3，fakeredis 驱动）。

覆盖 spec I/O & Edge-Case Matrix：
首条消息（初始化 turn=1）/ 连续消息（自增 + TTL 刷新）/ 并发 begin_turn
（turn 唯一）/ 过期重开（从零初始化）/ 并发 increment_followup（计数精确）
/ Redis 故障（端点层兜底，非 5xx）。
"""

import importlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest import mock

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.classifiers.llm_zero_shot import LlmZeroShotClassifier, _LlmIntentOutput
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX
from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.session import SessionManager, SessionSnapshot
from weiliao_agent.session.manager import DEFAULT_TTL_SECONDS, KEY_PREFIX


@pytest.fixture
def _restore_main_module() -> Any:
    """reload 类测试的清理：弹出 main 模块，防 FakeRedis 绑定的过期
    状态（app/_classifier）被后续导入该模块的测试读到。"""
    yield
    sys.modules.pop("weiliao_agent.main", None)

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


def test_main_composition_root_wires_session_manager(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
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


def test_write_intent_stores_per_turn_json_with_session_ttl(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    from weiliao_agent.models.intent import IntentClassification

    manager.begin_turn(SESSION_ID, TS_1)

    manager.write_intent(SESSION_ID, 1, IntentClassification(tag="PRICE_INQUIRY", confidence=0.9))

    key = f"{KEY_PREFIX}{SESSION_ID}:intent:1"
    raw = cast("bytes", client.get(key))
    stored = json.loads(raw.decode())
    assert stored == {"tag": "PRICE_INQUIRY", "confidence": 0.9}
    # 序列化口径即 model_dump_json()，且 TTL 与会话一致。
    assert client.ttl(key) == DEFAULT_TTL_SECONDS
    assert client.ttl(f"{KEY_PREFIX}{SESSION_ID}") == DEFAULT_TTL_SECONDS


def test_write_intent_uses_distinct_key_per_turn(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    from weiliao_agent.models.intent import IntentClassification

    manager.write_intent(SESSION_ID, 1, IntentClassification(tag="GREETING", confidence=0.9))
    manager.write_intent(SESSION_ID, 2, IntentClassification(tag="LOCATION", confidence=0.7))

    first = json.loads(cast("bytes", client.get(f"{KEY_PREFIX}{SESSION_ID}:intent:1")).decode())
    second = json.loads(cast("bytes", client.get(f"{KEY_PREFIX}{SESSION_ID}:intent:2")).decode())
    assert first["tag"] == "GREETING"
    assert second["tag"] == "LOCATION"


def test_write_intent_does_not_touch_session_hash(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # per-turn 键独立于会话 hash：意图写入不得残留/改写 turn 等权威字段。
    from weiliao_agent.models.intent import IntentClassification

    manager.write_intent(SESSION_ID, 3, IntentClassification(tag="UNKNOWN", confidence=0.5))

    assert client.hgetall(f"{KEY_PREFIX}{SESSION_ID}") == {}
    assert client.get(f"{KEY_PREFIX}{SESSION_ID}:intent:3") is not None


def test_record_followup_first_occurrence_does_not_increment_session_count(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    manager.begin_turn(SESSION_ID, TS_1)

    per_intent, session_count = manager.record_followup(SESSION_ID, "PRICE_INQUIRY")

    assert (per_intent, session_count) == (1, 0)  # 首次出现非追问，总数不自增
    key = f"{KEY_PREFIX}{SESSION_ID}"
    assert client.hget(key, "followup:PRICE_INQUIRY") == b"1"
    assert client.hget(key, "followup_count") == b"0"
    assert client.ttl(key) == DEFAULT_TTL_SECONDS  # EXPIRE 随脚本刷新


def test_record_followup_second_occurrence_marks_followup(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    manager.begin_turn(SESSION_ID, TS_1)
    manager.record_followup(SESSION_ID, "PRICE_INQUIRY")

    per_intent, session_count = manager.record_followup(SESSION_ID, "PRICE_INQUIRY")

    assert (per_intent, session_count) == (2, 1)  # 追问 #1：per-intent ≥2 命中
    key = f"{KEY_PREFIX}{SESSION_ID}"
    assert client.hget(key, "followup:PRICE_INQUIRY") == b"2"
    assert client.hget(key, "followup_count") == b"1"


def test_record_followup_same_tag_reaches_threshold_at_fourth_message(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # 语义钉死：追问 #3 = 同意图第 4 次消息（≥3 信号触发时刻）。
    manager.begin_turn(SESSION_ID, TS_1)
    assert manager.record_followup(SESSION_ID, "PRICE_INQUIRY") == (1, 0)
    assert manager.record_followup(SESSION_ID, "PRICE_INQUIRY") == (2, 1)
    assert manager.record_followup(SESSION_ID, "PRICE_INQUIRY") == (3, 2)
    assert manager.record_followup(SESSION_ID, "PRICE_INQUIRY") == (4, 3)
    assert client.hget(f"{KEY_PREFIX}{SESSION_ID}", "followup_count") == b"3"


def test_record_followup_counts_across_intents(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # 「≥3 无论意图类型」：跨意图累计第 3 次追问命中（各意图第 2 次）。
    manager.begin_turn(SESSION_ID, TS_1)
    manager.record_followup(SESSION_ID, "PRICE_INQUIRY")
    manager.record_followup(SESSION_ID, "AVAILABILITY")
    manager.record_followup(SESSION_ID, "LOCATION")

    assert manager.record_followup(SESSION_ID, "PRICE_INQUIRY") == (2, 1)
    assert manager.record_followup(SESSION_ID, "AVAILABILITY") == (2, 2)
    assert manager.record_followup(SESSION_ID, "LOCATION") == (2, 3)
    key = f"{KEY_PREFIX}{SESSION_ID}"
    assert client.hget(key, "followup:PRICE_INQUIRY") == b"2"
    assert client.hget(key, "followup:AVAILABILITY") == b"2"
    assert client.hget(key, "followup:LOCATION") == b"2"
    assert client.hget(key, "followup_count") == b"3"


def test_record_followup_isolates_per_intent_counters(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    manager.begin_turn(SESSION_ID, TS_1)
    manager.record_followup(SESSION_ID, "PRICE_INQUIRY")
    manager.record_followup(SESSION_ID, "PRICE_INQUIRY")

    per_intent, session_count = manager.record_followup(SESSION_ID, "GREETING")

    # GREETING 首次出现：per-intent 独立计数，会话总数只计 PRICE_INQUIRY 的命中。
    assert (per_intent, session_count) == (1, 1)
    key = f"{KEY_PREFIX}{SESSION_ID}"
    assert client.hget(key, "followup:GREETING") == b"1"
    assert client.hget(key, "followup:PRICE_INQUIRY") == b"2"


def test_record_followup_refreshes_ttl(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    manager.begin_turn(SESSION_ID, TS_1)
    client.expire(f"{KEY_PREFIX}{SESSION_ID}", 60)  # 模拟中途 TTL 被压短

    manager.record_followup(SESSION_ID, "PRICE_INQUIRY")

    assert client.ttl(f"{KEY_PREFIX}{SESSION_ID}") == DEFAULT_TTL_SECONDS


def test_record_followup_before_begin_turn_preserves_counts(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # record_followup 先执行（hash 已存在但无 turn 字段）：begin_turn
    # 不得误判为新会话而把 per-intent / followup_count 清零。
    manager.record_followup(SESSION_ID, "PRICE_INQUIRY")
    manager.record_followup(SESSION_ID, "PRICE_INQUIRY")

    snapshot = manager.begin_turn(SESSION_ID, TS_1)

    assert snapshot.turn == 1
    assert snapshot.followup_count == 1
    key = f"{KEY_PREFIX}{SESSION_ID}"
    assert client.hget(key, "followup:PRICE_INQUIRY") == b"2"


def test_concurrent_record_followup_counts_exactly(
    manager: SessionManager, client: fakeredis.FakeRedis
) -> None:
    # 并发同 session 同 intent：per-intent 与会话总数均精确不丢不重。
    manager.begin_turn(SESSION_ID, TS_1)
    threads = 50
    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(
            pool.map(
                lambda _i: manager.record_followup(SESSION_ID, "PRICE_INQUIRY"),
                range(threads),
            )
        )

    per_intents = sorted(r[0] for r in results)
    assert per_intents == list(range(1, threads + 1))
    assert all(r[1] == r[0] - 1 for r in results)  # 会话总数 = per-intent - 1
    key = f"{KEY_PREFIX}{SESSION_ID}"
    assert client.hget(key, "followup:PRICE_INQUIRY") == str(threads).encode()
    assert client.hget(key, "followup_count") == str(threads - 1).encode()


def test_main_composition_root_rejects_unknown_classifier(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
    # 非法配置值 fail-fast：组合根导入期拒绝启动。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.setenv("WEILIAO_INTENT_CLASSIFIER", "rules_engine")

    with pytest.raises(ValueError, match="unknown intent classifier"):
        importlib.reload(importlib.import_module("weiliao_agent.main"))


def test_main_composition_root_switch_to_none_classifier(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
    # 配置切换路由链路零改动：none 装配占位过滤器，消息正常出管道
    # （意图置 UNKNOWN），turn 仍由组合根 SessionManager 回填。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.setenv("WEILIAO_INTENT_CLASSIFIER", "none")
    main_module = importlib.reload(importlib.import_module("weiliao_agent.main"))

    http = TestClient(main_module.app)
    response = http.post("/v1/messages", json=_valid_payload())

    assert response.status_code == 200
    assert response.json()["degraded"] is False
    stored_turn = fake.hget(f"{KEY_PREFIX}{SESSION_ID}", "turn")
    assert int(cast("bytes", stored_turn)) == 1


def test_main_composition_root_defaults_to_llm_classifier(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
    # env 未设（或空串）时组合根装配默认 LLM zero-shot 实现——锁定
    # main.py 的默认接线不被误删/误改。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.delenv("WEILIAO_INTENT_CLASSIFIER", raising=False)
    main_module = importlib.reload(importlib.import_module("weiliao_agent.main"))

    assert isinstance(main_module._classifier, LlmZeroShotClassifier)


class _FakeStructuredModel:
    """``with_structured_output`` 同接口替身：返回固定结构化输出。"""

    def __init__(self, output: _LlmIntentOutput) -> None:
        self._output = output

    def invoke(self, messages: Any) -> _LlmIntentOutput:
        return self._output


def test_main_composition_root_writes_intent_to_redis(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
    # 默认链路端到端：分类结果经组合根注入的 write_intent 落盘 per-turn
    # 键，JSON 含 tag 与 confidence（fakeredis 无 LLM 网络）。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.delenv("WEILIAO_INTENT_CLASSIFIER", raising=False)
    main_module = importlib.reload(importlib.import_module("weiliao_agent.main"))
    classifier = main_module._classifier
    assert isinstance(classifier, LlmZeroShotClassifier)
    classifier._structured_model = _FakeStructuredModel(
        _LlmIntentOutput(tag="PRICE_INQUIRY", confidence=0.91)
    )
    http = TestClient(main_module.app)
    payload = {**_valid_payload(), "message": "你好，这套房多少钱？"}

    response = http.post("/v1/messages", json=payload)

    assert response.status_code == 200
    assert response.json()["degraded"] is False
    raw = fake.get(f"{KEY_PREFIX}{SESSION_ID}:intent:1")
    assert raw is not None
    stored = json.loads(cast("bytes", raw).decode())
    assert stored == {"tag": "PRICE_INQUIRY", "confidence": 0.91}


def test_main_composition_root_records_followup_to_redis(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
    # 默认链路端到端（Story 2.3 验收）：同一 session 同一意图第 2 条消息
    # 经组合根注入的 record_followup 落盘——followup:TAG=2、followup_count=1。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.delenv("WEILIAO_INTENT_CLASSIFIER", raising=False)
    main_module = importlib.reload(importlib.import_module("weiliao_agent.main"))
    classifier = main_module._classifier
    assert isinstance(classifier, LlmZeroShotClassifier)
    classifier._structured_model = _FakeStructuredModel(
        _LlmIntentOutput(tag="PRICE_INQUIRY", confidence=0.91)
    )
    http = TestClient(main_module.app)
    payload = {**_valid_payload(), "message": "你好，这套房多少钱？"}

    first = http.post("/v1/messages", json=payload)
    second = http.post(
        "/v1/messages",
        json={**payload, "timestamp": TS_2.isoformat()},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    key = f"{KEY_PREFIX}{SESSION_ID}"
    assert fake.hget(key, "followup:PRICE_INQUIRY") == b"2"
    assert fake.hget(key, "followup_count") == b"1"
    assert fake.ttl(key) == DEFAULT_TTL_SECONDS


def test_main_composition_root_wires_l1_writers_and_l0_lookup(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
    # Story 3.3 组合根冒烟：一次 POST 同时验证 (a) intent_router 经注入的
    # L1 writer 落最近意图快照、(b) cache_lookup 持有的 L0 客户端即组合根
    # 模块级实例（spy 其 get——撤销接线时本断言即红，防 Story 行为静默
    # 不在生产运行）。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.delenv("WEILIAO_INTENT_CLASSIFIER", raising=False)
    main_module = importlib.reload(importlib.import_module("weiliao_agent.main"))
    classifier = main_module._classifier
    assert isinstance(classifier, LlmZeroShotClassifier)
    classifier._structured_model = _FakeStructuredModel(
        _LlmIntentOutput(tag="PRICE_INQUIRY", confidence=0.91)
    )
    card = FactCard.model_validate(
        {
            "house_id": "house-001",
            "status": "active",
            "price_bucket": 12,
            "fact_version": 1,
            "as_of": TS_1,
        }
    )
    assert main_module._l0_cache.set("PRICE_INQUIRY", card) is True
    http = TestClient(main_module.app)
    payload = {**_valid_payload(), "message": "你好，这套房多少钱？"}

    with mock.patch.object(
        main_module._l0_cache, "get", wraps=main_module._l0_cache.get
    ) as get_spy:
        response = http.post("/v1/messages", json=payload)

    assert response.status_code == 200
    assert response.json()["degraded"] is False
    # (a) L1 最近意图快照：分类成功后组合根注入的 writer 已写入。
    assert main_module._l1_cache.get_recent_intent(SESSION_ID) == "PRICE_INQUIRY"
    # (b) cache_lookup 热路径真实经过模块级 _l0_cache（预置卡片命中）。
    assert get_spy.call_count == 1


def test_main_l1_ttl_above_30_fails_fast_at_import(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any
) -> None:
    # 配置错误拒绝启动：WEILIAO_L1_TTL_SECONDS=31 越 AD-5 硬上限，
    # reload 组合根即抛 ValueError（import-time fail-fast）。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.setenv("WEILIAO_L1_TTL_SECONDS", "31")
    with pytest.raises(ValueError, match="hard limit"):
        importlib.reload(importlib.import_module("weiliao_agent.main"))


@pytest.mark.parametrize("raw", ["0", "-5", "abc"])
def test_main_metrics_log_seconds_invalid_fails_fast_at_import(
    monkeypatch: pytest.MonkeyPatch, _restore_main_module: Any, raw: str
) -> None:
    # 配置错误拒绝启动：WEILIAO_METRICS_LOG_SECONDS 非正整数（含非
    # 整数），reload 组合根即抛 ValueError，消息带变量名（A 方案定时
    # 日志间隔，缺省 60）。
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.setenv("WEILIAO_METRICS_LOG_SECONDS", raw)
    with pytest.raises(ValueError, match="WEILIAO_METRICS_LOG_SECONDS"):
        importlib.reload(importlib.import_module("weiliao_agent.main"))
