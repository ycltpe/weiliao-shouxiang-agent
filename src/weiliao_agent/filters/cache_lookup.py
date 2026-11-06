"""缓存查找过滤器（Story 1.2 占位透传；Story 3.3 分层查找落点）。

Story 1.2 占位行为（未注入 L0 客户端）：直接透传上游信封（既有测试
逐字节锁定，零回归）。

Story 3.3：可选注入 ``L0CacheClient``（组合根模块级实例，AD-5）——
分层查找顺序 L1 → L0 → LLM 的 L0 落点：intent 缺失跳过查询直接透传；
否则 L0 ``get``，命中把 ``FactCard`` 挂到信封 ``fact_card`` 字段
（决策 A 方案），miss 保持 None 留给 Epic 4 的 LLM 层并记 ``miss``
trace 事件（与 L0 客户端的 hit 成对，Story 3.4 命中率口径前置）。
Redis 故障经 L0 客户端 fail-safe 归并为 miss（failure 事件在客户端
侧记录），本过滤器不抛、不影响买家应答。

Story 3.4：信封 ``is_followup=True``（intent_router 上游按 per-intent
≥2 写好，本过滤器不自行判定）整体跳过 L0 查询——不查、不挂卡片、记
``bypass`` trace 事件（与 hit/miss 同一 taxonomy，「同一 session 同一
意图第 2 次消息 100% 进入 LLM 链路」以 bypass 事件成对可验）、
``bypassed`` 计数 +1 且完全不计入命中率分子/分母。非追问分支在 3.3
行为上按查询结果挂 ``record_lookup(hit=...)`` 计数（命中与否均计入
分母）。metrics 记录经 try 包裹 fail-safe（再抛也吞掉，对齐 tracing
口径），过滤器自身不抛。
"""

from contextlib import suppress

from weiliao_agent.cache.l0_client import L0CacheClient
from weiliao_agent.filters.base import Filter
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import metrics, tracing


class CacheLookupFilter(Filter):
    """缓存查找：L0 命中挂事实卡片到信封，miss 透传留给 LLM 层。"""

    name = "cache_lookup"

    def __init__(self, l0_client: L0CacheClient | None = None) -> None:
        self._l0_client = l0_client

    def process(self, envelope: Envelope) -> Envelope:
        if self._l0_client is None:
            # 占位行为（Story 1.2，既有测试锁定）：直接透传上游信封。
            return envelope
        if envelope.intent is None:
            # 无 intent 即无 L0 key 可寻址（索引键首段）：跳过查询直接
            # 透传，下游兜底；miss 事件无从归因 intent，也不记录。
            return envelope
        if envelope.is_followup:
            # 追问禁走 L0（Story 3.4，冻结区）：无论 L0 是否有匹配卡片
            # 一律不查——买家重复追问必须进入 LLM 链路拿新鲜生成，绝不
            # 复读缓存。bypass 事件可观测且不计入命中率分子/分母。
            tracing.record_cache_event(
                envelope.trace_id,
                operation="bypass",
                stage="get",
                metadata={"intent": envelope.intent, "house_id": envelope.house_id},
            )
            with suppress(Exception):
                metrics.get_cache_metrics().record_bypass()
            return envelope
        card = self._l0_client.get(
            envelope.intent, envelope.house_id, trace_id=envelope.trace_id
        )
        # 非追问实际查询（hit/miss 均计分母；Redis 故障经客户端归并
        # miss 后 card=None，同样计 miss——3.3 事件口径）。
        with suppress(Exception):
            metrics.get_cache_metrics().record_lookup(hit=card is not None)
        if card is not None:
            return envelope.model_copy(update={"fact_card": card})
        # miss（含 Redis 故障经 L0 客户端 fail-safe 归并）：fact_card
        # 保持 None 留给 Epic 4 的 LLM 链路；miss 事件可观测。
        tracing.record_cache_event(
            envelope.trace_id,
            operation="miss",
            stage="get",
            metadata={"intent": envelope.intent, "house_id": envelope.house_id},
        )
        return envelope
