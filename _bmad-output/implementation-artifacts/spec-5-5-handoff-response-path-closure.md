---
title: 'Story 5.5 转人工后会话响应路径关闭'
type: 'feature'
created: '2026-11-10'
status: 'done'
baseline_commit: '09018974c0aa0da0a7d18876853ba7335191f5a7'
review_loop_iteration: 0
followup_review: true
followup_review_recommended: true
context: []
warnings: []
deferred:
  - summary: >-
      L1 进程内 handoff 快照契约（get/set_handoff_status、invalidate_handoff）
      自 3.3 预置以来全仓库零消费者，5.5 明确不消费后成为孤儿契约
    evidence: |-
      本 Story 之前 5.1/5.3 亦未消费（5.1 硬触发判定禁读 L1），孤儿状态
      非本 Story 引入；存废（删除或留作未来优化层）需人拍板，已超出
      Story 5.5 意图范围
    location: >-
      src/weiliao_agent/cache/l1_cache.py
    severity: low
  - summary: >-
      已转接会话存在固有在途竞态：闸门实读 handoff_status 后、管道执行前，
      另一消息可能并发 mark_handoff，使在途消息走完 AI 管道
    evidence: |-
      窗口真实存在，但属系统既有且在文档中接受的竞态语义（1.3「处理中途
      key 过期不中断在途消息」、5.3 适配器闸门 begin_turn 快照同款窗口）；
      修法（按 session 串行化或管道后复查）会改变既定在途语义，非本 Story
      引入，归系统性口径拍板
    severity: low
---

<intent-contract>

## Intent

**Problem:** 5.3 的终态闸门只挂在 HTTP 适配器入口（``begin_turn`` 快照判定），编排器本身对会话 handoff 状态无感知——任何绕过适配器直接调用 ``orchestrator.run()`` 的入口（未来消费者/内部调用方）都会把已转人工会话的消息走完整个 AI 管道，产生人机混答，违反 FR-12 合规红线。

**Approach:** 编排器新增终态闸门守卫层：组合根注入 SessionManager 的 handoff 状态读取引用，``run()`` 入口在 trace_id 回填后、打开 trace 上下文之前，经 SessionManager 实读 Redis 权威 ``handoff_status``——``"triggered"`` 即短路（零过滤器执行、零 LLM、零模板兜底），返回与 5.3 适配器闸门逐字节一致的安抚文案；5.3 适配器闸门保留不动（HTTP 主流径行为不变），编排器闸门作为权威关闭点对全部入口生效。

## Boundaries & Constraints

**Always:** handoff 判定权威值只认 Redis——编排器每次实读（不缓存、不读 L1 快照），会话过期（TTL 30 分钟）即读不到、放行正常管道（AC3 从零初始化语义）；短路发生在任何过滤器执行之前，信封不过滤不重建；闸门应答字节口径与 5.3 适配器闸门 100% 一致（两处共享同一构建来源，防漂移）；未知状态值与 Redis 读取失败一律 fail-closed 既有兜底（degraded 模板兜底 + 原 trace_id），绝不放行 AI 管道（绝不人机混答）；trace 事件与 WARNING 日志 fail-safe，绝不改变应答；编排器永不向上抛异常；不触碰 5.2 Kafka 指令链路与 5.3 安抚发送语义、5.1 触发轮短路语义。

**Never:** 不改 ``PipelineResult`` 响应契约（不加字段、响应体形状不动）；不把 L1 进程内 handoff 快照或任何进程内状态作为闸门判定依据（NFR-8 实例无状态，L1 快照仅作优化层、不影响正确性）；不在编排器内做 ``begin_turn``/自增 turn/刷新 TTL（会话写仍归 AD-4 单写者、入口适配器）；不重写 5.1 硬触发评估与触发轮出口；不动 5.2 指令发送器契约；闸门禁用（未注入 reader）时行为与 5.5 之前逐字节一致。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 期望输出 / 行为 | 错误处理 |
|------|------------|----------------|----------|
| 已转接会话新消息 | handoff_status="triggered"（reader 已注入） | 编排器短路：全部过滤器零执行、无 LLM 调用、无模板兜底下发；回 ``前缀+HANDOFF_REPLY_TEXT``，degraded=False；记 already_handed_off + ai.disclosure 事件与 WARNING 日志；无二次转交指令 | 事件/日志记录失败吞掉，应答逐字节不变 |
| 未触发会话 | handoff_status="none" | 走正常 AI 管道，行为与 5.5 前逐字节一致 | N/A |
| 状态缺失 / 会话过期 | reader 返回 None（key 或字段不存在） | 放行正常管道（过期后从零初始化语义由入口 begin_turn 承载） | N/A |
| 未知状态值 | handoff_status 非 none/triggered（Redis 损坏值） | fail-closed：degraded=True 模板兜底 + 原 trace_id，不进管道 | N/A |
| Redis 读取失败 | reader 抛 ``redis.RedisError`` | fail-closed：同未知值兜底口径，绝不放行管道 | N/A |
| trace_id 缺失 | 闸门短路且信封无 trace_id | 入口权威回填（32-hex）后照常记录事件与响应 | N/A |
| 未注入 reader | 本地/测试组装（None） | 闸门禁用，正常管道，与 5.5 前逐字节一致 | N/A |

