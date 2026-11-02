---
title: 'Story 2.1 意图标签模型与分类器接口'
type: 'feature'
created: '2026-11-02'
status: 'done'
followup_review_recommended: false
baseline_commit: '2db3d35a405109a4132b5a7037ff77b206d85333'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-2-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-test-2026-10-22/ARCHITECTURE-SPINE.md'
deferred:
  - summary: >-
      分类器异常失败路径（except Exception → UNKNOWN）完全静默，无日志/Langfuse 事件
    evidence: |-
      真实但当前不可达：占位分类器不存在，该路径仅在 Story 2.2 真实分类器接入后才有观测价值；
      观测挂点（过滤器内分类失败事件）需与 2.2 的 Langfuse trace 口径一并设计，非直接纠正。
    location: >-
      src/weiliao_agent/filters/intent_router.py
    severity: low
---

<intent-contract>

## Intent

**Problem:** 信封 `intent` 现为自由 `str | None`，9 类意图枚举无类型层定义，分类器无接口边界——实现选型（规则引擎 / gpt-4o-mini zero-shot / 微调小模型，PRD OQ-1 第 2 周定）将无法在不改路由链路代码的前提下替换。

**Approach:** 在 `models/` 定义 9 类 `IntentTag` 单一 Literal 枚举与分类结果模型，收窄信封 `intent` 字段；在 `intent_router` 过滤器定义 `IntentClassifier` Protocol（输入消息文本 + 会话上下文，输出单一标签 + 置信度）并以依赖注入挂到过滤器，默认无实现时保留现有占位行为。

## Boundaries & Constraints

**Always:**
- 意图标签为单一 `Literal` 类型枚举（9 类）：GREETING / PRICE_INQUIRY / AVAILABILITY / LOCATION / PHOTO_REQUEST / NEGOTIATION / LOAN_POLICY / COMPLAINT / UNKNOWN。
- 分类器接口仅接收消息文本与会话上下文，输出单一标签 + 置信度（AD-3 单意图路由）。
- 全链路 `model_validate` 校验：未知意图值 fail-closed——信封校验拒绝，端点层兜底模板文案。
- 过滤器契约不变：仅消费/产出信封；异常 fail-closed 为 UNKNOWN，过滤器自身不向上抛。
- `mypy src` strict 与 `ruff check src tests` 保持干净；既有测试全部不回归。

**Never:**
- 不实现任何真实分类算法与默认实现选型（Story 2.2）；不接入 Redis 会话写意图（Story 2.2 键 `weiliao:session:{session_id}:intent:{turn}`）。
- 不改管道编排、超时预算、降级兜底语义；不改动 `adapters/http.py` 的 fail-closed 结构。
- 信封不新增字段（六字段契约：session_id/house_id/turn/intent/timestamp/trace_id）——置信度不进信封。
- 热路径不引入 LangGraph/LangChain 依赖（AD-2）；不动 `templates.py` 文案内容与 AI 披露前缀。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 合法标签入信封 | payload `intent` 为 9 类之一 | `Envelope.model_validate` 通过，`intent` 为该标签 | 无错误 |
| 未知意图值 | payload `intent` 为 "DISCOUNT" 等非枚举字符串 | `model_validate` 抛 `ValidationError`；`POST /v1/messages` 返回 200 + 通用兜底文案 + 新 trace_id（fail-closed） | 端点层既有 ValidationError 兜底捕获 |
| 注入分类器正常 | `IntentRouterFilter(classifier=stub)`，stub 返回 `IntentClassification(tag=GREETING, confidence=0.9)` | `process` 产出信封 `intent="GREETING"` | 无错误 |
| 分类器抛异常 | 注入的 classifier `classify` 抛任意异常 | `process` 不抛，信封 `intent="UNKNOWN"`（fail-closed），信封其余字段透传 | 异常吞没降级为 UNKNOWN |
| 未注入分类器 | `IntentRouterFilter()`（默认） | 缺失 `intent` 置 UNKNOWN 的占位行为逐字节保留（既有测试锁定） | 无错误 |
| 置信度越界 | `IntentClassification(confidence=1.5)` | 构造抛 `ValidationError` | Pydantic 边界校验拒绝 |

</intent-contract>

## Code Map

