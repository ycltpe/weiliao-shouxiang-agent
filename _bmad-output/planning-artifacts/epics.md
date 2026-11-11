---
stepsCompleted: ["step-01-validate-prerequisites", "step-02-design-epics", "step-03-create-stories", "step-04-final-validation"]
inputDocuments:
  - "_bmad-output/planning/prds/prd-微聊首响Agent-2026-10-21/prd.md"
  - "_bmad-output/planning-artifacts/architecture/architecture-weiliao-shouxiang-agent-2026-10-22/ARCHITECTURE-SPINE.md"
  - "_bmad-output/implementation-artifacts/deferred-work.md"
---

# 微聊首响 Agent - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for 微聊首响 Agent, decomposing the requirements from the PRD, UX Design if it exists, and Architecture requirements into implementable stories.

## Requirements Inventory

### Functional Requirements

FR1: 意图分类 — 系统对每条买家文本消息输出一个意图标签（9 类：GREETING / PRICE_INQUIRY / AVAILABILITY / LOCATION / PHOTO_REQUEST / NEGOTIATION / LOAN_POLICY / COMPLAINT / UNKNOWN）和置信度分数；意图分类准确率 ≥90%（基于人工标注的测试集，规模 ≥1000 条，覆盖全部 9 类意图）；意图分类延迟 P99 < 50ms；意图分类结果写入会话状态（Redis），键：`weiliao:session:{session_id}:intent:{turn}`

FR2: 重复追问识别 — 系统识别同一 session 中针对同一意图的第 2 次及以后的消息，标记为 Follow-up Query；识别准确率 ≥95%（同义改写视为同一意图）；追问次数计数器存储于 Redis，TTL 与会话一致（30 分钟）；追问次数 ≥3 时，无论意图类型，强制触发转人工（Hard Trigger）

FR3: 硬触发转人工 — 命中任一硬触发条件（红线意图 NEGOTIATION / LOAN_POLICY / COMPLAINT、图片/语音输入、同一意图追问 ≥3 次、负面词汇命中）时立即将会话转交经纪人工作台，同时向买家发送安抚消息；红线意图 100% 转人工、零生成回答；转人工消息送达经纪人工作台延迟 P99 < 5s；买家收到通知："已为您转接经纪人，请稍候"

FR4: L0 事实卡片缓存 — 系统维护 L0 缓存，存储单元为事实卡片（非最终应答文案）；缓存 key 结构：`weiliao:l0:{intent}:{house_id}:{status}:{price_bucket}`；事实卡片包含：房源状态（active/sold/offline）、当前挂牌价（分桶到 10 万）、对口学校（如有）、最近 3 轮对话摘要；缓存 TTL 5 分钟，房源状态变更（Kafka `house.sync.trigger` 事件）时主动失效；L0 缓存整体命中率 ≥80%（压测验证）

FR5: 重复追问禁走缓存 — Follow-up Query 不得命中 L0 缓存，必须进入 LLM 链路；同一 session 中同一意图的第 2 次消息 100% 进入 LLM 链路（Langfuse trace 可验证）；LLM 响应不得与首次缓存响应完全相同（文本相似度 < 0.8）

FR6: LLM 受控生成 — 系统调用 OpenAI（gpt-4o 或 gpt-4o-mini）生成买家响应，prompt 包含事实卡片、对话上下文和生成约束；生成内容必须基于事实卡片中的房源数据，不得虚构价格、面积、学区信息；prompt 明示约束："不得承诺价格可谈、不得承诺学区名额、不得使用'最''第一'等违禁词"；生成延迟：首 token 到达 P99 < 1.5s

FR7: 双审核机制 — 系统对 LLM 生成内容执行后置审核，审核不通过时切换至模板兜底应答；100% 的 LLM 生成内容须通过风控中台审核后才可发送；审核不通过时买家收到模板兜底版本，服务无中断；审核通过率 ≥95%（上线前压测基线，作为全量放行门槛）

FR8: OpenAI 故障降级 — OpenAI 返回 429 / 5xx / 超时（>3s，架构收紧为 >1.8s）时自动切换至模板兜底；不可用期间服务可用性不降级（不返回 5xx 给买家）；降级切换时间 < 100ms；降级事件写入 Langfuse trace 并触发监控告警；恢复后按 10% 步进逐步切回

FR9: AI 身份披露 — 聊天界面在 Agent 响应气泡旁持续展示"AI 助手"标识，转人工后标识消失；Agent 响应 100% 携带 AI 标识；转人工后标识切换为经纪人姓名/头像，无残留；Agent 生成文本不得包含"我是这套房的经纪人"或等价表述（风控审核范围）

FR10: 会话状态一致性 — 同一 session 的连续消息，无论由哪个服务实例处理，均可读取一致的会话状态；会话状态存储于 Redis，TTL 30 分钟；多实例并发读取同一 session 状态不产生竞态条件（Redis 原子操作或分布式锁）；会话过期后重新发起对话，状态从零初始化，无残留

FR11: 指代消解（Coreference Resolution）— 系统对每条买家文本消息，在意图分类之前执行指代消解，输出消解后消息与引用元数据（引用实体类型、实体 ID）。房源指代（它/这套/那套/上面说的 等）消解准确率 ≥ 95%（人工标注测试集，含指代类样本 ≥ 20%，与 FR-1 测试集同期建设）；消解输出为自包含文本（"它多少钱" → "这套房（house_id: xxx）多少钱"），意图分类（FR-1）、重复追问判定（FR-2）与 LLM 生成（FR-6）只消费消解后消息，不直接读取买家原始文本；消解延迟 P99 < 10ms，FR-1 意图分类延迟预算（P99 < 50ms）口径为含消解的端到端；消解结果写入会话状态（Redis），键：`weiliao:session:{session_id}:coref:{turn}`；无法确定先行词时保留原文进入意图分类，不阻塞、不转人工；非房源实体指代（价格、楼层、位置等）MVP 尽力消解，失败按同一路径降级；先行词锚定会话单一房源（跨会话/跨房源不做，PRD §5）；实现选型（规则模板归一化 / 轻量序列标注模型 / LLM 改写）与 OQ-1 一同第 2 周据语料标注决定（PRD OQ-5），脊柱仅定接口边界（AD-11）

