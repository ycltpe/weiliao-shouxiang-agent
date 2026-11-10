---
title: 'Story 7.2 超时风暴压测与执行器生命周期重审'
type: 'feature'
created: '2026-11-10'
status: 'done'
followup_review_recommended: true
baseline_commit: '5372369cacfa9ecf3e030fc29a2d31de38177700'
review_loop_iteration: 0
context: ['{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md']
warnings: [oversized]
deferred: []
---

<intent-contract>

## Intent

**Problem:** 编排器每请求新建 ThreadPoolExecutor 且超时丢弃的执行线程无取消/复用机制（1.2 挂账），Epic 4 真实过滤器落地后超时风暴下线程可能积累、P99 劣化、资源膨胀；被超时丢弃的生成调用在后台跑完、浪费 token 无任何归因（4.3 挂账）；信封未 frozen 的所有权风险（1.1/1.2 挂账）未裁断；Redis socket 超时硬编码 0.5s 不可配（6.1 挂账）；FR-1 含消解端到端 P99 < 50ms 无实测（6.1/6.2/6.3 挂账）。

**Approach:** 先建可重复的进程内压测设施（llm_generate 注入慢速 stub 子图驱动超时风暴），以实测数据重审执行器生命周期三个决策点（共享池 / 取消机制 / 信封 frozen 所有权）并落地决策 + 回归测试；被丢弃生成调用的 token 归因落地记账或记录"不做"决策理由；Redis socket 超时改为 WEILIAO_REDIS_SOCKET_TIMEOUT 组合根 fail-fast 配置；以含 coreference → intent_router 全链的压测执行 FR-1 P99 实测。

## Boundaries & Constraints

**Always:** 买家可见应答、`degraded` 语义、过滤器链序与默认预算值逐字节不变；决策必须基于压测实测数据，三个决策点各留决策记录（选项/证据/结论/代码落点）入本 spec Design Notes；压测设施脚本化、可重复、非零退出即断言失败；压测全程不触达真实 OpenAI / Redis / Kafka（stub / fakeredis / 不注入）；新 env 变量同步 README 变量表与 .env.example（7.1 守卫测试强制反向一致）；文档与决策记录用中文；不虚构压测数据，实测不到的门（如无凭据的真实 API 延迟）显式 skip 并记录。

**Never:** 不改任何过滤器业务逻辑与既有超时/阈值参数默认值；不引入外部压测框架（locust 等）依赖——标准库 threading 即可；不做需要部署形态的运维动作（K8s liveness 等仍挂账）；不动 7.1 交付物结构与既有测试语义；不为过 frozen 决策而扩大重构 blast radius  beyond 信封写点与编排器（4 处原地写点见 Code Map）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 超时风暴压测 | 慢 stub 子图（单调用 > 编排器剩余预算）+ 并发请求群 | 逐跳超时即兜底应答；存活线程数风暴后不增长超基线预算；应答 P99 不劣化超端到端预算 | 无请求傻等全量超时；压测脚本断言失败非零退出 |
| 慢调用完成于丢弃后 | 被丢弃的生成调用在后台跑完 | 其 token 计入归因口径（metrics 计数 + trace 事件），或决策记录给出"不做"理由 | 记账本身 fail-safe，绝不影响应答路径 |
| WEILIAO_REDIS_SOCKET_TIMEOUT 非法 | 组合根导入期该变量非数值/非正/超上界 | 启动 fail-fast，错误消息带变量名 | 缺省回落 0.5s（现状值），合法值生效于 redis 客户端两个 timeout 位 |
| FR-1 全链压测 | coreference → intent_router 全链（真实规则 resolver + 分类器） | 产出 P99 实测值与 < 50ms 判定；分类器凭据不可用时该门 skip 并记录原因，不虚构 | 压测线程超时不悬挂进程 |

</intent-contract>

## Code Map