- `src/weiliao_agent/models/envelope.py` -- `Envelope.intent: str | None`（约第 44 行）待收窄为 `IntentTag | None`；docstring 已预留收窄说明（"9 类 Literal 枚举在 Story 2.1 定义后收窄"），同步更新为完成口径。
- `src/weiliao_agent/models/__init__.py` -- 仅一行 docstring；新增 intent 模型的导出位置。
- `src/weiliao_agent/filters/intent_router.py` -- 占位过滤器（缺失 intent 置 UNKNOWN）；本 Story 增加 `IntentClassifier` Protocol 与注入构造，是分类器可替换性（AC 第 4 条）的落点。
- `src/weiliao_agent/filters/templates.py` -- `_INTENT_PLACEHOLDERS`（9 类键 + UNKNOWN 值）与 `render_placeholder(intent: str | None)`；键类型可收窄为 `IntentTag`，文案内容只读不动。
- `src/weiliao_agent/filters/base.py` -- `Filter` 抽象基类；`IntentRouterFilter` 已继承，无需改动，仅作契约参照。
- `src/weiliao_agent/pipeline.py` -- 编排器第 31 行导入 `IntentRouterFilter`、默认链构造约第 46 行；本 Story 不改动，验证默认链行为不回归的参照点。
- `src/weiliao_agent/adapters/http.py` -- `post_message` 中 `Envelope.model_validate` 的 `ValidationError` 兜底（约第 72 行）即未知意图 fail-closed 的消费端；`_fallback_result` 签名随 `intent` 类型收窄微调类型标注，结构不动。
- `tests/test_pipeline.py` -- 第 187 行 `test_default_chain_intent_router_defaults_missing_intent_to_unknown` 与第 200 行 9 类文案遍历锁定占位行为，必须原样通过。
- `tests/test_envelope.py` -- 信封校验用例（intent 合法值 "PRICE_INQUIRY"、None 两例），收窄后语义不变。

## Tasks & Acceptance

**Execution:**
- `src/weiliao_agent/models/intent.py` -- 新建：`IntentTag = Literal[...]`（9 类，按 epics AC 顺序）；`IntentClassification` Pydantic 模型（`tag: IntentTag`，`confidence: float` 约束 0.0–1.0），全链路 `model_validate` 口径与信封一致 -- 数据格式约定的唯一权威定义。
- `src/weiliao_agent/models/__init__.py` -- 导出 `IntentTag` 与 `IntentClassification` -- 包级统一入口。
- `src/weiliao_agent/models/envelope.py` -- `intent` 收窄为 `IntentTag | None`，docstring 更新为完成口径 -- 未知值校验拒绝即"fail-closed 到模板兜底"的类型层实现。
- `src/weiliao_agent/filters/intent_router.py` -- 定义 `IntentClassifier` Protocol（`classify(message: str, context: ...)` → `IntentClassification`，`context` 具体形态由实现自定，接口仅承诺"消息文本 + 会话上下文"）；`IntentRouterFilter` 增加可选构造注入 `classifier: IntentClassifier | None = None`；注入时调用分类并写回信封 `intent`，分类异常降级 UNKNOWN；未注入时现行为不变 -- 实现可替换、切换无需改路由链路代码（AC 第 4 条）。
- `src/weiliao_agent/filters/templates.py` -- `_INTENT_PLACEHOLDERS` 键声明为 `IntentTag`、`render_placeholder` 参数收窄为 `IntentTag | None`；键值文案零改动 -- 类型对齐，行为不变。
- `src/weiliao_agent/adapters/http.py` -- `_fallback_result` 的 `intent` 参数类型标注随收窄微调（`str | None` → `IntentTag | None`）；其余零改动 -- 类型一致性。
- `tests/test_intent.py` -- 新建：I/O 矩阵六场景全覆盖（合法 9 类逐个校验、未知值拒绝、注入 stub 分类写回、分类异常降级 UNKNOWN、默认占位行为、置信度越界拒绝）-- 验收载体。

