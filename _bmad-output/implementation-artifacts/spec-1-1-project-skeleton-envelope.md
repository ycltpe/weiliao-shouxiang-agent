---
title: 'Story 1.1 项目骨架与信封模型'
type: 'feature'
created: '2026-10-31'
status: 'done'
baseline_commit: '69990ddf0552df589d54697bc0c7657fdfffd9c2'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-1-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-test-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 微聊首响 Agent 尚无统一项目骨架，各模块将各自为战；过滤器间缺少唯一通信契约（信封），管道-过滤器架构（过滤器间仅通过信封传递）无法落地。

**Approach:** 按架构 Structural Seed 建立全新包骨架（九目录），定义单一信封 Pydantic 模型（六核心字段 + 架构预留四字段），并按架构 Stack 表锁定 pyproject 依赖；既有 my-agent 原型按"不复用、不继承"处置。

## Boundaries & Constraints

**Always:**
- 目录严格按 Structural Seed：`adapters/`、`filters/`、`llm/`、`session/`、`cache/`、`models/`、`workers/`、`observability/`、`health/`，位于新包 `src/` 下。
- 信封为全链路唯一通信模型；六核心字段 `session_id`、`house_id`、`turn`、`intent`、`timestamp`、`trace_id`；预留 `message`、`resolved_message`、`coref`、`media_type` 四字段（本 Story 仅定义，不消费）。
- 信封提供 `model_validate` 类方法校验任意来源 dict；缺失必填字段、类型错误、未知枚举值一律 fail-closed（抛 `ValidationError`），由调用方兜底。
- pyproject 与架构 Stack 表一致：Python ≥3.11、FastAPI ≥0.115.0、Uvicorn ≥0.32.0、Pydantic ≥2.9.0、redis-py 8.1.0、Langfuse SDK 5.0.4、cachetools 7.2.4。
- 既有 my-agent 脚手架（anthropic、a2a-sdk、sse-starlette、postgres checkpoint 相关）不复用、不继承。
- 决策（Open Questions 已决）：新包名为 `weiliao_agent`；旧原型 `src/my_agent/` 与耦合测试（tests/ 下 5 个文件）直接删除，原型依赖从 pyproject 移除。

**Never:**
- 不实现任何过滤器逻辑、HTTP 端点、会话/缓存实现——本 Story 仅骨架与信封。
- 骨架代码不引入 LangGraph Runtime 依赖（LangGraph/LangChain-OpenAI 依赖按 Stack 表锁定版本，但代码不使用）。
- 不改 docker-compose.yml 与 .env 中的密钥内容。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | 合法六字段 dict 经 model_validate | 信封实例，timestamp 为 datetime 类型 | N/A |
| MISSING_REQUIRED | 缺 session_id 或 house_id 的 dict | ValidationError（fail-closed） | 调用方捕获后走模板兜底（本 Story 仅保证异常被抛出） |
| UNKNOWN_FIELD | 含未定义字段的 dict | 默认忽略（Pydantic 行为），不报错 | N/A |
| BAD_TYPE | trace_id 为非字符串（如 dict） | ValidationError | 同上，fail-closed |

</frozen-after-approval>

## Code Map

