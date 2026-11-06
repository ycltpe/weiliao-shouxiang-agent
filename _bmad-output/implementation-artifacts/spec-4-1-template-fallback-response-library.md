---
title: 'Story 4.1 模板兜底应答库'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'dispatch'
baseline_commit: 'cfe21d2c0ae7e5112d3e2e05dbe4b78abe1c1dfd'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 现有模板只是 Story 1.2 的占位实现，文案仍含“第一时间”等违禁表述；PHOTO_REQUEST 只有纯文本，无法在有图、有相册或无图时给出合适应答。

**Approach:** 将占位注册表升级为集中、同步、可被管道与后续审核/降级链路复用的模板库；保留现有字符串响应契约，并为图片请求提供链接、相册引导和无图话术。

## Boundaries & Constraints

**Always:**
- 以 `IntentTag` 的 9 类标签为唯一键来源；UNKNOWN 与通用兜底分别定义，缺失或运行时未知意图均 fail-closed 到通用兜底。
- 所有买家可见文本保留 `【AI 助手】` 前缀，不包含“最”“第一”、价格可谈、学区名额承诺或冒充经纪人的表述，也不伪造已执行的线下动作。
- 暴露同步接口；正常管道、HTTP 异常、Redis 故障统一复用。PHOTO_REQUEST 按非空图片链接、相册链接、无素材三种状态选择文案；素材只渲染到回复文本，不发起网络请求。
- 转人工分支不得消费本模板库，本 Story 不把转人工通知加入模板。
- 决策：本次实现并测试候选文案，不宣称已通过业务/合规确认；该确认保留为发布前人工门槛。

**Never:**
- 不实现 LLM、真实风控审核、OpenAI 降级或图片素材查询。
- 不改 `PipelineResult` 字段、过滤器顺序、2.5 秒预算、trace/degraded 语义及缓存行为。
- 不实现 Epic 5 的硬触发或转人工终态。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 意图兜底 | 任一合法意图 | 返回该意图的完整安全文案，带 AI 前缀 | 不依赖外部服务 |
| 未知意图 | None 或枚举外值 | 返回通用兜底，不泄露素材参数 | fail-closed |
| 有图片 | PHOTO_REQUEST + 非空图片链接 | 回复包含图片文案及原链接 | 空白链接按无素材处理 |
| 有相册 | PHOTO_REQUEST + 仅相册链接 | 回复包含相册引导及链接 | 空白链接按无素材处理 |
| 无图片 | PHOTO_REQUEST 且无有效素材 | 返回明确的无图替代话术 | 不谎称已索取或已发送 |
| 其他意图带素材 | 非 PHOTO_REQUEST + 素材参数 | 素材被忽略，仅返回对应意图文案 | 无额外副作用 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/filters/templates.py` -- 核心改造；复用 `AI_DISCLOSURE_PREFIX`，替换 `_INTENT_PLACEHOLDERS` 与 `render_placeholder()` 的占位口径。
- `src/weiliao_agent/models/intent.py` -- `IntentTag` 权威定义，只复用。
- `src/weiliao_agent/pipeline.py` -- 正常与 `_degraded()` 当前都调用 `render_placeholder()`；切换到新同步接口，结果结构不动。
- `src/weiliao_agent/adapters/http.py` -- `_fallback_result()` 复用同一接口；保留原有 trace_id 行为。
- `src/weiliao_agent/observability/tracing.py` -- 只消费最终回复，不修改。
- `tests/test_pipeline.py`、`tests/test_tracing.py`、`tests/test_session.py` -- 更新受新文案影响的断言，锁定既有管道、追踪与故障行为。
- `tests/test_templates.py` -- 新增模板矩阵、违禁表述及图片分支测试。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/filters/templates.py` -- 建立完整模板注册表与同步渲染接口，支持图片/相册/无图分支，并保留兼容包装。
- [x] `src/weiliao_agent/pipeline.py`、`src/weiliao_agent/adapters/http.py` -- 所有现有兜底入口改用新接口。
- [x] `tests/test_templates.py` 及受影响测试 -- 覆盖 I/O 矩阵、9 类完整性、文案安全约束和统一复用。

**Acceptance Criteria:**
- Given 任一调用方需要兜底，when 同步获取模板，then 与管道错误、后续审核失败和 OpenAI 故障可复用同一接口。
- Given 现有管道与 HTTP 故障场景，when 返回兜底，then trace、降级字段及非 5xx 行为不回归。
- Given 模板库交付，when 审查转人工相关代码，then 没有转人工分支调用模板库。

## Implementation Notes

## Dev Agent Record

### Implementation Plan

- 以 `IntentTag` 为注册表键类型，分别定义 UNKNOWN 与通用兜底；新增同步 `render_fallback()`，仅对 PHOTO_REQUEST 渲染图片或相册链接。
- 保留 `render_placeholder()` 作为完整委托的兼容包装；正常管道、管道降级和 HTTP 故障统一直接调用新接口。
- 通过行为矩阵、安全文案扫描和调用替身验证统一复用，不改动响应 schema、过滤器顺序、预算、trace 或缓存行为。

### Debug Log

