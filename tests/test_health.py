"""健康检查端点单测（Story 1.4；Story 3.4/4.4 追加 metrics snapshot 读数；
Story 4.5 追加熔断器快照与快照非零回读；Story 4.6 追加模型路由快照与
快照非零回读）。

覆盖 spec I/O 口径两场景：管道编排完成（含 response_assembler）→ 200；
链不完整 → 503（非 200 即满足 AC，负载均衡据此刻摘除实例）。
"""

from fastapi.testclient import TestClient

from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.base import Filter
from weiliao_agent.main import app as production_app
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.pipeline import PipelineOrchestrator


class _PassThroughFilter(Filter):
    """单过滤器替身：链中无 response_assembler，用于未就绪场景。"""

    name = "intent_router"

    def process(self, envelope: Envelope) -> Envelope:
        return envelope


def test_health_ready_returns_200() -> None:
    # Story 3.4（A 方案）：就绪响应附 metrics snapshot（压测 curl 即读）；
    # 快照读取自身 fail-safe，就绪判定不受影响。
    metrics_module.reset_cache_metrics()
    metrics_module.reset_risk_review_metrics()
    metrics_module.reset_llm_breaker_metrics()
    metrics_module.reset_model_routing_metrics()
    client = TestClient(create_app())  # 默认五过滤器链完整
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    cache_metrics = body["cache_metrics"]
    assert cache_metrics["lookups"] == 0
    assert cache_metrics["hits"] == 0
    assert cache_metrics["hit_rate"] is None
    # Story 4.4：就绪响应附审核通过率快照。
    risk_metrics = body["risk_review_metrics"]
    assert risk_metrics["total"] == 0
    assert risk_metrics["passed"] == 0
    assert risk_metrics["pass_rate"] is None
    # Story 4.5：就绪响应附熔断器快照（状态 + 计数）。
    llm_breaker = body["llm_breaker"]
    assert llm_breaker["state"] == "closed"
    assert llm_breaker["requests"] == 0
    assert llm_breaker["skipped"] == 0
    assert llm_breaker["trips"] == 0
    assert llm_breaker["recoveries"] == 0
    # Story 4.6：就绪响应附模型路由快照（各模型调用数/token/收紧状态）。
    model_routing = body["model_routing"]
    assert model_routing["primary_calls"] == 0
    assert model_routing["primary_tokens"] == 0
    assert model_routing["mini_calls"] == 0
    assert model_routing["mini_tokens"] == 0
    assert model_routing["tightened"] is False
    assert model_routing["over_limits"] == 0
    assert model_routing["recoveries"] == 0


def test_health_model_routing_snapshot_nonzero_readback() -> None:
    # Story 4.6 AC 第 5 条：路由活动后 /health 快照非零可读（非仅零值占位）。
    collector = metrics_module.reset_model_routing_metrics()
    try:
        collector.record_call("primary", 60)
        collector.record_call("forced_mini", 10)
        collector.set_tightened(True)
        collector.record_over_limit()
        client = TestClient(create_app())

        response = client.get("/health")

        assert response.status_code == 200
        model_routing = response.json()["model_routing"]
        assert model_routing["primary_calls"] == 1
        assert model_routing["primary_tokens"] == 60
        assert model_routing["mini_calls"] == 1
        assert model_routing["mini_tokens"] == 10
        assert model_routing["tightened"] is True
        assert model_routing["over_limits"] == 1
        assert model_routing["recoveries"] == 0
    finally:
        # 共享单例：中途断言失败也不得把收紧态/计数泄漏给后续用例。
        metrics_module.reset_model_routing_metrics()


def test_health_llm_breaker_snapshot_nonzero_readback() -> None:
    # Story 4.5 AC 第 5 条：熔断器活动后 /health 快照非零可读（非仅零值占位）。
    collector = metrics_module.reset_llm_breaker_metrics()
    try:
        collector.record_request()
        collector.record_skipped()
        collector.record_trip()
        collector.set_state("open")
        client = TestClient(create_app())

        response = client.get("/health")

        assert response.status_code == 200
        llm_breaker = response.json()["llm_breaker"]
        assert llm_breaker["state"] == "open"
        assert llm_breaker["requests"] == 1
        assert llm_breaker["skipped"] == 1
        assert llm_breaker["trips"] == 1
        assert llm_breaker["recoveries"] == 0
    finally:
        # 共享单例：中途断言失败也不得把 open 态/计数泄漏给后续用例。
        metrics_module.reset_llm_breaker_metrics()


def test_health_incomplete_chain_returns_503() -> None:
    orchestrator = PipelineOrchestrator(filters=[_PassThroughFilter()])
    client = TestClient(create_app(orchestrator))
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


class _RaisingOrchestrator:
    """探针消费即抛错的畸形编排器替身。"""

    @property
    def filter_names(self) -> tuple[str, ...]:
        raise RuntimeError("probe boom")


def test_health_probe_error_returns_503() -> None:
    # 探针异常不得让健康端点 500：负载均衡对 500/503 处置不同，
    # 按未就绪收敛摘除实例。
    client = TestClient(create_app(_RaisingOrchestrator()))  # type: ignore[arg-type]
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_health_production_wiring_ready() -> None:
    # 生产组合根装配冒烟：防 main.py 装配回归（如默认链漏挂过滤器）
    # 在注入替身的单测下不可见。导入即装配（Redis 客户端惰性连接）。
    client = TestClient(production_app)
    response = client.get("/health")
    assert response.status_code == 200
