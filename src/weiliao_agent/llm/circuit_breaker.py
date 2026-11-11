"""LLM 生成链路熔断器（Story 4.5）：三态状态机 + 确定性步进恢复。

动机（spec Intent）：生成链路虽全路径 fail-safe，但每次故障请求都要
傻等满生成预算才 fallback，故障期间持续烧预算且无恢复机制。本模块在
过滤器层叠一层 thread-safe 熔断器（不改 ``llm/client.py`` 重试/预算语义）：

- closed：正常放行，故障信号（OpenAI 侧 reason 词表）即跳闸 open；
- open：冷却期内请求立即拒绝（调用方不调子图、零 I/O 回模板兜底）；
- 冷却满后经 half-open 按步进比例确定性放量试探（取模计数，非随机）：
  试探成功放行比例递增、达 100% 闭合 closed 并记 recovered；试探失败
  立即回 open 并重启冷却。

判定口径（spec Boundaries）：故障词表只看 OpenAI 侧原因
``rate_limit`` / ``server_<status>`` / ``timeout`` / ``attempt_timeout``
/ ``network``（以 ``llm/client.py`` ``_classify_error`` 产出为准）；
确定性失败（400 系、凭据类型名、``invalid_output``）与 grounding 未过
（内容问题，中性）一律不跳闸。

纯进程内状态，不依赖 OpenAI SDK；全部埋点 fail-safe（metrics 收集器
异常吞掉），绝不让请求路径因熔断逻辑抛错。
"""

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from weiliao_agent.observability.metrics import LlmBreakerMetricsCollector

# 冷却期缺省秒数（组合根经 WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS 覆盖）。
DEFAULT_COOLDOWN_SECONDS = 10.0

# 恢复步进缺省值（组合根经 WEILIAO_LLM_BREAKER_RECOVERY_STEP 覆盖）：
# 0.1 即 half-open 按 10% 起步、每次试探成功 +10%、达 100% 闭合。
DEFAULT_RECOVERY_STEP = 0.1

# 三态状态机状态名（trace 事件 / 指标快照 / 告警日志共用词表）。
STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"

# OpenAI 侧故障 reason 词表（spec Boundaries，权威产出在
# llm/client.py _classify_error：429 / 5xx / 超时 / 网络错误）。
# 同步契约：本词表是 _classify_error 可重试产出的手工镜像——client
# 侧新增可重试 reason 时须同步此处；tests/test_llm_breaker.py 的
# test_trip_vocab_matches_classifier_contract 钉住对齐防两处漂移。
_TRANSIENT_REASONS = frozenset({"rate_limit", "timeout", "attempt_timeout", "network"})
_SERVER_REASON_PREFIX = "server_"

# 未携带模型 alias 的信号统一落入该缺省键（单模型路径与既有测试口径）。
_DEFAULT_MODEL_KEY = "__default__"

# 三态严重度序（指标 state 单值上报时取各模型键中的最重态）。
_STATE_SEVERITY = {STATE_CLOSED: 0, STATE_HALF_OPEN: 1, STATE_OPEN: 2}


def is_transient_reason(reason: str) -> bool:
    """reason 是否属 OpenAI 侧故障（跳闸词表）；确定性失败返回 False。"""
    return reason in _TRANSIENT_REASONS or reason.startswith(_SERVER_REASON_PREFIX)


@dataclass
class _BreakerState:
    """单模型键的三态机状态（code review Epic 4 补丁：按 alias 分键）。"""

    state: str = STATE_CLOSED
    opened_at: float = 0.0
    # half-open 当前放行步数 k（1 = 一个步进比例，如 10%）。
    current_step: int = 1
    # half-open 确定性取模计数（每次咨询递增，与 k 比较决定放行）。
    probe_counter: int = 0


