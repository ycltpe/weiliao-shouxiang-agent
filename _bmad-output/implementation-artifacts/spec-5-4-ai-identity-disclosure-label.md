---
title: 'Story 5.4 AI 身份披露标识'
type: 'feature'
created: '2026-11-09'
status: 'done'
baseline_commit: 'fdf56e7'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** AD-10 要求买家可见文本 100% 携带"AI 助手"标识（FR-9），但当前注入点分散在编排器最终组装点与 ``render_fallback`` 内部两处，转人工出口文案是否携标被 5.1 显式推迟到本 Story 拍板，且标识的展示/切换缺乏可审计的 Langfuse trace 锚点。

**Approach:** 把 ``response_assembler`` 占位换成真实组装器，作为 AI 标识的唯一注入载体（过审草稿加前缀、无草稿走模板兜底），所有应答出口写入 ``ai.disclosure`` trace 事件；转人工文案携标口径由 Open Question 拍板后全链路口径统一。

**Decisions:**
- Q1（2026-11-09 拍板，选 B）：转人工相关文案（触发轮 ``HANDOFF_REPLY_TEXT`` 与 5.3 终态闸门同文案）**携带** AI 标识——按"买家可见文本 100% 携带"的最严合规口径执行；``HANDOFF_REPLY_TEXT`` 常量本身保持纯文案不变，前缀由出口处统一注入（编排器出口与 http 闸门两处逐字节一致）；"转人工后标识消失"由 5.3 闸门拦截保证。

## Boundaries & Constraints

**Always:** 前缀常量唯一来源 ``templates.AI_DISCLOSURE_PREFIX``（``"【AI 助手】"``），文案与格式不改；买家可见文本 100% 携标（含转人工文案，Q1 决策 B），不漏标、不双标；一切 trace 记录 fail-safe（未配置 Langfuse 时 no-op，绝不影响应答）；通过 risk_review 的草稿除加前缀外不得再被改写（4.4 冻结语义）。

**Never:** 不改 ``PipelineResult`` 响应契约——不新增结构化身份字段（工作台展示切换由 Kafka 转交指令驱动，属经纪人工作台 UI 职责，本服务不新增 UI、不动任何前端）；不重写 ``render_fallback`` 既有行为与全部既有调用方口径；不动 5.1 硬触发短路语义、5.3 终态闸门语义与「严格先指令后通知」顺序（唯一允许的变化是 Q1 拍板的前缀字节）。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 期望输出 / 行为 | 错误处理 |
|------|------------|----------------|----------|
| 正常链-草稿过审 | generated_reply 非 None（risk_review 已放行） | 组装器产出 `前缀+草稿`，逐字节下发 | N/A |
| 正常链-无草稿 | 缓存/LLM 未产出或审核未过 | 组装器走 ``render_fallback``（自带前缀） | N/A |
| 草稿已带前缀 | LLM 草稿自身以 `【AI 助手】` 开头 | 跳过加前缀，防双标 | N/A |
| 降级路径 | 中途过滤器异常/超时 | 跳过组装器，``render_fallback`` 兜底（自带前缀）+ disclosed=True 事件 | 事件 fail-safe |
| 硬触发轮 | intent_router 判定 triggered | 走 ``HANDOFF_REPLY_TEXT`` 出口并加前缀（Q1 决策 B），编排器与闸门两处逐字节一致 | disclosed=True 事件 |
| 终态闸门（5.3） | 已转接会话再发消息 | 闸门回 ``HANDOFF_REPLY_TEXT``，携标口径与触发轮逐字节一致 | N/A |
| fail-closed 出口 | 请求校验失败 / Redis 失败 / 未知 handoff_status | ``_fallback_result`` 模板兜底（自带前缀）+ disclosed=True | N/A |
| Langfuse 未配置 | 任意路径 | 应答与降级语义逐字节不变 | 事件 no-op |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/filters/response_assembler.py` -- 当前为透传占位（4.1 起）；本 Story 核心改造点，实现真实组装器。
- `src/weiliao_agent/models/envelope.py:86` -- ``generated_reply`` 草稿挂点；本 Story 在其旁新增 ``assembled_reply: str | None = None`` 组装产出挂点（含字段口径注释），信封其余字段不动。
- `src/weiliao_agent/pipeline.py:48-51` -- ``HANDOFF_REPLY_TEXT`` 常量与「不加 AI 披露前缀……归 Story 5.4」的推迟注释（Q1 拍板点）；L195-211 最终组装点：目前内联 `前缀+草稿` / ``render_fallback`` 二选一，本 Story 改为消费 ``envelope.assembled_reply`` 并防御性回退 ``render_fallback``。
- `src/weiliao_agent/filters/templates.py:13,117` -- ``AI_DISCLOSURE_PREFIX`` 常量与 ``render_fallback`` 内部前缀（只读复用，不重写其行为）。
- `src/weiliao_agent/observability/tracing.py:252` -- ``record_delivery``（pipeline.delivery 事件，delivered_text 已含前缀文本）；``record_already_handed_off``（5.3）为新事件 helper ``record_ai_disclosure`` 的封装先例（事件 + metadata + fail-safe no-op）。
- `src/weiliao_agent/adapters/http.py:61-71,182-215` -- ``_fallback_result``（自带前缀）与 5.3 终态闸门出口（Q1 拍板点 + disclosed 事件挂点）。
- `tests/` -- ``test_pipeline.py`` 多处锁定前缀行为（L80-160 startswith、L225 精确等式）；``test_handoff_terminal_state.py`` 锁定闸门文案逐字节；``test_templates.py`` 锁定 ``render_fallback`` 前缀；``test_tracing.py`` 事件断言先例。新增 ``test_response_assembler.py``。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/models/envelope.py` -- 新增 `assembled_reply: str | None = None` 字段（组装产出挂点 + 口径注释，``assembled_reply`` 为组装器权威产出、编排器只读消费）-- 组装器与编排器间的回传通道。
- [x] `src/weiliao_agent/filters/response_assembler.py` -- 占位换真实组装器：``generated_reply`` 非 None 时加前缀（已带前缀则跳过防双标），否则 ``render_fallback(envelope.intent)``；结果写入 ``envelope.assembled_reply``；纯同步无副作用 -- 标识唯一注入载体（Epic 技术决策）。
- [x] `src/weiliao_agent/observability/tracing.py` -- 新增 ``record_ai_disclosure(trace_id, *, disclosed)``：``ai.disclosure`` 事件 + metadata（disclosed、label=前缀常量），封装/分级/fail-safe 口径同 ``record_already_handed_off`` -- 可观测锚点。
- [x] `src/weiliao_agent/pipeline.py` -- 最终返回点改消费 ``envelope.assembled_reply``（None 防御回退 ``render_fallback``）；handoff 出口 ``HANDOFF_REPLY_TEXT`` 加前缀（Q1 决策 B，常量保持纯文案）；正常/降级/handoff 三出口各调 ``record_ai_disclosure`` -- 收口统一注入。
- [x] `src/weiliao_agent/adapters/http.py` -- 终态闸门回 ``HANDOFF_REPLY_TEXT`` 加前缀（Q1 决策 B，与编排器出口逐字节一致），``_fallback_result`` 出口保持自带前缀；两处均调 ``record_ai_disclosure`` -- 链外出口不遗漏。
- [x] `tests/test_response_assembler.py`（新）+ 更新 `test_pipeline.py` / `test_handoff_terminal_state.py` / `test_tracing.py` -- I/O 矩阵全场景（双标防御/无草稿/降级/handoff/闸门/各 fail-closed 出口）+ 事件断言 -- 回归网。

