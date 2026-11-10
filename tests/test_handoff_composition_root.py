"""硬触发组合根 env fail-fast 与合法值回读单测（Story 5.1，AC 第 3 条）。

子进程导入 ``weiliao_agent.main``（导入即装配，同既有生产接线冒烟口径）：
``WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD`` 非正整数、
``WEILIAO_HANDOFF_REDLINE_INTENTS`` 解析为空或含未知意图标签、
``WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH`` 不可读或无词条时拒绝启动，
错误消息带变量名；合法 env 值须真的落到注入评估器（防死配置面）。
全部无网络。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"

_SCRUB_VARS = (
    "WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD",
    "WEILIAO_HANDOFF_REDLINE_INTENTS",
    "WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH",
    "WEILIAO_HANDOFF_KAFKA_TOPIC",
    "WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC",
    "WEILIAO_KAFKA_BROKERS",
)


def _import_main(
    env_overrides: dict[str, str], program: str = "import weiliao_agent.main"
) -> subprocess.CompletedProcess[bytes]:
    # 擦洗继承环境：开发机/CI 全局导出的受测变量会改变子进程导入行为，
    # 使断言错因错位（同 test_risk_review_composition_root.py 先例）。
    env = {
        key: value for key, value in os.environ.items() if key not in _SCRUB_VARS
    }
    env.update({"PYTHONPATH": str(_SRC), **env_overrides})
    return subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("raw", ("0", "-2", "not-a-number"))
def test_invalid_followup_threshold_fails_startup_with_variable_name(raw: str) -> None:
    result = _import_main({"WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD": raw})
    assert result.returncode != 0
    assert b"WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD" in result.stderr


@pytest.mark.parametrize("raw", ("BOGUS", "NEGOTIATION,UNKNOWN_TAG"))
def test_unknown_redline_label_fails_startup_with_variable_name(raw: str) -> None:
    result = _import_main({"WEILIAO_HANDOFF_REDLINE_INTENTS": raw})
    assert result.returncode != 0
    assert b"WEILIAO_HANDOFF_REDLINE_INTENTS" in result.stderr


@pytest.mark.parametrize("raw", ("", "   ", " , ,"))
def test_empty_redline_list_fails_startup_with_variable_name(raw: str) -> None:
    # 显式置空（空串/全是空白与逗号条目）令红线触发静默失效——
    # 装配期 fail-fast（错误消息带变量名）。
    result = _import_main({"WEILIAO_HANDOFF_REDLINE_INTENTS": raw})
    assert result.returncode != 0
    assert b"WEILIAO_HANDOFF_REDLINE_INTENTS" in result.stderr


def test_missing_negative_words_path_fails_startup_with_variable_name() -> None:
    result = _import_main(
        {"WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH": "/nonexistent/words.txt"}
    )
    assert result.returncode != 0
    assert b"WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH" in result.stderr


def test_empty_negative_words_file_fails_startup_with_variable_name(
    tmp_path: Path,
) -> None:
    words = tmp_path / "words.txt"
    words.write_text("# 只有注释\n\n", encoding="utf-8")
    result = _import_main({"WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH": str(words)})
    assert result.returncode != 0
    assert b"WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH" in result.stderr


def test_default_config_imports_cleanly() -> None:
    # 缺省装配：内置词表随包加载，红线三意图 + 阈值 3 生效。
    result = _import_main({})
    assert result.returncode == 0, result.stderr.decode()


def test_valid_handoff_env_reaches_assessor(tmp_path: Path) -> None:
    """验证缺口修补：合法 env 值须真的落到注入评估器（防死配置面）。"""
    words = tmp_path / "words.txt"
    words.write_text("定制负面词\n", encoding="utf-8")
    result = _import_main(
        {
            "WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD": "2",
            "WEILIAO_HANDOFF_REDLINE_INTENTS": "COMPLAINT, GREETING",
            "WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH": str(words),
        },
        program=(
            "import weiliao_agent.main as m; "
            "a = m._handoff_assessor; "
            "print(a.followup_threshold); "
            "print(sorted(a.redline_intents)); "
            "print(sorted(a.negative_words))"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    lines = result.stdout.decode().splitlines()
    assert lines[0] == "2"
    assert lines[1] == "['COMPLAINT', 'GREETING']"
    assert lines[2] == "['定制负面词']"


# ---- Story 5.2：转交指令 Kafka topic env fail-fast 与装配 ----


@pytest.mark.parametrize(
    "var", ("WEILIAO_HANDOFF_KAFKA_TOPIC", "WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC")
)
def test_empty_handoff_kafka_topic_fails_startup_with_variable_name(var: str) -> None:
    # strip 后为空的 topic（空串/纯空白）会让 producer 发送即败——
    # 配置错误拒绝启动（错误消息带变量名），不静默放行。
    result = _import_main({var: "   "})
    assert result.returncode != 0
    assert var.encode() in result.stderr


def test_dlq_topic_colliding_with_live_topic_fails_startup() -> None:
    # 评审轮次 1 补丁：DLQ topic 与指令 topic 相同会让死信写进 live
    # topic 且告警误导——装配期 fail-fast（两变量名均须出现）。
    result = _import_main(
        {
            "WEILIAO_HANDOFF_KAFKA_TOPIC": "same.topic",
            "WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC": "same.topic",
        }
    )
    assert result.returncode != 0
    assert b"WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC" in result.stderr
    assert b"WEILIAO_HANDOFF_KAFKA_TOPIC" in result.stderr


def test_default_config_imports_cleanly_without_brokers() -> None:
    # broker 未配置（本地开发/测试）：发送器不装配（None），缺省 topic
    # 仅解析不校验通过——与消费者门控同口径，导入即装配零网络。
    result = _import_main(
        {},
        program=(
            "import weiliao_agent.main as m; "
            "print(m._handoff_command_sender is None); "
            "print(m._HANDOFF_KAFKA_TOPIC); "
            "print(m._HANDOFF_KAFKA_DLQ_TOPIC)"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    lines = result.stdout.decode().splitlines()
    assert lines == ["True", "handoff.commands", "handoff.commands.dlq"]


def test_valid_handoff_kafka_env_reaches_sender_and_orchestrator() -> None:
    """验证缺口修补：合法 env 值须真的落到发送器并注入编排器（防死
    配置面）。"""
    result = _import_main(
        {
            "WEILIAO_KAFKA_BROKERS": "localhost:9092",
            "WEILIAO_HANDOFF_KAFKA_TOPIC": "handoff.cmd",
            "WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC": "handoff.cmd.dlq",
        },
        program=(
            "import weiliao_agent.main as m; "
            "from weiliao_agent.pipeline import PipelineOrchestrator; "
            "s = m._handoff_command_sender; "
            "print(s.topic); "
            "print(s.dlq_topic); "
            "route = next(r for r in m.app.routes "
            "             if getattr(r, 'path', '') == '/v1/messages'); "
            "cells = route.endpoint.__closure__ or (); "
            "orch = next(c.cell_contents for c in cells "
            "            if isinstance(c.cell_contents, PipelineOrchestrator)); "
            "print(orch._handoff_sender is s)"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    lines = result.stdout.decode().splitlines()
    assert lines == ["handoff.cmd", "handoff.cmd.dlq", "True"]


# ---- Story 5.5：编排器终态闸门 reader 装配 ----


def test_handoff_status_reader_wired_to_session_manager() -> None:
    """验证缺口修补：生产编排器须注入 SessionManager 作终态闸门 reader
    （防死配置面）——删掉 main.py 的注入行，本测试即红。"""
    result = _import_main(
        {},
        program=(
            "import weiliao_agent.main as m; "
            "from weiliao_agent.pipeline import PipelineOrchestrator; "
            "route = next(r for r in m.app.routes "
            "             if getattr(r, 'path', '') == '/v1/messages'); "
            "cells = route.endpoint.__closure__ or (); "
            "orch = next(c.cell_contents for c in cells "
            "            if isinstance(c.cell_contents, PipelineOrchestrator)); "
            "print(orch._handoff_status_reader is m._session_manager)"
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().splitlines() == ["True"]
