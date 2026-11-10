---
title: 'Story 6.2 房源指代归一化默认实现'
type: 'feature'
created: '2026-11-10'
status: 'done'
baseline_commit: 'bedf1e3177cd2bdcc5a37a7d6cf439d6c7455881'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: ['oversized']
deferred:
  - summary: >-
      P99 < 10ms 过滤器延迟预算无自动化验证路径（仓库无压测设施）。
    evidence: |-
      属实但 P99 是压测属性，与 Story 6.1 评审#15 同根同裁决；resolver 为单趟
      正则扫描、微秒级，性能风险可忽略。settle：归 Story 6.3 / FR-1 端到端
      验收（含消解的意图分类 P99 < 50ms 口径）一并压测。
    location: >-
      src/weiliao_agent/coref/rule_based.py
    severity: low
---

<intent-contract>

## Intent

**Problem:** Story 6.1 已把指代消解过滤器接上链首，但默认 resolver 为 None（恒等透传），"它/这套/那套/上面说的"等房源指代仍原样流向意图分类，PRD 小王场景（"它采光怎么样"）无法被正确理解。OQ-5 的最终选型（规则模板/序列标注/LLM 改写）第 2 周才定，但接口已锁定，需要一个可整体替换的规则模板默认实现先落地。

**Approach:** 新建纯函数、零 I/O 的规则模板 resolver（输入信封、锚定 `envelope.house_id`），把消息中的房源指代表达归一化为对齐 PRD 示例的"这套房（house_id: xxx）"式显式引用，产出 `CorefRef(house)` 引用元数据；组合根将其注入 Story 6.1 的 `CoreferenceFilter(resolver=...)` 钩子，过滤器本体与信封契约不动。配套评测 harness（内置合成种子集 + CSV 语料模式）承载 FR-11 的 ≥95% 准确率验收口径。

## Boundaries & Constraints

**Always:** 手写纯 Python、同步、resolver 为纯函数（无 I/O、无 Redis、无会话历史读取，先行词锚点仅取 `envelope.house_id`——信封必填字段，即会话单一房源）；唯一允许的内联标注格式为全角括号显式引用"这套房（house_id: {house_id}）"，同一消息内所有指代出现处全部替换；`resolved_message` / `coref` 仍由 coreference 过滤器经 `model_copy(update=...)` 唯一写入（resolver 只返回 `(resolved, refs)`，不碰信封）；同消息同房源的多条命中在 `coref` 列表去重为单条 house 引用（ref_count ∈ {0, 1}）；命中判定走"归一化"而非开放式实体链接，不过问指代是否真指该房源（会话已锚定单一房源）；过滤器 6.1 的 fail-safe 契约原样复用——resolver 任何异常回落透传、买家应答零影响。

**Never:** 不实现序列标注或 LLM 改写（OQ-5 选型前规则模板即默认实现，选型后组合根整体替换 resolver）；不改 `filters/coreference.py` 过滤器本体契约、`intent_router` 及任何下游（其已按 AD-11 消费 `resolved_message`）；不做跨会话/跨房源指代；非房源实体（价格/楼层/位置）的独立指代不归一化、不产对应 `CorefRef`——保留原文走既有降级路径；不虚构人工标注语料冒充 95% 验收门；不新增买家可见变化、不触发转人工。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 期望输出 / 行为 | 错误处理 |
|------|------------|----------------|----------|
| 单指代命中 | house_id=h1，message="它采光怎么样" | resolved="这套房（house_id: h1）采光怎么样"，coref=[CorefRef(house, h1)] | N/A |
| 多指代共存 | "这套采光好吗？它朝向呢" | 两处全部替换为同一显式引用，coref 去重为单条 house 引用 | N/A |
| 指代嵌价格问句 | "它的价格呢"（"它"为房源指代） | "这套房（house_id: h1）的价格呢"，coref=[house 引用]——house 锚定覆盖其价格/楼层/位置复合问法 | N/A |
| 无指代 | "今天方便看房吗" | resolved 逐字节等于原文，coref=[]（正常恒等，非 resolver_error） | N/A |
| 非房源独立指代 | "那层采光呢"、"那个价位呢" | 原文保留、coref=[]——MVP 降级路径，不阻塞不转人工 | N/A |
| 空白 / None message | 空串、纯空白、None | 过滤器 6.1 守卫不调用 resolver；resolver 自身对空串亦返回恒等（防御纵深） | N/A |
| resolver 自身异常 | 意外 bug | 过滤器既有 fail-safe：回落透传、resolver_error=True、买家应答零影响 | 6.1 机制原样承接 |

