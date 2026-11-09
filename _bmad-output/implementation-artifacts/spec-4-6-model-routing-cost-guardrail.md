---
title: 'Story 4.6 模型路由与成本护栏'
type: 'feature'
created: '2026-11-09'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'a33e986489cefbfd1f13ff8ee9780bff785ad045'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 生成链路所有意图都走同一个模型 alias（gpt-4o），打招呼、问地址这类简单意图也烧旗舰模型 token；成本只有 Langfuse 事后报表可看，没有预算阈值和自动收紧手段，成本异常增长时只能人工看账后处理。

**Approach:** 在 LLM 层引入配置化的模型路由（按意图复杂度在 gpt-4o / gpt-4o-mini 间选择）+ token 预算护栏：滑动窗口内累计 token 消耗超阈值即自动收紧路由（全部强制 mini）并触发告警，窗口回落自动解除。模型选择写入 Langfuse trace，单条消息可复盘模型与用量。

**Decisions（人类拍板，2026-11-09）：**
- 路由依据 = 按意图复杂度（Open Question 1 选 A）：信封契约不动，名单内简单意图走 mini，其余走主模型。
- 预算窗口 = 每分钟滑动窗口（Open Question 2 选 A）：滑窗内累计 token ≥ 阈值即收紧，回落即恢复。

## Boundaries & Constraints

**Always:**
- 路由策略配置化：模型 alias、简单意图名单、预算阈值、窗口时长全部经 env 配置、可灰度调整，默认按 FR/AC 值生效。
- 路由只决定「用哪个模型生成」：买家可见语义逐字节不动，grounding 校验、熔断器、风控审核、模板兜底链路行为全部不变。
- intent=None（分类 fail-closed 路径）与名单外意图一律走主模型（质量优先）。
- 预算护栏只统计成功生成回传的 usage（total_tokens），失败请求不计；护栏自身 fail-safe，绝不阻断请求路径。
- 收紧期间所有意图强制 mini；窗口消耗回落到阈值以下自动恢复常规路由。
- 全部埋点（trace 事件、指标、/health 快照、告警日志）复用 4.4/4.5 既有模式，各自 fail-safe。

