# weiliao-agent

微聊首响 Agent —— 管道-过滤器架构的首响应答服务（开发中）。

## 架构

买家消息经标准化信封（Envelope）流经处理管道，每个过滤器仅消费上游信封、
产出下游信封，过滤器之间禁止私有耦合。当前代码库已完成项目骨架与信封模型
（Story 1.1）及过滤器链编排、会话状态管理、健康检查端点、Langfuse
追踪脚手架（Story 1.2-1.5，Epic 1 已交付）；意图分类、事实缓存、LLM
子图、转人工与指代消解等能力见 `_bmad-output/implementation-artifacts/`
各 Story spec。

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
字段（`session_id` / `house_id` / `timestamp`）或类型错误一律
fail-closed，抛出 `pydantic.ValidationError`，由调用方兜底。
`trace_id` 自 Story 1.5 起放宽为可选——缺失由编排器入口权威生成，
客户端携带值按正式契约透传（纯空白串拒绝）。

## 快速启动

前置：Python ≥3.11 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 1. 安装依赖（含 dev 工具）
uv sync --all-extras

# 2. 启动数据服务（docker-compose.yml 仅含 redis/redpanda 等本地数据服务，
#    应用本体不在其中、无 Dockerfile——compose 起依赖，应用本地前台运行）
docker compose up -d redis redpanda redpanda-init
#    本地联调 broker：单节点 redpanda，Kafka API 暴露于 9092，init 容器自动
#    创建 house.sync.trigger topic；零 broker 开发可跳过本步（消费者/发送器
#    不装配，服务仍就绪）

# 3. 生成配置（.env 由组合根启动时自动加载——锚定仓库根路径，
#    override=False 不覆盖已导出的进程环境变量；统一从仓库根目录启动）
cp .env.example .env

# 4. 【必需步骤】替换 OPENAI_API_KEY 占位符为真实密钥
#    凭据为惰性读取：/health 就绪不代表凭据可用，不配密钥 LLM 链路全量
#    模板兜底。表征：占位/未配置密钥时 LLM 子图调用反复 401 失败——
#    llm_breaker 反复跳闸 + llm_breaker_alert 告警（/health llm_breaker
#    快照 state 非 closed），处置见 docs/runbook.md §4。OpenAI 兼容网关
#    另取消注释 OPENAI_BASE_URL 并填网关地址（缺省=官方 API）。

# 5. 启动（须从仓库根目录启动——相对路径配置以启动 cwd 解析）
uv run uvicorn weiliao_agent.main:app --reload

# 6. 验证就绪
curl -s localhost:8000/health
# → {"status": "ready", "cache_metrics": …, "risk_review_metrics": …,
#    "llm_breaker": …, "model_routing": …}
```

结构化指标日志（命中率/审核/熔断器/路由，stderr JSON 单行，间隔
`WEILIAO_METRICS_LOG_SECONDS`）与三类 ERROR 级告警
（`model_routing_alert` / `llm_breaker_alert` / `risk_review_alert`）的
触发条件、判读与处置步骤见 [docs/runbook.md](docs/runbook.md)。

## 环境变量

复制 `.env.example` 为 `.env` 即得全量可启动配置（`.env.example` 取消注释
条目取值全部非空）。环境变量清单以代码（`src/weiliao_agent/main.py` 组合根
及各模块读取点）为唯一事实源，`tests/test_env_example.py` 守护测试双向钉住
「代码消费集 ↔ 样例声明集」一致性。

**通用规则：**

- 数值型变量（下表「缺省」为数字者）**留空即启动失败**（fail-fast，错误
  消息带变量名）——os.environ.get 对空串不回落缺省；恢复缺省的正确做法是
  **删除该行**，而非留空。
- 修改 `.env` 后**须重启进程生效**（无热加载；`uvicorn --reload` 只监听
  代码变更，不监听 `.env`）。
- 代码 fail-fast 的变量，配置错误拒绝启动（错误消息带变量名），属设计内
  安全行为。
- `.env` 由服务启动早期自动加载（不覆盖已存在的进程环境变量），详见
  「快速启动」。

**LLM 凭据（OpenAI SDK 默认读取，非 WEILIAO_ 前缀）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `OPENAI_API_KEY` | 无（必填占位） | OpenAI 兼容网关凭据；SDK 默认读取，服务启动不校验 | 不配密钥 LLM 链路全量模板兜底；/health 就绪不代表凭据可用 |
| `OPENAI_BASE_URL` | 官方 API | 网关服务地址（OpenAI SDK 默认读取）；`.env.example` 按 G1-A 注释占位 | 配错地址 SDK 静默指向坏主机：熔断跳闸 + llm_breaker_alert 洪泛 |

**会话与缓存（Story 1.3 / 3.3）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_REDIS_URL` | `redis://127.0.0.1:6379/0` | 会话状态 Redis（会话状态唯一写入者） | 指向实例须可用；Redis 故障按 fail-closed 逐消息降级，服务仍就绪 |
| `WEILIAO_REDIS_SOCKET_TIMEOUT` | `0.5` | Redis socket 超时秒数（Story 7.2，须 (0, 1.0]，非数值/非正/非有限 nan/inf/超上界启动 fail-fast）；会话写路径（begin_turn/write_intent/write_coref 等同步 SET）的有界阻塞兜底 | 调大容忍慢 Redis（占用热路径过滤器预算，上界 1.0s 须远小于 2.5s 内部预算）；调小更快 fail-safe 回落 |
| `WEILIAO_L1_TTL_SECONDS` | `30` | L1 进程内缓存 TTL（AD-5 硬上限 30，>30 启动 fail-fast）；仅存会话热点快照，事实卡片严禁入 L1 | 调小：追问状态更快回落 Redis 权威值；硬上限防事实卡片过期前外发 |