- `src/weiliao_agent/pipeline.py:233-296` -- `_run_chain` 执行器现状：每请求 `ThreadPoolExecutor(max_workers=len(filters))` + `shutdown(wait=False)`；`_degraded` 两种归因入口；`_handoff_result` 出口分支（决策落地时行为不得变）
- `src/weiliao_agent/pipeline.py:204` -- 信封原地写点之一（trace_id 权威回填）；另三处在 `src/weiliao_agent/adapters/http.py:188,201,224`（turn/trace_id 入口适配器）——frozen 决策的全部 blast radius
- `src/weiliao_agent/models/envelope.py:70` -- Envelope（BaseModel 非 frozen）；过滤器全链均为 model_copy 整信封替换（`filters/llm_generate.py:157` 等），无过滤器原地变更
- `src/weiliao_agent/filters/llm_generate.py` -- 生成落点：注入 `LlmGenerationSubgraph` 接缝（压测慢 stub 注入点）；`_generate` 单调用路径
- `src/weiliao_agent/llm/client.py:115-140` -- OpenAiGenerationClient 预算/attempt 超时语义（真实客户端 1.8s 慢调用先例）；压测 stub 需模拟"客户端内超时归一 + usage 回传"形态
- `src/weiliao_agent/observability/metrics.py:381-460` -- ModelRoutingMetricsCollector token 记账现状（成功口径 record_call）；被丢弃调用归因的候选挂点
- `src/weiliao_agent/main.py:143,157-161` -- REDIS_SOCKET_TIMEOUT_SECONDS=0.5 硬编码与 redis 客户端组装点（WEILIAO_REDIS_SOCKET_TIMEOUT 落点）
- `src/weiliao_agent/session/manager.py` -- 会话写路径（begin_turn 同步 SET 预算的受益方，只读参考）
- `src/weiliao_agent/health/probe.py` -- /health 快照挂点（压测期间读数可选；不改）
- `tests/test_pipeline.py:55-75,112-130` -- `_SlowFilter` 与既有超时测试先例（压测 stub 与断言风格参照）
- `tests/eval_intent_classifier.py` / `tests/eval_coref_resolver.py` -- 既有评测 harness 模式（FR-1 压测设施参照其 CLI/退出码风格）
- `tests/test_env_example.py` -- 7.1 守卫测试：新 env 变量必须同步 .env.example 与 README 表
- `_bmad-output/implementation-artifacts/deferred-work.md:12-17,~67,100,102-123` -- 待关闭/重路由挂账条目原文

## Tasks & Acceptance

**Execution:**
- `scripts/loadtest_timeout_storm.py` -- 新建可重复压测设施：进程内组装编排器（慢 stub 子图注入 llm_generate，单调用延迟 > 剩余预算触发逐跳超时丢弃），并发请求群打完超时风暴，量取并打印存活线程数（threading.enumerate 口径）、应答 P99、丢弃计数；内置断言（线程有界、P99 在预算内）失败非零退出；CLI 参数化并发数/请求数/单次延迟 -- 决策证据来源与 NFR10 验收工具
- `src/weiliao_agent/pipeline.py` + `tests/test_pipeline.py` -- 按 D1/D2/D3 决策记录落地执行器生命周期改造（共享池/取消/信封所有权三决策点各出结论），并为落地形态补回归测试：风暴后线程有界、超时丢弃语义与应答字节不变（既有 822 用例逐字节背书基线）-- 三决策落地
- `src/weiliao_agent/observability/metrics.py` + `tests/test_model_routing_metrics.py`（或就近新测试文件）-- 被丢弃生成调用的 token 归因：调用完成点记账（区别于成功口径）+ trace 事件，或按 D4 决策记录"不做"理由 -- 4.3 挂账收口
- `src/weiliao_agent/main.py` + `README.md` + `.env.example` + `tests/test_env_example.py` -- WEILIAO_REDIS_SOCKET_TIMEOUT 可配置（浮点秒，非正/非数值/超上界 fail-fast 带变量名，上界须远小于 2.5s 内部预算）；README 变量表补行、.env.example 补条目、守护测试补用例 -- 6.1 挂账收口
- `scripts/loadtest_intent_chain.py`（或并入上设施为子命令）-- FR-1 全链压测：真实 CoreferenceFilter + 分类器（llm_zero_shot 凭据门：无 OPENAI_API_KEY 时显式 skip 并记录，凭据门内测真实延迟；门内断言 P99 < 50ms）-- FR-1 实测验收
- `_bmad-output/implementation-artifacts/deferred-work.md` -- 关闭 1.2 执行器/信封 frozen（12-17 行）与 4.3 token 归因（~67 行）挂账；6.1 Redis 超时（100 行）与 FR-1/coref P99（102-123 行）条目重路由标注至本 Story 收口结果 -- 挂账台账一致性

