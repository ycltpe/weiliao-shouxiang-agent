---
title: 'Story 5.1 硬触发条件判定'
type: 'feature'
created: '2026-11-09'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '972d0f9eee2273721fc86fae04ea7d5159868b80'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 当前管道对红线意图（价格谈判/贷款政策/投诉）、图片/语音输入、同一意图反复追问、负面词汇四类高风险信号无任何转人工机制，敏感场景会继续走缓存/LLM 生成回答，合规红线可能被触碰。

**Approach:** 在 intent_router 过滤器内引入单一硬触发评估模块（AD-12），集中判定四类硬触发条件：命中任一即短路下游（缓存/LLM/风控/组装全部不执行），判定结果（触发与否 + 命中条件）写入信封，handoff 状态经会话管理模块写入 Redis（AD-4 单写者），全程记录 Langfuse trace。硬触发条件集（追问阈值、负面词表、红线意图范围）配置化，默认按 FR 值生效，可灰度调整。

**Decisions（人类拍板，2026-11-09）：**
- 命中后的买家响应 = 固定文案"已为您转接经纪人，请稍候"（Q1 选 A）：5.1 短路下游后由转人工出口分支直接回复该文案，5.1 可独立上线，5.2/5.3 只补 Kafka 指令与终态收口；该文案为转人工专用出口文案，不走模板兜底库。
- 负面词表形态 = repo 内置静态词表文件（Q2 选 A）：词表由风控中台产出发布进仓库，服务启动只读加载，env 仅覆盖文件路径；加载失败启动即 fail-fast。

## Boundaries & Constraints

**Always:**
- 全部四类硬触发由 intent_router 内单一评估模块统一判定，判定结果写入信封（触发标记 + 命中条件清单），并记录 Langfuse trace（可审计漏判）。
- 硬触发命中即短路：cache_lookup / llm_generate / risk_review / response_assembler 均不得执行，红线意图 100% 零生成回答。
- handoff 状态写入唯一入口为 SessionManager（单写者，AD-4）；追问计数读 Redis 权威值（record_followup 返回值），不读 L1 快照。
- 命中条件的判定顺序与判定结果必须与配置一致，默认追问阈值 = 3，红线意图 = NEGOTIATION / LOAN_POLICY / COMPLAINT。

