# 运维手册（Runbook）—— metrics 通道告警处置

本文档覆盖 `weiliao-agent` 全部 ERROR 级告警的处置：metrics stderr
JSON 通道三类——`model_routing_alert`（模型路由收紧）、
`llm_breaker_alert`（LLM 熔断非 closed 态）、`risk_review_alert`
（风控审核通过率跌破阈值），以及发送器通道的
`handoff_command_alert`（转人工指令发送失败）。每类告警给出触发条件、
`/health` 快照字段判读、日志抓取与处置步骤。

## 1. metrics stderr JSON 通道

发射机制：FastAPI lifespan 托管的定时循环（间隔
`WEILIAO_METRICS_LOG_SECONDS`，缺省 60s），每次循环按序发射四份
INFO 级结构化日志，消息体即纯 JSON 单行（`record.message` 可直接
`json.loads`）：

| 事件 | 级别 | 内容 |
|------|------|------|
| `cache_metrics` | INFO | L0 命中率计数（hit/miss/bypassed/hit_rate 等，无告警） |
| `risk_review_metrics` | INFO | 审核 total/passed/rejected/timeout/error/pass_rate |
| `llm_breaker_metrics` | INFO | 熔断器 state/requests/skipped/trips/recoveries |
| `model_routing_metrics` | INFO | 路由 primary/mini 调用与 token 累计、tightened 等 |
| `discarded_generation_metrics` | INFO | 被超时丢弃但后台跑完的生成调用数与 token 累计（Story 7.2 纯浪费成本口径，与成功口径互斥不重叠；无告警形态） |

同循环内满足条件时追加 ERROR 级告警日志：

| 告警事件 | 触发条件 |
|----------|----------|
| `risk_review_alert` | 累计审核样本 ≥ 20 且 pass_rate < `WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD`（缺省 0.95） |
| `llm_breaker_alert` | 熔断器 state ≠ `closed`（open/half_open，即 LLM 降级持续中） |
| `model_routing_alert` | 预算护栏 tightened=true（滑窗累计 total_tokens ≥ `WEILIAO_LLM_ROUTING_BUDGET_TOKENS`，全部强制 mini） |

另有发送器路径的 `handoff_command_alert`（转人工指令 producer 启动
失败或发送重试耗尽写入 DLQ）由 lifespan/发送器直接发射，不在定时循环内。
日志发射自身 fail-safe：任何异常吞掉，指标记录绝不二次影响买家应答。

## 2. 部署形态与日志抓取

本仓库 `docker-compose.yml` 仅含 redis/redpanda 等本地数据服务，应用
本体不在其中、仓库不提供 Dockerfile——应用以本地前台进程运行。
因此 `docker logs` 示例仅适用于自行容器化的部署形态，各形态等价命令：

```bash
# 形态 A：本地前台进程（仓库默认形态）——stderr 直接可见
uv run uvicorn weiliao_agent.main:app
# 需要留档时重定向 stderr：
uv run uvicorn weiliao_agent.main:app 2>>/var/log/weiliao/metrics.log

# 形态 B：systemd 托管
journalctl -u weiliao-agent -f

# 形态 C：自行容器化（docker logs 仅在此时适用）
docker logs -f <container> 2>&1 | grep --line-buffered 'model_routing_alert'

# 通用：按告警事件过滤（以下示例以形态 B/C 的流式输出为例，
# 形态 A 的重定向文件同理）
journalctl -u weiliao-agent -f | grep --line-buffered '"event": "llm_breaker_alert"'
grep '"event": "risk_review_alert"' /var/log/weiliao/metrics.log | tail -20
```

实时读数不依赖日志——就绪实例随时可读快照：

```bash
curl -s localhost:8000/health | python3 -m json.tool
# 四份快照各自独立 fail-safe：任一取数失败只丢自己那份，不连坐其他。
```

## 3. model_routing_alert（模型路由收紧）

触发条件：滑窗（`WEILIAO_LLM_ROUTING_WINDOW_SECONDS`，缺省 60s）
内累计成功生成的 `total_tokens`（primary + mini）≥
`WEILIAO_LLM_ROUTING_BUDGET_TOKENS`（缺省 50000）→ 护栏收紧：全部意图
强制走 mini 模型；滑窗消耗回落到阈值以下自动恢复常规路由。收紧态持续
期间每个发射周期都会出现本告警。

判读（`curl /health` → `model_routing` 快照）：