**Acceptance Criteria:**
- Given 慢 stub 子图与压测设施，when 执行超时风暴压测，then 风暴后存活线程数不增长超基线预算、应答 P99 不劣化超端到端预算（NFR10 验收口径），设施可重复执行且断言失败非零退出
- Given 三个决策点的决策记录（Design Notes），when 审阅，then 共享池/取消机制/信封 frozen 各有选项、实测证据、明确结论与代码落点，且结论已落地于 pipeline.py 并有回归测试
- Given 被超时丢弃的生成调用在后台完成，when 查询归因口径，then token 消耗有独立记账与 trace 事件可查，或决策记录载明"不做"的理由与再触发条件
- Given WEILIAO_REDIS_SOCKET_TIMEOUT 设为非法值，when 导入组合根，then fail-fast 且错误消息带变量名；设为合法值时 redis 客户端 socket_timeout/socket_connect_timeout 均生效
- Given FR-1 全链压测设施，when 以含 coreference → intent_router 全链执行，then 产出 P99 实测值与 < 50ms 判定；分类器凭据不可用时显式 skip 并记录原因，不虚构数据
- Given 全部落地完成，when `uv run pytest` 与 `uv run ruff check src tests` / `uv run mypy`，then 全绿（既有测试零回归）
- Given deferred-work 台账，when 核对 1.2/4.3/frozen/FR-1/Redis 五组挂账条目，then 均已关闭或标注重路由至本 Story 结论

## Spec Change Log

## Review Triage Log