**Never:**
- 不发送 Kafka 转交指令、不实现买家安抚通知下发（Story 5.2 / 5.3）。
- 不做 AI 身份标识注入（Story 5.4）。
- 禁止硬触发判定分散到多个过滤器各自评估；禁止命中后任何过滤器继续走缓存/LLM 路径。
- 不修改信封既有字段语义与校验规则；媒体类型具体取值若需约束，仅新增不改旧。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 红线意图 | intent = NEGOTIATION / LOAN_POLICY / COMPLAINT | 触发转人工，下游零执行，handoff 状态写 Redis | N/A |
| 非文本输入 | media_type 非空且非文本值 | 触发转人工（图片/语音不分类、不生成） | N/A |
| 追问达阈值 | record_followup 返回该意图追问序号 ≥ 阈值（默认 3） | 触发转人工 | Redis 故障时追问记录 fail-safe（沿用 2.3 语义，不触发） |
| 负面词汇 | message 命中词表任一词条 | 触发转人工 | 词表加载失败 fail-fast（启动即败，不静默放行） |
| 多条件同时命中 | 红线意图 + 负面词同时命中 | 触发，命中条件清单包含全部命中项 | N/A |
| 未命中 | 普通意图文本消息 | 管道照常执行，信封 handoff 字段为默认未触发 | N/A |
| 判定模块自身异常 | 评估代码抛异常 | fail-closed：按触发处理并记 trace 告警，禁止放行到生成链路 | 同左 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/models/intent.py` -- IntentTag 单一 Literal 枚举（9 值，红线三标签已存在，勿改）。
- `src/weiliao_agent/models/envelope.py:32` -- Envelope 模型；`media_type: str|None`（L61，架构预留未消费）位于此；新增 handoff 判定字段挂点。
- `src/weiliao_agent/filters/intent_router.py:54` -- IntentRouterFilter；分类 trace（L163）、追问记录 `_record_followup`（L208，record_followup 返回值即权威追问序号）；L196 附近注释声明"本 Story 不写信封 handoff 字段"——本 Story 解除该限制。
- `src/weiliao_agent/pipeline.py:75` -- PipelineOrchestrator 默认链 L79-91（intent_router → cache_lookup → llm_generate → risk_review → response_assembler）；短路分支在此实现。
- `src/weiliao_agent/session/manager.py` -- SessionManager 单写者；`record_followup`（L136，Lua L70）；`handoff_status` 字段已存在于 hash 与 SessionSnapshot（L92），Lua 初始化恒 `"none"`；`mark_handoff` 方法骨架已标注"仍待 Epic 5 交付"。
- `src/weiliao_agent/observability/tracing.py` -- 模块级 helper 模式（`record_followup_hard_trigger` 等，fail-safe no-op）；新 trace 事件照此封装。
- `src/weiliao_agent/main.py` -- 组合根；配置解析在文件顶部（纯 env，`WEILIAO_*`，fail-fast raise ValueError）；Story 4.6 配置实例 L241-310 可复制其步骤。
- `tests/` -- 扁平命名 `test_<模块>.py`；4.5/4.6 各含模块测试 + 组合根装配测试先例；共享 fixture 在 `tests/conftest.py`。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/handoff/trigger.py`（新模块，包名可斟酌）-- 实现硬触发评估模块：四类条件集中判定（红线意图集合 / 媒体类型 / 追问序号阈值 / 负面词表），返回触发标记 + 命中条件清单；默认常量（阈值 3、红线三意图）权威在此模块 -- AD-12 单一评估点。
- [x] `src/weiliao_agent/handoff/negative_words.txt`（新文件，路径随实现斟酌）-- 内置负面词表，风控中台产出发布，一行一条，启动只读加载 -- 词表落地形态（决策 Q2-A）。
- [x] `src/weiliao_agent/models/envelope.py` -- 新增 handoff 判定字段（触发标记 + 命中条件清单，默认未触发；形态见 Design Notes）-- 判定结果供下游与会话管理消费。
- [x] `src/weiliao_agent/filters/intent_router.py` -- 分类与追问记录完成后调用评估模块，结果写信封、记 trace（未命中也记，支撑漏判审计）-- 判定集中挂载点。
- [x] `src/weiliao_agent/pipeline.py` -- 编排器识别信封 handoff 触发标记，跳过 cache/llm/risk/assembler，走转人工出口分支并回复固定文案"已为您转接经纪人，请稍候" -- 命中即短路、零生成回答。
- [x] `src/weiliao_agent/session/manager.py` -- 实现 `mark_handoff(session_id, conditions)`：handoff_status 由 `"none"` 置为 `"triggered"`（值域见 Design Notes），单写者约束不变 -- AD-4。
- [x] `src/weiliao_agent/main.py` -- 新增 env 配置解析（追问阈值 / 负面词表文件路径 / 红线意图集合，`WEILIAO_HANDOFF_*` 前缀，fail-fast），装配注入评估模块与编排器短路分支 -- 灰度调整通道。
- [x] `src/weiliao_agent/observability/tracing.py` -- 新增硬触发判定 trace helper（命中条件、触发结果、判定异常告警），复用 fail-safe no-op 模式 -- 可观测。
- [x] `tests/test_handoff_trigger.py` + `tests/test_handoff_composition_root.py`（命名可斟酌）-- 覆盖 I/O 矩阵全部场景（含判定异常 fail-closed、词表加载失败 fail-fast、env 装配校验）-- 防漏判/误放行的回归网。

**Acceptance Criteria:**
- Given 意图分类与追问计数可用，when 消息命中任一硬触发条件（红线意图 / 图片语音 / 同一意图追问 ≥3 / 负面词汇），then 立即触发转人工分支，cache/llm/risk/assembler 零执行，红线意图 100% 无生成回答。
- Given 硬触发已判定，then 判定结果（触发标记 + 命中条件）写入信封且写 Langfuse trace，handoff 状态经 SessionManager 写入 Redis（`handoff_status` = `"triggered"`）。
- Given 运营调整配置（追问阈值、负面词表、红线意图范围），when 服务重启，then 判定行为按新配置生效，非法配置值启动即 fail-fast。
- Given 判定模块抛异常，when 处理任意消息，then fail-closed 按触发处理并记 trace 告警，消息绝不进入生成链路。

## Implementation Notes

## Spec Change Log

## Review Triage Log

评审轮次 1（2026-11-09，三层：盲审 / 边界猎手 / 验证缺口）：

