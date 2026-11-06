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
  cache/           # L0 Redis 客户端（事实卡片）、L1 cachetools 封装（会话热点快照，TTL ≤30s）
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

L1 进程内缓存（Story 3.3，会话级热点读优化层）TTL 可调，缺省 30 秒：

```bash
# L1 快照 TTL（AD-5 硬上限 30s，传入 >30 启动即 fail-fast）
WEILIAO_L1_TTL_SECONDS=30
```

房源事件同步消费者（Story 3.2，Kafka `house.sync.trigger`）为可选启停：

```bash
# 非空才启动消费者（缺省不启动：本地开发与测试零 broker 零噪音）
WEILIAO_KAFKA_BROKERS=localhost:9092
# 以下为缺省值，显式设置可覆盖：house.sync.trigger / weiliao-house-sync
WEILIAO_KAFKA_TOPIC=house.sync.trigger
WEILIAO_KAFKA_GROUP_ID=weiliao-house-sync
```

缓存命中率指标（Story 3.4，A 方案读数入口）：追问（`is_followup`）
禁走 L0 缓存、强制进入 LLM 链路，并计入 `bypassed` 而完全不计入
命中率分子/分母。命中率口径：分子 = L0 命中（`l1_hit` 为 L1 预留
并入位，当前无 L1 读消费者、恒 0）；分母 = 非追问且 intent 非 None
的实际 L0 查询数（hit + miss，Redis 故障归并 miss）；分母为 0 时
`hit_rate` 为 `null`。计数器是进程内单例：多实例部署时每个进程独立
计数，`/health` 读到的是被采样实例自身的数；计数窗口等于进程生命
周期，进程重启即清零。读数方式：

```bash
# 压测期间定时打 stderr（结构化 JSON 单行；缺省 60s，非正整数 fail-fast）
WEILIAO_METRICS_LOG_SECONDS=60

# 或随时读就绪实例的快照
curl -s localhost:8000/health
# → {"status": "ready", "cache_metrics": {"l0_hit": …, "l0_miss": …,
#    "l1_hit": 0, "bypassed": …, "lookups": …, "hits": …, "hit_rate": …}}
```

本地联调 broker：`docker compose up -d redpanda redpanda-init`（单节点
redpanda，Kafka API 暴露于 9092，init 容器自动创建
`house.sync.trigger` topic），随后以前述
`WEILIAO_KAFKA_BROKERS=localhost:9092` 启动服务，即可经 rpk 发送事件
观察消费日志与 Langfuse 事件。

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
