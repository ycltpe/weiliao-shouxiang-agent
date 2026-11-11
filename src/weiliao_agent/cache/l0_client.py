"""L0 Redis 缓存客户端（Story 3.1，Epic 3 地基，AD-5）。

事实卡片的唯一缓存层：key 结构 ``weiliao:l0:{intent}:{house_id}:{status}:
{price_bucket}``（验收标准钉死）、TTL 300 秒、JSON 序列化。读取方事先
不知道 ``status`` / ``price_bucket`` 两值，故引入二级索引键
``weiliao:l0:idx:{intent}:{house_id}`` 存当前完整卡片 key；失效采用
逻辑作废（墓碑键 ``weiliao:l0:tomb:{house_id}`` 标记，非物理删除，
旧值留 Redis 可审计），墓碑与数据同 TTL 语义、随写入刷新。

部署约束：单实例 Redis（key 结构按冻结区验收标准固定，不含 cluster
hash tag；Lua 多键脚本假定同槽位）。

原子性：读/写/作废均为 Lua 脚本单 roundtrip——读路径「墓碑 → 索引 →
卡片」一次完成，无客户端往返间隙，invalidate/set 无法在读取中途插入，
保证已作废房源的读取永不错误返回旧卡片（评审循环 1 修订）。

乱序版本语义权威定义（评审循环 2）：**墓碑版本 V 表示「fact_version
≤ V 的事实均已作废」**。据此：

- ``get`` 仅在「无墓碑，或卡片 fact_version > 墓碑版本」时返回卡片；
  墓碑存在且卡片版本不高于墓碑版本时返回 None（作废不遮蔽更新事实）。
- ``set`` 双重守卫：(a) 墓碑存在且墓碑版本 ≥ 新卡片版本即拒写
  （迟到的低版本事件不得复活、同版本重放不得撤销作废）；(b) 索引指向
  的现存卡片版本不低于新卡片版本则不重指索引（旧事件不得回指旧事实）。
  守卫通过才写卡片键、重指索引、双双 EXPIRE——**不清墓碑**（评审循环
  3：墓碑随 TTL 自然过期；DEL 会造成跨 intent 已作废旧事实复活）。
- 三个 Lua 中 ``tonumber`` 结果一律先作 nil 检查：解码出的
  fact_version 缺失或非数值时按「无版本信息」fail-open（不触发 Lua
  比较错误、不令 invalidate 整体失败）。
- ``invalidate`` 的 fact_version 为**必填**（缺省即抛 ``ValueError``——
  无版本墓碑在读路径 fail-open 下不遮蔽任何卡片，静默空操作的作废比
  快速失败更危险）；墓碑版本取「现存墓碑版本与传入版本之最大者」——
  迟到的低版本 invalidate 不得拉低墓碑（作废不遮蔽更新事实）。

失效粒度：house_id（房源状态变更影响该房源全部 intent 的缓存）。

Fail-safe（冻结区）：Redis 故障（``RedisError``）一律不抛——``get``
视为 miss 返回 None，``set`` / ``invalidate`` 静默失败，Langfuse 记录
失败事件，应答链路零影响。原始字节的 UTF-8 解码损坏与 JSON/schema
损坏一样归入 corrupted 视为 miss（fail-safe 承诺覆盖一切损坏数据）。

事件 taxonomy（Langfuse，trace_id 可贯穿）：
``operation ∈ {hit, write, invalidated, corrupted, failure}``（failure
与 corrupted 为 ERROR 级；stage 词表：set / get / invalidate）。
"""

import json
from datetime import UTC, datetime
from typing import Any, cast

import redis

from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.models.intent import IntentTag
from weiliao_agent.observability import tracing

