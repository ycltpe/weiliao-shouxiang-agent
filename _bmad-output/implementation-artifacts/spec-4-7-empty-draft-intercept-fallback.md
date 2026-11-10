---
title: 'Story 4.7 空草稿拦截与兜底承接'
type: 'feature'
created: '2026-11-10'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred:
  - summary: >-
      恰为 AI_DISCLOSURE_PREFIX（或前缀+空白）的草稿在 response_assembler 按"已携标"原样透传，买家可收到只有前缀没有正文的消息
    evidence: |-
      该草稿 strip 后非空（is_blank_draft=False）且 startswith(AI_DISCLOSURE_PREFIX) → 透传分支命中。
      属 Story 5.4 已携标透传语义的既有行为，本 Story diff 未触碰该分支；epic AC 将拦截范围限定为
      空串/仅空白，"前缀但无正文"是否按无草稿拦截需意图层拍板后另行立项。
    location: >-
      src/weiliao_agent/filters/response_assembler.py:27
    severity: medium
  - summary: >-
      仅含零宽空格（U+200B）或 BOM（U+FEFF）的草稿经 strip() 判为非空白，携标前缀下发
    evidence: |-
      已实测 "\u200b".strip() 与 "\ufeff".strip() 均原样保留。epic AC 判空集合明确为
      空格/换行/制表符组合，是否扩展判空字符集属意图层决定；既有非空草稿透传行为，非本 Story 引入。
    location: >-
      src/weiliao_agent/filters/base.py:12
    severity: low
baseline_commit: 'c409248675d1e05265fe3efd85ca4e6c9e3e3ee9'
---

<intent-contract>

## Intent

**Problem:** 生成链路理论上可能产出空串或纯空白草稿（如模型返回空 content）。现有 response_assembler 只判 `generated_reply is None`，空草稿会被加上"【AI 助手】"前缀直接下发——买家收到只有前缀没有正文的残缺消息；risk_review 还会拿空文本去调风控中台，浪费审核资源并污染通过率指标。

**Approach:** 定义唯一的草稿判空口径（strip 后为空即"无草稿"），在 risk_review 与 response_assembler 两处入口统一拦截：拦截后走既有模板兜底路径（render_fallback），并在 Langfuse trace 记录 draft_empty 拦截事件；非空草稿路径逐字节冻结不动。

## Boundaries & Constraints

**Always:**
- 判空口径唯一出处：共享纯函数（strip 后为空串即判空），risk_review 与 response_assembler 两处共用，禁止各处自写判断条件。
- 拦截语义 = "按无草稿处理"：risk_review 对空草稿不调中台、不计通过率分母；response_assembler 对空草稿走 `render_fallback(envelope.intent)`，买家收到完整模板应答。
- 拦截必须留痕：Langfuse trace 记录 draft_empty 拦截事件（含 stage 归因区分 risk_review / response_assembler），trace_id 符合 32-hex 可检索；事件记录 fail-safe（自身再抛也吞掉）。
- 标准管道里每条消息至多一条 draft_empty 事件：risk_review 先拦截并清回 None，下游组装器看到 None 不再重复记录。
- 拦截属设计内行为：`degraded` 语义不动，不触发告警日志，买家应答不中断。

**Never:**
- 不改非空草稿的既有行为：加前缀携标、已携标跳过防双标、过审草稿除加前缀外冻结（4.4 冻结约束）逐字节保持。
- `generated_reply=None`（未生成/红线/熔断/审核不通过）的既有行为不变，且 None 不算拦截、不记 draft_empty 事件。
- 不改 llm_generate、LLM 子图、熔断器、模型路由、模板库、信封模型；不引入新指标收集器或 /health 快照（AC 未要求）。
- 不对草稿做任何清洗/改写/strip 后再用（判空只用于分支选择，空白草稿直接作废，不尝试修复）。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 预期输出 / 行为 | 错误处理 |
|------|------------|----------------|---------|
| 空串草稿组装 | 审核通过且 `generated_reply == ""` | 不携标组装；`assembled_reply == render_fallback(intent)` 完整模板应答；trace 记 draft_empty 事件（stage=response_assembler） | 事件记录自身异常吞掉，兜底照常 |
| 纯空白草稿 | `generated_reply == "  \n\t "` | strip 后判空，与空串同等处理 | 同上 |
| 非空草稿 | `generated_reply` 非空（含已携标） | 行为与现状逐字节一致（加前缀或跳过），不记 draft_empty 事件 | N/A |
| 空草稿到 risk_review | `generated_reply` 为空串/纯空白 | 不调中台、不计分母；清回 None；trace 记 draft_empty 事件（stage=risk_review） | 事件记录自身异常吞掉 |
| None 草稿 | `generated_reply is None` | 两过滤器均按现状透传/兜底，零新增行为、不记事件 | N/A |

