---
title: 'Story 4.5 OpenAI 故障降级与步进恢复'
type: 'feature'
created: '2026-11-08'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'b7b56125399fdd96fa698c0d8475bcde78b927ef'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 生成链路虽已全路径 fail-safe（生成异常吞掉、草稿为 None、回模板兜底），但每次故障请求都要傻等满 0.9s 生成预算才 fallback，故障期间持续烧预算且无"故障中"概念；更没有恢复机制——OpenAI 恢复后无任何通道感知并切回；降级事件无 Langfuse 记录、无监控告警、/health 无读数，AC「恢复后按 10% 步进逐步切回」完全未实现。

**Approach:** 在 LLM 生成链路引入线程安全熔断器（closed/open/half-open 三态）：命中 OpenAI 侧故障（429 / 5xx / 超时 / 网络错误）即跳闸，open 期间跳过的请求立即回模板兜底（切换 <100ms、不触达子图）；冷却期后经 half-open 按 10% 步进放量试探，成功递进、失败回跳。trace 事件 + 指标计数 + /health 快照 + 定时告警日志，全部复用 4.4 risk_review 指标的既有模式。

## Boundaries & Constraints

**Always:**
- 故障判定只看 OpenAI 侧原因：`rate_limit` / `server_*` / `timeout` / `attempt_timeout` / `network`（reason 词表以 `llm/client.py` `_classify_error` 产出为准）；确定性失败（400 系、凭据、invalid_output）与 grounding 未过**不跳闸**。
- 熔断只作用于 `llm_generate` 过滤器链路；意图分类器（Epic 2，独立 OpenAI 客户端）不在本 Story 范围。
- 买家可见语义逐字节不动：跳过/失败路径与 4.3/4.4 同口径（degraded=False，质量信号走 trace + 指标 + 告警），任何路径不返回 5xx。
- 熔断器自身与全部埋点 fail-safe：绝不让请求路径因熔断逻辑抛错。
- 红线意图/非文本输入照旧在熔断咨询**之前**跳过，不污染熔断统计。

