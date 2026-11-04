# Epic 3 Context: 事实卡片缓存体系

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

建立两级缓存体系：L0 Redis 分布式缓存以「事实卡片」为存储单元承接房源事实应答，L1 进程内缓存承接会话级热点，使买家首次查询可秒回事实应答且整体缓存命中率 ≥80%；同时以 Kafka 房源事件作为事实卡片的唯一写入路径，保证缓存事实与房源主数据一致；并实现「重复追问禁走 L0、强制进入 LLM 链路」，确保买家重复追问时永远拿到新鲜生成而非复读缓存。本 Epic 直接决定首响延迟与缓存拦截率两项核心指标的达成。

## Stories

- Story 3.1: 事实卡片 Schema 与 L0 缓存客户端
- Story 3.2: Kafka 房源事件同步消费者
- Story 3.3: L1 进程内缓存与分层查找顺序
- Story 3.4: 重复追问禁走缓存与命中率指标

## Requirements & Constraints

- L0 缓存 key 结构：`weiliao:l0:{intent}:{house_id}:{status}:{price_bucket}`（段间以 `:` 分隔），TTL 5 分钟；Redis 值一律 JSON 序列化。
- L0 缓存整体命中率 ≥80%（压测验证）。命中率统计口径：L1 与 L0 命中均计入分子，基数为全部首次消息，重复追问不计入分母。
- 事实卡片为单一 Pydantic 模型，字段：house_id、status（active/sold/offline）、price_bucket（10 万分桶）、school（可选）、dialogue_summary（最近 3 轮）、fact_version（int，随 Kafka 事件单调递增）、as_of（timestamp）。
- 缓存失效语义为逻辑作废（标记删除）而非物理删除；房源状态变更时主动失效。
- Kafka `house.sync.trigger` 房源事件是事实卡片的唯一写入路径：服务不直写数据库，Redis 缓存由事件消费者更新/失效；消费延迟 ≤500ms，超期未消费的事件按失效处理并触发告警。
- Follow-up Query 不得命中 L0 缓存，必须进入 LLM 链路；同一 session 中同一意图的第 2 次消息 100% 进入 LLM 链路（以 Langfuse trace 可验证为准），且 LLM 响应与首次缓存响应的文本相似度须 < 0.8。
- 查找顺序严格为 L1 → L0 → LLM。
- L1 命中率优化不得破坏实例无状态约束：L1 仅为优化层，不影响正确性，任意实例可处理任意消息。
- handoff 状态快照与 Redis 权威状态的一致性须有明确失效语义：handoff 写入后 L1 快照立即作废，转人工判定不得读取过期快照。

## Technical Decisions

- 缓存分层职责（AD-5）：L1 = cachetools.TTLCache 进程内缓存，TTL ≤30s，仅存会话级热点（最近意图、追问计数快照、handoff 状态快照），严禁存事实卡片；L0 = Redis 分布式缓存（redis-py 8.1.0），专存事实卡片。追问计数的权威值只存 Redis，L1 快照仅作读优化，任何硬触发判定不得读 L1 快照。
- 事实卡片 Kafka 事件唯一写入路径（AD-6）：事实变更以 Kafka 事件为权威写入路径，消费者侧最大事件延迟预算 500ms，超期触发告警；禁止服务直写 DB 的双写模式。
- fact_version 作为乐观失效依据：卡片带单调递增版本号，配合 Kafka 消费延迟上限形成闭环，无需物理删除。
- 本 Epic 的代码归属：L0 客户端与 L1 封装在 `cache/` 目录，Kafka 房源事件消费者在 `workers/`（house_sync），事实卡片模型在 `models/`。
- 缓存查找是热路径过滤器，须手写纯 Python、不依赖 LangGraph Runtime；过滤器间仅通过信封传递，禁止跨过滤器直接函数调用。
- 过滤器异常一律降级至模板兜底；缓存命中、失效事件均记录 Langfuse trace（trace_id 贯穿全链路）。

## Cross-Story Dependencies

- 依赖 Epic 1：信封模型与过滤器链（Story 1.1/1.2）是会话状态与缓存查找的编排基础；会话状态单写者模块（Story 1.3）提供 session_id 与追问计数权威值；Langfuse 脚手架（Story 1.5）承载命中率与禁走缓存的可验证口径。
- 依赖 Epic 2：Follow-up Query 标记与追问计数来自 Story 2.3，是 Story 3.4「禁走 L0」逻辑的输入；缓存 key 中的 intent 字段来自意图分类（Story 2.1/2.2）。
- 指向 Epic 4：Follow-up Query 强制进入的 LLM 链路由 Epic 4 实现；本 Epic 只保证路由正确与可观测。
- 指向 Epic 5：handoff 状态快照的失效语义需与转人工分支（Epic 5）的写入时机对齐。
- Story 3.4 的命中率指标与禁走缓存验证依赖 Story 3.1-3.3 全部就位后方可压测验收。
