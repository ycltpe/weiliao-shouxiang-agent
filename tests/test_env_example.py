"""Story 7.1 .env.example 守护测试（spec 7.1 Tasks，审查 G4+G5 加固）。

口径（spec Boundaries & Constraints）：环境变量清单以代码（组合根与各
模块 env 读取点）为唯一事实源。本文件把「代码消费集 ↔ .env.example
声明集」的双向一致性、数值边界、README 文档覆盖与「干净 checkout
cp 后启动」行为全部钉成自动化断言，防文档/样例漂移。
"""

import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SRC_DIR = REPO_ROOT / "src" / "weiliao_agent"
README = REPO_ROOT / "README.md"
RUNBOOK = REPO_ROOT / "docs" / "runbook.md"

# G1-A 注释 opt-out 集：这两个变量以注释形态保留占位示例，照抄 .env 后
# 处于未配置态、走代码缺省（审核 fail-closed 兜底 / 内置绝对路径词表）。
# 守卫断言相应放宽为「消费 ⊆ 声明 ∪ 注释 opt-out 集」（spec 决策 G1-A）。
COMMENTED_OPT_OUT = frozenset(
    {
        "WEILIAO_RISK_REVIEW_ENDPOINT",
        "WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH",
    }
)

# 数值型变量（空串无法解析，启动 fail-fast 且错误消息带变量名——既有
# 行为；README 注明「留空即启动失败，需删行恢复缺省」）。
NUMERIC_VARS = [
    "WEILIAO_REDIS_SOCKET_TIMEOUT",
    "WEILIAO_L1_TTL_SECONDS",
    "WEILIAO_METRICS_LOG_SECONDS",
    "WEILIAO_RISK_REVIEW_TIMEOUT_MS",
    "WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD",
    "WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS",
    "WEILIAO_LLM_BREAKER_RECOVERY_STEP",
    "WEILIAO_LLM_ROUTING_BUDGET_TOKENS",
    "WEILIAO_LLM_ROUTING_WINDOW_SECONDS",
    "WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD",
]

# 整数型（其余数值变量为 float 口径）。
INTEGER_VARS = frozenset(
    {
        "WEILIAO_L1_TTL_SECONDS",
        "WEILIAO_METRICS_LOG_SECONDS",
        "WEILIAO_RISK_REVIEW_TIMEOUT_MS",
        "WEILIAO_LLM_ROUTING_BUDGET_TOKENS",
        "WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD",
    }
)

# 文档明载 strip-empty fail-fast 的字符串变量（空串/纯空白即启动失败，
# 与数值型同口径参数化覆盖）。
STRING_BLANK_FAIL_FAST_VARS = [
    "WEILIAO_LLM_MINI_MODEL",
    "WEILIAO_HANDOFF_KAFKA_TOPIC",
    "WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC",
    "WEILIAO_HANDOFF_REDLINE_INTENTS",
]

# README 环境变量表声明的文档边界（B6：数值测试校验文档边界，与代码
# fail-fast 口径一致；调参以 README 为权威，漂移即红）。
# ⚠ 本表是边界的第三份手写副本（另两份：README 变量表、main.py 组合根
# fail-fast 校验）——任一处变更须同步三处，本守卫负责在漂移时报警。
# 结构 (low, low_inclusive, high, high_inclusive)；正数必需变量用左开
# 区间（0 会通过闭区间守卫但 main.py 非正 fail-fast）。
DOCUMENTED_BOUNDS: dict[str, tuple[float, bool, float, bool]] = {
    "WEILIAO_REDIS_SOCKET_TIMEOUT": (0, False, 1.0, True),  # >0，上界 1.0
    "WEILIAO_L1_TTL_SECONDS": (1, True, 30, True),  # AD-5 硬上限 30
    "WEILIAO_METRICS_LOG_SECONDS": (1, True, math.inf, True),
    "WEILIAO_RISK_REVIEW_TIMEOUT_MS": (300, True, 800, True),
    "WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD": (0, True, 1, True),
    "WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS": (0, False, math.inf, True),  # >0
    "WEILIAO_LLM_BREAKER_RECOVERY_STEP": (0, False, 1, True),  # (0, 1]
    "WEILIAO_LLM_ROUTING_BUDGET_TOKENS": (0, False, math.inf, True),  # >0 整数
    "WEILIAO_LLM_ROUTING_WINDOW_SECONDS": (0, False, math.inf, True),  # >0
    "WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD": (1, True, math.inf, True),
}

