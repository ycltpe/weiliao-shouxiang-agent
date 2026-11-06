---
title: 'Story 4.3 事实卡片 Grounding 受控生成'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '462bd11d47e6b1e68a407e8f933342b35a299756'
context:
  - '_bmad-output/implementation-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 4.2 的生成底座只停在模块级：管道里没有生成过滤器，事实卡片（3.3 挂在信封上）没人消费，生成内容没有任何机制保证基于卡片事实——虚构价格、面积、学区的风险无人把守；3.4 的"追问进 LLM 链路"与 4.4 的审核也因此没有可消费的生成草稿。

**Approach:** 强化 prompt 的 grounding 约束与明示违禁表述清单；新增确定性 grounding 校验（纯 Python）；新增 `llm_generate` 过滤器把受控生成接入管道（cache_lookup 之后、risk_review 之前），生成草稿写入信封新增可选字段 `generated_reply`。审核（4.4）未落地前，买家可见应答 100% 仍是模板兜底——LLM 内容在任何路径都不得未经审核触达买家。

**决策（Checkpoint 1，2026-11-06）：** 采用影子接入（选项 A）——生成过滤器进管道、草稿只到信封，接受 4.3 上线至 4.4 完成之间的真实 token 花费；spec 全量保留（约 2500 tokens，超 1600 建议上限，单一目标不可拆，用户知情接受）。

## Boundaries & Constraints

**Always:**
- 生成输入仅消费 `resolved_message`（为 None 时回落 `message`，指代消解属 FR-11 前置、本 Story 不实现）+ 信封 `fact_card` + `intent` + `trace_id`；红线意图（NEGOTIATION/LOAN_POLICY/COMPLAINT）与 `media_type` 非 text 一律跳过生成、不产生草稿（红线优先于 LLM 链路，Epic 5 硬触发落地前的先行把守）。
- 草稿挂信封新增可选字段 `generated_reply: str | None = None`（追加可选字段，既有 model_validate 调用兼容）；`response_assembler`、`PipelineResult` 买家可见语义不动——本 Story 内下发文本始终是模板兜底。
- grounding 校验为确定性纯 Python 检查，fail-closed：违禁表述清单（含"我是这套房的经纪人"及等价自称、"最""第一""价格可谈""学区名额"）、内部标注/分段标记（【事实卡片】等）泄漏、输出出现任何 http(s) 链接（事实卡片无链接字段，链接必属无中生有）；未通过即丢弃草稿 + trace 记失败事件。
- `llm_generate` 过滤器全路径 fail-safe、不向上抛：未注入子图、生成异常（含凭据缺失）、校验未过，都不降级买家应答、不改 `degraded`——影子模式下生成链路任何故障对买家零感知。
- 过滤器顺序固定：intent_router → cache_lookup → llm_generate → risk_review → response_assembler；`PipelineOrchestrator` 默认链与 `main.py` 组合根同步接线（沿用 3.3 先例）。
- prompt 在 4.2 三段结构上强化生成约束段：明示不得承诺价格可谈/学区名额、禁用"最""第一"等违禁词、禁止自称或暗示真人经纪人身份；模板库 4.1 的 `_FORBIDDEN_MATERIAL_FRAGMENTS` 复用为违禁清单底稿并扩充自称条目。

