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
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_placeholder
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import tracing
from weiliao_agent.pipeline import PipelineOrchestrator
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

    def start_as_current_observation(self, **kwargs: Any) -> _FakeObservationCM:
        self.observations.append(kwargs)
        if self.raise_on_start:
            msg = "sdk start boom"
            raise RuntimeError(msg)
        return _FakeObservationCM(self, kwargs)

    def create_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


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
    # 响应 trace_id 与信封回填值一致（客户端无关）。
    assert envelope.trace_id == result.trace_id
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
    assert deliveries[0]["output"] == render_placeholder("PRICE_INQUIRY")


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
    assert body["reply"] == render_placeholder("PRICE_INQUIRY")
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
    assert _TRACE_ID_HEX.fullmatch(body["trace_id"]) is None  # 与编排器口径无关，仅非空即可