</intent-contract>

## Code Map

- `src/weiliao_agent/filters/response_assembler.py` -- 改：组装入口判空拦截点。现状只判 `is None`（第 23 行附近），空草稿会拼出"【AI 助手】"残缺消息；模板兜底经 `render_fallback`（已内含前缀，透传不二次加注）。
- `src/weiliao_agent/filters/risk_review/filter.py` -- 改：管道先到的拦截点。现状 `draft is None` 分支（第 53-56 行）透传不调中台；空草稿当前会直达 `_review` 调中台并计入分母。拦截后 `model_copy(update={"generated_reply": None})` 与审核不通过清稿口径（第 70 行）一致。
- `src/weiliao_agent/filters/base.py` -- 新增共享判空纯函数（过滤器契约模块，纯函数不违反"过滤器间禁止直接调用"——无状态、无过滤器实例交互）；两处拦截的唯一口径出处。
- `src/weiliao_agent/observability/tracing.py` -- 新增 `record_draft_empty_event`：仿 `record_model_routing_event`（第 806 行）的 `_recordable`/`_client_or_none`/`suppress(Exception)` 骨架；事件名仿既有命名（如 `llm.model_routing_event`），metadata 含 `stage`；DEFAULT 级（设计内拦截，同熔断 skip 口径）。
- `src/weiliao_agent/filters/templates.py` -- 只读：`render_fallback` / `AI_DISCLOSURE_PREFIX` 直接复用，本 Story 不改。
- `src/weiliao_agent/filters/llm_generate.py` -- 只读：草稿唯一产出方（第 109 行 `model_copy` 挂稿），本 Story 不在产出侧拦截。
- `tests/test_response_assembler.py` / `tests/test_risk_review_filter.py` / `tests/test_tracing.py` -- 扩：三个既有测试文件的替身惯例（fake client / spy tracing / _FakeLangfuse）直接沿用，全程零网络。

## Tasks & Acceptance

**Execution:**
- `src/weiliao_agent/filters/base.py` -- 新增共享判空纯函数（strip 后为空即 True）-- 两处拦截口径唯一出处，I/O 矩阵全场景分支。
- `src/weiliao_agent/observability/tracing.py` -- `record_draft_empty_event(trace_id, *, stage)` -- AC 1/4 的 trace 锚点，fail-safe 骨架照抄既有事件函数。
- `src/weiliao_agent/filters/risk_review/filter.py` -- 空草稿分支：记事件 + 清回 None + 不调中台不计分母 -- I/O 矩阵"空草稿到 risk_review"与 AC 4。
- `src/weiliao_agent/filters/response_assembler.py` -- 空草稿走 `render_fallback` + 记事件 -- I/O 矩阵"空串/纯空白草稿组装"与 AC 1/2。
- `tests/test_response_assembler.py` + `tests/test_risk_review_filter.py` + `tests/test_tracing.py` -- 扩：空串/纯空白/None/非空四态全覆盖、中台零调用与分母断言、事件本体直测与 stage 归因、管道级单事件断言（risk_review 拦截后组装不再二次记录）-- 全程替身零网络。

