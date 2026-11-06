---
title: 'Story 4.2 LLM 子图与 OpenAI 客户端'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '14b5a6aff86c9b12d6a51e6a598fdac912955c09'
context:
  - '_bmad-output/implementation-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `llm/` 目前只有空包占位，Epic 4 的受控生成没有 LangGraph 子图与 OpenAI 客户端，prompt 也无集中管理，后续 Grounding（4.3）、审核（4.4）与降级（4.5）没有可复用的生成底座。

**Approach:** 在 `llm/` 内实现 LangGraph 1.2.16 子图（生成节点）+ OpenAI 客户端（LangChain-OpenAI ≥1.6.2）+ 集中 prompt 模板（事实卡片/对话上下文/生成约束三段），对外只暴露同步生成接口（AD-2）；仅交付模块与接口，不接入管道热路径。

## Boundaries & Constraints

**Always:**
- 对外仅暴露同步 `generate()` 接口；子图仅进程内 `.invoke()`，不依赖 LangGraph Runtime。
- 模型名 alias 注入：构造参数 > `WEILIAO_LLM_MODEL` > 默认 `gpt-4o`，禁硬编码（沿用分类器口径）。
- 生成预算默认 1.8s（含至多 1 次重试，可配置）：429 / 5xx / 超时 / 网络错误重试 1 次；失败抛统一 `LlmGenerationError`（自报 `reason`），不抛裸底层异常。
- prompt 集中 `llm/prompts.py`，固定三段：事实卡片、对话上下文、生成约束；输入仅消费 resolved_message + fact_card + intent，不读买家原始文本。
- 生成成功经 `tracing.record_generation` 记录模型名与 token 用量，追踪 fail-safe。temperature=0，max_tokens 默认 512（可配置）。
- 无事实卡片（L0 miss，fact_card=None）照常生成，事实段明示暂无事实，不虚构。

**Never:**
- 不把生成结果接入买家可见路径：不改管道过滤器链/顺序、`PipelineResult`、信封 schema；正常路径仍走模板兜底，接入归 Story 4.3/4.4/4.5。
- 不实现风控审核、模板降级切换、模型路由与成本护栏（属 4.4/4.5/4.6）。
- 不引入 LangGraph Runtime 服务化部署或多进程 checkpoint。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常生成 | resolved_message + fact_card | 返回生成文本；trace 记模型与 token 用量 | 无 |
| 无事实卡片 | resolved_message + fact_card=None | 生成，事实段为"暂无房源事实"口径 | 不虚构价格/面积/学区 |
| 空消息 | resolved_message 为 None 或空串 | 拒绝调用模型 | fail-fast，reason=invalid_input |
| 可重试错误 | 429 / 5xx / 超时 / 网络错误 | 重试恰 1 次，成功则正常返回 | 仍失败抛 LlmGenerationError，总预算 ≤1.8s |
| 不可重试错误 | 400 / 401 / 403 / 内容过滤 | 不重试直接失败 | 抛 LlmGenerationError，reason 可区分 |
| 凭据缺失 | 未配置 OPENAI_API_KEY | 首次调用惰性构造才失败 | 构造异常归一为 LlmGenerationError |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/llm/__init__.py` -- 现仅 docstring 占位；本 Story 填充并导出同步接口。
- `src/weiliao_agent/classifiers/llm_zero_shot.py` -- 复用其模式：alias 注入、惰性构造、`future.result(timeout)` 超时双保险、统一错误自报 reason、可注入替身。
- `src/weiliao_agent/observability/tracing.py` -- `record_generation()`（L382）已就位，直接消费，不改。
- `src/weiliao_agent/models/fact_card.py` / `models/envelope.py` -- `FactCard` 与信封字段只读复用，不改 schema。
- `pyproject.toml` -- langgraph 1.2.16、langchain-openai ≥1.6.2 已声明，不动。
- `tests/` -- 替身式单测风格（test_templates.py 等），新测试同口径，无网络。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/llm/prompts.py` -- 三段 prompt 构建，fact_card=None 走"暂无事实"口径 -- AC 第 3 条。
- [x] `src/weiliao_agent/llm/subgraph.py` -- LangGraph StateGraph：生成节点调用客户端，编译后进程内 `.invoke()` -- AD-2 同步接口。
- [x] `src/weiliao_agent/llm/client.py` -- OpenAI 客户端：alias 注入、惰性构造、预算内至多 1 次重试、统一错误、延迟埋点 -- AC 第 2 条与 NFR-4。
- [x] `src/weiliao_agent/llm/__init__.py` -- 导出同步入口与错误类型；组合根不接线（无管道改动）。
- [x] `tests/test_llm_prompts.py`、`tests/test_llm_client.py`、`tests/test_llm_subgraph.py` -- 替身覆盖 I/O 矩阵全部分支。

**Acceptance Criteria:**
- Given 任一同步调用方，when 调用 `generate()`，then 不依赖 LangGraph Runtime、进程内返回生成文本。
- Given 429/5xx/超时，when 生成，then 恰重试 1 次、总预算 ≤1.8s，仍失败抛统一异常且 reason 可归因。
- Given prompt 模板，when 渲染，then 固定包含事实卡片、对话上下文、生成约束三段。
- Given 生成成功，when 追踪记录，then trace 含模型名与 token 用量（替身验证）。

## Implementation Notes