### 2026-11-10 — Review pass 1
- verdicts: 27 findings — high 0, medium 4, low 17, false 6, maybe-false 0
- layers: blind 14 / edge 4 / verification-gap 4（2 条 filed 预验证采信）/ intent-alignment 5
- findings:
  - `[low]` `[patch]` 脚本 docstring「形态 A（默认，--budget 0.5）」与 argparse 默认 2.5 不符 — 已修 docstring：形态 A 需显式 --budget 0.5，argparse 默认即形态 B。
  - `[low]` `[patch]` docstring 声称「Redis 用 fakeredis」实为 `CacheLookupFilter(l0_client=None)` 零 I/O 透传 — 已修文案与实现一致（且与「不触达真实 Redis」边界相符）。
  - `[low]` `[patch]` `_run_storm` docstring 称返回 3 元组、实际 4 元组（基线未载）— 已修 docstring。
  - `[low]` `[patch]` WEILIAO_REDIS_SOCKET_TIMEOUT 合法值正路径无接线断言（AC4 后半可静默回归硬编码）— 已补子进程用例（0.7 → connection_kwargs 两个 timeout 位均 == 0.7）+ 边界接受参数化（1.0/0.1）。
  - `[low]` `[patch]` ruff/mypy 验证面缺 scripts 与全 src — spec Verification 节扩为 `ruff check src tests scripts` / `mypy src`，主代理运行通过（48 文件 no issues）。
  - `[low]` `[reject]` 编排器无 close()，实例弃置残留 ≤链长空闲线程 — 生产单实例零影响；且 CPython ThreadPoolExecutor 空闲 worker 阻塞于 work queue，`shutdown(wait=False)` 亦不释放空闲线程，所提修复无法兑现声称效果；pytest 进程退出即清，非日常可达缺陷。
  - `[low]` `[patch]` 线程有界断言计数进程全局线程、受其他测试残留池影响（实测 4→2→1 漂移）— 已改增量口径（测试前外来池线程基线 + 池大小上界）。
  - `[low]` `[patch]` `LLM_CLIENT_MAX_WORKERS = 8` 为 llm/client.py `_MAX_WORKERS` 的手写副本 — 已改为直接导入，消除漂移面。
  - `[low]` `[patch]` `orchestrator.loadtest_completions` 猴子补丁 + `type: ignore` — 已改 `_build_orchestrator` 返回 `(orchestrator, completion_log)` 元组。
  - `[low]` `[reject]` http 逃逸兜底路径 trace 分裂（frozen 后不复用回填 trace_id）— 仅编排器自身 bug 时可达的防御路径；与 1.5 既有挂账（`_fallback_result` 36 位 id 不可检索）同根，实现者已在交付报告标注，随该挂账一并裁断；非日常可达。
  - `[false]` `[reject]` assert 仅编译期、python -O 下 `handoff_gate_result` 会收到 None — 证伪：`if + model_copy` 是真实运行时守卫（回填后 `envelope.trace_id` 确有值），assert 仅 mypy 类型收窄，-O 删除后运行时行为逐字节不变。
  - `[medium]` `[patch]` 共享池排队自提交时刻消耗逐跳预算：并发超池大小时排队等待可拖垮自身全快的请求（旧每请求执行器下可成功），「应答字节不变」论断此前只经全降级风暴测试 — Design Notes D1 已补「排队预算效应」段：固有权衡、worker 占用 ≤ 单跳执行时长（窗口有界）、降级语义与兜底字节口径不变；混合负载差异记录为残余风险。
  - `[false]` `[reject]` spec 元数据（status in-progress）与 deferred-work closed 不一致 — 工作流自身生命周期维护（收口步统一置 done），非缺陷。
  - `[low]` `[patch]` 两压测脚本无运维文档、intent_chain 默认参数 400 次真实 OpenAI 调用无成本提示 — README「压测设施」节已补：用法、退出码语义、凭据门行为、成本提示。
  - `[medium]` `[patch]` 丢弃归因回调仅在客户端 FutureTimeoutError 分支注册——「编排器先丢、客户端预算内成功完成」窗口不进丢弃桶（其 token 记成功口径）— 口径修正：metrics.py / tracing.py docstring 与 Design Notes D2/D4 改为精确口径（丢弃桶 = 客户端硬预算放弃形态），显式记录该已知窗口（可达条件、成功口径/成本护栏仍捕获、hop-discard 信令属 D2 已拒范畴）；买家应答与成本可见性不受影响。
  - `[low]` `[reject]` 编排器无 close()（与盲审同证据）— 同行 reject 理由。
  - `[low]` `[patch]` `--budget 0`/负数下断言空虚通过（零慢调用执行仍打印「断言通过」）— 已加正有限数 CLI 校验。
  - `[low]` `[patch]` `--llm-budget` 非正崩溃退出、`--call-delay nan` 绕过 delay>budget 校验 — 已并入同一数值校验块（isfinite 先行）。
  - `[medium]` `[patch]`（vgap 预验证采信）合法 redis 超时值从未验证落到客户端两个 timeout 位 — 同本表第 4 行补丁。
  - `[low]` `[patch]`（vgap 预验证采信）`emit_discarded_generation_metrics_log` 载荷形态无测试、破坏兄弟 emitter 皆有测试的仓库惯例 — 已补 caplog 结构化 JSON + logger 失败 fail-safe 两用例。
  - `[low]` `[patch]`（vgap）`loadtest_intent_chain` 编排器组装在凭据门之后、skip 路径下从不执行（kwarg 漂移潜伏）— 组装已移至凭据门之前（LlmZeroShotClassifier 构造凭据惰性、已验证安全）。
  - `[medium]` `[patch]`（vgap）共享池排队预算消耗/字节不变性论断依赖全降级测试 — 与本表「排队预算效应」行同根同补丁。
  - `[low]` `[patch]`（intent 层）FR-1 挂账标 closed 而 P99 实测门未执行（ledger 说关、数字不存在）— deferred-work 该条目已重开为 open-2026-11-10（设施就位、凭据后复跑取得判定即关）。
  - `[false]` `[reject]`（intent 层）NFR10 进程内模拟 ≠ 运行中服务实测 — 证伪：Epic 上下文验收口径本身规定「压测用可控慢过滤器驱动（llm_generate 以 stub 控制慢速）」，进程内 stub 即意图规定的测量形态。
  - `[false]` `[reject]`（intent 层）「慢调用可取消」want 未落地代码 — 证伪：AC 原文「共享池/cancel 机制/信封所有权有明确决策记录并落地」，D2 决策记录（不做 + 压测证据 + 再触发条件）即该决策点的落地形态。
  - `[false]` `[reject]`（intent 层）Redis 超时「评估」只做了算术论证未做测量 — 证伪：6.1 挂账 settle 原文即提议「如 WEILIAO_REDIS_SOCKET_TIMEOUT 组合根 fail-fast 配置」，本 Story 按既定 settle 路径落地机制并文档化边界。
  - `[false]` `[reject]`（intent 层）test_tracing 断言反转属契约暗改 — 证伪：D3 决策记录显式载断该所有权口径（调用方信封保持 None、响应 trace_id 权威），验收意图（响应携带 + 全链同一 trace）逐字节保持，非暗改。