# Story 7.2 非法取值 fail-fast 矩阵（WEILIAO_REDIS_SOCKET_TIMEOUT）：
# 非数值 / 非正 / 非有限（nan/inf）/ 超上界——全部须带变量名拒绝启动。
REDIS_SOCKET_TIMEOUT_ILLEGAL_VALUES = ["abc", "0", "-0.5", "nan", "inf", "2.0"]

# 引号内字面量扫描（B5）：覆盖 os.environ.get 读点与 _ENDPOINT_ENV 等
# 模块级常量；docstring 反引号形态天然排除。
_QUOTED_RE = re.compile(r"""["'](WEILIAO_[A-Z0-9_]+)["']""")
# 未加引号形态扫描（E4）：f-string/拼接构造的 env 名会逃逸引号扫描；
# 与引号/反引号相邻的 token（docstring 反引号形态）天然排除。
_UNQUOTED_RE = re.compile(r"""(?<!["'`\w])(WEILIAO_[A-Z0-9_]+)(?!["'`\w])""")


def _declared_active() -> dict[str, str]:
    """dotenv_values 解析 .env.example（B14：不再手写解析，行内注释
    语义与 python-dotenv 一致），返回取消注释条目。"""
    values = dotenv_values(ENV_EXAMPLE)
    assert values, f"{ENV_EXAMPLE} 无可解析条目"
    return {k: v for k, v in values.items() if k is not None and v is not None}


def _scan_consumed() -> set[str]:
    """扫描 src/weiliao_agent 下全部 .py 的 env 消费点（双扫描口径）。

    扫描前先经 tokenize 剥离注释行（两个扫描同口径）——纯注释提及
    的 WEILIAO_X（如已下线的历史变量）不是消费点，不得假阳进消费集。
    """
    consumed: set[str] = set()
    for path in sorted(SRC_DIR.rglob("*.py")):
        text = _strip_comments(path.read_text(encoding="utf-8"))
        consumed |= set(_QUOTED_RE.findall(text))
        consumed |= set(_UNQUOTED_RE.findall(text))
    return consumed


def _strip_comments(source: str) -> str:
    """tokenize 剥离注释（保持行列位置不变以便按行正则扫描）。"""
    lines = [list(line) for line in source.splitlines()]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return source  # 防御：解析失败退回原文，扫描仍可用
    if not tokens:
        return source
    # 行基数自适应：首 token 恒起于物理行 0，其行号即本环境 tokenize
    # 行号基数（0 基或 1 基随 Python 构建而异），不做硬编码假设。
    row_base = tokens[0].start[0]
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        (start_row, start_col), (end_row, _end_col) = tok.start, tok.end
        for row in range(start_row - row_base, end_row - row_base + 1):
            if not 0 <= row < len(lines):
                continue
            col = start_col if row == start_row - row_base else 0
            for i in range(col, len(lines[row])):
                lines[row][i] = " "
    return "\n".join("".join(line) for line in lines)


def _raw_lines() -> list[str]:
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def _clean_env() -> dict[str, str]:
    """构造无 WEILIAO_*/OPENAI_* 污染的子进程 env（G3 同口径）。"""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("WEILIAO_", "OPENAI_"))
    }


def _example_env() -> dict[str, str]:
    """.env.example 取值注入进程 env 的干净基线（模拟 cp 后 dotenv 加载）。"""
    env = _clean_env()
    env.update(dict(_declared_active()))
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return env


