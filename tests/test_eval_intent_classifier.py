"""eval 评测 harness 门禁逻辑单测（2026-11-10 评审补丁）。

背景（verification-gap 层变异论证）：`eval_intent_classifier` 不在
pytest 收集路径（文件名非 `test_` 前缀），`_evaluate` 门禁四分支与
`_load_csv` 非法行过滤无任何自动化验证——变异 `>= GATE_ACCURACY` 改
`>`、删 coverage 条件、`MIN_EVAL_ROWS` 改 10、deferred 分支 `return 0`
改 `return 1` 全套件全绿。本文件以 stub 分类器 + 合成 rows 锁定四门
分支，全程无网络。
"""

import csv
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_intent_classifier as eval_harness  # noqa: E402

from weiliao_agent.models.intent import IntentClassification, IntentTag


class _EchoTagClassifier:
    """text 即标签的分类替身：text 编码期望标签，返回之（confidence 固定）。

    行构造时把标签写进 text 即得 100% 准确；显式构造 text 与 label
    不一致的行即得确定性的误判。
    """

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        return IntentClassification(tag=message, confidence=0.9)  # type: ignore[arg-type]


def _rows(counts: Mapping[IntentTag, int], *, mismatch: int = 0) -> list[tuple[str, IntentTag]]:
    """按类配额构造 (text=标签, label) rows；mismatch 条 GREETING 行
    的 label 故意错配（accuracy 确定性下降）。"""
    rows: list[tuple[str, IntentTag]] = []
    for tag, n in counts.items():
        rows.extend((tag, tag) for _ in range(n))
    wrong: IntentTag = "COMPLAINT"
    rows.extend(("GREETING", wrong) for _ in range(mismatch))
    return rows


_BALANCED: dict[IntentTag, int] = {
    "GREETING": 112,
    "PRICE_INQUIRY": 112,
    "AVAILABILITY": 112,
    "LOCATION": 112,
    "PHOTO_REQUEST": 111,
    "NEGOTIATION": 111,
    "LOAN_POLICY": 111,
    "COMPLAINT": 111,
    "UNKNOWN": 108,
}


def test_gate_deferred_below_min_rows(capsys: pytest.CaptureFixture[str]) -> None:
    # 999 行（9 类全覆盖）：规模门未达，deferred exit 0（变异目标 1）。
    rows = _rows(_BALANCED)[:999]
    assert len(rows) == 999

    assert eval_harness._evaluate(_EchoTagClassifier(), rows) == 0
    assert "GATE DEFERRED" in capsys.readouterr().out


def test_gate_deferred_when_coverage_incomplete(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1000 行但仅 1 类覆盖：coverage 门未达，deferred exit 0
    # （变异目标：删 coverage 条件后本用例即以 exit 1 翻红）。
    rows = [("GREETING", "GREETING") for _ in range(1000)]

    assert eval_harness._evaluate(_EchoTagClassifier(), rows) == 0
    assert "GATE DEFERRED" in capsys.readouterr().out


def test_gate_pass_when_accuracy_met(capsys: pytest.CaptureFixture[str]) -> None:
    # 1000 行 9 类全覆盖 100% 准确：exit 0 GATE PASS。
    rows = _rows(_BALANCED)
    assert len(rows) == 1000

    assert eval_harness._evaluate(_EchoTagClassifier(), rows) == 0
    assert "GATE PASS" in capsys.readouterr().out


def test_gate_fail_when_accuracy_below_gate(capsys: pytest.CaptureFixture[str]) -> None:
    # 1150 行 9 类全覆盖、150 条确定性误判（accuracy=1000/1150≈0.870
    # < 0.9）：exit 1 GATE FAIL。
    rows = _rows(_BALANCED, mismatch=150)
    assert len(rows) == 1150

    assert eval_harness._evaluate(_EchoTagClassifier(), rows) == 1
    assert "GATE FAIL" in capsys.readouterr().out


def test_load_csv_tolerates_bom_and_skips_invalid_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # BOM（Excel/WPS 另存）+ 非法行（空 text / 非法 label）过滤：
    # 仅 2 条合法行进评测基数（评审补丁：utf-8-sig）。
    csv_path = tmp_path / "eval.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["text", "label"])
        writer.writerow(["GREETING", "GREETING"])
        writer.writerow(["你好", "greeting"])  # 小写归一：合法
        writer.writerow(["", "GREETING"])  # 空 text：跳过
        writer.writerow(["你好", "DISCOUNT"])  # 非法标签：跳过

    rows = eval_harness._load_csv(str(csv_path))

    assert rows == [("GREETING", "GREETING"), ("你好", "GREETING")]


def test_load_csv_rejects_missing_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("foo,bar\n1,2\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="CSV must have columns text,label"):
        eval_harness._load_csv(str(csv_path))
