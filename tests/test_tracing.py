"""Langfuse 追踪脚手架单测（Story 1.5）。

覆盖 spec I/O & Edge-Case Matrix 四行：
- 正常消息：trace_id 服务端生成回填、响应携带、过滤器 span 记录；
- Langfuse 未配置：disabled 冒烟，行为与 Story 1.4 一致；
- 过滤器异常降级：trace_id 不变、降级事件含归因过滤器名；
- SDK 调用抛错：注入抛错 client，管道结果不受影响。
外加成本追踪封装（Epic 4 消费面）与最终下发文本记录。
"""

import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.models.envelope import Envelope, HandoffAssessment
from weiliao_agent.observability import tracing
from weiliao_agent.pipeline import HANDOFF_REPLY_TEXT, PipelineOrchestrator
from weiliao_agent.session import SessionSnapshot

_TRACE_ID_HEX = re.compile(r"^[0-9a-f]{32}$")


class _FakeSpan:
    """最小 span 替身：只实现脚手架会调用的 update。"""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class _FakeObservationCM:
    """最小上下文管理器替身：记录 enter/exit 与构造参数。"""

    def __init__(self, client: "_FakeLangfuse", kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.entered = False
        self.exited = False
        self.span = _FakeSpan()
        self._client = client

    def __enter__(self) -> _FakeSpan:
        if self._client.raise_on_enter:
            msg = "sdk enter boom"
            raise RuntimeError(msg)
        self.entered = True
        self._client.entered_spans.append(self)
        return self.span

    def __exit__(self, *exc_info: Any) -> bool:
        self.exited = True
        return False


class _FakeLangfuse:
    """Langfuse client 替身：记录全部观测调用，可按需抛错。"""

    def __init__(self, *, raise_on_start: bool = False, raise_on_enter: bool = False) -> None:
        self.raise_on_start = raise_on_start
        self.raise_on_enter = raise_on_enter
        self.observations: list[dict[str, Any]] = []
        self.entered_spans: list[_FakeObservationCM] = []
        self.events: list[dict[str, Any]] = []
        # code review 补丁：替身补 update_current_span，使 record_delivery
        # 的根 span metadata 同步路径在测试中被真实执行并断言。
        self.span_updates: list[dict[str, Any]] = []

    def start_as_current_observation(self, **kwargs: Any) -> _FakeObservationCM:
        self.observations.append(kwargs)
        if self.raise_on_start:
            msg = "sdk start boom"
            raise RuntimeError(msg)
        return _FakeObservationCM(self, kwargs)

    def create_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def update_current_span(self, **kwargs: Any) -> None:
        self.span_updates.append(kwargs)


@pytest.fixture
def fake_langfuse() -> Iterator[_FakeLangfuse]:
    client = _FakeLangfuse()
    tracing.set_langfuse_client(client)
    yield client
    tracing.set_langfuse_client(None)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": "sess-001",
        "house_id": "house-001",
        "timestamp": "2026-10-31T10:00:00+08:00",
        "trace_id": "ab" * 16,
        "intent": "PRICE_INQUIRY",
        "message": "这套房多少钱？",
    }
    payload.update(overrides)
    return payload


class _PassThroughFilter(Filter):
    def __init__(self, name: str) -> None:
        self.name = name

    def process(self, envelope: Envelope) -> Envelope:
        return envelope


class _RaisingFilter(Filter):
    name = "cache_lookup"

    def process(self, envelope: Envelope) -> Envelope:
        msg = "boom"
        raise RuntimeError(msg)


class _SleepingFilter(Filter):
    """睡满预算的过滤器：触发 FutureTimeoutError 超时降级（slow_filter）。"""

    name = "risk_review"

    def process(self, envelope: Envelope) -> Envelope:
        import time

        time.sleep(1.0)
        return envelope


class _FailingSessionManager:
    """begin_turn 抛 redis 连接异常的替身，模拟 Redis 故障 fail-closed。"""

    def begin_turn(self, session_id: str, timestamp: Any) -> SessionSnapshot:
        import redis

        msg = "Connection refused"
        raise redis.ConnectionError(msg)


