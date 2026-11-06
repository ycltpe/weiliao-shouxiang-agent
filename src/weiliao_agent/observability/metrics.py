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
