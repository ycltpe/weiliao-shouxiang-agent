"""HTTP 接入适配器 —— 最小 FastAPI 同步端点（Story 1.2）。

唯一端点 ``POST /v1/messages``（请求-响应式）。无效输入在端点层
fail-closed：不进入管道，返回兜底应答 + 新生成 trace_id，HTTP 非 5xx。
鉴权/限流等横切能力与 Kafka 接入不在本 Story 范围。
"""

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from weiliao_agent.filters.templates import render_placeholder
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.pipeline import PipelineOrchestrator, PipelineResult


def _fallback_result(intent: str | None = None, trace_id: str | None = None) -> PipelineResult:
    """构造 fail-closed 兜底应答：兜底文案 + 新生成 trace_id。"""
    return PipelineResult(
        reply=render_placeholder(intent),
        trace_id=trace_id if trace_id is not None else str(uuid.uuid4()),
        degraded=True,
        failed_filter=None,
    )


def create_app(orchestrator: PipelineOrchestrator | None = None) -> FastAPI:
    """装配 FastAPI 应用；编排器可注入，便于测试替身。"""
    app = FastAPI(title="weiliao-agent")
    orchestrator_ = orchestrator if orchestrator is not None else PipelineOrchestrator()

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
        try:
            return orchestrator_.run(envelope).to_dict()
        except Exception:
            # 编排器逃逸异常兜底：买家侧永不暴露 5xx。
            return _fallback_result(envelope.intent, envelope.trace_id).to_dict()

    return app