FR12: 转人工后会话响应路径关闭 — 转人工（handoff 状态，权威值存 Redis，经 SessionManager 读取）后，入口/编排器消费 handoff_status 短路同 session 后续消息，不再重建信封走完整 AI 管道，实现"转人工后 Agent 响应路径关闭、避免人机混答"（AD-7 原子终态的强制 enforcement）；需补"转人工后零 AI 回复"端到端测试

FR13: 空串/仅空白草稿处理 — 空串或仅空白草稿按"无草稿"处理，不产出只有前缀无正文的买家应答、不产生前缀双标；口径（2026-11-10 拍板）：由 risk_review/响应组装入口拦截，空草稿不进入携标组装，走模板兜底应答路径；拦截发生在草稿过审之前，不触碰冻结约束"过审草稿除加前缀外不得再被改写"；risk_review 对空草稿的拦截口径与本条一致

### NonFunctional Requirements

NFR1: 首响端到端延迟 P99 < 3s — 从买家消息到达系统到买家收到响应（SM-1）

NFR2: 服务可用性 99.95% — Agent 服务自身可用性，不含 OpenAI 侧故障（OpenAI 故障由模板兜底承接）（SM-3）

NFR3: 转人工延迟 P99 < 5s — 触发转人工到消息送达经纪人工作台（SM-7）

NFR4: LLM 链路延迟预算 — OpenAI 生成端到端预算 0.9s（编排器 NFR-1 出口余量决策；单次 attempt ≤0.45s、至多 1 次重试，code review Epic 4 修订口径）；断路器对 429/5xx/超时跳闸；降级切换 <100ms；风控审核超时（默认 500ms，可调 300-800ms）视为不通过并切模板兜底（AD-8）

NFR5: Kafka 房源事件消费延迟 ≤500ms — 超期未消费事件按失效处理并触发告警（AD-6）

NFR6: 全链路可观测 — Langfuse trace_id 贯穿全链路；降级事件、审核结果、缓存命中均记录 trace；成本追踪记录 token 消耗与模型调用费用；SM-5/SM-6 验收口径以 Langfuse trace 数据为准

NFR7: 健康检查端点 — `GET /health` 挂在响应组装过滤器，返回 200 表示服务就绪

NFR8: 实例无状态 — 逻辑架构保持实例无状态、任意实例可处理任意消息；L1 仅为优化层不影响正确性

NFR9: 运维文档与配置交付 — 全部新增 env 变量（含 4.4/4.5/4.6 的 WEILIAO_LLM_BREAKER_* 系列）在 runbook/README 有文档说明；`/health` 的 model_routing 快照与 ERROR 级 `model_routing_alert` 告警有 runbook 可查；`.env.example` 不留空值变量（blank=用缺省值或示例填具体值），整体拷贝为 .env 即可启动

NFR10: 超时风暴压测与执行器生命周期重审 — 编排器逐跳超时丢弃的执行线程无取消/复用机制（Story 1.2/4.3 挂账同根）；在 Epic 4 真实过滤器（带副作用）落地后执行超时风暴压测，重审执行器生命周期（共享池/cancel/信封所有权），并评估为被丢弃的生成调用记 token 归因

### Additional Requirements

- 技术栈锁定（架构 Stack 表）：Python ≥3.11、FastAPI ≥0.115.0、Uvicorn ≥0.32.0、Pydantic ≥2.9.0、LangGraph 1.2.16（仅 LLM 子图）、LangChain-OpenAI ≥1.6.2、redis-py 8.1.0、Langfuse SDK 5.0.4、cachetools 7.2.4、OpenAI gpt-4o / gpt-4o-mini
- 项目骨架（Structural Seed）作为 Epic 1 Story 1 基座：`adapters/`（Kafka consumer/producer、外部服务适配）、`filters/`（intent_router, cache_lookup, risk_review, response_assembler）、`llm/`（LangGraph 子图、OpenAI client、prompt 管理）、`session/`（会话状态管理，单写者，含 handoff 状态）、`cache/`（L0 Redis 客户端、L1 cachetools 封装）、`models/`（信封、事实卡片、意图标签）、`workers/`（Kafka 消费者：房源事件同步、转人工指令）、`observability/`（Langfuse trace、监控指标、成本追踪）、`health/`（健康检查端点）
- 管道-过滤器架构（AD-1）：过滤器间仅通过信封传递，禁止跨过滤器直接函数调用；信封为单一 Pydantic 模型，含 session_id、house_id、turn、intent、timestamp、trace_id 六字段
- 热路径手写纯 Python（AD-2）：意图分类、路由、缓存查找、风控审核、响应组装、下发不依赖 LangGraph Runtime；LLM 生成过滤器内部允许 LangGraph 子图，对外仅暴露同步生成接口
- 单意图路由（AD-3）：意图分类输出单一标签 + 置信度；多意图场景拆分为多次独立消息处理
- 会话状态单写者（AD-4）：会话状态仅由会话管理模块写入，过滤器只读并传 session_id；追问计数用 Redis 原子操作；handoff 状态由会话管理模块在转人工分支写入
- L1/L0 缓存分层（AD-5）：L1 = cachetools.TTLCache 进程内缓存，仅存会话级热点（最近意图、追问计数、handoff 状态快照），不存事实卡片，TTL ≤30s；L0 = Redis 分布式缓存存事实卡片，TTL 5 分钟；查找顺序 L1 → L0 → LLM；Follow-up Query 禁走 L0（FR-5）；缓存拦截率口径：L1 与 L0 命中均计入分子，基数为全部首次消息（重复追问不计入分母）；失效语义为逻辑作废非物理删除
- 事实卡片 Kafka 事件唯一写入路径（AD-6）：服务不直写 DB；Redis 缓存由事件消费者更新/失效
- 转人工 Kafka 单向指令（AD-7）：转人工 = Kafka 转交指令（→ SpringBoot 工作台）+ 买家安抚通知（→ 下发适配器），先指令后通知；转人工分支为原子终态出口，禁止回流响应路径、禁止使用模板兜底文案；指令发送失败重试 3 次后写死信队列并触发人工告警
- 数据格式约定：意图标签为单一 `Literal` 类型枚举（9 类），全链路 `model_validate` 校验，未知值 fail-closed 到模板兜底；`turn` 为会话内绝对消息序号，由会话管理模块在消息入管道前赋值；Redis 值用 JSON 序列化
- 事实卡片 schema：单一 Pydantic 模型：`house_id`、`status`（active/sold/offline）、`price_bucket`（10 万分桶）、`school`（可选）、`dialogue_summary`（最近 3 轮）、`fact_version`（int，Kafka 事件单调递增）、`as_of`（timestamp）
- 错误处理约定：所有过滤器捕获异常后降级至模板兜底（转人工分支除外，见 AD-7）；禁止将未审核内容发买家；错误响应携带 trace_id
- 意图分类器具体选型延期至第 2 周（PRD OQ-1，据语料标注决定）：规则引擎 / gpt-4o-mini zero-shot / 微调小模型；脊柱仅定接口边界（AD-3 单意图路由）
- 部署拓扑延期（K8s vs 公司 PaaS），逻辑架构须保持实例无状态
- 既有 my-agent 脚手架（anthropic、a2a-sdk、postgres checkpoint、sse-starlette）为无关原型依赖，不复用、不继承
- 指代消解过滤器（AD-11）：`filters/coreference` 为管道第一个热路径过滤器（信封标准化之后、意图分类之前），手写纯 Python，毫秒级可控；coref 结果由指代消解过滤器产出、经会话管理模块写入会话状态（状态变更矩阵既定分工）；无法确定先行词时保留原文、不阻塞不转人工

