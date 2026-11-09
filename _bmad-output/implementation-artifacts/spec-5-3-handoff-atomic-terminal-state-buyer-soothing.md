---
title: 'Story 5.3 转人工原子终态与买家安抚通知'
type: 'feature'
created: '2026-11-09'
status: 'done'
baseline_commit: '7c863fe21aeb0157e117cc82e84e8f152d1e5170'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Story 5.1/5.2 之后转人工仍不是终态：买家在转接后再发消息，会重新走完整 AI 管道并收到 AI 回复（人机混答，直接违反本 Story AC）；红线消息还会重复触发判定、重复发送转交指令（5.2 评审 #18 遗留）。

**Approach:** 在响应路径入口加终态闸门：``begin_turn`` 快照已带 ``handoff_status``（5.1 落盘、当前无人消费），适配器在调用编排器前检查——已转接会话的消息不进入管道，直接回固定安抚文案，并记可观测事件。触发轮本身行为逐字节不变。

**Decisions（人类拍板，2026-11-09）：**
- 转接后买家再发消息的应答文案 = 重复同一句 ``HANDOFF_REPLY_TEXT``（Q1 选 A）：实现最简、全链路唯一文案口径，与 Epic 固定文案约束一致。

## Boundaries & Constraints

**Always:**
- 闸门在编排器之前：已转接 session 的消息 intent_router/cache/llm/risk/assembler 全零执行——不二次触发、不再发第二条转交指令。
- 闸门判定权威 = ``SessionManager.begin_turn`` 快照的 ``handoff_status``（AD-4 单写者读口径），零新增 Redis 往返。
- 闸门应答 ``degraded=False``（设计内终态分支，非故障降级）；trace_id 语义与管道一致（缺失即生成回填）。
- 闸门路径记 Langfuse trace 事件 + WARNING 级日志（可审计），记录本身 fail-safe。
- 触发轮行为冻结不变：严格先指令后通知（AD-7）、出口文案 ``HANDOFF_REPLY_TEXT``。

**Never:**
- 不改硬触发判定、信封字段语义、``HANDOFF_REPLY_TEXT``、发送器（5.1/5.2 冻结）。
- 不做 AI 身份标识（Story 5.4）；不动模板兜底库；闸门路径绝不回模板兜底文案。
- 禁止闸门判定失败时放行进入 AI 路径（fail-closed：拿不准就回既有兜底，绝不人机混答）。
- 不新增 session 字段、不改 Lua 脚本（``handoff_status`` 字段 5.1 已就绪）。
- 不做经纪人接管后的状态解除——session TTL 到期自然结束，无该需求。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 转接后再消息 | handoff_status="triggered"，买家再发文本 | 不进入编排器，回 ``HANDOFF_REPLY_TEXT``（Q1-A），记 trace 事件，不落转写 | 事件记录失败吞掉，应答不变 |
| 触发轮回归 | 首条消息即命中硬触发 | 5.1/5.2 行为逐字节不变：先指令后通知 + HANDOFF_REPLY_TEXT | N/A |
| Redis begin_turn 失败 | 任意会话消息 | 既有 fail-closed 模板兜底（无法得知终态，维持既有口径） | 既有语义不变 |
| 无会话管理器 | 测试替身装配 | 无闸门（无权威状态），走原管道路径 | N/A |
| 新会话首消息 | handoff_status="none" | 正常管道 | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/adapters/http.py:151` -- ``post_message``：``begin_turn`` 挂点（L159）后、``orchestrator_.run``（L171）前即闸门点；``snapshot.handoff_status`` 已返回但当前无人消费；``_record_transcript``（5.2 追加）为落盘先例，闸门轮**不落**转写（无 agent 回复，逐对语义）。
- `src/weiliao_agent/session/manager.py:149` -- ``TurnSnapshot.handoff_status``（5.1 Lua 落盘，"none"/"triggered"）——零改动直接消费，不新增原语。
- `src/weiliao_agent/pipeline.py:60` -- ``HANDOFF_REPLY_TEXT`` 常量与 `_handoff_result`（L210）触发轮出口——本 Story 冻结不动，闸门直接复用常量保证文案逐字节一致。
- `src/weiliao_agent/observability/tracing.py:427` -- ``record_handoff_assessment``（事件 + 分级 + fail-safe）为新闸门 trace helper 的封装先例。
- `tests/` -- 扁平 ``test_<模块>.py``；fakeredis 共享 fixture（conftest）；5.2 ``test_handoff_composition_root.py`` 装配测试先例；``test_pipeline.py`` 触发轮回归用例在本 Story 必须保持全绿。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/observability/tracing.py` -- 新增终态闸门 trace helper（handoff.already_handed_off 事件 + 分级 + fail-safe no-op 模式）-- 可观测锚点。
- [x] `src/weiliao_agent/adapters/http.py` -- ``post_message`` 加闸门：``snapshot.handoff_status == "triggered"`` 时短路——不调编排器、回 ``HANDOFF_REPLY_TEXT``（Q1-A）、记事件、不落转写；trace_id 缺失即生成 -- 原子终态核心。
- [x] `tests/test_handoff_terminal_state.py`（命名可斟酌）-- 覆盖 I/O 矩阵全场景（转接后再消息/触发轮回归/Redis 失败/无 session manager）+ 装配级测试（闸门在真实编排器前拦截，替身编排器可断言零调用）-- 回归网。