class LlmCircuitBreaker:
    """LLM 生成链路熔断器：closed/open/half-open 三态，线程安全。

    按模型 alias 分键（code review Epic 4 补丁）：各方法可选 ``model``
    参数隔离状态——mini 模型瞬态故障只熔断 mini 键，主模型健康路径
    不受影响（此前全局单状态会让单模型故障连坐全链路）；缺省 None
    落 ``__default__`` 键，单模型路径与既有调用口径逐字节不变。

    挂点（由 LlmGenerateFilter 消费，breaker 为可选注入——None=不熔断）：
    - ``before_request(model)``：请求前咨询，False 即跳过（调用方不调子图）；
    - ``on_success(model)``：生成未抛 OpenAI 故障即成功；返回 True 表示本次
      信号令熔断器闭合（recovered，调用方记 trace 事件）；
    - ``on_failure(reason, model)``：仅故障词表跳闸；返回 True 表示本次失败令
      熔断器进入/重回 open（调用方记 ERROR 级 tripped 事件）。

    构造参数 fail-fast（非正冷却 / 越界步进抛 ValueError）；``clock``
    可注入 fake 时钟（测试控制冷却）；``metrics`` 注入指标收集器单例
    （None=不上报，保持纯状态机可单测）。
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        recovery_step: float = DEFAULT_RECOVERY_STEP,
        metrics: "LlmBreakerMetricsCollector | None" = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cooldown_seconds <= 0:
            # 非正冷却会让 open 态永久拒绝流量（配置错误须装配期暴露）。
            msg = f"cooldown_seconds must be positive, got {cooldown_seconds!r}"
            raise ValueError(msg)
        if not 0.0 < recovery_step <= 1.0:
            msg = f"recovery_step must be within (0, 1], got {recovery_step!r}"
            raise ValueError(msg)
        self._cooldown_seconds = cooldown_seconds
        self._recovery_step = recovery_step
        # 量化口径：步进取整为 N = round(1/step) 档，实际放行比例 =
        # k/N（k 为当前步数）——step=0.1 → N=10（10% 步进），
        # step=0.3 → N=3（33.3% 步进）；round 收敛浮点误差（1/0.1）。
        self._steps_total = max(1, round(1.0 / recovery_step))
        self._metrics = metrics
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[str, _BreakerState] = {}

    def _state_for(self, model: str | None) -> _BreakerState:
        """取（惰性建）模型键状态；调用方须已持锁。"""
        key = model if model else _DEFAULT_MODEL_KEY
        state = self._states.get(key)
        if state is None:
            state = _BreakerState()
            self._states[key] = state
        return state

    def _worst_state(self) -> str:
        """各模型键中的最重态（指标单值 state 上报口径）；须已持锁。"""
        if not self._states:
            return STATE_CLOSED
        return max(self._states.values(), key=lambda s: _STATE_SEVERITY[s.state]).state

    @property
    def state(self) -> str:
        """当前三态（closed/open/half_open），指标快照与测试断言消费。"""
        with self._lock:
            return self._worst_state()

    def before_request(self, model: str | None = None) -> bool:
        """请求前咨询：True 放行（计 request），False 拒绝（计 skipped）。

        open 且冷却未满：拒绝；冷却已满：转 half-open 并从首个步进比例
        开始试探。half-open：按 ``counter % N < k`` 确定性放量（无随机）。
        """
        with self._lock:
            state = self._state_for(model)
            if state.state == STATE_OPEN:
                if self._clock() - state.opened_at < self._cooldown_seconds:
                    self._record_skipped()
                    return False
                # 冷却已满：转入 half-open 步进试探（放行计数复位）。
                state.state = STATE_HALF_OPEN
                state.current_step = 1
                state.probe_counter = 0
                self._set_metrics_state(self._worst_state())
            if state.state == STATE_HALF_OPEN:
                index = state.probe_counter
                state.probe_counter += 1
                if index % self._steps_total < state.current_step:
                    self._record_request()
                    return True
                self._record_skipped()
                return False
            self._record_request()
            return True

    def on_success(self, model: str | None = None) -> bool:
        """成功信号：half-open 放行请求推进恢复；closed/open 态忽略。

        试探成功放行比例 +1 步；达 100%（k == N）闭合 closed 并返回
        True（调用方记 recovered 事件）；其余返回 False。
        """
        with self._lock:
            state = self._state_for(model)
            if state.state != STATE_HALF_OPEN:
                return False
            state.current_step += 1
            if state.current_step >= self._steps_total:
                state.state = STATE_CLOSED
                self._record_recovery()
                self._set_metrics_state(self._worst_state())
                return True
            return False

    def on_failure(self, reason: str, model: str | None = None) -> bool:
        """故障信号：仅 OpenAI 侧故障词表跳闸；其余 reason 不动状态。

        closed → open 跳闸；half-open 试探失败回 open 并重启冷却；已
        open 时不重启冷却（并发迟到失败不得延长故障窗口）。返回 True
        表示本次失败造成了 open 状态迁移（调用方记 tripped 事件）。
        """
        if not is_transient_reason(reason):
            return False
        with self._lock:
            state = self._state_for(model)
            if state.state == STATE_OPEN:
                return False
            state.state = STATE_OPEN
            state.opened_at = self._clock()
            state.current_step = 1
            state.probe_counter = 0
            self._record_trip()
            self._set_metrics_state(self._worst_state())
            return True

    def snapshot(self) -> dict[str, Any]:
        """内部状态读数（调试与单测断言；/health 走指标快照非本接口）。"""
        with self._lock:
            default = self._state_for(None)
            snapshot: dict[str, Any] = {
                "state": self._worst_state(),
                "current_step": default.current_step,
                "steps_total": self._steps_total,
                "opened_at": default.opened_at,
            }
            # 各模型键读数（分键补丁）：缺省键之外的分键状态在此可见。
            snapshot["models"] = {
                key: {"state": s.state, "current_step": s.current_step}
                for key, s in self._states.items()
                if key != _DEFAULT_MODEL_KEY
            }
            return snapshot

    # ---- 埋点（全部 fail-safe：收集器异常吞掉，绝不影响请求路径）----

    def _record_request(self) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.record_request()

    def _record_skipped(self) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.record_skipped()

    def _record_trip(self) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.record_trip()

    def _record_recovery(self) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.record_recovery()

    def _set_metrics_state(self, state: str) -> None:
        if self._metrics is not None:
            with suppress(Exception):
                self._metrics.set_state(state)
