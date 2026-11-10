---
title: 'Story 6.3 下游消费接入与追问判定 retrofit'
type: 'feature'
created: '2026-11-10'
status: 'done'
baseline_revision: '6545095ba1e4fbb9b34e3512a2585efc7c718938'
baseline_commit: '6545095ba1e4fbb9b34e3512a2585efc7c718938'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
deferred:
  - summary: >-
      FR-1「含消解端到端意图分类 P99 < 50ms」实测 deferred——仓库无压测
      设施，重路由至 Story 7.2 压测设施执行真实测量（关闭 6.1#15 /
      6.2 盲#4 同根挂账）。
    evidence: |-
      分类器 45ms 硬超时守卫 + latency_ms 埋点已就位，coref 为微秒级
      单趟规则；与 6.1#15、6.2 盲#4 同裁决同根，deferred-work.md 已
      留痕并闭环旧条目。settle：Story 7.2 压测设施落地后以
      coreference → intent_router 全链执行 FR-1 P99 实测验收。
    severity: low
  - summary: >-
      _followup_anchor 序列化非单射：entity_id 含「:」或「|」时不同
      coref 引用集可碰撞为同一计数锚点——MVP 单 house 引用生产不可达，
      defer 至 OQ-5 选型故事一并加固。
    evidence: |-
      CorefRef.entity_id 仅 min_length=1 无字符集限制；碰撞需单消息
      多引用（MVP resolver 去重为单条 house 引用，恒锚定 envelope.
      house_id），生产不可达。settle：OQ-5 widen CorefRef 生产（非
      house 类型/多引用）时须同步加固锚点序列化（转义或长度前缀）。
    location: >-
      src/weiliao_agent/filters/intent_router.py:324-337
    severity: low
---

<intent-contract>

## Intent

**Problem:** 链首 coreference 过滤器（Story 6.1/6.2 已交付真实 resolver）就位后，`resolved_message` 在生产链上已必然有值，但两个决策性下游仍是"resolved 优先、原文回落"的双轨口径（`intent_router.py:117-123`、`llm_generate.py:71-76`）——买家原文仍是一条合法的决策输入路径，与"意图分类（FR-1）、LLM 生成（FR-6）只消费消解后消息，禁止直接读取买家原始文本做决策"的口径相悖；追问判定（Story 2.3）纯按意图标签计数，`coref` 实体锚定比较（FR-2 口径："意图标签为准，辅以 coref 实体锚定比较"）机制缺位；PRD 小王场景缺少管道级端到端验证。

**Approach:** 把 intent_router 与 llm_generate 的决策输入收窄为纯 `resolved_message`（缺失/空白走既有 fail-closed UNKNOWN / 跳过生成语义，原文回落路径移除）；追问计数键从 per-tag 扩为 per-(tag, coref 锚点)，锚点由信封 `coref` 列表机械导出（MVP 即 house 的 `entity_id`，空 coref 走哨兵值，行为向后兼容）；补管道级端到端用例（小王场景 + FR-2 指代对）。

## Boundaries & Constraints

**Always:** 意图分类与 LLM 生成的决策输入唯一来源为 `resolved_message`；`resolved_message` 缺失或空白时 intent_router 走既有 fail-closed UNKNOWN、llm_generate 走既有跳过语义，任何路径不得读取 `envelope.message` 做决策；追问判定仍以分类器意图标签为准、禁止任何文本相似度比对，`coref` 仅用于计数键锚定不参与相似度；会话写入只经 `SessionManager` 单写者（Lua 原子单 roundtrip、TTL 随 EXPIRE 刷新口径不变）；trace 埋点（`intent.followup` / hard trigger 信号）与 `is_followup` 阈值（per-intent ≥2）口径不变；买家可见链路（response_assembler、http 转写）零改动。