**Acceptance Criteria:**
- Given 会话已转人工（handoff_status="triggered"）且未过期，when 买家再发任何消息，then 该消息不进入编排器（全过滤器零执行、零二次指令发送），买家收到 ``HANDOFF_REPLY_TEXT``（Q1-A），无 AI 回复、无模板兜底文案。
- Given 闸门短路，when 应答返回，then ``degraded=False``、携带 trace_id、Langfuse 留有可审计事件。
- Given 触发轮本身，when 硬触发命中，then 5.1/5.2 行为逐字节不变（先指令后通知 + HANDOFF_REPLY_TEXT + 转写落盘）。
- Given Redis ``begin_turn`` 失败，when 任一会话消息到达，then 既有 fail-closed 兜底语义不变（非 5xx）。

## Implementation Notes

## Spec Change Log

## Review Triage Log

- 闸门轮不落转写会在 turn 序号中留洞，可能破坏假设连续轮序的 `read_transcript` 消费方 — `false` — 转写是 Redis list 尾部截取（RPUSH/LTRIM，manager.py L41），消费方（转交载荷取最近 3 轮）按列表条目读取天然容忍缺轮；无消费方按键序索引。
- 已转接会话每条消息都发 WARNING 事件+日志，无速率限制/去重，可观测量无界 — `false`（拒绝）— 事件量等于买家消息量，服务本就须逐条应答；速率限制/去重属新增复杂度，日常场景碰不到的收益。
- `test_handoff_terminal_state.py` 从 `test_handoff_command` 私有导入 `_FakeTransport`，测试文件耦合 — `low` — 真实但仅测试卫生问题；最小修复 = 内联替身，直接修正，patch。
- 闸门无指标计数器，运营无法对闸门流量建看板 — `false`（拒绝）— spec 可观测锚点定义为 trace 事件 + WARNING 日志，计数器是 spec 之外的新表面；日志量本身可告警。
- 闸门 WARNING 日志缺 `turn` 字段（事件 metadata 有）— `low` — 一行日志参数的直接修正，patch。
- 未知 `handoff_status` 值（非 none/triggered）等值比较静默落入正常管道 — `medium` — 已核实：snapshot 字段为裸 `str` 无值集校验（manager.py L214），未知值进编排器可能人机混答并二次触发，违反 spec Never「拿不准就回既有兜底」的 fail-closed 口径；可经 fakeredis 直写损坏值复现；最小修复 = 未知值分支回 `_fallback_result`，直接修正无新表面，patch。
- edge-case hunter 同根发现：未知 `handoff_status` 应 fail-closed 回兜底而非进管道 — `medium` — 与上一条同一根因，同组 patch。
- `test_gate_backfills_trace_id_when_missing` 未断言 `run_calls==0`/reply — `low` — 补两个断言的直接修正，与下一条同组 patch。
- `test_begin_turn_redis_failure_keeps_existing_fallback` 未断言 `run_calls==0` — `low` — 补断言的直接修正，与上一条同组 patch。
- 闸门路径内联构造裸 `PipelineResult`，与编排器结果形状重复 — `false`（拒绝）— PipelineResult 形态不变时无实际危害；耦合编排器私有构造法是设计变更而非直接修正，Schema 演进时再统一。
- 无面向 API 消费方的闸门行为文档 — `false`（拒绝）— 应答与正常回复不可区分是 Q1-A 已拍板设计；spec 即文档，本 Story 无 docs 交付物。

## Design Notes

- 闸门放适配器而非编排器内：判定数据（``begin_turn`` 快照）天然在适配器手中，编排器签名与触发轮路径零改动；「响应路径关闭」的语义由入口拦截实现，管道内部保持纯编排。
- 天然收口 5.2 评审 #18（同 session 连续红线消息产生 N 条指令）：终态会话消息不进入 intent_router，不可能二次触发。
- 闸门轮不落转写：转写是 buyer/agent 逐对原文（5.2 契约），无 agent 回复的轮次混入会污染工作台上下文。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警
- `uv run mypy src` -- expected: 零告警（仓库 mypy strict 口径，仅扫 src）