**Acceptance Criteria:**
- Given 正常链且草稿过审，when 应答下发，then ``reply`` 逐字节等于 `前缀+草稿`，trace 留有 ``ai.disclosure``（disclosed=True）事件。
- Given 全部应答出口（正常/降级/硬触发/闸门/校验失败/Redis 失败），when 任一应答产生，then 携标口径与 Q1 答案 100% 一致——无漏标、无双标，闸门与触发轮文案逐字节一致。
- Given 会话已转人工，when 买家再发消息，then 服务侧零 AI 应答、零 AI 标识下发（5.3 语义逐字节不变，除 Q1 拍板的前缀），工作台切换经纪人姓名/头像无残留。
- Given Langfuse 未配置或事件记录抛异常，when 任一路径执行，then 应答文本、``degraded``、trace_id 语义逐字节不变。
- Given 5.1 触发轮与 5.2 先指令后通知回归，when 跑全量测试，then 全绿（唯一允许的差异是 Q1 拍板的前缀字节）。

## Implementation Notes

- 组装器经 `model_copy` 产出新信封（输入不改写，4.4 冻结语义）；编排器最终返回点只读消费 `assembled_reply`，`None` 时防御回退 `render_fallback`（该分支由 `test_normal_exit_records_ai_disclosure` 的替身链隐式覆盖：passthrough stub 不写挂点，正常出口走回退路径并断言事件与 reply 携标）。
- `record_ai_disclosure` 落 DEFAULT 级（常规合规锚点，同 `record_delivery` 惯例）；`handoff.already_handed_off` 的 WARNING 级是「设计内终态分支」语义，不适用于本事件。
- fail-closed 出口：客户端携带可挂 trace_id（32-hex）时事件正常落（Redis 失败、未知 handoff_status 两路有测试断言）；无有效 trace_id 的校验失败出口按 1.5 起 `_recordable` 惯例静默跳过——评审已 defer 待人拍板（见 deferred-work.md）。

## Spec Change Log

## Review Triage Log

评审轮 1（2026-11-09，三层：盲审/边界/验证缺口，20 条发现）：