### UX Design Requirements

无 — MVP 无独立 UX 设计文档。AI 身份披露标识（FR-9）经经纪人工作台聊天界面承载，不在本服务范围内新增 UI 工作。

### FR Coverage Map

FR1: Epic 2 - 意图分类（9 类标签 + 置信度 + 会话状态写入）
FR2: Epic 2 - 重复追问识别（Redis 计数器 + ≥3 次硬触发）
FR3: Epic 5 - 硬触发转人工（Kafka 指令 + 买家安抚通知）
FR4: Epic 3 - L0 事实卡片缓存（key 结构、TTL、Kafka 失效、命中率 ≥80%）
FR5: Epic 3 - 重复追问禁走 L0，强制进入 LLM 链路
FR6: Epic 4 - LLM 受控生成（事实卡片 grounding + 违禁词约束）
FR7: Epic 4 - 双审核机制（风控审核 + 模板兜底）
FR8: Epic 4 - OpenAI 故障降级（切换 <100ms、步进恢复、可观测）
FR9: Epic 5 - AI 身份披露（标识展示、转人工切换、话术合规）
FR10: Epic 1 - 会话状态一致性（Redis、单写者、原子操作、TTL 30min）
FR11: Epic 6 - 指代消解（管道首位过滤器、准确率 ≥95%、P99 < 10ms、coref 会话状态写入、降级不阻塞）
FR12: Epic 5 - 转人工后响应路径关闭（handoff_status 短路 + 零 AI 回复端到端测试）
FR13: Epic 4 - 空草稿拦截（risk_review/组装入口拦截，模板兜底承接）

NFR 映射：NFR1（端到端延迟）贯穿 Epic 2-4 验收；NFR2（可用性）主落在 Epic 4 降级能力；NFR3（转人工延迟）落在 Epic 5；NFR4（LLM 链路延迟预算）落在 Epic 4；NFR5（Kafka 消费延迟）落在 Epic 3；NFR6（全链路可观测）落在 Epic 1 并在各 Epic 验收；NFR7（健康检查）、NFR8（实例无状态）落在 Epic 1；NFR9（运维文档与配置交付）落在 Epic 7；NFR10（超时风暴压测与执行器重审）落在 Epic 7。

## Epic List

### Epic 1: 消息管道基座与会话状态
建立管道-过滤器骨架（信封模型、过滤器编排、错误兜底约定），实现会话状态单写者管理（Redis、原子操作、TTL 30 分钟）、健康检查端点与 Langfuse 全链路追踪脚手架。完成后，任意买家消息可被服务可靠接收、编排并读取一致的会话上下文。
**FRs covered:** FR10（另承载 NFR7、NFR8、NFR6 基座及架构 Structural Seed 要求）

### Epic 2: 意图识别与追问感知路由
实现 9 类意图分类器（规则引擎 / gpt-4o-mini zero-shot / 微调小模型，选型第 2 周确定，脊柱仅定接口边界）、置信度输出与会话状态写入；实现 Follow-up Query 识别（同 session 同意图第 2 次起，Redis 原子计数器）与追问 ≥3 次硬触发信号。完成后，系统理解每条买家消息的意图并感知重复追问。
**FRs covered:** FR1, FR2

### Epic 3: 事实卡片缓存体系
实现 L0 Redis 事实卡片缓存（key 结构、5 分钟 TTL、命中率 ≥80%）与 L1 进程内热点缓存（cachetools，TTL ≤30s，仅会话级热点）；实现 Kafka 房源事件消费者作为事实卡片唯一写入路径（消费延迟 ≤500ms、状态变更主动失效）与 Follow-up Query 禁走 L0 强制进入 LLM 链路。完成后，首次查询可秒回事实应答，重复追问永远拿到新鲜生成。
**FRs covered:** FR4, FR5（另承载 NFR5）

