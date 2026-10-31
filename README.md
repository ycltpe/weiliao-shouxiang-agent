# weiliao-agent

微聊首响 Agent —— 管道-过滤器架构的首响应答服务（开发中）。

## 架构

买家消息经标准化信封（Envelope）流经处理管道，每个过滤器仅消费上游信封、
产出下游信封，过滤器之间禁止私有耦合。当前代码库已完成项目骨架与信封模型
（Story 1.1），过滤器链、会话管理、健康检查与追踪脚手架等能力将在后续 Story 实现。

## 目录结构（Structural Seed）

```
src/weiliao_agent/
  adapters/        # Kafka consumer/producer、外部服务适配
  filters/         # 热路径过滤器：coreference、intent_router、cache_lookup、risk_review、response_assembler
  llm/             # LangGraph 子图、OpenAI client、prompt 管理
  session/         # 会话状态管理（单写者，含 handoff 状态）
  cache/           # L0 Redis 客户端、L1 cachetools 封装
  models/          # Pydantic 模型：信封、事实卡片、意图标签（Literal）
  workers/         # Kafka 消费者：房源事件同步、转人工指令
  observability/   # Langfuse trace、监控指标、成本追踪
  health/          # 健康检查端点（挂在响应组装过滤器）
```

## 信封模型

`models/envelope.py` 中的 `Envelope` 是管道全链路唯一通信模型，核心字段：
`session_id`、`house_id`、`turn`、`intent`、`timestamp`、`trace_id`；另预留
`message`、`resolved_message`、`coref`、`media_type` 四字段（暂未消费）。

任意来源的 dict 必须经 `Envelope.model_validate` 校验后方可进入管道；缺失必填
字段（`session_id` / `house_id` / `timestamp` / `trace_id`）或类型错误一律
fail-closed，抛出 `pydantic.ValidationError`，由调用方兜底。

## 环境变量

复制 `.env.example` 为 `.env` 并填写（LLM 网关使用 OpenAI 兼容接口）：

```bash
LLM_MODEL_ID=your_model_id
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://your-gateway/v1

# 会话状态 Redis（Story 1.3，会话状态唯一写入者）
WEILIAO_REDIS_URL=redis://127.0.0.1:6379/0
```

## 开发

```bash
# 安装依赖（含 dev 工具）
uv sync --all-extras

# 运行测试
uv run pytest

# 代码检查
uv run ruff check src tests
uv run mypy src
```