| # | 发现 | 裁决 | 处置 |
|---|------|------|------|
| 1 | 分类器异常 fail-closed 路径直接返回 UNKNOWN 未做硬触发评估（盲审+边界猎手同指，`intent_router.py` 分类 except 分支）；非文本媒体/负面词命中词不依赖分类输出，该路径下会流入生成链路，违反矩阵"非文本输入无条件触发" | high（核实属实：代码与测试 `test_router_classification_failure_leaves_handoff_unassessed` 均锁定该行为） | patch |
| 2 | 路由器层无负面词接线测试（验证缺口层，已扰动实证：message 实参改 None 全套件仍绿） | medium（属实，预验证可信） | patch |
| 3 | marker 写失败复用 `intent.classification_failure` 事件，污染分类失败信号 | low（属实，开发者向：漏判审计/失败率看板混入非分类故障） | patch |
| 4 | fail-closed 哨兵 `assessment_error` 随 conditions 持久化进 Redis，5.2 payload 将携带内部诊断词 | low（属实；信封保留哨兵不变，仅过滤持久化清单） | patch |
| 5 | `SessionSnapshot` 不暴露 `handoff_conditions`，5.2/5.3 消费者将被迫绕过 SessionManager 读裸 Redis，侵蚀 AD-4 封装 | low（属实，开发者向） | patch |
| 6 | 三个 `WEILIAO_HANDOFF_*` env 未写入 `.env.example`（仓库惯例：全部旋钮均在此文档化） | low（属实，直接补录） | patch |
| 7 | `test_mark_handoff_refreshes_ttl` 硬编码 1800s 默认 TTL | low（属实；修正是直接的：构造时显式传 ttl 并按其断言） | patch |
| 8 | 评估器构造器不过滤空/空白词条：`negative_words` 含 `""` 时 `"" in message` 恒真，全量误触发转人工 | medium（属实：构造器 `frozenset(negative_words)` 无过滤； wired 路径经 `load_negative_words` strip 安全，但公共构造器是埋雷点，修复是直接的词条规范化） | patch |
| 9 | handoff 状态落盘后无任何读取方，转人工后同 session 后续消息仍走 AI 管道 | medium（属实，但消费方属 Story 5.2/5.3——会话响应路径关闭是 5.3 原子终态范畴） | defer |
| 10 | `mark_handoff` 缺键产生不完整 hash，`read_snapshot` 返回 None 被测试固化 | low（属实但拒绝：生产入口 begin_turn 先于管道执行，该路径不可达；修复需向 Lua 加 turn 初始化，超出直接修正） | reject |
| 11 | 无触发量指标，灰度调整无法量化 | false（驳回：Epic 可观测 FR 要求"命中条件、触发结果写 trace"已实现——全量判定事件经 Langfuse dashboard 可计数，量化视图可得） | reject |
| 12 | `negative_words.txt` 打包未声明，wheel 可能漏发 | false（驳回：hatchling wheel `packages` 默认包含包目录下全部文件，词表随包发布无需额外声明） | reject |
| 13 | `media_type` 精确匹配 `"text"` 大小写敏感，`"text/plain"` 会致全量误触发 | false（驳回：Design Notes 明确拍板"None/'text' 视为文本，其余一律判触发，枚举收紧留待适配器 Story"——所述行为即 spec 语义；且当前无任何 media 生产者） | reject |
| 14 | sprint-status/spec/任务勾选三处状态不一致 | false（驳回：系工作流状态机自身——step-03 置 in-progress、step-04 置 in-review，非代码缺陷） | reject |
| 15 | 每条消息全量发 Langfuse 事件有容量成本，diff 未记权衡 | false（驳回：未命中也记是 spec Always 拍板的漏判审计口径，docstring 已载依据；与既有每条消息的分类 trace 事件同量级） | reject |
| 16 | assessor 注入而 marker 为 None 时触发不落盘 | low（属实但拒绝：组合根恒成对注入，该状态不可达；修复需新增构造守卫，属加复杂度） | reject |
| 17 | `negative_words` 为空集（构造器默认）时负面条件静默失效 | false（驳回：生产接线经 `load_negative_words` fail-fast 保证非空；构造器默认空集是可选注入语义，与 marker/assessor 可选性同口径） | reject |
| 18 | 词表文件非 UTF-8 时 `UnicodeDecodeError` 逃逸 ValueError 契约、报错缺变量名 | false（驳回：`UnicodeDecodeError` 是 `ValueError` 子类，main.py 的 `except ValueError` 会包装并带变量名重新抛出，fail-fast 契约成立） | reject |

## Design Notes

- handoff_status 值域：本期只新增 `"triggered"`（Lua 初始化 `"none"` 不变）；5.2/5.3 如需更细状态机（如 `"handed_off"`）在后续 Story 扩展，本期不为未来状态预留枚举。
- 信封新增字段形态建议嵌套模型 `HandoffAssessment{triggered: bool, conditions: list[str]}` 挂为 `handoff: HandoffAssessment | None = None`，未评估为 None、评估未触发为 `triggered=False`——区分"没评估"与"评估了没命中"，下游短路只认 `triggered is True`。
- 追问达阈语义：record_followup 返回的（该意图追问序号, 总追问数）中，该意图序号 ≥ 阈值即命中——与 Story 2.3 的 `is_followup`（第 2 次起）口径不同，勿复用 is_followup。
- 媒体输入语义：`media_type` 为 None 或 `"text"` 视为文本；其余任何值一律判触发。具体媒体枚举在接入适配器 Story 落地前不收紧校验。
- 负面词匹配：对 `message` 做子串包含匹配（词条为连续字面量，无正则），命中即记该词条为命中条件。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增 handoff 用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警（若仓库用其他 lint 命令以仓库现有脚本为准）