### Epic 4: LLM 受控应答与风控兜底
实现 LangGraph LLM 子图与 OpenAI 客户端（受控生成、事实卡片 grounding、违禁词约束、首 token P99 < 1.5s）；实现后置风控审核（100% 覆盖、不通过切模板兜底、通过率 ≥95% 基线）与 OpenAI 故障降级（429/5xx/超时 >1.8s 自动切换 <100ms、步进恢复、降级可观测）。完成后，买家获得自然、合规、事实准确的 AI 应答，任何故障不中断服务。
**FRs covered:** FR6, FR7, FR8, FR13（另承载 NFR4；新增 Story 4.7 空草稿拦截）

### Epic 5: 转人工升级与 AI 身份合规
实现硬触发转人工全流程（红线意图 / 图片语音 / 追问 ≥3 次 / 负面词汇 → Kafka 转交指令先送、买家安抚通知后达，P99 < 5s，失败重试 3 次 + 死信队列）；实现 AI 身份披露标识（Agent 响应 100% 携带、转人工后切换为经纪人身份、生成话术不得冒充经纪人）。完成后，红线会话即时触达经纪人工作台，买家全程知情。
**FRs covered:** FR3, FR9, FR12（另承载 NFR3；新增 Story 5.5 转人工后响应路径关闭）

### Epic 6: 买家消息指代消解
实现管道首位指代消解过滤器（`filters/coreference`，信封标准化之后、意图分类之前，手写纯 Python）：将买家消息中的房源指代（它/这套/那套/上面说的 等）归一化为自包含消解后消息与引用元数据，消解结果写入会话状态（`weiliao:session:{session_id}:coref:{turn}`）；无法确定先行词时保留原文降级，不阻塞、不转人工。完成后，意图分类、重复追问判定与 LLM 生成均无需回溯对话历史即可正确工作。实施注记：FR-2 追问判定口径为"基于消解后消息"，Story 2-3 已实现部分需配套 retrofit 接入点改造；实现选型（规则模板 / 序列标注 / LLM 改写）随 OQ-5 第 2 周定，脊柱仅定接口边界。
**FRs covered:** FR11

### Epic 7: 可运维性与过载韧性
补齐运维可交付物：全部新增 env 变量（含 WEILIAO_LLM_BREAKER_* 系列）、`/health` 的 model_routing 快照与 ERROR 级 `model_routing_alert` 告警统一写入 runbook/README，`.env.example` 空值治理（blank=缺省值或示例值），整体拷贝即可启动；执行超时风暴压测并重审编排器执行器生命周期（共享池/cancel/信封所有权），评估被丢弃生成调用的 token 归因记账。完成后，运维不读源码即可排障，超时风暴下服务不劣化、无资源膨胀。
**FRs covered:** NFR9, NFR10

## Epic 1: 消息管道基座与会话状态

建立管道-过滤器骨架、信封模型与错误兜底约定，实现会话状态单写者管理、健康检查与全链路可观测性脚手架，使任意买家消息可被服务可靠接收、编排并读取一致的会话上下文。

### Story 1.1: 项目骨架与信封模型

As a 开发工程师,
I want 依据 Structural Seed 建立项目骨架并定义单一信封模型,
So that 后续所有过滤器与模块在统一结构上开发，信封成为过滤器间唯一通信契约。

**Acceptance Criteria:**

**Given** 架构 Structural Seed 定义（adapters/、filters/、llm/、session/、cache/、models/、workers/、observability/、health/）
**When** 初始化代码库与依赖（Python ≥3.11、FastAPI ≥0.115.0、Pydantic ≥2.9.0 等，按架构 Stack 表锁定）
**Then** 全部目录与最小可运行骨架就位，`pyproject.toml` 版本与 Stack 表一致，既有 my-agent 脚手架依赖不复用、不继承
**And** 信封为单一 Pydantic 模型，含 session_id、house_id、turn、intent、timestamp、trace_id 六字段，全链路 `model_validate` 校验，未知值 fail-closed

### Story 1.2: 管道过滤器编排与统一错误兜底

As a 开发工程师,
I want 实现过滤器链编排（intent_router → cache_lookup → risk_review → response_assembler）并统一异常兜底,
So that 任一过滤器异常时买家仍收到安全应答，服务不返回 5xx。

**Acceptance Criteria:**

**Given** 信封模型与过滤器目录结构已就位
**When** 编排过滤器链并规定过滤器间仅通过信封传递、禁止跨过滤器直接函数调用（AD-1）
**Then** 热路径（意图分类、路由、缓存查找、风控审核、响应组装、下发）为手写纯 Python，不依赖 LangGraph Runtime（AD-2）
**And** 所有过滤器捕获异常后降级至模板兜底（转人工分支除外），未审核内容禁止发买家，错误响应携带 trace_id
**And** 内部管道异常/超时触发兜底时，买家收到兜底应答的时限受端到端延迟预算约束（NFR-1，P99 < 3s），不得傻等全量超时后才返回
**And** 本 Story 附带最小占位模板集（每意图一条安全文案）使错误兜底机制可独立验收；占位模板在 Story 4.1 被完整模板库替换，保证 Epic 1 不依赖 Epic 4 即可交付

### Story 1.3: 会话状态单写者管理

As a 买家,
I want 我的会话状态（含追问计数与转人工状态）在连续消息间保持一致,
So that 无论哪个服务实例处理我的消息，对话体验都连续、准确。

**Acceptance Criteria:**

