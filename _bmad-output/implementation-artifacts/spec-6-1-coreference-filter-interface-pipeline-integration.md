---
title: 'Story 6.1 指代消解过滤器接口与管道接入'
type: 'feature'
created: '2026-11-10'
status: 'done'
baseline_commit: 'fee5684'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** FR-11/AD-11 要求指代消解过滤器占据管道首位（intent_router 之前），但信封的 `resolved_message`/`coref` 只是占位字段（`coref` 尚为 `list[Any]`，`CorefRef` 模型未定义），`SessionManager.write_coref` 在代码里标注「仍待后续交付」，链中也没有该过滤器。OQ-5 真实消解选型（规则模板/序列标注/LLM 改写）第 2 周才定，接口边界必须先锁定，保证选型可插拔替换、接入不阻塞现有链路。

**Approach:** 定义权威 `CorefRef` 模型并收窄信封字段；新建 `filters/coreference` 过滤器（默认原文透传 + 可注入消解钩子），插入链首；coref 结果经 AD-4 单写者回调写入会话状态审计 key `weiliao:session:{session_id}:coref:{turn}`；透传场景自身延迟控制在 P99 < 10ms 预算内。

## Boundaries & Constraints

**Always:** 手写纯 Python、同步、不依赖 LangGraph Runtime（AD-2）；过滤器仅消费上游信封、经 `model_copy(update=...)` 产出下游信封；`CorefRef` 权威定义在 `models/`，信封是唯一真相源；写会话状态走组合根注入的 `SessionManager` 方法引用（AD-4），try 包裹 fail-safe——写失败不降级、不外抛、不阻塞，仅记 trace 事件；审计 key 仅作留痕，任何下游禁止反读 Redis 重建 coref；透传默认实现：`resolved_message` = 原文拷贝、`coref` = 空列表；过滤器自身延迟计入 FR-1 端到端预算。

**Never:** 不实现真实消解算法（归 Story 6.2 / OQ-5 选型后替换钩子）；不改 `intent_router` 及其下游（其已按 AD-11 读 `resolved_message` 回落 `message`，天然兼容）；不触发转人工、不产生任何买家可见变化；不改 `PipelineResult` 响应契约与既有 Redis key；不新增买家侧交互。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 期望输出 / 行为 | 错误处理 |
|------|------------|----------------|----------|
| 默认透传 | 任意文本消息，未注入 resolver | `resolved_message` = 原文、`coref` = `[]`，审计 key 写入 JSON | N/A |
| 钩子命中 | 注入 resolver 返回 `(resolved, refs)` | 信封携带钩子产出与 `CorefRef` 列表，审计写入 | 钩子抛异常 → 回落透传并记 trace |
| turn 缺失 | 信封 `turn` is None | 正常产出信封，跳过审计写（同 `_record_followup` 先例） | N/A |
| 审计写失败 | Redis 抛异常 | 信封照常下发，买家应答零影响 | fail-safe + trace 失败事件 |
| 过滤器自身异常 | 意外 bug | 编排器既有兜底接管：降级模板、`degraded=True` | 编排器 `_run_chain` 既有机制 |
| 媒体/空消息 | `message` 空 | 透传，`coref` = `[]` | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/models/envelope.py:96-101` -- `resolved_message`/`coref` 占位字段（docstring 明示待 AD-11 收窄）；本 Story 收窄 `coref` 为 `list[CorefRef] | None`。
- `src/weiliao_agent/models/` -- 无 coref 模型；新建 `coref.py` 承载 `CorefRef`。
- `src/weiliao_agent/filters/base.py:18-22` -- `Filter` ABC：`process(envelope) -> Envelope` + 类属性 `name`（降级归因用）。
- `src/weiliao_agent/pipeline.py` -- `_run_chain` docstring 上方锁定的链序；编排器逐跳超时/异常兜底，coreference 插链首无需动编排器代码。
- `src/weiliao_agent/filters/intent_router.py:104-111,117-122` -- AD-4 注入回调与 AD-11 消费口径先例；`_record_followup`（turn 非 None 前置 + fail-safe）写法照搬。
- `src/weiliao_agent/session/manager.py:46-47,314` -- `write_coref`「仍待后续交付」标注；`write_intent` 为 per-turn JSON SET + 随写刷新 EXPIRE（TTL :64）的最贴近先例。
- `src/weiliao_agent/main.py:545-580` -- 组合根过滤器装配区（`intent_writer=_session_manager.write_intent` 注入先例）；coreference 过滤器在此装配到链首。
- `src/weiliao_agent/observability/tracing.py:286` -- `record_classification`（latency_ms + fail-safe）为 `record_coref` 事件 helper 的封装先例。
- `tests/test_cache_lookup.py` -- 新过滤器测试范本（fakeredis 本地 fixture、`_envelope()` 工厂、fake_langfuse 事件断言、metrics 单例 autouse 重置）；`tests/test_session.py` -- manager 矩阵测试范本（TTL 过期、并发）。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/models/coref.py`（新）-- 定义 `CorefRef`：`entity_type`（枚举 house/price/floor/location/other）、`entity_id`（str\|None；house 类型必填校验）；信封 `coref` 字段收窄为 `list[CorefRef] | None` 并更新注释 -- 引用元数据权威模型。
- [x] `src/weiliao_agent/filters/coreference.py`（新）-- `CoreferenceFilter(Filter)`（`name = "coreference"`）：构造注入 `resolver: Callable[[Envelope], tuple[str, list[CorefRef]]] | None = None` 与 `coref_writer` 回调；默认透传产出新信封，`time.monotonic` 计 latency；审计写 try 包裹 fail-safe（turn 非 None 前置）-- 接口边界 + 链首透传。
- [x] `src/weiliao_agent/session/manager.py` -- 新增 `write_coref(session_id, turn, payload)`：`json.dumps(..., ensure_ascii=False)` + SET 带 EXPIRE 随写刷新 TTL，移除 docstring 待交付标注 -- AD-4 单写者落地审计 key。
- [x] `src/weiliao_agent/observability/tracing.py` -- 新增 `record_coref(trace_id, *, latency_ms, ref_count, audit_written)`：fail-safe no-op 事件 helper，封装口径同 `record_classification` -- 可观测锚点。
- [x] `src/weiliao_agent/main.py` -- 链首装配 `CoreferenceFilter(coref_writer=_session_manager.write_coref)`（无新 env 配置则不新增解析段）；核对 `filters/__init__.py` docstring -- 管道接入。
- [x] `tests/test_coreference.py`（新）+ `tests/test_session.py` 补 `write_coref` 用例 -- I/O 矩阵全场景（透传/钩子命中/钩子异常回落/turn None/审计失败/媒体消息）+ TTL 与 JSON 断言 + 链序锁定 -- 回归网。

