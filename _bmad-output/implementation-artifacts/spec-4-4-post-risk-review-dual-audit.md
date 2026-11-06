---
title: 'Story 4.4 后置风控双审核'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '193889f3a0006382d1f5a83ea82b62dacc45785a'
context:
  - '_bmad-output/implementation-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 4.3 起管道里已有通过 grounding 校验的 LLM 草稿（信封 `generated_reply`），但 `risk_review` 仍是占位透传：草稿没有任何机制送审，买家可见应答 100% 还是模板兜底，LLM 链路的价值无法兑现；同时「审核通过率 ≥95%」只停留在压测要求文字上，线上没有通过率指标与跌破阈值告警，审核质量滑坡无人能感知。

**Approach:** 把 `risk_review` 占位换成真实后置审核：消费信封草稿，调风控中台审核客户端（默认 500ms 超时视为不通过，300–800ms 可调），通过则草稿经编排器最终组装点下发（携带 AI 身份前缀），不通过/超时/异常一律清草稿、回模板兜底，全路径 fail-safe 不向上抛；新增审核结果 trace 事件、通过率指标收集器（/health 快照 + 定时结构化日志 + 跌破阈值告警）；默认链与组合根同步接线。

**双审核口径（决策，2026-11-06）：**「双审核」= 4.3 确定性 grounding 校验（第一道，产出草稿）+ 本 Story 风控中台审核（第二道，放行草稿）。PRD 标题与正文的矛盾按架构脊柱落地：只做后置审核（AD-8），不做生成前审核。

**审核协议（决策 Checkpoint 1，2026-11-06）：** 风控中台真实接口文档暂缺，采用自定义最小 JSON 协议：POST `{text, intent, trace_id, house_id}` → 响应 `{"pass": bool, "reason": str?}`；客户端可注入替身，真实中台联调期再写适配层。不阻塞本 Story 与 4.5。

## Boundaries & Constraints

**Always:**
- 100% 的 LLM 草稿（`generated_reply` 非 None）须经中台审核通过才可达买家；审核不通过/超时/异常/未配置 endpoint → 草稿作废（信封清回 None）+ 模板兜底。审核拒绝属设计内行为：`degraded` 保持 False（与 4.3 影子模式口径一致），可观测性走 trace 事件与指标。
- `generated_reply` 为 None 的信封（红线意图/非文本/生成失败/校验未过）：risk_review 原样透传，不调中台、不计通过率分母，买家应答仍是模板兜底。
- 审核请求携带 text + intent + trace_id + house_id 元数据；超时默认 500ms，``WEILIAO_RISK_REVIEW_TIMEOUT_MS`` 可调 300–800（越界 fail-fast 带变量名）。未配置 ``WEILIAO_RISK_REVIEW_ENDPOINT`` 时 fail-closed：任何草稿视为不通过，绝不默认放行；生产必须显式配置。
- 审核客户端与过滤器全路径 fail-safe：网络错误/非 200/响应缺字段一律归一为「不通过」，绝不向上抛影响编排器；超时的执行线程不傻等，有界线程池承接（同 OpenAiGenerationClient 口径）。
- 买家可见文本 = ``templates.AI_DISCLOSURE_PREFIX`` + 审核通过草稿，组装发生在编排器最终返回点；通过审核的内容到达该点后不得再被改写。
- 过滤器顺序固定：intent_router → cache_lookup → llm_generate → risk_review → response_assembler。
- trace_id 贯穿：审核结果（passed/rejected/timeout/error + latency_ms + reason）记 Langfuse 事件，记录本身 fail-safe。
- 指标口径：分母 = 实际审核调用数（含超时/错误），分子 = passed；超时与错误计入失败。快照进 /health（``risk_review_metrics`` 键）+ lifespan 定时结构化日志（共用 ``WEILIAO_METRICS_LOG_SECONDS`` 循环）。样本 <20 不告警防抖动；阈值 ``WEILIAO_RISK_REVIEW_PASS_RATE_THRESHOLD`` 默认 0.95（0–1 校验，fail-fast），跌破时发射 ERROR 级告警日志（``event=risk_review_alert``）。