def _four_pass_through_filters() -> list[Filter]:
    return [
        _PassThroughFilter("intent_router"),
        _PassThroughFilter("cache_lookup"),
        _PassThroughFilter("risk_review"),
        _PassThroughFilter("response_assembler"),
    ]


def test_missing_trace_id_backfilled_and_returned(fake_langfuse: _FakeLangfuse) -> None:
    """矩阵行 1：trace_id 缺失 → 服务端生成回填；响应携带；全链路同一 trace。"""
    orchestrator = PipelineOrchestrator(filters=_four_pass_through_filters())
    envelope = Envelope.model_validate(_payload(trace_id=None))

    result = orchestrator.run(envelope)

    assert result.degraded is False
    assert result.trace_id is not None
    assert _TRACE_ID_HEX.fullmatch(result.trace_id)  # 服务端生成（Langfuse 口径）
    # Story 7.2（D3 frozen 所有权裁断）：回填不再原地变更调用方信封——
    # 调用方持有的信封保持 None（frozen，model_copy 产出内部副本），
    # 响应 trace_id 即权威生成值（客户端契约不变）。
    assert envelope.trace_id is None
    assert result.trace_id is not None
    # 消息级根 span + 4 个过滤器 span，全部挂在同一 trace。
    names = [obs["name"] for obs in fake_langfuse.observations]
    assert "pipeline.message" in names
    assert names.count("filter:intent_router") == 1
    assert names.count("filter:cache_lookup") == 1
    assert names.count("filter:risk_review") == 1
    assert names.count("filter:response_assembler") == 1
    for obs in fake_langfuse.observations:
        assert obs["trace_context"]["trace_id"] == result.trace_id
    # 根 span 携带买家原文与三文本字段口径（resolved 当前恒等于原文）。
    root = next(o for o in fake_langfuse.observations if o["name"] == "pipeline.message")
    assert root["metadata"]["message"] == "这套房多少钱？"
    assert root["metadata"]["resolved_message"] == "这套房多少钱？"


