"""Story 2.2 默认分类器实现 I/O 矩阵单测。

覆盖 spec I/O & Edge-Case Matrix：
合法消息分类（信封写回 + writer 调用 + trace 成功事件）/ Redis 写入
失败 fail-safe（应答不降级、信封保留分类、trace 记 intent_write_failed）/
turn=None 或未注入 writer 跳过写回 / LLM 实现超时守卫（reason=timeout
失败事件）/ 配置切换工厂（合法值装配、非法值 fail-fast）/ 置信度缺省
回填 0.8 / 结构化输出校验拒绝。
"""

import time
from collections.abc import Mapping
from typing import Any

import pytest

from weiliao_agent.classifiers import create_classifier
from weiliao_agent.classifiers.llm_zero_shot import (
    IntentClassificationError,
    LlmZeroShotClassifier,
    _LlmIntentOutput,
)
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentClassification
from weiliao_agent.observability import tracing

TRACE_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _envelope(turn: int | None = 1, message: str = "你好，这套房多少钱？") -> Envelope:
    return Envelope.model_validate(
        {
            "session_id": "sess-001",
            "house_id": "house-001",
            "turn": turn,
            "timestamp": "2026-10-31T10:00:00+08:00",
            "trace_id": TRACE_ID,
            "message": message,
            # Story 6.3 夹具口径：决策输入唯一来源为 resolved_message
            # （standalone 装配显式携带，透传场景逐字节等于原文）。
            "resolved_message": message,
        }
    )


class _StubClassifier:
    """返回固定分类结果的测试替身。"""

    source = "stub"

    def __init__(self, classification: IntentClassification) -> None:
        self._classification = classification

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        return self._classification


