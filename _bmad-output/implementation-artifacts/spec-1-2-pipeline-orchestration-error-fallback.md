---
title: 'Story 1.2 管道过滤器编排与统一错误兜底'
type: 'feature'
created: '2026-10-31'
status: 'done'
route: 'dispatch'
baseline_commit: 'a36c1d519d1dce8ddb5b1bece068892f271b74a2'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-1-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-weiliao-shouxiang-agent-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 信封与目录骨架已就位，但过滤器链不存在：消息无法在 intent_router → cache_lookup → risk_review → response_assembler 间流转，任一过滤器将来出现异常时买家将暴露于 5xx 或服务无应答。

**Approach:** 手写纯 Python 编排器串起四过滤器占位实现，统一异常捕获 + 端到端 deadline 逐跳预算，异常/超时一律降级至占位模板兜底（转人工分支除外），使兜底机制本 Story 即可独立验收，不依赖 Epic 4 模板库。

## Boundaries & Constraints

**Always:**
- 过滤器链严格为 intent_router → cache_lookup → risk_review → response_assembler；过滤器仅消费上游信封、产出下游信封，禁止跨过滤器直接函数调用（AD-1）。
- 热路径手写纯 Python，不依赖 LangGraph Runtime（AD-2）；本 Story 四过滤器为占位实现（透传/最小行为），具体能力由后续 Epic 填充。
- 任一过滤器抛异常 → 捕获并降级模板兜底，跳过剩余过滤器；错误应答携带 trace_id；禁止未审核内容发买家（占位过滤器输出视为已审核）。
- 占位模板集：9 类意图各一条安全文案 + 1 条通用兜底；intent 缺失或未知值时用通用兜底。
- 编排器持有端到端 deadline（默认内部预算 2.5s，NFR-1 P99 < 3s 留出口余量，可配置），逐跳检查剩余预算，预算耗尽立即兜底，不得傻等全量超时。
- 兜底机制可独立验收：测试注入假异常/假慢过滤器即可验证全路径。
- 决策（Open Questions 已决）：服务入口为最小 FastAPI 同步端点 `POST /v1/messages`（请求-响应式，Story 1.4 的 /health 日后挂同一 app）；无效输入在端点层 fail-closed，返回兜底应答 + 新生成 trace_id，非 5xx。
- 决策（Open Questions 已决）：全部买家可见占位文案（含兜底应答）携带固定 AI 身份披露前缀（"AI 助手"式），Story 4.1 完整模板库替换时继承该约定。

**Never:**
- 不实现真实意图分类、缓存、风控、LLM 能力（占位透传）。
- 不实现转人工分支（AD-7 兜底除外条款，属 Epic 5），不实现 coreference 过滤器（AD-11，属 Epic 2/后续）。
- 不实现 Kafka 接入适配器；HTTP 入口仅 `POST /v1/messages` 一个端点，不含鉴权/限流等横切能力。
- 不修改信封模型字段口径；时区/非空约束按 deferred-work 留待 Story 1.3/1.5。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | 合法信封入管道 | 逐跳产出信封，最终返回占位应答（含 trace_id，degraded=False） | N/A |
| FILTER_RAISES | intent_router（或任一过滤器）抛异常 | 捕获后返回兜底应答（含 trace_id，degraded=True），剩余过滤器不执行 | 异常不向上抛，记录 failed_filter |
| DEADLINE_EXCEEDED | 某过滤器执行超过剩余预算 | 立即触发兜底应答，不傻等；总耗时不超内部预算 | 同上，记录 slow_filter 与超时跳 |
| INVALID_INPUT | dict 缺必填字段/类型错误 | 不进入管道；端点层 fail-closed 返回兜底应答 + 新生成 trace_id，HTTP 非 5xx | ValidationError 由端点层捕获 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/models/envelope.py` -- Envelope 模型（只读；本 Story 不改字段口径）
- `src/weiliao_agent/filters/__init__.py` -- 仅包 docstring；四个过滤器模块本 Story 新建
- `src/weiliao_agent/pipeline.py` -- 新建：编排器（本 Story 核心交付）
- `pyproject.toml` -- pytest asyncio auto、ruff/mypy strict 已配；不新增依赖
- `tests/test_envelope.py` -- 既有单测风格参照（模型级直接断言）
- `_bmad-output/implementation-artifacts/deferred-work.md` -- 信封时区/非空两项缺口明确留 1.3/1.5，本 Story 不结算
- `ARCHITECTURE-SPINE.md` AD-1/AD-2/AD-7/AD-8 与"错误处理"一致性约定 -- 全部约束来源

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/filters/base.py` -- Filter 抽象基类（name + process(envelope) -> Envelope）-- AD-1 统一契约
- [x] `src/weiliao_agent/filters/intent_router.py`、`cache_lookup.py`、`risk_review.py`、`response_assembler.py` -- 占位实现（透传/最小行为，逐跳产出合法信封）-- 链就位
- [x] `src/weiliao_agent/pipeline.py` -- 编排器：顺序调用 + 异常捕获 + deadline 逐跳预算（ThreadPoolExecutor future.result(timeout=剩余预算)，超时结果丢弃）+ 兜底路由 -- AC 核心
- [x] `src/weiliao_agent/filters/templates.py` -- 占位模板注册表：9 意图各一条 + 通用兜底，intent 缺失/未知回退通用 -- 独立验收载体
- [x] `src/weiliao_agent/adapters/http.py` + `src/weiliao_agent/main.py` -- 最小 FastAPI 端点 POST /v1/messages，ValidationError → 兜底应答 + 新 trace_id，HTTP 非 5xx -- "不返回 5xx"字面落点
- [x] `tests/test_pipeline.py` -- I/O 矩阵四场景：happy / 单过滤器异常 / 慢过滤器超时 / 无效输入 -- AC 可独立验收