</intent-contract>

## Code Map

- `src/weiliao_agent/filters/coreference.py` -- Story 6.1 过滤器全文：resolver 钩子契约 `Envelope -> (resolved, refs)`、空白守卫（resolver 永不收到空白消息）、refs 经 `CorefRef.model_validate` 复核、异常回落透传（resolver_error=True）；本 Story 过滤器本体零改动，仅组合根注入 resolver。
- `src/weiliao_agent/models/coref.py` -- `CorefRef` 权威定义：house 类型必填 `entity_id`；resolver 产 house 引用时传 `entity_id=envelope.house_id` 即天然合规。
- `src/weiliao_agent/models/envelope.py:96-101` -- `resolved_message` / `coref` 挂点与 `house_id` 必填（min_length=1）定义：resolver 锚点直接取信封字段，信封即唯一真相源。
- `src/weiliao_agent/main.py:545-580` -- 组合根过滤器装配区：`CoreferenceFilter(coref_writer=_session_manager.write_coref)` 现状，本 Story 在此追加 `resolver=RuleBasedCorefResolver()` 注入（仅此一行装配改动）。
- `src/weiliao_agent/filters/intent_router.py:117-124` -- 下游消费口径（AD-11 回落读原文）：只读证据，禁止改动。
- `tests/eval_intent_classifier.py` -- 评测 harness 范本（Story 2.2）：内置合成种子集冒烟 + CSV 语料模式 + 真实验收门 deferred 说明，本 Story 镜像其结构。
- `tests/test_coreference.py` -- 过滤器测试范本（fakeredis fixture、`_envelope()` 工厂、fake_langfuse 断言）；链序锁定用例（filter_names 断言）在本文件与 `tests/test_pipeline.py:197`，均自建 orchestrator，本 Story 只增不改。
- `_bmad-output/planning-artifacts/epics.md:538-560` -- Story 6.2 验收口径原文（≥95%、≥20% 指代类占比、降级路径）。
- `_bmad-output/implementation-artifacts/epic-6-context.md` -- Epic 约束：内联标注格式唯一、P99 < 10ms、锚定会话单一房源、不做跨房源。

## Tasks & Acceptance

**Execution:**
- `src/weiliao_agent/coref/__init__.py` + `src/weiliao_agent/coref/rule_based.py`（新）-- `RuleBasedCorefResolver`：可调用类（`__call__(envelope) -> tuple[str, list[CorefRef]]`），内置房源指代表达模式表（覆盖"它/这个/这套/那套/该房/这套房/那套房子"等；「上面说的」类前缀短语随其后的量词名词命中，替换点以 Golden 示例为准），命中即替换为"这套房（house_id: {house_id}）"全角显式引用并产出单条 house `CorefRef`；无命中返回 `(原文, [])`；纯函数零 I/O -- 默认消解实现，OQ-5 后可整体替换。
- `src/weiliao_agent/main.py` -- 组合根 `CoreferenceFilter(resolver=RuleBasedCorefResolver(), coref_writer=...)` 注入 -- 真实消解接入生产链。
- `tests/test_rule_based_coref.py`（新）-- I/O 矩阵全场景单测：单/多指代、嵌价格问句、无指代恒等、非房源降级、空串防御、全角括号格式逐字节断言、同消息去重、house_id 直取信封 -- 规则行为回归网。
- `tests/test_coreference.py` -- 补集成用例：真实 resolver 注入过滤器的端到端场景（小王场景"它采光怎么样"全链路经 filter 产出 resolved/coref/审计 payload 形状）-- 钩子与默认实现的接线验证。
- `tests/eval_coref_resolver.py`（新）-- 评测 harness：内置合成种子集（指代类样本占比 ≥ 20%）冒烟；CSV 语料模式（列 `text,has_house_coref`，可选 `expected_resolved` 逐字节比对），提供 CSV 即执行 ≥95% 验收门（不达标 exit 1）；无 CSV 时打印"人工标注语料与 FR-1 同期建设、真实验收门 deferred"说明并 exit 0 -- FR-11 准确率口径的承载体。
- `_bmad-output/implementation-artifacts/deferred-work.md` -- 追加条目：人工标注消解语料未就绪，FR-11 ≥95% 真实验收门 deferred（语料与 FR-1 测试集同期建设后复跑本 harness）-- deferral 留痕。