**Never:**
- 不改 `OpenAiGenerationClient` 的重试/预算语义（4.2/4.5 冻结口径）：客户端保持单模型纯粹，由路由层为每个 alias 各持一个 client 实例。
- 不改信封模型、管道编排器、risk_review、response_assembler、熔断器语义；不动 Epic 5 转人工路径。
- 不做多级替代模型降级链（Epic 4 技术决策明确排除）；不做意图分类器（Epic 2 独立客户端）的路由与预算。
- 不硬编码模型名/价目表；成本复盘依赖 Langfuse 按模型自算（既有 `record_generation` 口径），本 Story 不引入本地计费。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 预期输出 / 行为 | 错误处理 |
|------|------------|----------------|---------|
| 简单意图 | intent ∈ 配置名单，预算正常 | 选 mini alias 生成；trace 记真实模型与 usage | N/A |
| 复杂意图 | intent 不在名单或为 None，预算正常 | 选主模型 alias 生成 | N/A |
| 预算超限 | 滑窗内累计 total_tokens ≥ 阈值 | 后续请求（无论意图）强制 mini；trace 记 forced_mini 收紧事件，ERROR 级 `model_routing_alert` 告警日志 | 护栏判定自身异常吞掉、按常规路由放行处理，绝不阻断生成 |
| 窗口回落 | 滑窗内消耗 < 阈值 | 自动恢复按名单路由；trace 记 recovered 事件 | N/A |
| usage 缺失 | 响应无 usage_metadata（替身） | 按 0 token 计数，预算不受影响 | N/A |
| 配置非法 | 预算/窗口非正、nan/inf、名单含未知意图标签、alias 空串 | 组合根 fail-fast 拒绝启动（错误消息带变量名） | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/llm/client.py` -- 只读：单模型客户端（构造注入 alias、惰性建 ChatOpenAI 并缓存、预算/重试语义冻结）。路由层为每个 alias 各建一个实例，此类不改。
- `src/weiliao_agent/llm/model_router.py` -- 新建：路由表（intent→alias）+ 双 client 调度 + 滑窗 token 预算护栏（clock 可注入、线程安全、构造 fail-fast、判定/收紧/恢复全 fail-safe）。
- `src/weiliao_agent/llm/subgraph.py` -- 接入点：`LlmGenerationSubgraph(client)` 增加可选 `router` 注入；generate 节点 router 路径按 intent 选 alias 生成，`record_generation` 记真实模型（既有 cost={} 口径不动）。
- `src/weiliao_agent/llm/__init__.py` -- 惰性默认子图入口不变（router=None 语义冻结，既有测试不受影响）；导出新符号。
- `src/weiliao_agent/filters/llm_generate.py` -- 只读不改：熔断咨询在子图之前、与路由正交。
- `src/weiliao_agent/observability/tracing.py` -- 仿 `record_llm_breaker_event` 新增 `record_model_routing_event`（outcome: primary/mini/forced_mini/recovered，fail-safe）。
- `src/weiliao_agent/observability/metrics.py` -- 仿 `LlmBreakerMetricsCollector` 新增路由指标收集器（按模型调用数、token 累计、收紧状态、超限次数）+ 模块单例 + reset + 定时日志（收紧态追加 ERROR 级 `model_routing_alert`）。
- `src/weiliao_agent/health/probe.py` -- 仿 `llm_breaker_payload` 新增 `model_routing_payload()` 挂 /health。
- `src/weiliao_agent/main.py` -- 组合根：新 env 解析校验 fail-fast（错误消息带变量名）；构造 router（持两 client + 指标单例）注入子图；`_metrics_log_loop` 增发路由日志。
- `.env.example` -- 新 env 文档条目（仿 4.5 先例）。
- `tests/` -- `test_model_router.py`（新建：fake clock + 假 client 覆盖矩阵全分支）、`test_llm_subgraph.py`（扩：router 路径模型选择）、`test_model_routing_metrics.py`（新建）、`test_health.py`（扩：快照回读）、组合根 env 回读测试（扩 4.4 既有先例）、`test_house_sync.py`（扩：定时日志含路由事件）——全程替身零网络。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/llm/model_router.py` -- 路由表、双 client 调度、滑窗预算护栏（fake clock 可测）-- I/O 矩阵与 AC 1/2。
- [x] `src/weiliao_agent/llm/subgraph.py` + `llm/__init__.py` -- 可选 router 注入与生成节点调度；router=None 既有路径逐字节不变 -- AC 1。
- [x] `src/weiliao_agent/observability/tracing.py` -- `record_model_routing_event` -- AC 3（模型选择入 trace 可复盘）。
- [x] `src/weiliao_agent/observability/metrics.py` + `health/probe.py` + `main.py` -- 指标单例、/health 快照、定时日志与告警、组合根 env 校验接线 -- AC 2/4 与监控告警。
- [x] `.env.example` -- 新 env 文档条目。
- [x] `tests/`（一新建 + 四扩展）-- 替身覆盖矩阵全分支、滑窗（fake clock）、配置 fail-fast、组合根回读断言，无网络。

**Acceptance Criteria:**
- Given 预算正常且 intent 为名单内简单意图，when 生成请求进入子图，then 用 mini alias 生成，trace 的 generation 观测记录真实模型名与 token 用量。
- Given 滑窗内累计 token ≥ 阈值，when 后续生成请求到达，then 全部意图强制 mini，trace 留 forced_mini 收紧事件，ERROR 级 `model_routing_alert` 告警日志发射，买家可见应答语义不变。
- Given 滑窗消耗回落到阈值以下，when 新请求到达，then 自动恢复按名单路由，trace 留 recovered 事件。
- Given 预算/窗口配置非法（非正、nan/inf、名单含未知意图、alias 空串），when 组合根装配，then fail-fast 拒绝启动（错误消息带变量名）；合法值经子进程导入回读落到注入 router。
- Given 服务运行中，when curl /health 或定时指标日志发射，then 就绪响应含 `model_routing` 快照（各模型调用数/token/收紧状态），收紧态定时日志追加 ERROR 级告警。

## Implementation Notes

## Design Notes

- 滑窗实现：router 内维护 (timestamp, tokens) 双端队列，判定与计数时剔除窗口外条目；clock 可注入（仿 4.5 熔断器 fake clock 测试先例），生产用 `time.monotonic`。
- 收紧/恢复口径：≥阈值收紧、<阈值恢复，无额外迟滞带（避免配置面膨胀，抖动由窗口平滑天然抑制）。
- 路由在子图节点内完成（LLM 编排骨架允许位于子图内），过滤器与管道零改动；熔断仍居过滤器层、模型无关、不受影响。
- 预算只统计成功回传的 usage（`total_tokens` 缺键容忍为 0），失败请求不计入。

