# Epic 6 Context: 买家消息指代消解

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

在热路径管道最前端（信封标准化之后、意图分类之前）落地指代消解过滤器（`filters/coreference`，手写纯 Python），将买家消息中的房源指代（它/这套/那套/上面说的 等）归一化为自包含消解后消息与引用元数据，使意图分类、重复追问判定与 LLM 生成全程无需回溯对话历史即可正确工作。无法确定先行词时保留原文降级，不阻塞、不转人工。本 Epic 锁定接口边界，具体实现选型（规则模板 / 序列标注 / LLM 改写）留待 OQ-5 第 2 周依据语料标注决定，接口须支持可插拔替换。

注：规划工件仅含 epics 文件与架构脊柱，无独立 PRD/UX 文档，本上下文据两者编译。

## Stories

- Story 6.1: 指代消解过滤器接口与管道接入
- Story 6.2: 房源指代归一化默认实现
- Story 6.3: 下游消费接入与追问判定 retrofit

## Requirements & Constraints

- 过滤器在信封标准化之后、intent_router 之前执行，为管道第一个热路径过滤器；手写纯 Python、不依赖 LangGraph Runtime，自身延迟 P99 < 10ms。
- 房源指代消解准确率 ≥ 95%，以人工标注测试集评估（指代类样本占比 ≥ 20%，与 FR-1 测试集同期建设）。
- FR-1 延迟口径验收以"含消解的端到端"计算：意图分类 P99 < 50ms。
- 无法确定先行词时保留原文进入意图分类，不阻塞、不转人工，由分类置信度与 UNKNOWN 既有路径承接；非房源实体指代（价格、楼层、位置等）MVP 尽力消解，失败按同一路径降级。
- 消解锚定会话单一房源：先行词由会话状态确定，做的是归一化而非开放式实体链接；跨会话/跨房源指代不做。
- 端到端验收须通过 PRD 小王场景（"它采光怎么样"）。

## Technical Decisions

- 信封为单一 Pydantic 模型，含 `session_id`、`house_id`、`turn`、`intent`、`timestamp`、`trace_id`、`message`（买家原文）、`resolved_message`（消解后消息）、`coref`（引用元数据，可空）等字段。过滤器仅消费上游信封、产出下游信封，禁止跨过滤器直接函数调用。
- 消解输出写入信封 `resolved_message` / `coref` 字段。`resolved_message` 的唯一允许内联标注格式为对齐 PRD 示例的"这套房（house_id: xxx）多少钱"式显式引用。
- `coref` 引用元数据为单一 Pydantic 模型 `CorefRef` 的列表（0..n 条），权威定义在 `models/`：字段 `entity_type`（枚举：house/price/floor/location/other）、`entity_id`（house 类型必填 house_id，其余可空）。信封模型即唯一真相源，下游禁止自定义解析。
- 信封 `coref` 字段是下游唯一消费路径；Redis `weiliao:session:{session_id}:coref:{turn}` 仅作审计留痕，禁止任何下游过滤器反读 Redis 重建 coref 做运行时决策。
- 会话状态单写者：coref 结果由指代消解过滤器产出、会话管理模块写入 Redis（JSON 序列化）；过滤器只读会话状态并传 session_id。
- 意图分类（FR-1）、LLM 生成（FR-6）只消费消解后消息，禁止直接读取买家原始文本做决策。
- 重复追问判定（FR-2）以意图分类输出的意图标签为准（同义改写归并是分类器的职责），辅以 coref 实体锚定比较，禁止直接对消解后消息做文本相似度比对；"它多少钱"与"这套房多少钱"计为同一意图。
- 实现选型为 OQ-5 开放项：规则模板归一化 / 轻量序列标注模型 / LLM 改写，第 2 周据语料标注决定；脊柱仅锁定接口边界，LLM 改写方案须另行论证 10ms 延迟预算可行性。

## UX & Interaction Patterns

- 买家视角的体验目标：追问始终围绕会话绑定的同一套房子被系统正确理解（"它多少钱" → "这套房（house_id: xxx）多少钱"）。
- `resolved_message` 是管道内部表示，禁止进入下发适配器与任何买家可见文案；响应组装仅消费买家原文 `message`。
- 消解失败（先行词不确定）对买家透明：走既有 UNKNOWN 降级路径，不触发转人工、不产生额外买家侧交互。

## Cross-Story Dependencies

- Story 6.2、6.3 依赖 6.1 的接口接入与信封扩展（`resolved_message`/`coref` 字段、`CorefRef` 模型）。
- Story 6.3 依赖 Epic 2 的追问判定（Story 2-3 已部分实现），需 retrofit 其接入点改为基于意图标签 + coref 锚定的口径。
- Story 6.3 与意图分类（Epic 2 的 2.x 系列）及 LLM 生成（Epic 4 的 4.3）存在消费契约：下游仅消费消解后消息，上游接口变更需同步协调。