class _RecordingWriter:
    """记录写回调用的 writer 替身；可配置抛 redis 错误。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, int, IntentClassification]] = []
        self._fail = fail

    def __call__(
        self, session_id: str, turn: int, classification: IntentClassification
    ) -> None:
        if self._fail:
            import redis

            msg = "Connection refused"
            raise redis.ConnectionError(msg)
        self.calls.append((session_id, turn, classification))


class _FakeEventLangfuse:
    """只记录 create_event 的最小 Langfuse 替身（trace 事件断言用）。"""

    def __init__(self, *, raise_on_event: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self.raise_on_event = raise_on_event

    def create_event(self, **kwargs: Any) -> None:
        if self.raise_on_event:
            msg = "sdk event boom"
            raise RuntimeError(msg)
        self.events.append(kwargs)


@pytest.fixture(autouse=True)
def _reset_langfuse_client() -> Any:
    tracing.set_langfuse_client(None)
    yield
    tracing.set_langfuse_client(None)


def test_classification_success_writes_back_and_calls_writer() -> None:
    fake = _FakeEventLangfuse()
    tracing.set_langfuse_client(fake)  # type: ignore[arg-type]
    writer = _RecordingWriter()
    classification = IntentClassification(tag="PRICE_INQUIRY", confidence=0.93)
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(classification), intent_writer=writer
    )

    result = filter_.process(_envelope())

    assert result.intent == "PRICE_INQUIRY"
    assert writer.calls == [("sess-001", 1, classification)]
    # trace 成功事件：标签/置信度/来源/延迟。
    (event,) = fake.events
    assert event["name"] == "intent.classification"
    metadata = event["metadata"]
    assert metadata["tag"] == "PRICE_INQUIRY"
    assert metadata["confidence"] == 0.93
    assert metadata["source"] == "stub"
    assert isinstance(metadata["latency_ms"], float)


def test_writer_failure_fails_safe_and_records_trace_event() -> None:
    fake = _FakeEventLangfuse()
    tracing.set_langfuse_client(fake)  # type: ignore[arg-type]
    writer = _RecordingWriter(fail=True)
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="GREETING", confidence=0.9)),
        intent_writer=writer,
    )

    result = filter_.process(_envelope())  # 不向上抛，应答不降级

    assert result.intent == "GREETING"  # 信封保留分类结果
    failure = [e for e in fake.events if e["name"] == "intent.classification_failure"]
    assert len(failure) == 1
    assert failure[0]["level"] == "ERROR"  # 评审补丁：失败事件级别锁定
    assert failure[0]["metadata"]["reason"] == "intent_write_failed"


def test_turn_none_skips_writer_but_classifies() -> None:
    writer = _RecordingWriter()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="GREETING", confidence=0.9)),
        intent_writer=writer,
    )

    result = filter_.process(_envelope(turn=None))

    assert result.intent == "GREETING"
    assert writer.calls == []  # 无 turn 即无 per-turn 键可写


def test_writer_not_injected_skips_write_but_classifies() -> None:
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="LOCATION", confidence=0.7))
    )

    result = filter_.process(_envelope())

    assert result.intent == "LOCATION"  # 既有替身装配路径天然兼容


def test_classification_failure_records_reason_and_degrades() -> None:
    fake = _FakeEventLangfuse()
    tracing.set_langfuse_client(fake)  # type: ignore[arg-type]

    class _TimeoutClassifier:
        def classify(
            self, message: str, context: Mapping[str, Any]
        ) -> IntentClassification:
            raise IntentClassificationError("timeout", "exceeded 45ms budget")

    filter_ = IntentRouterFilter(classifier=_TimeoutClassifier())

    result = filter_.process(_envelope())

    assert result.intent == "UNKNOWN"  # 过滤器契约：不向上抛
    failure = [e for e in fake.events if e["name"] == "intent.classification_failure"]
    assert len(failure) == 1
    assert failure[0]["level"] == "ERROR"  # 评审补丁：失败事件级别锁定
    assert failure[0]["metadata"]["reason"] == "timeout"


def test_trace_event_failure_is_swallowed() -> None:
    # SDK create_event 抛错：分类与写回结果不受影响（fail-safe 口径）。
    fake = _FakeEventLangfuse(raise_on_event=True)
    tracing.set_langfuse_client(fake)  # type: ignore[arg-type]
    writer = _RecordingWriter()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="GREETING", confidence=0.9)),
        intent_writer=writer,
    )

    result = filter_.process(_envelope())

    assert result.intent == "GREETING"
    assert writer.calls  # 写回不受 trace 失败影响


def test_non_recordable_trace_id_skips_events() -> None:
    fake = _FakeEventLangfuse()
    tracing.set_langfuse_client(fake)  # type: ignore[arg-type]
    writer = _RecordingWriter()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(IntentClassification(tag="GREETING", confidence=0.9)),
        intent_writer=writer,
    )
    envelope = _envelope().model_copy(update={"trace_id": "trace-001"})

    result = filter_.process(envelope)

    assert result.intent == "GREETING"
    assert writer.calls  # 写回不依赖 trace
    assert fake.events == []


class _FakeStructuredModel:
    """``with_structured_output`` 同接口替身：可配置延迟与返回/抛错。"""

    def __init__(
        self,
        output: _LlmIntentOutput | None = None,
        *,
        delay_seconds: float = 0.0,
        raise_exc: Exception | None = None,
    ) -> None:
        self._output = output
        self._delay = delay_seconds
        self._raise = raise_exc

    def invoke(self, messages: Any) -> _LlmIntentOutput:
        if self._delay:
            time.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        assert self._output is not None
        return self._output


def test_llm_classifier_returns_structured_output() -> None:
    classifier = LlmZeroShotClassifier(
        structured_model=_FakeStructuredModel(
            _LlmIntentOutput(tag="PRICE_INQUIRY", confidence=0.88)
        )
    )

    result = classifier.classify("这套房多少钱？", {"session_id": "s1"})

    assert result == IntentClassification(tag="PRICE_INQUIRY", confidence=0.88)


def test_llm_classifier_backfills_confidence_when_model_omits_it() -> None:
    # 模型不自评：固定口径回填 0.8（不回填 1.0，为追问归并保留信号空间）。
    classifier = LlmZeroShotClassifier(
        structured_model=_FakeStructuredModel(_LlmIntentOutput(tag="GREETING"))
    )

    result = classifier.classify("你好", {})

    assert result.confidence == 0.8


def test_llm_classifier_hard_timeout_guard() -> None:
    classifier = LlmZeroShotClassifier(
        timeout_seconds=0.02,
        structured_model=_FakeStructuredModel(
            _LlmIntentOutput(tag="GREETING", confidence=0.9), delay_seconds=0.5
        ),
    )

    started = time.monotonic()
    with pytest.raises(IntentClassificationError) as exc_info:
        classifier.classify("你好", {})
    elapsed = time.monotonic() - started

    assert exc_info.value.reason == "timeout"
    assert elapsed < 0.4  # 守卫先行返回，不傻等 LLM 调用


def test_llm_classifier_wraps_underlying_errors() -> None:
    classifier = LlmZeroShotClassifier(
        structured_model=_FakeStructuredModel(raise_exc=RuntimeError("api down"))
    )

    with pytest.raises(IntentClassificationError) as exc_info:
        classifier.classify("你好", {})

    assert exc_info.value.reason == "RuntimeError"


def test_llm_classifier_rejects_out_of_range_model_confidence() -> None:
    classifier = LlmZeroShotClassifier(
        structured_model=_FakeStructuredModel(
            _LlmIntentOutput(tag="GREETING", confidence=1.5)
        )
    )

    with pytest.raises(IntentClassificationError) as exc_info:
        classifier.classify("你好", {})

    assert exc_info.value.reason == "invalid_output"


def test_llm_classifier_source_and_factory_default() -> None:
    classifier = create_classifier("llm_zero_shot")

    assert isinstance(classifier, LlmZeroShotClassifier)
    assert classifier is not None
    assert classifier.source == "llm_zero_shot"


def test_factory_none_returns_placeholder_mode() -> None:
    assert create_classifier("none") is None


def test_factory_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown intent classifier"):
        create_classifier("rules_engine")
