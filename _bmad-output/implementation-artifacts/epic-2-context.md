# Epic 2 Context: 意图识别与追问感知路由

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

让系统理解每条买家文本消息背后的意图，并感知买家是否在反复追问同一问题。本 Epic 产出 9 类意图分类能力（含置信度输出）和重复追问识别（含追问计数与升级信号），为后续路由决策——走缓存、走 LLM 还是转人工——提供权威输入。意图理解与追问感知是全链路的首个质量关口：分类准确率不达标，缓存命中和 LLM 生成都失去正确性基础；追问识别缺失，买家会收到复读式应答且无法在适当时机转接经纪人。

## Stories

- Story 2.1: 意图标签模型与分类器接口
- Story 2.2: 意图分类默认实现
- Story 2.3: 重复追问识别

## Requirements & Constraints

- 意图标签为 9 类固定枚举：GREETING / PRICE_INQUIRY / AVAILABILITY / LOCATION / PHOTO_REQUEST / NEGOTIATION / LOAN_POLICY / COMPLAINT / UNKNOWN。全链路以单一 Literal 类型承载，经 `model_validate` 校验，未知值 fail-closed 到模板兜底应答。
- 分类输出为单一标签 + 置信度分数。多意图消息不拆分处理，按置信度最高的标签路由（多意图扇出留待 V2）。
- 分类延迟 P99 < 50ms。注意此预算为含前置指代消解的端到端口径，意图分类自身只消费消解后的消息（resolved_message），不直接读取买家原文。
- 分类准确率 ≥90%，基于规模 ≥1000 条、覆盖全部 9 类意图的人工标注测试集验收。
- 分类结果必须写入会话状态（Redis），键结构 `weiliao:session:{session_id}:intent:{turn}`，写入行为需经 Langfuse trace 可验证。
- 同一 session 中针对同一意图的第 2 次及以后消息标记为 Follow-up Query，识别准确率 ≥95%，同义改写视为同一意图（归并职责在分类器，不得对消息文本做相似度比对）。
- 追问次数计数器存于 Redis，用原子操作维护，TTL 与会话一致（30 分钟）。
- 追问次数 ≥3 时，无论意图类型，必须产出强制的转人工信号（Hard Trigger）供路由层消费。

## Technical Decisions

- 分类器接口只接收消息文本与会话上下文，输出单一标签 + 置信度。具体实现（规则引擎 / gpt-4o-mini zero-shot / 微调小模型）选型延期至第 2 周据语料标注决定；实现必须通过配置切换，切换不得改动路由链路代码。Story 2.1 只固化接口边界，Story 2.2 交付默认实现。
- 意图分类、路由属热路径，手写纯 Python，不依赖 LangGraph Runtime；分类器即便选用 LLM 方案，也必须满足上述延迟预算。
- 过滤器间仅通过信封传递信息。信封中与本 Epic 相关的字段为 `intent`（分类结果写入处）、`session_id`、`turn`、`trace_id`、`resolved_message`（分类器的唯一输入）、`coref`（引用元数据，可空）。
- `turn` 为会话内绝对消息序号，由会话管理模块在消息入管道前赋值，分类结果写入会话状态时按此序号寻址。
- 会话状态单写者：意图与追问计数只能由会话管理模块写入，过滤器只读并传 session_id。追问计数的权威值只存 Redis，任何进程内缓存（L1）不得作为判定依据。
- 追问 ≥3 次的硬触发判定不归本 Epic 实现，而由 intent_router 过滤器内统一的硬触发评估模块集中判定（该模块在 Epic 5 语境下扩展）；本 Epic 负责产出计数权威值与触发信号，判定方读 Redis 权威值而非 L1 快照。

## Cross-Story Dependencies

- Story 2.1（接口与标签模型）是 Story 2.2 和 2.3 的前置；2.3 依赖 2.2 的分类结果已写入会话状态，才能识别同 session 同意图的重复追问。
- 依赖 Epic 1：信封模型与校验约定（Story 1.1）、会话状态单写者与 Redis 接入（Story 1.3）、Langfuse trace 脚手架（Story 1.5）。Story 1.2 的占位模板集是 fail-closed 路径的兜底承载。
- 本 Epic 的产出是下游 Epic 的输入：追问标记供 Epic 3 实现"Follow-up 禁走 L0 缓存、强制进入 LLM 链路"；追问 ≥3 信号供 Epic 5 的硬触发评估模块消费并触发转人工。
