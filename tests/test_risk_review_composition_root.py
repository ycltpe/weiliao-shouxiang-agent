"""后置审核组合根 fail-fast 单测（Story 4.4，AC 第 5 条；Story 4.6 追加
模型路由 env fail-fast 与合法值回读，同文件先例扩展）。

子进程导入 ``weiliao_agent.main``（导入即装配，同既有生产接线冒烟口径）：
``WEILIAO_RISK_REVIEW_TIMEOUT_MS`` 越界 300–800 或
``WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD`` 非 0–1 时拒绝启动，
错误消息带变量名；``WEILIAO_LLM_ROUTING_BUDGET_TOKENS`` 非正整数、
``WEILIAO_LLM_ROUTING_WINDOW_SECONDS`` 非正/非有限（nan/inf）、
``WEILIAO_LLM_ROUTING_SIMPLE_INTENTS`` 含未知意图标签、
``WEILIAO_LLM_MINI_MODEL`` 空串同理（AC 第 4 条）。全部无网络。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
            "WEILIAO_LLM_MINI_MODEL",
            "WEILIAO_LLM_ROUTING_SIMPLE_INTENTS",
            "WEILIAO_LLM_ROUTING_BUDGET_TOKENS",
            "WEILIAO_LLM_ROUTING_WINDOW_SECONDS",
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


# ---- Story 4.6：模型路由组合根 env fail-fast 与合法值回读（AC 第 4 条）----


@pytest.mark.parametrize("raw", ("0", "-10", "not-a-number"))
def test_invalid_routing_budget_fails_startup_with_variable_name(raw: str) -> None:
    result = _import_main({"WEILIAO_LLM_ROUTING_BUDGET_TOKENS": raw})
    assert result.returncode != 0
    assert b"WEILIAO_LLM_ROUTING_BUDGET_TOKENS" in result.stderr


@pytest.mark.parametrize("raw", ("0", "-1", "nan", "inf"))
def test_invalid_routing_window_fails_startup_with_variable_name(raw: str) -> None:
    result = _import_main({"WEILIAO_LLM_ROUTING_WINDOW_SECONDS": raw})
    assert result.returncode != 0
    assert b"WEILIAO_LLM_ROUTING_WINDOW_SECONDS" in result.stderr


def test_unknown_simple_intent_label_fails_startup_with_variable_name() -> None:
    result = _import_main({"WEILIAO_LLM_ROUTING_SIMPLE_INTENTS": "GREETING,BOGUS"})
    assert result.returncode != 0
    assert b"WEILIAO_LLM_ROUTING_SIMPLE_INTENTS" in result.stderr


@pytest.mark.parametrize("raw", ("", "   ", " , ,"))
def test_empty_simple_intent_list_fails_startup_with_variable_name(raw: str) -> None:
    # 显式置空（空串/全是空白与逗号条目）解析为空名单：mini 常规路由
    # 会静默失效——装配期 fail-fast（错误消息带变量名）。
    result = _import_main({"WEILIAO_LLM_ROUTING_SIMPLE_INTENTS": raw})
    assert result.returncode != 0
    assert b"WEILIAO_LLM_ROUTING_SIMPLE_INTENTS" in result.stderr


@pytest.mark.parametrize("raw", ("", "   "))
def test_empty_mini_model_alias_fails_startup_with_variable_name(raw: str) -> None:
    result = _import_main({"WEILIAO_LLM_MINI_MODEL": raw})
    assert result.returncode != 0
    assert b"WEILIAO_LLM_MINI_MODEL" in result.stderr


def test_valid_routing_env_reaches_router() -> None:
    """验证缺口修补：合法 env 值须真的落到注入 router（防死配置面）。

    子进程导入后回读 router 的 mini alias/预算/窗口/名单并打印，主进程断言。
    """
    result = _import_main(
        {
            "WEILIAO_LLM_MINI_MODEL": "gpt-4o-mini-x",
            "WEILIAO_LLM_ROUTING_SIMPLE_INTENTS": "GREETING, LOCATION",
            "WEILIAO_LLM_ROUTING_BUDGET_TOKENS": "12345",
            "WEILIAO_LLM_ROUTING_WINDOW_SECONDS": "45",
        },
        program=(
            "import weiliao_agent.main as main_module; "
            "router = main_module._model_router; "
            "print(router.mini_alias); "
            "print(router.budget_tokens); "
            "print(router.window_seconds); "
            "print(','.join(sorted(router.simple_intents)))"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().split() == [
        "gpt-4o-mini-x",
        "12345",
        "45.0",
        "GREETING,LOCATION",
    ]


def test_router_wired_into_llm_generate_subgraph() -> None:
    """接线守护：删掉 main.py 的 router=_model_router 注入，本测试即红。

    子进程遍历生产 app 装配链（仿 test_llm_breaker.py
    test_breaker_wired_into_llm_generate_filter 先例）：/v1/messages 端点
    闭包持有编排器，链内 LlmGenerateFilter 的子图 router 必须与组合根
    构造的实例同一对象——缺此守护成本护栏成为外观健康的死代码。
    """
    result = _import_main(
        {},
        program=(
            "import weiliao_agent.main as m; "
            "from weiliao_agent.filters.llm_generate import LlmGenerateFilter; "
            "from weiliao_agent.pipeline import PipelineOrchestrator; "
            "route = next(r for r in m.app.routes "
            "             if getattr(r, 'path', '') == '/v1/messages'); "
            "cells = route.endpoint.__closure__ or (); "
            "orch = next(c.cell_contents for c in cells "
            "            if isinstance(c.cell_contents, PipelineOrchestrator)); "
            "filt = next(f for f in orch._filters "
            "            if isinstance(f, LlmGenerateFilter)); "
            "print(filt._subgraph._router is m._model_router)"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().strip() == "True"