**Acceptance Criteria:**
- Given 信封模型与过滤器目录已就位，当编排四过滤器链且过滤器间仅经信封传递，则链可执行完成，热路径纯 Python、无 LangGraph Runtime 依赖
- Given 任一过滤器抛异常，当管道执行，则买家侧收到模板兜底应答且携带 trace_id，服务不返回 5xx，未执行过滤器被跳过
- Given 某过滤器执行逼近端到端预算，当剩余预算不足，则编排器立即触发兜底、不傻等全量超时，兜底应答在 P99 < 3s 预算内
- Given 占位模板集就位，当任一意图（含缺失/未知）需兜底，则存在对应安全文案，错误兜底机制可独立验收

### Review Findings

- [x] [Review][Patch] 超时/预算耗尽路径 `slow_filter=filter_.name` 未做兜底（过滤器子类未覆写 name 时为空串，降级事件归因丢失），与第 133 行逃逸路径 `getattr(...) or type(...).__name__` 口径不一致 [src/weiliao_agent/pipeline.py:306,319]
- [x] [Review][Patch] spec Verification 声明 `uv run ruff check .` 预期无告警，与仓库实际矛盾（全仓 302 存量告警）；应收窄为 `ruff check src tests`（1.3/1.4/1.5 已用收窄口径）[spec-1-2 Verification]

## Implementation Notes

## Spec Change Log

## Review Triage Log

### 2026-10-31 审查轮（三层：blind-hunter 12 项 / edge-case-hunter 5 项 / verification-gap 0 缺口 + 1 其他）

