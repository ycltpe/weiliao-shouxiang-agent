"""L1 进程内缓存客户端（Story 3.3，AD-5）。

会话级热点缓存：cachetools.TTLCache 进程内封装，仅存三类会话热点——
最近意图（IntentTag）、追问计数快照（int）、handoff 状态快照（str），
按 session_id 键控。事实应答数据（FactCard）严禁入 L1（类型签名 +
运行时校验双重拒绝），只能经 L0 进入信封（查找顺序 L1 → L0 → LLM
由 cache_lookup 单一落点承载）。

定位（AD-5 / NFR-8）：L1 是优化层，非正确性依赖——内容为易逝优化
副本，任意实例可处理任意消息（读者 miss / 过期一律回落 Redis 权威
值，由调用方自行裁决），进程重启即清零。追问计数权威值只存 Redis
（AD-4），L1 快照仅作读优化；硬触发判定（Epic 5，AD-12）禁读 L1。

线程安全：TTLCache 非线程安全，而管道跑在 ThreadPoolExecutor 上，
故单把 ``threading.Lock`` 包裹全部读写（操作 O(1)，持锁时间可忽略；
分片锁无实测竞争依据，YAGNI）。handoff 快照失效语义：写入后经
``invalidate_handoff`` 立即删除（转人工判定不得读到过期快照），
为 Epic 5 的 mark_handoff 预置契约。

Fail-fast 与 fail-safe 的边界：TTL 上限（AD-5 硬上限 30s）与类型
错误属程序错误，构造/写入即抛（fail-fast）；但调用方（intent_router）
对一切写入异常静默吞掉、不记 trace、不应答降级——L1 写失败只可能是
程序错误，记 trace 反而给热路径加噪音（设计笔记的非对称设计）。
"""

import threading
import time
from collections.abc import Callable, Hashable
from typing import cast, get_args

from cachetools import TTLCache

from weiliao_agent.models.intent import IntentTag

# L1 TTL 缺省 30 秒；AD-5 硬上限 30 秒——传入 >30 一律 fail-fast。
L1_DEFAULT_TTL_SECONDS = 30
L1_MAX_TTL_SECONDS = 30
# L1 容量缺省 4096 条（LRU 逐出最旧条目，客户端不抛异常）。
L1_DEFAULT_MAXSIZE = 4096

# 三类快照的类别键前缀：单缓存内以 (kind, session_id) 复合键区分。
_KIND_RECENT_INTENT = "recent_intent"
_KIND_FOLLOWUP_COUNT = "followup_count"
_KIND_HANDOFF_STATUS = "handoff_status"

# IntentTag 9 类标签的运行时可判定口径：与 models/intent.py 的
# Literal 权威定义同源派生（防手抄漂移——未来新增第 10 类标签时
# mypy 与运行时口径同步生效）；set_recent_intent 拒收枚举外值与
# FactCard 等非 str 对象（类型拒绝）。
_INTENT_TAGS: tuple[str, ...] = get_args(IntentTag)


class L1CacheClient:
    """L1 进程内缓存：三类会话热点快照的读写 / 作废 API。

    进程内单实例（组合根接线，模块级共享）；实例自身除缓存内容外
    无权威状态。``timer`` 可注入（测试假时钟驱动 TTL 过期断言）。
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = L1_DEFAULT_TTL_SECONDS,
        maxsize: int = L1_DEFAULT_MAXSIZE,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            # 防御：TTLCache(ttl=0) 不报错但条目写入即过期——永不命中的
            # 死缓存比快速失败更危险（构造期 fail-fast）。
            msg = f"ttl_seconds must be positive, got {ttl_seconds}"
            raise ValueError(msg)
        if ttl_seconds > L1_MAX_TTL_SECONDS:
            msg = (
                f"ttl_seconds={ttl_seconds} exceeds AD-5 hard limit "
                f"{L1_MAX_TTL_SECONDS}s for L1 in-process cache"
            )
            raise ValueError(msg)
        if maxsize < 1:
            # 防御：cachetools 延迟到首次写入才抛内部 ValueError
            # （"value too large"），构造期 fail-fast 给出明确口径。
            msg = f"maxsize must be at least 1, got {maxsize}"
            raise ValueError(msg)
        self._lock = threading.Lock()
        self._cache: TTLCache[Hashable, object] = TTLCache(
            maxsize=maxsize, ttl=ttl_seconds, timer=timer
        )

    def set_recent_intent(self, session_id: str, intent: IntentTag) -> None:
        """写最近意图快照（TTL 内 get_recent_intent 命中）。

        类型拒绝（fail-fast）：intent 非 IntentTag 9 类之一（含
        FactCard 等任意非 str 对象）抛 ``TypeError``——调用方
        （intent_router）以 try 包裹静默吞掉，不影响买家应答。
        """
        if not isinstance(intent, str) or intent not in _INTENT_TAGS:
            raise TypeError(
                f"recent_intent must be an IntentTag, got {type(intent).__name__}"
            )
        with self._lock:
            self._cache[(_KIND_RECENT_INTENT, session_id)] = intent

    def get_recent_intent(self, session_id: str) -> IntentTag | None:
        """读最近意图快照；miss / 过期返回 None（调用方回落权威值）。"""
        with self._lock:
            value = self._cache.get((_KIND_RECENT_INTENT, session_id))
        return cast("IntentTag | None", value)

    def set_followup_count(self, session_id: str, count: int) -> None:
        """写追问计数快照（会话总数，int）；类型拒绝非 int（含 FactCard）。"""
        if isinstance(count, bool) or not isinstance(count, int):
            # bool 是 int 子类：True/False 会穿透 isinstance 检查，
            # 类型拒绝口径须显式排除。
            raise TypeError(
                f"followup_count must be an int, got {type(count).__name__}"
            )
        with self._lock:
            self._cache[(_KIND_FOLLOWUP_COUNT, session_id)] = count

    def get_followup_count(self, session_id: str) -> int | None:
        """读追问计数快照；miss / 过期返回 None（硬触发判定须回 Redis 权威值）。"""
        with self._lock:
            value = self._cache.get((_KIND_FOLLOWUP_COUNT, session_id))
        return cast("int | None", value)

    def set_handoff_status(self, session_id: str, status: str) -> None:
        """写 handoff 状态快照（str）；类型拒绝非 str（含 FactCard）。"""
        if not isinstance(status, str):
            raise TypeError(
                f"handoff_status must be a str, got {type(status).__name__}"
            )
        with self._lock:
            self._cache[(_KIND_HANDOFF_STATUS, session_id)] = status

    def get_handoff_status(self, session_id: str) -> str | None:
        """读 handoff 状态快照；miss / 过期返回 None。"""
        with self._lock:
            value = self._cache.get((_KIND_HANDOFF_STATUS, session_id))
        return cast("str | None", value)

    def invalidate_handoff(self, session_id: str) -> None:
        """handoff 快照立即作废（同步删除，转人工判定不得读过期快照）。

        快照不存在时为空操作；为 Epic 5 的 mark_handoff 预置的失效
        契约——handoff 状态变更写入后必须经本方法同步作废 L1 快照。
        """
        with self._lock:
            self._cache.pop((_KIND_HANDOFF_STATUS, session_id), None)