**Given** Redis 与会话管理模块可用
**When** 会话管理模块作为唯一写入者维护会话状态（AD-4），过滤器只读并传 session_id
**Then** `turn` 为会话内绝对消息序号，由会话管理模块在消息入管道前赋值
**And** 会话状态 TTL 30 分钟，追问计数用 Redis 原子操作，多实例并发读取同一 session 无竞态
**And** 会话过期后重新发起对话从零初始化，无残留状态
**And** 会话 TTL 过期瞬间恰有消息在管道中处理的竞态有明确语义：过期判定以消息入管道时的会话读取结果为准，处理中途过期不中断在途消息（handoff 终态除外）

### Story 1.4: 健康检查端点

As a 平台运维,
I want 通过健康检查端点感知服务就绪状态,
So that 负载均衡与发布流程可安全摘挂实例。

**Acceptance Criteria:**

**Given** 服务已启动且管道编排完成
**When** 对 `GET /health` 发起请求（端点挂在响应组装过滤器，NFR-7）
**Then** 服务就绪时返回 200，未就绪时返回非 200 状态

### Story 1.5: Langfuse 全链路追踪脚手架

As a 平台运营,
I want 每个请求携带贯穿全链路的 trace_id 并记录成本,
So that 端到端延迟、降级事件与模型调用费用可观测、可验收（SM-5/SM-6）。

**Acceptance Criteria:**

**Given** Langfuse SDK 5.0.4 已集成
**When** 任意消息进入管道
**Then** trace_id 生成并写入信封，贯穿全部过滤器与下游调用，响应携带 trace_id
**And** 成本追踪记录 token 消耗与模型调用费用，SM-5/SM-6 验收口径以 Langfuse trace 数据为准（NFR-6）

## Epic 2: 意图识别与追问感知路由

实现 9 类意图分类与置信度输出，识别重复追问并维护追问计数，为路由决策（缓存 vs LLM vs 转人工）提供输入。

### Story 2.1: 意图标签模型与分类器接口

As a 开发工程师,
I want 定义 9 类意图的单一 Literal 枚举与分类器接口边界,
So that 分类器具体实现可替换（规则引擎 / gpt-4o-mini zero-shot / 微调小模型）而不影响路由链路。

**Acceptance Criteria:**

**Given** 信封模型已定义 intent 字段
**When** 定义意图标签为单一 `Literal` 类型枚举：GREETING / PRICE_INQUIRY / AVAILABILITY / LOCATION / PHOTO_REQUEST / NEGOTIATION / LOAN_POLICY / COMPLAINT / UNKNOWN
**Then** 分类器接口仅接收消息文本与会话上下文，输出单一标签 + 置信度（AD-3，多意图场景拆分为多次独立消息处理）
**And** 全链路 `model_validate` 校验，未知值 fail-closed 到模板兜底
**And** 分类器实现通过配置选择（规则引擎 / gpt-4o-mini zero-shot / 微调小模型），切换无需改动路由链路代码；默认实现与切换方式在第 2 周选型时确定（PRD OQ-1）

### Story 2.2: 意图分类默认实现

As a 买家,
I want 系统准确理解我消息背后的意图,
So that 我得到与问题匹配的应答，而非答非所问。

**Acceptance Criteria:**

**Given** 分类器接口已定义，默认实现已选定（规则引擎 / gpt-4o-mini zero-shot / 微调小模型，第 2 周据语料标注决定，PRD OQ-1）
**When** 处理一条买家文本消息
**Then** 输出单一意图标签与置信度分数，分类延迟 P99 < 50ms
**And** 意图分类准确率 ≥90%（基于人工标注测试集，规模 ≥1000 条，覆盖全部 9 类意图）
**And** 结果写入会话状态（Redis），键：`weiliao:session:{session_id}:intent:{turn}`，写入经 Langfuse trace 可验证

### Story 2.3: 重复追问识别

As a 买家,
I want 系统感知我在反复追问同一个问题,
So that 后续应答会重新生成而非机械重复，并在必要时为我转接经纪人。

**Acceptance Criteria:**

**Given** 意图分类结果已写入会话状态
**When** 同一 session 中针对同一意图的第 2 次及以后消息到达
**Then** 标记为 Follow-up Query，识别准确率 ≥95%（同义改写视为同一意图）
**And** 追问次数计数器存储于 Redis（原子操作），TTL 与会话一致（30 分钟）
**And** 追问次数 ≥3 时，无论意图类型，强制触发转人工信号（Hard Trigger）供路由层消费

## Epic 3: 事实卡片缓存体系

实现 L0 Redis 事实卡片缓存与 L1 进程内热点缓存，以 Kafka 房源事件为事实卡片唯一写入路径，保证首次查询秒回、重复追问永远拿到新鲜生成。

### Story 3.1: 事实卡片 Schema 与 L0 缓存客户端

As a 开发工程师,
I want 定义事实卡片 Pydantic 模型并实现 L0 Redis 缓存客户端,
So that 房源事实以结构化、可失效的单元被缓存和读取。

**Acceptance Criteria:**

**Given** Redis 已接入（redis-py 8.1.0）
**When** 定义事实卡片 schema（单一 Pydantic 模型）：house_id、status（active/sold/offline）、price_bucket（10 万分桶）、school（可选）、dialogue_summary（最近 3 轮）、fact_version（int，单调递增）、as_of（timestamp），Redis 值 JSON 序列化
**Then** 缓存 key 结构为 `weiliao:l0:{intent}:{house_id}:{status}:{price_bucket}`，TTL 5 分钟
**And** 失效语义为逻辑作废（非物理删除），房源状态变更时主动失效

### Story 3.2: Kafka 房源事件同步消费者

As a 平台运营,
I want 房源数据变更通过 Kafka 事件驱动缓存更新与失效,
So that 缓存事实始终与房源主数据一致，且服务不直写数据库（AD-6）。

**Acceptance Criteria:**

**Given** Kafka 消费者基础设施就位（workers/ 目录）
**When** 消费 `house.sync.trigger` 房源事件
**Then** 事实卡片由事件消费者更新/失效（唯一写入路径），消费延迟 ≤500ms（NFR-5）
**And** 超期未消费事件按失效处理并触发告警

