---
title: 'Story 2.3 重复追问识别'
type: 'feature'
created: '2026-11-02'
status: 'done'
baseline_commit: '1689378cbc658287ecbf89810c017d5fe8e4c24d'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-2-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-test-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Story 2.2 已把分类结果写入会话状态，但系统无法感知买家是否在反复追问同一问题：Redis 会话 hash 仅有 1.3 交付的 `followup_count` 标量原语（从未被业务调用），无 per-intent 计数；信封也无 Follow-up 标记——Epic 3「追问禁走 L0 缓存」与 Epic 5「追问 ≥3 硬触发」都缺输入。

**Approach:** 分类成功后，`intent_router` 过滤器经注入的会话单写者回调对 (session, intent) 计数做 Redis 原子自增（Lua 单 roundtrip），同意图第 2 次起标记 Follow-up Query 写入信封新字段 `is_followup`；会话级 `followup_count` 仅在追问命中时自增，达到 3 时产出 Hard Trigger 信号（Langfuse trace 事件 + Redis 权威值），判定与转人工归 Epic 5 的 AD-12 模块。

## Boundaries & Constraints

**Always:**
- 追问判定只以分类器输出的意图标签为准（AD-11：同义改写归并是分类器职责，禁止对消息文本做相似度比对）。
- 会话状态写入只经 `SessionManager`（AD-4 单写者）：过滤器注入 recorder callable，不持有 Redis 客户端；追问计数权威值只存 Redis。
- 计数自增必须原子（Lua 单 roundtrip，架构并发行），TTL 与会话一致（30 分钟），EXPIRE 随脚本刷新。
- 记录失败 fail-safe：recorder 抛异常时应答不降级、信封保留分类结果、`is_followup` 保持默认 False、trace 记失败事件，过滤器自身不向上抛。
- 既有行为零回归：占位行为、UNKNOWN fail-closed、置信度不进信封、既有全部测试绿；`mypy src` strict 与 `ruff check src tests` 干净。