**Acceptance Criteria:**
- Given 任意买家消息进入管道，when 通过过滤器链，then `coreference` 在链首执行（filter_names 顺序有测试锁定），位于 intent_router 之前，手写纯 Python。
- Given 默认实现（未注入 resolver），when 文本消息通过，then `resolved_message` 逐字节等于原文、`coref == []`、审计 key `weiliao:session:{sid}:coref:{turn}` 写入 JSON 且 TTL 随会话刷新。
- Given 注入 resolver 返回引用，when 执行，then 信封 `coref` 含校验通过的 `CorefRef`（house 类型必填 `entity_id`），审计写入；resolver 抛异常时回落透传且买家应答零影响。
- Given 审计写失败或 Langfuse 未配置，when 任一路径执行，then 应答、降级语义不变，仅 trace 侧留有失败/no-op 事件。
- Given 全量回归，when `uv run pytest tests/ -q`，then 全绿且无既有行为变化（intent_router 回落逻辑、编排器兜底、`PipelineResult` 契约逐字节不变）。

## Implementation Notes

- 全量验证：696 passed（33s）、ruff 零告警、mypy strict 零告警（subagent 与主会话双重确认）。
- 判断点（subagent 记录）：resolver 异常回落时仍以回落透传值写审计键（审计轨迹完整，trace 事件 ref_count=0 可观测回落）；`audit_written=False` 合并「turn 缺失跳过」与「写失败」两种留痕（规格签名只有单布尔，实现忠实于签名）。

## Spec Change Log

## Review Triage Log

评审轮 1（2026-11-10，三层：盲审/边猎/验证缺口，18 条发现）：