**Never:**
- 不做 OpenAI 故障降级切换与步进恢复（4.5）、模型路由与成本护栏（4.6）、转人工（Epic 5）；`response_assembler` 占位不动（完整组装不归本 Story，仅编排器最终返回点消费已过审草稿）。
- 不改 grounding 校验、llm_generate、模板库文案、信封既有字段语义与 2.5s 预算/trace 回填口径；不引入新的第三方 HTTP 依赖（审核客户端用标准库 urllib）。
- 不让未审核内容触达买家可见路径；不做本地敏感词前置层（OQ-2 挂账，归 deferred）；不交付压测脚本（≥95% 为上线前真实中台压测基线，本 Story 交付指标口径与告警机制）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 草稿就绪审核通过 | generated_reply 非空 + 中台返回 pass | 草稿保留信封，买家 reply = AI 前缀+草稿，degraded=False | trace 记 passed（含 latency_ms） |
| 审核拒绝 | 中台返回 pass=false | 草稿清 None，模板兜底，degraded=False | trace 记 rejected + reason |
| 审核超时 | 中台 500ms 未返回 | 同上，归因 timeout | 执行线程不傻等，归一为不通过 |
| 审核异常 | 网络错/非 200/坏响应/endpoint 未配置 | 同上，归因 error | 一律归一不通过，不向上抛 |
| 无草稿 | generated_reply=None | 原样透传，不调中台，模板兜底 | 无 |
| 慢审核拖预算 | 审核耗时逼近剩余跳预算 | 过滤器内超时先行归一（同 4.3 有界化先例），不触发编排器 slow_filter 降级 | timeout 行覆盖 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/filters/risk_review.py` -- 占位过滤器（透传），本 Story 包化为 `filters/risk_review/`（`__init__.py` 再导出 `RiskReviewFilter`，pipeline/main/tests 既有导入路径不变）；过滤器本体与审核客户端（`client.py`，urllib + 有界线程池 + 超时守卫，仿 `llm/client.py` 结构）同包。
- `src/weiliao_agent/pipeline.py` -- `_run_chain` 最终返回点：reply 改为「`generated_reply` 非 None → `AI_DISCLOSURE_PREFIX + 草稿`，否则 `render_fallback(envelope.intent)`」；预算/degraded/trace 语义不动。
- `src/weiliao_agent/models/envelope.py` -- 只读消费 `generated_reply`；不改字段。
- `src/weiliao_agent/filters/templates.py` -- 复用 `AI_DISCLOSURE_PREFIX` 与 `render_fallback`（只读）。
- `src/weiliao_agent/observability/tracing.py` -- 新增 `record_risk_review_event`（outcome: passed/rejected/timeout/error，仿 `record_llm_generate_event` 形状，fail-safe）。
- `src/weiliao_agent/observability/metrics.py` -- 新增 `RiskReviewMetricsCollector`（线程安全，仿 `CacheMetricsCollector`）+ `emit_risk_review_metrics_log`（含阈值告警判定）。
- `src/weiliao_agent/health/probe.py` -- `risk_review_metrics_payload()` 挂 /health（3.4 附快照先例）。
- `src/weiliao_agent/main.py` -- 组合根装配 RiskReviewFilter（注入客户端 + env 配置 fail-fast）；`_metrics_log_loop` 增发审核指标日志。
- `tests/test_risk_review_filter.py`、`tests/test_risk_review_client.py`、`tests/test_risk_review_metrics.py` -- 新建，替身无网络覆盖矩阵全分支；`test_pipeline.py` 默认链组装断言随最终组装点语义更新。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/filters/risk_review/`（包化 + `client.py`）-- HTTP JSON 审核客户端（urllib、超时守卫、错误归一、可注入 transport）+ 真实过滤器（消费草稿、通过保留/不通过清除、trace+metrics 埋点、全 fail-safe）-- AC 第 1/2 条与 I/O 矩阵。
- [x] `src/weiliao_agent/pipeline.py` -- 最终返回点组装已过审草稿（AI 前缀）-- AC 第 1 条。
- [x] `src/weiliao_agent/observability/tracing.py` -- `record_risk_review_event` -- AC 第 3 条（审核结果记录 trace）。
- [x] `src/weiliao_agent/observability/metrics.py` + `health/probe.py` + `main.py` -- 通过率指标、/health 快照、定时日志、阈值告警、组合根接线与 env 校验 -- AC 第 3 条（持续监控告警）。
- [x] `tests/`（四个新建 + 受影响既有测试更新）-- 替身覆盖矩阵全分支，无网络。

**Acceptance Criteria:**
- Given 信封带 `generated_reply` 且中台返回通过，when 跑完整管道，then 买家可见 reply = AI 前缀+草稿，`degraded=False`，trace 留有审核 passed 事件（含 latency_ms）。
- Given 中台返回不通过/超时（>500ms）/异常或未配置 endpoint，when 跑管道，then 买家收到模板兜底，`degraded=False`，trace 留有对应失败事件，草稿不触达买家。
- Given `generated_reply=None`（红线意图/非文本/生成失败），when risk_review 执行，then 不调中台、透传信封、买家应答为模板兜底。
- Given 累计审核样本 ≥20 且通过率跌破阈值（默认 0.95），when 定时发射指标日志，then 触发 ERROR 级告警日志（`event=risk_review_alert`）；且 /health 就绪响应含 `risk_review_metrics` 快照。
- Given 审核超时配置越界（如 900ms）或阈值非法，when 组合根装配，then fail-fast 拒绝启动（错误消息带变量名）。

## Implementation Notes

## Spec Change Log

## Review Triage Log

2026-11-06 评审（盲审 13 + 边界 4 + 验证缺口 3，共 20 条）：

