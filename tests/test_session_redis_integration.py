"""真实 Redis 并发原子性集成测试（code review 补丁，关闭 verification gap）。

fakeredis 2.40 的 server 级全局锁使命令完全串行，无法在测试环境暴露
真实 Redis 下的竞态——AC-2「并发 turn 唯一、计数精确」的竞态回归在
唯一会违反它的环境中无任何验证会失败。本文件在提供
``WEILIAO_TEST_REDIS_URL`` 时以真实服务端验证，缺省 skip（退出码 0，
不虚构数据冒充验收门）。

运行方式::

    WEILIAO_TEST_REDIS_URL=redis://127.0.0.1:6379/15 uv run pytest -m integration
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
import redis

from weiliao_agent.session import SessionManager

_URL = os.environ.get("WEILIAO_TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _URL,
        reason="需要真实 Redis（设置 WEILIAO_TEST_REDIS_URL，建议独立 db）",
    ),
]

_CONCURRENCY = 100


def test_concurrent_begin_turn_yields_unique_turns_real_redis() -> None:
    """AC-2 真实环境验证：并发 begin_turn 的 turn 集合恰为 1..N 无重复。"""
    client = redis.Redis.from_url(_URL)  # type: ignore[arg-type]
    manager = SessionManager(client, ttl_seconds=60)
    session_id = f"itest:{os.getpid()}:begin-turn"
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            snapshots = list(
                pool.map(
                    lambda _: manager.begin_turn(session_id, datetime.now(UTC)),
                    range(_CONCURRENCY),
                )
            )
        turns = sorted(s.turn for s in snapshots)
        assert turns == list(range(1, _CONCURRENCY + 1))
    finally:
        client.delete(session_id)


def test_concurrent_increment_followup_counts_exactly_real_redis() -> None:
    """AC-2 真实环境验证：并发 increment_followup 计数精确为 N（无丢失）。"""
    client = redis.Redis.from_url(_URL)  # type: ignore[arg-type]
    manager = SessionManager(client, ttl_seconds=60)
    session_id = f"itest:{os.getpid()}:increment-followup"
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(
                pool.map(lambda _: manager.increment_followup(session_id), range(_CONCURRENCY))
            )
        assert max(results) == _CONCURRENCY
        assert manager.increment_followup(session_id) == _CONCURRENCY + 1
    finally:
        client.delete(session_id)
