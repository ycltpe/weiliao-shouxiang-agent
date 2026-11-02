"""应用入口：装配编排器、SessionManager、意图分类器与 HTTP 端点。

启动方式：``uvicorn weiliao_agent.main:app --reload``。
``/health`` 由 create_app 装配（Story 1.4，NFR-7）。

Story 2.2 组合根：``WEILIAO_INTENT_CLASSIFIER`` 选分类器实现
（默认 ``llm_zero_shot``；``none`` 保留占位行为），非法值由工厂
fail-fast——导入期抛 ``ValueError``，服务拒绝启动。意图写回经
``SessionManager.write_intent`` 方法引用注入（AD-4 单写者），路由
链路代码零改动。
"""

import os

import redis

from weiliao_agent.adapters.http import create_app
from weiliao_agent.classifiers import create_classifier
from weiliao_agent.filters.cache_lookup import CacheLookupFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import RiskReviewFilter
from weiliao_agent.pipeline import PipelineOrchestrator
from weiliao_agent.session import SessionManager

# Redis socket 超时须远小于 2.5s 内部预算（NFR-1 P99 < 3s 出口余量口径）。
REDIS_SOCKET_TIMEOUT_SECONDS = 0.5

_redis_client = redis.Redis.from_url(
    os.environ.get("WEILIAO_REDIS_URL", "redis://127.0.0.1:6379/0"),
    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
)

_session_manager = SessionManager(_redis_client)
# 空串等空值回落默认实现（文档承诺），仅显式非法值 fail-fast。
_classifier = create_classifier(os.environ.get("WEILIAO_INTENT_CLASSIFIER") or "llm_zero_shot")

app = create_app(
    PipelineOrchestrator(
        filters=[
            IntentRouterFilter(
                classifier=_classifier,
                intent_writer=_session_manager.write_intent,
            ),
            CacheLookupFilter(),
            RiskReviewFilter(),
            ResponseAssemblerFilter(),
        ]
    ),
    _session_manager,
)
