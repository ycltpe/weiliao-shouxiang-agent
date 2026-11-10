"""房源指代消解评测 harness（Story 6.2，FR-11 ≥95% 真实验收门 deferred）。

用法：

- ``uv run python tests/eval_coref_resolver.py``：内置合成种子集冒烟
  （指代类/非指代类样本，校验指代类占比 ≥ 20% 的种子集完整性门槛，
  逐条断言 resolver 预测与种子标签一致）。无 CSV 时打印"人工标注语料
  与 FR-1 测试集同期建设、真实验收门 deferred"说明并 exit 0。
- ``uv run python tests/eval_coref_resolver.py eval.csv``：读标注 CSV
  （列：``text,has_house_coref``，可选 ``expected_resolved`` 逐字节
  比对消解后文本；``utf-8-sig`` 读取，兼容 Excel BOM 导出），输出
  准确率、混淆对、非法行跳过数与逐字节比对失败样本；提供 CSV 即
  执行真实验收门（房源指代消解准确率 ≥ 95%，不达标 exit 1）。
  验收口径完整性门槛：CSV 指代类样本占比 < 20% 时验收口径无效，
  打印说明并 exit 1（不进准确率门）。
"""

import argparse
import csv
import sys
from collections.abc import Sequence
from typing import TextIO

from weiliao_agent.coref import RuleBasedCorefResolver
from weiliao_agent.models.envelope import Envelope

GATE_ACCURACY = 0.95
MIN_COREF_RATIO = 0.2
# 评测锚点：Golden 示例口径的占位 house_id（会话锚定语义，不影响
# has_house_coref 判定；expected_resolved 列即按此 house_id 逐字节比对）。
EVAL_HOUSE_ID = "h1"

# 合成种子集：指代类样本（resolver 应产出 house 引用）与非指代类样本
# （应恒等透传）。冒烟验证 harness 机制与 resolver 接线——真实验收
# 以人工标注语料复跑本 harness 为准（语料与 FR-1 测试集同期建设）。
SEEDS_WITH_COREF: list[str] = [
    "它采光怎么样",
    "它多少钱",
    "这套采光怎么样",
    "上面说的那套还有吗",
    "它的价格呢",
    "这套房还有吗",
    "那套房子在几层",
    "这个房子朝向好吗",
    "该房源真实在售吗",
    "这房子离地铁远吗",
    "这套是精装吗",
    "它周边吵不吵",
    "那套房今天能看吗",
    "这套的户型图有吗",
    "那个房子房东好说话吗",
    "这套房东急售吗",
    "它满五唯一吗",
    "那套房子税费多少",
]

SEEDS_WITHOUT_COREF: list[str] = [
    "今天方便看房吗",
    "价格能便宜点吗",
    "离地铁远吗",
    "那层采光呢",
    "那个价位呢",
    "总价多少",
    "在吗",
    "你好",
    "贷款首付比例是多少",
    "能发几张照片吗",
    "楼层是多少",
    "位置发我一下",
    "那个位置偏吗",
    "什么时候可以签约",
    "周边有学校吗",
    "早上好",
    "回复也太慢了吧",
    "最低什么价",
    "那个楼层隔音吗",
    "物业费怎么收",
    "其它楼层还有吗",
    "小区停车方便吗",
    "你会写诗吗",
    "那个地段怎么样",
    "好的",
]

_TRUE_VALUES = {"1", "true", "yes", "y", "是"}
_FALSE_VALUES = {"0", "false", "no", "n", "否"}


def _parse_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return None  # 非法标签：fail-closed，不进评测基数


def _resolve(
    resolver: RuleBasedCorefResolver, text: str
) -> tuple[str, bool]:
    """跑 resolver 一次，返回 ``(消解后文本, 是否产出 house 引用)``。"""
    envelope = Envelope.model_validate(
        {
            "session_id": "eval-session",
            "house_id": EVAL_HOUSE_ID,
            "timestamp": "2026-11-10T10:00:00+08:00",
            "message": text,
        }
    )
    resolved, refs = resolver(envelope)
    return resolved, bool(refs)