## Design Notes

决策记录（实现期已按压测实测数据填充，step-04 审查重点）。实测环境：
macOS aarch64 / CPython 3.11.15，压测设施
`scripts/loadtest_timeout_storm.py`（进程内组装，慢 stub 经
`OpenAiGenerationClient` 注入同接口 chat 替身——预算内归一抛
``LlmGenerationError("timeout")``、后台跑完回传确定性 usage
total_tokens=27），全程序不触达真实 OpenAI / Redis / Kafka。

**实测基线（D1 改造前，每请求新建 ThreadPoolExecutor，形态 A：
24 并发 × 4 请求 = 96 请求，单调用延迟 2.0s，编排器预算 0.5s <
客户端预算 0.9s，2026-11-10）：** 风暴期存活线程峰值 **100**（基线 1，
``threading.enumerate()`` 采样口径），排空后 13；96/96 应答逐跳兜底
（degraded=True，归因可逐字节查）；P99 = 522ms（预算 500ms + 200ms
断言内）；被丢弃慢调用完成 24 次。——1.2 挂账「超时风暴下线程可能
积累」预估坐实为真：执行线程随并发线性膨胀（峰值 ≈ 基线 + 并发请求
线程 + 每请求执行线程 + 客户端池 8）。

- **D1 执行器共享池 vs 每请求新建**——**结论：共享池（选项 b），已落地。**
  选项：(a) 每请求新建 + shutdown(wait=False)（现状，压测实证线程无界）；
  (b) 编排器级共享池（随编排器生命周期共存，大小 = max(1, len(filters))）；
  (c) 模块级全局池（跨编排器共享，测试隔离差，否）。
  证据（同口径重测，形态 A 96 请求）：风暴期线程峰值 **100 → 41**
  （断言预算 = 基线 1 + 并发 24 + 池 6 + 客户端池 8 + 余量 4 = 42，通过）；
  P99 506ms（预算 +200ms 内）；96/96 逐跳兜底、慢调用完成 24 次与
  llm_generate 归因 24 条一致、应答字节与 degraded 语义逐字节不变
  （822 既有用例零回归 + tests/test_executor_lifecycle.py 6 例新增）。
  生产形态（形态 B，编排器预算 2.5s，客户端 0.9s 内归一）：峰值 42、
  P99 2511ms（≤ 预算 +200ms）、96/96 兜底、慢调用完成 71 次与归因
  calls=71/tokens=1917 精确一致。已知行为变化（设计内）：共享池排队
  使过载下部分请求的丢弃归因落在排队超预算的更早跳（实测分布
  coreference/cache_lookup/risk_review/llm_generate）——应答字节与
  兜底语义不变，归因仍逐字节可查。代码落点：pipeline.py
  ``PipelineOrchestrator.__init__``（共享池构造，thread_name_prefix=
  "weiliao-pipeline"）与 ``_run_chain``（去每请求建池/关停）。
  **排队预算效应（审查 G4 补丁，负载行为补充记录）**：共享池下
  ``future.result(timeout=remaining)`` 自提交时刻计预算——并发超过
  池大小（6）时，排队等待本身消耗该跳剩余预算：自身全快的请求可能
  因排在他人慢跳之后而超时降级（旧每请求执行器形态下该请求可成功）。
  这是有界池「线程不积累 ↔ 过载下部分请求提前降级」的固有权衡，
  worker 占用时长 ≤ 单跳执行时长（窗口有界），降级语义与应答字节
  口径不变（同一模板兜底路径）。NFR10 验收口径（线程不积累、P99
  不超预算）即建立在此权衡上；混合负载（快慢请求并存）下「快请求
  被排队拖降级」的买家可见差异未单独压测，记录为残余风险（见
  Auto Run Result）。
