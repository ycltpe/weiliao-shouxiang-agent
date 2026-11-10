# Epic 5 Context: 转人工升级与 AI 身份合规

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

实现硬触发条件的转人工全流程与 AI 身份合规保障。命中红线意图（价格谈判 / 贷款政策 / 投诉）、图片/语音输入、同一意图追问 ≥3 次、负面词汇命中任一条件时，会话立即转交经纪人工作台，同时向买家发送安抚通知，转人工消息送达工作台延迟 P99 < 5s。转人工是原子终态出口：此后同一会话的 Agent 响应路径彻底关闭，杜绝人机混答。全程保障买家知情权：Agent 响应 100% 携带"AI 助手"标识，转人工后无缝切换为经纪人身份。

## Stories

- Story 5.1: 硬触发条件判定
- Story 5.2: Kafka 转人工单向指令
- Story 5.3: 转人工原子终态与买家安抚通知
- Story 5.4: AI 身份披露标识
- Story 5.5: 转人工后会话响应路径关闭

## Requirements & Constraints

- 红线意图（NEGOTIATION / LOAN_POLICY / COMPLAINT）100% 转人工、零生成回答；命中硬触发后禁止任何过滤器继续走缓存或 LLM 路径。
- 硬触发条件全集：红线意图、图片/语音输入、同一意图追问 ≥3 次、负面词汇命中。条件集（追问阈值、负面词表、意图范围）必须配置化，默认按产品要求取值，支持灰度调整以避免追问误触发制造噪音转接。
- 转人工动作 = Kafka 转交指令（→ SpringBoot 经纪人工作台）+ 买家安抚通知（→ 下发适配器），严格先指令后通知；买家收到"已为您转接经纪人，请稍候"。
- 转人工消息送达经纪人工作台延迟 P99 < 5s；指令发送失败重试 3 次后写死信队列并触发人工告警，禁止静默降级。
- 转交指令 payload 携带会话上下文（含最近 3 轮对话摘要），经纪人接管时无需翻历史。
- 转人工分支为原子终态出口：禁止回流响应组装/下发路径，禁止使用模板兜底文案；转人工后同 session 后续消息零 AI 回复（无 LLM 生成、无模板兜底下发），该行为须有端到端测试覆盖并持续通过。
- handoff 状态权威值存 Redis（TTL 30 分钟）；会话过期后新消息状态从零初始化，走正常 AI 管道。
- Agent 响应 100% 携带"AI 助手"标识；转人工后标识消失并切换为经纪人姓名/头像，无残留；标识的展示与切换逻辑可通过 Langfuse trace 对应的会话记录验证。
- Agent 生成文本不得包含"我是这套房的经纪人"或等价表述（风控审核范围，属既有合规项）。
- 全链路可观测：硬触发命中条件、转人工事件均须记录 Langfuse trace。

## Technical Decisions

- 硬触发评估集中归属（AD-12）：全部硬触发条件由 intent_router 过滤器内的单一硬触发评估模块统一判定，判定结果（触发/未触发 + 命中条件）写入信封并记录 Langfuse trace；判定逻辑位于 intent_router 之后的独立判定点，供会话管理模块记录 handoff 状态。不允许四类条件分散在多个过滤器各自评估。
- 追问计数读取 Redis 权威值（AD-4），绝不读 L1 快照；handoff 状态由会话管理模块（单写者）在转人工分支写入，过滤器只读并传 session_id。
- 负面词汇词库由风控中台提供、服务内只读加载；词库 owner 待定，但不影响接口边界。
- 图片/语音输入由接入适配器在信封标注媒体类型（media_type: text/other），硬触发评估模块对非 text 直接判触发；意图分类与 LLM 链路不处理非文本输入。
- 转人工单向指令（AD-7）：Python 侧不重建双向 IM 连接，与 SpringBoot 工作台仅经 Kafka 解耦；转人工分支为原子终态出口，不回流主响应路径。
- 响应路径关闭（FR-12 强制 enforcement）：转人工后入口/编排器经 SessionManager 读取 handoff_status 短路同 session 后续消息，不重建信封、不走完整 AI 管道。L1 进程内 handoff 快照仅作优化层读取，不影响正确性；handoff 写入后 L1 快照立即作废，转人工判定不读过期快照。
- AI 身份披露（AD-10）落在响应组装过滤器（filters/response_assembler）。
- 信封为单一 Pydantic 模型，全链路 `model_validate` 校验；错误处理约定下所有过滤器异常降级至模板兜底，但转人工分支除外（AD-7）。
- 转人工自身流程（Kafka 指令 + 安抚通知链路）在 Story 5.5 改动中不触碰、行为保持不变。

## UX & Interaction Patterns

- 身份披露经经纪人工作台聊天界面承载，本服务不新增 UI；MVP 无独立 UX 设计文档。
- 买家侧关键体验：转接发生时收到明确安抚通知；转接后不再收到任何 AI 回复；AI 助手标识与经纪人身份的切换无残留、无中途态。

## Cross-Story Dependencies

- Story 5.1 依赖 Epic 2 的意图分类结果与追问计数（Redis 原子计数器）；Epic 6 落地后追问判定口径升级为基于消解后消息。
- Story 5.2 依赖 Epic 1 的 adapters/ Kafka producer 基础设施与信封模型。
- Story 5.3/5.5 依赖 Epic 1 的 session/ 会话管理模块写入 handoff 状态（AD-4 单写者）。
- Story 5.4 依赖响应组装与下发链路（Epic 1 骨架、Epic 4 应答链路）。
- Story 5.5 不得改变 Story 5.2/5.3 的指令与安抚发送链路行为。
