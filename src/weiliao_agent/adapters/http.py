"""HTTP 接入适配器 —— 最小 FastAPI 同步端点（Story 1.2，Story 1.3 接入会话，Story 1.4 健康检查）。

消息端点 ``POST /v1/messages``（请求-响应式）与健康检查 ``GET /health``
（就绪 200 / 未就绪 503，NFR-7）。无效输入在端点层 fail-closed：不进入
管道，返回兜底应答 + 新生成 trace_id，HTTP 非 5xx。信封校验通过后，
注入的 SessionManager 先 ``begin_turn`` 并把快照的 turn 回填信封（入管道
前赋值，AD-4 单写者）；Redis 故障 fail-closed：兜底应答 + 原 trace_id，
HTTP 非 5xx。未注入 SessionManager（如测试）则跳过会话步骤。鉴权/限流等
横切能力与 Kafka 接入不在本 Story 范围。
"""

import logging
import uuid
from typing import Any

import redis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from weiliao_agent.filters.templates import render_placeholder
from weiliao_agent.health import cache_metrics_payload, is_ready
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentTag
from weiliao_agent.pipeline import PipelineOrchestrator, PipelineResult
from weiliao_agent.session import SessionManager

logger = logging.getLogger(__name__)


def _fallback_result(
    intent: IntentTag | None = None,
    trace_id: str | None = None,
) -> PipelineResult:
    """构造 fail-closed 兜底应答：兜底文案 + 新生成 trace_id。"""
    return PipelineResult(
        reply=render_placeholder(intent),
        trace_id=trace_id if trace_id is not None else str(uuid.uuid4()),
        degraded=True,
        failed_filter=None,
    )


def create_app(
    orchestrator: PipelineOrchestrator | None = None,
    session_manager: SessionManager | None = None,
) -> FastAPI:
    """装配 FastAPI 应用；编排器与 SessionManager 可注入，便于测试替身。"""
    app = FastAPI(title="weiliao-agent")
    orchestrator_ = orchestrator if orchestrator is not None else PipelineOrchestrator()
    session_manager_ = session_manager

    @app.get("/health")
    def health() -> JSONResponse:
        # NFR-7：管道编排完成（含 response_assembler）即就绪。健康检查
        # 非买家消息：不带 trace_id/兜底语义，不经过滤器链。
        try:
            ready = is_ready(orchestrator_)
        except Exception:
            # 探针自身异常不得让健康端点 500（负载均衡对 500/503 处置
            # 不同，500 属 paging 级事件）：按未就绪收敛，摘除实例待介入。
            ready = False
        if ready:
            # Story 3.4（A 方案）：就绪响应附缓存命中率快照，压测
            # curl /health 即读；metrics 取数自身 fail-safe，不阻端点。
            try:
                payload = cache_metrics_payload()
            except Exception:
                # 快照取数失败不得静默：观测功能无声脱落无迹可查。
                logger.warning(
                    "cache metrics snapshot unavailable; omitting from /health",
                    exc_info=True,
                )
                payload = {}
            return JSONResponse(status_code=200, content={"status": "ready", **payload})
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    @app.exception_handler(RequestValidationError)
    async def validation_fallback(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 无效输入（含 JSON 解析失败）fail-closed：兜底应答 + 新生成
        # trace_id，HTTP 非 5xx。
        return JSONResponse(status_code=200, content=_fallback_result().to_dict())

    @app.post("/v1/messages")
    def post_message(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            envelope = Envelope.model_validate(payload)
        except ValidationError:
            # 无效输入 fail-closed：兜底应答 + 新生成 trace_id，不返回 5xx。
            return _fallback_result().to_dict()
        if session_manager_ is not None:
            try:
                snapshot = session_manager_.begin_turn(envelope.session_id, envelope.timestamp)
            except redis.RedisError:
                # Redis 故障 fail-closed：兜底应答 + 原 trace_id，非 5xx。
                # 仅收敛 redis 异常族：编程错误（如返回值解析 TypeError）
                # 照常上抛，不静默降级掩盖缺陷。
                return _fallback_result(envelope.intent, envelope.trace_id).to_dict()
            envelope.turn = snapshot.turn
        else:
            # 无会话管理器（测试替身）时 turn 无权威来源：不信任 payload
            # 自带值，置 None 防止客户端伪造序号混入管道。
            envelope.turn = None
        try:
            return orchestrator_.run(envelope).to_dict()
        except Exception:
            # 编排器逃逸异常兜底：买家侧永不暴露 5xx。
            return _fallback_result(envelope.intent, envelope.trace_id).to_dict()

    return app
