"""指代消解过滤器 I/O 矩阵单测（Story 6.1，fakeredis 驱动）。

覆盖 spec I/O & Edge-Case Matrix 全部场景与验收标准：
默认透传（resolved_message 逐字节等于原文、coref == []、审计键 JSON
+ TTL 随会话刷新）、注入 resolver 命中（信封携带钩子产出与 CorefRef
清单、审计写入）、resolver 异常回落透传且买家应答零影响、turn 缺失
跳过审计写（同 _record_followup 先例）、审计写失败 fail-safe + trace
失败事件、空消息/媒体透传、CorefRef 校验（house 必填 entity_id）、
默认链序锁定（coreference 链首，intent_router 之前）、trace 事件
口径（latency_ms / ref_count / audit_written / audit_error / resolver_error）。
"""

import json
from typing import Any, cast
from unittest import mock

import fakeredis
import pytest
import redis
from conftest import _FakeLangfuse
from pydantic import ValidationError

from weiliao_agent.coref import RuleBasedCorefResolver
from weiliao_agent.filters.coreference import CoreferenceFilter
from weiliao_agent.models.coref import CorefRef
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.pipeline import PipelineOrchestrator
from weiliao_agent.session import SessionManager
from weiliao_agent.session.manager import DEFAULT_TTL_SECONDS, KEY_PREFIX

TRACE_ID = "ab" * 16  # 32 位小写十六进制（Langfuse 可记录口径）
SESSION_ID = "sess-001"
HOUSE_A = "house-001"
MESSAGE = "这套房的价格是多少？"


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def manager(redis_client: fakeredis.FakeRedis) -> SessionManager:
    return SessionManager(redis_client)


def _envelope(
    message: str | None = MESSAGE,
    *,
    turn: int | None = 1,
) -> Envelope:
    payload: dict[str, Any] = {
        "session_id": SESSION_ID,
        "house_id": HOUSE_A,
        "timestamp": "2026-11-05T10:00:00+08:00",
        "trace_id": TRACE_ID,
        "message": message,
    }
    if turn is not None:
        payload["turn"] = turn
    return Envelope.model_validate(payload)


def _audit_key(turn: int = 1) -> str:
    return f"{KEY_PREFIX}{SESSION_ID}:coref:{turn}"


# ---- CorefRef 权威模型校验 ----


def test_coref_ref_house_requires_entity_id() -> None:
    # house 类型必填实体锚点：缺省与空串一律校验拒绝（fail-closed）。
    with pytest.raises(ValidationError):
        CorefRef(entity_type="house")
    with pytest.raises(ValidationError):
        CorefRef(entity_type="house", entity_id="")
    ref = CorefRef(entity_type="house", entity_id=HOUSE_A)
    assert ref.entity_id == HOUSE_A


def test_coref_ref_non_house_entity_id_optional() -> None:
    # 价格 / 楼层 / 位置等泛指表达允许缺省锚点。
    assert CorefRef(entity_type="price").entity_id is None
    assert CorefRef(entity_type="floor", entity_id="3").entity_id == "3"
    with pytest.raises(ValidationError):
        CorefRef(entity_type="bogus")  # 未知实体类型 fail-closed


# ---- 默认透传 ----


def test_default_passthrough_resolved_message_byte_identical(
    manager: SessionManager, redis_client: fakeredis.FakeRedis
) -> None:
    # 未注入 resolver：resolved_message 逐字节等于原文、coref == []。
    filter_ = CoreferenceFilter(coref_writer=manager.write_coref)
    envelope = _envelope()

    result = filter_.process(envelope)

    assert result.resolved_message == MESSAGE
    assert result is not envelope  # 新信封产出，非原地改写（model_copy）
    assert result.coref == []
    # 透传也写审计键（设计 Notes：审计轨迹在 6.2 前完整存在）。
    raw = cast("bytes", redis_client.get(_audit_key()))
    stored = json.loads(raw.decode())
    assert stored == {
        "trace_id": TRACE_ID,
        "resolved_message": MESSAGE,
        "coref": [],
    }
    assert redis_client.ttl(_audit_key()) == DEFAULT_TTL_SECONDS


def test_default_passthrough_preserves_other_envelope_fields() -> None:
    filter_ = CoreferenceFilter()
    envelope = _envelope()

    result = filter_.process(envelope)

    assert result.session_id == SESSION_ID
    assert result.house_id == HOUSE_A
    assert result.trace_id == TRACE_ID
    assert result.turn == 1
    assert result.intent is None