**Never:** 不引入文本相似度比对；不改 coreference 过滤器本体、信封模型与 `CorefRef` 定义；不做 OQ-5 选型、不扩展非 house 类型的独立归一化（锚点导出口径为通用 (entity_type, entity_id) 序列，MVP 只有 house）；不动 45ms 分类硬超时守卫、红线意图跳过与 media/空消息 skip 语义；不改 `write_intent`/`write_coref` 签名；不落地压测设施——FR-1"含消解端到端意图分类 P99 < 50ms"的实测无仓库设施（与 6.1#15、6.2 盲#4 同根），本 Story 以 deferral 留痕重路由至 Story 7.2 的压测故事关闭挂账，不虚构测量。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 期望输出 / 行为 | 错误处理 |
|------|------------|----------------|----------|
| resolved 与原文并存 | resolved="这套房（house_id: h1）多少钱"，message="它多少钱" | 分类器收到的文本逐字节等于 resolved；LLM prompt 同口径 | 无 |
| resolved 缺失 | resolved=None，message 有值 | intent_router fail-closed UNKNOWN（分类器不被调用），llm_generate 跳过生成 | 既有 fail-closed，追问零副作用 |
| resolved 空白 | resolved=" " 或 "" | 同上：UNKNOWN / 跳过生成 | 同上 |
| 同锚同 tag 二现（FR-2 指代对） | 轮1"它多少钱"（anchor=h1），轮2"这套房多少钱"（anchor=h1），分类 tag 相同 | 轮2 `is_followup=True`，per-(tag,anchor) 计数=2——两消息计为同一意图 | 无 |
| 异锚同 tag | tag 相同，coref 锚定 h1 vs h2 | 各自独立计数，互不计为追问 | 无 |
| coref 空 | 无指代消息（如"今天方便看房吗"） | 哨兵锚，计数行为与既有 per-tag 口径一致（向后兼容） | 无 |
| recorder 抛 RedisError | 分类成功 | 应答不降级、`is_followup=False`、trace 记 `followup_record_failed` | 2.3 fail-safe 原样承接 |

</intent-contract>

## Code Map

- `src/weiliao_agent/filters/intent_router.py:117-123` -- 决策输入双轨回落点（本 Story 移除原文分支）；`:138` 分类器调用；`:144-146` intent 写回；`:184` `_record_followup` 调用点；`:192-242` 追问判定全文（当前只读 session_id/turn/tag，不读 coref）；`:28` 模块 docstring 已钉死"禁止文本相似度比对"。
- `src/weiliao_agent/filters/llm_generate.py:71-76` -- 生成输入双轨回落点（本 Story 移除原文分支，空白 resolved 走既有 skip）；`:113-125` `_generate` 传参子图。
- `src/weiliao_agent/session/manager.py:103-121` -- `_LUA_RECORD_FOLLOWUP` 原子脚本（HINCRBY `followup:{TAG}`、EXPIRE 刷新）；`:234` `record_followup(session_id, tag)` 签名（本 Story 扩 anchor 参数）；`:332-337` `write_coref`（冻结签名，纯审计）。
- `src/weiliao_agent/models/envelope.py:96-109` -- `resolved_message`/`coref`/`is_followup` 字段定义，只读。
- `src/weiliao_agent/models/coref.py` -- `CorefRef`（`entity_type`/`entity_id`），锚点导出依据。
- `src/weiliao_agent/pipeline.py:107-123` + `tests/test_pipeline.py:194-201` -- 链序锁定：`coreference → intent_router → cache_lookup → llm_generate → risk_review → response_assembler`。
- `tests/test_intent.py:36` -- 信封夹具当前仅 `message`（回落移除后需补 `resolved_message`）；`:128-138` resolved 优先用例；`:244-267` `_followup_payload`/`_StubFollowupRecorder`/`_RaisingFollowupRecorder` 替身模式；`:273-394` 追问矩阵七用例。
- `tests/test_session.py:390-492` -- `record_followup` fakeredis 矩阵（隔离/TTL/并发）；`:43-48` fakeredis fixture；`:595` 组合根接线用例。
- `tests/test_llm_generate_filter.py:165-188` -- 双轨回落锁定断言（需改写为纯 resolved 口径）；`:223-226` 双空透传。
- `tests/test_coreference.py:382` -- 小王场景 coref 层端到端范本（真实 resolver + 真实 SessionManager.write_coref + fakeredis），本 Story 在其上加管道级用例。
- `_bmad-output/implementation-artifacts/deferred-work.md` -- 6.2 挂账的 FR-11/P99 条目归宿（重路由至 Story 7.2）。
- `_bmad-output/implementation-artifacts/spec-6-2-*.md` -- 前置契约：coref 单消息去重单条 house 引用（anchor 导出由此简化）、过滤器 fail-safe、审计口径。