- **D2 取消机制**——**结论：不做（候选结论经压测证据维持），记录理由。**
  选项：(a) cooperative cancel（过滤器 Protocol 增 cancel 契约，全链
  6 过滤器改造）；(b) ``future.cancel()``（仅未开始任务有效，运行中
  线程无效——Python 无法强杀线程）；(c) 不做。
  证据：D1 落地后线程已天然有界复用（峰值 41 ≤ 42，排空后回落至
  18 = 基线 + 池 6 + 客户端池 8 + 主/协调线程）；丢弃跳的副作用面已
  由 D4 归因覆盖（口径与已知边界见 D4——「编排器先丢但客户端预算内
  成功」窗口的 token 由成功口径/成本护栏捕获，不进丢弃桶）；实测中
  被丢弃的 llm 跳在客户端 0.9s 预算内自行归一返回，编排器池 worker
  占用时长有界（≤ 单跳预算），取消仅能提前释放 worker，不改变任何
  买家可见口径。收益（提前释放 worker）与 blast radius（过滤器契约
  变更）不匹配。再触发条件：出现「必须立即中止丢弃跳副作用」的合规/
  成本硬需求（如被丢弃调用产生买家可见副作用、丢弃调用成本失控且
  上游支持中止 API，或要求丢弃桶覆盖「编排器先丢」窗口的记账口径），
  或过滤器契约下次演进时一并重估。
- **D3 信封 frozen**——**结论：做（frozen=True），已落地。**
  选项：(a) frozen（构造期禁止原地写）；(b) 不冻结 + 文档纪律约定。
  证据（实测重构面）：全仓原地写点 grep 实证仅 4 处 src——
  pipeline.py 编排器 trace_id 权威回填 1 处 + adapters/http.py
  turn/trace_id 入口适配 3 处（spec Code Map 预估与实际一致）；测试
  侧同口径写点仅 2 处（tests/test_session.py、
  tests/test_handoff_response_path_closure.py，均同语义改 model_copy）；
  另有 1 处既有断言锁定「编排器原地回填调用方信封」
  （tests/test_tracing.py::test_missing_trace_id_backfilled_and_returned
  的信封一致性行）——按 D3 所有权裁断改写：调用方信封保持 None
  （frozen），响应 trace_id 即权威生成值，该用例验收意图（响应携带 +
  全链同一 trace，逐字节断言于 result/fake_langfuse）全部保持。
  重构总面 = 4 写点 model_copy 化 + 2 测试行同语义改写 + 1 断言所有权
  口径改写，822 既有用例零语义改动
  （全链过滤器本就 model_copy 整信封替换，无过滤器原地变更）。
  收益：「超时丢弃线程原地变更信封污染编排器引用」从 code review
  纪律变为 pydantic frozen 的构造期 loud failure（新过滤器误用在
  开发期爆开，绝不带病进生产）。代码落点：models/envelope.py
  ``Envelope.model_config = ConfigDict(frozen=True)``；写点改造
  pipeline.py ``run()``、adapters/http.py ``post_message`` 3 处；
  防回潮回归 tests/test_envelope.py::
  test_frozen_rejects_in_place_mutation。