| # | 发现（来源） | 裁决 | 证据 / 去向 |
|---|------------|------|-----------|
| 1 | disclosed 形参恒 True、事件无审计信号（盲#1、边#4 同根） | false | 设计即「事件锚点 + delivered_text 双重可验」（Design Notes）；签名与 disclosed=True 均为规格契约；全部合规出口应答必携标，无 reachable 的 False 场景 |
| 2 | pipeline 防御回退分支无测试覆盖（盲#2） | false | `test_normal_exit_records_ai_disclosure` 用 passthrough 替身链（不写 assembled_reply），正常出口恰好走该回退分支并断言事件与 reply 携标 |
| 3 | 无 trace_id 的 fail-closed 出口永远记不出事件（盲#3、边#6、验证缺口 other 同根） | medium → defer | 属实：uuid4 回填过不了 32-hex `_recordable`；但 `_recordable` 惯例自 1.5 既有、非本 Story 引入，买家可见携标不受影响，AC1 只要求正常链留痕；已录 deferred-work.md 待人拍板矩阵行或 trace_id 政策 |
| 4 | fail-closed 矩阵行三个子场景仅 Redis 失败路有事件断言（盲#4） | medium → patch（部分） | 请求校验失败子场景无可挂 trace_id、与 #3 同根 defer；未知 handoff_status 子场景带 "e"*32 可挂却缺断言——已 patch：补 `test_unknown_handoff_status_fails_closed` 的 fake_langfuse + ai.disclosure 断言 |
| 5 | 既有裸解包 `(event,) = fake_langfuse.events` 的测试会炸（盲#5） | false | 裸解包仅见于单事件 helper 单测（test_tracing L413/427/458）；handoff 系测试均按 name 过滤；全量 679 通过直接证伪 |
| 6 | tracing 依赖 filters.templates 倒置分层（盲#6） | false | 边猎已核实无环；templates 不依赖 observability；唯一修法（挪常量）违反冻结约束「前缀常量唯一来源 templates.AI_DISCLOSURE_PREFIX」 |
| 7 | metadata 冗余、label 是活常量非实发文本（盲#7） | false | `"event"` 键是 5.3 既有封装惯例；label vs delivered_text 比对即规格设计的漂移校验手段 |
| 8 | 中置【AI 助手】导致文本内标签重复（盲#8） | false | 与改动前内联组装逐字节同行为（非本 Story 引入）；任何清洗修法违反冻结约束「过审草稿除加前缀外不得再被改写」 |
| 9 | 两个兜底测试近乎重复（盲#9） | low → reject | 纯测试文件简化，无日常损害，修正成本大于收益 |
| 10 | 编排器防御回退与「唯一注入载体」矛盾（盲#10） | false | 防御回退是规格任务明文；render_fallback 是预置前缀模板库非注入点；「唯一载体」指草稿加注 |
| 11 | test_normal_exit_records_ai_disclosure 名不副实（盲#11） | false | 测名指「正常出口」，恰覆盖该出口的事件与回退携标；草稿链路由 test_final_assembly + risk_review 套件覆盖（验证缺口层已核实） |
| 12 | 规格段落空置/状态滞后（盲#12） | low → 已处理 | 工作流过程态所致；本轮已补 Implementation Notes 与本 Triage Log |
| 13 | 空串草稿产出纯前缀应答（边#1） | medium → defer | 与改动前行为逐字节一致（旧代码同样只判 is not None），非本 Story 引入；修法触碰草稿冻结约束；已录 deferred-work.md |
| 14 | 前导空白+前缀草稿双标（边#2） | medium → defer | 同 #13 根因（None-only 判断沿用旧语义），同单 defer 条目 |
| 15 | 非默认链产出空串 assembled_reply（边#3） | false | 无 reachable 生产方：全仓库仅组装器写该字段且永不产出空串 |
| 16 | 删除内联组装后无组装器链丢弃过审草稿（边#5） | false | 规格明文改消费 assembled_reply + 防御回退；默认链含真实组装器且 filter_names 顺序有测试锁定 |
| 17 | 事件落 DEFAULT 级偏离「分级口径同 already_handed_off」（边#7） | false | WARNING 是「终态分支」语义；常规事件 DEFAULT 有 record_delivery 判例；无具名损害（审计按事件名查询，测试可证） |

## Design Notes

- 组装器做注入载体而非留在编排器内联：Epic 技术决策明文「AI 身份披露由响应组装过滤器承载」，且 4.4 注释已预留「完整组装器归后续 Epic」；编排器最终返回点退化为「读组装产出 + 防御回退」，正常链输出逐字节不变，既有锁定测试自然保持绿色。
- 双标防御规则：仅对 ``generated_reply`` 草稿加前缀，``render_fallback`` 产出一律原样透传（其内部已带前缀）；草稿侧用 ``startswith`` 一次性守卫，不做全文替换。
- 不加结构化身份字段：``PipelineResult`` 契约冻结，工作台的身份切换由 Kafka 转交指令驱动；trace 侧 ``ai.disclosure`` 事件 + ``delivered_text`` 文本双重可验，满足 AC「经 Langfuse trace 会话记录验证」。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警
- `uv run mypy src` -- expected: 零告警（仓库 mypy strict 口径，仅扫 src）