**Never:**
- 不改 `llm/client.py` 的重试/预算语义（429/5xx/超时重试恰 1 次是 4.2 冻结口径）；熔断叠在过滤器层，客户端保持纯粹。
- 不改管道编排器、`response_assembler`、模板兜底的任何语义；不动 Epic 5 转人工终态路径。
- 不做多级替代模型降级链（Epic 4 Technical Decisions 明确排除）；不做模型路由（4.6）。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 预期输出 / 行为 | 错误处理 |
|------|------------|----------------|---------|
| 正常生成 | breaker closed，子图返回文本 | 草稿照常写入信封；breaker 记成功信号 | N/A |
| OpenAI 故障 | generate 抛 `LlmGenerationError`（reason ∈ 故障词表） | 该请求草稿 None（既有吞错路径）；breaker 跳闸 open；**后续**请求被跳过：不调子图、<100ms 回模板兜底 | trace 记 ERROR 级跳闸事件；ERROR 级 `llm_breaker_alert` 告警日志 |
| open 期间请求 | breaker open 且冷却未满 | 立即跳过（不调子图、不烧预算），信封原样透传，买家收模板兜底 | trace 记 skip 事件（DEFAULT 级）；degraded 语义不动 |
| 步进恢复 | open 且冷却已满 | 进入 half-open：按 10% 比例放行试探（确定性计数取模，非随机） | 试探成功 → 放行比例 +10%，达 100% 闭合 closed 并记 recovered 事件；试探失败 → 回 open，冷却重启 |
| 确定性失败 | reason 为 http_4xx / 凭据类型名 / invalid_output / grounding 未过 | 不跳闸，breaker 状态与统计不动 | 既有路径（吞错/丢草稿）不变 |
| 红线/非文本 | NEGOTIATION 等或 media_type 非 text | 熔断咨询前的既有早退，breaker 不被触达 | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/llm/circuit_breaker.py` -- 新建：熔断器本体（三态状态机、故障 reason 判定、构造参数 fail-fast、线程安全）；纯进程内状态，不依赖 OpenAI SDK。
- `src/weiliao_agent/filters/llm_generate.py` -- 注入式接入点：请求前 `before_request()` 咨询（False 即跳过 + trace skip 事件），成功后 `on_success()`，`LlmGenerationError` 且故障 reason 时 `on_failure()`；breaker 为可选注入（None=不熔断，保持既有测试与惰性默认路径语义）。
- `src/weiliao_agent/llm/client.py` -- 只读消费 reason 词表（`_classify_error` 产出）；不改代码。
- `src/weiliao_agent/observability/tracing.py` -- 新增 `record_llm_breaker_event`（outcome: tripped/skip/recovered，仿 `record_risk_review_event` 形状，fail-safe）。
- `src/weiliao_agent/observability/metrics.py` -- 新增 `LlmBreakerMetricsCollector`（线程安全，仿 `RiskReviewMetricsCollector`：requests/skipped/trips/recoveries + 当前状态快照）+ `emit_llm_breaker_metrics_log`（非 closed 态在定时日志中追加 ERROR 级 `llm_breaker_alert`）。
- `src/weiliao_agent/health/probe.py` -- `llm_breaker_payload()` 挂 /health（仿 risk_review 快照先例，各自 fail-safe）。
- `src/weiliao_agent/main.py` -- 组合根：构造熔断器注入 LlmGenerateFilter；`WEILIAO_LLM_BREAKER_COOLDOWN_SECONDS`（缺省 10，非正 fail-fast 带变量名）、`WEILIAO_LLM_BREAKER_RECOVERY_STEP`（缺省 0.1，须 0<x<=1，越界 fail-fast 带变量名）；`_metrics_log_loop` 增发 breaker 指标日志。
- `src/weiliao_agent/pipeline.py`、`filters/templates.py`、`filters/response_assembler.py` -- 只读，不改（最终组装点与兜底语义冻结）。
- `tests/test_llm_breaker.py`（新建：状态机全分支，注入 fake clock 控制冷却）、`tests/test_llm_breaker_metrics.py`（新建）、`tests/test_llm_generate_filter.py`（扩：breaker 注入矩阵——跳过不调子图、故障跳闸、成功信号、确定性失败不跳闸）、`tests/test_health.py`（扩：快照非零回读）、`tests/test_house_sync.py` 既有循环测试（扩：定时日志含 `llm_breaker` 事件）——全程替身零网络。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/llm/circuit_breaker.py` -- 熔断器状态机（closed/open/half-open、确定性步进放量、故障 reason 词表判定、构造 fail-fast、线程安全）-- AC 第 1/2/3 条与 I/O 矩阵。
- [x] `src/weiliao_agent/filters/llm_generate.py` -- 可选注入 breaker 的咨询/信号挂点 + skip 透传 -- AC 第 1/2 条（切换 <100ms 的前提是 open 期间零子图调用）。
- [x] `src/weiliao_agent/observability/tracing.py` -- `record_llm_breaker_event` -- AC 第 2 条（降级事件入 trace）。
- [x] `src/weiliao_agent/observability/metrics.py` + `health/probe.py` + `main.py` -- 计数器、/health 快照、定时日志与告警、组合根 env 校验接线 -- AC 第 3 条（监控告警）与配置 fail-fast。
- [x] `.env.example` -- 两个新环境变量文档条目（仓库既有 WEILIAO_ 变量文档先例）。
- [x] `tests/`（两个新建 + 三个既有扩展）-- 替身覆盖矩阵全分支与组合根回读断言，无网络。

**Acceptance Criteria:**
- Given OpenAI 连续返回 429/5xx/超时，when 首条故障请求后再有买家消息进 LLM 链路，then 熔断器 open、该请求不调子图即回模板兜底（切换 <100ms），trace 留有跳闸 ERROR 事件，且 degraded=False、无 5xx。
- Given 熔断器 open 持续超过冷却时长，when 后续请求到达，then 按 10% 比例放行试探：试探成功放行比例递增至 100% 后闭合（trace 留 recovered 事件）；任一试探失败立即回 open 并重启冷却。
- Given 400 系/凭据/invalid_output/grounding 未过，when 生成失败，then 不跳闸、放行比例不变，既有吞错/丢草稿路径逐字节不变。
- Given 冷却时长或步进配置非法（0、负数、step>1），when 组合根装配，then fail-fast 拒绝启动（错误消息带变量名）；合法值经子进程导入回读落到注入熔断器与指标单例。
- Given 服务运行中，when curl /health 或定时指标日志发射，then 就绪响应含 `llm_breaker` 快照（状态+计数），非 closed 态定时日志追加 ERROR 级 `llm_breaker_alert`。

## Implementation Notes

## Design Notes