# L0 key 前缀（架构一致性约定）。
KEY_PREFIX = "weiliao:l0:"
# 二级索引键前缀：存当前完整卡片 key（status/price_bucket 读取方未知）。
INDEX_KEY_PREFIX = "weiliao:l0:idx:"
# 墓碑键前缀：逻辑作废标记（非物理删除）。
TOMBSTONE_KEY_PREFIX = "weiliao:l0:tomb:"

# L0 TTL 默认 300 秒（AD-5：5 分钟，可配置——仅未来组合根 3.2 传入）。
DEFAULT_TTL_SECONDS = 300

# get 单 roundtrip 原子读「墓碑 → 索引 → 卡片」：返回
# ``{墓碑标记('v:'+版本或 ''), 卡片 payload(或 '')}`` 二元数组——Lua
# 数组遇 nil/false 会塌陷，故空值一律以空串哨兵承载；非法 UTF-8 字节
# 原样透传（cjson.encode 遇非法字节会抛错逃逸 fail-safe，故不用 JSON
# 承载卡片 payload），Python 侧解码损坏归入 corrupted。
_LUA_GET = """
local tomb_raw = redis.call('GET', KEYS[1])
local tomb_marker = ''
if tomb_raw then
  local ok, tomb = pcall(cjson.decode, tomb_raw)
  if ok and type(tomb) == 'table' then
    local v = tonumber(tomb['fact_version'])
    -- 缺失/非数值按「无版本信息」fail-open：不遮蔽任何卡片。
    if v ~= nil then
      tomb_marker = 'v:' .. v
    end
  end
end
local idx = redis.call('GET', KEYS[2])
local card = ''
if idx then
  local payload = redis.call('GET', idx)
  if payload then
    card = payload
  end
end
return { tomb_marker, card }
"""

# set 单 roundtrip 原子写「索引 + 值 + 双双 EXPIRE」，写入前双重守卫。
_LUA_SET = """
local tomb_key = KEYS[1]
local idx_key = KEYS[2]
local card_key = KEYS[3]
local card_json = ARGV[1]
local new_version = tonumber(ARGV[2])
local ttl_seconds = tonumber(ARGV[3])

-- 守卫 (a)：墓碑存在且其版本 >= 新卡片版本即拒写（迟到的低版本事件
-- 不得复活已作废房源、同版本重放不得撤销作废）。
local tomb_raw = redis.call('GET', tomb_key)
if tomb_raw then
  local ok, tomb = pcall(cjson.decode, tomb_raw)
  if ok and type(tomb) == 'table' then
    local tomb_version = tonumber(tomb['fact_version'])
    -- nil 检查：损坏墓碑按「无版本信息」fail-open（不触发比较错误）。
    if tomb_version ~= nil and tomb_version >= new_version then
      return 0
    end
  end
end

-- 守卫 (b)：索引指向的现存卡片版本不低于新卡片版本则不重指索引
-- （旧事件不得回指旧事实）；现存卡片损坏按「无版本信息」fail-open。
local existing_key = redis.call('GET', idx_key)
if existing_key then
  local existing_raw = redis.call('GET', existing_key)
  if existing_raw then
    local ok, existing = pcall(cjson.decode, existing_raw)
    if ok and type(existing) == 'table' then
      local existing_version = tonumber(existing['fact_version'])
      if existing_version ~= nil and existing_version >= new_version then
        return 0
      end
    end
  end
end

redis.call('SET', card_key, card_json)
redis.call('SET', idx_key, card_key)
redis.call('EXPIRE', card_key, ttl_seconds)
redis.call('EXPIRE', idx_key, ttl_seconds)
-- 评审循环 3：守卫通过后不清墓碑（DEL 墓碑会让其他 intent 的已作废旧
-- 卡片复活可命中）；墓碑随 TTL 自然过期，高版本卡片本就不被低版本
-- 墓碑遮蔽。
return 1
"""