**Acceptance Criteria:**
- Given 信封 payload 的 `intent` 为 9 类枚举之一，when `Envelope.model_validate`，then 校验通过且 `intent` 保留该标签。
- Given `POST /v1/messages` payload 的 `intent` 为枚举外字符串，when 请求到达端点，then 返回 200、`degraded=True`、通用兜底文案、新 trace_id，不进入管道。
- Given `IntentRouterFilter` 注入了返回 `IntentClassification(tag, confidence)` 的分类器，when `process` 执行，then 产出信封 `intent` 等于分类标签，其余字段透传。
- Given 注入的分类器 `classify` 抛异常，when `process` 执行，then 信封 `intent="UNKNOWN"` 且过滤器不向上抛异常。
- Given 未注入分类器的默认过滤器链，when 处理 `intent=None` 的信封，then 行为与既有测试锁定口径逐字节一致（UNKNOWN 兜底文案）。
- Given `uv run pytest tests -q`，when 全量执行，then 既有 39 例 + 新增用例全部通过，零回归。

## Spec Change Log

（空——首个评审循环前由 step-04 填充）

## Review Triage Log

### 2026-11-02 — Review pass
- verdicts: 13 findings — high 0, medium 2, low 7, false 4, maybe-false 0
- findings:
  - `[medium]` `[patch]` 空字符串消息绕过无文本守卫（与 edge-case #1 同根因分组）— 守卫 `if message is None` 对 `""` 不生效（信封 message/resolved_message 无 min_length），分类器收到空文本，违反 fail-closed 契约；edge-case 层实测复现 — patch：守卫改 `if not message`，补空串用例（已修复）。
  - `[low]` `[defer]` 分类器失败路径完全静默（无日志/Langfuse 事件）— 真实但当前不可达：占位分类器不存在，失败路径仅在 Story 2.2 真实分类器接入后才有观测价值；观测挂点需与 2.2 trace 口径一并设计，非直接纠正 — defer 至 frontmatter deferred。
  - `[false]` `[reject]` 回退买家原文违反 Epic 2「只消费消解后消息」— 驳回：消解过滤器（AD-11）尚未交付，回退是文档化过渡口径，切换点由 test_intent.py 的 resolved_message 优先用例 + 模块 docstring 钉死，非静默变更。
  - `[low]` `[patch]` 缺「分类器为权威」覆盖用例（preset intent 被分类结果覆盖未测）— patch：补 preset PRICE_INQUIRY → 分类写回 GREETING 用例（已修复）。
  - `[false]` `[reject]` Spec Change Log / Review Triage Log 占位文本过时 — 驳回：triage log 即在本评审 pass 填充；change log 按模板约定仅在 bad_spec 回环时填充，当前无回环。
  - `[low]` `[patch]` ALL_TAGS 手抄 9 类枚举可与 Literal 权威定义漂移 — patch：改 `typing.get_args(IntentTag)` 派生（已修复）。
  - `[false]` `[reject]` Protocol context 契约欠规范（键仅 docstring 声明）— 驳回：spec 明确 context 形态由实现自定，TypedDict 收紧违背该开放性设计；过滤器供给的四键已有测试断言锁定。
  - `[low]` `[patch]` classify 协议方法体缺显式 `...` — patch：补上（已修复）。
  - `[low]` `[patch]` 分类异常路径透传断言不全（仅 session_id/trace_id）— patch：补齐 house_id/turn/timestamp，与 happy path 一致（已修复）。
  - `[medium]` `[patch]` 空串消息进分类器（edge-case 层，与 blind-hunter #1 同根因分组）— 同组同路由，已随该条目一并修复。
  - `[low]` `[patch]` `.tag` 访问在 try 块外，非一致性返回的 AttributeError 逃出 process() — patch：tag 提取与信封写回移入 try，任何非一致性返回 fail-closed UNKNOWN（已修复）。
  - `[low]` `[patch]` `model_copy(update=...)` 跳过校验，枚举外 tag 可静默写入信封 — patch：写回改经 `Envelope.model_validate`，补 model_construct 非法 tag 用例（已修复）。
  - verification-gap 层：无发现（I/O 矩阵六场景与既有测试覆盖均已核验在跑且通过）。
  - `[false]` `[reject]` intent 对齐审计（描述性，无行动项）：diff 落在 R1+R2 读法；R3（配置选择机制）由 epics AC 原文延期（「切换方式在第 2 周选型时确定」），spec Design Notes 明示不动组合根——无规格外偏离。

## Design Notes