## Tasks & Acceptance

**Execution:**
- `src/weiliao_agent/filters/intent_router.py` -- 决策输入收窄为纯 `resolved_message`（移除 `else envelope.message` 回落；None/空白 → 既有 fail-closed UNKNOWN 路径，注释口径从"交付前退化"更新为 FR-11 终态）；`_record_followup` 从信封 `coref` 机械导出锚点字符串（`(entity_type, entity_id)` 排序拼接，空列表 → 哨兵值如 `none`）并随 tag 传入 recorder -- 决策消费唯一化 + FR-2 锚定接入。
- `src/weiliao_agent/session/manager.py` -- `record_followup(session_id, tag, anchor)`：Lua 计数键 `followup:{TAG}` → `followup:{TAG}:{anchor}`，原子性/TTL 刷新/双计数返回语义不变；docstring 更新锚定口径 -- per-(tag, anchor) 计数权威值。
- `src/weiliao_agent/filters/llm_generate.py` -- 移除原文回落（`resolved_message` None/空白 → 既有跳过生成语义原样承接，不产生草稿），注释口径同步 -- FR-6 纯消解输入。
- `tests/test_intent.py` -- 夹具补 `resolved_message`；新增"分类器只见消解文本"用例（classifier spy 逐字节断言 resolved、原文存在时零接触）；resolved 缺失/空白 → UNKNOWN 且分类器零调用；追问锚定矩阵：同锚同 tag 二现 → `is_followup=True`（"它多少钱"/"这套房多少钱"计为同一意图）、异锚同 tag → 独立计数、空 coref → 哨兵锚向后兼容、recorder 异常 fail-safe 原样绿 -- 行为锁定网。
- `tests/test_session.py` -- `record_followup` 锚定键 fakeredis 用例：per-(tag,anchor) 隔离、异锚不互相累计、哨兵锚行为、TTL 刷新、并发精确 -- Redis 层锚定语义锁定。
- `tests/test_llm_generate_filter.py` -- 双轨回落断言改写为纯 resolved 口径（空白 resolved → skip、原文字段在场也不读取）；双空透传用例随新语义更新 -- 生成输入锁定。
- `tests/test_pipeline.py` -- 管道级端到端：真实 `CoreferenceFilter(resolver=RuleBasedCorefResolver(), coref_writer=...)` + stub 分类器 + fakeredis SessionManager 装配 orchestrator：小王场景"它采光怎么样" → 信封 resolved 含 house 显式引用、intent=stub tag、应答组装成功；FR-2 指代对两轮"它多少钱"→"这套房多少钱" → 第二轮 `is_followup=True` -- PRD 场景与 FR-2 口径的端到端验收面。
- `_bmad-output/implementation-artifacts/deferred-work.md` -- 追加条目：FR-1"含消解端到端意图分类 P99 < 50ms"实测 defer 至 Story 7.2 压测设施（论据：coref 为微秒级单趟规则、分类器 45ms 硬超时守卫 + `latency_ms` 埋点已就位，与 6.1#15/6.2 盲#4 同裁决同根），关闭 6.2 挂账 -- deferral 留痕闭环。