**意图分类（Story 2.2）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_INTENT_CLASSIFIER` | `llm_zero_shot` | 意图分类器实现（`none` 保留占位行为） | 非法值启动 fail-fast；换实现只动配置与组合根 |
| `WEILIAO_INTENT_MODEL` | `gpt-4o-mini` | 意图分类模型 alias（构造参数 > 本变量 > 模块缺省） | 换更优模型提分类质量；alias 不存在时分类 fail-closed 走兜底 |
| `WEILIAO_INTENT_TIMEOUT_SECONDS` | `0.8` | 意图分类硬超时秒数（构造参数 > 本变量 > 模块缺省；2026-11-10 评审放宽，原 45ms 与真实 LLM 网络 RTT 物理不相容） | 调小收紧延迟、调大容忍慢网络；非正值启动 fail-fast（构造抛 `ValueError`） |

**Kafka（Story 3.2 / 5.2 共用门控，可选启停）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_KAFKA_BROKERS` | 未配置（不启动） | 非空才启动 `house.sync.trigger` 消费者与转人工指令发送器；样例 `localhost:9092` 对应 compose redpanda | **删除该行即零 broker 零噪音**，服务仍就绪（消费者后台重试、发送器启动失败禁用，既有 fail-safe） |
| `WEILIAO_KAFKA_TOPIC` | `house.sync.trigger` | 房源同步 topic | 须与 init 容器创建的 topic 一致 |
| `WEILIAO_KAFKA_GROUP_ID` | `weiliao-house-sync` | 房源同步消费组 | 多实例共享消费进度用同组；独立重复消费用异组 |

**指标日志（Story 3.4）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_METRICS_LOG_SECONDS` | `60` | 命中率/审核/熔断器/路由结构化 stderr 日志定时发射间隔（非正整数 fail-fast） | 调小：排障时更快看到读数；调大：降低日志量。告警（跌破阈值/非 closed/收紧态）随同循环发射，处置见 runbook |

缓存命中率口径（Story 3.4，A 方案）：追问（`is_followup`）禁走 L0 缓存、
强制进入 LLM 链路，并计入 `bypassed` 而完全不计入命中率分子/分母；分子 =
L0 命中（`l1_hit` 为 L1 预留并入位，当前无 L1 读消费者、恒 0）；分母 = 非
追问且 intent 非 None 的实际 L0 查询数（hit + miss，Redis 故障归并 miss）；
分母为 0 时 `hit_rate` 为 `null`。计数器是进程内单例：多实例部署时每个进程
独立计数，`/health` 读到的是被采样实例自身的数；计数窗口等于进程生命周期，
进程重启即清零。注：`l1_hit` 字段为预留并入位，AD-5 严禁 L1 存事实卡片、
L1 不存在事实命中，该字段永不兑现、恒 0（2026-11-11 评审裁决 D1）。

**风控中台审核（Story 4.4）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_RISK_REVIEW_ENDPOINT` | 未配置（fail-closed） | 双审核第二道 endpoint；`.env.example` 按 G1-A 注释占位 | 未配置：任何 LLM 草稿视为不通过、回模板兜底（设计内安全行为）；配错地址：每次草稿 500ms 超时 + 样本 ≥20 后 risk_review_alert 常亮 |
| `WEILIAO_RISK_REVIEW_TIMEOUT_MS` | `500` | 审核超时毫秒数（300–800，越界 fail-fast） | 调大容忍慢中台（占用端到端预算）；调小快速失败归 fail-closed |
| `WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD` | `0.95` | 通过率告警阈值（须 0–1，越界 fail-fast） | 累计审核样本 ≥20 且 pass_rate 跌破阈值时发射 ERROR 级 risk_review_alert（处置见 runbook） |