**Acceptance Criteria:**
- Given 风控审核通过且 `generated_reply` 为空串，when response_assembler 处理该信封，then 不执行携标组装，`assembled_reply` 为完整模板兜底应答（含前缀），且 Langfuse trace 记录 draft_empty 拦截事件（trace_id 为 32-hex 可检索）。
- Given `generated_reply` 为仅空白字符串（空格/换行/制表符任意组合），when 任一拦截入口处理，then strip 后判空，与空串完全同等处理。
- Given `generated_reply` 非空，when 两过滤器处理，then 行为与现状逐字节一致（加前缀携标或已携标跳过），不触碰"过审草稿除加前缀外不得再被改写"冻结约束，且不产生 draft_empty 事件。
- Given 空草稿到达 risk_review，when 拦截判定，then 与响应组装入口共用同一判空口径，按无草稿处理：不调风控中台、不计通过率分母、草稿清回 None，trace 记录 draft_empty 事件；标准管道中该消息全程仅一条该事件。
- Given `generated_reply` 为 None，when 两过滤器处理，then 全部行为与 Story 4.4/5.4 现状逐字节一致，不新增任何事件或副作用。

## Spec Change Log

## Review Triage Log

### 2026-11-10 — Review pass
- verdicts: 11 findings — high 0, medium 1, low 4, false 6, maybe-false 0
- findings:
  - `[false]` `[reject]` is_blank_draft(None) 返回 False 被称"函数名与契约不符" — 当前程序不存在该坏结果：两处调用点均先判 None 再调 helper，docstring 明确记载 None 不判空的设计语义；所述误路由需一个无视文档的未来重构才会发生，属未证明可达的假想
  - `[medium]` `[defer]` 恰为 AI_DISCLOSURE_PREFIX（或前缀+空白）的草稿绕过拦截原样下发 — 已核实：该草稿 strip 后非空、startswith 前缀 → 透传，买家收到只有前缀的消息；属 Story 5.4 已携标透传语义的既有行为（本 diff 未触碰该分支），epic AC 将拦截范围限定为空串/仅空白，扩至"前缀无正文"需意图层拍板 — 已延期
  - `[low]` `[defer]` 零宽字符（U+200B）/BOM（U+FEFF）组成的草稿 strip 后判非空、携标下发 — 已核实 `"\u200b".strip()` 原样保留；epic AC 判空集合明确为 空格/换行/制表符，是否扩展属意图层决定；既有非空草稿透传行为，非本 Story 引入 — 已延期
  - `[false]` `[reject]` 事件 metadata.event 缺 `response.` 前缀被称命名空间不一致 — 仓库既有惯例即如此：`llm.breaker_event`/`llm_breaker_event`、`risk_review.event`/`risk_review_event` 均同构拆分，本实现完全跟随 4.4/4.5/4.6 惯例，无引入性偏差
  - `[low]` `[patch]` `_draft_empty_events` 测试助手在两测试文件重复 — 已迁移至 tests/conftest.py（fake_langfuse fixture 旁），事件名锚点单一化
  - `[low]` `[patch]` response_assembler 两个分支重复调用 render_fallback — 已合并：blank 判定一次，blank 路径只记事件，None/blank 共享单次 render_fallback 调用
  - `[false]` `[reject]` spec 审查时 Spec Change Log / Review Triage Log 为空 — 模板定义即"首次审查前为空"，本步骤负责追加，非缺陷
  - `[false]` `[reject]` AC 1"风控审核通过且 draft 为空串"被称自相矛盾 — 该措辞逐字继承自 epics.md AC，描述组装器直测场景（信封态可构造），直测已覆盖；非行为缺陷，且修正即改本 build 的 spec，按规则拒绝
  - `[low]` `[patch]` 共享判空函数 None 语义无直测守护 — 已新增 tests/test_filters_base.py 直测 is_blank_draft(None) is False 及空白/非空参数化变体
  - `[false]` `[reject]`（意图对齐层）买家可见面仅被代理验证 — 验证缺口层已追踪：PipelineOrchestrator 在 pipeline.py:206 读 assembled_reply 产出 result.reply，即系统最外表面，端到端测试断言 result.reply 等于完整模板兜底，意图期望所在面与测试执行面一致
  - `[false]` `[reject]`（意图对齐层）trace 事件仅对 fake Langfuse 断言、真实后端可检索未端到端验证 — 全仓库 4.x/5.x 所有事件均走同一 fake 替身惯例（零网络测试纪律），非本 Story 引入的缺口