**Acceptance Criteria:**
- Given 信封 `resolved_message` 与 `message` 并存（链首 coreference 已写入 resolved），when intent_router 与 llm_generate 执行，then 分类器与生成 prompt 收到的文本逐字节等于 `resolved_message`，`envelope.message` 在任何决策路径零读取（spy/prompt 断言锁定）。
- Given `resolved_message` 缺失或空白，when intent_router / llm_generate 执行，then 分别 fail-closed UNKNOWN（分类器零调用）与跳过生成（无草稿、信封透传），追问计数零副作用。
- Given 同 session 同 intent tag 且 coref 锚定同一 house，when 轮1"它多少钱"后轮2"这套房多少钱"经完整管道，then 轮2 信封 `is_followup=True`、Redis `followup:{TAG}:{h1}`=2——两消息计为同一意图（FR-2 口径）。
- Given 同 intent tag 但 coref 锚定不同 house，when 追问判定，then 两消息各自独立计数、互不计为追问。
- Given 无指代消息（coref 空），when 追问判定，then 计数行为与既有 per-tag 口径逐点一致（哨兵锚向后兼容，既有 2.3 测试语义不回归）。
- Given recorder 抛 `redis.RedisError`，when 分类成功，then 应答不降级、`is_followup=False`、trace 记失败事件（2.3 fail-safe 原样）。
- Given 全量回归，when `uv run pytest tests/ -q`、`uv run ruff check src tests`、`uv run mypy src`，then 全部通过（strict 口径）、既有行为零回归（链序、审计、hard trigger 信号口径不变）。

## Spec Change Log

## Review Triage Log

### 2026-11-10 — Review pass
- verdicts: 21 findings — high 0, medium 2, low 8, false 11, maybe-false 0
- findings:
  - `[false]` `[reject]` 意#1：FR-11 回落条款被 R1b 实质改写 — 反驳：epic-6-context 技术决策明文"意图分类（FR-1）、LLM 生成（FR-6）只消费消解后消息，禁止直接读取买家原始文本做决策"；FR-11 降级（保留原文）由链首 coreference 过滤器 fail-safe 承接（透传 resolved 逐字节等于原文），决策行为等价
  - `[false]` `[reject]` 意#2：追问判定"基于消解后消息"的文本面期望 vs 元数据面实现 — 反驳：epic 明文"以意图分类输出的意图标签为准，辅以 coref 实体锚定比较，禁止直接对消解后消息做文本相似度比对"——diff 精确实现该口径
  - `[false]` `[reject]` 意#3：FR-1 P99 验收从测量面后移到文书面 — 已按 6.1#15/6.2 盲#4 同根裁决 defer 并留痕 deferred-work.md（重路由 Story 7.2），spec Never 明令不虚构测量；非未处理缺口
  - `[false]` `[reject]` 意#4：端到端链面收窄（stub 分类器、无 cache/llm/risk）— 仓库既有 e2e 口径如此（6.2 小王场景 coref 层端到端、2.3 组合根用例均 stub 分类器）；故事验收面（resolved 进分类、应答组装、FR-2 判同）已由真实 resolver + 真实 SessionManager + 管道装配覆盖
  - `[false]` `[reject]` 盲#1："口径不变"声称 vs per-(tag,anchor) 行为分歧 — 生产不可达：会话锚定单一房源、MVP resolver 恒产单条 house 引用（锚点恒为会话 house），跨锚场景不存在；阈值/信号机制对所有可达状态不变
  - `[false]` `[reject]` 盲#2：锚定口径 hard-trigger 阈值无跨锚测试 — 跨锚累计生产不可达（同盲#1）；既有跨意图哨兵测试即生产形态（单锚），Zeno 层同裁决（no-demonstration）
  - `[low]` `[defer]` 盲#3：frontmatter deferred:[] 与 P99 deferral 矛盾 — 已在前置 deferred 列表补 FR-1 P99 条目（机器可读留痕）
  - `[low]` `[patch]` 盲#4：deferred-work 旧挂账条目未标注关闭 — 已 patch：6.1 条目加 closed-2026-11-10 状态行；6.2 盲#4 原未入台账，补录同状态行
  - `[low]` `[patch]` 盲#5：manager.py 模块 docstring Story 2.3 段落仍写 followup:{TAG} — 已 patch：更新为 followup:{TAG}:{anchor} 锚定键口径
  - `[low]` `[patch]` 盲#6：trace metadata 键 per_intent_count 名实漂移 — 已 patch：改名 per_tag_anchor_count（签名/metadata/docstring/调用点/测试断言同步）
  - `[false]` `[reject]` 盲#7：旧键格式读侧未审计 — 反驳：rg 全仓验证无代码直读 followup:{TAG} 字段；5.1 硬触发消费内存返回值（trigger.py:118-138），快照只读 followup_count
  - `[false]` `[reject]` 盲#8：hash 字段基数无界 — 生产单锚点：字段数 ≤ 9 tag × 1 锚 + 哨兵，有界；多锚场景不可达
  - `[medium]` `[patch]` 盲#9：fail-closed 路径 assessor 输入未锁定（与验#1 同根）— 已 patch：两个 fail-closed 用例注入 recording assessor，原文在场时断言收到 None/空白 resolved
  - `[false]` `[reject]` 盲#10：_followup_anchor 顺序无关无单测 — sorted() 保证顺序无关，所述坏结果不发生；单引用生产使多引用/None-id 形态不可达
  - `[false]` `[reject]` 盲#11：LLM 半边无端到端 — spec 管道任务未承诺装配 llm_generate；AC1 prompt 半边由单测 chat spy 逐字节锁定、分类器半边管道级锁定，组合覆盖
  - `[low]` `[patch]` 盲#12：cast(Any) 绕过 Filter 接口 — 已 patch：_EnvelopeRecorder 继承 Filter，删除 cast
  - `[false]` `[reject]` 盲#13：两个日志为空 — 流程态（同 6.2 盲#10 先例）：本 triage 即回填 Review Triage Log，Spec Change Log 仅 bad_spec 循环时写入
  - `[low]` `[defer]` 边#1：锚点序列化非单射（entity_id 含 :/| 时碰撞）— 属实但 MVP 不可达（单引用生产）；注入面随 OQ-5 widen CorefRef 生产才打开，spec Never 已把非 house 生产排除至 OQ-5——defer 至 OQ-5 故事
  - `[low]` `[patch]` 边#2：envelope.py 两处陈旧回落注释（与验#2 同根）— 已 patch：更新为 6.3 终态口径（注：spec 原标 envelope 只读，此 patch 仅改注释、不改契约，为回落删除的孤儿文档最小修正）
  - `[medium]` `[patch]` 验#1：assessor message 实参无 spy 锁定（预验证采纳，同盲#9）— 同 patch：recording assessor 断言
  - `[low]` `[patch]` 验#2：envelope.py 陈旧注释（同边#2）— 同 patch

