"""会话状态单写者（Story 1.3，AD-4 核心交付）。

会话状态仅本模块写入：Redis hash 单键 ``weiliao:session:{session_id}``，
字段 ``turn``（会话内绝对消息序号）、``followup_count``（追问计数，Epic 2
消费）、``handoff_status``（Epic 5 消费，本 Story 恒 ``"none"``）、
``last_message_at``（ISO8601 字符串）。TTL 30 分钟（可配置），每次入口
调用刷新。

原子性落点：``begin_turn`` 与 ``increment_followup`` 均为 Lua 脚本单
roundtrip（读旧态/初始化/自增/刷新时间戳/EXPIRE 一次完成），满足延迟
预算且并发安全；权威值只存 Redis，进程内不做任何会话状态缓存。

过期竞态语义：调用方（入口适配器）在消息入管道前取快照，处理中途 key
过期不中断在途消息；key 被 TTL 清除后重新发起即从零初始化（turn=1）。

Story 2.2 已交付：``write_intent`` 将分类结果写入 per-turn 键
``weiliao:session:{session_id}:intent:{turn}``（JSON：
``IntentClassification.model_dump_json()``，TTL 与会话一致），单写者
口径不变。

Story 2.3 已交付：``record_followup`` 经 Lua 单 roundtrip 原子维护
per-intent 追问计数（会话 hash 字段 ``followup:{TAG}``）与会话追问
总数 ``followup_count``（仅追问命中——per-intent ≥2——时自增），并
随脚本刷新 EXPIRE；返回 ``(per_intent_count, followup_count)`` 双计数。
追问判定的权威值只存 Redis（AD-4），UNKNOWN 不参与计数（调用方
口径）。仍待后续交付：``write_coref``（指代消解 Story）、
``mark_handoff``（Epic 5）及 ``:coref:{turn}`` 键落盘。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import redis

from weiliao_agent.models.intent import IntentClassification, IntentTag

# 会话状态 key 前缀（架构一致性约定）。
KEY_PREFIX = "weiliao:session:"

# 会话 TTL 默认 30 分钟（FR-10），可配置。
DEFAULT_TTL_SECONDS = 1800

_LUA_BEGIN_TURN = """
local key = KEYS[1]
local last_message_at = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
-- 新会话判定以 turn 字段是否存在为准（不能用 turn==1：increment_followup
-- 先执行时 hash 已存在但无 turn 字段，HINCRBY 得 1 会误判为新会话）。
local turn = redis.call('HINCRBY', key, 'turn', 1)
-- 仅补齐缺失字段：已有 followup_count 不得被静默清零。
if redis.call('HEXISTS', key, 'followup_count') == 0 then
  redis.call('HSET', key, 'followup_count', 0)
end
if redis.call('HEXISTS', key, 'handoff_status') == 0 then
  redis.call('HSET', key, 'handoff_status', 'none')
end
redis.call('HSET', key, 'last_message_at', last_message_at)
redis.call('EXPIRE', key, ttl_seconds)
local followup_count = tonumber(redis.call('HGET', key, 'followup_count'))
local handoff_status = redis.call('HGET', key, 'handoff_status')
return {turn, followup_count, handoff_status}
"""

_LUA_INCREMENT_FOLLOWUP = """
local key = KEYS[1]
local ttl_seconds = tonumber(ARGV[1])
local count = redis.call('HINCRBY', key, 'followup_count', 1)
redis.call('EXPIRE', key, ttl_seconds)
return count
"""

_LUA_RECORD_FOLLOWUP = """
local key = KEYS[1]
local tag_field = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local per_intent = redis.call('HINCRBY', key, tag_field, 1)
local session_count = 0
if per_intent >= 2 then
  -- 追问命中（同意图第 2 次起）才自增会话追问总数；否则只读出现值，
  -- 不得为首次出现创建 followup_count 字段（语义钉死：非 turn 序号）。
  session_count = redis.call('HINCRBY', key, 'followup_count', 1)
else
  local existing = redis.call('HGET', key, 'followup_count')
  if existing then
    session_count = tonumber(existing)
  end