</intent-contract>

## Code Map

- `src/weiliao_agent/pipeline.py` -- 核心改造点。`PipelineOrchestrator.run()`（L131-156）入口：trace_id 权威回填点之后、``trace_message`` 之前插入闸门检查；``__init__``（L110-139）新增可选注入参数。``_handoff_result``（L213+）与 http 闸门共享的字节口径先例（``f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"``、``record_ai_disclosure``）。``HandoffCommandSender`` Protocol（L75-89）为「编排器只依赖窄协议」的注入先例。
- `src/weiliao_agent/session/manager.py` -- 需新增只读原语 ``read_handoff_status(session_id) -> str | None``（HGET 单 roundtrip；key/字段缺失返回 None；纯读不刷新 TTL、不初始化——与 ``read_snapshot`` L320-345 同为只读先例，但口径更窄、不解析 turn/时间戳）。``mark_handoff``（L290-306）为状态权威写入方，本 Story 不动。
- `src/weiliao_agent/adapters/http.py` -- 5.3 终态闸门（L182-215）：``snapshot.handoff_status == "triggered"`` 分支的应答构建、事件与日志代码将改为调用共享 helper（行为逐字节不变，既有 ``test_handoff_terminal_state.py`` 锁定）。该闸门保留——HTTP 主流径仍先于编排器拦截，编排器闸门对直接调用入口生效。
- `src/weiliao_agent/observability/tracing.py` -- ``record_already_handed_off``（L595）、``record_ai_disclosure``（L628）为闸门事件先例，只读复用。
- `src/weiliao_agent/cache/l1_cache.py` -- ``get/set_handoff_status``/``invalidate_handoff``（L160-185）是 3.3 为 Epic 5 预置的 L1 handoff 快照契约；本 Story 闸门**不消费** L1（判定只认 Redis 权威值），仅保持「L1 为优化层、不影响正确性」的 NFR-8 口径，本文件不动。
- `src/weiliao_agent/main.py` -- 组合根（L550-580）：``PipelineOrchestrator(...)`` 装配点注入 ``handoff_status_reader=_session_manager``；模块级 ``_session_manager``（L170 附近）现成。
- `tests/test_handoff_terminal_state.py` -- 5.3 闸门锁定测试（fakeredis + TestClient + 计数过滤器/编排器替身），共享 helper 重构后必须原样全绿；新增 ``_CountingFilter`` 可复用为零执行断言模式。
- `tests/test_session.py` -- ``SessionManager`` fakeredis 测试先例，新增只读原语测试挂这里或随新测试文件。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/session/manager.py` -- 新增 ``read_handoff_status(session_id) -> str | None``：HGET ``handoff_status`` 单 roundtrip，key/字段缺失返回 None，纯读不刷新 TTL/不初始化字段，损坏值原样返回（判定归调用方）；写失败不吞异常口径不变 -- 闸门权威读取原语（AD-4 只读先例）。
- [x] `src/weiliao_agent/pipeline.py` -- 新增 ``HandoffStatusReader`` Protocol（``read_handoff_status(session_id) -> str | None``，窄协议同 ``HandoffCommandSender`` 先例）与共享 helper ``handoff_gate_result(trace_id, *, session_id, turn)``（公开名，构建闸门 ``PipelineResult`` + already_handed_off/ai.disclosure 事件 + WARNING 日志，字节口径即 ``f"{AI_DISCLOSURE_PREFIX}{HANDOFF_REPLY_TEXT}"``、degraded=False）；``__init__`` 增 ``handoff_status_reader: HandoffStatusReader | None = None``；``run()`` 入口 trace_id 回填后查闸门：``"triggered"`` → 返回共享 helper 结果（不打开 trace_message、不进 ``_run_chain``、零过滤器执行）；未知值或读取异常 → ``_degraded`` fail-closed 兜底（归因 handoff_gate）；``"none"``/None → 正常管道 -- 编排器级响应路径关闭（AC1/AC2/AC3）。
- [x] `src/weiliao_agent/adapters/http.py` -- 5.3 闸门 ``"triggered"`` 分支的内联应答/事件/日志改为调用 pipeline 共享 helper（session_id/turn 入参透传），分支语义、字节与既有测试逐字节不变 -- 双闸门单一字节来源，防两处漂移。
- [x] `src/weiliao_agent/main.py` -- 组合根 ``PipelineOrchestrator(...)`` 注入 ``handoff_status_reader=_session_manager``（``None`` 闸门语义不变，无新 env） -- 生产装配生效。
- [x] `tests/test_handoff_response_path_closure.py`（新）-- 编排器直接调用端到端（fakeredis 真 SessionManager + 计数过滤器链 + 假发送器）：mark_handoff 后 ``run()`` 断言全过滤器零执行、reply 逐字节等于 ``前缀+HANDOFF_REPLY_TEXT``、degraded=False、发送器零调用、fake_langfuse 已挂两事件、WARNING 日志；``none``/None 状态走正常管道（过滤器执行）；未知值与读取异常 fail-closed（degraded=True 模板兜底、零过滤器执行）；trace_id 缺失回填/携带透传；未注入 reader 时闸门禁用 -- AC「端到端测试用例存在且通过」+ 回归网。

**Acceptance Criteria:**
- Given session 已 ``mark_handoff``（Redis ``handoff_status="triggered"``），when 同 session 新消息经编排器 ``run()`` 到达，then 编排器经 SessionManager 实读权威状态后短路——全部过滤器零执行、无 LLM 生成、无模板兜底应答下发，买家侧应答逐字节等于 5.3 闸门文案（``前缀+HANDOFF_REPLY_TEXT``，degraded=False），且无二次转交指令。
- Given 短路发生，when 查询 Langfuse trace 与服务日志，then 留有 ``already_handed_off`` 事件、``ai.disclosure``（disclosed=True）事件与 WARNING 级日志；无 ``record_delivery`` 事件。
- Given handoff 状态不存在或会话已过期（TTL 30 分钟，reader 返回 None），when 新消息到达编排器，then 放行正常 AI 管道（从零初始化语义由入口 ``begin_turn`` 承载，编排器不做会话写）。
- Given ``handoff_status`` 为未知值或 Redis 读取抛错，when 新消息到达编排器，then fail-closed 既有兜底（degraded=True 模板兜底 + 原 trace_id），绝不放行 AI 管道。
- Given 5.2/5.3 转人工分支（Kafka 指令 + 买家安抚通知 + 适配器终态闸门），when 跑全量回归，then 该路径行为逐字节不变（``test_handoff_terminal_state.py`` 原样全绿）。
- Given 编排器未注入 reader（None），when 任意消息到达，then 行为与 5.5 之前逐字节一致（闸门禁用）。

## Spec Change Log

## Review Triage Log

评审轮 1（2026-11-10，四层：盲审 10 / 边界 2 / 验证缺口 1 / 意图对齐 6，共 19 条）：
- verdicts: 19 findings — high 0, medium 3, low 10, false 6, maybe-false 0
- findings:
  - `[medium]` `[patch]` 盲#1/边#1 同根：闸门只捕获 ``redis.RedisError``，reader 其他异常（如非 UTF-8 字节的 UnicodeDecodeError）逃逸 run()，违反「编排器永不向上抛异常」与 Protocol 契约 -- 已 patch：catch 扩至 ``Exception``（fail-closed 兜底不变），全量 794 绿
  - `[low]` `[reject]` 盲#2：闸门 HGET 在 deadline 预算之外，最坏 +0.5s -- 属实但日常不可见（典型 RTT 毫秒级，超时场景管道本即降级），修复属设计改动非直接修正，不采纳
  - `[low]` `[reject]` 盲#3：全量消息多付一次 Redis RTT、无熔断器、闸门无计数指标 -- 具名损害不成立（trace 事件已可审计；指标/熔断属意图外新增面），不采纳
  - `[low]` `[patch]` 盲#4：http.py 跨模块导入下划线私有 ``_handoff_gate_result`` -- 已 patch：改名公开 ``handoff_gate_result`` 并更新导入
  - `[low]` `[patch]` 盲#5：``read_handoff_status`` 原语自身无单测（纯读不刷 TTL/不初始化、损坏值原样返回无锁定） -- 已 patch：test_session.py 补 5 个 fakeredis 聚焦用例
  - `[low]` `[patch]` 盲#6 + 意图#1 + 意图#2 同根：双闸门 armed 配置的 HTTP 级交互（恰好一次 already_handed_off 事件、零 delivery、零 handoff_gate 降级）无测试锁定，「端到端」仅停在编排器直接调用面 -- 已 patch：新增 TestClient 级 armed 双闸门用例
  - `[low]` `[defer]` 盲#7：L1 handoff 快照契约零消费者成孤儿 -- 属实但非本 Story 引入（3.3 预置起即零消费，5.1 禁读 L1），存废需人拍板，已录 deferred
  - `[false]` `[reject]` 盲#8：规格 Verification 段未记录结果 -- 结果由工作流 Finalize 落 ``## Auto Run Result``（本段），模板该段本就只列命令，判伪
  - `[low]` `[patch]` 盲#9：turn 透传进闸门 helper 从未被断言（测试全 turn=None） -- 已 patch：短路用例置 turn=7 并断言事件 metadata 与 WARNING 日志携 turn
  - `[false]` `[reject]` 盲#10/意图#6 同根：epic-5-context.md 头注由 Compiled 变 Generated -- 该文件系 step-01 缓存失效后由工作流强制的 compile-epic-context 再生，头注与 compile 模板一致，判伪
  - `[low]` `[defer]` 边#2：闸门实读后、管道执行前并发 mark_handoff 的在途竞态（人机混答窗口） -- 窗口真实但属文档化接受的既有语义（1.3 过期竞态、5.3 同款窗口），非本 Story 引入，修法改变既定在途语义，已录 deferred
  - `[medium]` `[patch]` 缺口#1（预验证，变异实证）：main.py 注入行无测试锁定——删除注入全库测试仍绿，闸门可静默失效 -- 已 patch：test_handoff_composition_root.py 补装配断言（删行即红）
  - `[false]` `[reject]` 意图#3：AC4「不触碰」字面违反（改了 5.3 适配器闸门分支） -- 5.3 套件全绿锁定字节等价，AC4 所指「指令与安抚发送链路」未动，判伪
  - `[false]` `[reject]` 意图#4：AC2「零模板兜底」与损坏值/读取失败的 fail-closed 兜底字面冲突 -- 本规格 I/O 矩阵明文规定该行为（捕获的意图即「拿不准就兜底」），AC2 兜底禁令指已知转接的短路路径，判伪
  - `[false]` `[reject]` 意图#5：编排器面无生产入口、保护的是假想面 -- 即本规格 Problem 段明文拍板的设计前提，判伪

## Design Notes

- 闸门放两处而非搬走：5.3 适配器闸门保留（HTTP 主流径，判定数据天然在 ``begin_turn`` 快照手中，冻结语义不动）；编排器闸门是权威关闭点，覆盖直接调用入口。HTTP 流程中编排器闸门不触发（适配器先拦截），事件零重复。
- 判定只认 Redis 实读、不读 L1：L1 handoff 快照 TTL ≤30s 与 Redis 会话 TTL 30 分钟之间存在过期竞态（会话过期后 L1 可能短暂残留 "triggered"），正向信任 L1 会误关新会话；反向（L1 miss 回 Redis）又无节省。Redis HGET 单 roundtrip（socket 超时 0.5s）在 2.5s 预算内可忽略，故按「拿不准就回权威值」口径不消费 L1——满足 AC「L1 快照仅作优化层读取，不影响正确性（NFR8）」。
- Redis 读取失败 fail-closed 而非放行：与 5.3「拿不准就回既有兜底，绝不人机混答」同口径；HTTP 流程中 Redis 故障已在适配器 ``begin_turn`` 处兜底，编排器读失败仅影响直接调用入口。
- 共享 helper 而非复制粘贴：5.3 评审已确立「编排器出口与 http 闸门两处逐字节一致」，本 Story 出现第三处同字节出口，内联复制会引入漂移面；helper 行为等价替换由既有 5.3/5.4 锁定测试全绿保证。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增端到端用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警
- `uv run mypy src` -- expected: 零告警（仓库 mypy strict 口径，仅扫 src）

## Auto Run Result

Status: done

**Summary of implemented change:**
编排器新增终态闸门守卫层：``run()`` 入口经注入的 ``HandoffStatusReader``（组合根接 ``SessionManager``）实读 Redis 权威 ``handoff_status``——``"triggered"`` 短路（零过滤器执行、零 LLM、零模板兜底，应答/事件/日志经共享 helper ``handoff_gate_result`` 与 5.3 适配器闸门逐字节一致）；``"none"``/缺失（会话过期）放行正常管道；未知值与读取异常 fail-closed 既有兜底（degraded 模板兜底 + 原 trace_id）。5.3 适配器闸门保留，编排器闸门对直接调用入口生效。判定不消费 L1 快照（仅 Redis 权威，NFR-8）。

**Files changed:**
- `src/weiliao_agent/session/manager.py` -- 新增只读原语 ``read_handoff_status``（HGET 单 roundtrip，缺失返回 None，纯读不刷 TTL/不初始化）
- `src/weiliao_agent/pipeline.py` -- ``HandoffStatusReader`` Protocol、公开共享 helper ``handoff_gate_result``、``__init__`` 可选注入、``run()`` 入口闸门（catch ``Exception`` fail-closed）
- `src/weiliao_agent/adapters/http.py` -- 5.3 闸门 triggered 分支改调共享 helper（字节等价）
- `src/weiliao_agent/main.py` -- 组合根注入 ``handoff_status_reader=_session_manager``
- `tests/test_handoff_response_path_closure.py`（新，9 用例）-- 编排器直接调用端到端全矩阵 + armed 双闸门 HTTP 级用例 + turn 透传断言
- `tests/test_session.py` -- ``read_handoff_status`` 5 个聚焦单测
- `tests/test_handoff_composition_root.py` -- 装配注入断言（删注入行即红）
- `_bmad-output/implementation-artifacts/epic-5-context.md` -- step-01 强制再生（epics.md 已含 Story 5.5）

**Review findings breakdown:**
- patches applied: 6 项 9 行记录 — 闸门 catch 扩至 ``Exception``（medium）、组合根注入测试锁定（medium，变异实证）、helper 改名公开（low）、``read_handoff_status`` 单测（low）、armed 双闸门 HTTP 用例（low）、turn 透传断言（low）
- deferred: 2 项（L1 handoff 孤儿契约——3.3 预置即零消费，非本 Story 引入；在途 mark_handoff 竞态——1.3/5.3 文档化接受的既有语义）
- rejected: 8 项（盲#2 预算外 RTT 理论损害、盲#3 指标/熔断属意图外新增面、盲#8 结果落点误读、盲#10/意图#6 工作流强制再生误读、意图#3 字节等价已锁定、意图#4 矩阵明文规定、意图#5 故事既定前提）

**Follow-up review recommendation:** true
本轮 patched 2 个 medium 条目，触发 follow-up。具体未核风险：闸门 catch 扩至 ``Exception`` 后，reader 内部编程错误不再上抛（与 http.py ``begin_turn``「仅收敛 redis 异常族」的仓库惯例相反），该口径取舍靠测试网兜底、未经独立复核。

**Verification performed:**
- `uv run pytest tests/ -q` -- 794 passed（含新增 14 用例，5.3/5.4 回归全绿）
- `uv run ruff check src tests` -- All checks passed
- `uv run mypy src` -- no issues found in 48 source files
- 矩阵审计：I/O 矩阵 7 行全覆盖（8 初始用例 + 补丁新增断言），全部实跑通过

**Residual risks:**
- 编排器闸门在 HTTP 主流径恒不触发（适配器先拦截），其生产价值依赖未来非 HTTP 入口；行为由 armed 双闸门用例锁定
- Redis 权威实读为每消息一次额外 HGET（毫秒级）；无闸门专用计数指标，审计走 Langfuse trace 事件