def _smoke_seeds(resolver: RuleBasedCorefResolver) -> int:
    """内置种子集冒烟：逐条断言预测与标签一致；返回退出码。"""
    total = len(SEEDS_WITH_COREF) + len(SEEDS_WITHOUT_COREF)
    coref_count = len(SEEDS_WITH_COREF)
    ratio = coref_count / total if total else 0.0
    print(
        f"seed_set rows={total} coref_rows={coref_count} "
        f"coref_ratio={ratio:.3f} (min {MIN_COREF_RATIO})"
    )
    if ratio < MIN_COREF_RATIO:
        # 种子集完整性门槛：指代类占比不足说明种子集失真，冒烟即失败。
        print(
            f"SEED SET INVALID: coref ratio {ratio:.3f} < {MIN_COREF_RATIO}",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    for text in SEEDS_WITH_COREF:
        _, predicted = _resolve(resolver, text)
        if not predicted:
            failures.append(f"missed house coref: {text!r}")
    for text in SEEDS_WITHOUT_COREF:
        _, predicted = _resolve(resolver, text)
        if predicted:
            failures.append(f"false positive house coref: {text!r}")

    if failures:
        print(f"SMOKE FAIL: {len(failures)}/{total} seed rows wrong", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"SMOKE PASS: {total}/{total} seed rows correct (rule template resolver)")
    print(
        "人工标注语料与 FR-1 测试集同期建设、真实验收门 deferred："
        "语料就绪后以 `uv run python tests/eval_coref_resolver.py eval.csv` "
        f"复跑本 harness 执行 >={GATE_ACCURACY:.0%} 验收门"
    )
    return 0


def _load_csv(path: str) -> tuple[list[tuple[str, bool, str | None]], int]:
    """读标注 CSV，返回 ``(合法行, fail-closed 跳过的非法行数)``。"""
    try:
        # utf-8-sig：兼容 Excel 导出带 BOM 的 CSV。
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return _read_rows(handle)
    except OSError as exc:
        msg = f"cannot read CSV {path!r}: {exc}"
        raise SystemExit(msg) from exc


def _read_rows(handle: TextIO) -> tuple[list[tuple[str, bool, str | None]], int]:
    rows: list[tuple[str, bool, str | None]] = []
    skipped = 0
    reader = csv.DictReader(handle)
    required = {"text", "has_house_coref"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        msg = (
            "CSV must have columns text,has_house_coref "
            f"(optional expected_resolved); got {reader.fieldnames}"
        )
        raise SystemExit(msg)
    for line in reader:
        text = (line.get("text") or "").strip()
        label = _parse_bool(line.get("has_house_coref") or "")
        if not text or label is None:
            skipped += 1  # 非法行 fail-closed 跳过（不进评测基数）
            continue
        expected_resolved = (line.get("expected_resolved") or "").strip() or None
        rows.append((text, label, expected_resolved))
    return rows, skipped


def _evaluate_csv(
    resolver: RuleBasedCorefResolver,
    rows: list[tuple[str, bool, str | None]],
    skipped: int,
) -> int:
    """逐条评测并打印报告；提供 CSV 即执行 ≥95% 真实验收门。"""
    correct = 0
    confusion: dict[tuple[bool, bool], int] = {}
    byte_mismatches: list[str] = []
    for text, expected, expected_resolved in rows:
        resolved, predicted = _resolve(resolver, text)
        passed = predicted == expected
        if expected_resolved is not None and resolved != expected_resolved:
            passed = False
            byte_mismatches.append(
                f"byte mismatch: {text!r} -> {resolved!r} != {expected_resolved!r}"
            )
        key = (expected, predicted)
        confusion[key] = confusion.get(key, 0) + 1
        if passed:
            correct += 1

    accuracy = correct / len(rows) if rows else 0.0
    print(
        f"resolver_source=rule_based rows={len(rows)} "
        f"accuracy={accuracy:.3f} ({correct}/{len(rows)}) "
        f"gate>={GATE_ACCURACY:.0%}"
    )
    print(f"skipped_invalid_rows={skipped} (fail-closed, excluded from base)")
    print(
        "confusion (expected -> predicted): "
        f"coref_hit={confusion.get((True, True), 0)}, "
        f"coref_miss={confusion.get((True, False), 0)}, "
        f"plain_hit={confusion.get((False, False), 0)}, "
        f"plain_false_positive={confusion.get((False, True), 0)}"
    )
    for mismatch in byte_mismatches[:10]:
        print(f"  {mismatch}")

    # 真实验收门：人工标注 CSV 即执行（FR-11 ≥95%，不达标 exit 1）。
    if accuracy >= GATE_ACCURACY:
        print(f"GATE PASS: accuracy {accuracy:.3f} >= {GATE_ACCURACY}")
        return 0
    print(f"GATE FAIL: accuracy {accuracy:.3f} < {GATE_ACCURACY}")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "csv",
        nargs="?",
        help="标注语料 CSV（列：text,has_house_coref，可选 expected_resolved；utf-8-sig）",
    )
    args = parser.parse_args(argv)

    resolver = RuleBasedCorefResolver()
    if not args.csv:
        return _smoke_seeds(resolver)
    rows, skipped = _load_csv(args.csv)
    if not rows:
        print("no valid rows in CSV", file=sys.stderr)
        return 2
    # 验收口径完整性门槛：人工语料指代类样本占比 ≥ 20%（spec：指代类
    # 占比 ≥20% 与 FR-1 测试集同期建设）——占比不足时验收口径无效，
    # 不进准确率门，直接 exit 1。
    coref_rows = sum(1 for _, label, _ in rows if label)
    ratio = coref_rows / len(rows)
    if ratio < MIN_COREF_RATIO:
        print(
            f"CSV coref ratio {ratio:.3f} < {MIN_COREF_RATIO}: 指代类样本占比不足，"
            "验收口径无效（spec：人工语料指代类占比 ≥20% 方可执行 FR-11 门）",
            file=sys.stderr,
        )
        return 1
    return _evaluate_csv(resolver, rows, skipped)


if __name__ == "__main__":
    raise SystemExit(main())