# invalidate 单 roundtrip 原子写墓碑：版本取「现存与传入之最大者」
# （迟到的低版本 invalidate 不得拉低墓碑），TTL 同 300s 随写入刷新。
# 返回实际生效的墓碑版本（trace 事件据此记录，防低版本事件误导观测）。
_LUA_INVALIDATE = """
local tomb_key = KEYS[1]
local incoming_version = tonumber(ARGV[1])
local invalidated_at = ARGV[2]
local ttl_seconds = tonumber(ARGV[3])

local effective_version = incoming_version
local tomb_raw = redis.call('GET', tomb_key)
if tomb_raw then
  local ok, tomb = pcall(cjson.decode, tomb_raw)
  if ok and type(tomb) == 'table' then
    local tomb_version = tonumber(tomb['fact_version'])
    -- nil 检查：损坏现存墓碑按「无版本信息」fail-open（以传入版本为准）。
    if tomb_version ~= nil and tomb_version > effective_version then
      effective_version = tomb_version
    end
  end
end

local payload = cjson.encode({
  fact_version = effective_version,
  invalidated_at = invalidated_at
})
redis.call('SET', tomb_key, payload)
redis.call('EXPIRE', tomb_key, ttl_seconds)
return effective_version
"""


class L0CacheClient:
    """L0 Redis 缓存客户端：FactCard 的 get / set / invalidate 三 API。

    Redis 客户端注入（组合根 3.2 接线，本 Story 无生产调用方）；实例
    无状态、不持有任何进程内缓存副本（AD-5）。全部路径 fail-safe：
    ``RedisError`` 不抛、get 视 miss、set/invalidate 静默失败 + Langfuse
    failure 事件。
    """

    def __init__(
        self,
        client: redis.Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            msg = f"ttl_seconds must be positive, got {ttl_seconds}"
            raise ValueError(msg)
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._get_script = client.register_script(_LUA_GET)
        self._set_script = client.register_script(_LUA_SET)
        self._invalidate_script = client.register_script(_LUA_INVALIDATE)

    def tombstone_version(self, house_id: str) -> float | None:
        """只读读取现存墓碑版本（无副作用诊断探针）。

        供 Story 3.2 消费者区分「守卫拒写」与「Redis 故障」：与以
        ``invalidate`` 作探针不同，本方法不新建墓碑——瞬断恢复场景下
        副作用式探针新立的墓碑（版本恰等于事件版本）会反噬后续重试
        写入、致事实丢失（2026-11-11 评审 P1 修复）。返回 None 归并
        「无现存墓碑 / 墓碑无版本信息 / Redis 故障」三种情形（fail-safe：
        Redis 故障由调用方后续写入重试自行暴露并记 failure，本探针
        不重复记事件）。
        """
        try:
            raw = self._client.get(self._tombstone_key(house_id))
        except redis.RedisError:
            return None
        if raw is None:
            return None
        try:
            tomb = json.loads(self._decode(raw))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(tomb, dict):
            return None
        version = tomb.get("fact_version")
        # bool 是 int 子类：True/False 作版本无意义，归并「无版本信息」。
        if isinstance(version, bool) or not isinstance(version, (int, float)):
            return None
        return version

    def get(
        self,
        intent: IntentTag,
        house_id: str,
        *,
        trace_id: str | None = None,
    ) -> FactCard | None:
        """按 (intent, house_id) 读事实卡片；miss / 作废 / 损坏返回 None。

        Lua 单 roundtrip 原子读「墓碑 → 索引 → 卡片」，Python 侧裁决：
        无索引或无卡片 → miss；墓碑存在且卡片版本不高于墓碑版本 →
        返回 None（逻辑作废；更高版本卡片不被作废遮蔽）。命中经
        ``model_validate_json`` 校验，损坏/非法 UTF-8 归入 corrupted
        视为 miss。RedisError → None + failure 事件（fail-safe）。
        """
        stage = "get"
        try:
            raw = self._get_script(
                keys=[self._tombstone_key(house_id), self._index_key(intent, house_id)],
                args=[],
            )
            tomb_version, card_payload = self._decode_pair(raw, stage, trace_id)
            if card_payload is None:
                return None
            card = self._validate_card(card_payload, trace_id)
            if card is None:
                return None
            if tomb_version is not None and card.fact_version <= tomb_version:
                tracing.record_cache_event(
                    trace_id,
                    operation="invalidated",
                    stage=stage,
                    metadata={
                        "intent": intent,
                        "house_id": house_id,
                        "fact_version": card.fact_version,
                        "tombstone_version": tomb_version,
                    },
                )
                return None
            tracing.record_cache_event(
                trace_id,
                operation="hit",
                stage=stage,
                metadata={
                    "intent": intent,
                    "house_id": house_id,
                    "fact_version": card.fact_version,
                },
            )
            return card
        except redis.RedisError as exc:
            self._record_failure(exc, stage, intent, house_id, trace_id)
            return None

    def set(
        self,
        intent: IntentTag,
        card: FactCard,
        *,
        trace_id: str | None = None,
    ) -> bool:
        """写入事实卡片（索引 + 值 + 双双 EXPIRE 单 roundtrip）。

        双重 fact_version 守卫（墓碑版本 ≥ 新卡片版本拒写；现存卡片版本
        不低于新卡片版本不重指索引）；守卫通过不清墓碑（跨 intent 作废
        保留）。返回是否写入成功；守卫拒绝返回 False（无事件——拒绝
        可观测性归 Story 3.2 消费者侧）。RedisError → False + failure
        事件（静默失败，fail-safe）。
        """
        stage = "set"
        try:
            written = self._set_script(
                keys=[
                    self._tombstone_key(card.house_id),
                    self._index_key(intent, card.house_id),
                    self._card_key(intent, card),
                ],
                args=[card.model_dump_json(), card.fact_version, self._ttl_seconds],
            )
            if not int(cast("int | str | bytes", written)):
                return False
            tracing.record_cache_event(
                trace_id,
                operation="write",
                stage=stage,
                metadata={
                    "intent": intent,
                    "house_id": card.house_id,
                    "fact_version": card.fact_version,
                },
            )
            return True
        except redis.RedisError as exc:
            self._record_failure(exc, stage, intent, card.house_id, trace_id)
            return False

    def invalidate(
        self,
        house_id: str,
        fact_version: int | None = None,
        *,
        invalidated_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> bool:
        """逻辑作废某房源的全部 intent 缓存（墓碑标记，非物理删除）。

        ``fact_version`` 必填（默认 None 即缺省）：无版本墓碑在读路径
        fail-open 下不遮蔽任何卡片（作废形同虚设），静默空操作比快速
        失败更危险，缺省即抛 ``ValueError`` 且不写任何 Redis 键。墓碑
        版本取「现存与传入之最大者」；trace 事件记录 Lua 返回的实际生效
        版本。RedisError → False + failure 事件（静默失败，fail-safe）。
        """
        if fact_version is None:
            msg = "fact_version is required for invalidate (tombstone versioning)"
            raise ValueError(msg)
        stage = "invalidate"
        timestamp = invalidated_at if invalidated_at is not None else datetime.now(UTC)
        try:
            effective = self._invalidate_script(
                keys=[self._tombstone_key(house_id)],
                args=[fact_version, timestamp.isoformat(), self._ttl_seconds],
            )
            # Lua 返回的生效版本（现存墓碑与传入之最大者）可能因既有
            # 墓碑含浮点版本（如 2.5）而解析失败：按损坏归并——记
            # corrupted 事件、版本 metadata 置空，作废本身仍生效。
            effective_version: int | None
            try:
                effective_version = int(cast("int | str | bytes", effective))
            except (TypeError, ValueError):
                effective_version = None
                self._record_corrupted("malformed_effective_version", stage, trace_id)
            tracing.record_cache_event(
                trace_id,
                operation="invalidated",
                stage=stage,
                metadata={
                    "house_id": house_id,
                    "fact_version": effective_version,
                },
            )
            return True
        except redis.RedisError as exc:
            self._record_failure(exc, stage, None, house_id, trace_id)
            return False

    @staticmethod
    def _index_key(intent: IntentTag, house_id: str) -> str:
        return f"{INDEX_KEY_PREFIX}{intent}:{house_id}"

    @staticmethod
    def _card_key(intent: IntentTag, card: FactCard) -> str:
        return f"{KEY_PREFIX}{intent}:{card.house_id}:{card.status}:{card.price_bucket}"

    @staticmethod
    def _tombstone_key(house_id: str) -> str:
        return f"{TOMBSTONE_KEY_PREFIX}{house_id}"

    def _decode_pair(
        self, raw: Any, stage: str, trace_id: str | None
    ) -> tuple[float | None, str | None]:
        """解析 get Lua 返回的 ``{墓碑标记, 卡片 payload}`` 二元组。

        墓碑标记为 ``v:<版本>`` 或空值；卡片为空值视为 miss。形状不符
        （Redis 协议保证为二元数组，防御）或非法 UTF-8 字节一律按损坏
        归并：``(None, None)`` 视为 miss + corrupted 事件。
        """
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            self._record_corrupted("unexpected_eval_result", stage, trace_id)
            return None, None
        tomb_version: float | None = None
        tomb_raw, card_raw = raw
        if tomb_raw:
            try:
                marker = self._decode(cast("str | bytes", tomb_raw))
                if marker.startswith("v:"):
                    # float 解析与 _LUA_SET 的 tonumber 对齐：浮点版本
                    # （如手工修复写入的 v:2.5）参与版本比较而非按损坏
                    # fail-open——读开写闭裁决相反会卡死「读得到但写
                    # 不进」窗口（2026-11-11 评审 P2 修复）。
                    tomb_version = float(marker[2:])
                else:
                    self._record_corrupted(
                        "malformed_tombstone_version", stage, trace_id
                    )
            except (UnicodeDecodeError, ValueError):
                # 非法 UTF-8 或非整数版本（如 JSON 浮点 fact_version →
                # 标记 v:2.5）：按「无版本信息」fail-open，不遮蔽卡片。
                self._record_corrupted("malformed_tombstone_version", stage, trace_id)
        card_payload: str | None = None
        if card_raw:
            try:
                card_payload = self._decode(cast("str | bytes", card_raw))
            except UnicodeDecodeError:
                self._record_corrupted("utf8_decode_error", stage, trace_id)
        return tomb_version, card_payload

    def _validate_card(self, payload: str, trace_id: str | None) -> FactCard | None:
        """卡片 payload 反序列化：校验失败一律视为 miss + corrupted 事件。"""
        try:
            return FactCard.model_validate_json(payload)
        except ValueError:
            self._record_corrupted("card_payload_invalid", "get", trace_id)
            return None

    def _record_failure(
        self,
        exc: redis.RedisError,
        stage: str,
        intent: IntentTag | None,
        house_id: str,
        trace_id: str | None,
    ) -> None:
        """Redis 故障 fail-safe：ERROR failure 事件，不抛（应答零影响）。"""
        tracing.record_cache_event(
            trace_id,
            operation="failure",
            stage=stage,
            metadata={
                "intent": intent,
                "house_id": house_id,
                "error_type": type(exc).__name__,
            },
        )

    @staticmethod
    def _record_corrupted(reason: str, stage: str, trace_id: str | None) -> None:
        """损坏数据事件（ERROR corrupted）：视为 miss，绝不抛。"""
        tracing.record_cache_event(
            trace_id,
            operation="corrupted",
            stage=stage,
            metadata={"reason": reason},
        )

    @staticmethod
    def _decode(value: str | bytes) -> str:
        return value.decode() if isinstance(value, bytes) else value
