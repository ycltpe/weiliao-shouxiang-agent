"""缓存命中率指标收集器（Story 3.4，A 方案读数口径的权威落点）。

口径（spec Boundaries，冻结区）：
- 分子 = L0 命中数（计数器另留 ``l1_hit`` 预留并入位——AD-5 严禁 L1
  存事实卡片、3.3 亦未交付 L1 读路径，L1 当前无读消费者、贡献恒 0，
  未来承接读路径时并入分子，口径不变）；
- 分母 = 非追问且 intent 非 None 的实际 L0 查询数（hit + miss；
  Redis 故障经 L0 客户端 fail-safe 归并 miss，同 3.3 事件口径）；
- 被 bypass 的追问完全不计入分子也不计入分母，仅 ``bypassed`` 计数；
- 分母为 0 时 ``hit_rate`` 为 None（无任何实际查询，命中率无定义）。

线程安全：过滤器在 ThreadPoolExecutor 线程内执行，全部计数经
``threading.Lock`` 保护。记录入口自身不抛异常（调用方仍按 fail-safe
口径兜底吞错，对齐 tracing 约定）。

模块级单例供热路径与组合根共用；测试经 ``reset_cache_metrics`` 注入
重置实现隔离。
"""

import json
import logging
import threading
from contextlib import suppress
from typing import Any, TypedDict

# 审核结果归因词表：权威定义在 filters.risk_review.client（本模块只读
# 引用，防两处 Literal 漂移；client 无内部依赖，不构成导入环）。
from weiliao_agent.filters.risk_review.client import RiskReviewOutcome

_logger = logging.getLogger(__name__)


class CacheMetricsSnapshot(TypedDict):
    """``snapshot()`` 返回结构（/health 响应与定时日志共用）。"""

    l0_hit: int
    l0_miss: int
    l1_hit: int  # 预留并入位（当前无 L1 读消费者，恒 0）
    bypassed: int
    lookups: int  # 分母：非追问实际 L0 查询数
    hits: int  # 分子：L0 命中（L1 预留并入）
    hit_rate: float | None  # hits / lookups；分母 0 时为 None


