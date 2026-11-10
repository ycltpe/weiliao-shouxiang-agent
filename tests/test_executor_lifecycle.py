"""执行器生命周期回归测试（Story 7.2，D1/D3 落地 + D4 归因回归）。

压测设施（scripts/loadtest_timeout_storm.py）的 pytest 化最小内核：
共享池风暴后线程有界、超时丢弃语义与应答字节不变、池跨请求复用、
被丢弃生成调用的 token 归因（完成点记账 + trace 事件，互斥不重复）。
无网络/凭据/真实 Redis。
"""

import json
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX, render_fallback
from weiliao_agent.llm.client import LlmGenerationError, OpenAiGenerationClient
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import metrics as metrics_module
from weiliao_agent.observability import tracing
from weiliao_agent.observability.metrics import (
    get_discarded_generation_metrics,
    reset_discarded_generation_metrics,
)
from weiliao_agent.pipeline import PipelineOrchestrator

_METRICS_LOGGER = "weiliao_agent.observability.metrics"


def _valid_payload(intent: str | None = "PRICE_INQUIRY") -> dict[str, Any]:
    return {
        "session_id": "sess-exec",
        "house_id": "house-exec",
        "turn": 1,
        "intent": intent,
        "timestamp": "2026-10-31T10:00:00+08:00",
        "trace_id": "cd" * 16,
    }


class _PassThroughFilter(Filter):
    def __init__(self, name: str) -> None:
        self.name = name

    def process(self, envelope: Envelope) -> Envelope:
        return envelope


class _SlowFilter(Filter):
    """慢过滤器：单调用阻塞远超编排器剩余预算（逐跳超时丢弃驱动）。"""

    def __init__(
        self, name: str, delay_seconds: float, completions: list[int] | None = None
    ) -> None:
        self.name = name
        self._delay_seconds = delay_seconds
        # 完成计数（list.append 原子）：与压测设施同口径的三方一致断言
        #（llm_generate 丢弃归因 == 慢跳完成数）。
        self._completions = completions

    def process(self, envelope: Envelope) -> Envelope:
        time.sleep(self._delay_seconds)
        if self._completions is not None:
            self._completions.append(1)
        return envelope


class _SlowChat:
    """慢 chat 替身：回传确定性 usage（D4 归因断言的期望值口径）。"""

    content = "stub 生成草稿（回归测试占位文案）"
    usage_metadata = {"input_tokens": 15, "output_tokens": 12, "total_tokens": 27}

    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def invoke(self, messages: object) -> Any:
        del messages
        time.sleep(self._delay_seconds)
        return self


class _FastChat:
    """即时成功 chat 替身。"""

    content = "您好，该房源价格约 120 万分桶档。"
    usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    def invoke(self, messages: object) -> Any:
        del messages
        return self