- RED：新增测试初次收集失败，确认 `render_fallback` 尚不存在。
- GREEN：模板行为矩阵 20 项先行通过；切换管道与 HTTP 后，相关 78 项测试通过。
- 初次全量验证为 277 项测试通过、mypy 通过；ruff 检出模板注册表 8 处长行，拆行后复查通过。

### Completion Notes

- 完成 9 类意图与独立通用兜底的安全候选文案，所有输出保留 AI 前缀；空白素材按无素材处理，图片优先于相册，其他意图与未知意图不泄露素材。
- 正常、过滤器异常、Redis 故障入口均复用 `render_fallback()`；既有 trace、degraded、failed_filter、非 5xx 行为由回归测试锁定。
- 转人工相关代码未接入模板库；未实现素材查询、网络请求、LLM、审核或 Epic 5 行为。
- 评审补丁后最终验证：`pytest tests -q` 315 项通过；`ruff check src tests` 无告警；`mypy src` 对 32 个源文件无错误。素材链接校验、图片/相册无效回退及“空白图片 + 有效相册”用例均已覆盖。
- 本次交付仅完成候选文案及自动化约束验证，不代表已通过业务/合规确认；该确认仍是发布前人工门槛。

## File List

- `src/weiliao_agent/filters/templates.py` -- 修改：集中模板库、同步渲染接口与兼容包装。
- `src/weiliao_agent/pipeline.py` -- 修改：正常及降级结果改用 `render_fallback()`。
- `src/weiliao_agent/adapters/http.py` -- 修改：HTTP/Redis 故障兜底改用 `render_fallback()`。
- `tests/test_templates.py` -- 新增：模板矩阵、安全约束、图片分支、兼容包装及统一复用测试。
- `tests/test_pipeline.py` -- 修改：断言切换到新接口。
- `tests/test_tracing.py` -- 修改：最终下发文本断言切换到新接口。
- `tests/test_intent.py` -- 修改：未知意图通用兜底断言切换到新接口。
- `_bmad-output/implementation-artifacts/spec-4-1-template-fallback-response-library.md` -- 修改：任务、实施记录、文件清单、变更记录与状态。
- `_bmad-output/implementation-artifacts/sprint-status.yaml` -- 修改：Story 4.1 状态。

## Spec Change Log

- 2026-11-06：实现集中同步模板兜底库，切换现有调用入口并补齐行为、安全和回归测试。

## Review Triage Log

### 2026-11-06 评审轮（blind 9 项 / edge-case 2 项 / verification-gap 0 项）

- blind#1 图片分支当前无生产调用方 — **low / defer** — 核实生产入口确实只传 `intent`；冻结块明确不实现素材查询，素材接入属后续响应组装，记入 deferred-work 跟踪。
- blind#2 素材链接未校验 — **medium / patch（G1）** — 核实 `render_fallback()` 对调用方链接仅 `strip()` 后直接拼接，非法 scheme、控制字符或超长文本可进入买家可见文本。
- edge#1 素材可绕过违禁词扫描 — **medium / patch（G1）** — 该层已按其规则核实；静态扫描不覆盖动态链接，与 blind#2 同根因。
- edge#2 非 http(s) 链接可直接渲染 — **medium / patch（G1）** — 该层已按其规则核实；与 blind#2 同根因。
- blind#3 链接后紧跟中文句号 — **low / patch（G2）** — 核实 `_PHOTO_IMAGE_FALLBACK` 与 `_PHOTO_ALBUM_FALLBACK` 均把 `。` 放在 URL 后，复制或自动识别链接时可能带句号。
- blind#4 测试对“最”做裸子串断言 — **low / 拒绝** — 当前静态文案不含误伤词，风险是未来编辑提示不精确；修复需引入更复杂的语义词表，超过本 Story 的最小修正。
- blind#5 Code Map 提及 test_session.py 更新 — **low / 拒绝** — 核实该文件仅复用 AI 前缀、不渲染模板；按评审规则，修正方式为编辑本 Story 规格，不在本轮处理。
- blind#6 转人工禁用模板缺少自动化断言 — **low / 拒绝** — 已用代码搜索核实转人工/ handoff 相关模块均未消费模板；本 Story 冻结块明确转人工属 Epic 5，为不存在代码添加静态约束超出最小修正。
- blind#7 缺“空白图片 + 有效相册”测试 — **low / patch（G3）** — 核实现有参数化只覆盖双空或双空白，未锁定图片空白后回退相册的分支。
- blind#8 兼容包装无弃用计划 — **low / 拒绝** — 包装行为与已批准设计一致；增加告警或删除计划都会改变现有调用契约，意图未要求。
- blind#9 Epic 4 状态疑似超前 — **false / 拒绝** — sprint-status 工作流说明规定首个 Story 开始时 Epic 自动转 `in-progress`，Epic 1–3 均同口径。

## Design Notes

新接口命名为 `render_fallback()`；保留 `render_placeholder()` 作为兼容包装，避免旧调用方静默失效。图片链接优先于相册链接，二者都为空时使用无图文案；回复仍是字符串，不扩大 HTTP schema。

## Verification

**Commands:**
- `uv run pytest tests -q` -- 全部通过。
- `uv run ruff check src tests` -- 无告警。
- `uv run mypy src` -- strict 无错误。