def test_empty_and_media_message_pass_through(
    manager: SessionManager, redis_client: fakeredis.FakeRedis
) -> None:
    # message 为空（None / 空串）：透传、coref == []、审计照常写。
    filter_ = CoreferenceFilter(coref_writer=manager.write_coref)

    none_result = filter_.process(_envelope(None))
    empty_result = filter_.process(_envelope(""))

    assert none_result.resolved_message is None
    assert none_result.coref == []
    assert empty_result.resolved_message == ""
    assert empty_result.coref == []
    assert redis_client.get(_audit_key(1)) is not None


def test_whitespace_only_message_passes_through_without_resolver_call(
    manager: SessionManager, redis_client: fakeredis.FakeRedis
) -> None:
    # 纯空白 message 为 truthy：守卫必须 strip 后判定，绝不调用 resolver。
    def resolver(envelope: Envelope) -> tuple[str, list[CorefRef]]:
        msg = "resolver must not be called"
        raise AssertionError(msg)

    filter_ = CoreferenceFilter(
        resolver=resolver, coref_writer=manager.write_coref
    )

    result = filter_.process(_envelope("   "))

    assert result.resolved_message == "   "
    assert result.coref == []
    stored = json.loads(
        cast("bytes", redis_client.get(_audit_key())).decode()
    )
    assert stored["resolved_message"] == "   "


def test_passthrough_without_writer_writes_nothing(
    redis_client: fakeredis.FakeRedis,
) -> None:
    # 未注入 writer（编排器默认链口径）：透传、零 Redis 访问。
    filter_ = CoreferenceFilter()
    result = filter_.process(_envelope())

    assert result.coref == []
    assert redis_client.keys() == []


# ---- 注入 resolver 命中 ----


def test_injected_resolver_output_attached_and_audited(
    manager: SessionManager, redis_client: fakeredis.FakeRedis
) -> None:
    refs = [
        CorefRef(entity_type="house", entity_id=HOUSE_A),
        CorefRef(entity_type="price"),
    ]

    def resolver(envelope: Envelope) -> tuple[str, list[CorefRef]]:
        assert envelope.message == MESSAGE
        return "house-001 的价格是多少？", refs

    filter_ = CoreferenceFilter(
        resolver=resolver, coref_writer=manager.write_coref
    )

    result = filter_.process(_envelope())

    assert result.resolved_message == "house-001 的价格是多少？"
    assert result.coref == refs
    raw = cast("bytes", redis_client.get(_audit_key()))
    stored = json.loads(raw.decode())
    assert stored == {
        "trace_id": TRACE_ID,
        "resolved_message": "house-001 的价格是多少？",
        "coref": [
            {"entity_type": "house", "entity_id": HOUSE_A},
            {"entity_type": "price", "entity_id": None},
        ],
    }
    assert redis_client.ttl(_audit_key()) == DEFAULT_TTL_SECONDS


# ---- resolver 异常回落透传 ----


def test_resolver_returning_invalid_refs_falls_back_to_passthrough(
    manager: SessionManager,
    redis_client: fakeredis.FakeRedis,
    fake_langfuse: _FakeLangfuse,
) -> None:
    # refs 复核：model_construct 绕校验的非法 CorefRef（house 无锚点）
    # 在 resolver 调用的同一 try 内拒绝，回落透传——process 不抛错。
    invalid = CorefRef.model_construct(entity_type="house", entity_id=None)

    def resolver(envelope: Envelope) -> tuple[str, list[CorefRef]]:
        return "消解后文本", [invalid]

    filter_ = CoreferenceFilter(
        resolver=resolver, coref_writer=manager.write_coref
    )

    result = filter_.process(_envelope())

    assert result.resolved_message == MESSAGE  # 回落原文
    assert result.coref == []
    stored = json.loads(
        cast("bytes", redis_client.get(_audit_key())).decode()
    )
    assert stored["resolved_message"] == MESSAGE
    assert stored["coref"] == []
    event = next(
        e for e in fake_langfuse.events if e["name"] == "coref.resolution"
    )
    assert event["metadata"]["ref_count"] == 0
    assert event["metadata"]["resolver_error"] is True  # 复核失败回落归因


def test_resolver_exception_falls_back_to_passthrough(
    manager: SessionManager,
    redis_client: fakeredis.FakeRedis,
    fake_langfuse: _FakeLangfuse,
) -> None:
    def resolver(envelope: Envelope) -> tuple[str, list[CorefRef]]:
        msg = "resolver boom"
        raise RuntimeError(msg)

    filter_ = CoreferenceFilter(
        resolver=resolver, coref_writer=manager.write_coref
    )

    result = filter_.process(_envelope())

    # 回落透传：买家应答零影响（下游按原文消费），审计以回落值落盘。
    assert result.resolved_message == MESSAGE
    assert result.coref == []
    stored = json.loads(
        cast("bytes", redis_client.get(_audit_key())).decode()
    )
    assert stored == {
        "trace_id": TRACE_ID,
        "resolved_message": MESSAGE,
        "coref": [],
    }
    event = next(
        e for e in fake_langfuse.events if e["name"] == "coref.resolution"
    )
    assert event["metadata"]["ref_count"] == 0  # 回落可观测
    assert event["metadata"]["resolver_error"] is True  # 消解持续失败观测
    assert event["metadata"]["audit_written"] is True
    assert event["metadata"]["audit_error"] is False