## Design Notes

- 移除原文回落是安全的：生产默认链 coreference 恒在链首且 fail-safe 恒产出 `resolved_message`（透传场景 resolved 逐字节等于原文，含空白守卫），回落分支在生产链上不可达；单元测试中 standalone 装配的信封由夹具显式补 `resolved_message`，使"决策输入唯一"成为可测契约而非隐含假设。
- 追问锚点从信封 `coref` 字段机械导出（排序拼接 `(entity_type, entity_id)`，空列表 → 哨兵），不读 Redis 审计键、不回溯历史——锚定的唯一真相源是信封，与 6.1"信封即唯一消费路径"一致；MVP resolver 单消息至多一条 house 引用，锚点导出天然简单，OQ-5 扩展非 house 类型时口径不变。
- 向后兼容设计：空 coref 走哨兵锚，等价于旧 per-tag 键语义演进，不引入第二条判定路径。
- 硬触发评估 `_assess_handoff` 与分类器共用同一输入变量，收窄后硬触发文本口径自动一致，无需独立改动。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增锚定/端到端用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警
- `uv run mypy src` -- expected: 零告警（strict 口径）

**Manual checks:**
- 人工走查 `intent_router.py` / `llm_generate.py` 全文确认无 `envelope.message` 决策性读取残留（留痕性消费：pipeline trace、http 转写、handoff 载荷，正确且不动）。

## Auto Run Result

Status: done