- blind#1 envelope.py 不在 diff、契约未审 — **false** — Envelope 是 Story 1.1 交付物（已 in-review），edge-case 层实读确认 `intent: str | None` 与本 Story 各分支可达；冻结块禁止本 Story 改其口径。拒绝。
- blind#2 异常静默吞、无日志 — **low / 拒绝** — 可观测性（traceback 落盘与追踪）为 Story 1.5 Langfuse 脚手架的明确范围，本 Story spec 未要求日志；临时日志将与 1.5 方案重复建设。
- blind#3 + edge#3 + edge#5（同根因：端点层无兜底网）— **medium / patch** — 已核验：`orchestrator_.run()` 逃逸异常（无外层防护）→ HTTP 500；畸形 JSON → FastAPI 默认 422（非 5xx 但无兜底应答与 trace_id），均违反"买家永不暴露 5xx + 兜底应答"承诺。已修：`RequestValidationError` 全局 handler 返回兜底+新 trace_id（200）；`run()` 调用外包 try/except Exception → 兜底应答。
- edge#1 `run()` 在 `future.result` 之外的异常可逃逸 — **low / patch** — 已核验：除 `future.result` 的窄 except 外无外层防护，`filter_.name` AttributeError 等路径可逃逸，与矩阵"异常不向上抛"约定不符。已修：`run()` 循环外层加 try/except Exception 统一兜底（归因用 getattr 防二次 AttributeError）。
- edge#2 worker 线程 `BaseException` 逃逸 — **false** — 捕获 `BaseException` 会吞掉投递到主线程的 `KeyboardInterrupt`（result() 等待期间），破坏关停，提议修复比现状更糟；worker 过滤器收到 SystemExit/KeyboardInterrupt 无可演示路径。拒绝。
- edge#4 `Filter.name` 裸注解，缺名子类在降级路径 AttributeError — **low / patch** — 已核验：`name: str` 无默认值，mypy strict 不强制子类赋值。已修：基类默认 `name: str = ""`，管道兜底归因再加 getattr 双保险。
- blind#4 意图字面量四处重复、无共享枚举 — **low / 拒绝** — 9 类集合已被测试钉死（选择键测试枚举全部 9 类，漂移即红）；信封 docstring 明确 Literal 枚举由 Story 2.1 收窄时统一，现引入枚举属提前设计。拒绝。
- blind#5 每请求新建线程池、超时线程残留积累 — **maybe-false / defer** — 线程积累的危害依赖"过滤器可超时"这一前提；占位过滤器生产环境均为微秒级透传，超时仅存在于注入的测试替身，积累场景当前不可达。若真实过滤器（Epic 4）落地后为真，预计 **medium（未核验）**；裁定方式：Epic 4 接入真实过滤器后做超时风暴压测，重审执行器生命周期（共享池/cancel/信封所有权）。
- blind#6 过滤器产出不校验类型 — **false** — spec 设计注明确"逐跳不重验信封……重验属浪费"；mypy strict 编译期强制契约；端点兜底网（blind#3 修复）兜住残余逃逸。拒绝。
- blind#7 信封未 frozen、超时线程可变更多次引用的信封 — **low / defer** — Envelope 无 frozen 是 Story 1.1 的既有状态（pre-existing），非本 Story 引入；危害需"带副作用过滤器 + 超时丢弃"组合，当前占位过滤器只读/整信封替换。随 Epic 4 真实过滤器落地一并重审（与 blind#5 同批）。
- blind#8 无效输入测试 trace_id 断言空洞 — **low / patch** — 已核验：原 payload 只含 session_id，"!= trace-001" 比较无意义。已修：payload 带 trace_id 但缺必填字段，断言返回新生成 id；另加畸形 JSON 用例锁 422 路径兜底。
- blind#9 模板测试只查前缀与不等式 — **low / patch** — 已核验：空正文或与通用兜底同文案的缺陷可过测。已修：断言 9 类正文非空且两两不同。
- blind#10 路由器只归一 None、越枚举值透传 — **false** — spec 设计注明确 fail-closed 收口在渲染层（"None 或不在 9 类内一律通用兜底"），渲染层已测 NOT_AN_INTENT；收口单一，无买家可见危害。拒绝。
- blind#11 trace_id 无格式校验/唯一性/会话域 — **low / 拒绝** — trace_id 为客户端关联手段，信封口径冻结；格式校验属横切关注且不在 intent 内，追踪规范化随 Story 1.5 统一处理。拒绝。
- blind#12 无请求大小限制/背压 — **low / 拒绝（intent 排除）** — 冻结块 Never 清单明确"不含鉴权/限流等横切能力"。拒绝。
- vgap#1（其他）`uv run ruff check .` 全仓 302 存量告警 — **low / defer** — 全部位于 vendored `.agents/skills/`、`.claude/skills/` 脚本树与 `docs/`，pre-existing，与本 diff 无关；建议后续统一 ruff 排除配置或收敛存量。

## Design Notes

- 兜底应答结构：`PipelineResult{reply, trace_id, degraded, failed_filter}`，degraded=False 时 failed_filter 为 None。
- 逐跳不重验信封：过滤器出入参均为强类型 Envelope，模型即保证；重验属浪费（Pydantic 校验非零成本）。
- 超时语义：全局 deadline 在管道入口设定；每跳提交线程池执行，future.result(timeout=剩余预算)；超时即兜底，执行线程结果丢弃（占位过滤器无副作用，可安全丢弃）；预算按 wall-clock 计算。
- 模板选择键：envelope.intent 命中 9 类则取对应文案；None 或不在 9 类内一律通用兜底（fail-closed 口径与信封未知枚举一致）。

## Verification

**Commands:**
- `uv sync` -- expected: 无新依赖，lock 不变
- `uv run pytest` -- expected: 全绿（含 I/O 矩阵四场景）
- `uv run ruff check src tests` -- expected: 无告警（code review 补丁：全仓含 vendored skills/docs 存量告警，收窄口径同 1.3-1.5）
- `uv run mypy src` -- expected: strict 模式无错误
