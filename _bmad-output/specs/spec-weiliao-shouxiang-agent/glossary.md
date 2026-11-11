# 术语表

PRD §3 与脊柱一致性约定中的领域词汇与数据 schema。意图枚举是 CAP-1 的输出契约，事实卡片 schema 是 CAP-4 的存储契约。

## 领域词汇

- **首次消息（First Message）** — 买家在一个房源会话中发出的第 1 条文本消息。系统对首次消息进行意图识别并生成首响；缓存拦截率的统计基数为全部首次消息。
- **重复追问（Follow-up Query）** — 同一买家在同一房源会话中，针对同一意图发出的第 2 次及以后的消息。重复追问必须走 LLM，不得走缓存（避免机械化重复）。
- **意图（Intent）** — 系统对买家消息的分类结果，单一 `Literal` 类型枚举（见下）。全链路 `model_validate` 校验，未知值 fail-closed 到模板兜底。
- **红线意图（Red-line Intent）** — 必须转人工、禁止 Agent 生成回答的意图类型：NEGOTIATION / LOAN_POLICY / COMPLAINT。
- **硬触发（Hard Trigger）** — 无需置信度判断、命中即转人工的确定性规则：红线意图、图片/语音输入、同一意图追问 ≥ 3 次、负面词汇命中。
- **转人工（Human Handoff）** — 将会话控制权移交经纪人工作台的过程，包含消息转发和经纪人通知两个动作（先 Kafka 指令后安抚通知）。
- **L0 缓存（L0 Cache）** — Redis 分布式缓存，以"意图 + 房源 ID + 房源状态 + 价格分桶"为 key。存储单元是事实卡片，不是最终文案，由模板引擎或 LLM 在消费时组装。
- **L1 缓存（L1 Cache）** — cachetools.TTLCache 进程内缓存，仅存会话级热点（最近意图、追问计数、handoff 状态快照），不存事实卡片，TTL ≤ 30s。
- **模板兜底（Template Fallback）** — 当 LLM 不可用或审核不通过、超时时，由预定义模板 + 事实卡片拼装应答的降级模式。PHOTO_REQUEST 的模板应答须提供图片素材（房源图片链接/相册引导）与无图替代话术；空串/仅空白草稿按"无草稿"落入本路径。
- **模型路由（Model Routing）** — 按流量特征（意图复杂度/置信度）在 gpt-4o / gpt-4o-mini 之间的配置化选择，配套 token 预算护栏：预算超限时自动收紧（提高 mini 占比）并触发 ERROR 级告警，路由决策记录 Langfuse trace。
- **AI 身份披露（AI Disclosure）** — 聊天界面中向买家明示"当前由 AI 助手回复"的标识，不得以任何形式伪装真人经纪人。
- **会话（Session）** — 买家与某套房源的一次连续对话，以 session_id 标识，超过 30 分钟无消息自动过期。
- **首响延迟（First Response Latency）** — 从买家消息到达系统到买家收到响应的端到端耗时。
- **缓存拦截率** — 命中 L1/L0 缓存（零 LLM 调用）的请求占全部首次消息的比例，目标 ≥ 80%；与"缓存命中率"不同，后者不作为优化目标。
- **指代消解（Coreference Resolution）** — 在意图分类之前执行的预处理：将会话上下文中已知的实体引用（"它""这套""那套""上面说的"等）归一化为显式实体引用。锚定会话单一房源，先行词由会话状态确定，核心是归一化而非开放式实体链接。
- **消解后消息（Resolved Message）** — 指代消解的输出：原始消息中的指代替换为显式引用后的自包含文本（如"它多少钱" → "这套房（house_id: xxx）多少钱"）。意图分类、重复追问判定与 LLM 生成的唯一输入；下游禁止直接读取买家原始文本。

## 意图枚举（9 类）

`GREETING / PRICE_INQUIRY / AVAILABILITY / LOCATION / PHOTO_REQUEST / NEGOTIATION / LOAN_POLICY / COMPLAINT / UNKNOWN`

其中红线意图（**加粗**）必须转人工：`GREETING / PRICE_INQUIRY / AVAILABILITY / LOCATION / PHOTO_REQUEST / **NEGOTIATION** / **LOAN_POLICY** / **COMPLAINT** / UNKNOWN`

## 消息信封 schema（单一 Pydantic 模型，十字段）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session_id | str | 会话标识 |
| house_id | str | 房源标识 |
| turn | int | 会话内绝对消息序号，由会话管理模块在消息入管道前赋值 |
| intent | Intent Literal | 意图标签（见上枚举） |
| timestamp | datetime | 消息时间戳 |
| trace_id | str | Langfuse trace，贯穿全链路 |
| message | str | 买家原文（仅指代消解过滤器消费） |
| resolved_message | str | 消解后消息，意图分类/重复追问判定/LLM 生成的唯一输入 |
| coref | list[CorefRef], 可空 | 指代引用元数据（见下），信封为下游唯一消费路径 |
| media_type | Literal | text / other，由接入适配器赋值；非 text 由硬触发评估直接判转人工 |

## CorefRef schema（单一 Pydantic 模型）

指代消解输出的引用元数据，`coref` 字段的列表元素（0..n 条），权威定义在 models/。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| entity_type | Literal | house / price / floor / location / other |
| entity_id | str, 可选 | house 类型必填 house_id，其余可空 |

## 事实卡片 schema（单一 Pydantic 模型）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| house_id | str | 房源标识 |
| status | Literal | active / sold / offline |
| price_bucket | str | 当前挂牌价，分桶到 10 万 |
| school | str, 可选 | 对口学校 |
| dialogue_summary | str | 最近 3 轮对话摘要 |
| fact_version | int | Kafka 事件单调递增版本号 |
| as_of | datetime | 事实数据截止时间 |