### Story 3.3: L1 进程内缓存与分层查找顺序

As a 开发工程师,
I want 用 L1 进程内缓存承接会话级热点并按 L1 → L0 → LLM 顺序查找,
So that 热点访问不穿透到 Redis，同时事实卡片不被进程内缓存污染。

**Acceptance Criteria:**

**Given** L0 缓存客户端已实现
**When** 实现 L1 = cachetools.TTLCache 进程内缓存（TTL ≤30s），仅存会话级热点：最近意图、追问计数、handoff 状态快照
**Then** L1 不存事实卡片，查找顺序严格为 L1 → L0 → LLM
**And** L1 仅为优化层不影响正确性，实例无状态约束保持（NFR-8）
**And** handoff 状态快照与 Redis 权威状态的一致性有明确失效语义：handoff 写入后 L1 快照立即作废，转人工判定不读取过期快照

### Story 3.4: 重复追问禁走缓存与命中率指标

As a 买家,
I want 我重复追问时系统重新生成应答而非复读缓存,
So that 我不会收到与上一轮雷同的回复。

**Acceptance Criteria:**

**Given** L1/L0 缓存与 Follow-up 标记均可用
**When** Follow-up Query 到达
**Then** 不得命中 L0 缓存，必须进入 LLM 链路；同一 session 同一意图第 2 次消息 100% 进入 LLM 链路（Langfuse trace 可验证）
**And** LLM 响应不得与首次缓存响应完全相同（文本相似度 < 0.8）
**And** L0 缓存整体命中率 ≥80%（压测验证）；缓存拦截率口径：L1 与 L0 命中均计入分子，基数为全部首次消息（重复追问不计入分母）

## Epic 4: LLM 受控应答与风控兜底

实现 LangGraph LLM 子图与 OpenAI 受控生成、后置风控双审核、OpenAI 故障降级与模板兜底，保证买家获得自然、合规、事实准确的 AI 应答，任何故障不中断服务。

### Story 4.1: 模板兜底应答库

As a 买家,
I want 当 AI 生成不可用时收到得体、安全的模板应答,
So that 我的咨询体验不因系统故障而中断。

**Acceptance Criteria:**

**Given** 9 类意图标签已定义
**When** 为每类意图编写模板兜底文案（含 Unknown 场景），文案经业务/合规确认，不含违禁表述
**Then** 模板库对过滤器链暴露同步获取接口，供错误兜底（Story 1.2）、审核不通过（Story 4.4）与 OpenAI 故障（Story 4.5）统一复用
**And** 转人工分支禁止使用模板兜底文案（AD-7）
**And** PHOTO_REQUEST 的应答方式有明确定义：模板库提供图片素材应答（房源图片链接/相册引导）与无图时的替代话术，避免买家发照片请求后收到纯文本敷衍

### Story 4.2: LLM 子图与 OpenAI 客户端

As a 开发工程师,
I want 在 LLM 过滤器内部实现 LangGraph 子图并对外暴露同步生成接口,
So that 热路径不依赖 LangGraph Runtime，而生成质量与编排能力不受损。

**Acceptance Criteria:**

**Given** 管道过滤器骨架已就位
**When** 实现 LangGraph 1.2.16 子图与 OpenAI 客户端（LangChain-OpenAI ≥1.6.2，模型 gpt-4o / gpt-4o-mini），对外仅暴露同步生成接口（AD-2）
**Then** 生成延迟：首 token 到达 P99 < 1.5s；OpenAI 端到端 ≤1.8s（含 1 次重试，NFR-4）
**And** prompt 模板集中管理（llm/prompt 管理），包含事实卡片、对话上下文与生成约束三段结构

### Story 4.3: 事实卡片 Grounding 受控生成

As a 平台运营,
I want LLM 应答严格基于事实卡片中的房源数据,
So that 买家不会收到虚构的价格、面积或学区信息，合规风险可控。

**Acceptance Criteria:**

**Given** LLM 子图与事实卡片 schema 已实现
**When** 生成买家响应
**Then** 生成内容必须基于事实卡片中的房源数据，不得虚构价格、面积、学区信息
**And** prompt 明示约束："不得承诺价格可谈、不得承诺学区名额、不得使用'最''第一'等违禁词"，生成文本不得包含"我是这套房的经纪人"或等价表述

### Story 4.4: 后置风控双审核

As a 买家,
I want 每条 AI 应答在发出前经过风控审核,
So that 我收到的内容合规、得体，审核异常时服务无中断地切换为模板应答。

**Acceptance Criteria:**

**Given** 模板兜底库与 LLM 生成链路可用
**When** 对 LLM 生成内容执行后置审核（risk_review 过滤器）
**Then** 100% 的 LLM 生成内容须通过风控中台审核后才可发送
**And** 审核不通过（含超时，默认 500ms、可调 300-800ms，超时视为不通过）时买家收到模板兜底版本，服务无中断
**And** 审核通过率 ≥95%（上线前压测基线，作为全量放行门槛），审核结果记录 Langfuse trace
**And** 审核通过率的持续监控有阈值告警：线上通过率跌破阈值时触发告警，避免只在压测时验证一次

### Story 4.5: OpenAI 故障降级与步进恢复

As a 平台运营,
I want OpenAI 故障时系统自动切换模板兜底并按步进恢复,
So that 服务可用性不因上游故障降级（不返回 5xx 给买家），且恢复过程平稳。

**Acceptance Criteria:**

**Given** LLM 链路与模板兜底均已实现
**When** OpenAI 返回 429 / 5xx / 超时（>1.8s，架构收紧值）
**Then** 自动切换至模板兜底，降级切换时间 < 100ms，不可用期间服务可用性不降级（NFR-2）
**And** 降级事件写入 Langfuse trace 并触发监控告警
**And** 恢复后按 10% 步进逐步切回