**Never:**
- 不实现硬触发判定、转人工路由与信封 handoff 字段（AD-12 评估模块，Epic 5）；本 Story 只产出计数权威值与触发信号。
- 不做追问禁走 L0 缓存的路由逻辑（Epic 3）；不改 `write_intent`（2.2 冻结签名）、`pipeline.py` 默认链与 `templates.py` 文案。
- UNKNOWN 不参与追问判定（计数与标记均跳过，理由见 Design Notes）。
- 不以任何进程内缓存（L1）作为追问判定依据（AD-4/AD-5）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 同意图首次消息 | 分类成功（tag 非 UNKNOWN），turn 非 None，recorder 已注入 | per-intent 计数→1；信封 `is_followup=False`；`followup_count` 不自增；trace 记追问事件 | 无错误 |
| 同意图第 2 次消息 | 该意图在 session 已出现 1 次 | per-intent 计数→2；`is_followup=True`；`followup_count`→1 | 无错误 |
| 追问总数达 3 | 本次追问使会话 `followup_count`→3 | trace 记 hard trigger 信号事件（命中条件=追问≥3，计数=3）；判定方（Epic 5）读 Redis 权威值 | 无错误 |
| recorder 抛 redis.RedisError | 分类成功 | 应答不降级；信封保留 intent；`is_followup=False`；trace 记 `followup_record_failed` | try 包裹 fail-safe |
| turn=None / 未注入 recorder | 测试替身装配 | 跳过追问记录，仅分类 + trace | 无错误 |
| 分类失败 / 空文本 / tag=UNKNOWN | fail-closed UNKNOWN | 不调 recorder，不累积任何追问计数 | 既有 fail-closed |
| 并发同 session 同 intent | 多线程同时记录 | 各次计数精确不丢不重（Lua 原子，同 1.3 并发口径） | 无错误 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/models/envelope.py` -- 新增 `is_followup: bool = False` 字段与字段口径 docstring；既有字段语义不动。信封为过滤器间唯一通信载体（AD-1），Epic 3 的 cache_lookup 需同步消费此标记。
- `src/weiliao_agent/session/manager.py` -- 新增 `record_followup(session_id, tag) -> tuple[int, int]`（per-intent 计数、会话追问总数），Lua 单 roundtrip（HINCRBY `followup:{TAG}`、按命中条件自增 `followup_count`、EXPIRE）；1.3 的 `increment_followup` 原语与既有 Lua 不动（锁定测试）；docstring 更新为 2.3 完成口径。
- `src/weiliao_agent/filters/intent_router.py` -- 构造新增可选 `followup_recorder: Callable[[str, IntentTag], tuple[int, int]] | None`；分类成功、`turn` 非 None、recorder 存在且 tag 非 UNKNOWN 时调用，结果写信封 `is_followup`（per-intent ≥2）；异常 try 包裹 fail-safe；模块 docstring 更新。
- `src/weiliao_agent/observability/tracing.py` -- 新增 `record_followup`（tag/per_intent_count/session_followup_count/is_followup 事件）与 `record_followup_hard_trigger`（信号事件，count），fail-safe 口径同 `record_classification`。
- `src/weiliao_agent/main.py` -- 组合根注入 `followup_recorder=_session_manager.record_followup`，路由链路其余装配不动。
- `tests/test_session.py` -- `record_followup` fakeredis 用例：首现/二现/追问总数累计/达 3 阈值/per-intent 隔离/TTL 刷新/increment 先于 begin_turn。
- `tests/test_intent.py` -- 过滤器追问矩阵用例：标记写回信封、recorder 失败 fail-safe、UNKNOWN 跳过、turn=None 跳过、达阈值发 hard trigger trace 事件。

## Tasks & Acceptance

**Execution:**
 - [x] `src/weiliao_agent/models/envelope.py` -- 新增 `is_followup: bool = False` 及 docstring 口径 -- Epic 3 同步消费的 Follow-up 标记载体
 - [x] `src/weiliao_agent/session/manager.py` -- 新增 `record_followup` + Lua 脚本（原子、TTL 刷新、返回双计数）-- AD-4 单写者与原子性要求
 - [x] `src/weiliao_agent/observability/tracing.py` -- 新增 `record_followup` / `record_followup_hard_trigger` -- 追问标记与触发信号 Langfuse 可验证
 - [x] `src/weiliao_agent/filters/intent_router.py` -- 注入 recorder、判定写回、fail-safe 与 trace 挂点 -- 追问识别的落点
 - [x] `src/weiliao_agent/main.py` -- 组合根注入 recorder -- 默认链路装配完整
 - [x] `tests/test_session.py` + `tests/test_intent.py` -- I/O 矩阵全场景单测（含并发计数精确用例）-- 锁定行为、防回归

**Acceptance Criteria:**
- Given 同 session 同意图第 2 次消息且分类成功，when 管道执行完毕，then 信封 `is_followup=True`，Redis 会话 hash 的 `followup:{TAG}`=2、`followup_count`=1，TTL 与会话一致（30 分钟）。
- Given 本次追问使会话追问总数达到 3，when 管道执行完毕，then Langfuse 消息 trace 可检索 hard trigger 信号事件（命中条件=追问≥3、计数=3），且 Redis 权威值与信号一致。
- Given recorder 抛 `redis.RedisError`，when 分类成功，then 买家应答不降级、信封保留分类结果、`is_followup=False`、trace 记录失败事件。
- Given 分类 fail-closed 为 UNKNOWN（含占位 `none` 模式全量 UNKNOWN），when 处理，then 不调用 recorder、不累积追问计数（降级路径零副作用）。
- Given 多线程并发对同 session 同意图记录追问，when 全部完成，then 各次计数精确不丢不重。
- Given `uv run pytest tests -q`，when 全量执行，then 既有用例 + 新增用例全部通过，零回归；`ruff check src tests` 与 `mypy src`（strict）无告警。

### Review Findings

（2026-11-03 bmad-code-review 独立二次评审：blind-hunter 14 / edge-case-hunter 3 / verification-gap 3 / acceptance-auditor 4，共 24 项原始发现，合并为 18 项——decision-needed 1 / patch 2 / defer 1 / dismiss 14）

- [x] [Review][Decision] `followup_count` 双写原语硬化选择（low；盲审+边界+验证缺口+验收审计四层同指）— 1.3 `increment_followup`（无条件自增）与 2.3 `record_followup`（仅追问命中自增）共享同名字段，当前唯一屏障是 G4 docstring 警告；若未来调用方误用 increment_followup 把计数推过 3，`== 3` 穿越信号将静默永不触发。选项：A 维持 docstring 警告（现状零改动）/ B 弃用 increment_followup（需修订本 spec「既有原语不动」约束并改 1.3 锁定测试）/ C 字段改名隔离（需数据语义迁移、同样动 1.3 测试）。**裁决（2026-11-03）：选 A 维持 docstring 警告，风险接受。**
- [x] [Review][Patch] G1 修复缺负边界回归测试（medium；验证缺口层经变异测试实证：`== 3` 改回 `>= 3` 全套件仍绿） [`src/weiliao_agent/filters/intent_router.py:172`] — 补两条 stub 用例：recorder 返 `(3, 4)`（计数已过 3 的追问不重发信号，锁 E1）与 `(1, 3)`（会话已达标时新意图首次出现非追问、不发信号，锁 E2）。**已修复（2026-11-03）：`test_followup_past_threshold_does_not_reemit_hard_trigger` 与 `test_followup_first_occurrence_after_threshold_emits_nothing`，全套件 114 例绿。**
- [x] [Review][Patch] `record_followup` docstring 阈值措辞矛盾（low；验收审计层发现，已读码证实） [`src/weiliao_agent/session/manager.py:161`] — docstring 写「据会话总数 ≥3 产出 hard trigger 信号」，与实现的恰为 3 穿越语义（`== 3`）矛盾，会误导 Epic 5 消费方；改为「恰为 3（第 3 次追问穿越）」。**已修复（2026-11-03）。**
- [x] [Review][Defer] 并发计数精确性仅 fakeredis 验证 [`tests/test_session.py:452`] — deferred, pre-existing（fakeredis server 级全局串行，与首次评审 deferred 条目及 Story 1.3 条目同根因；已记录 deferred-work.md，settle：部署形态落地后补真实 Redis 集成测试）。

## Implementation Notes

- Lua 脚本内做条件自增：`per_intent >= 2` 才 `HINCRBY followup_count`，
  否则仅 `HGET` 现值不创建字段——保证「首次出现」对会话总数零副作用，
  语义钉死由脚本层兜底（非调用方约定）。
- 过滤器用 `classified.model_copy(update={"is_followup": True})` 置位：
  值源自过滤器自身布尔判定（非分类器输出），无需 2.2 的 model_validate
  全量重校验口径；其余信封字段经 model_copy 透传不变。
- trace 失败事件复用 `record_classification_failure`（reason=
  `followup_record_failed`），未新增失败事件类型——与 2.2 的
  `intent_write_failed` 同口径。
- 组合根端到端用例（test_main_composition_root_records_followup_to_redis）
  锁定默认链路两次同意图消息后 Redis `followup:TAG=2` / `followup_count=1`
  / TTL 全量刷新，追问判定不止于替身层。

## Spec Change Log

（空——首个评审循环前由 step-04 填充）

## Review Triage Log

| # | 发现 | 裁决 | 证据 / 路由 |
|---|------|------|------------|
| B1 | hard trigger 信号在计数达 3 后每条后续消息重复触发，且非追问消息（新意图首次出现，Lua else 分支 HGET 回读既有 ≥3 计数）也触发 | medium → patch (G1) | `intent_router` 172-176 行 `if session_followup_count >= 3` 未绑定 is_followup 亦无穿越语义，违反冻结 I/O 矩阵「本次追问使 followup_count→3」；最小修法 `is_followup and session_followup_count == 3`（HINCRBY 步长恒为 1，必然精确穿越） |
| B2 | 追问阈值在 Lua 与过滤器两层重复（per-intent ≥2 两处、≥3 第三处字面量），无单一权威 | low → reject | 消除重复需 Lua 参数化（阈值经 ARGV 注入）并跨模块共享常量，给脚本与调用方加参数超出直接修正；当前两层取值一致、无实测漂移，1.3 既有 Lua 同风格 |
| B3 | `followup_count` 被 1.3 `increment_followup`（无条件自增）与 2.3 `record_followup`（条件自增）两个语义不同的原语共享，未来调用方会污染权威计数 | low → patch (G4) | 共享同 hash 字段属实；当前无业务调用 increment_followup（全仓 rg 仅测试与定义）；最小修法为其 docstring 加语义警告，指名 record_followup 为 Epic 2 追问计数权威路径 |
| B4 | AC2 无端到端 Langfuse 断言（组合根测试只查 Redis hash 与 TTL） | low → defer（同 V1 根因） | 与 V1 同根因：仓内 Langfuse 事件验证只经 set_langfuse_client 替身接缝（1.5/2.2 既定模式，tracing.py:54 载明）；真实 SDK 契约检验超出本仓常规验证路径 |
| B5 | hard trigger 事件 `level="WARNING"` 无断言 | low → patch (G2) | 替身记录全部 kwargs，补一行断言即直接修正，防 Epic 5 按 level 分流时回归 |
| B6 | fake_langfuse fixture teardown 无条件 set None，不恢复先前 client | low → patch (G3) | 当前套件无预配置 client 故无实证危害；快照-恢复为 2.2 B5/V4 同类测试卫生的直接修正 |
| B7 | 并发计数测试只证 fakeredis 串行锁，非真实 Redis 原子性 | low → defer | 与 deferred-work 中 1.3 条目同根因（fakeredis server 级全局串行）；settle 同路径：部署形态落地后补容器化真实 Redis 并发集成测试 |
| B8 | 失败事件丢弃异常细节 + `_RaisingFollowupRecorder` 函数内 import redis | low → reject（细节）/ low → patch (G5)（import） | 细节：reason-only 是 2.2 冻结契约（spec-2-2 评审 B3/B10 已同型裁决 reject），修法需给 `record_classification_failure` 新增参数超出直接修正；import：移至模块顶即直接修正 |
| B9 | spec TTL 硬编码 30 分钟 vs 实现参数化；UNKNOWN 排除仅在过滤器层，manager 契约未强制 | 均 reject | TTL：当前默认 1800s 与 spec 逐字一致、无实测漂移，唯一修法是改冻结 spec 文本（明令拒绝）；UNKNOWN：唯一调用方过滤器按冻结矩阵跳过，无可达违例，manager docstring 已载调用方口径，加守卫属未实证复杂度 |
| V1 | hard trigger/followup 事件形状仅经假 client 验证 | low → defer（采信该层 filed disposition） | 1.5/2.2 既定替身接缝模式；真实 Langfuse 集成检验随部署/可观测 deferred 工作一并落地 |
| E1 | 计数 >3 后后续追问重复发 hard trigger | medium → patch (G1) | 同 B1 根因（`>= 3` 无穿越语义） |
| E2 | 既有计数 ≥3 时新意图首次出现（非追问）也发 hard trigger | medium → patch (G1) | 同 B1 根因；Lua else 分支回读既有计数使非追问路径拿到 ≥3 |
| E3 | spec 读者预期恰一次穿越信号，实现每条 ≥3 消息都发 | medium → patch (G1) | 同 B1 根因；冻结矩阵「本次追问使→3」与 Design Notes「触发时刻=第 3 次追问命中」均写穿越语义 |

分组路由：G1（B1+E1+E2+E3，medium）/ G2（B5）/ G3（B6）/ G4（B3）/ G5（B8 import）→ 全部 patch；B7、B4+V1 → defer；无 intent_gap / bad_spec，不触发回环。

## Design Notes

**UNKNOWN 排除口径：** 分类失败/占位降级时 tag=UNKNOWN，若计入追问会把「分类不可用」放大为「买家被误升级」，且 `none` 模式下全量 UNKNOWN 会使计数与 turn 同步膨胀、≥3 信号在降级态误触发。排除后降级路径零副作用；「≥3 无论意图类型」仍成立——其余 8 类有效意图的追问均计数。

**`followup_count` 语义钉死：** 1.3 交付的标量为「会话累计追问次数」，仅在追问命中（per-intent ≥2）时自增，而非每条消息自增——否则恒等于 turn 序号，「≥3」会在第 3 条普通消息误触发。追问 #1 = 同意图第 2 次消息；触发时刻 = 会话内第 3 次追问命中（同意图第 4 次，或跨意图累计第 3 次）。

**信号形态最小化：** 本 Story 只产信号（trace 事件 + Redis 权威值），不做判定、不写 handoff 信封字段——AD-12 明确判定结果写信封属 Epic 5 的评估模块，避免双口径。Epic 5 判定方读 Redis 权威值而非任何快照。

**真实准确率门禁沿用 deferred：** 「识别准确率 ≥95%」依赖分类器质量与标注语料，与 2.2 的 ≥90% 门禁同根因；真实验收随 2.2 deferred 的语料门禁一并复跑，本 Story 交付确定性判定逻辑单测（计数/标记/触发边界）。

## Verification

**Commands:**
- `uv run pytest tests -q` -- expected: 既有用例 + 新增全部通过，零回归。
- `uv run ruff check src tests` -- expected: 无告警。
- `uv run mypy src` -- expected: strict 无错误。