class _FakeLangfuse:
    """tracing 替身接缝（tests/test_tracing.py 同口径）：记录 create_event。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@pytest.fixture
def fake_langfuse() -> Iterator[_FakeLangfuse]:
    client = _FakeLangfuse()
    tracing.set_langfuse_client(client)
    yield client
    tracing.set_langfuse_client(None)


@pytest.fixture(autouse=True)
def _reset_discarded_metrics() -> Iterator[None]:
    reset_discarded_generation_metrics()
    yield
    reset_discarded_generation_metrics()


def _pipeline_pool_threads() -> int:
    """编排器共享池线程数（weiliao-pipeline 命名口径，与压测断言同粒度）。"""
    return sum(1 for t in threading.enumerate() if t.name.startswith("weiliao-pipeline"))


def test_shared_pool_bounds_threads_under_timeout_storm() -> None:
    """D1 回归：超时风暴后共享池线程有界（不随请求数增长）、全部逐跳
    丢弃、应答字节与模板兜底逐字节一致、丢弃归因三方一致（与压测
    设施同口径：排队饥饿下归因可落在更早跳，凡执行的慢跳必对应一条
    llm_generate 归因）。"""
    # 外来池线程基线（审查 G7）：断言用增量口径——进程内其他测试
    # 遗留的 weiliao-pipeline 线程（GC 时序不定）不计入本池的界。
    foreign_pool_threads = _pipeline_pool_threads()
    completions: list[int] = []
    chain = [
        _PassThroughFilter("coreference"),
        _PassThroughFilter("intent_router"),
        _SlowFilter("llm_generate", delay_seconds=0.6, completions=completions),
        _PassThroughFilter("response_assembler"),
    ]
    orchestrator = PipelineOrchestrator(filters=chain, budget_seconds=0.1)
    envelope = Envelope.model_validate(_valid_payload())
    expected_reply = render_fallback(envelope.intent)
    results: list[tuple[bool, str | None]] = []
    results_lock = threading.Lock()

    def _run() -> None:
        result = orchestrator.run(Envelope.model_validate(_valid_payload()))
        with results_lock:
            results.append((result.degraded, result.failed_filter))
        assert result.reply == expected_reply  # 应答字节不变（含披露前缀）

    threads = [threading.Thread(target=_run) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads), "超时风暴后请求线程悬挂"
    assert len(results) == 16
    assert all(degraded for degraded, _ in results), "风暴下应全部逐跳兜底"
    # 风暴期间本池线程不超池大小（增量口径：本测试新建的编排器
    # 至多 max(1, len(chain)) 个池 worker 存活——共享池不随请求积累）。
    assert _pipeline_pool_threads() <= foreign_pool_threads + max(1, len(chain))
    chain_names = {f.name for f in chain}
    assert all(
        failed in chain_names for _, failed in results
    ), "丢弃归因必须落在链内过滤器名"
    # 排空被丢弃的慢跳（0.6s）后池线程回收至有界（池大小=链长）。
    time.sleep(0.8)
    assert _pipeline_pool_threads() <= foreign_pool_threads + max(1, len(chain))
    # 归因覆盖：凡开始执行的慢跳（完成计数）必有 llm_generate 归因兜底
    #（attribution ≥ completions：归因另有「llm 跳剩余预算 ≤ 0 立即兜底」
    # 路径不产生执行——不丢不乱，与压测设施同口径）。
    llm_attributed = sum(1 for _, failed in results if failed == "llm_generate")
    assert llm_attributed >= len(completions) >= 1


def test_pool_reused_across_runs_no_thread_accumulation() -> None:
    """D1 回归：池跨请求复用——多次超时丢弃后池线程不积累。"""
    foreign_pool_threads = _pipeline_pool_threads()  # 增量口径（审查 G7）
    orchestrator = PipelineOrchestrator(
        filters=[
            _PassThroughFilter("intent_router"),
            _SlowFilter("llm_generate", delay_seconds=0.4),
        ],
        budget_seconds=0.05,
    )
    for _ in range(6):
        result = orchestrator.run(Envelope.model_validate(_valid_payload()))
        assert result.failed_filter == "llm_generate"
        # 排空上一请求的丢弃慢跳（0.4s）：顺序复跑不排队，断言聚焦
        # 「池复用不积累」而非排队归因。
        time.sleep(0.45)
    time.sleep(0.6)
    assert _pipeline_pool_threads() <= foreign_pool_threads + 2  # max(1, len(filters))
    # 池复用而非每请求新建：后续正常请求照常工作。
    ok = PipelineOrchestrator(filters=[_PassThroughFilter("intent_router")])
    assert ok.run(Envelope.model_validate(_valid_payload())).degraded is False


def test_slow_call_discarded_then_attributed_at_completion(
    fake_langfuse: _FakeLangfuse,
) -> None:
    """D4 回归：被超时丢弃的生成调用在后台跑完后——token 计入丢弃归因
    口径（区别于成功口径，互斥不重复）+ trace 事件可检索。"""
    client = OpenAiGenerationClient(
        budget_seconds=0.05, chat_model=_SlowChat(delay_seconds=0.2)
    )
    with pytest.raises(LlmGenerationError) as exc_info:
        client.generate([], trace_id="ef" * 16)
    assert exc_info.value.reason == "timeout"
    # 完成点记账：后台跑完（0.2s）+ 回调执行，轮询等待而非定死 sleep。
    deadline = time.monotonic() + 3.0
    snapshot = get_discarded_generation_metrics().snapshot()
    while snapshot["calls"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
        snapshot = get_discarded_generation_metrics().snapshot()
    assert snapshot == {"calls": 1, "tokens": 27}
    # trace 事件：完成点补挂同一 trace_id，DEFAULT 级（非故障信号）。
    discarded_events = [
        e for e in fake_langfuse.events if e.get("name") == "llm.discarded_generation"
    ]
    assert len(discarded_events) == 1
    event = discarded_events[0]
    assert event["level"] == "DEFAULT"
    assert event["metadata"]["usage"] == _SlowChat.usage_metadata


def test_success_path_not_counted_as_discarded() -> None:
    """D4 回归：成功口径（调用侧正常返回）绝不进丢弃归因计数。"""
    client = OpenAiGenerationClient(budget_seconds=1.0, chat_model=_FastChat())
    result = client.generate([], trace_id="ab" * 16)
    assert result.usage["total_tokens"] == 15
    assert get_discarded_generation_metrics().snapshot() == {"calls": 0, "tokens": 0}


def test_emit_discarded_generation_metrics_log_is_structured_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D4 发射形态（审查 G2 补丁）：定时日志为纯 JSON 单行、calls/tokens
    字段口径正确——runbook 已文档化该通道，形态漂移（键名/序列化）必须
    有测试钉住（仿 test_llm_breaker_metrics 的 emitter 先例）。"""
    collector = get_discarded_generation_metrics()
    collector.record_discarded(27)
    collector.record_discarded(15)
    with caplog.at_level(logging.INFO, logger=_METRICS_LOGGER):
        metrics_module.emit_discarded_generation_metrics_log()

    records = [r for r in caplog.records if r.name == _METRICS_LOGGER]
    payload = json.loads(records[0].message)  # 消息体即纯 JSON，无文本前缀
    assert payload["event"] == "discarded_generation_metrics"
    assert payload["calls"] == 2
    assert payload["tokens"] == 42