### Story 4.6: 模型路由与成本护栏

As a 平台运营,
I want 按流量特征路由 gpt-4o / gpt-4o-mini 并设定 token 预算,
So that 应答质量与调用成本平衡，成本异常增长时有控制手段而不只靠事后看报表。

**Acceptance Criteria:**

**Given** OpenAI 客户端已实现（Story 4.2），成本追踪已记录 token 消耗与模型调用费用（Story 1.5）
**When** 定义模型路由策略（如：按意图复杂度选择模型，简单意图走 gpt-4o-mini；置信度分流经 code review Epic 4 收窄至意图维度）与 token 预算阈值
**Then** 路由策略配置化，可灰度调整；模型选择记录 Langfuse trace，可复盘单条消息的模型与费用
**And** 预算超限时自动收紧路由（提高 mini 模型占比）并触发告警，而非仅记录

### Story 4.7: 空草稿拦截与兜底承接

As a 买家,
I want 草稿为空或仅空白时收到完整的模板兜底应答,
So that 我不会收到只有"AI 助手："前缀没有正文的残缺消息，也不会看到双标前缀。

**Acceptance Criteria:**

**Given** 风控审核通过且 draft 为空串（""）
**When** 响应组装过滤器处理该结果
**Then** 不执行携标组装，走模板兜底应答路径，买家收到完整模板应答
**And** Langfuse trace 记录 draft_empty 拦截事件（trace_id 符合 32-hex 可检索）

**Given** draft 为仅空白字符串（空格/换行/制表符组合）
**When** 响应组装过滤器处理该结果
**Then** strip 后判空，与空串同等按"无草稿"处理

**Given** draft 非空
**When** 响应组装
**Then** 行为与现状逐字节一致（加前缀携标，不再做任何清洗改写，不触碰"过审草稿除加前缀外不得再被改写"冻结约束）

**Given** 空草稿到达 risk_review
**When** 拦截判定
**Then** 与响应组装入口口径一致（统一"按无草稿处理"）

## Epic 5: 转人工升级与 AI 身份合规

实现硬触发条件的转人工全流程（Kafka 单向指令 + 买家安抚通知）、转人工原子终态与 AI 身份披露，保证红线会话即时触达经纪人工作台且买家全程知情。

### Story 5.1: 硬触发条件判定

As a 平台运营,
I want 红线意图与高风险信号 100% 触发转人工,
So that 价格谈判、贷款政策、投诉等敏感场景零生成回答，合规红线不被触碰。

**Acceptance Criteria:**

**Given** 意图分类与追问计数已可用
**When** 命中任一硬触发条件：红线意图（NEGOTIATION / LOAN_POLICY / COMPLAINT）、图片/语音输入、同一意图追问 ≥3 次、负面词汇命中
**Then** 立即触发转人工分支，红线意图 100% 转人工、零生成回答
**And** 判定逻辑集中于 intent_router 之后的独立判定点，判定结果写入信封并供会话管理模块记录 handoff 状态（AD-4）
**And** 硬触发条件集（追问阈值、负面词表、意图范围）配置化，默认按 FR 值生效，可灰度调整以避免追问误触发制造噪音转接

### Story 5.2: Kafka 转人工单向指令

As a 经纪人,
I want 转人工指令即时、可靠地送达我的工作台,
So that 我能在买家等待期内接管会话（P99 < 5s）。

**Acceptance Criteria:**

**Given** 硬触发条件已判定，adapters/ Kafka producer 可用
**When** 执行转人工
**Then** 转人工 = Kafka 转交指令（→ SpringBoot 工作台）+ 买家安抚通知（→ 下发适配器），严格先指令后通知（AD-7）
**And** 转人工消息送达经纪人工作台延迟 P99 < 5s（NFR-3）
**And** 指令发送失败重试 3 次后写死信队列并触发人工告警
**And** 转交指令 payload 携带会话上下文（含最近 3 轮对话摘要），经纪人接管时无需翻历史即可了解买家诉求

### Story 5.3: 转人工原子终态与买家安抚通知

As a 买家,
I want 转接发生时收到明确安抚通知，且转接后不再收到 AI 回复,
So that 我清楚知道接下来由真人服务，不会人机混答。

**Acceptance Criteria:**

**Given** 转人工指令已发送
**When** 买家安抚通知发送
**Then** 买家收到通知："已为您转接经纪人，请稍候"
**And** 转人工分支为原子终态出口：禁止回流响应路径、禁止使用模板兜底文案（AD-7），转人工后该 session 的 Agent 响应路径关闭

### Story 5.4: AI 身份披露标识

As a 买家,
I want 始终清楚正在与我对话的是 AI 助手,
So that 我的知情权和平台合规要求得到保障。

**Acceptance Criteria:**

**Given** 响应组装与下发链路已实现
**When** Agent 响应到达聊天界面
**Then** Agent 响应 100% 携带"AI 助手"标识（FR-9），经经纪人工作台聊天界面承载
**And** 转人工后标识消失，切换为经纪人姓名/头像，无残留
**And** "AI 助手"标识的展示与切换逻辑可通过 Langfuse trace 对应的会话记录验证

### Story 5.5: 转人工后会话响应路径关闭

As a 买家/经纪人,
I want 转人工后同一会话的后续消息不再产生任何 AI 应答,
So that 不会出现人机混答，经纪人接管后对话由人继续。

**Acceptance Criteria:**

**Given** session 已 mark_handoff（handoff 状态落盘 Redis）
**When** 同 session 新消息到达入口/编排器
**Then** 编排器经 SessionManager 读取 handoff_status（权威值在 Redis）短路该消息，不重建信封走完整 AI 管道

**Given** 短路发生
**When** 观察买家侧下发
**Then** 零 AI 回复：无 LLM 生成、无模板兜底应答下发

**Given** handoff 状态不存在或会话已过期（TTL 30 分钟）
**When** 新消息到达
**Then** 状态从零初始化，走正常 AI 管道