- **D4 丢弃调用 token 归因**——**结论：做（调用完成点记账 + trace
  事件），已落地。** 选项：(a) 调用完成点记账（客户端 done-callback）；
  (b) 编排器侧预估记账（丢弃瞬间估算，无真实 usage，口径虚构，否）；
  (c) 不做 + 理由。
  证据：完成点是唯一「stub 与真实客户端同口径」的归因位置——usage
  由 ``_to_result`` 统一回传，成功/被丢弃两形态在后台完成点无差别；
  压测三方一致实测（形态 A：慢调用完成 24 == 归因 calls 24 ==
  tokens 648/27；形态 B：71 == 71 == 1917/27；后台失败调用无 token
  产出不计入，单测覆盖）；记账/事件全部 fail-safe（suppress 全包），
  应答路径零触碰、与成功口径（ModelRoutingMetricsCollector.record_call
  路由层记账）互斥不重叠（同一次调用绝不重复计入两口径）。代码落点：
  llm/client.py ``generate`` 超时放弃路径注册
  ``_discarded_completion_callback``（trace_id 可选透传）；
  observability/metrics.py ``DiscardedGenerationMetricsCollector`` +
  模块级单例 + ``emit_discarded_generation_metrics_log``（已接入
  组合根定时日志循环，INFO 级无 alert——纯浪费是成本信号非故障
  信号）；observability/tracing.py
  ``record_discarded_generation``（DEFAULT 级 trace 事件）；
  llm/subgraph.py generate 节点透传 trace_id。
  **已知边界（审查 G5/E1 补丁显式记录）**：丢弃桶口径 = 「客户端硬
  预算放弃（FutureTimeoutError 分支）且后台成功完成」。「编排器逐跳
  先丢弃、但调用在客户端预算内成功完成」的窗口（可达条件：上游跳
  积压使 llm 跳剩余预算 < 调用延迟 < 客户端 0.9s 预算——生产默认链
  中需上游跳消耗 >1.6s）不进丢弃桶：其 token 由成功口径照常捕获
  （成本护栏 WEILIAO_LLM_ROUTING_BUDGET_TOKENS 照常累计），买家
  应答不受影响。窗口覆盖需 hop-discard 信令（编排器→过滤器→客户端
  的取消式契约），即 D2 已拒的 cooperative-cancel 范畴——再触发
  条件沿用 D2 记录。

## Verification

**Commands:**
- `uv run python scripts/loadtest_timeout_storm.py` -- expected: 打印线程数/P99/丢弃计数，断言通过退出码 0
- `uv run pytest` -- expected: 全绿、既有用例零回归
- `uv run ruff check src tests scripts` -- expected: All checks passed（含两个压测脚本；审查 B5 补丁将 scripts 纳入 lint 面）
- `uv run mypy src` -- expected: no issues found（审查 B5 补丁：全 src 面，非仅两个文件）

**Manual checks (if no CLI):**
- 决策记录四项齐备且引用压测实测数字；deferred-work 五组条目状态核对

## Auto Run Result

Status: done

**实现摘要：** 超时风暴压测设施（`scripts/loadtest_timeout_storm.py`，进程内
stub 慢调用驱动）落地并实测：每请求新建执行器形态风暴期线程峰值 100（基线 1）
坐实 1.2 挂账，D1 共享池落地后峰值回落至 41（预算 42 内）、P99 506ms（预算
+200ms 内）、应答字节与 degraded 语义逐字节不变。D2 取消机制决策「不做」
（理由与再触发条件见 Design Notes）。D3 信封 frozen 落地（4 写点 + 2 测试行
改 model_copy，822 既有用例零语义改动）。D4 丢弃调用 token 归因落地
（`DiscardedGenerationMetricsCollector` + `record_discarded_generation` 事件
+ 定时结构化日志），压测三方一致实测（完成数 == 归因 calls == tokens/27）。
`WEILIAO_REDIS_SOCKET_TIMEOUT` 组合根 fail-fast 可配置落地（收口 6.1）。
FR-1 全链压测设施 `scripts/loadtest_intent_chain.py` 落地，本环境无凭据按
spec 口径显式 skip（不虚构数据），deferred-work 该条目保持 open 待凭据复跑。