选择 `typing.Protocol` 而非 ABC 定义分类器接口：实现方（规则引擎 / LLM zero-shot / 微调模型）本就异构，Protocol 允许其保持任意继承结构，符合"可替换而不影响路由链路"。注入点放在 `IntentRouterFilter` 构造参数而非配置模块硬注册：路由链路代码因此零感知切换，"通过配置选择"由组合根（如 `main.py`，本 Story 不动）日后装配时兑现，Story 2.2 提供默认实现时亦无需回改本接口。

信封收窄的类型传导：`IntentTag` 是 `str` 子类型，`tracing.py` 等处 `intent: str | None` 标注无需改动即可接收；仅对外显式消费意图枚举的 `render_placeholder` 与 `_fallback_result` 做标注对齐。

## Implementation Notes（Story 2.1 实现回填）

- `IntentClassifier.classify` 的 `context` 落实为 `Mapping[str, Any]`（过滤器供给 session_id / house_id / turn / trace_id 的只读映射）；实现方上下文形态仍可在组合根装配时自行约定，接口承诺不变。
- 过滤器分类输入取 `resolved_message` 优先、缺省退化 `message`（指代消解过滤器交付前）；无文本（None 或空串——信封对 message 无 min_length 约束）时不调分类器，直接 fail-closed UNKNOWN（I/O 矩阵未列该场景，按过滤器契约补充）。
- 注入分类器后一律以分类结果写回 `intent`（分类器为权威）；未注入时占位行为逐字节保留。
- 分类结果写回经 `Envelope.model_validate` 全量校验（非 `model_copy` 旁路）：非一致性分类器返回的枚举外 tag（如经 model_construct 绕过校验）或异常返回（None / 无 .tag）一律 fail-closed UNKNOWN，过滤器自身不向上抛。

## Verification

**Commands:**
- `uv run pytest tests -q` -- expected: 全部通过（既有 39 例 + 新增），零回归。
- `uv run ruff check src tests` -- expected: 无告警。
- `uv run mypy src` -- expected: strict 模式无错误。

## Auto Run Result

- Summary: 交付 9 类 `IntentTag` Literal 枚举与 `IntentClassification`（标签+0.0–1.0 置信度）权威模型；信封 `intent` 收窄为 `IntentTag | None`（未知值 model_validate 拒绝 → 端点层 fail-closed 模板兜底）；`IntentClassifier` Protocol 经构造注入挂入 `IntentRouterFilter`（分类写回信封、异常/空文本/非法标签一律 fail-closed UNKNOWN，过滤器不向上抛）；未注入时占位行为逐字节保留。
- Files changed:
  - `src/weiliao_agent/models/intent.py`（新建）-- 9 类 Literal 枚举 + 分类结果模型
  - `src/weiliao_agent/models/envelope.py` -- `intent` 收窄 + docstring 完成口径
  - `src/weiliao_agent/models/__init__.py` -- 导出 `IntentTag` / `IntentClassification`
  - `src/weiliao_agent/filters/intent_router.py` -- `IntentClassifier` Protocol + 注入构造 + fail-closed 写回
  - `src/weiliao_agent/filters/templates.py` / `src/weiliao_agent/adapters/http.py` -- 类型标注对齐，行为零改动
  - `tests/test_intent.py`（新建）-- I/O 矩阵六场景 + 评审补丁新增 4 例
  - `_bmad-output/implementation-artifacts/epic-2-context.md`（新建）-- Epic 2 编译上下文
- Review breakdown: 13 findings — 6 项 patch 全部应用（0 high / 1 medium 空串守卫 / 5 low）；1 项 low defer（分类失败路径静默观测）；4 项 false 驳回（原文回退"违规"、日志占位过时、context 欠规范、intent 对齐无偏离）；verification-gap 层零发现。
- Follow-up review recommendation: false（本 pass patch 无 high，medium 条目仅 1 个，未达 2 个阈值）。
- Verification: `uv run pytest tests -q` → 73 passed（既有 50 例零回归 + 新增 23 例）；`uv run ruff check src tests` → All checks passed；`uv run mypy src` → strict 无错误（24 文件）。
- Residual risks: 分类器失败路径无观测挂点（defer，Story 2.2 接入真实分类器时一并设计）；"通过配置选择"的装配机制留待组合根（Story 2.2 选型落地时兑现，spec Design Notes 已明示）。