## Spec Change Log

## Review Triage Log

2026-11-09 三方审查（盲审 Ptolemy 11 条 / 边界 Herschel 6 条 / 验证缺口 Kuhn 1+1 条），逐条核实定夺：

| # | 发现 | 裁定 | 依据 |
| --- | --- | --- | --- |
| B1 | `.env.example` 四个新变量空值，整体拷贝即启动失败 | defer | 既有惯例：4.5 的 BREAKER 变量同样留空（.env.example:27/33），非本 Story 引入 |
| B2/E2 | `WEILIAO_LLM_ROUTING_SIMPLE_INTENTS` 显式置空解析为空名单被静默接受、mini 路由悄悄失效 | medium → patch | 核实 main.py 解析逻辑：空条目全被过滤、空列表通过 router 校验；静默失效违背本仓库「配置错装配期必须暴露」口径 |
| B3 | main.py 缺省名单硬编码 "GREETING,LOCATION"，与 DEFAULT_SIMPLE_INTENTS 可漂移 | low → patch | 直接修正（缺省值从常量派生），无公共面变化 |
| B4/E3 | mini alias 只查空白未 strip，带空白 alias 通过校验直达 API | low → patch | 直接修正（解析时 strip 一次） |
| B5/E4 | mini alias 与主模型 alias 相同时路由整体静默失效、无任何信号 | low → patch | 真实但非本 Story 日常路径；最小修复为装配期 ERROR 日志（不新增 spec 之外的 fail-fast 行为） |
| B6 | 指标快照缺 window_tokens 与 budget/window 配置，事故时看不到距阈值多近 | low → 拒绝 | 修复需给 collector/快照/health/日志载荷加字段，超出 AC 第 5 条已满足的快照口径，属加面而非直接修正 |
| B7 | 路由事件在生成成功前记录，失败请求留幽灵事件 | false | 核实 _generate_via_router：select 之后才 generate，事件记录与 account 均在 generate 成功之后，不存在幽灵事件 |
| B8 | 无测试断言「生成失败不消耗预算」，account 前移回归会静默双计 | medium → patch | 核实 tests/：子图级失败路径零覆盖；spec Boundaries 明令「失败请求不计」，补守护测试 |
| B9 | client 与 router 独立注入，fallback 可能悄悄用错模型 | low → 拒绝 | 仅为假想未来误用（当前组合根传主模型 client）；修复需构造期断言或改签名，属加守卫 |
| B10 | record_call 接受任意 str，拼写错误 outcome 静默计入 mini | low → 拒绝 | 单调用点传 router 自有常量，运行期类型标注不防任何实际场景，日常遇不到 |
| B11 | 无 runbook/README 文档更新，告警含义与调参口径只在源码里 | defer | 既有惯例：4.4/4.5 新增 env 与告警同样只写 .env.example（README 无 BREAKER 条目），非本 Story 引入 |
| H1/E6 | 收紧→恢复在 select() 提交但 recovered 事件仅在生成成功后补记，失败请求状态已恢复却无事件 | low → 拒绝 | 窗口回落恢复本身是真实状态、与单次请求成败无关（spec：回落即恢复）；事件补记挪前会制造 B7 式幽灵事件，两害相权维持现状 |
| H5 | test_health_ready_returns_200 重置单例无 finally，断言失败泄漏状态 | false | 核实该用例：重置后只读快照（is_ready/快照读取不写单例），中途失败无状态可漏 |
| K1 | 组合根 router=_model_router 注入无接线守护测试，删掉注入全库仍绿（预验证缺口） | medium → patch | 采纳：仿 test_breaker_wired_into_llm_generate_filter（test_llm_breaker.py:369）补子进程接线守护 |
| K2 | record_model_routing_event 本体无直测（现有仅 spy 调用方传参） | low → patch | 测试-only 直接补充，仿 test_tracing.py _FakeLangfuse 惯例 |

## Verification

**Commands:**
- `uv run pytest tests -q` -- 全部通过（含新增/扩展替身测试）。
- `uv run ruff check src tests` -- 无告警。
- `uv run mypy src` -- strict 无错误。