- `pyproject.toml` -- 项目定义（hatchling，wheel 打包 `src/my_agent`）；依赖与打包路径的调整对象
- `uv.lock` -- 已锁 95 包；redis/langfuse/cachetools 未锁定，`uv add` 需联网
- `src/my_agent/` -- 旧原型 9 文件（main.py、config.py、api/、agent/、a2a/），处置对象，不可复用
- `tests/test_a2a.py`、`tests/test_agent.py`、`tests/test_multi_agent.py`、`tests/test_api.py`、`tests/conftest.py` -- 全部深度耦合旧原型，随处置方案删除或重写
- `.env.example` -- 仅 LLM_* 三变量；本 Story 不新增（REDIS_URL 等留给 Story 1.3）
- `docker-compose.yml` -- 本地服务合集（含 redis 7:6379，无 Kafka），只读
- `.venv/` -- Python 3.11.15 可用，fastapi 0.139.2 / pydantic 2.13.4 已装

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- 重命名项目与新包路径，移除原型依赖，按 Stack 表新增 redis 8.1.0、langfuse 5.0.4、cachetools 7.2.4（LangGraph 1.2.16、LangChain-OpenAI ≥1.6.2 按表核对保留），`uv sync` 更新 lock -- AC 要求 pyproject 与 Stack 表一致
- [x] `src/<新包>/` -- 创建 Structural Seed 九目录（各含 `__init__.py`）-- 基座目录契约
- [x] `src/<新包>/models/envelope.py` -- 实现信封模型 + `model_validate` 校验 -- AC 核心交付
- [x] `tests/test_envelope.py` -- I/O 矩阵四场景单测 -- AC 可独立验收
- [x] 旧原型处置 -- 按 Open Questions 答案删除或迁移 `src/my_agent/` 与耦合测试 -- 架构"不复用、不继承"

**Acceptance Criteria:**
- Given 架构 Structural Seed 定义，当初始化代码库与依赖，则全部目录与最小可运行骨架就位，pyproject 版本与 Stack 表一致，既有 my-agent 依赖不复用、不继承
- Given 任意来源的信封 dict，当经 `model_validate` 校验，则合法输入产出信封实例，缺必填字段/类型错误 fail-closed 抛 `ValidationError`

### Review Findings

- [x] [Review][Patch] 组合根 Redis URL 非法时 import 即崩（`redis.Redis.from_url` 裸抛 ValueError，无 fail-fast 提示）[src/weiliao_agent/main.py]
- [x] [Review][Patch] 信封 `min_length=1` 不挡纯空白字符串（`" "` 可通过 session_id/house_id/trace_id 校验，生成 `weiliao:session: ` 类脏 key）[src/weiliao_agent/models/envelope.py:81-86]
- [x] [Review][Patch] README 与实现矛盾两处：声称过滤器链/会话/健康/追踪"将在后续 Story 实现"（本 Epic 已交付），且 trace_id 仍列为必填 fail-closed（Story 1.5 已放宽为可选）[README.md]
- [x] [Review][Patch] spec Verification 声明 `uv run ruff check .` 预期无告警，与仓库实际矛盾（全仓 302 存量告警）；应收窄为 `ruff check src tests`（1.3/1.4/1.5 已用收窄口径）[spec-1-1 Verification]

## Implementation Notes

## Spec Change Log

## Review Triage Log

评审轮次 1（2026-10-31，三层并行：blind-hunter / edge-case-hunter / verification-gap）：