- 步进放量的确定性实现：维护整数请求计数与「当前步数 k/总步数 N」（step=0.1 即 N=10），放行条件 `counter % N < k`，无随机、可测。
- 成功信号口径：子图 `generate()` 未抛 OpenAI 故障即算成功（grounding 未过属内容问题、非 OpenAI 故障，中性不信号）。`on_success` 仅在 half-open 放行请求上推进恢复，closed 态为正常统计。
- skip 事件 DEFAULT 级：open 期间每次跳过都记 ERROR 会造成噪音洪流，ERROR 只留给跳闸与告警日志（trace 可检索 tripped/recovered 锚点）。
- 「切换 <100ms」的可测口径：open 状态下 skip 路径不创建子图 future、零 I/O，测试断言子图替身调用次数与纯内存路径即可，不做脆弱的真时钟断言。

## Spec Change Log

## Review Triage Log

2026-11-08 评审（盲审 12 + 边界 4 + 验证缺口 2，共 18 条）：

- [medium→patch] 冷却期 env 校验 `nan`/`inf` 洞 — `float("nan")` 与 `float("inf")` 解析通过且 `nan <= 0` 为 False：NaN 令 open 立即抖回 half-open，inf 令熔断永不恢复。修复：组合根校验加 `math.isfinite` 且补 nan/inf 用例（盲审 #1 + 边界 #1 同根）。
- [low→patch] 熔断故障词表与 `llm/client.py _classify_error` 为手工镜像，无测试钉住 client 实际产出串 — client 改 reason 串则熔断静默永不跳闸。修复：加词表对齐测试（client 源码含全部跳闸词 + `is_transient_reason` 全放行）防漂移（盲审 #2；4.4 评审「同 Literal 两处定义」先例的镜像处理）。
- [false] 「原始 TimeoutError/ConnectionError 泄漏则永不跳闸」— 被驳回：`llm/client.py generate()` 对全部异常归一为 `LlmGenerationError`（catch-all 分支），子图层无未守卫 I/O；类型名 reason 仅出现在凭据等确定性失败，按设计本来就不跳闸（盲审 #3）。
- [low→patch] 过滤器 fail-safe（breaker 咨询/信号抛错吞掉按放行处理）无测试 — 修复：补替身抛错用例断言放行与事件不丢（盲审 #4）。
- [false] 「跨 epoch 陈旧成功/失败信号污染新 half-open 轮次」— 被驳回：过滤器内生成受 0.9s 预算上界约束，half-open 仅在完整冷却（缺省 10s）后可达；陈旧信号到达窗口与 half-open 窗口不重叠，所述时序在实证运行条件下不可达（盲审 #5 + 边界 #2/#4 同根）。
- [low→patch] 恢复步进取整（`round(1/step)`）未文档化，step=0.15 等值的实际粒度偏离配置字面 — 修复：docstring/.env.example 注明量化语义（N=round(1/step)，比例=k/N），不改行为（盲审 #6 + 边界 #3 同根）。
- [low→patch] `test_health_llm_breaker_snapshot_nonzero_readback` 无 autouse 重置固件，中途断言失败会弄脏共享单例 — 修复：改用固件前后重置（盲审 #7）。
- [false] 「spec 状态 in-progress 与任务全勾选矛盾、sprint-status 应为 review」— 被驳回：工作流产物状态，评审启动前已置 in-review，sprint-status 在呈现步迁移（盲审 #8）。
- [false] 「open 期间迟到故障不计数、无持续故障信号」— 被驳回：持续故障信号=定时 `llm_breaker_alert`（非 closed 态每周期 ERROR 级），open 期不重启冷却为设计内口径（盲审 #9）。
- [false] 「/health 键名 `llm_breaker` 无 `_metrics` 后缀不一致」— 被驳回：spec AC 明文规定 `llm_breaker` 键（盲审 #10）。
- [false] 「Implementation Notes 空节」— 被驳回：模板实现期追加区，留空合规；修复=改 spec 属被拒类（盲审 #11）。
- [low→patch] `circuit_breaker.py` docstring「层叠Thread-safe」中英粘连笔误 — 修复：改正文（盲审 #12）。
- [medium→patch]（验证缺口层，pre-verified）组合根 `breaker=_llm_breaker` 注入无接线守护——删掉该行使全 suite 仍绿（生产链路与替身链各测各的）— 修复：扩组合根测试断言 main 装配链中 LlmGenerateFilter 的 breaker 即 `_llm_breaker`（或经 TestClient 走查 skipped 计数）（验证缺口 #1）。
- [low→reject] `test_house_sync.py` 等待循环无超时 — 被驳回：继承自 3.4 既有形状，非本 Story 引入；低概率且修复超出直接更正（验证缺口 #2 Other findings）。

## Verification

**Commands:**
- `uv run pytest tests -q` -- 全部通过（含新增/扩展替身测试）。
- `uv run ruff check src tests` -- 无告警。
- `uv run mypy src` -- strict 无错误。