**变更文件：**
- `scripts/loadtest_timeout_storm.py` / `scripts/loadtest_intent_chain.py` — 新建压测设施（审查补丁：CLI 数值校验、常量级联导入、装配前移凭据门、文档修正）
- `src/weiliao_agent/pipeline.py` — D1 编排器级共享执行池
- `src/weiliao_agent/models/envelope.py` — D3 `ConfigDict(frozen=True)`
- `src/weiliao_agent/adapters/http.py` — 3 处信封写点改 model_copy
- `src/weiliao_agent/llm/client.py` / `llm/subgraph.py` — D4 完成点归因回调 + trace_id 透传
- `src/weiliao_agent/observability/metrics.py` / `tracing.py` — 丢弃归因计数器/事件/定时日志（口径 docstring 已按审查修正）
- `src/weiliao_agent/main.py` — Redis socket 超时可配置 + 定时日志接入
- `README.md` / `.env.example` / `docs/runbook.md` — 变量表、样例、runbook 通道表 + 压测设施运维节
- `tests/test_executor_lifecycle.py`（新建）/ `test_env_example.py` / `test_envelope.py` / `test_tracing.py` / `test_session.py` / `test_handoff_response_path_closure.py` — 回归与守护测试
- `_bmad-output/implementation-artifacts/deferred-work.md` — 1.2/4.3/frozen/6.1 四条挂账 closed；FR-1 条目重开（open-2026-11-10）

**审查结论：** 第 1 轮 4 层 27 条发现 — high 0 / medium 4 / low 17 / false 6。
补丁落地 3 个 medium 条目（D4 归因口径修正 + 已知窗口记录；Redis 超时正路径
接线测试；D1 排队预算效应补充记录）+ 10 个 low 条目（脚本文档/校验/常量级联、
线程断言增量口径、emitter 形态测试、组装前移、README 运维节、spec 验证面扩
至 scripts/全 src、FR-1 挂账重开）。reject 3 条（编排器 close() ×2——CPython
池语义下修复不兑现且生产单实例零影响；http 逃逸路径 trace 分裂——bug-only
防御路径、与 1.5 既有挂账同根）。false 6 条（-O assert、spec 生命周期状态、
stub 口径即意图规定、cancel 决策记录即落地形态、Redis settle 路径、D3 断言
显式载断）。deferred 0 条。

** patched counts by verdict：** medium 3、low 10。

**后续审查建议：true** — 未验证风险具名：D4「编排器先丢窗口」与 D1「排队
预算效应」均为文档化裁断、无自动化断言背书；混合负载（快慢请求并存）下快
请求被排队拖至降级的买家可见差异未单独压测（风暴测试均为全慢链）。复审
应核：Design Notes 边界措辞与实现 docstring 是否一致、增量口径线程断言在
其他 runner 下的稳定性。

**执行验证：**
- `uv run pytest` — 842 passed, 2 skipped（基线 822 + 新增 20；2 skip 为
  开发机 .env 保护的设计内跳过），零回归（2026-11-10 补丁后终验）
- `uv run ruff check src tests scripts` — All checks passed（2026-11-10）
- `uv run mypy src` — 48 source files no issues（2026-11-10）
- `uv run python scripts/loadtest_timeout_storm.py --budget 0.5` — 形态 A
  断言通过（峰值 42 ≤ 42、P99 517ms、归因 24/24/648）（2026-11-10 补丁后）
- `uv run python scripts/loadtest_timeout_storm.py` — 形态 B 断言通过
  （峰值 42、P99 2514ms、归因一致）（2026-11-10 补丁后）
- `uv run python scripts/loadtest_intent_chain.py` — 凭据门显式 skip、
  退出码 0（含门前组装执行）（2026-11-10 补丁后）

**残余风险：**
- FR-1 P99 < 50ms 实测门未执行（无 OPENAI_API_KEY，按口径 skip 未虚构）；
  配置凭据后复跑 `scripts/loadtest_intent_chain.py` 即出判定，deferred-work
  条目保持 open。
- D4 已知边界：「编排器先丢、客户端预算内成功」窗口的 token 记成功口径
  而非丢弃桶（成本护栏照常捕获；信令化归因属 D2 已拒范畴，再触发条件见
  Design Notes D2/D4）。
- D1 排队预算效应：并发超池大小时排队消耗逐跳预算，混合负载下部分快请求
  可能提前降级（窗口有界、兜底语义不变；未单独压测）。
- 编排器实例无 close()：CPython 池语义下空闲 worker 滞留至进程退出，生产
  单实例无影响；测试进程内残留为增量口径断言已容纳。
