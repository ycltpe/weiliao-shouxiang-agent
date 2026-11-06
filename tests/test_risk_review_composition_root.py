"""后置审核组合根 fail-fast 单测（Story 4.4，AC 第 5 条）。

子进程导入 ``weiliao_agent.main``（导入即装配，同既有生产接线冒烟口径）：
``WEILIAO_RISK_REVIEW_TIMEOUT_MS`` 越界 300–800 或
``WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD`` 非 0–1 时拒绝启动，
错误消息带变量名。全部无网络。
"""

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"


def _import_main(
    env_overrides: dict[str, str], program: str = "import weiliao_agent.main"
) -> subprocess.CompletedProcess[bytes]:
    # 擦洗继承环境：开发机/CI 全局导出的受测变量会改变子进程导入行为，
    # 使断言错因（如继承非法 TIMEOUT 令 threshold 测试报错对象错位）。
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            "WEILIAO_RISK_REVIEW_TIMEOUT_MS",
            "WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD",
        )
    }
    env.update({"PYTHONPATH": str(_SRC), **env_overrides})
    return subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )


def test_out_of_range_review_timeout_fails_startup_with_variable_name() -> None:
    for raw in ("900", "299"):
        result = _import_main({"WEILIAO_RISK_REVIEW_TIMEOUT_MS": raw})
        assert result.returncode != 0
        assert b"WEILIAO_RISK_REVIEW_TIMEOUT_MS" in result.stderr


def test_invalid_pass_rate_threshold_fails_startup_with_variable_name() -> None:
    for raw in ("1.5", "-0.1", "not-a-number"):
        result = _import_main({"WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD": raw})
        assert result.returncode != 0
        assert b"WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD" in result.stderr


def test_valid_config_imports_cleanly() -> None:
    result = _import_main(
        {
            "WEILIAO_RISK_REVIEW_TIMEOUT_MS": "800",
            "WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD": "0.9",
        }
    )
    assert result.returncode == 0


def test_valid_env_values_reach_client_and_collector() -> None:
    """验证缺口修补：合法 env 值须真的落到注入客户端与指标单例。

    只测「拒绝方向」会让接线成死配置面（main.py 忽略 env 仍全绿）。
    子进程导入后回读两个接线点的实际值并打印，主进程断言。
    """
    result = _import_main(
        {
            "WEILIAO_RISK_REVIEW_TIMEOUT_MS": "800",
            "WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD": "0.9",
        },
        program=(
            "import weiliao_agent.main as main_module; "
            "from weiliao_agent.observability import metrics; "
            "print(main_module._risk_review_client._timeout_ms); "
            "print(metrics.get_risk_review_metrics().pass_rate_threshold)"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().split() == ["800", "0.9"]