| # | 来源 | 发现 | 裁定 | 证据 | 路由 |
|---|------|------|------|------|------|
| 1 | blind-hunter | 重写后无任何入口点，fastapi/uvicorn 成摆设 | false | frozen Intent 明确"本 Story 仅骨架与信封"，Never 条排除 HTTP 端点；可运行面属 Story 1.2/1.4 | 驳回 |
| 2 | blind-hunter | 同一变更删除整个旧应用、只搭新骨架 | false | frozen 决策：旧原型"不复用、不继承"、直接删除；增量过渡不在意图内 | 驳回 |
| 3 | blind-hunter + verification-gap | README 仍描述 my-agent 的 /a2a、/api/v1、`uv run my-agent`，照做即失败 | low | 已核实 README 全文过时（项目已改名 weiliao-agent）；真实伤害开发者 onboarding | patch |
| 4 | blind-hunter + verification-gap + edge-case-hunter | MISSING_REQUIRED 只参数化 session_id/house_id，timestamp/trace_id 的必填 fail-closed 无测试保护 | medium | verification-gap 预验证：将 timestamp 放宽为 Optional 后 6 个测试仍全过；docstring 承诺四字段必填 fail-closed，回归不可检测 | patch |
| 5 | blind-hunter + edge-case-hunter | 接受无时区的 naive timestamp | maybe-false | 接受 naive 属实，但 spec 未规定时区策略、下游消费者本 Story 不存在；若属实则 medium（跨跳时间比较崩溃/事件乱序） | defer（若属实则 medium；由后续 Story spec 决定 timestamp 时区策略） |
| 6 | blind-hunter | unknown-field 静默忽略与"一律 fail-closed"矛盾 | false | frozen 矩阵明确 UNKNOWN_FIELD "默认忽略（Pydantic 行为），不报错"；fail-closed 仅约束必填/类型/未知枚举 | 驳回 |
| 7 | blind-hunter | __version__ 丢失、包根不 re-export Envelope | maybe-false（若属实则 low） | 事实属实但无消费者；Story 1.4 健康端点若需版本届时再定 | 驳回（若属实则仅 low） |
| 8 | blind-hunter | langgraph/redis/langfuse/cachetools 精确 pin 无注释、违背 >= 惯例 | false | frozen 要求"pyproject 与架构 Stack 表一致"，精确版本即 Stack 表规定，依据在架构文档 | 驳回 |
| 9 | blind-hunter | 配置管理（pydantic-settings）被移除且无替代 | false | frozen 决策：原型依赖移除；REDIS_URL 等配置留 Story 1.3 | 驳回 |
| 10 | blind-hunter | 空 stub 包无 TODO/追踪标记，易被误认为已实现 | maybe-false（若属实则 low） | stub 各含职责 docstring；"被误认"无具体伤害实例 | 驳回（若属实则仅 low） |
| 11 | blind-hunter | 信封缺序列化/往返保证（model_dump 往返、冻结语义） | maybe-false（若属实则 low） | 本 Story 无消费者序列化信封；Kafka 消息体格式属后续 Story | 驳回（若属实则仅 low） |
| 12 | blind-hunter | 无安装环境导入冒烟测试，打包错误不会被发现 | false | 已在 .venv 安装环境（sys.path 无 src）实测 `import weiliao_agent` 成功；当前打包无误 | 驳回 |
| 13 | edge-case-hunter | session_id/house_id/trace_id 接受空字符串 | maybe-false | 接受空串属实，但非空约束未在 spec 任何层规定；若属实则 medium（会话/路由查找失效） | defer（若属实则 medium；由后续 Story spec 决定标识符非空约束） |
| 14 | edge-case-hunter | turn 接受 0 与负值 | maybe-false（若属实则 low） | ge=1 未在 spec 规定；turn 由会话管理赋值（Story 1.3） | 驳回（若属实则仅 low） |
| 15 | edge-case-hunter | BAD_TYPE 场景只测 trace_id，未测 timestamp/turn | false | 矩阵 BAD_TYPE 行字面即"trace_id 为非字符串"；且 timestamp 的 isinstance(datetime)、turn==1 断言已能捕获这两字段的类型回退 | 驳回 |
| 16 | edge-case-hunter | 默认 lax 模式强转（turn='1'→1）违背"类型错误一律 fail-closed" | false | "类型错误"在 Pydantic 语义下指不可强转输入（矩阵以 dict 验证）；可强转输入属 Pydantic 默认行为，spec 选定 Pydantic 即接受其语义 | 驳回 |

## Design Notes

信封字段必填性：`session_id`、`house_id`、`timestamp`、`trace_id` 必填；`turn`、`intent` 允许为 None（turn 由会话管理在入管道前赋值、intent 由分类器填写，均属后续 Story），但非 None 时类型必须正确。`intent` 本 Story 为 `str | None`，9 类 Literal 枚举在 Story 2.1 定义后收窄。预留四字段默认 None，不参与本 Story 验收。

## Verification

**Commands:**
- `uv sync` -- expected: lock 更新成功，无依赖冲突
- `uv run pytest` -- expected: 全绿（旧耦合测试已按处置方案移除）
- `uv run ruff check src tests` -- expected: 无告警（code review 补丁：全仓含 vendored skills/docs 存量告警，收窄口径同 1.3-1.5）
- `uv run mypy src` -- expected: strict 模式无错误