**LLM 生成与熔断器（Story 4.3 / 4.5）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_LLM_MODEL` | `gpt-4o` | 主模型 alias（构造参数 > 本变量 > 模块缺省）；名单外意图与 intent=None 走主模型 | alias 不存在/无权限：熔断跳闸 + llm_breaker_alert，服务回模板兜底 |
| `WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS` | `10` | 熔断器冷却期（非正或非有限 nan/inf fail-fast）；OpenAI 侧故障跳闸后冷却期内请求不调子图、立即回模板兜底 | 调大：上游持续故障期减少无效试探；调小：更快恢复 LLM 草稿链路 |
| `WEILIAO_LLM_BREAKER_RECOVERY_STEP` | `0.1` | 恢复步进（须 0<x≤1，越界 fail-fast）；冷却满后按该比例确定性放量试探，成功递增、达 100% 闭合 | 量化口径：档位 N=round(1/step)，放行比例=k/N（0.1→10 档 10% 步进；0.3→3 档 33.3% 步进）；调小：恢复更保守 |

**模型路由与 token 预算护栏（Story 4.6）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_LLM_MINI_MODEL` | `gpt-4o-mini` | mini 模型 alias（strip 后为空 fail-fast）；名单内简单意图与预算收紧期走该模型 | 与主模型 alias 相同时启动 ERROR 告警留痕（路由静默失效，不 fail-fast） |
| `WEILIAO_LLM_ROUTING_SIMPLE_INTENTS` | `GREETING,LOCATION` | 简单意图名单（逗号分隔；解析为空或含未知标签 fail-fast） | 名单内意图省成本走 mini；调宽名单降本、缩名单提质量 |
| `WEILIAO_LLM_ROUTING_BUDGET_TOKENS` | `50000` | 滑窗 token 预算阈值（非正整数 fail-fast）；窗口内累计成功生成 total_tokens ≥ 该值即收紧、全部强制 mini，回落自动恢复 | 调小：更早收紧省成本；调大：容忍流量高峰。收紧期发射 ERROR 级 model_routing_alert（处置见 runbook） |
| `WEILIAO_LLM_ROUTING_WINDOW_SECONDS` | `60` | 滑窗时长秒数（非正或非有限 nan/inf fail-fast） | 与预算阈值联动定义「每分钟 token 预算」口径 |

**转人工（Story 5.1 / 5.2）：**

| 变量 | 缺省 | 说明 | 调参影响 |
|------|------|------|----------|
| `WEILIAO_HANDOFF_FOLLOWUP_THRESHOLD` | `3` | 硬触发追问阈值（非正整数 fail-fast）；同一意图追问序号 ≥ 该值即触发转人工 | 调小：更早转人工、人工接管率高；调大：更多会话留在自动链路 |
| `WEILIAO_HANDOFF_REDLINE_INTENTS` | `NEGOTIATION,LOAN_POLICY,COMPLAINT` | 红线意图集合（解析为空或含未知标签 fail-fast）；命中任一即触发转人工，下游 cache/llm/risk/assembler 零执行 | 按运营红线调整；意图标签须在意图枚举内 |
| `WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH` | 仓库内置词表绝对路径 | 负面词表文件路径（不可读或无词条 fail-fast，不静默放行）；`.env.example` 按 G1-A 注释占位（相对路径样例以启动 cwd 解析，须仓库根目录启动） | 自定义词表：取消注释并保证启动 cwd 正确；删行回落内置绝对路径 |
| `WEILIAO_HANDOFF_KAFKA_TOPIC` | `handoff.commands` | 转人工指令 topic（strip 后为空 fail-fast）；经纪人工作台消费该 topic | 复用 `WEILIAO_KAFKA_BROKERS` 门控——broker 未配置不装配发送器 |
| `WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC` | `handoff.commands.dlq` | 死信 topic（为空或与指令 topic 相同 fail-fast）；发送重试 3 次仍失败写入 + handoff_command_alert 告警 | 独立死信队列；与指令 topic 相同会死信写进 live topic |

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

**压测设施（Story 7.2）：**

```bash
# 超时风暴压测（NFR10 验收工具）：进程内 stub 慢调用驱动，零外部服务依赖
uv run python scripts/loadtest_timeout_storm.py                # 形态 B（生产影子口径，默认）
uv run python scripts/loadtest_timeout_storm.py --budget 0.5   # 形态 A（逐跳丢弃口径）
# 退出码 0 = 全部断言通过（线程有界 / P99 在预算内 / 丢弃归因一致）；
# 非零 = 打印断言失败清单。读数口径见脚本 docstring。

# FR-1 全链压测：coreference → intent_router 端到端 P99 < 50ms
uv run python scripts/loadtest_intent_chain.py
# 无 OPENAI_API_KEY 时显式 skip（退出码 0，不产出数据）；有凭据时
# 默认参数发起 8×50=400 次真实结构化输出调用——评估 OpenAI 成本后再执行。
```