- 三段以固定标记（【事实卡片】/【对话上下文】/【生成约束】）渲染，测试按标记断言；价格恒以分桶口径表述。
- 客户端级 timeout 按「预算 / (1+重试次数)」摊到单次调用，调用侧 `future.result(预算)` 守总预算；错误归一表：429→rate_limit、5xx→server_<status>、400 系→http_<status>、连接→network、超时→timeout。
- `max_tokens` 经 `max_completion_tokens` 传入（langchain-openai 1.6.2+ 规范名）。
- 凭据缺失路径以假 langchain_openai 模块单测锁定（不依赖真实 OPENAI_API_KEY 环境）。
- 验证：`uv run pytest tests -q` 348 项通过；`ruff check` 无告警；`mypy src` strict 35 文件无错误。
- 评审补丁：单次尝试超时改报 reason=attempt_timeout（与总预算耗尽区分）；budget_seconds<=0 构造 fail-fast；超时弃用 future 加 done-callback 取回异常；模块级惰性子图加 Lock 双检；补模块入口转发/复用测试、APITimeoutError 重试用例、_to_result 内容块两分支测试；测试卫生三处归位。补丁后 `pytest tests -q` 353 项通过，ruff/mypy 无告警。

## Spec Change Log

## Review Triage Log

### 2026-11-06 评审轮（blind 10 项 / edge-case 4 项 / verification-gap 2 项）

- blind#1 diff 新文件头用绝对路径 — **false** — 系 `git diff --no-index` 生成评审临时文件的产物，非交付代码内容；仓库内文件与 git 跟踪路径正常。
- blind#2 alias 测试缺 delenv — **low / patch** — 若运行环境恰设 `WEILIAO_LLM_MODEL`，默认断言假败；最小修正为测试开头 `monkeypatch.delenv(..., raising=False)`。
- blind#3 reason 混用 timeout — **low / patch** — 单次尝试超时（APITimeoutError/内置 TimeoutError）与调用侧总预算耗尽同报 `timeout`，NFR-4 压测定位时无法区分；最小修正为单次尝试超时改报 `attempt_timeout`（仍属可重试）。
- blind#4 模块级惰性子图无锁 — **low / patch** — 并发首调 check-then-act 竞态会重复装配、泄漏一个执行器；最小修正为加 `threading.Lock` 双检。与 edge#3 同根因合并。
- blind#5 客户端线程池无 close — **low / 拒绝** — 与既有分类器（llm_zero_shot）同模式，服务期为单例池，日常伤害可忽略；修复需新增公开生命周期接口，超出直接修正。
- blind#6 重试矩阵缺 APITimeoutError — **low / patch** — 生产超时的真实异常类型未覆盖，`isinstance(exc, APITimeoutError)` 分支零验证；补参数化用例即可。
- blind#7 _to_result 内容块分支零测试 — **low / patch** — list-of-dict 拼接与"非 str/list 拒绝"两分支无测试，结构化 content 回归无信号；补两个用例。与 verification-gap#2 同根因合并。
- blind#8 模块级 generate() 入口零测试 — **low / patch** — 公开契约（参数转发 + 惰性单例）无断言，删参/每次重建均全绿；以 monkeypatch 桩验证转发与复用。与 verification-gap#1 同根因合并。
- blind#9 买家消息可碰撞分段标记 — **low / 拒绝** — 日常买家输入恰含【生成约束】等标记的概率低，且 4.4 后置审核对下发内容兜底；结构性加固（转义/分隔）属 4.3 prompt 约束职责，修复超直接修正。
- blind#10 测试卫生三处 — **low / patch** — client 测试错放 test_llm_subgraph.py、`_messages()` 跨文件重复、"12" 裸子串断言过弱；均为直接修正。
- edge#1 usage NaN/Inf — **false** — usage_metadata 源自 OpenAI API JSON 整数，无在库可达生产者制造 NaN；找不到可达路径即非缺陷。
- edge#2 budget_seconds<=0 无校验 — **low / patch** — 配置错误下每次 generate 必超时且归因晦涩；最小修正为构造时 fail-fast（带变量名，同 L1 TTL 口径）。
- edge#3 并发首调竞态 — **low / patch** — 并入 blind#4 同组。
- edge#4 超时弃用 future 异常未取回 — **low / patch** — 预算触发且工作线程最终抛错时 stderr 出 "Future exception was never retrieved" 噪音；最小修正为 `future.add_done_callback` 取回异常。
- verification-gap#1 模块入口未验证 — **low / patch** — 并入 blind#8 同组（该层证据预验证）。
- verification-gap#2 内容块分支未验证 — **low / patch** — 并入 blind#7 同组（该层证据预验证）。

## Design Notes

- 客户端沿用分类器的超时双保险：客户端级 timeout 设在预算内，无法中止的线程由有界线程池兜底（同 `_MAX_WORKERS=8` 口径）。
- 重试仅对可重试错误；400 系与凭据错误立即失败，不在确定性错误上烧预算。
- TTFT 口径：非流式 invoke，生成节点以子图 span 起止观测延迟；「首 token P99 <1.5s / 端到端 ≤1.8s」是上线前压测门槛，本 Story 交付埋点与预算守卫，不做流式改造。
- 费用计算不在本 Story：`record_generation` 只记 usage，cost 由 Langfuse 按模型自算（4.6 细化）。

## Verification

**Commands:**
- `uv run pytest tests -q` -- 全部通过。
- `uv run ruff check src tests` -- 无告警。
- `uv run mypy src` -- strict 无错误。
