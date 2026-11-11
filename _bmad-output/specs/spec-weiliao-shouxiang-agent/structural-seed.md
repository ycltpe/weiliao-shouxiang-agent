# 结构种子与能力映射

架构脊柱的 Structural Seed（代码库目录骨架）与 Capability → Architecture Map（能力落点），实现期以此对齐物理结构与 AD 约束。

## 目录结构种子

```text
首响 Agent 服务（Python，独立部署）
  adapters/          # Kafka consumer/producer、外部服务适配
  filters/           # 热路径过滤器：coreference（指代消解，管道首位）, intent_router, cache_lookup, risk_review, response_assembler
  llm/               # LangGraph 子图、OpenAI client、prompt 管理
  session/           # 会话状态管理（单写者，含 handoff 状态）
  cache/             # L0 Redis 客户端、L1 cachetools 封装
  models/            # Pydantic 模型：信封、事实卡片、意图标签（Literal）
  workers/           # Kafka 消费者：房源事件同步、转人工指令
  observability/     # Langfuse trace、监控指标、成本追踪
  health/            # 健康检查端点（挂在响应组装过滤器）
```

## Capability → Architecture Map

| Capability | Lives in | Governed by |
| --- | --- | --- |
| CAP-11 指代消解（FR-11） | `filters/coreference` | AD-1, AD-2, AD-11 |
| CAP-1/2/3 意图识别与路由（FR-1/2/3） | `filters/intent_router`（含硬触发评估模块） | AD-1, AD-2, AD-3, AD-12 |
| CAP-4/5 缓存与事实卡片（FR-4/5） | `cache/`, `workers/house_sync` | AD-4, AD-5, AD-6 |
| CAP-6/7/8 LLM 生成与风控（FR-6/7/8） | `llm/`, `filters/risk_review` | AD-2, AD-8, AD-9 |
| CAP-12 转人工后响应路径关闭（FR-12） | 编排器入口（handoff_status 短路）+ `session/`（权威状态读取） | AD-4, AD-7 |
| CAP-13 模型路由与成本护栏 | `llm/`（路由与预算）、`observability/`（成本追踪） | AD-9 |
| CAP-9 AI 身份披露（FR-9） | `filters/response_assembler` | AD-10 |
| CAP-10 会话状态管理（FR-10） | `session/` | AD-4 |