def _run_subprocess(
    args: list[str],
    *,
    env: dict[str, str],
    timeout: float = 90,
) -> subprocess.CompletedProcess[str]:
    """子进程执行；超时带诊断信息失败（E6），不让 CI 静默挂起。"""
    try:
        return subprocess.run(
            args,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        pytest.fail(
            f"子进程超时（{timeout}s）：{' '.join(args)}\n"
            f"--- stdout ---\n{out}\n--- stderr ---\n{err}"
        )


# ---- 守护用例（声明 ↔ 消费一致性、取值与文档边界） ----


def test_example_parses_with_dotenv_values() -> None:
    """B14：样例经 python-dotenv 语义解析。取消注释 WEILIAO_* 21 个，
    加 G1-A 注释占位 2 个共 23 个（与代码消费集同口径）。"""
    values = _declared_active()
    weiliao = {k for k in values if k.startswith("WEILIAO_")}
    assert len(weiliao) >= 21, f"声明的 WEILIAO_* 不足 21 个：{sorted(weiliao)}"
    assert "OPENAI_API_KEY" in values


def test_active_entries_all_non_empty() -> None:
    """G1-A 非空规则：取消注释的条目取值全部非空（空串 fail-fast 根因）。"""
    blanks = [k for k, v in _declared_active().items() if not v.strip()]
    assert not blanks, f".env.example 取消注释条目存在空值：{blanks}"
    # 无「=」的裸键（dotenv 解析为 None）同样视为空值配置，显式拒绝。
    bare = [ln for ln in _raw_lines() if re.match(r"^[A-Z][A-Z0-9_]*\s*$", ln)]
    assert not bare, f".env.example 存在无取值的裸键行：{bare}"


def test_no_duplicate_entries() -> None:
    """E5：重复条目（含注释形态）会导致 dotenv 静默覆盖，必须拒绝。"""
    seen: dict[str, int] = {}
    for line in _raw_lines():
        match = re.match(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)\s*=", line)
        if match:
            key = match.group(1)
            seen[key] = seen.get(key, 0) + 1
    duplicates = sorted(k for k, n in seen.items() if n > 1)
    assert not duplicates, f".env.example 存在重复条目：{duplicates}"


def test_commented_opt_out_set_matches_file() -> None:
    """G1-A：注释形态的 WEILIAO_* 恰好等于 opt-out 集（无未经决策的注释变量）。"""
    declared = {k for k in _declared_active() if k.startswith("WEILIAO_")}
    # 原始文本中所有 WEILIAO_* KEY= 出现（含注释行）。
    all_mentioned = set()
    for line in _raw_lines():
        match = re.match(r"^\s*(?:#\s*)?(WEILIAO_[A-Z0-9_]+)\s*=", line)
        if match:
            all_mentioned.add(match.group(1))
    commented = all_mentioned - declared
    assert commented == set(COMMENTED_OPT_OUT), (
        f"注释形态变量与 G1-A opt-out 集不一致：文件={sorted(commented)} "
        f"期望={sorted(COMMENTED_OPT_OUT)}"
    )


def test_consumed_vars_subset_of_declared() -> None:
    """守卫（B5/E4）：代码消费的 WEILIAO_* ⊆ 声明 ∪ 注释 opt-out 集。"""
    declared = {k for k in _declared_active() if k.startswith("WEILIAO_")}
    consumed = _scan_consumed()
    unknown = consumed - declared - set(COMMENTED_OPT_OUT)
    assert not unknown, (
        f"代码消费但 .env.example 未声明的变量：{sorted(unknown)}"
        f"（补充声明或加入 G1-A opt-out 集）"
    )


def test_no_dead_declared_vars() -> None:
    """反向死变量检测（B5）：声明的 WEILIAO_* ⊆ 消费集 ∪ 注释 opt-out 集。"""
    declared = {k for k in _declared_active() if k.startswith("WEILIAO_")}
    consumed = _scan_consumed()
    dead = declared - consumed - set(COMMENTED_OPT_OUT)
    assert not dead, (
        f".env.example 声明但代码不消费的失效变量：{sorted(dead)}"
        f"（如 LLM_MODEL_ID/LLM_API_KEY/LLM_BASE_URL 即此类历史遗留）"
    )


def test_openai_credential_vars_declared() -> None:
    """凭据变量口径：OPENAI_API_KEY 取消注释（占位非空，SDK 默认读取）；
    OPENAI_BASE_URL 按 G1-A 可注释占位（缺省=官方 API）。"""
    values = _declared_active()
    assert values["OPENAI_API_KEY"].strip(), "OPENAI_API_KEY 须非空占位"
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^\s*(?:#\s*)?OPENAI_BASE_URL\s*=", text, re.MULTILINE)
    # 历史失效变量（代码不消费，Story 7.1 删除）不得回潮。
    for stale in ("LLM_MODEL_ID", "LLM_API_KEY", "LLM_BASE_URL"):
        assert stale not in _scan_consumed()
        assert not re.search(rf"^\s*(?:#\s*)?{stale}\s*=", text, re.MULTILINE)


def test_numeric_values_are_finite() -> None:
    """B6：数值样例拒 inf/nan（组合根对冷却/窗口类变量显式 fail-fast）。"""
    values = _declared_active()
    for name in NUMERIC_VARS:
        raw = values[name]
        number = float(raw)
        assert math.isfinite(number), f"{name} 样例值非有限：{raw!r}"


def test_numeric_values_within_documented_bounds() -> None:
    """B6：数值样例须落在 README 文档边界内（文档↔代码口径一致）。"""
    values = _declared_active()
    for name, (low, low_inclusive, high, high_inclusive) in DOCUMENTED_BOUNDS.items():
        number = float(values[name])
        low_ok = number >= low if low_inclusive else number > low
        high_ok = number <= high if high_inclusive else number < high
        low_bracket = "[" if low_inclusive else "("
        high_bracket = "]" if high_inclusive else ")"
        interval = f"{low_bracket}{low}, {high}{high_bracket}"
        assert low_ok and high_ok, f"{name}={number} 越界文档边界 {interval}"


def test_integer_vars_are_plain_integers() -> None:
    """整型变量样例须为纯整数字面量——「3.7」类取值守卫可绿但启动
    int() 解析即崩，样例必须给出 int() 可解析的形态。"""
    values = _declared_active()
    for name in INTEGER_VARS:
        raw = values[name]
        try:
            int(raw)
        except ValueError:
            pytest.fail(f"{name} 样例非整数字面量：{raw!r}（组合根 int() 解析将崩）")


def test_readme_env_section_covers_all_vars() -> None:
    """B15：README 环境变量表覆盖全部消费变量（防 README 漂移无守卫）。"""
    text = README.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"WEILIAO_[A-Z0-9_]+", text))
    consumed = _scan_consumed()
    missing = consumed - mentioned
    assert not missing, f"README 环境变量表缺失变量说明：{sorted(missing)}"
    assert "OPENAI_API_KEY" in text and "OPENAI_BASE_URL" in text