| # | 发现（来源） | 裁决 | 证据 / 去向 |
|---|------------|------|-----------|
| 1 | resolver 异常回落与恒等消解在 trace 事件不可区分（盲#1） | low → patch | 属实：ref_count=0 两种语义同口径；已 patch——record_coref metadata 增 resolver_error，仅异常回落为 True，测试锁定 |
| 2 | audit_written=False 混合「写失败/turn 缺失/writer 未注入」三态（盲#2） | low → patch | 属实：故障告警会被设计内跳过淹没；已 patch——增 audit_error 字段，仅 writer 抛异常为 True |
| 3 | latency_ms 计时不含同步 Redis SET，系统性低估（盲#3） | medium → patch | 属实：计时在 _write_audit 前停止；已 patch——计时移到审计写之后，docstring 改为同步写入口径 |
| 4 | Redis 同步 SET 无超时预算，慢Redis 拖住链首（盲#4） | medium → defer | 属实但 write_intent 自 2.x 同模式，非本 Story 引入；已录 deferred-work.md，归 Epic 7 可运维性 |
| 5 | 审计 payload 缺 trace_id，无法关联 Langfuse trace（盲#5） | low → patch | 属实：审计留痕缺追溯锚点；已 patch——payload 增 trace_id（None 直写），pinned 断言同步更新 |
| 6 | 编排器默认链 CoreferenceFilter() 无 writer 与组合根不一致（盲#6） | false | 默认过滤器不注入回调是仓库既有模式（IntentRouterFilter() 等同），AC 口径指组合根生产链，组合根端到端测试已锁定 |
| 7 | coref 收窄是兼容性断点，旧格式 envelope JSON 会 fail-closed（盲#7） | false | 验证缺口层已全仓库核实：仅 coreference 过滤器写 coref，无任何缓存/序列化路径存旧格式 envelope |
| 8 | 矩阵「过滤器自身异常」无测试、「媒体/空消息」未测 media_type（盲#8） | false | 编排器兜底由 test_pipeline 既有 _RaisingFilter 用例覆盖（机制与过滤器无关）；过滤器不读 media_type，媒体信封与 message=None 完全同路径 |
| 9 | resolver 返回值信任边界：未校验 refs 经 model_copy 流入信封，payload 构建在 try 外（盲#9、边#1、边#3 同根） | medium → patch | 边猎已实证（model_construct 的 house/None 被信封接受）；已 patch——refs 经 CorefRef.model_validate 逐复核纳入 resolver 同一 try，非法即回落透传，process 永不因 resolver 产出抛错 |
| 10 | 纯空白 message（"   "）为 truthy 会进入 resolver（盲#13、边#2 同根） | low → patch | 属实：已 patch——守卫改 not (message and message.strip())，补回归测试 |
| 11 | envelope docstring 仍将 message 标「架构预留字段」但实际已被消费（盲#10） | low → patch | 属实：已 patch——注释口径更新为买家原文（coreference 透传拷贝 / intent_router AD-11 回落均消费） |
| 12 | 规格 frontmatter 状态滞后、三节日志为空（盲#11） | false | 流程过程态：盲审读到的是 diff 内旧版规格；triage 时已置 in-review 并回填 Implementation Notes / 本 Triage Log |
| 13 | sprint-status 头部注释 last_updated 陈旧 + 6-1 状态未推进（盲#12） | low → defer（头部注释部分） | 头部注释陈旧先于本 Story 存在，已录 deferred-work.md；6-1 状态由本工作流在评审收口时推进，非缺陷 |
| 14 | coref 收窄在 HTTP 入口 model_validate 执行点零测试（验#1，预验证） | medium → patch | filed evidence 已证实可整体回退不红；已 patch——test_envelope.py 增合法接受/非法拒绝两用例 |
| 15 | P99 < 10ms 延迟预算无自动化验证路径（验#2，预验证） | medium(unverified) → defer | 属实但 P99 是压测属性、仓库无压测设施；归 Story 6.3/FR-1 端到端验收，已录 deferred-work.md |
| 16 | tracing 模块 docstring「coref（Story 2）恒等于原文」旧注失真（验#3 other） | low → patch | 本 Story 使该 claim 由空真变假；已 patch——更新为 6.1 实际口径 |

## Design Notes

- 接口边界即「可注入 resolver 钩子 + 信封两字段 + 审计回调」三件套；默认 resolver 为 None 时透传，是恒等消解。Story 6.2/OQ-5 选型落地时只需组合根替换 resolver，过滤器本体与信封契约不动。
- 透传也写审计 key（`resolved_message` = 原文、`coref` = `[]`）：让审计轨迹在 6.2 之前就完整存在，且让「信封 `coref` 为 None」保持「过滤器未执行」的语义。
- 延迟预算论证：透传路径仅字段拷贝 + 可选单次 Redis SET（fire-and-forget 语义，失败即弃），编排器 `filter_span` 每跳自动埋点，纯 Python 毫秒级可控。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警
- `uv run mypy src` -- expected: 零告警（仓库 mypy strict 口径，仅扫 src）