# ---- turn 缺失：跳过审计写 ----


def test_turn_none_skips_audit_write_but_produces_envelope(
    manager: SessionManager, redis_client: fakeredis.FakeRedis
) -> None:
    # 同 _record_followup 先例：turn 为 None 跳过审计写，信封照常产出。
    filter_ = CoreferenceFilter(coref_writer=manager.write_coref)

    result = filter_.process(_envelope(turn=None))

    assert result.resolved_message == MESSAGE
    assert result.coref == []
    assert redis_client.keys() == []


# ---- 审计写失败 fail-safe ----


def test_audit_write_failure_is_fail_safe_with_trace_event(
    manager: SessionManager,
    redis_client: fakeredis.FakeRedis,
    fake_langfuse: _FakeLangfuse,
) -> None:
    filter_ = CoreferenceFilter(coref_writer=manager.write_coref)

    with mock.patch.object(
        redis_client, "set", side_effect=redis.RedisError("connection boom")
    ):
        result = filter_.process(_envelope())

    # 信封照常下发、过滤器不抛、买家应答零影响；trace 记失败事件。
    assert result.resolved_message == MESSAGE
    assert result.coref == []
    assert redis_client.keys() == []
    event = next(
        e for e in fake_langfuse.events if e["name"] == "coref.resolution"
    )
    assert event["metadata"]["audit_written"] is False
    assert event["metadata"]["audit_error"] is True  # 故障归因（需告警）


# ---- trace 事件口径 ----


def test_record_coref_event_carries_latency_and_counts(
    manager: SessionManager, fake_langfuse: _FakeLangfuse
) -> None:
    refs = [CorefRef(entity_type="location", entity_id="望京")]
    filter_ = CoreferenceFilter(
        resolver=lambda envelope: ("去望京怎么走", refs),
        coref_writer=manager.write_coref,
    )

    filter_.process(_envelope())

    event = next(
        e for e in fake_langfuse.events if e["name"] == "coref.resolution"
    )
    assert event["metadata"]["event"] == "coref_resolution"
    assert event["metadata"]["ref_count"] == 1
    assert event["metadata"]["resolver_error"] is False
    assert event["metadata"]["audit_written"] is True
    assert event["metadata"]["latency_ms"] >= 0.0


def test_record_coref_no_trace_id_is_noop(fake_langfuse: _FakeLangfuse) -> None:
    # trace_id 缺失（编排器入口回填前）：no-op 零事件，过滤器照常产出。
    filter_ = CoreferenceFilter()
    envelope = _envelope().model_copy(update={"trace_id": None})

    result = filter_.process(envelope)

    assert result.coref == []
    assert fake_langfuse.events == []


# ---- 链序锁定 ----


def test_coreference_filter_is_chain_head_before_intent_router() -> None:
    # AC 第 1 条：coreference 在链首执行，位于 intent_router 之前。
    orchestrator = PipelineOrchestrator(filters=None)
    assert orchestrator.filter_names == (
        "coreference",
        "intent_router",
        "cache_lookup",
        "llm_generate",
        "risk_review",
        "response_assembler",
    )


# ---- Story 6.2 集成：真实 resolver 注入过滤器（钩子接线验证） ----


def test_real_rule_based_resolver_xiaowang_scenario_end_to_end(
    manager: SessionManager, redis_client: fakeredis.FakeRedis
) -> None:
    # 小王场景全链路：真实 RuleBasedCorefResolver 注入过滤器——买家
    # 原文 "它采光怎么样" 产出消解后文本 + 单条 house 引用，审计
    # payload 形状与信封 coref 一致（钩子与默认实现接线验证）。
    filter_ = CoreferenceFilter(
        resolver=RuleBasedCorefResolver(), coref_writer=manager.write_coref
    )
    envelope = _envelope("它采光怎么样")

    result = filter_.process(envelope)

    expected_resolved = "这套房（house_id: house-001）采光怎么样"
    assert result.resolved_message == expected_resolved
    assert result.coref == [CorefRef(entity_type="house", entity_id=HOUSE_A)]
    raw = cast("bytes", redis_client.get(_audit_key()))
    stored = json.loads(raw.decode())
    assert stored == {
        "trace_id": TRACE_ID,
        "resolved_message": expected_resolved,
        "coref": [{"entity_type": "house", "entity_id": HOUSE_A}],
    }
    assert redis_client.ttl(_audit_key()) == DEFAULT_TTL_SECONDS