**Never:**
- 不改 `risk_review` / `response_assembler` 行为，不实现风控中台审核（4.4），不做 OpenAI 降级切换与步进恢复（4.5），不做模型路由与成本护栏（4.6）。
- 不让 LLM 生成内容成为买家可见文本；不改 `PipelineResult` 字段、trace_id 回填/透传语义、降级语义与 2.5s 端到端预算。
- 不改 `FactCard` schema、L0/L1 缓存行为与命中率口径；不实现指代消解过滤器（FR-11）；不触碰转人工与 handoff（Epic 5）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 卡片就绪正常生成 | resolved_message + fact_card + 非红线 intent | generated_reply=通过校验的草稿；trace 记模型/token（4.2 既有埋点） | 无 |
| L0 miss / 追问 | fact_card=None | 照常生成（事实段"暂无事实"口径），草稿照常产出 | 校验未过丢草稿 |
| 红线意图/非文本 | NEGOTIATION/LOAN_POLICY/COMPLAINT 或 media_type=other | 不调模型、不产出草稿，generated_reply 保持 None | 无 |
| 消息为空 | message 与 resolved_message 均空/空白 | 跳过生成，信封原样透传 | fail-safe，不触达模型 |
| 生成异常/超时 | OpenAI 故障（4.5 未接入，无重试外兜底） | 无草稿，买家可见应答不变 | 吞异常记 trace，degraded=False |
| 校验未过 | 输出含违禁表述/标注泄漏/链接 | 丢草稿，generated_reply=None | trace 记 grounding 失败事件 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/filters/llm_generate.py` -- 新建过滤器（name=`llm_generate`）：输入归一（resolved_message 回落 message）→ 红线/媒体/空消息跳过 → 调 `llm.generate()` → grounding 校验 → 草稿 model_copy 写回信封；注入子图构造参数（组合根注入，缺省用 llm 模块级入口，同 3.3 cache_lookup 注入模式）。
- `src/weiliao_agent/llm/grounding.py` -- 新建确定性校验：`verify_grounding(text) -> str | None`（返回首条命中问题或 None）；违禁清单含自称经纪人变体、"最""第一""价格可谈""学区名额"、分段标记、http(s) 链接；纯函数无 I/O，可独立单测。
- `src/weiliao_agent/llm/prompts.py` -- 只改生成约束段措辞与违禁明示（4.2 三段结构与渲染标记不动，既有 test_llm_prompts 断言不回归）。
- `src/weiliao_agent/models/envelope.py` -- 追加 `generated_reply: str | None = None` 可选字段 + 字段口径注释；既有校验规则不动。
- `src/weiliao_agent/pipeline.py` -- 默认链插入 LlmGenerateFilter（cache_lookup 与 risk_review 之间）；`_run_chain`/`_degraded`/预算/trace 语义不动。
- `src/weiliao_agent/main.py` -- 组合根装配 LlmGenerateFilter（缺省注入模块级 llm 入口；装配期不触达 OpenAI 凭据）。
- `src/weiliao_agent/filters/templates.py` -- 复用 `_FORBIDDEN_MATERIAL_FRAGMENTS` 作为违禁清单底稿（只读引用，不改模板行为）。
- `src/weiliao_agent/observability/tracing.py` -- 只消费既有事件（record_generation 已在子图内）；grounding 失败复用 record_degradation 或新增轻量事件需在本 Story 内定义并 fail-safe（不改既有事件形状）。
- `tests/test_llm_generate_filter.py`、`tests/test_llm_grounding.py` -- 新建：替身子图覆盖 I/O 矩阵全部分支；既有 test_pipeline 默认链测试（span 数、degraded 语义）按五过滤器口径更新。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/llm/grounding.py` -- 违禁清单 + verify_grounding 纯函数 -- AC 第 2 条（不得含经纪人自称/违禁词）的确定性把守。
- [x] `src/weiliao_agent/llm/prompts.py` -- 生成约束段强化（明示价格可谈/学区名额/违禁词/自称经纪人禁令） -- AC 第 2 条 prompt 明示要求。
- [x] `src/weiliao_agent/models/envelope.py` -- 追加 generated_reply 可选字段 -- 草稿信封挂点。
- [x] `src/weiliao_agent/filters/llm_generate.py` -- 生成过滤器（跳过规则、生成、校验、写回、全 fail-safe） -- AC 第 1 条与 I/O 矩阵。
- [x] `src/weiliao_agent/pipeline.py` + `src/weiliao_agent/main.py` -- 默认链与组合根接线 -- 过滤器顺序约束。
- [x] `tests/test_llm_grounding.py`、`tests/test_llm_generate_filter.py` + 受影响既有测试更新 -- 替身覆盖矩阵全分支，无网络。

**Acceptance Criteria:**
- Given 带 fact_card 的非红线文本消息，when 跑完整管道，then 信封 generated_reply 为基于卡片内容的生成草稿，买家可见 reply 仍为模板兜底文本（degraded=False）。
- Given 生成文本含"我是这套房的经纪人"或违禁词/内部标注/链接，when grounding 校验，then 草稿被丢弃、generated_reply=None 且 trace 留有失败事件。
- Given 红线意图（NEGOTIATION/LOAN_POLICY/COMPLAINT）或 media_type 非 text，when llm_generate 执行，then 不调用模型、不产生草稿。
- Given OpenAI 不可用（凭据缺失/超时），when 跑管道，then 行为与 4.2 前逐字节一致：模板应答、degraded=False、不向上抛。
- Given prompt 渲染，when 检查生成约束段，then 明示不得承诺价格可谈/学区名额、禁用"最""第一"、禁止自称经纪人。

## Implementation Notes

