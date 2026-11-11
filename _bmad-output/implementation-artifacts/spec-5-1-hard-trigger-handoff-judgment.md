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

### Review Findings

（2026-11-10 Epic 5 级 code review，diff 范围 972d0f9..93b2194 存 `review-epic5.diff`。全局统计：**3 项 decision + 11 项 patch + 9 项 defer + 6 项 dismissed**；各分录见 spec-5-1 ~ spec-5-5。本分录 2 项 decision + 4 项 patch + 4 项 defer。四评审层（blind-hunter / edge-case-hunter / verification-gap / acceptance-auditor）并行，全部发现经主审逐一对照当前工作树源码复核。）

- [x] [Review][Patch] 追问 ≥3 硬触发口径被 per-(tag, anchor) 计数稀释，与 Epic 5 冻结的「同一意图 ≥3」语义冲突（medium；acceptance-auditor 层，主审已读码确认）【2026-11-11 裁决：B——硬触发判定改回「同一意图」语义，评估模块取数改为 per-tag 聚合计数（非会话级总数，后者会把跨意图追问混入阈值）】 [`src/weiliao_agent/session/manager.py:250-280`、`src/weiliao_agent/filters/intent_router.py:217-270`] — 计数键为 `followup:{TAG}:{anchor}`，anchor 由信封 coref 机械导出：买家交替发「它多少钱」（coref 命中 → anchor `house:{id}`）与「多少钱」（无指代 → 哨兵 `none`）时计数分散在两个键下，任一键可能永远达不到 3，追问硬触发静默失效。本行为由 diff 内 Story 6.3 锚定改造引入。修法（B）：`_LUA_RECORD_FOLLOWUP` 在同一 roundtrip 内额外维护 per-tag 聚合字段（`followup_tag:{TAG}` HINCRBY），`record_followup` 返回值带 per-tag 聚合计数；is_followup 口径保持 per-(tag, anchor) 不变（Epic 6 FR-2），仅硬触发阈值判定改用 per-tag 聚合值。epic-5-context 原文「同一意图追问 ≥3」与实现随之自洽。
- [x] [Review][Patch] 负面词匹配语义：纯子串包含 + 无 Unicode 归一化，双向失守（medium；blind-hunter + edge-case-hunter 双层独立命中）【2026-11-11 裁决：A——保留纯子串匹配，加 Unicode 归一化（NFC + NFKC 全半角折叠 + 内置简繁映射覆盖词表字符），误触发治理归风控词表瘦身】 [`src/weiliao_agent/handoff/trigger.py:137-140`、`src/weiliao_agent/handoff/trigger.py:150-165`] — 误触发方向：词表含「垃圾」等通用子串，买家问「小区垃圾分类做得如何」即命中被强制转人工（成本=真人介入+会话报废，灰度调阈值救不了子串误命中）；绕过方向：繁体「騙子」、零宽字符插入「骗\u200b子」均不命中，四类触发之一的负面词通道可被 trivially 规避。修法（A）：评估前对消息做归一化（NFC 组合 + NFKC 全半角折叠 + 剥零宽字符），词表加载时同法归一化并按内置简繁映射生成词表字符的繁体变体词条（依赖内建映射，不引第三方包）；匹配语义保持子串包含不变。风控词表瘦身（如「垃圾」词条处置）另行推进，不在本 patch。
- [x] [Review][Patch] HTTP 入口未剥离客户端可控的信封内部字段，`handoff.triggered` / `media_type` 可伪造绕过 AD-12 评估直发转交指令（high；edge-case-hunter 层，主审已读码确认可达） [`src/weiliao_agent/adapters/http.py:171-176`、`src/weiliao_agent/models/envelope.py:100-110`、`src/weiliao_agent/pipeline.py:320-324`] — `POST /v1/messages` 请求体经 `Envelope.model_validate(payload)` 直接接受客户端自带的 `handoff` / `media_type`；链首 coreference 只覆写 `resolved_message`/`coref`，过滤器循环每步检查 `envelope.handoff.triggered`，伪造值在 intent_router 评估前即短路 `_handoff_result`，以自选 conditions 向工作台 topic 发指令（伪造工单/绕过红线语义）；伪造 `media_type="image"` 同理强制触发。修法：入口适配器经 model_copy 重置服务端自有字段（`handoff=None`、`media_type=None`——与既有 turn 重置同型），直至接入适配器真正标注媒体类型。
- [x] [Review][Patch] 判定异常 fail-closed（assessment_error）路径无任何本地日志（medium；blind-hunter 层） [`src/weiliao_agent/filters/intent_router.py:304-318`] — 评估器为纯内存代码，若因缺陷持续抛异常，每条消息都被判触发 → 会话强制终态 → 尝试发指令（条件清单为空），观测完全依赖 Langfuse；对照：发送器被禁用都补了逐条 WARNING，这条更危险的路径反而无本地留痕。修法：except 分支补 ERROR/WARNING logger（记录 session_id/trace_id/异常类型），不改变 fail-closed 语义。
- [x] [Review][Patch] 5.1 评估器/标记器组合根接线无内省测试守护——删除注入行全部测试仍绿（medium；verification-gap 层，主审已核实 tests/ 全库无 `_handoff_marker` 接线断言） [`src/weiliao_agent/main.py:623-624`、`tests/test_handoff_composition_root.py:108`] — 现有测试只回读模块级 `_handoff_assessor` 属性；对照 5.2 有 `orch._handoff_sender is s`、5.5 有 `orch._handoff_status_reader is m._session_manager` 内省测试，5.1 唯独缺这一对。红线意图/负面词硬触发若接线回归，生产静默失效、全套件绿。修法：与 `test_handoff_status_reader_wired_to_session_manager` 同型的子进程内省测试，断言 IntentRouterFilter 实体的 assessor/marker 引用。
- [x] [Review][Patch] 自定义负面词表带 BOM 时首词条永久失配（low；edge-case-hunter 层） [`src/weiliao_agent/handoff/trigger.py:155`] — `read_text(encoding="utf-8")` 非 `utf-8-sig`；`WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH` 指向 Windows/Excel 编辑保存的词表时首行词条带 `\ufeff` 前缀，子串匹配永不命中且 fail-fast 不报错（文件非空、加载成功）。同 diff 的 `tests/eval_coref_resolver.py` 已用 `utf-8-sig`，两处口径不一致。修法：改 `utf-8-sig`。
- [x] [Review][Defer] `handoff_conditions` 落盘 Redis 后生产零消费方（死写）（low；blind-hunter + verification-gap 双层） [`src/weiliao_agent/session/manager.py:135`、`src/weiliao_agent/session/manager.py:391-392`] — `_LUA_MARK_HANDOFF` HSET 的 `handoff_conditions` 字段经 begin_turn/read_snapshot 双解析路径读出，但 5.2 载荷实际消费 `envelope.handoff.conditions`（command.py `_build_command`），snapshot 字段无人读；5.1 评审 #5 patch 的理由（「5.2/5.3 将被迫绕过 SessionManager 读裸 Redis」）未兑现。Lua 字段、双解析路径与快照字段成纯负债，与已 defer 的 L1 handoff 快照同属孤儿契约。settle：拍板存废（删除字段 + 双解析路径，或保留至工作台消费方落地）。
- [x] [Review][Defer] 负面词/媒体硬触发评估基于 `resolved_message` 而非买家原文（low；blind-hunter 层） [`src/weiliao_agent/filters/intent_router.py:297-299`] — `_assess_handoff` 的 message 实参为 `resolved_message`；Epic 6.2 规则 resolver 会改写文本，若改写意外改变含负面词的片段即漏判（5.1 矩阵写的是「message 命中词表」=买家原文语义）。当前 resolver 透传等价，属潜伏契约漂移。settle：6.2 验收时明确拍板评估输入口径（原文 vs resolved）。
- [x] [Review][Defer] 追问计数键格式变更（`followup:{TAG}` → `followup:{TAG}:{anchor}`）无旧键兼容读取（low；verification-gap 层） [`src/weiliao_agent/session/manager.py:272`] — 旧部署计数键无 `:none` 后缀，新代码不双读不迁移；30 分钟 TTL 窗口内滚动升级，升级前计数为 2 的会话从 1 重新累计，`followup_threshold=3` 硬触发推迟 2 条消息；测试只建新格式键。settle：部署形态落地时拍板是否加一次性双读（TTL 窗口短，亦可接受瞬时不一致）。
- [x] [Review][Defer] `record_followup` trace metadata 键 `per_intent_count` 更名为 `per_tag_anchor_count`，既有消费方静默断流（low；edge-case-hunter 删除检查） [`src/weiliao_agent/observability/tracing.py:395`] — 任何按旧键名建好的 Langfuse 看板/告警规则升级后不再收到数据，diff 内无兼容期或迁移注记。项目尚无看板，影响为潜在性。settle：首个看板落地前在 runbook 注记键名口径。