def test_every_filter_records_span_and_delivery_on_trace(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """矩阵行 1 补全：过滤器 span 全记录、最终下发文本写入 trace。"""
    orchestrator = PipelineOrchestrator(filters=_four_pass_through_filters())
    envelope = Envelope.model_validate(_payload())

    result = orchestrator.run(envelope)

    assert result.trace_id == "ab" * 16  # payload 携带值透传，全链路同一 trace
    filter_spans = [
        s for s in fake_langfuse.entered_spans if s.kwargs["name"].startswith("filter:")
    ]
    assert len(filter_spans) == 4
    assert all(s.exited for s in filter_spans)
    deliveries = [e for e in fake_langfuse.events if e["name"] == "pipeline.delivery"]
    assert len(deliveries) == 1
    assert deliveries[0]["output"] == result.reply
    assert deliveries[0]["metadata"]["delivered_text"] == result.reply
    assert deliveries[0]["metadata"]["degraded"] is False
    # code review 补丁：最终下发文本同步写入根 span metadata（SM-5/SM-6
    # 消费者口径），且与交付事件一致。
    assert fake_langfuse.span_updates[-1]["metadata"]["delivered_text"] == result.reply


def test_filter_exception_records_degradation_with_attribution(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """矩阵行 3：降级兜底后 trace_id 不变，降级事件含归因过滤器名。"""
    orchestrator = PipelineOrchestrator(
        filters=[
            _PassThroughFilter("intent_router"),
            _RaisingFilter(),
            _PassThroughFilter("response_assembler"),
        ]
    )
    envelope = Envelope.model_validate(_payload())

    result = orchestrator.run(envelope)

    assert result.degraded is True
    assert result.failed_filter == "cache_lookup"
    assert result.trace_id == "ab" * 16
    degradations = [e for e in fake_langfuse.events if e["name"] == "pipeline.degradation"]
    assert len(degradations) == 1
    assert degradations[0]["level"] == "ERROR"
    assert degradations[0]["metadata"]["failed_filter"] == "cache_lookup"
    # 失败过滤器 span 标记 ERROR 并原样退出。
    failed_span = next(
        s for s in fake_langfuse.entered_spans if s.kwargs["name"] == "filter:cache_lookup"
    )
    assert failed_span.exited is True
    assert any(u.get("level") == "ERROR" for u in failed_span.span.updates)
    # 降级路径仍补记最终下发文本（兜底文案）。
    deliveries = [e for e in fake_langfuse.events if e["name"] == "pipeline.delivery"]
    assert len(deliveries) == 1
    assert deliveries[0]["output"] == render_fallback("PRICE_INQUIRY")


def test_raising_sdk_client_does_not_affect_pipeline_result() -> None:
    """矩阵行 4：SDK 调用抛任意异常 → 追踪跳过，管道结果不受影响。"""
    for raise_on_start, raise_on_enter in ((True, False), (False, True)):
        tracing.set_langfuse_client(
            _FakeLangfuse(raise_on_start=raise_on_start, raise_on_enter=raise_on_enter)
        )
        try:
            orchestrator = PipelineOrchestrator(filters=_four_pass_through_filters())
            envelope = Envelope.model_validate(_payload(trace_id=None))

            result = orchestrator.run(envelope)

            assert result.degraded is False
            assert result.failed_filter is None
            assert result.reply.startswith(AI_DISCLOSURE_PREFIX)
            assert _TRACE_ID_HEX.fullmatch(result.trace_id)
        finally:
            tracing.set_langfuse_client(None)


def test_record_generation_records_cost_on_trace(fake_langfuse: _FakeLangfuse) -> None:
    """AC 4：成本追踪封装挂到当前消息 trace（Epic 4 消费面，替身断言）。"""
    tracing.record_generation(
        "ab" * 16,
        model="gpt-4o-mini",
        usage={"input": 10, "output": 5, "total": 15},
        cost={"input": 0.0001, "output": 0.0002, "total": 0.0003},
    )

    generations = [o for o in fake_langfuse.observations if o.get("as_type") == "generation"]
    assert len(generations) == 1
    assert generations[0]["trace_context"]["trace_id"] == "ab" * 16
    assert generations[0]["model"] == "gpt-4o-mini"
    assert generations[0]["usage_details"] == {"input": 10, "output": 5, "total": 15}
    assert generations[0]["cost_details"] == {"input": 0.0001, "output": 0.0002, "total": 0.0003}


def test_non_hex_trace_id_passes_through_with_tracing_skipped(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """评审补丁：非 32-hex 携带值原样流经管道与响应，Langfuse 记录全跳过。"""
    orchestrator = PipelineOrchestrator(filters=_four_pass_through_filters())
    envelope = Envelope.model_validate(_payload(trace_id="trace-001"))

    result = orchestrator.run(envelope)

    assert result.degraded is False
    assert result.trace_id == "trace-001"  # 透传，服务端不重写
    assert fake_langfuse.observations == []  # 非法 id：追踪整体跳过
    assert fake_langfuse.events == []


def test_deadline_exhaustion_records_slow_filter_attribution(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """评审补丁：预算耗尽路径降级事件带 slow_filter 归因（确定性负预算）。"""
    orchestrator = PipelineOrchestrator(
        filters=_four_pass_through_filters(), budget_seconds=-1.0
    )
    envelope = Envelope.model_validate(_payload())

    result = orchestrator.run(envelope)

    assert result.degraded is True
    assert result.failed_filter == "intent_router"  # 首跳剩余预算即耗尽
    assert result.trace_id == "ab" * 16
    degradations = [e for e in fake_langfuse.events if e["name"] == "pipeline.degradation"]
    assert len(degradations) == 1
    assert degradations[0]["metadata"]["slow_filter"] == "intent_router"
    assert degradations[0]["metadata"]["failed_filter"] is None


def test_slow_filter_timeout_marks_span_error(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """评审补丁：过滤器真超时（FutureTimeoutError）路径同样记录归因与 ERROR span。"""
    orchestrator = PipelineOrchestrator(
        filters=[
            _PassThroughFilter("intent_router"),
            _SleepingFilter(),
            _PassThroughFilter("response_assembler"),
        ],
        budget_seconds=0.2,
    )
    envelope = Envelope.model_validate(_payload())

    result = orchestrator.run(envelope)

    assert result.degraded is True
    assert result.failed_filter == "risk_review"
    degradations = [e for e in fake_langfuse.events if e["name"] == "pipeline.degradation"]
    assert len(degradations) == 1
    assert degradations[0]["metadata"]["slow_filter"] == "risk_review"
    slow_span = next(
        s for s in fake_langfuse.entered_spans if s.kwargs["name"] == "filter:risk_review"
    )
    assert any(u.get("level") == "ERROR" for u in slow_span.span.updates)


def test_degradation_record_survives_raising_create_event() -> None:
    """矩阵行 3 兜底：降级事件记录本身再抛异常也吞掉，应答不受影响。"""

    class _EventRaisingClient(_FakeLangfuse):
        def create_event(self, **kwargs: Any) -> None:
            msg = "event boom"
            raise RuntimeError(msg)

    tracing.set_langfuse_client(_EventRaisingClient())
    try:
        orchestrator = PipelineOrchestrator(
            filters=[_PassThroughFilter("intent_router"), _RaisingFilter()]
        )
        envelope = Envelope.model_validate(_payload())

        result = orchestrator.run(envelope)

        assert result.degraded is True
        assert result.failed_filter == "cache_lookup"
        assert result.trace_id == "ab" * 16
    finally:
        tracing.set_langfuse_client(None)


def test_langfuse_unconfigured_pipeline_behaves_normally(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """矩阵行 2：无环境变量 → SDK disabled 全 no-op，行为与 Story 1.4 一致。"""
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_BASEURL"):
        monkeypatch.delenv(var, raising=False)
    tracing.set_langfuse_client(None)  # 强制惰性初始化走 disabled 单例

    with caplog.at_level(logging.WARNING, logger="langfuse"):
        client = TestClient(create_app())
        response = client.post("/v1/messages", json=_payload())
        health = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is False
    assert body["trace_id"] == "ab" * 16
    assert body["reply"] == render_fallback("PRICE_INQUIRY")
    # 矩阵行 2 子句：健康检查同样不受 SDK disabled 影响（200 ready）。
    assert health.status_code == 200
    # Story 3.4（A 方案）起就绪响应附 metrics snapshot，仅锁定 status。
    assert health.json()["status"] == "ready"
    # 禁用 warning 仅一次（SDK 单例收敛）。
    disabled_warnings = [r for r in caplog.records if "disabled" in r.getMessage().lower()]
    assert len(disabled_warnings) == 1


def test_redis_down_fallback_generates_trace_id_when_payload_omits_it() -> None:
    """评审补丁：trace_id 放宽为可选后，Redis 故障兜底对无 trace_id
    payload 必须仍返回非空（新生成）trace_id——契约「错误应答携带
    trace_id」在故障路径上不可静默破坏。"""
    app = create_app(session_manager=_FailingSessionManager())  # type: ignore[arg-type]
    client = TestClient(app)
    payload = _payload()
    del payload["trace_id"]

    response = client.post("/v1/messages", json=payload)

    assert response.status_code == 200  # 非 5xx
    body = response.json()
    assert body["degraded"] is True
    assert body["trace_id"]  # 非空，兜底路径新生成
    # code review 补丁：兜底 trace_id 与编排器同口径（32-hex），
    # 错误应答可按响应 id 在 Langfuse 检索。
    assert _TRACE_ID_HEX.fullmatch(body["trace_id"])


# ---- Story 4.6：record_model_routing_event 本体直测（事件名/级别/fail-safe）----


def test_record_model_routing_event_forced_mini_is_default_level(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # code review Epic 4 补丁：forced_mini 每请求一条 ERROR 会成噪音
    # 洪流（同熔断 skip 口径），收紧迁移瞬间的 tightened 才是 ERROR 锚点。
    tracing.record_model_routing_event(
        "ab" * 16, outcome="forced_mini", intent="PRICE_INQUIRY"
    )

    (event,) = fake_langfuse.events
    assert event["name"] == "llm.model_routing_event"
    assert event["level"] == "DEFAULT"
    assert event["metadata"]["event"] == "model_routing_event"
    assert event["metadata"]["outcome"] == "forced_mini"
    assert event["metadata"]["intent"] == "PRICE_INQUIRY"


def test_record_model_routing_event_tightened_is_error_level(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 进入收紧的迁移瞬间是成本异常信号锚点：级别写反会静默丢失。
    tracing.record_model_routing_event(
        "ab" * 16, outcome="tightened", intent="PRICE_INQUIRY"
    )

    (event,) = fake_langfuse.events
    assert event["name"] == "llm.model_routing_event"
    assert event["level"] == "ERROR"
    assert event["metadata"]["outcome"] == "tightened"


@pytest.mark.parametrize("outcome", ["primary", "mini", "recovered"])
def test_record_model_routing_event_routine_outcomes_are_default_level(
    fake_langfuse: _FakeLangfuse, outcome: str
) -> None:
    tracing.record_model_routing_event("ab" * 16, outcome=outcome, intent="GREETING")

    (event,) = fake_langfuse.events
    assert event["name"] == "llm.model_routing_event"
    assert event["level"] == "DEFAULT"
    assert event["metadata"]["outcome"] == outcome


def test_record_model_routing_event_skips_missing_trace_id(
    fake_langfuse: _FakeLangfuse,
) -> None:
    tracing.record_model_routing_event(None, outcome="forced_mini", intent="GREETING")

    assert fake_langfuse.events == []


def test_record_model_routing_event_is_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    # client.create_event 抛异常也吞掉，绝不向上抛（不影响应答链路）。
    class _RaisingClient:
        def create_event(self, **kwargs: Any) -> None:
            msg = "sdk create_event boom"
            raise RuntimeError(msg)

    monkeypatch.setattr(tracing, "_client", _RaisingClient())
    tracing.record_model_routing_event("ab" * 16, outcome="forced_mini")  # 不抛即通过


# ---- Story 4.7：record_draft_empty_event 本体直测（事件名/级别/stage 归因）----


@pytest.mark.parametrize("stage", ["risk_review", "response_assembler"])
def test_record_draft_empty_event_writes_default_level_with_stage(
    fake_langfuse: _FakeLangfuse, stage: str
) -> None:
    tracing.record_draft_empty_event("ab" * 16, stage=stage)

    (event,) = fake_langfuse.events
    assert event["name"] == "response.draft_empty_event"
    assert event["level"] == "DEFAULT"  # 设计内拦截：ERROR 只留真实故障
    assert event["metadata"]["event"] == "draft_empty_event"
    assert event["metadata"]["stage"] == stage
    assert event["trace_context"]["trace_id"] == "ab" * 16


def test_record_draft_empty_event_skips_missing_or_invalid_trace_id(
    fake_langfuse: _FakeLangfuse,
) -> None:
    tracing.record_draft_empty_event(None, stage="risk_review")
    tracing.record_draft_empty_event("not-a-trace-id", stage="response_assembler")

    assert fake_langfuse.events == []


def test_record_draft_empty_event_is_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    # 记录本身再抛也吞掉，绝不令拦截路径崩溃（兜底照常）。
    class _RaisingClient:
        def create_event(self, **kwargs: Any) -> None:
            msg = "sdk create_event boom"
            raise RuntimeError(msg)

    monkeypatch.setattr(tracing, "_client", _RaisingClient())
    tracing.record_draft_empty_event("ab" * 16, stage="risk_review")  # 不抛即通过


# ---- ai.disclosure 事件（Story 5.4，AD-10 可审计锚点） ----


def test_record_ai_disclosure_writes_event_with_label(fake_langfuse: _FakeLangfuse) -> None:
    tracing.record_ai_disclosure("ab" * 16, disclosed=True)

    (event,) = fake_langfuse.events
    assert event["name"] == "ai.disclosure"
    assert event["metadata"]["event"] == "ai_disclosure"
    assert event["metadata"]["disclosed"] is True
    # label 为前缀常量唯一来源，审计侧可直接比对 delivered_text。
    assert event["metadata"]["label"] == AI_DISCLOSURE_PREFIX


def test_record_ai_disclosure_skips_non_recordable_trace_id(
    fake_langfuse: _FakeLangfuse,
) -> None:
    tracing.record_ai_disclosure(None, disclosed=True)
    tracing.record_ai_disclosure("trace-001", disclosed=True)  # 非 32-hex

    assert fake_langfuse.events == []


def test_record_ai_disclosure_is_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    # 事件记录抛异常也吞掉，绝不二次影响买家应答。
    class _RaisingClient:
        def create_event(self, **kwargs: Any) -> None:
            msg = "sdk create_event boom"
            raise RuntimeError(msg)

    monkeypatch.setattr(tracing, "_client", _RaisingClient())
    tracing.record_ai_disclosure("ab" * 16, disclosed=True)  # 不抛即通过


def test_normal_exit_records_ai_disclosure(fake_langfuse: _FakeLangfuse) -> None:
    # 正常链出口：disclosed=True 事件与 delivered_text 双重可验。
    orchestrator = PipelineOrchestrator(filters=_four_pass_through_filters())
    envelope = Envelope.model_validate(_payload())

    result = orchestrator.run(envelope)

    (disclosure,) = [e for e in fake_langfuse.events if e["name"] == "ai.disclosure"]
    assert disclosure["metadata"]["disclosed"] is True
    (delivery,) = [e for e in fake_langfuse.events if e["name"] == "pipeline.delivery"]
    assert delivery["metadata"]["delivered_text"] == result.reply
    assert result.reply.startswith(disclosure["metadata"]["label"])


def test_degraded_exit_records_ai_disclosure(fake_langfuse: _FakeLangfuse) -> None:
    # 降级出口：兜底文案携标，披露事件照常记录。
    orchestrator = PipelineOrchestrator(
        filters=[
            _PassThroughFilter("intent_router"),
            _RaisingFilter(),
            _PassThroughFilter("response_assembler"),
        ]
    )
    envelope = Envelope.model_validate(_payload())

    result = orchestrator.run(envelope)

    assert result.degraded is True
    (disclosure,) = [e for e in fake_langfuse.events if e["name"] == "ai.disclosure"]
    assert disclosure["metadata"]["disclosed"] is True
    assert result.reply == render_fallback("PRICE_INQUIRY")


class _HandoffTriggeringFilter(Filter):
    """硬触发替身：首跳挂 triggered 判定结果。"""

    def __init__(self) -> None:
        self.name = "intent_router"

    def process(self, envelope: Envelope) -> Envelope:
        return envelope.model_copy(
            update={"handoff": HandoffAssessment(triggered=True, conditions=["redline"])}
        )


def test_handoff_exit_records_ai_disclosure_with_prefixed_reply(
    fake_langfuse: _FakeLangfuse,
) -> None:
    # 硬触发出口（Q1 决策 B）：转人工文案携标 + disclosed=True 事件。
    orchestrator = PipelineOrchestrator(filters=[_HandoffTriggeringFilter()])
    envelope = Envelope.model_validate(_payload())

    result = orchestrator.run(envelope)

    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    (disclosure,) = [e for e in fake_langfuse.events if e["name"] == "ai.disclosure"]
    assert disclosure["metadata"]["disclosed"] is True