class CacheMetricsCollector:
    """进程内缓存指标计数器（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._l0_hit = 0
        self._l0_miss = 0
        self._bypassed = 0
        self._l1_hit = 0

    def record_lookup(self, *, hit: bool) -> None:
        """记录一次非追问实际 L0 查询的结果（hit/miss 计入命中率分母）。"""
        with self._lock:
            if hit:
                self._l0_hit += 1
            else:
                self._l0_miss += 1

    def record_bypass(self) -> None:
        """记录一次追问禁走（不计入分子也不计入分母，仅 bypassed 计数）。"""
        with self._lock:
            self._bypassed += 1

    def reset(self) -> None:
        """清零全部计数器（测试隔离注入点）。"""
        with self._lock:
            self._l0_hit = 0
            self._l0_miss = 0
            self._bypassed = 0
            self._l1_hit = 0

    def snapshot(self) -> CacheMetricsSnapshot:
        """返回分子/分母/命中率读数（分母 0 时 hit_rate 为 None）。"""
        with self._lock:
            hits = self._l0_hit + self._l1_hit
            lookups = self._l0_hit + self._l0_miss
            l0_hit, l0_miss = self._l0_hit, self._l0_miss
            l1_hit, bypassed = self._l1_hit, self._bypassed
        return CacheMetricsSnapshot(
            l0_hit=l0_hit,
            l0_miss=l0_miss,
            l1_hit=l1_hit,
            bypassed=bypassed,
            lookups=lookups,
            hits=hits,
            hit_rate=hits / lookups if lookups else None,
        )


# 模块级单例：cache_lookup 热路径与组合根（/health 响应、定时日志）共用。
_collector = CacheMetricsCollector()


def get_cache_metrics() -> CacheMetricsCollector:
    """返回进程内模块级单例计数器。"""
    return _collector


def reset_cache_metrics() -> CacheMetricsCollector:
    """清零模块级单例并返回（测试隔离的注入重置点）。"""
    _collector.reset()
    return _collector


def emit_cache_metrics_log() -> None:
    """A 方案定时读数：结构化 JSON 单行经 logging 落 stderr。

    定时器调用点（main.py lifespan）与日志发射自身均 fail-safe：
    任何异常吞掉，指标记录绝不二次影响买家应答。

    消息体即纯 JSON（``record.message`` 可直接 ``json.loads``），无
    文本前缀——README「结构化 JSON 单行」口径。
    """
    with suppress(Exception):
        payload: dict[str, Any] = {
            "event": "cache_metrics",
            **get_cache_metrics().snapshot(),
        }
        _logger.info("%s", json.dumps(payload, sort_keys=True))


# 通过率告警阈值默认值（WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD 缺省）。
DEFAULT_RISK_REVIEW_PASS_RATE_THRESHOLD = 0.95

# 告警最小样本量：累计审核 <20 不告警，防小样本抖动误报。
MIN_RISK_REVIEW_ALERT_SAMPLES = 20


class RiskReviewMetricsSnapshot(TypedDict):
    """``snapshot()`` 返回结构（/health 响应与定时日志共用）。"""

    total: int  # 分母：实际审核调用数（含超时/错误）
    passed: int  # 分子：passed
    rejected: int
    timeout: int
    error: int
    pass_rate: float | None  # passed / total；分母 0 时为 None


class RiskReviewMetricsCollector:
    """后置审核通过率指标计数器（线程安全，仿 CacheMetricsCollector）。

    口径（spec Boundaries，冻结区）：分母 = 实际审核调用数（含超时/
    错误），分子 = passed；超时与错误计入失败。草稿为 None 的信封不调
    中台、不计分母（risk_review 过滤器保证）。分母 0 时 ``pass_rate``
    为 None（无实际审核，通过率无定义）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._passed = 0
        self._rejected = 0
        self._timeout = 0
        self._error = 0
        self._pass_rate_threshold = DEFAULT_RISK_REVIEW_PASS_RATE_THRESHOLD

    def record(self, outcome: RiskReviewOutcome) -> None:
        """记录一次实际审核调用的结果（passed/rejected/timeout/error）。"""
        with self._lock:
            if outcome == "passed":
                self._passed += 1
            elif outcome == "rejected":
                self._rejected += 1
            elif outcome == "timeout":
                self._timeout += 1
            else:
                self._error += 1

    def reset(self) -> None:
        """清零计数器并还原默认阈值（测试隔离注入点）。"""
        with self._lock:
            self._passed = 0
            self._rejected = 0
            self._timeout = 0
            self._error = 0
            self._pass_rate_threshold = DEFAULT_RISK_REVIEW_PASS_RATE_THRESHOLD

    def snapshot(self) -> RiskReviewMetricsSnapshot:
        """返回分子/分母/通过率读数（分母 0 时 pass_rate 为 None）。"""
        with self._lock:
            passed = self._passed
            rejected = self._rejected
            timeout = self._timeout
            error = self._error
        total = passed + rejected + timeout + error
        return RiskReviewMetricsSnapshot(
            total=total,
            passed=passed,
            rejected=rejected,
            timeout=timeout,
            error=error,
            pass_rate=passed / total if total else None,
        )

    @property
    def pass_rate_threshold(self) -> float:
        """跌破告警阈值（0–1，组合根经 env 配置并 fail-fast 校验）。"""
        with self._lock:
            return self._pass_rate_threshold

    def set_pass_rate_threshold(self, value: float) -> None:
        """更新告警阈值；越界（非 0–1）fail-fast（防御性二次校验）。"""
        if not 0.0 <= value <= 1.0:
            msg = f"pass_rate_threshold must be within 0-1, got {value!r}"
            raise ValueError(msg)
        with self._lock:
            self._pass_rate_threshold = value


# 模块级单例：risk_review 热路径与组合根（/health 响应、定时日志）共用。
_risk_review_collector = RiskReviewMetricsCollector()


def get_risk_review_metrics() -> RiskReviewMetricsCollector:
    """返回进程内模块级单例审核指标计数器。"""
    return _risk_review_collector


def reset_risk_review_metrics() -> RiskReviewMetricsCollector:
    """清零模块级单例并返回（测试隔离的注入重置点）。"""
    _risk_review_collector.reset()
    return _risk_review_collector


def emit_risk_review_metrics_log() -> None:
    """定时发射审核通过率结构化日志；跌破阈值追加 ERROR 级告警。

    告警口径（spec Boundaries）：累计审核样本 ≥ ``MIN_RISK_REVIEW_ALERT_SAMPLES``
    且 ``pass_rate < threshold`` 时发射 ERROR 级告警日志
    （``event=risk_review_alert``）；样本不足不告警防抖动。日志发射自身
    fail-safe：任何异常吞掉，指标记录绝不二次影响买家应答。消息体即
    纯 JSON（``record.message`` 可直接 ``json.loads``），同 cache 口径。
    """
    with suppress(Exception):
        collector = get_risk_review_metrics()
        snapshot = collector.snapshot()
        threshold = collector.pass_rate_threshold
        payload: dict[str, Any] = {
            "event": "risk_review_metrics",
            "pass_rate_threshold": threshold,
            **snapshot,
        }
        _logger.info("%s", json.dumps(payload, sort_keys=True))
        total = snapshot["total"]
        rate = snapshot["pass_rate"]
        if total >= MIN_RISK_REVIEW_ALERT_SAMPLES and rate is not None and rate < threshold:
            alert: dict[str, Any] = {
                "event": "risk_review_alert",
                "pass_rate": rate,
                "pass_rate_threshold": threshold,
                "total": total,
                "rejected": snapshot["rejected"],
                "timeout": snapshot["timeout"],
                "error": snapshot["error"],
            }
            _logger.error("%s", json.dumps(alert, sort_keys=True))