**Given** 转人工分支自身流程（Kafka 转交指令 + 买家安抚通知，Story 5.2/5.3）
**When** Story 5.5 改动后
**Then** 该路径行为不变（本次改动不触碰指令与安抚发送链路）

**And** "转人工后零 AI 回复"端到端测试用例存在且通过
**And** L1 进程内 handoff 状态快照仅作优化层读取，不影响正确性（NFR8）

## Epic 6: 买家消息指代消解

实现管道首位指代消解过滤器（`filters/coreference`，信封标准化之后、意图分类之前，手写纯 Python）：将买家消息中的房源指代归一化为自包含消解后消息与引用元数据，消解结果写入会话状态；无法确定先行词时保留原文降级，不阻塞、不转人工。完成后，意图分类、重复追问判定与 LLM 生成均无需回溯对话历史即可正确工作。

### Story 6.1: 指代消解过滤器接口与管道接入

As a 系统,
I want coreference 过滤器以既定接口边界接入管道首位,
So that 后续实现选型（OQ-5）可插拔替换，且接入不阻塞现有链路。

**Acceptance Criteria:**

**Given** 任意买家消息进入管道
**When** 通过过滤器链
**Then** coreference 在信封标准化之后、intent_router 之前执行，手写纯 Python，毫秒级可控（AD-2、AD-11）

**Given** 实现选型未决（OQ-5，规则模板 / 序列标注 / LLM 改写，第 2 周据语料标注决定）
**When** 默认实现运行
**Then** 原文透传 + 归一化钩子，输出消解后消息与引用元数据结构（引用实体类型、实体 ID），接口边界即脊柱锁定范围

**Given** 消解执行完成
**When** 写会话状态
**Then** 键 `weiliao:session:{session_id}:coref:{turn}`，由会话管理模块写入（单写者 AD-4）

**And** 透传场景下该过滤器自身延迟 P99 < 10ms 预算内

### Story 6.2: 房源指代归一化默认实现

As a 买家,
I want "它/这套/那套/上面说的"被归一化为显式房源引用,
So that 我的追问始终围绕同一套房子被系统正确理解。

**Acceptance Criteria:**

**Given** 消息含房源指代且会话状态已确定 house_id（会话绑定单一房源）
**When** 执行消解
**Then** 输出自包含消解后文本（"它多少钱" → "这套房（house_id: xxx）多少钱"）

**Given** 人工标注测试集（指代类样本 ≥ 20%，与 FR-1 测试集同期建设）
**When** 评估消解质量
**Then** 房源指代消解准确率 ≥ 95%（FR-11）

**Given** 无法确定先行词
**When** 执行消解
**Then** 保留原文进入意图分类，不阻塞、不转人工（由分类置信度与 UNKNOWN 既有路径承接）

**Given** 非房源实体指代（价格、楼层、位置等）
**When** 执行消解
**Then** MVP 尽力消解，失败按上一条路径降级

### Story 6.3: 下游消费接入与追问判定 retrofit

As a 系统,
I want 意图分类、追问判定与 LLM 生成只消费消解后消息,
So that 指代与同义改写叠加的场景被正确识别为同一意图，全链路无需回溯对话历史。

**Acceptance Criteria:**

**Given** coreference 过滤器已接入管道（Story 6.1）
**When** 意图分类（2.x）与 LLM 生成（4.3）取输入
**Then** 只消费消解后消息，不直接读取买家原始文本（FR-11）

**Given** Story 2-3 追问判定已实现
**When** 配套 retrofit 接入点
**Then** 追问判定基于消解后消息，"它多少钱"与"这套房多少钱"计为同一意图（FR-2 口径）

**And** FR-1 延迟口径验收：意图分类 P99 < 50ms 为含消解的端到端
**And** PRD 小王场景（"它采光怎么样"）端到端链路验证通过

## Epic 7: 可运维性与过载韧性

补齐运维可交付物（env 变量、model_routing 快照与告警的 runbook/README，`.env.example` 空值治理），并执行超时风暴压测、重审编排器执行器生命周期，使运维不读源码即可排障、超时风暴下服务不劣化不膨胀。

### Story 7.1: 运维文档与配置交付

As a 运维工程师,
I want 环境变量与告警有 runbook 可查、示例配置可直接启动,
So that 排障不读源码，交接与部署不靠口口相传。

**Acceptance Criteria:**

**Given** 全部新增 env 变量（含 4.4/4.5/4.6 的 WEILIAO_LLM_BREAKER_* 系列）
**When** 查阅 README/runbook
**Then** 每个变量有说明、缺省值与调参影响（NFR9）

**Given** `/health` 的 model_routing 快照与 ERROR 级 `model_routing_alert` 告警
**When** 告警触发
**Then** runbook 中有对应处置步骤可查

**Given** 干净 checkout 的仓库
**When** `cp .env.example .env` 后启动
**Then** 服务直接启动成功（无空值导致的启动失败）

### Story 7.2: 超时风暴压测与执行器生命周期重审

As a 系统运营者,
I want 超时风暴下执行线程不积累、慢调用可取消,
So that 个别慢请求被丢弃后服务不劣化、资源不膨胀、浪费的 token 有归因。

**Acceptance Criteria:**

**Given** 可控慢过滤器（llm_generate 以 stub 控制慢速），Epic 4 真实过滤器已落地
**When** 执行超时风暴压测
**Then** 执行线程不积累，P99 不劣化超预算（NFR10）

**Given** 压测结果
**When** 重审执行器生命周期
**Then** 共享池/cancel 机制/信封所有权（frozen 评估）有明确决策记录并落地

**Given** 被超时丢弃的生成调用
**When** 评估归因方案
**Then** token 记账落地，或记录"不做"的决策理由

**And** deferred-work 中 1.2/4.3 执行器相关挂账条目关闭
