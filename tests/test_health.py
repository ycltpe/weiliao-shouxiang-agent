"""健康检查端点单测（Story 1.4；Story 3.4 追加 metrics snapshot 读数）。

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
    client = TestClient(create_app())  # 默认四过滤器链完整
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    cache_metrics = body["cache_metrics"]
    assert cache_metrics["lookups"] == 0
    assert cache_metrics["hits"] == 0
    assert cache_metrics["hit_rate"] is None


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