| 字段 | 含义 |
|------|------|
| `tightened` | 当前是否收紧（告警判定的直接依据） |
| `primary_calls` / `primary_tokens` | 主模型调用数 / token 累计（收紧期应停在告警前水平） |
| `mini_calls` / `mini_tokens` | mini 调用数 / token 累计（含收紧期 forced_mini，告警期持续攀升） |
| `over_limits` | 收紧进入次数（持续递增说明预算与真实流量长期不匹配） |
| `recoveries` | 收紧恢复次数（处置有效后应递增） |

### 诊断步骤

1. 确认告警真实性：`curl -s localhost:8000/health` 看 `tightened`
   与 `over_limits` 是否同步递增（防止日志通道重复投递误判）。
2. 区分原因：真实流量/token 突增（`primary_tokens` 增速与业务量一致），
   还是异常重试/循环放大消耗（业务量平稳但 token 激增——查 Langfuse
   trace 有无重复生成）。
3. 真实增长：调大 `WEILIAO_LLM_ROUTING_BUDGET_TOKENS` 或
   `WEILIAO_LLM_ROUTING_WINDOW_SECONDS`（改后重启生效），观察
   `recoveries` 递增确认恢复。
4. 异常放大：按 trace 定位重试来源修复；护栏本身工作正常，无需动参数。

### 影响与边界

收紧期主模型零调用、全部走 mini——买家应答不断（兜底语义不变），但生成
质量与成本结构降级为 mini 档位。另注意：若启动日志出现 ERROR
「model routing degenerate」（primary 与 mini alias 相同），属配置选择
留痕而非本告警——检查 `WEILIAO_LLM_MINI_MODEL` 是否与
`WEILIAO_LLM_MODEL` 撞名。

## 4. llm_breaker_alert（LLM 熔断非 closed）

触发条件：LLM 子图调用连续失败（OpenAI 429/5xx/超时/网络错误）
令熔断器跳闸，state 进入 `open` 或 `half_open`。冷却期（
`WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS`，缺省 10s）内请求不调子图、
立即回模板兜底；冷却满后 half-open 按 `WEILIAO_LLM_BREAKER_RECOVERY_STEP`
（缺省 0.1，即 10% 起步）确定性放量试探，成功递增、达 100% 闭合；试探
再败则跳回 open。非 closed 态持续期间每个发射周期都会出现本告警。

判读（`curl /health` → `llm_breaker` 快照）：

| 字段 | 含义 |
|------|------|
| `state` | `closed` / `open` / `half_open`（告警判定的直接依据） |
| `requests` | 熔断放行（实际触达子图）的请求数 |
| `skipped` | open/half-open 拒绝数（零子图调用、立即模板兜底）——告警期持续攀升 |
| `trips` | 跳闸次数（closed→open 与 half_open→open 迁移） |
| `recoveries` | 恢复闭合次数（处置有效后应递增） |

### 诊断步骤

1. 查凭据与网关：`OPENAI_API_KEY` 是否有效/过期（服务启动不校验凭据，
   /health 就绪不代表凭据可用）；若配置了 `OPENAI_BASE_URL`，用
   `curl -m 5 <base_url>/models -H "Authorization: Bearer $OPENAI_API_KEY"`
   验证网关连通性与鉴权。
2. 查模型 alias：`WEILIAO_LLM_MODEL` / `WEILIAO_LLM_MINI_MODEL` 是否
   拼写正确、账号是否有权限（无权限典型表现为 403/404 反复失败）。
3. 查配额：429 占比高说明 token/RPM 配额不足——升配额或调大
   `WEILIAO_LLM_ROUTING_BUDGET_TOKENS` 让护栏更早收紧保护主模型。
4. 等待自愈或调参：上游恢复后熔断器自动闭合（观察 `recoveries` 递增）；
   上游持续故障期可调大 `WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS` 减少
   无效试探。频繁跳闸（`trips` 高但上游正常）优先按 1–2 排查配置。

### 影响与边界

open 期间 LLM 草稿全停，买家应答 100% 模板兜底（应答不断、未经审核的
LLM 内容绝不外发）。`skipped` 高而 `requests` 为 0 是设计内行为，非故障
本身。

## 5. risk_review_alert（审核通过率跌破阈值）

触发条件：累计审核样本 ≥ 20（`MIN_RISK_REVIEW_ALERT_SAMPLES`，防
抖动）且 `pass_rate < WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD`（缺省
0.95）。样本不足 20 不告警。

判读（`curl /health` → `risk_review_metrics` 快照）：