**实现变更摘要：** intent_router 决策输入收窄为纯 `resolved_message`（原文回落移除，None/空白 → fail-closed UNKNOWN，分类器零调用）；llm_generate 同步移除原文回落（空白 resolved → 跳过生成）；追问计数键扩为 per-(tag, anchor)，锚点由信封 coref 机械导出（排序拼接，空列表/None → 哨兵 `none`），Lua 原子语义/TTL/双计数返回不变；补管道级端到端用例（小王场景 + FR-2 指代对）。

**文件清单：**
- `src/weiliao_agent/filters/intent_router.py` — 纯 resolved 决策输入 + `_followup_anchor` 导出 + recorder 三参调用
- `src/weiliao_agent/filters/llm_generate.py` — 原文回落移除，空白跳过语义承接
- `src/weiliao_agent/session/manager.py` — `record_followup(session_id, tag, anchor)`，Lua 键 `followup:{TAG}:{anchor}`；模块 docstring 锚定口径（评审补丁）
- `src/weiliao_agent/observability/tracing.py` — metadata 键 `per_intent_count` → `per_tag_anchor_count`（评审补丁，名实一致）
- `src/weiliao_agent/models/envelope.py` — 两处陈旧回落注释更新为 6.3 终态口径（评审补丁，仅注释）
- `tests/test_intent.py` — 夹具补 resolved；原文零接触/缺失/空白矩阵；锚定矩阵（同锚/异锚/哨兵/fail-safe）；recording assessor 锁定 fail-closed 路径输入（评审补丁）
- `tests/test_session.py` — 锚定键 fakeredis 矩阵六用例 + 组合根锚定断言
- `tests/test_llm_generate_filter.py` — 纯 resolved 口径改写（含原文在场不读取）
- `tests/test_pipeline.py` — 小王场景 + FR-2 指代对管道级端到端；`_EnvelopeRecorder` 继承 Filter（评审补丁）
- `tests/test_classifier.py` / `tests/test_handoff_trigger.py` — 夹具补 resolved_message、recorder 三参签名
- `_bmad-output/implementation-artifacts/deferred-work.md` — FR-1 P99 deferral 条目（重路由 Story 7.2）+ 6.1/6.2 挂账 closed 标注（评审补丁补录 6.2 条目）

**评审发现处理：** 21 条发现（medium 2 / low 8 / false 11）。patch 6 项全部修复：assessor fail-closed 输入锁定（盲#9+验#1，medium）、deferred-work 台账闭环（盲#4）、manager docstring 锚定口径（盲#5）、telemetry 键名实一致（盲#6）、_EnvelopeRecorder 接口 conformance（盲#12）、envelope 陈旧回落注释（边#2+验#2）。defer 2 项（已录 frontmatter deferred）：FR-1 P99 实测（盲#3，重路由 Story 7.2）、锚点序列化注入面（边#1，OQ-5 时一并加固）。reject（false）11 条：意#1-#4（epic 明文裁决/仓库先例覆盖）、盲#1/2/8/10（生产单锚点不可达）、盲#7（rg 验证无旧键读侧）、盲#11（AC1 组合覆盖）、盲#13（流程态日志，同 6.2 盲#10 先例）。

**后续评审建议：** false。本 pass patched 条目 medium 1 组（无 high），未达 ≥2 medium 门槛；所有补丁均为测试锁定/文档一致性修正，行为面无未验证变更。

**验证记录：**
- `uv run pytest tests/ -q` — 752 passed（补丁后全量）
- `uv run ruff check src tests` — All checks passed
- `uv run mypy src` — Success: no issues found in 48 source files
- I/O 矩阵七行覆盖审计：每行均有测试锁定且在本轮全量中运行通过

**残余风险：**
- FR-1 P99 < 50ms 实测依赖 Story 7.2 压测设施（deferred 留痕）；当前保障为 45ms 分类硬守卫 + coref 微秒级规则。
- 锚点序列化对含分隔符的 entity_id 非单射，MVP 单引用生产不可达；OQ-5 widen CorefRef 生产时须同步加固（deferred 留痕）。
- 硬触发负面词扫描现消费 resolved_message（与分类器同口径）；生产中 resolved 与原文仅差指代归一化片段，属 spec 接受的口径一致化。