- grounding 违禁清单以 4.1 `_FORBIDDEN_MATERIAL_FRAGMENTS` 为底稿扩充自称经纪人变体（去重保序）；检查顺序固定：违禁表述 → 分段标记泄漏 → http(s) 链接；空输出 fail-closed（reason=empty_output）。
- 过滤器故障可观测走新增 `tracing.record_llm_generate_event`（outcome=grounding_failed / generation_error，ERROR 级，fail-safe），与编排器降级事件区分（影子模式 degraded 不动）。
- 计划外补充：验收时发现 AC 第 5 条（prompt 明示违禁约束）仅有"学区名额"单条断言覆盖，已在 test_llm_prompts.py 补"价格可谈"/"「最」「第一」"/"我是这套房的经纪人"三条断言。
- 验证：`uv run pytest tests -q` 386 项通过；`ruff check src tests` 无告警；`mypy src` strict 37 文件无错误。
- 评审补丁轮（7 组 patch 一次下发）：默认链改注入 `LLM_GENERATE_BUDGET_SECONDS=0.9` 有界子图（慢调用过滤器内超时归一，保 degraded 语义；对 Code Map"缺省模块级入口"措辞的偏差在此记录——过滤器级惰性入口保留，仅默认链装配方式有界化）；grounding 增裸域名/自称正则/1000 字符 runaway 三道 fail-closed；resolved_message 空白即回落 message；补默认五链整链走查、慢生成预算内返回、消息文本优先级与 reason 断言。补丁后 397 项通过，ruff/mypy 无告警。

## Spec Change Log

## Review Triage Log

### 2026-11-06 评审轮（blind 11 项 / edge-case 5 项 / verification-gap 0 项）

- blind#1 慢生成触发编排器 slow_filter 降级 — **medium / patch（并入 G1）** — 属实：过滤器只接异常不接超时，OpenAI 拖过剩余跳预算时 orchestrator 置 degraded=True 并跳过剩余过滤器，违反 I/O 矩阵「degraded=False」行。最小修正：默认链（pipeline 默认 + main.py 组合根）注入 budget_seconds=0.9 的有界子图，慢调用在过滤器内超时归一（degraded 不动）；残余竞态（前两跳吃掉 >1.6s）属 1.2 既有语义，随 deferred 条目处理。
- edge#4（claim）同上 — **并入 G1（carried）**，证据同 blind#1。
- blind#6 无慢生成测试 — **low / patch（并入 G1）** — 属实：与 blind#1 同根因，补 slow-stub 用例锁定「过滤器预算内返回 + degraded=False」。
- blind#2 超时丢弃的生成线程继续烧 token — **low / defer** — 属实但是预存问题：1.2 deferred 条目已记「超时丢弃的执行线程无取消机制……Epic 4 接入真实过滤器后重审执行器生命周期」，本 Story 只是让其可达；修复（共享池/取消）即该挂账项的 settle 内容。追加 deferred 条目（token 花费角度）。
- blind#3 裸「最」「第一」子串误报（最近/最后误伤）— **low / defer** — 属实：fail-closed 安全方向，prompt 已明示禁用使日常草稿很少含「最」；该权衡内生于 Story AC 与 4.1 清单的人类既定违禁词，改匹配语义（分词/豁免表）需业务定夺。settle：随 4.4 通过率/grounding_failed 监控数据观察后定。
- blind#4 空白 resolved_message 不回落 message — **low / patch（G4）** — 属实：`if message is None` 只判 None，空白消解消息会跳过生成；最小修正改为空白即回落。
- blind#5 AC1 测试未用真实默认五过滤器链 — **low / patch（G5）** — 属实：现有测试手工拼三过滤器链；补默认链整链走查（monkeypatch 模块入口替身，锁定草稿生成 + 模板应答 + degraded=False，含失败路径）。
- blind#7 _URL_PATTERN 漏裸域名 — **low / patch（G6）** — 属实：`https?://` 之外 www. 与 example.com 形式可通过；与「链接必属无中生有」的既定依据同向，最小修正扩充模式。
- edge#2 同上 — **并入 G6（carried）**。
- blind#8 自称经纪人枚举不完整且无补偿控制 — **low / patch（G7）** — 属实：枚举法天然不完备（「我是这套房子的经纪人」可通过）；最小修正加「(我是|作为).{0,8}经纪人」正则兜底 + 补测试。补偿监控（grounding_failed 率告警）属 4.4 通过率监控既定范围，不属本 Story。
- edge#3 同上（正则建议）— **并入 G7（carried）**。
- edge#5（claim）同上 — **并入 G7（carried）**。
- blind#9 校验不约束输出长度 — **low / patch（G8）** — 属实：prompt 第 7 条「不超过 200 字」无确定性把守；最小修正加 runaway 上限（1000 字符，仅拦失控输出，不卡正常草稿）。
- blind#10 优先级/归因断言过弱 — **low / patch（G9）** — 属实：priority 测试只数调用次数、过滤器层不断言具体 reason；补消息文本捕获与 reason 元数据断言。
- blind#11 spec 在 in-progress 时声明验证结果、分节空 — **false** — 系流程瞬态：验证运行时状态必为 in-progress，Implementation Notes 已补记、状态已转 in-review，非交付缺陷。

## Verification

**Commands:**
- `uv run pytest tests -q` -- 全部通过（含新增替身测试与更新的默认链测试）。
- `uv run ruff check src tests` -- 无告警。
- `uv run mypy src` -- strict 无错误。
