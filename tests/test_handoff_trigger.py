"""硬触发评估与转人工出口 I/O 矩阵单测（Story 5.1，AD-12）。

覆盖 spec I/O & Edge-Case Matrix 全部场景：
红线意图 / 非文本输入 / 追问达阈值 / 负面词汇 / 多条件同时命中 /
未命中 / 判定模块自身异常（fail-closed）/ 词表加载失败（fail-fast），
外加信封挂点、intent_router 挂载点（含 marker 单写者与 trace 事件）、
编排器短路（下游零执行 + 固定文案）与 SessionManager.mark_handoff
（fakeredis 驱动）。env 装配 fail-fast 见 test_handoff_composition_root.py。
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import fakeredis
import pytest

from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.templates import AI_DISCLOSURE_PREFIX
from weiliao_agent.handoff.trigger import (
    CONDITION_ASSESSMENT_ERROR,
    CONDITION_FOLLOWUP_THRESHOLD,
    CONDITION_NON_TEXT_MEDIA,
    CONDITION_REDLINE_INTENT,
    DEFAULT_FOLLOWUP_THRESHOLD,
    DEFAULT_REDLINE_INTENTS,
    HardTriggerAssessor,
    load_negative_words,
)
from weiliao_agent.models.envelope import Envelope, HandoffAssessment
from weiliao_agent.models.intent import IntentClassification
from weiliao_agent.pipeline import HANDOFF_REPLY_TEXT, PipelineOrchestrator
from weiliao_agent.session import SessionManager

_TRACE_ID = "a" * 32  # 32-hex 口径，fake_langfuse 才收得到事件
_TS = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)

_WORDS = frozenset({"骗子", "垃圾"})


def _assessor(**overrides: Any) -> HardTriggerAssessor:
    params: dict[str, Any] = {"negative_words": _WORDS}
    params.update(overrides)
    return HardTriggerAssessor(**params)


def _assess(
    assessor: HardTriggerAssessor,
    *,
    intent: Any = "GREETING",
    media_type: str | None = None,
    count: int = 0,
    message: str | None = "你好",
) -> HandoffAssessment:
    return assessor.assess(
        intent=intent,
        media_type=media_type,
        per_intent_followup_count=count,
        message=message,
    )


# ---- 评估模块：spec I/O 矩阵 ----


@pytest.mark.parametrize("tag", ["NEGOTIATION", "LOAN_POLICY", "COMPLAINT"])
def test_redline_intent_triggers(tag: str) -> None:
    result = _assess(_assessor(), intent=tag)
    assert result.triggered is True
    assert result.conditions == [CONDITION_REDLINE_INTENT]


@pytest.mark.parametrize("tag", ["GREETING", "PRICE_INQUIRY", "UNKNOWN", None])
def test_non_redline_intent_does_not_trigger(tag: Any) -> None:
    result = _assess(_assessor(), intent=tag)
    assert result.triggered is False
    assert result.conditions == []


@pytest.mark.parametrize("media", ["image", "voice", "audio/ogg", "anything"])
def test_non_text_media_triggers(media: str) -> None:
    result = _assess(_assessor(), media_type=media)
    assert result.triggered is True
    assert result.conditions == [CONDITION_NON_TEXT_MEDIA]


@pytest.mark.parametrize("media", [None, "text"])
def test_text_media_does_not_trigger(media: str | None) -> None:
    result = _assess(_assessor(), media_type=media)
    assert result.triggered is False


def test_followup_at_default_threshold_triggers() -> None:
    result = _assess(_assessor(), count=DEFAULT_FOLLOWUP_THRESHOLD)
    assert result.triggered is True
    assert result.conditions == [CONDITION_FOLLOWUP_THRESHOLD]
    below = _assess(_assessor(), count=DEFAULT_FOLLOWUP_THRESHOLD - 1)
    assert below.triggered is False


def test_followup_custom_threshold() -> None:
    assessor = _assessor(followup_threshold=2)
    assert _assess(assessor, count=1).triggered is False
    assert _assess(assessor, count=2).triggered is True


def test_negative_word_substring_hit_records_word() -> None:
    result = _assess(_assessor(), message="你们这些骗子")
    assert result.triggered is True
    assert result.conditions == ["骗子"]


def test_negative_word_multiple_hits_record_all_words_sorted() -> None:
    result = _assess(_assessor(), message="垃圾骗子")
    assert result.triggered is True
    assert result.conditions == sorted(_WORDS)


def test_no_negative_word_no_trigger() -> None:
    result = _assess(_assessor(), message="这套房价格多少")
    assert result.triggered is False


def test_message_none_skips_negative_check() -> None:
    result = _assess(_assessor(), message=None)
    assert result.triggered is False


def test_multiple_conditions_hit_in_fixed_order() -> None:
    result = _assess(
        _assessor(),
        intent="NEGOTIATION",
        media_type="image",
        count=3,
        message="骗子",
    )
    assert result.triggered is True
    assert result.conditions == [
        CONDITION_REDLINE_INTENT,
        CONDITION_NON_TEXT_MEDIA,
        CONDITION_FOLLOWUP_THRESHOLD,
        "骗子",
    ]


def test_default_constants_are_fr_values() -> None:
    assert DEFAULT_FOLLOWUP_THRESHOLD == 3
    assert set(DEFAULT_REDLINE_INTENTS) == {"NEGOTIATION", "LOAN_POLICY", "COMPLAINT"}


# ---- 评估模块：构造 fail-fast ----


@pytest.mark.parametrize("bad", [0, -1, True, "3", 1.5])
def test_invalid_followup_threshold_fails_fast(bad: Any) -> None:
    with pytest.raises(ValueError, match="followup_threshold"):
        _assessor(followup_threshold=bad)


def test_empty_redline_set_fails_fast() -> None:
    with pytest.raises(ValueError, match="redline_intents"):
        _assessor(redline_intents=[])


def test_unknown_redline_label_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown intent tags"):
        _assessor(redline_intents=["NEGOTIATION", "BOGUS"])


def test_negative_words_normalized_in_constructor() -> None:
    # 空/纯空白词条构造侧归一：'' 是任意消息的子串，脏词条会令负面
    # 条件命中一切消息（全部误转人工）；strip 后丢弃，语义不变。
    assessor = HardTriggerAssessor(negative_words=["  骗子 ", "", "   "])
    assert assessor.negative_words == frozenset({"骗子"})
    assert _assess(assessor, message="你们是骗子").conditions == ["骗子"]
    assert _assess(assessor, message="你好").triggered is False


# ---- 匹配归一化（2026-11-11 裁决 A：NFC/NFKC + 剥零宽 + 简繁折叠） ----


def test_negative_word_traditional_variant_hits_after_normalization() -> None:
    # 繁体消息折叠为简体后命中简体词表——「騙子」不再绕过负面通道。
    assessor = _assessor()
    assert _assess(assessor, message="你們這些騙子").conditions == ["骗子"]


def test_negative_word_zero_width_bypass_closed() -> None:
    # 零宽字符插入（骗\u200b子）剥除后命中——trivial 绕过关闭。
    assessor = _assessor()
    assert _assess(assessor, message="你这个骗\u200b子").conditions == ["骗子"]
    assert _assess(assessor, message="你这个骗\ufeff子").conditions == ["骗子"]


def test_negative_word_full_width_folds_to_half_width_domain() -> None:
    # NFKC 全半角折叠：词表含全角形态时两侧同口径（词条侧构造归一
    # 已覆盖；此处锁消息侧折叠不破坏既有简体命中）。
    assessor = HardTriggerAssessor(negative_words=["ＱＱ诈骗"])
    assert _assess(assessor, message="你这是QQ诈骗").conditions == ["QQ诈骗"]


def test_negative_word_substring_semantics_unchanged_for_benign_collision() -> None:
    # 裁决 A 保持子串语义：「垃圾分类」仍命中「垃圾」——误触发治理
    # 归风控词表瘦身，本补丁不改动匹配语义（锁行为不漂移）。
    assessor = _assessor()
    assert _assess(assessor, message="小区垃圾分类几点").conditions == ["垃圾"]


def test_normalized_word_list_matches_normalized_message() -> None:
    # 词表繁体/全角形态加载时归一为简体半角——与消息侧同口径。
    assessor = HardTriggerAssessor(negative_words=["騙子", "ＱＱ诈骗"])
    assert assessor.negative_words == frozenset({"骗子", "QQ诈骗"})
    assert _assess(assessor, message="你这个骗子").conditions == ["骗子"]


# ---- 词表加载 ----


def test_load_negative_words_strips_bom(tmp_path: Any) -> None:
    # utf-8-sig：Windows/Excel 保存的词表首行带 BOM——首词条不再
    # 永久失配（Epic 5 code review 补丁）。
    path = tmp_path / "words.txt"
    path.write_bytes("\ufeff骗子\n垃圾\n".encode("utf-8"))
    assert load_negative_words(path) == frozenset({"骗子", "垃圾"})


def test_load_negative_words_skips_blanks_and_comments(tmp_path: Any) -> None:
    path = tmp_path / "words.txt"
    path.write_text("# 注释行\n骗子\n\n  垃圾  \n", encoding="utf-8")
    assert load_negative_words(path) == frozenset({"骗子", "垃圾"})


def test_load_negative_words_missing_file_fails_fast(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="unreadable"):
        load_negative_words(tmp_path / "missing.txt")


def test_load_negative_words_empty_file_fails_fast(tmp_path: Any) -> None:
    path = tmp_path / "words.txt"
    path.write_text("# 只有注释\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no entries"):
        load_negative_words(path)


def test_bundled_negative_words_loads() -> None:
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "weiliao_agent"
        / "handoff"
        / "negative_words.txt"
    )
    words = load_negative_words(path)
    assert "骗子" in words
    assert all(word and "#" not in word for word in words)


# ---- 信封挂点 ----


def _envelope(**overrides: Any) -> Envelope:
    payload: dict[str, Any] = {
        "session_id": "sess-001",
        "house_id": "house-001",
        "turn": 1,
        "timestamp": "2026-11-09T10:00:00+08:00",
        "trace_id": _TRACE_ID,
        "message": "你好",
    }
    payload.update(overrides)
    # Story 6.3：分类器与硬触发评估共用 resolved_message 输入——
    # message 覆写时 resolved 默认跟随（除非用例显式覆写 resolved）。
    payload.setdefault("resolved_message", payload["message"])
    return Envelope.model_validate(payload)


def test_envelope_handoff_defaults_to_none_and_validates() -> None:
    assert _envelope().handoff is None  # 未评估为 None
    assessed = _envelope(handoff={"triggered": True, "conditions": ["redline_intent"]})
    assert assessed.handoff is not None
    assert assessed.handoff.triggered is True
    assert assessed.handoff.conditions == ["redline_intent"]
    # 评估了没命中：triggered=False 且与 None 区分
    missed = _envelope(handoff={"triggered": False, "conditions": []})
    assert missed.handoff is not None
    assert missed.handoff.triggered is False


# ---- intent_router 挂载点 ----


class _StubClassifier:
    source = "stub"

    def __init__(self, tag: str = "GREETING", *, fail: bool = False) -> None:
        self._tag = tag
        self._fail = fail

    def classify(self, message: str, context: Any) -> IntentClassification:
        if self._fail:
            msg = "classifier boom"
            raise RuntimeError(msg)
        return IntentClassification(tag=self._tag, confidence=0.9)  # type: ignore[arg-type]


class _RecordingMarker:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._fail = fail

    def __call__(self, session_id: str, conditions: list[str]) -> None:
        if self._fail:
            msg = "redis boom"
            raise RuntimeError(msg)
        self.calls.append((session_id, conditions))


class _FlakyMarker:
    """前 N 次抛错、之后成功的 marker 替身（重试矩阵用）。"""

    def __init__(self, *, failures: int) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._failures = failures

    def __call__(self, session_id: str, conditions: list[str]) -> None:
        self.calls.append((session_id, conditions))
        if len(self.calls) <= self._failures:
            msg = "redis boom"
            raise RuntimeError(msg)


def _router(
    tag: str = "GREETING",
    *,
    recorder: Any = None,
    marker: _RecordingMarker | None = None,
    assessor: HardTriggerAssessor | None = None,
) -> IntentRouterFilter:
    return IntentRouterFilter(
        classifier=_StubClassifier(tag),
        followup_recorder=recorder,
        handoff_assessor=assessor if assessor is not None else _assessor(),
        handoff_marker=marker if marker is not None else _RecordingMarker(),
    )


def test_router_redline_writes_envelope_and_marks_handoff(
    fake_langfuse: Any,
) -> None:
    marker = _RecordingMarker()
    result = _router("NEGOTIATION", marker=marker).process(_envelope())

    assert result.handoff is not None
    assert result.handoff.triggered is True
    assert result.handoff.conditions == [CONDITION_REDLINE_INTENT]
    assert marker.calls == [("sess-001", [CONDITION_REDLINE_INTENT])]
    (event,) = [
        e for e in fake_langfuse.events if e["name"] == "handoff.assessment"
    ]
    assert event["level"] == "WARNING"
    assert event["metadata"]["triggered"] is True


def test_router_miss_records_trace_but_does_not_mark(fake_langfuse: Any) -> None:
    marker = _RecordingMarker()
    result = _router("GREETING", marker=marker).process(_envelope())

    assert result.handoff is not None
    assert result.handoff.triggered is False
    assert result.handoff.conditions == []
    assert marker.calls == []
    (event,) = [
        e for e in fake_langfuse.events if e["name"] == "handoff.assessment"
    ]
    assert event["level"] == "DEFAULT"  # 未命中也记，支撑漏判审计


def test_router_followup_count_at_threshold_triggers(fake_langfuse: Any) -> None:
    # 2026-11-11 裁决 B：阈值判定取 per-tag 聚合计数——per-anchor 被
    # 锚定稀释（=1）也不影响「同一意图 ≥3」命中。
    def recorder(session_id: str, tag: str, anchor: str) -> tuple[int, int, int]:
        return 1, 5, 3

    result = _router("PRICE_INQUIRY", recorder=recorder).process(_envelope())
    assert result.handoff is not None
    assert result.handoff.triggered is True
    assert result.handoff.conditions == [CONDITION_FOLLOWUP_THRESHOLD]


def test_router_followup_per_anchor_dilution_still_triggers(
    fake_langfuse: Any,
) -> None:
    # 裁决 B 回归锚点：per-(tag, anchor) 细键被交替锚定稀释（任一键
    # 永不达 3）时，per-tag 聚合仍使硬触发在第 3 次同意图消息命中——
    # 若实现退回 per-anchor 取数，本条即红。
    per_anchor: dict[str, int] = {}
    state = {"per_tag": 0}
    anchors = iter(["house:h1", "none", "house:h1"])

    def recorder(session_id: str, tag: str, anchor: str) -> tuple[int, int, int]:
        anchor = next(anchors)
        per_anchor[anchor] = per_anchor.get(anchor, 0) + 1
        state["per_tag"] += 1
        return per_anchor[anchor], 0, state["per_tag"]

    filter_ = _router("PRICE_INQUIRY", recorder=recorder)
    first = filter_.process(_envelope())
    second = filter_.process(_envelope())
    third = filter_.process(_envelope())

    assert not first.handoff.triggered
    assert not second.handoff.triggered
    # per-anchor 分别只有 2 与 1（细键口径下永不达 3）；per-tag 聚合 = 3。
    assert third.handoff.conditions == [CONDITION_FOLLOWUP_THRESHOLD]


def test_router_followup_per_tag_reaches_threshold_at_third_message(
    fake_langfuse: Any,
) -> None:
    # 语义钉死（裁决 B）：「同一意图 ≥3」= 该意图第 3 次消息即命中
    # （per-tag 聚合 = 3），第 1、2 次不触发。
    per_tag = {"n": 0}

    def recorder(session_id: str, tag: str, anchor: str) -> tuple[int, int, int]:
        per_tag["n"] += 1
        return per_tag["n"], 0, per_tag["n"]

    filter_ = _router("PRICE_INQUIRY", recorder=recorder)
    first = filter_.process(_envelope())
    second = filter_.process(_envelope())
    third = filter_.process(_envelope())

    assert first.handoff is not None and not first.handoff.triggered
    assert second.handoff is not None and not second.handoff.triggered
    assert third.handoff is not None
    assert third.handoff.conditions == [CONDITION_FOLLOWUP_THRESHOLD]


def test_router_recorder_failure_followup_not_triggered_but_redline_assessed(
    fake_langfuse: Any,
) -> None:
    # Redis 故障 fail-safe（沿用 2.3 语义）：追问条件不触发，但其余
    # 条件照常判定——红线意图在 Redis 故障时绝不得漏放。
    def failing_recorder(
        session_id: str, tag: str, anchor: str
    ) -> tuple[int, int, int]:
        msg = "redis down"
        raise RuntimeError(msg)

    result = _router("NEGOTIATION", recorder=failing_recorder).process(_envelope())
    assert result.handoff is not None
    assert result.handoff.triggered is True
    assert result.handoff.conditions == [CONDITION_REDLINE_INTENT]


def test_router_assessor_exception_fails_closed(fake_langfuse: Any) -> None:
    class _BoomAssessor:
        def assess(self, **kwargs: Any) -> HandoffAssessment:
            msg = "assess boom"
            raise RuntimeError(msg)

    marker = _RecordingMarker()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier("GREETING"),
        handoff_assessor=_BoomAssessor(),  # type: ignore[arg-type]
        handoff_marker=marker,
    )
    result = filter_.process(_envelope())  # 不向上抛

    assert result.handoff is not None
    assert result.handoff.triggered is True  # fail-closed：按触发处理
    assert result.handoff.conditions == [CONDITION_ASSESSMENT_ERROR]
    # 信封保留诊断哨兵（审计归因），但传给 marker 的清单过滤该哨兵——
    # 5.2 的转交载荷消费 handoff_conditions，不携带内部诊断 token。
    assert marker.calls == [("sess-001", [])]
    (event,) = [
        e for e in fake_langfuse.events if e["name"] == "handoff.assessment"
    ]
    assert event["level"] == "ERROR"
    assert event["metadata"]["error"] == "RuntimeError"


def test_router_marker_failure_fails_safe_and_records_trace(fake_langfuse: Any) -> None:
    result = _router("NEGOTIATION", marker=_RecordingMarker(fail=True)).process(
        _envelope()
    )
    assert result.handoff is not None
    assert result.handoff.triggered is True  # 信封保留判定结果，应答不降级
    (event,) = [e for e in fake_langfuse.events if e["name"] == "handoff.mark_failed"]
    assert event["level"] == "ERROR"
    # 不复用分类失败信号：分类失败事件必须缺席（评审补丁口径）。
    assert not [
        e for e in fake_langfuse.events if e["name"] == "intent.classification_failure"
    ]


def test_router_marker_failure_retries_then_succeeds(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # 2026-11-11 裁决 A：marker 写失败有界重试——瞬时故障在重试内
    # 成功则终态落盘、无失败事件、无 ERROR 日志。
    marker = _FlakyMarker(failures=2)
    with caplog.at_level(logging.ERROR):
        result = _router("NEGOTIATION", marker=marker).process(_envelope())

    assert result.handoff is not None
    assert result.handoff.triggered is True
    assert len(marker.calls) == 3  # 初败 2 次 + 末次成功
    assert "handoff.mark_failed" not in [
        e["name"] for e in fake_langfuse.events
    ]
    assert not [
        r for r in caplog.records if r.levelno >= logging.ERROR
    ]


def test_router_marker_failure_exhaustion_logs_error(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # 裁决 A：重试耗尽——会话未入终态（人机混答窗口）必须本地可观测：
    # ERROR 日志（可检索可告警）+ trace 失败事件双留痕。
    with caplog.at_level(logging.ERROR):
        _router("NEGOTIATION", marker=_FlakyMarker(failures=99)).process(_envelope())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("handoff mark failed" in r.getMessage() for r in errors)
    assert any("sess-001" in r.getMessage() for r in errors)
    (event,) = [e for e in fake_langfuse.events if e["name"] == "handoff.mark_failed"]
    assert event["level"] == "ERROR"


def test_router_assessor_exception_logs_local_error(
    fake_langfuse: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # 补丁：assessment_error fail-closed 路径补本地 ERROR 日志——
    # 评估器持续故障会把全量消息误判触发，仅 Langfuse 可见时未配置
    # Langfuse 的部署零留痕。
    class _BoomAssessor:
        def assess(self, **kwargs: Any) -> HandoffAssessment:
            msg = "assess boom"
            raise RuntimeError(msg)

    filter_ = IntentRouterFilter(
        classifier=_StubClassifier("GREETING"),
        handoff_assessor=_BoomAssessor(),  # type: ignore[arg-type]
        handoff_marker=_RecordingMarker(),
    )
    with caplog.at_level(logging.ERROR):
        result = filter_.process(_envelope())

    assert result.handoff is not None
    assert result.handoff.conditions == [CONDITION_ASSESSMENT_ERROR]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("handoff assessment failed" in r.getMessage() for r in errors)


def test_router_non_text_media_triggers_without_classifier_output_dependency(
    fake_langfuse: Any,
) -> None:
    result = _router("GREETING").process(_envelope(media_type="image"))
    assert result.handoff is not None
    assert result.handoff.triggered is True
    assert result.handoff.conditions == [CONDITION_NON_TEXT_MEDIA]


def test_router_empty_message_non_text_media_still_assessed() -> None:
    result = _router("GREETING").process(_envelope(message=None, media_type="voice"))
    assert result.intent == "UNKNOWN"
    assert result.handoff is not None
    assert result.handoff.triggered is True
    assert result.handoff.conditions == [CONDITION_NON_TEXT_MEDIA]


def test_router_negative_word_in_message_triggers(fake_langfuse: Any) -> None:
    marker = _RecordingMarker()
    result = _router("GREETING", marker=marker).process(
        _envelope(message="你们这些骗子")
    )
    assert result.handoff is not None
    assert result.handoff.triggered is True
    assert result.handoff.conditions == ["骗子"]
    assert marker.calls == [("sess-001", ["骗子"])]


def test_router_classification_failure_still_assesses_handoff(
    fake_langfuse: Any,
) -> None:
    # 分类异常 fail-closed 路径与空消息路径同口径：以 UNKNOWN / 追问
    # 序号 0 照常评估——负面词与媒体条件不依赖分类器输出，高风险消息
    # 绝不在分类失败时漏入 cache/LLM。
    marker = _RecordingMarker()
    filter_ = IntentRouterFilter(
        classifier=_StubClassifier(fail=True),
        handoff_assessor=_assessor(),
        handoff_marker=marker,
    )
    hit = filter_.process(_envelope(message="你们这些骗子"))
    assert hit.intent == "UNKNOWN"
    assert hit.handoff is not None
    assert hit.handoff.triggered is True
    assert hit.handoff.conditions == ["骗子"]
    assert marker.calls == [("sess-001", ["骗子"])]

    clean = filter_.process(_envelope(message="你好"))
    assert clean.intent == "UNKNOWN"
    assert clean.handoff is not None
    assert clean.handoff.triggered is False  # 评估了没命中，与 None 区分


def test_router_without_assessor_passes_through() -> None:
    filter_ = IntentRouterFilter(classifier=_StubClassifier("NEGOTIATION"))
    result = filter_.process(_envelope())
    assert result.handoff is None


# ---- 编排器短路 ----


class _RecordingFilter(Filter):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    def process(self, envelope: Envelope) -> Envelope:
        self._log.append(self.name)
        return envelope


def test_pipeline_handoff_short_circuits_and_replies_fixed_text(
    fake_langfuse: Any,
) -> None:
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            _router("NEGOTIATION"),
            _RecordingFilter("cache_lookup", log),
            _RecordingFilter("llm_generate", log),
            _RecordingFilter("risk_review", log),
            _RecordingFilter("response_assembler", log),
        ]
    )
    result = orchestrator.run(_envelope())

    assert log == []  # 下游零执行：cache/llm/risk/assembler 全跳过
    # Story 5.4 决策 Q1-B：转人工文案携标，常量本身保持纯文案不变。
    assert HANDOFF_REPLY_TEXT == "已为您转接经纪人，请稍候"
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert result.degraded is False  # 设计内分支，非降级
    assert result.failed_filter is None


def test_pipeline_no_trigger_runs_full_chain(fake_langfuse: Any) -> None:
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[
            _router("GREETING"),
            _RecordingFilter("cache_lookup", log),
            _RecordingFilter("risk_review", log),
            _RecordingFilter("response_assembler", log),
        ]
    )
    result = orchestrator.run(_envelope())

    assert log == ["cache_lookup", "risk_review", "response_assembler"]
    assert result.reply != HANDOFF_REPLY_TEXT
    assert result.degraded is False


def test_pipeline_pre_assessed_envelope_short_circuits_first_filter() -> None:
    log: list[str] = []
    orchestrator = PipelineOrchestrator(
        filters=[_RecordingFilter("cache_lookup", log)]
    )
    envelope = _envelope(handoff={"triggered": True, "conditions": ["x"]})
    result = orchestrator.run(envelope)
    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert log == ["cache_lookup"]  # 首跳透传执行后立即短路，无后续跳


class _RecordingCommandSender:
    """HandoffCommandSender 协议替身：记录载荷 reply 实参。"""

    def __init__(self) -> None:
        self.replies: list[str] = []

    def send_command(self, envelope: Envelope, *, reply: str) -> None:
        self.replies.append(reply)


def test_pipeline_handoff_payload_reply_carries_ai_prefix(
    fake_langfuse: Any,
) -> None:
    # Epic 5 code review 补丁：载荷当前轮 agent_reply 与买家实收逐字节
    # 一致（均带 AI 披露前缀）——与同 payload 历史轮（转写落盘带前缀）
    # 及 5.2 决策 Q1-A「买家所见逐字原文」对齐。
    sender = _RecordingCommandSender()
    orchestrator = PipelineOrchestrator(
        filters=[_router("NEGOTIATION")],
        handoff_sender=sender,
    )
    result = orchestrator.run(_envelope())

    assert result.reply == f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"
    assert sender.replies == [f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"]


# ---- SessionManager.mark_handoff（AD-4 单写者，fakeredis 驱动） ----


@pytest.fixture
def session_manager() -> tuple[SessionManager, fakeredis.FakeRedis]:
    client = fakeredis.FakeRedis()
    return SessionManager(client), client


def test_mark_handoff_sets_triggered_and_conditions(
    session_manager: tuple[SessionManager, fakeredis.FakeRedis],
) -> None:
    manager, client = session_manager
    manager.begin_turn("sess-001", _TS)
    assert manager.read_snapshot("sess-001") is not None
    assert manager.read_snapshot("sess-001").handoff_status == "none"  # type: ignore[union-attr]

    manager.mark_handoff("sess-001", ["redline_intent", "骗子"])

    snapshot = manager.read_snapshot("sess-001")
    assert snapshot is not None
    assert snapshot.handoff_status == "triggered"
    raw = client.hgetall("weiliao:session:sess-001")
    conditions = json.loads(raw[b"handoff_conditions"].decode())
    assert conditions == ["redline_intent", "骗子"]
    # SessionSnapshot 暴露命中条件清单：5.2/5.3 消费快照即可，无需
    # 绕过 SessionManager 直读原始 Redis。
    assert snapshot.handoff_conditions == ["redline_intent", "骗子"]


def test_snapshot_handoff_conditions_default_empty(
    session_manager: tuple[SessionManager, fakeredis.FakeRedis],
) -> None:
    manager, _ = session_manager
    snapshot = manager.begin_turn("sess-001", _TS)
    assert snapshot.handoff_status == "none"
    assert snapshot.handoff_conditions == []  # 字段未落盘：缺省空清单


def test_begin_turn_snapshot_carries_conditions_after_mark(
    session_manager: tuple[SessionManager, fakeredis.FakeRedis],
) -> None:
    # begin_turn 的 Lua 一并回读 handoff_conditions：已触发会话的后续
    # 消息快照须真实携带条件，不得因缺省退化为空清单。
    manager, _ = session_manager
    manager.begin_turn("sess-001", _TS)
    manager.mark_handoff("sess-001", ["followup_threshold"])
    snapshot = manager.begin_turn("sess-001", _TS)
    assert snapshot.handoff_status == "triggered"
    assert snapshot.handoff_conditions == ["followup_threshold"]


def test_mark_handoff_refreshes_ttl(
    session_manager: tuple[SessionManager, fakeredis.FakeRedis],
) -> None:
    _, client = session_manager
    manager = SessionManager(client, ttl_seconds=600)  # 显式 TTL，不依赖默认 1800
    manager.begin_turn("sess-001", _TS)
    manager.mark_handoff("sess-001", ["non_text_media"])
    ttl = client.pttl("weiliao:session:sess-001")
    assert 0 < ttl <= 600 * 1000


def test_mark_handoff_on_missing_key_initializes_status_only(
    session_manager: tuple[SessionManager, fakeredis.FakeRedis],
) -> None:
    manager, client = session_manager
    manager.mark_handoff("sess-new", ["followup_threshold"])
    fields = {
        k.decode(): v.decode() for k, v in client.hgetall("weiliao:session:sess-new").items()
    }
    assert fields["handoff_status"] == "triggered"
    assert json.loads(fields["handoff_conditions"]) == ["followup_threshold"]
    # 无 turn/last_message_at 的不完整 hash：read_snapshot 防御性返回 None
    assert manager.read_snapshot("sess-new") is None