**Acceptance Criteria:**
- Given 信封已绑定 house_id 且消息为"它采光怎么样"，when 买家消息经链首 coreference 过滤器，then `resolved_message` 为"这套房（house_id: {house_id}）采光怎么样"、`coref` 恰含一条 entity_id 等于该 house_id 的 house 类型 `CorefRef`、审计键写入含同一引用的 JSON，intent_router 及下游零改动即消费消解后文本。
- Given 消息不含房源指代（含"那层/那个价位"等非房源独立指代），when 执行消解，then `resolved_message` 逐字节等于原文、`coref == []`，不阻塞、不转人工，由分类置信度与 UNKNOWN 既有路径承接。
- Given 同消息多处房源指代，when 执行，then 全部出现处替换为显式引用且 `coref` 去重为单条 house 引用（ref_count=1）。
- Given 内置种子评测集，when `uv run python tests/eval_coref_resolver.py`，then harness 冒烟通过、指代类占比 ≥ 20% 有校验、真实语料缺失时打印 deferred 说明并以 exit 0 退出。
- Given 全量回归，when `uv run pytest tests/ -q`、`uv run ruff check src tests`、`uv run mypy src`，then 全部通过（仓库 mypy strict 口径）、既有行为零变化（过滤器 fail-safe 语义、链序锁定、审计口径不变）。

## Spec Change Log

## Review Triage Log