| 字段 | 含义 |
|------|------|
| `total` | 实际审核调用数（分母，含超时/错误） |
| `pass_rate` | passed / total；total=0 时为 null |
| `passed` / `rejected` | 中台判通过 / 判拒绝数 |
| `timeout` | 审核请求超时数（endpoint 不可达或预算不足的典型信号） |
| `error` | 审核请求 5xx/网络错误数（中台故障信号） |

### 诊断步骤

1. 按字段分布定因：
   - `timeout` 占比高：`WEILIAO_RISK_REVIEW_ENDPOINT` 不可达或
     `WEILIAO_RISK_REVIEW_TIMEOUT_MS`（300–800）预算不足——
     `curl -m 2 <endpoint>` 验证连通性，必要时调大超时（占用端到端
     预算，注意 300–800 fail-fast 边界）。
   - `error` 占比高：中台 5xx/网络故障——联系中台方，服务侧继续
     fail-closed 兜底。
   - `rejected` 占比高：中台内容策略收紧或 LLM 草稿质量下降——经
     Langfuse trace 抽查被拒草稿，区分中台侧与生成侧原因。
2. 检查配置缺口：`WEILIAO_RISK_REVIEW_ENDPOINT` 未配置时每次草稿
   fail-closed 视为不通过，快照读数天然失真（total 增长、passed 恒 0）——
   这是配置缺口而非中台故障，按 README「快速启动」补齐配置。
3. 阈值本身：`WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD` 调低可减少告警
   噪音，但须与运营对审核质量的容忍度对齐（0–1，越界 fail-fast）。

### 影响与边界

审核 fail-closed 语义恒定——任何「不通过/超时/错误」草稿一律回模板兜底，
买家侧绝无未审核内容外发；告警期服务可用性不受影响。

## 6. handoff_command_alert（转人工指令发送失败）

发送器通道告警，不在 metrics 定时循环内、随事件即时发射，两处来源：

触发条件：

- 发送耗尽：硬触发命中后转交指令发送重试 3 次（首次发送即第 1 次，
  有界指数退避，总阻塞预算 `SEND_BUDGET_SECONDS`）仍失败——写入 DLQ
  topic（`WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC`，缺省 handoff.commands.dlq）
  并发射 ERROR 级 `handoff_command_alert`；
- producer 启动失败：lifespan 启动发送器（aiokafka producer start）
  失败——发射 `handoff_command_alert producer start failed; sender
  disabled`，发送器禁用，转人工出口分支跳过发送（买家安抚文案不变）。

判读（事件 payload 为文本键值、非纯 JSON，字段在消息体内）：

| 字段 | 含义 |
|------|------|
| `session_id` / `trace_id` / `handoff_id` | 定位会话与触发（Langfuse trace 同键可查 sent / failed / dlq 全过程） |
| `topic` / `attempts` / `error` | 目标 topic、已试次数、归一错误名 |
| `dlq_topic` / `dlq_written` | 死信目标 topic 与 DLQ 是否写成功（`dlq_written=false` 说明 DLQ 亦不可用，仅告警留痕） |

### 诊断步骤

1. producer 启动失败型：查 `WEILIAO_KAFKA_BROKERS` 连通性（broker 未
   就绪/地址错）——`kafkacat -b <brokers> -L` 或 `docker compose restart
   redpanda`；恢复后重启本服务（发送器装配期定版，运行期不重连）。
2. 发送耗尽型：查 broker 可用性与 `WEILIAO_HANDOFF_KAFKA_TOPIC`（缺省
   handoff.commands）是否存在/可写；`dlq_written=false` 时同时查
   `WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC` 是否存在——DLQ topic 须预创建。
3. 恢复与补发：发送语义 at-least-once，工作台消费者按 payload 中
   `handoff_id`（UUID4）去重——DLQ 中的指令可由消费侧 redrive 回
   `handoff.commands` 补投，重复指令对消费者安全。
4. 影响边界：发送全程 fail-safe，任何失败绝不改变买家应答（出口文案恒为
   固定安抚文案）；告警期损失的是经纪人工作台及时接管，买家侧无未审核
   内容外发。

## 7. 处置验证示例

```bash
# 就绪与四份快照
curl -s localhost:8000/health | python3 -m json.tool

# 路由告警确认（形态 B 示例）
journalctl -u weiliao-agent --since "10 min ago" \
  | grep '"event": "model_routing_alert"' | tail -5

# 熔断器是否已恢复（recoveries 递增 / state 回 closed）
curl -s localhost:8000/health | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['llm_breaker'])"

# 审核率回升确认
curl -s localhost:8000/health | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['risk_review_metrics'])"
```
