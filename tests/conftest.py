"""测试共享夹具：Langfuse client 替身与换入换出（Story 3.3 评审补丁）。

``_FakeLangfuse`` + ``fake_langfuse`` 原分别复制于 test_l0_cache.py /
test_cache_lookup.py，逐文件复制会漂移——收敛为单一共享实现（夹具
teardown 快照先前 client 恢复，而非无条件置 None）。
"""

import os
from collections.abc import Iterator
from typing import Any

import pytest

from weiliao_agent.observability import tracing

# Story 7.1（审查 G3/B10/V2）+ 评审补丁：组合根导入期 load_dotenv 会
# 读取仓库根 .env 并写入 os.environ。处置分三层：
# 1. conftest 导入（收集期最先执行，早于任何测试模块可能模块级导入
#    组合根）时快照现存 WEILIAO_*/OPENAI_*——运维/CI 刻意导出的值不能
#    无差别删除；
# 2. 快照后立即 scrub，消除本机 .env 与 CI 的行为分叉；
# 3. autouse fixture 在每个测试后重 scrub（importlib.reload(组合根) 会
#    重跑 load_dotenv 把本机 .env 重新注入、击穿 monkeypatch.delenv——
#    tests/test_session.py:677 场景），session 结束恢复快照。
_ENV_PREFIXES = ("WEILIAO_", "OPENAI_")
_ENV_SNAPSHOT = {k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES)}


def _scrub_env() -> None:
    for name in [k for k in os.environ if k.startswith(_ENV_PREFIXES)]:
        del os.environ[name]


_scrub_env()


@pytest.fixture(autouse=True)
def _re_scrub_env_after_test() -> Iterator[None]:
    """每个测试后重 scrub 两前缀，防 importlib.reload 重注入击穿隔离。"""
    yield
    _scrub_env()


@pytest.fixture(scope="session", autouse=True)
def _restore_env_snapshot_at_session_end() -> Iterator[None]:
    """session 结束恢复导入期快照（scrub 后重新导出运维/CI 原值）。"""
    yield
    _scrub_env()
    os.environ.update(_ENV_SNAPSHOT)


class _FakeLangfuse:
    """Langfuse client 替身：记录全部事件调用，可按需抛错。"""

    def __init__(self, *, raise_on_event: bool = False) -> None:
        self.raise_on_event = raise_on_event
        self.events: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> None:
        if self.raise_on_event:
            msg = "sdk create_event boom"
            raise RuntimeError(msg)
        self.events.append(kwargs)


@pytest.fixture
def fake_langfuse() -> Iterator[_FakeLangfuse]:
    """换入 _FakeLangfuse；teardown 恢复先前 client（快照式）。"""
    previous = tracing._client_or_none()
    client = _FakeLangfuse()
    tracing.set_langfuse_client(client)
    yield client
    tracing.set_langfuse_client(previous)


def draft_empty_events(fake_langfuse: _FakeLangfuse) -> list[dict[str, Any]]:
    """过滤出 draft_empty 拦截事件（Story 4.7）。

    事件名锚点 ``response.draft_empty_event`` 的唯一定义处：测试断言统一
    经本 helper 取事件，重命名/改taxonomy时单点改动，不会逐文件漂移。
    """
    return [e for e in fake_langfuse.events if e["name"] == "response.draft_empty_event"]