end
redis.call('EXPIRE', key, ttl_seconds)
return {per_intent, session_count}
"""


@dataclass(frozen=True)
class SessionSnapshot:
    """入管道前的会话状态快照：权威值仅存 Redis，调用方按快照继续。"""

    turn: int
    followup_count: int
    handoff_status: str
    last_message_at: datetime


class SessionManager:
    """会话状态唯一写入者（AD-4）。Redis 客户端注入，实例无状态。

    Redis 故障处理策略由调用方收口：本模块不吞异常，入口适配器
    fail-closed 走既有兜底模式（复用 Story 1.2 错误兜底）。
    """

    def __init__(
        self,
        client: redis.Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._begin_turn_script = client.register_script(_LUA_BEGIN_TURN)
        self._increment_followup_script = client.register_script(_LUA_INCREMENT_FOLLOWUP)
        self._record_followup_script = client.register_script(_LUA_RECORD_FOLLOWUP)

    def begin_turn(self, session_id: str, timestamp: datetime) -> SessionSnapshot:
        """入管道前快照：初始化/自增 turn、刷新 TTL 与 ``last_message_at``。

        原子单 roundtrip；并发调用同 session 时各得唯一 turn。会话 TTL
        已过期时从零初始化（turn=1），无残留字段。
        """
        raw = self._begin_turn_script(
            keys=[self._key(session_id)],
            args=[timestamp.isoformat(), self._ttl_seconds],
        )
        turn, followup_count, handoff_status = cast(
            "tuple[object, object, object]", tuple(cast("list[object]", raw))
        )
        return SessionSnapshot(
            turn=int(cast("int | str | bytes", turn)),
            followup_count=int(cast("int | str | bytes", followup_count)),
            handoff_status=(
                handoff_status.decode()
                if isinstance(handoff_status, bytes)
                else str(handoff_status)
            ),
            last_message_at=timestamp,
        )

    def increment_followup(self, session_id: str) -> int:
        """追问计数 Redis 原子自增并刷新 TTL；返回权威值（Epic 2 消费）。

        警告（Story 2.3 语义钉死）：本原语无条件自增 ``followup_count``，
        而 Epic 2 追问计数的权威语义为「仅追问命中（per-intent ≥2）时
        自增」。追问识别必须只经 ``record_followup``——本方法不得用于
        追问计数，否则会把普通消息混入权威计数、污染 ≥3 信号。
        """
        count = self._increment_followup_script(
            keys=[self._key(session_id)],
            args=[self._ttl_seconds],
        )
        return int(cast("int | str | bytes", count))

    def record_followup(self, session_id: str, tag: IntentTag) -> tuple[int, int]:
        """追问识别原子记录（Story 2.3，AD-4）：per-intent 计数 + 会话总数。

        Lua 单 roundtrip：``HINCRBY followup:{TAG}`` 维护 per-intent 计数；
        仅当 per-intent ≥2（追问命中）时自增会话 ``followup_count``——
        否则恒等于 turn 序号，「≥3」会在第 3 条普通消息误触发；EXPIRE
        随脚本刷新，TTL 与会话一致。返回 ``(per_intent_count,
        session_followup_count)`` 双计数，调用方据 per-intent ≥2 判定
        ``is_followup``、据会话总数 ≥3 产出 hard trigger 信号。

        调用方口径（intent_router）：仅分类成功、turn 非 None 且 tag 非
        UNKNOWN 时调用；UNKNOWN 与本模块 1.3 的 ``increment_followup``
        原语均不参与本计数。写失败不吞异常：调用方经 try 包裹 fail-safe。
        """
        raw = self._record_followup_script(
            keys=[self._key(session_id)],
            args=[f"followup:{tag}", self._ttl_seconds],
        )
        per_intent, session_count = cast(
            "tuple[object, object]", tuple(cast("list[object]", raw))
        )
        return (
            int(cast("int | str | bytes", per_intent)),
            int(cast("int | str | bytes", session_count)),
        )

    def write_intent(
        self,
        session_id: str,
        turn: int,
        classification: IntentClassification,
    ) -> None:
        """分类结果单写者落盘（Story 2.2，AD-4）：per-turn 键 + 会话 TTL。

        ``SET weiliao:session:{session_id}:intent:{turn} {model_dump_json()} EX
        ttl_seconds`` 单 roundtrip；键独立于会话 hash，TTL 与会话一致
        （30 分钟默认）。写失败不吞异常：调用方（过滤器）经 try 包裹
        fail-safe，应答不降级。
        """
        key = f"{self._key(session_id)}:intent:{turn}"
        self._client.set(key, classification.model_dump_json(), ex=self._ttl_seconds)

    def read_snapshot(self, session_id: str) -> SessionSnapshot | None:
        """只读当前会话快照；key 不存在（已过期）或为缺 turn /
        ``last_message_at`` 的不完整 hash 时返回 None（防御非本模块写入）。"""
        raw = self._client.hgetall(self._key(session_id))
        if not raw:
            return None
        fields = {self._decode(k): self._decode(v) for k, v in raw.items()}
        raw_turn = fields.get("turn")
        raw_last_message_at = fields.get("last_message_at")
        if raw_turn is None or raw_last_message_at is None:
            return None
        return SessionSnapshot(
            turn=int(raw_turn),
            followup_count=int(fields.get("followup_count", "0")),
            handoff_status=fields.get("handoff_status", "none"),
            last_message_at=datetime.fromisoformat(raw_last_message_at),
        )

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{KEY_PREFIX}{session_id}"

    @staticmethod
    def _decode(value: str | bytes) -> str:
        return value.decode() if isinstance(value, bytes) else value