## Design Notes

- 事件命名沿用既有 taxonomy 风格：函数 `record_draft_empty_event`，事件名 `response.draft_empty_event`（metadata `event="draft_empty_event"`），附加 `stage: "risk_review" | "response_assembler"` 归因。DEFAULT 级：拦截是设计内兜底，同 4.5 熔断 skip 的"ERROR 只留真实故障"口径，不制造噪音洪流。
- 单事件保证靠"拦截即清回 None"：risk_review 清稿后组装器走既有 None 分支（无事件），不会在有效管道里双记；组装器侧记录仅覆盖绕过 risk_review 的调用/测试场景。
- 判空不做修复性 strip 回用：空白草稿直接作废。尝试 strip 后组装会引入"改写草稿"嫌疑，触碰 4.4 冻结约束的解读风险。

## Verification

**Commands:**
- `uv run pytest tests -q` -- 全部通过（含新增/扩展替身测试，零网络）。
- `uv run ruff check src tests` -- 无告警。
- `uv run mypy src` -- strict 无错误。

## Auto Run Result

Status: done

**Summary of implemented change:** 空草稿（空串/纯空白）在 risk_review 与 response_assembler 两处入口按统一口径（共享 `is_blank_draft`，strip 后判空）拦截：risk_review 不调中台、不计通过率分母、清回 None 并记 draft_empty 事件（stage=risk_review）；response_assembler 走完整模板兜底并记事件（stage=response_assembler）；标准管道全程单事件。非空与 None 草稿路径逐字节冻结。

**Files changed:**
- `src/weiliao_agent/filters/base.py` — 新增共享判空纯函数 `is_blank_draft`（None 不判空，docstring 锁定语义）
- `src/weiliao_agent/observability/tracing.py` — 新增 `record_draft_empty_event`（DEFAULT 级，stage 归因，fail-safe）
- `src/weiliao_agent/filters/risk_review/filter.py` — 空草稿拦截分支（记事件 + 清回 None + 零中台调用）
- `src/weiliao_agent/filters/response_assembler.py` — 空草稿走模板兜底（blank 判定一次，None/blank 共享单次 render_fallback）
- `tests/conftest.py` — 共享 `draft_empty_events` 断言助手（事件名锚点单一化）
- `tests/test_filters_base.py`（新建）— is_blank_draft 直测（None 语义钉死 + 空白/非空参数化）
- `tests/test_response_assembler.py` / `tests/test_risk_review_filter.py` / `tests/test_tracing.py` — 扩展覆盖矩阵全分支与端到端单事件断言

**Review findings breakdown:** 三方审查（盲审 9 / 边界 0 / 验证缺口 0）+ 意图对齐描述性报告。patch 3 条（均为 low：测试助手去重、组装器 render_fallback 去重、判空函数直测守护）；defer 2 条（medium：前缀但无正文的草稿透传，low：零宽/BOM 字符判空，均为既有行为、需意图层拍板）；reject 6 条（函数名语义争议、事件 metadata 命名空间惯例、审查时空日志、AC 1 措辞继承自 epics、买家面代理质疑被验证缺口层追踪反驳、fake Langfuse 惯例质疑）。patch 计数：high 0 / medium 0 / low 3。

**Follow-up review recommendation:** false（首 pass 无 high patch、medium patch 数为 0）。

**Verification performed:** `uv run pytest tests -q` 779 passed；`uv run ruff check src tests` All checks passed；`uv run mypy src` strict 48 文件无错误。I/O 矩阵 5 行逐一有对应测试且全部运行通过。

**Residual risks:** 延期两项见 frontmatter `deferred`（前缀无正文草稿透传、零宽/BOM 判空），均需意图层决策，不影响本 Story AC 达成。