- [false] 空串草稿（`generated_reply == ""`）直达买家只带前缀 — 被驳回：`llm/grounding.py` 的 `verify_grounding` 首条即拒空/空白输出（`empty_output`），且 llm_generate 是唯一写入者且仅写通过校验的文本，`generated_reply` 不可能为空串。
- [false] 审核客户端 ThreadPoolExecutor 无 close/shutdown — 被驳回：与 spec 指定先例 `llm/client.py`（OpenAiGenerationClient）结构一致（spec 明文「仿 llm/client.py 结构」「同 OpenAiGenerationClient 口径」），进程生命周期单例无线程泄漏；pipeline 的 shutdown 是每跑一次的执行器，生命周期不同不可类比。
- [false] 执行队列无界、超时风暴下任务堆积 — 被驳回：与 spec 指定的 OpenAiGenerationClient 同口径；单次调用至多挂起一个有 urllib 超时上界（≤800ms）的任务，八工位池按 8/timeout 速率消化，属设计内行为。
- [low→patch] /health 合并单 try 块：risk_review 快照抛错时 cache 快照被连带丢弃（改动引入的健壮性回退）— 修复：两个快照各包各的 fail-safe。
- [low→patch] `RiskReviewOutcome` 在 client.py 与 metrics.py 重复定义，仅靠注释同步有漂移风险 — 修复：metrics 从 client 导入同一 Literal（client 无内部依赖，不成环）。
- [false] `record()` 裸 else 把未知 outcome 计入 error — 被驳回：全部产出点（client 四个固定分支 + 测试替身）均构造类型内值，运行期不可能出现第五值；修复（hot path 加 raise）反而侵蚀过滤器 fail-safe 语义。
- [false] endpoint 未在启动时校验 URL 格式 — 被驳回：冻结区决策明确 endpoint 走运行期 fail-closed（而非启动 fail-fast），配置笔误的方向是安全失效且经 error 归因 + 通过率指标可见，属设计内。
- [false] 测试机继承环境变量污染组合根子进程测试 — 部分驳回：三个测试对受测变量均显式覆盖（override 优先），唯一真实暴露是「继承非法 TIMEOUT 使 threshold 测试错因报错」；按 low→patch 顺手修（_scrub 两个变量），不单列缺陷。
- [low→reject] test_health 只有零态快照断言、无非零端到端回读 — 驳回：零态测试已走通 collector→probe→http 合并→JSON 全路径，非零值不改变任何控制流，新测试不覆盖新行为。
- [low→patch] `test_alert_respects_custom_threshold` 内注释残句「0.6 >= 0.5 默认阈值，但 < 自定义 0.5 之上场景取反：」与下文代码矛盾 — 修复：改写为准确注释。
- [low→patch] `test_slow_transport_times_out_without_waiting` 私有属性赋值（noqa SLF001）且后台留 2s 真睡线程拖慢 pytest 进程退出 — 修复：slow transport 经构造函数注入并缩短睡眠时长。
- [low→patch] filter.py 惰性默认客户端 docstring 称「首次审核才按环境变量构造」但仅 endpoint 读环境变量，timeout 恒 500 — 修复：改正 docstring 表述（组合根负责 env 校验与注入，spec 设计内）。
- [low→patch] 三个新环境变量未入 `.env.example` — 修复：补文档条目（仓库已有 WEILIAO_ 变量文档先例）。
- [low→patch] `_get_default_client` 惰性初始化无双检锁，并发首用可构造重复客户端 — 修复：按 `llm/__init__.py` 既有双检锁先例加锁。
- [medium→patch]（验证缺口层，pre-verified）组合根只测 fail-fast 方向，未断言合法 env 值真的落到注入客户端（timeout→`_risk_review_client`）与指标单例（threshold→collector）— 修复：子进程导入后回读断言两个值。
- [medium→patch]（验证缺口层，pre-verified）lifespan 定时循环新增的 `emit_risk_review_metrics_log()` 调用无观察测试，删除该行使全 suite 仍绿 — 修复：扩 `test_house_sync.py` 既有循环测试，断言出现 `risk_review_metrics` 事件记录。

## Design Notes

- 「双审核」按两道理解：grounding（4.3，确定性、产草稿）+ 中台审核（4.4，放行草稿）。PRD 标题歧义已按脊柱 AD-8 选边，仅后置。
- 审核拒绝/超时 `degraded=False`：买家收到模板兜底是设计内行为而非服务故障，与 4.3「生成链路故障对买家零感知」同口径；质量信号走 trace 事件 + 通过率指标 + 告警，不靠响应字段表达。
- 未配置 endpoint 选择运行期 fail-closed（而非启动 fail-fast）：本地开发/测试零中台可跑，且任何环境不配置中台即不放行 LLM 内容，安全方向内生于默认。
- 编排器最终返回点替代 response_assembler 占位消费草稿：占位过滤器本 Story 不动，最小改动让已过审内容可达买家；完整组装器留给后续 Epic。

## Verification

**Commands:**
- `uv run pytest tests -q` -- 全部通过（含新增替身测试与更新的默认链测试）。
- `uv run ruff check src tests` -- 无告警。
- `uv run mypy src` -- strict 无错误。
