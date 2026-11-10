"""eval_coref_resolver harness 验收门契约锁定（评审补丁）。

harness 的进程级契约——exit 0/1 语义、种子冒烟、CSV 指代类占比校验、
BOM 兼容、缺失文件的 SystemExit——此前不在任何自动化验证路径内；
本文件以临时 CSV 驱动 ``main()`` 锁定上述行为。
"""

from pathlib import Path

import pytest
from eval_coref_resolver import main

_VALID_HEADER = "text,has_house_coref\n"
# 1 指代类 + 4 非指代类：占比恰为 0.2（完整性门槛下限，≥ 即有效）。
_ALL_CORRECT_ROWS = (
    "它多少钱,1\n"
    "今天方便看房吗,0\n"
    "那层采光呢,0\n"
    "总价多少,0\n"
    "在吗,0\n"
)


def _write_csv(tmp_path: Path, content: str) -> str:
    path = tmp_path / "eval.csv"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_seed_smoke_main_returns_zero() -> None:
    # 无 CSV：内置种子集冒烟（含指代类占比 ≥20% 完整性校验）+ deferred
    # 说明，exit 0。
    assert main([]) == 0


def test_all_correct_csv_passes_gate(tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path, _VALID_HEADER + _ALL_CORRECT_ROWS)
    assert main([csv_path]) == 0


def test_wrong_row_csv_fails_gate(tmp_path: Path) -> None:
    # 错标一行（accuracy 0.8 < 0.95）且占比 0.4 ≥ 0.2：走准确率门 exit 1。
    rows = _ALL_CORRECT_ROWS.replace("在吗,0", "在吗,1")
    csv_path = _write_csv(tmp_path, _VALID_HEADER + rows)
    assert main([csv_path]) == 1


def test_low_coref_ratio_csv_rejected_before_gate(tmp_path: Path) -> None:
    # 全非指代类语料：准确率再高占比 0 < 0.2，验收口径无效，exit 1
    # （不进准确率门）。
    rows = "今天方便看房吗,0\n总价多少,0\n在吗,0\n你好,0\n早上好,0\n"
    csv_path = _write_csv(tmp_path, _VALID_HEADER + rows)
    assert main([csv_path]) == 1


def test_bom_csv_reads_normally(tmp_path: Path) -> None:
    # Excel 导出 BOM（utf-8-sig）：正常读取、列名不受 BOM 污染。
    path = tmp_path / "eval.csv"
    path.write_bytes(b"\xef\xbb\xbf" + (_VALID_HEADER + _ALL_CORRECT_ROWS).encode("utf-8"))
    assert main([str(path)]) == 0


def test_missing_csv_exits_with_message(
    tmp_path: Path,
) -> None:
    # 缺失/不可读 CSV：带说明的 SystemExit（不得裸 traceback）。
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "nope.csv")])
    assert "cannot read CSV" in str(excinfo.value)