def test_emit_discarded_generation_metrics_log_is_fail_safe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """发射自身 fail-safe（审查 G2）：snapshot 异常吞掉，不得向调用方
    （组合根定时循环）上抛。"""

    def _boom() -> Any:
        raise RuntimeError("snapshot boom")

    monkeypatch.setattr(
        metrics_module, "get_discarded_generation_metrics", _boom
    )
    with caplog.at_level(logging.INFO, logger=_METRICS_LOGGER):
        metrics_module.emit_discarded_generation_metrics_log()  # 不抛即通过


def test_discarded_background_failure_not_counted() -> None:
    """D4 回归：被丢弃的调用在后台失败（无 token 产出）不计入口径——
    纯浪费归因只认成功回传 usage 的完成。"""

    class _FailingChat:
        def invoke(self, messages: object) -> Any:
            del messages
            time.sleep(0.2)
            msg = "backend boom"
            raise RuntimeError(msg)

    client = OpenAiGenerationClient(budget_seconds=0.05, chat_model=_FailingChat())
    with pytest.raises(LlmGenerationError):
        client.generate([])
    time.sleep(0.5)
    assert get_discarded_generation_metrics().snapshot() == {"calls": 0, "tokens": 0}


def test_degraded_reply_carries_prefix_and_trace_id() -> None:
    """风暴应答兜底字节口径：降级文案携披露前缀 + 原 trace_id 透传。"""
    orchestrator = PipelineOrchestrator(
        filters=[_SlowFilter("llm_generate", delay_seconds=0.4)],
        budget_seconds=0.05,
    )
    result = orchestrator.run(Envelope.model_validate(_valid_payload()))
    assert result.degraded is True
    assert result.trace_id == "cd" * 16
    assert result.reply.startswith(AI_DISCLOSURE_PREFIX)