### 2026-11-10 — Review pass
- verdicts: 22 findings — high 0, medium 3, low 12, false 7, maybe-false 0
- findings:
  - `[medium]` `[patch]` 盲#1：非房源守卫过窄——`这个周末方便看房吗` 被误改写为 house 显式引用（守卫名词表仅价格/楼层/位置类，`这套` 等无守卫）— 属实：`这个/那个` 后非名单名词即替换；已 patch——`_NON_HOUSE_NOUNS` 增补 周末/小区/中介/合同/物业/餐，守卫判定前剥離 tail 的「的」与标点，补 3 个恒等测试
  - `[low]` `[patch]` 盲#2：守卫仅 immediate-follow 生效，名词类未覆盖（`那个小区/物业/中介/合同` 未保护）— 部分属实：`那层楼被改写`的举例不成立（`那层` 不在模式表），但未保护名词类属实，与盲#1 同根同 patch 修复
  - `[low]` `[patch]` 盲#3：CSV 模式不校验指代类占比 ≥20%，偏斜语料可空过 ≥95% 门 — 属实：占比是验收口径的一部分；已 patch——CSV 模式与种子集同门槛，不足即 exit 1，并有 pytest 锁定
  - `[low]` `[defer]` 盲#4：P99 < 10ms 延迟预算无自动化验证 — 属实但仓库无压测设施（6.1#15 同例已 defer）；已录 frontmatter deferred 列表，归 Story 6.3/FR-1 端到端验收
  - `[low]` `[patch]` 盲#5：`None` 消息返回 `("", [])` 与过滤器恒等拷贝 `None` 的差异未写明 — 属实但不可达（过滤器守卫保证 `None` 不进 resolver）；已 patch——docstring 写明两层防御口径，测试保留
  - `[low]` `[patch]` 盲#6：模式表缺 此房/此套/这套住房/该房屋/本房源 等书面语变体，无排除说明 — 属实（静默漏命中走恒等降级，危害低）；已 patch——补入模式表并加测试
  - `[low]` `[patch]` 盲#7：`它` 负向守卫仅查单字符；守卫与命中共存场景无测试 — 单字符部分无实证坏结果（`其它/其他/它们` 已覆盖），混合场景缺测试属实；已 patch——补 `那个价位呢？它朝向呢` 共存测试
  - `[false]` `[reject]` 盲#8：其他 6.1 时代组合根测试可能残留恒等断言 — 反驳：已全仓审计，其余组合根用例（test_session.py:575/600/675）只断言 intent/L1/L0/degraded 键，不断言 resolved_message 恒等；全量 735 绿佐证
  - `[low]` `[patch]` 盲#9：harness 退出码不一致、非法行静默跳过无计数 — 属实（开发者向 ergonomics）；已 patch——utf-8-sig、OSError 友好退出、skipped_invalid_rows 计数入报告
  - `[false]` `[reject]` 盲#10：规格两日志为空 — 流程过程态（同 6.1#12 裁决）：本 triage 即回填 Review Triage Log 与 Auto Run Result
  - `[low]` `[patch]` 盲#11：跨房源比较 `这套比那套贵吗` 退化自比较，未留痕 — 属实：spec Never 排除跨房源指代但输出形态未文档化；已 patch——docstring 已知限制段 + 测试锁定当前行为
  - `[medium]` `[patch]` 边#1：模式表缺 `这间房/那间房/该房子` 致截断损坏（`…（house_id: h）房还有吗`）— 属实（比静默漏命中更坏：产出损坏文本）；已 patch——补全长形式 8 个，长度降序机制天然保证 precedence，补截断回归测试
  - `[low]` `[patch]` 边#2：`那个的价位呢`（的间隔）守卫失效 — 属实，与盲#1 同根（守卫剥離缺失）；同 patch 修复并有专测
  - `[low]` `[patch]` 边#3：输入已含 `（house_id: x）` 标注时双重标注 — 属实但需买家原样输入内部格式，日常不可达；修复为直接单条件守卫，已 patch 加测试
  - `[low]` `[patch]` 边#4：CSV 路径不存在抛裸 traceback — 属实（语料作者向）；已 patch——OSError 转 SystemExit 友好信息
  - `[low]` `[patch]` 边#5：UTF-8 BOM（Excel 默认）致 header 误拒 — 属实；已 patch——`utf-8-sig` 读取，pytest 锁定
  - `[medium]` `[patch]` 验#1：harness 验收门契约（exit 0/1、占比校验）不在任何自动化验证路径 — 预验证属实：pytest 永不执行 eval_*.py，exit code 被翻转全仓仍绿；已 patch——新建 tests/test_eval_coref_resolver.py 锁定种子冒烟/门通过/门失败/占比不足/BOM/缺文件六条契约
  - `[false]` `[reject]` 意#1：`无法确定先行词` 语义面被归一化窄化 — 反驳：`<intent-contract>`（人工批准）即采 R2 归一化解读，Golden 示例与守卫矩阵已将其操作化，非缺陷
  - `[false]` `[reject]` 意#2：裸 `上面说的还有吗` 不归一化 — 反驳：spec Tasks 明定「『上面说的』类前缀短语随其后的量词名词命中，替换点以 Golden 示例为准」，无命中走恒等降级在契约内
  - `[false]` `[reject]` 意#3：FR-11 ≥95% 验收面被 deferral 挪移 — 反驳：spec 明令不虚构人工标注语料、harness 即验收口径承载体，deferral 经 deferred-work.md 留痕可溯源
  - `[false]` `[reject]` 意#4：`不阻塞不转人工` 被继承而非新测 — 反驳：resolver 异常回落由 6.1 既有 raising-resolver 用例覆盖（本 Story 过滤器本体零改动），真实 resolver 为无状态纯函数无可注入故障面
  - `[false]` `[reject]` 意#5：下游消费仅由未改动性建立 — 反驳：AC1 的验收面即过滤器产出的信封字段（已测）；下游 retrofit 属 Story 6.3 范围（epics.md:562-578 明列）

## Design Notes

- Golden 示例（逐字节口径）："它多少钱" → "这套房（house_id: h1）多少钱"；"这套采光怎么样" → "这套房（house_id: h1）采光怎么样"；"上面说的那套还有吗" → "上面说的这套房（house_id: h1）还有吗"（模式命中"那套"即替换，前缀"上面说的"自然保留）。
- "它"在单一房源会话中直接按房源指代处理，是"归一化而非开放式实体链接"的落地：不过问真实先行词，只做模板命中后的自包含改写；误命中代价低（改写仍指向会话房源，语义不偏移），漏命中走恒等降级。
- coref 单消息去重为单条 house 引用：同消息命中 N 处只描述一个实体锚点，ref_count ∈ {0, 1} 让 6.1 的 `record_coref` 观测口径（resolver_error / ref_count）保持极简可告警。
- 非房源实体不做独立归一化：CorefRef 的 price/floor/location 类型为 OQ-5 选型后扩展留口，MVP 规则对"那层/那个价位"无可靠锚点，按 Epic 约束"失败按同一路径降级"保留原文。
- 性能：模式表为正则/字典序单趟扫描，纯 Python 微秒级，P99 < 10ms 预算天然满足，无需异步化。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警
- `uv run mypy src` -- expected: 零告警（仓库 mypy strict 口径，仅扫 src）
- `uv run python tests/eval_coref_resolver.py` -- expected: 种子集冒烟通过，打印真实语料 deferred 说明，exit 0