# ---- LLM 熔断器指标（Story 4.5，仿 RiskReviewMetricsCollector）----


class LlmBreakerMetricsSnapshot(TypedDict):
    """``snapshot()`` 返回结构（/health 响应与定时日志共用）。"""

    state: str  # 熔断器当前三态：closed / open / half_open
    requests: int  # 熔断放行（实际触达子图）的请求数
    skipped: int  # open/half-open 拒绝（零子图调用，立即模板兜底）数
    trips: int  # 跳闸次数（closed→open 与 half-open→open 迁移）
    recoveries: int  # 恢复闭合次数（half-open→closed 迁移）


class LlmBreakerMetricsCollector:
    """LLM 熔断器指标计数器（线程安全）。

    口径：计数由熔断器本体（``llm.circuit_breaker``）在状态迁移与放行
    决策点回写——requests/skipped 为决策计数，trips/recoveries 为状态
    迁移计数；state 由熔断器迁移时同步（快照含当前状态，/health 即读）。
    全部入口自身不抛异常（调用方仍按 fail-safe 口径兜底吞错，对齐
    tracing 约定）。模块级单例供热路径与组合根共用；测试经
    ``reset_llm_breaker_metrics`` 注入重置实现隔离。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "closed"
        self._requests = 0
        self._skipped = 0
        self._trips = 0
        self._recoveries = 0

    def record_request(self) -> None:
        """记录一次放行（请求将实际触达子图）。"""
        with self._lock:
            self._requests += 1

    def record_skipped(self) -> None:
        """记录一次拒绝（open/half-open 未放量，零子图调用）。"""
        with self._lock:
            self._skipped += 1

    def record_trip(self) -> None:
        """记录一次跳闸（进入 open）。"""
        with self._lock:
            self._trips += 1

    def record_recovery(self) -> None:
        """记录一次恢复闭合（回到 closed）。"""
        with self._lock:
            self._recoveries += 1

    def set_state(self, state: str) -> None:
        """同步熔断器当前三态（迁移点权威写入，快照/告警消费）。"""
        with self._lock:
            self._state = state

    def reset(self) -> None:
        """清零全部计数器并还原 closed 态（测试隔离注入点）。"""
        with self._lock:
            self._state = "closed"
            self._requests = 0
            self._skipped = 0
            self._trips = 0
            self._recoveries = 0

    def snapshot(self) -> LlmBreakerMetricsSnapshot:
        """返回状态 + 计数读数（/health 响应与定时日志共用）。"""
        with self._lock:
            return LlmBreakerMetricsSnapshot(
                state=self._state,
                requests=self._requests,
                skipped=self._skipped,
                trips=self._trips,
                recoveries=self._recoveries,
            )


# 模块级单例：熔断器与组合根（/health 响应、定时日志）共用。
_llm_breaker_collector = LlmBreakerMetricsCollector()


def get_llm_breaker_metrics() -> LlmBreakerMetricsCollector:
    """返回进程内模块级单例熔断器指标计数器。"""
    return _llm_breaker_collector


def reset_llm_breaker_metrics() -> LlmBreakerMetricsCollector:
    """清零模块级单例并返回（测试隔离的注入重置点）。"""
    _llm_breaker_collector.reset()
    return _llm_breaker_collector


def emit_llm_breaker_metrics_log() -> None:
    """定时发射熔断器结构化日志；非 closed 态追加 ERROR 级告警。

    告警口径（spec Boundaries）：熔断器非 closed 态（open/half_open，
    即降级持续中）时发射 ERROR 级 ``llm_breaker_alert`` 告警日志——
    与 risk_review 跌破阈值告警同通道，无需累计样本（状态本身即信号）。
    日志发射自身 fail-safe：任何异常吞掉，指标记录绝不二次影响买家应答。
    消息体即纯 JSON（``record.message`` 可直接 ``json.loads``），同 cache 口径。
    """
    with suppress(Exception):
        snapshot = get_llm_breaker_metrics().snapshot()
        payload: dict[str, Any] = {"event": "llm_breaker_metrics", **snapshot}
        _logger.info("%s", json.dumps(payload, sort_keys=True))
        if snapshot["state"] != "closed":
            alert: dict[str, Any] = {"event": "llm_breaker_alert", **snapshot}
            _logger.error("%s", json.dumps(alert, sort_keys=True))
