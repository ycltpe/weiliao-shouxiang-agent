"""测试共享夹具：Langfuse client 替身与换入换出（Story 3.3 评审补丁）。

``_FakeLangfuse`` + ``fake_langfuse`` 原分别复制于 test_l0_cache.py /
test_cache_lookup.py，逐文件复制会漂移——收敛为单一共享实现（夹具
teardown 快照先前 client 恢复，而非无条件置 None）。
"""

from collections.abc import Iterator
from typing import Any

import pytest

from weiliao_agent.observability import tracing


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