## Auto Run Result

Status: done

**实现变更摘要：** 交付可插拔规则模板默认 resolver `RuleBasedCorefResolver`（`src/weiliao_agent/coref/`，纯函数零 I/O、锚定 `envelope.house_id`），把房源指代归一化为"这套房（house_id: xxx）"全角显式引用并产出单条去重 house `CorefRef`；组合根注入 Story 6.1 过滤器钩子（过滤器本体与信封契约零改动）；评测 harness `tests/eval_coref_resolver.py` 承载 FR-11 ≥95% 验收口径（真实语料门 deferred 并留痕 deferred-work.md）。

**文件清单：**
- `src/weiliao_agent/coref/__init__.py`（新）-- 包导出，OQ-5 替换留口
- `src/weiliao_agent/coref/rule_based.py`（新）-- 规则模板 resolver：模式表（含全长形式）、非房源守卫（名词类 + 剥離）、`它` 负向守卫、双重标注防御、跨房源已知限制说明
- `src/weiliao_agent/main.py` -- 组合根 `CoreferenceFilter(resolver=RuleBasedCorefResolver(), ...)` 注入
- `tests/test_rule_based_coref.py`（新）-- 规则行为矩阵单测（Golden 逐字节、守卫、降级、防御纵深）
- `tests/test_coreference.py` -- 真实 resolver 过滤器集成用例（小王场景 + 审计 payload）
- `tests/eval_coref_resolver.py`（新）-- FR-11 评测 harness（种子冒烟 + CSV 验收门 + 占比校验 + BOM/错误输入卫生）
- `tests/test_eval_coref_resolver.py`（新）-- harness 契约 pytest 锁定（六条 exit-code/占比/BOM 契约）
- `tests/test_session.py` -- 组合根审计测试随真实 resolver 接线更新（payload 形状不变）
- `_bmad-output/implementation-artifacts/deferred-work.md` -- FR-11 真实验收门 deferral 留痕

**评审发现处理：** 22 条发现（medium 3 / low 12 / false 7）。patch 9 项全部修复：守卫窄化组（盲#1/盲#2/边#2，medium）、模式表截断组（边#1/盲#6，medium）、harness 契约自动化（验#1，medium）、CSV 卫生（盲#9/边#4/边#5）、CSV 占比校验（盲#3）、双重标注（边#3）、None 防御口径文档化（盲#5）、混合场景测试（盲#7）、跨房源退化留痕（盲#11）。defer 1 项：P99 延迟压测无设施（盲#4，同 6.1#15 先例，归 Story 6.3/FR-1）。reject（false）7 条：盲#8（陈旧断言——全仓审计反驳）、盲#10（流程态日志）、意#1-#5（intent-contract 已裁决或属 6.3 范围）。

**后续评审建议：** true。理由：本轮 patch 了 3 个 medium 条目（≥2 门槛）。具体未验证风险：评审补丁对 resolver 规则行为做了实质性扩充（守卫名词表 + 剥離逻辑、8 个新模式、双重标注守卫），虽均有测试锁定，但规则面变更与 spec 归一化口径的一致性值得一轮独立复核。

**验证记录：**
- `uv run pytest tests/ -q` — 735 passed（补丁后全量）
- `uv run ruff check src tests` — All checks passed
- `uv run mypy src` — Success: no issues found in 48 source files
- `uv run python tests/eval_coref_resolver.py` — SMOKE PASS 43/43，占比 0.419 ≥ 0.2，deferred 说明打印，exit 0

**残余风险：**
- ≥95% 真实验收门依赖人工标注语料（与 FR-1 同期建设，deferred-work.md 留痕）；规则模板对表外表达 recall 有限（漏命中走恒等降级，买家无感）。
- 跨房源比较消息（如"这套比那套贵吗"）按单房源锚定归一化会产生自比较形态，已在 docstring 已知限制段留痕并测试锁定；OQ-5 选型后由真实算法承接。
- P99 < 10ms 无压测验证（defer，见 frontmatter deferred 列表）。
