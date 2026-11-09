"""模型路由与 token 预算护栏（Story 4.6）。

动机（spec Intent）：生成链路所有意图都走同一旗舰模型 alias，打招呼、
问地址这类简单意图也烧旗舰 token；成本只有 Langfuse 事后报表，无预算
阈值与自动收紧手段。本模块在 LLM 层引入配置化路由 + 滑窗预算护栏：

- 路由表：名单内简单意图（默认 GREETING / LOCATION）走 mini alias，
  其余意图（含 intent=None 的 fail-closed 分类路径）走主模型（质量优先）；
- 预算护栏：滑动窗口内累计成功生成的 ``total_tokens`` ≥ 阈值即收紧
  （全部意图强制 mini），窗口消耗回落到阈值以下自动恢复常规路由；
- 路由只决定「用哪个模型生成」：prompt 渲染、grounding 校验、熔断器、
  风控审核、模板兜底链路行为全部不变（买家可见语义逐字节不动）。

口径（spec Boundaries，冻结区）：
- 预算只统计成功生成回传的 usage（``total_tokens`` 缺键/缺失容忍为 0），
  失败请求不计入（account 只在 generate 成功后由调用方触发）；
- 收紧/恢复无迟滞带：≥阈值收紧、<阈值恢复，抖动由窗口平滑天然抑制；
- 路由层为每个 alias 各持一个 ``OpenAiGenerationClient`` 实例——
  客户端保持单模型纯粹（4.2/4.5 冻结语义，本模块不改 client）；
- 判定/收紧/恢复全 fail-safe：调用方（子图节点）对 select/account 的
  异常兜底吞掉、回退主模型放行处理，护栏绝不阻断生成路径。

纯进程内状态（双端队列 + 锁），不依赖 OpenAI SDK；``clock`` 可注入
 fake 时钟（仿 4.5 熔断器测试先例），生产用 ``time.monotonic``。
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

from weiliao_agent.llm.client import OpenAiGenerationClient
from weiliao_agent.models.intent import IntentTag

if TYPE_CHECKING:
    from weiliao_agent.observability.metrics import ModelRoutingMetricsCollector

# mini 模型 alias 缺省值（组合根经 WEILIAO_LLM_MINI_MODEL 覆盖；禁硬编码
# 到调用点——本常量为 env 缺省的唯一权威）。
DEFAULT_MINI_MODEL_ALIAS = "gpt-4o-mini"

# 简单意图名单缺省值（组合根经 WEILIAO_LLM_ROUTING_SIMPLE_INTENTS 覆盖）：
# 打招呼、问地址属低复杂度意图，走 mini 模型成本最优。
DEFAULT_SIMPLE_INTENTS: frozenset[str] = frozenset({"GREETING", "LOCATION"})

# 滑窗 token 预算缺省值（组合根经 WEILIAO_LLM_ROUTING_BUDGET_TOKENS 覆盖）：
# 窗口内累计 total_tokens 达该值即收紧、全部强制 mini。
DEFAULT_BUDGET_TOKENS = 50_000

# 滑窗时长缺省秒数（组合根经 WEILIAO_LLM_ROUTING_WINDOW_SECONDS 覆盖；
# spec 决策：每分钟滑动窗口）。
DEFAULT_WINDOW_SECONDS = 60.0

# 意图标签全集（models/intent.py IntentTag Literal 的权威镜像，构造校验
# 名单用；未知标签 fail-fast 拒绝装配）。
KNOWN_INTENTS: frozenset[str] = frozenset(get_args(IntentTag))

# 单次路由判定的 outcome 类型（transition 只取 recovered）。
RoutingOutcome = Literal["primary", "mini", "forced_mini"]

# 路由事件 outcome 词表（trace 事件 / 指标快照共用）。primary/mini 为常规
# 路由结果，forced_mini 为收紧期强制结果，recovered 为收紧→恢复迁移事件。
OUTCOME_PRIMARY: RoutingOutcome = "primary"
OUTCOME_MINI: RoutingOutcome = "mini"
OUTCOME_FORCED_MINI: RoutingOutcome = "forced_mini"
OUTCOME_RECOVERED: Literal["recovered"] = "recovered"


@dataclass(frozen=True)
class RoutingDecision:
    """一次路由判定结果：选定的 client + trace outcome + 迁移事件。

    ``transition`` 仅在「收紧期 → 窗口回落恢复」的判定瞬间为
    ``recovered``（调用方据此补记 recovered 事件）；其余为 None。
    收紧（forced_mini）本身即 outcome，无需独立迁移字段。
    """

    client: OpenAiGenerationClient
    outcome: RoutingOutcome
    transition: Literal["recovered"] | None = None


def _resolve_alias(client: OpenAiGenerationClient, *, param: str) -> str:
    """取 client 实际生效 alias；空串/非串 fail-fast（错误消息带参数名）。"""
    alias = getattr(client, "model", None)
    if not isinstance(alias, str) or not alias.strip():
        msg = f"{param} model alias must be a non-empty string, got {alias!r}"
        raise ValueError(msg)
    return alias


class ModelRouter:
    """意图→alias 路由表 + 双 client 调度 + 滑窗 token 预算护栏。

    挂点（由子图 generate 节点消费，router 为可选注入——None=单模型
    既有路径逐字节不变）：
    - ``select(intent)``：判定本次生成用哪个 client（含收紧/恢复状态机），
      返回 ``RoutingDecision``；仅做判定与状态迁移，不计数；
    - ``account(decision, usage)``：成功生成后回写 usage 进滑窗并上报
      指标（失败请求不记账——调用方只在 generate 成功后调用）。

    构造参数 fail-fast（非正/非有限预算或窗口、名单含未知意图标签、
    任一 client alias 空串均抛 ValueError，错误消息带变量名）；
    ``clock`` 可注入 fake 时钟；``metrics`` 注入指标收集器单例
    （None=不上报，保持纯状态机可单测）。全部埋点 fail-safe。
    """

    def __init__(
        self,
        primary_client: OpenAiGenerationClient,
        mini_client: OpenAiGenerationClient,
        *,
        simple_intents: Collection[str] = DEFAULT_SIMPLE_INTENTS,
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        metrics: "ModelRoutingMetricsCollector | None" = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary_alias = _resolve_alias(primary_client, param="primary_client")
        self._mini_alias = _resolve_alias(mini_client, param="mini_client")
        unknown = sorted(set(simple_intents) - KNOWN_INTENTS)
        if unknown:
            # 名单含未知意图标签：配置错误须装配期暴露（带标签值定位）。
            msg = f"simple_intents contains unknown intent labels: {unknown!r}"
            raise ValueError(msg)
        _budget_invalid = (
            not isinstance(budget_tokens, int)
            or isinstance(budget_tokens, bool)
            or budget_tokens <= 0
        )
        if _budget_invalid:
            # 非正预算令护栏永不收紧（配置失效须装配期暴露，同熔断器口径）。
            msg = f"budget_tokens must be a positive integer, got {budget_tokens!r}"
            raise ValueError(msg)
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            # nan 令窗口语义未定义、inf 令条目永不过期（配置失效须暴露）。
            msg = f"window_seconds must be a positive finite number, got {window_seconds!r}"
            raise ValueError(msg)
        self._primary_client = primary_client
        self._mini_client = mini_client
        self._simple_intents = frozenset(simple_intents)
        self._budget_tokens = budget_tokens
        self._window_seconds = window_seconds
        self._metrics = metrics
        self._clock = clock
        self._lock = threading.Lock()
        # (timestamp, tokens) 双端队列：左侧最旧，判定与计数时剔除窗口外
        # （age >= window_seconds）条目；队列规模受窗口时长天然有界。
        self._window: deque[tuple[float, int]] = deque()
        self._tightened = False

    @property
    def primary_alias(self) -> str:
        """主模型 alias（组合根回读测试与调试消费）。"""
        return self._primary_alias

    @property
    def mini_alias(self) -> str:
        """mini 模型 alias（组合根回读测试与调试消费）。"""
        return self._mini_alias

    @property
    def simple_intents(self) -> frozenset[str]:
        """生效的简单意图名单（组合根回读测试与调试消费）。"""
        return self._simple_intents

    @property
    def budget_tokens(self) -> int:
        """滑窗 token 预算阈值（组合根回读测试与调试消费）。"""
        return self._budget_tokens

    @property
    def window_seconds(self) -> float:
        """滑窗时长秒数（组合根回读测试与调试消费）。"""
        return self._window_seconds

    @property
    def tightened(self) -> bool:
        """当前是否处于预算收紧态（指标/调试消费）。"""
        with self._lock:
            return self._tightened

    def select(self, intent: IntentTag | None) -> RoutingDecision:
        """按 intent 与护栏状态选定生成 client（判定本身不计数）。

        每次判定先剔除窗口外条目再累计：累计 ≥ 阈值即收紧（后续全部
        强制 mini， outcome=forced_mini）；收紧后累计回落到阈值以下即
        恢复常规路由（outcome 照常、transition=recovered 补记事件）。
        intent=None（分类 fail-closed）与名单外意图一律走主模型。
        """
        with self._lock:
            total = self._prune_and_total()
            if total >= self._budget_tokens:
                if not self._tightened:
                    # 收紧迁移：只记一次超限与状态（持续收紧期不重复计数）。
                    self._tightened = True
                    self._set_metrics_tightened(True)
                    self._record_over_limit()
                return RoutingDecision(
                    client=self._mini_client, outcome=OUTCOME_FORCED_MINI
                )
            transition: Literal["recovered"] | None = None
            if self._tightened:
                # 恢复迁移：累计 < 阈值即回到常规路由（无迟滞带）。
                self._tightened = False
                self._set_metrics_tightened(False)
                self._record_recovery()
                transition = OUTCOME_RECOVERED
            if intent is not None and intent in self._simple_intents:
                return RoutingDecision(
                    client=self._mini_client, outcome=OUTCOME_MINI, transition=transition
                )
            return RoutingDecision(
                client=self._primary_client, outcome=OUTCOME_PRIMARY, transition=transition
            )

    def account(self, decision: RoutingDecision, usage: Mapping[str, int]) -> None:
        """成功生成后记账：usage 计入滑窗 + 按 outcome 上报指标。

        只统计成功回传的 usage（调用方只在 generate 成功后调用）；
        ``total_tokens`` 缺失/非数值容忍为 0（替身响应无 usage_metadata
        时预算不受影响）。forced_mini 走 mini 模型，计入 mini 口径。
        """
        tokens = _extract_total_tokens(usage)
        with self._lock:
            self._prune_and_total()
            self._window.append((self._clock(), tokens))
        self._record_call(decision.outcome, tokens)

    def snapshot(self) -> dict[str, Any]:
        """内部状态读数（调试与单测断言；/health 走指标快照非本接口）。"""
        with self._lock:
            total = self._prune_and_total()
            return {
                "tightened": self._tightened,
                "window_tokens": total,
            }

    def _prune_and_total(self) -> int:
        """剔除窗口外条目（age >= window）并返回窗内累计 token。

        持锁调用（self._lock 保护双端队列）；返回前队列左侧全部为
        窗口内条目。
        """
        now = self._clock()
        cutoff = now - self._window_seconds
        while self._window and self._window[0][0] <= cutoff:
            self._window.popleft()
        return sum(tokens for _, tokens in self._window)

    # ---- 埋点（全部 fail-safe：收集器异常吞掉，绝不影响请求路径）----

    def _set_metrics_tightened(self, tightened: bool) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.set_tightened(tightened)

    def _record_over_limit(self) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.record_over_limit()

    def _record_recovery(self) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.record_recovery()

    def _record_call(self, outcome: RoutingOutcome, tokens: int) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.record_call(outcome, tokens)


def _extract_total_tokens(usage: Mapping[str, int]) -> int:
    """取 ``total_tokens`` 标量；缺失/None/非数值容忍为 0（fail-safe）。"""
    try:
        value = usage.get("total_tokens", 0)
        return max(0, int(value)) if isinstance(value, (int, float)) else 0
    except Exception:
        return 0


__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "DEFAULT_MINI_MODEL_ALIAS",
    "DEFAULT_SIMPLE_INTENTS",
    "DEFAULT_WINDOW_SECONDS",
    "KNOWN_INTENTS",
    "ModelRouter",
    "OUTCOME_FORCED_MINI",
    "OUTCOME_MINI",
    "OUTCOME_PRIMARY",
    "OUTCOME_RECOVERED",
    "RoutingDecision",
    "RoutingOutcome",
]