def test_runbook_covers_all_metric_alerts() -> None:
    """AC2 守卫：runbook 覆盖全部告警事件（metrics 定时循环三类 +
    发送器通道 handoff_command_alert）与 /health 快照键。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    for event in (
        "model_routing_alert",
        "llm_breaker_alert",
        "risk_review_alert",
        "handoff_command_alert",
    ):
        assert event in text, f"runbook 缺失告警处置章节：{event}"
    for snapshot_key in ("tightened", "state", "pass_rate", "over_limits"):
        assert snapshot_key in text, f"runbook 缺失 /health 判读字段：{snapshot_key}"


def test_subprocess_timeout_includes_diagnostics() -> None:
    """E6：子进程超时时诊断信息（stderr 回显）随失败消息抛出。"""
    # pytest.fail 抛 _pytest.outcomes.Failed（BaseException 系），
    # 不能用 pytest.raises(Exception) 承接。
    from _pytest.outcomes import Failed

    with pytest.raises(Failed) as exc_info:
        _run_subprocess(
            [
                sys.executable,
                "-c",
                "import sys, time; sys.stderr.write('diag-marker'); "
                "sys.stderr.flush(); time.sleep(60)",
            ],
            env=_clean_env(),
            timeout=5,
        )
    assert "diag-marker" in str(exc_info.value)


# ---- 矩阵行子进程用例（与「cp 后 dotenv 加载」同口径） ----


def test_clean_checkout_import_with_example_env() -> None:
    """干净启动行：以 .env.example 取值注入子进程导入组合根——fail-fast
    校验全部通过（等价 cp .env.example .env 后 dotenv 加载的路径）。"""
    result = _run_subprocess(
        [sys.executable, "-c", "import weiliao_agent.main; print('IMPORT_OK')"],
        env=_example_env(),
    )
    assert result.returncode == 0, (
        f"组合根导入失败（干净启动行）\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


def test_example_env_health_via_testclient_lifespan() -> None:
    """AC3 服务半边自动化：以 .env.example 取值为进程 env，import 组合根
    后进入 TestClient(app) lifespan 上下文（不绑真实端口、不起 uvicorn
    进程），GET /health 断言 200 ready 且四份快照字段齐全。"""
    code = (
        "import weiliao_agent.main as m\n"
        "from fastapi.testclient import TestClient\n"
        "with TestClient(m.app) as client:\n"
        "    r = client.get('/health')\n"
        "    print('STATUS', r.status_code)\n"
        "    d = r.json()\n"
        "    print('READY', d.get('status'))\n"
        "    print('KEYS', ' '.join(sorted(d)))\n"
    )
    result = _run_subprocess([sys.executable, "-c", code], env=_example_env())
    assert result.returncode == 0, (
        f"TestClient lifespan 启动失败（AC3 服务半边）\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "STATUS 200" in result.stdout, (
        f"/health 非 200：\n{result.stdout}\n{result.stderr}"
    )
    assert "READY ready" in result.stdout
    for snapshot_key in (
        "cache_metrics",
        "risk_review_metrics",
        "llm_breaker",
        "model_routing",
    ):
        assert snapshot_key in result.stdout, (
            f"/health 就绪响应缺快照 {snapshot_key}：\n{result.stdout}"
        )


@pytest.mark.parametrize("var_name", NUMERIC_VARS)
def test_blank_numeric_var_fails_fast_with_var_name(var_name: str) -> None:
    """空串 fail-fast 行（B7 参数化全部数值变量）：留空即启动失败，
    错误消息带变量名。"""
    env = _example_env()
    env[var_name] = ""
    result = _run_subprocess(
        [sys.executable, "-c", "import weiliao_agent.main"],
        env=env,
    )
    assert result.returncode != 0, f"{var_name} 空串竟启动成功（应 fail-fast）"
    assert var_name in result.stderr, (
        f"fail-fast 错误消息缺变量名 {var_name}：\n{result.stderr}"
    )


@pytest.mark.parametrize("var_name", STRING_BLANK_FAIL_FAST_VARS)
def test_blank_string_var_fails_fast_with_var_name(var_name: str) -> None:
    """空串 fail-fast 字符串行（文档明载 strip-empty fail-fast）：留空即
    启动失败，错误消息带变量名。"""
    env = _example_env()
    env[var_name] = ""
    result = _run_subprocess(
        [sys.executable, "-c", "import weiliao_agent.main"],
        env=env,
    )
    assert result.returncode != 0, f"{var_name} 空串竟启动成功（应 fail-fast）"
    assert var_name in result.stderr, (
        f"fail-fast 错误消息缺变量名 {var_name}：\n{result.stderr}"
    )


@pytest.mark.parametrize("raw_value", REDIS_SOCKET_TIMEOUT_ILLEGAL_VALUES)
def test_redis_socket_timeout_illegal_value_fails_fast_with_var_name(
    raw_value: str,
) -> None:
    """Story 7.2 非法取值 fail-fast 矩阵（AC 第 4 条收口 6.1 挂账）：
    WEILIAO_REDIS_SOCKET_TIMEOUT 为非数值 / 非正 / 非有限（nan/inf）/
    超上界时启动失败，错误消息带变量名。"""
    env = _example_env()
    env["WEILIAO_REDIS_SOCKET_TIMEOUT"] = raw_value
    result = _run_subprocess(
        [sys.executable, "-c", "import weiliao_agent.main"],
        env=env,
    )
    assert result.returncode != 0, (
        f"WEILIAO_REDIS_SOCKET_TIMEOUT={raw_value!r} 竟启动成功（应 fail-fast）"
    )
    assert "WEILIAO_REDIS_SOCKET_TIMEOUT" in result.stderr, (
        f"fail-fast 错误消息缺变量名：\n{result.stderr}"
    )


def test_redis_socket_timeout_legal_value_reaches_client_kwargs() -> None:
    """Story 7.2（审查 G1 补丁）合法值正路径：WEILIAO_REDIS_SOCKET_TIMEOUT
    须同时落到 redis 客户端 socket_timeout / socket_connect_timeout 两个
    timeout 位（AC 第 4 条后半；非法矩阵只证 fail-fast、不证接线——接线
    回归回硬编码时全部测试仍绿，必须有本用例钉住）。"""
    env = _example_env()
    env["WEILIAO_REDIS_SOCKET_TIMEOUT"] = "0.7"
    result = _run_subprocess(
        [
            sys.executable,
            "-c",
            (
                "import json\n"
                "import weiliao_agent.main as m\n"
                "kw = m._redis_client.connection_pool.connection_kwargs\n"
                "print(json.dumps({k: kw.get(k) for k in "
                "('socket_timeout', 'socket_connect_timeout')}))"
            ),
        ],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    timeouts = json.loads(result.stdout.strip().splitlines()[-1])
    assert timeouts == {"socket_timeout": 0.7, "socket_connect_timeout": 0.7}


@pytest.mark.parametrize("raw_value", ["1.0", "0.1"])
def test_redis_socket_timeout_boundary_values_accepted(raw_value: str) -> None:
    """边界接受（审查 G1）：上界 1.0 接受、小正值接受——与非法矩阵的
    上界拒绝（2.0）/ 非正拒绝（0）对称成对（缺省 0.5 由 clean-checkout
    导入用例覆盖）。"""
    env = _example_env()
    env["WEILIAO_REDIS_SOCKET_TIMEOUT"] = raw_value
    result = _run_subprocess(
        [sys.executable, "-c", "import weiliao_agent.main"],
        env=env,
    )
    assert result.returncode == 0, result.stderr


@contextlib.contextmanager
def _temporary_repo_env(content: str) -> Iterator[None]:
    """临时以给定内容写仓库根 .env 的上下文（用毕删除）。

    开发机保护：.env 已存在即 pytest.skip——不拿内存备份+finally 赌真实
    .env（SIGKILL/断电窗口会毁掉它）；CI 干净环境（无 .env）照跑。
    """
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        pytest.skip("开发机保护：仓库根 .env 已存在，跳过临时写盘用例")
    try:
        env_file.write_text(content, encoding="utf-8")
        yield
    finally:
        env_file.unlink(missing_ok=True)


def test_dotenv_file_loading_sentinel() -> None:
    """V1：.env 文件加载路径自动化背书——临时以哨兵值写仓库根 .env，
    子进程裸导入（进程 env 已 scrub）断言文件取值生效。删掉/移位
    load_dotenv 本用例即红。真实 .env 存在时跳过（开发机保护）。"""
    sentinel_var = "WEILIAO_LLM_BREAKER_RECOVERY_STEP"
    sentinel_value = "0.5"
    with _temporary_repo_env(f"{sentinel_var}={sentinel_value}\n"):
        result = _run_subprocess(
            [
                sys.executable,
                "-c",
                "import weiliao_agent.main as m; "
                "print('SENTINEL', m._LLM_BREAKER_RECOVERY_STEP)",
            ],
            env=_clean_env(),
        )
        assert result.returncode == 0, (
            f"哨兵 .env 导入失败\n--- stdout ---\n{result.stdout}"
            f"\n--- stderr ---\n{result.stderr}"
        )
        assert f"SENTINEL {sentinel_value}" in result.stdout, (
            f"dotenv 文件加载未生效（期望 {sentinel_var}={sentinel_value}）：\n"
            f"{result.stdout}\n{result.stderr}"
        )


def test_dotenv_does_not_override_process_env() -> None:
    """override=False 优先级背书：进程 env 预设值须胜过 .env 文件冲突值。"""
    sentinel_var = "WEILIAO_LLM_BREAKER_RECOVERY_STEP"
    process_value = "0.3"
    with _temporary_repo_env(f"{sentinel_var}=0.5\n"):
        env = _clean_env()
        env[sentinel_var] = process_value
        result = _run_subprocess(
            [
                sys.executable,
                "-c",
                "import weiliao_agent.main as m; "
                "print('SENTINEL', m._LLM_BREAKER_RECOVERY_STEP)",
            ],
            env=env,
        )
        assert result.returncode == 0, (
            f"override=False 变体导入失败\n--- stdout ---\n{result.stdout}"
            f"\n--- stderr ---\n{result.stderr}"
        )
        assert f"SENTINEL {process_value}" in result.stdout, (
            f"进程 env 未胜过 .env（期望 {process_value}，.env 写 0.5）：\n"
            f"{result.stdout}\n{result.stderr}"
        )
