"""应用入口：装配编排器、SessionManager 与 HTTP 端点，供 uvicorn 启动。

启动方式：``uvicorn weiliao_agent.main:app --reload``。
Story 1.4 的 ``/health`` 日后挂同一 app。
"""

import os

import redis

from weiliao_agent.adapters.http import create_app
from weiliao_agent.pipeline import PipelineOrchestrator
from weiliao_agent.session import SessionManager

# Redis socket 超时须远小于 2.5s 内部预算（NFR-1 P99 < 3s 出口余量口径）。
REDIS_SOCKET_TIMEOUT_SECONDS = 0.5

_redis_client = redis.Redis.from_url(
    os.environ.get("WEILIAO_REDIS_URL", "redis://127.0.0.1:6379/0"),
    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
)

app = create_app(PipelineOrchestrator(), SessionManager(_redis_client))
