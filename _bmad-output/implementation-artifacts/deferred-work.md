# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-project-skeleton-envelope.md`
  summary: 信封 `timestamp` 是否要求带时区（拒绝 naive datetime）未在 Story 1.1 spec 中规定
  evidence: 评审发现 `Envelope` 接受无时区 timestamp（若属实则 medium：跨跳时间比较崩溃/事件乱序）；spec 各层均未涉及时区策略。 settle：由消费 timestamp 的后续 Story spec（如 1.3 会话管理 / 1.5 Langfuse 追踪）明确时区契约后补校验器。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-project-skeleton-envelope.md`
  summary: 信封字符串标识符（session_id/house_id/trace_id）是否要求非空未在 Story 1.1 spec 中规定
  evidence: 评审发现空字符串可通过校验（若属实则 medium：会话/路由查找失效）；spec 未规定非空约束。settle：由后续 Story spec 明确标识符规则后补 `min_length` 校验。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-pipeline-orchestration-error-fallback.md`
  summary: 每请求新建 ThreadPoolExecutor，超时丢弃的执行线程无取消/复用机制，超时风暴下线程可能积累（severity 未核验）
  evidence: 占位过滤器生产环境均为微秒级透传、不可能触发超时，线程积累场景当前不可达；若真实过滤器（Epic 4）落地后为真预计 medium（P99 劣化/资源膨胀）。settle：Epic 4 接入带副作用的真实过滤器后做超时风暴压测，重审执行器生命周期（共享池/cancel/信封所有权）。
  status: closed-2026-11-10，Story 7.2 超时风暴压测实证（24 并发 × 4 请求，scripts/loadtest_timeout_storm.py 形态 A）：每请求新建执行器形态风暴期存活线程峰值 100（基线 1），挂账预估坐实为真。D1 决策落地共享池（pipeline.py 编排器级 ThreadPoolExecutor，大小 max(1, len(filters))，thread_name_prefix="weiliao-pipeline"），重测峰值 41 ≤ 预算 42、P99 506ms ≤ 预算 +200ms、应答字节与 degraded 语义逐字节不变；D2 取消机制决策「不做」（理由见 spec-7.2 Design Notes D2）；回归测试 tests/test_executor_lifecycle.py。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-pipeline-orchestration-error-fallback.md`
  summary: 信封模型未设 frozen，超时丢弃的线程若在原地变更信封可能污染编排器持有的引用
  evidence: Envelope 无 frozen 是 Story 1.1 既有状态（pre-existing），非本 Story 引入；危害需"带副作用过滤器 + 超时丢弃"组合，当前占位过滤器只读或整信封替换（model_copy），不可达。随 Epic 4 真实过滤器落地与上条同批重审。
  status: closed-2026-11-10，Story 7.2 D3 决策落地 frozen：信封原地写仅 4 处（编排器 trace_id 回填 + http 适配器 turn/trace_id 三处），已全部改 model_copy 整信封替换（Envelope.model_config frozen=True）；重构面实测 = 4 写点 + 2 测试行（test_session / test_handoff_response_path_closure 同语义改写），收益 = 未来过滤器原地变更信封从隐患变为开发期 loud failure；防回潮回归 tests/test_envelope.py::test_frozen_rejects_in_place_mutation。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-pipeline-orchestration-error-fallback.md`
  summary: spec 声明的验证命令 `uv run ruff check .` 全仓执行报 302 个存量告警，按字面跑验收会失败
  evidence: 全部位于 vendored `.agents/skills/`、`.claude/skills/` 脚本树与 `docs/`，pre-existing，与本 Story 代码无关（`ruff check src tests` 干净）。settle：统一 ruff 排除配置（extend-exclude）或收敛存量告警。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-session-state-single-writer.md`
  summary: 会话状态并发原子性（turn 唯一/计数精确）仅在全局串行的 fakeredis 下验证，非原子实现（如 Python 侧 read-modify-write）同样能通过现有并发测试
  evidence: verification-gap 层已读 fakeredis 2.40 源码确证其 server 级全局锁使命令完全串行，无法暴露真实 Redis 下的竞态；本 Story 边界明确"不部署/运维 Redis"，无真实 Redis 可用。settle：部署形态落地后补真实 Redis 集成测试（容器化 redis + 并发压测断言 turn 唯一）；当前依赖结构性单脚本（register_script 单 roundtrip）保证。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-4-health-check-endpoint.md`
  summary: `/health` 仅覆盖就绪（管道编排完成），无存活（liveness）对位端点；进程死锁/线程饥饿（如管道 ThreadPoolExecutor 积累）时探针仍返回 200
  evidence: NFR-7/Story 1.4 AC 只规定就绪端点，静态链判定是规格本身；死锁检测超出"管道编排完成"口径。settle：部署拓扑（K8s vs PaaS）决策后，按平台惯例补 liveness 探针与 hung 检测。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-5-langfuse-tracing-scaffold.md`
  summary: 买家对话文本（原文/下发文本）明文发往 Langfuse，无脱敏、masking 或自托管（LANGFUSE_HOST）的数据边界决策
  evidence: 架构「可观测」约定本身规定三文本字段入 Langfuse（ARCHITECTURE-SPINE 可观测行），Story 1.5 仅按架构落地，非本 Story 引入。settle：需人类就数据合规口径定夺——部署决策时选择自托管/云区，或在 observability 层加 masking 钩子（SDK 5 支持 MaskOtelSpansFunction）。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-5-langfuse-tracing-scaffold.md`
  summary: trace_id 生成双格式并存：编排器/信封链路用 `uuid4().hex`（32-hex，Langfuse 可检索），HTTP 兜底 `_fallback_result` 用 `str(uuid.uuid4())`（带连字符，Langfuse 不可检索）
  evidence: `_fallback_result` 为 Story 1.2 预存代码，spec 明确要求 http.py 零改动、兜底行为与 1.4 逐字节一致；本 Story 未引入该分歧。settle：统一兜底口径时（如 Story 2.x 接入端点改造）一并定夺兜底 trace_id 是否也要 Langfuse 可检索。

- source_spec: `_bmad-output/implementation-artifacts/spec-2-3-followup-query-detection.md`
  summary: 追问计数/hard trigger 的 Langfuse 事件（intent.followup / intent.followup_hard_trigger）形状仅经 set_langfuse_client 替身接缝验证，真实 Langfuse SDK 契约（create_event 参数、事件可检索性）无任何常规验证
  evidence: 仓内既定模式（Story 1.5/2.2 起所有 tracing 事件均走同一替身接缝，tracing.py:54 载明）；AC「Langfuse 可检索」实际依赖部署后真实 SDK 行为。settle：随部署/可观测 deferred 工作（如 Langfuse 自托管/masking 决策）一并补真实集成检验。

- source_spec: `_bmad-output/implementation-artifacts/spec-2-3-followup-query-detection.md`
  summary: record_followup 的 50 线程并发计数精确性仅在 fakeredis（server 级全局串行锁）下验证，真实 Redis 下 Lua 原子性无集成测试
  evidence: 与 Story 1.3 已 deferred 条目同根因（fakeredis 使命令完全串行，非原子实现同样可通过）；当前依赖结构性单脚本（register_script 单 roundtrip）保证。settle：同 1.3 条目——部署形态落地后补容器化真实 Redis 并发集成测试。

## Deferred from: code review of spec-2-3-followup-query-detection (2026-11-03)

- `record_followup` 并发计数精确性与「恰一条消息得 count==3」属性仅在 fakeredis（server 级全局串行锁）下验证，真实 Redis 原子性与并发穿越唯一性无集成测试。与 Story 1.3/首次评审 deferred 条目同根因同 settle 路径：部署形态落地后补容器化真实 Redis 并发集成测试。

## Deferred from: code review of spec-3-2-kafka-house-event-sync-consumer (2026-11-05)

- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-kafka-house-event-sync-consumer.md`
  summary: docker-compose redpanda/redpanda-init 服务无任何自动化校验（仅 spec Verification 手工步骤），compose 配置回归无法被测试发现
  evidence: 本 story 新增的 dev broker 只靠 spec 手工检查守护；仓库无 CI，pytest/mypy/ruff 均不触碰 compose 文件，redpanda-init 命令或 healthcheck 的笔误可全绿入库。settle：属流程决策——引入 compose 解析校验（如 CI 里 docker compose config -q 或 pytest + pyyaml 断言 topic 创建服务），需先定 CI 形态。

## Deferred from: code review of spec-4-1-template-fallback-response-library (2026-11-06)

- source_spec: `_bmad-output/implementation-artifacts/spec-4-1-template-fallback-response-library.md`
  summary: PHOTO_REQUEST 的图片/相册素材分支已提供同步接口，但当前生产调用点均不传素材，图片素材来源尚未接入
  evidence: Story 4.1 冻结块明确不实现图片素材查询；接口为后续响应组装/素材服务接入预留。settle：在实现图片素材查询的后续 Story 接入真实调用方，并验证有图、有相册、无图三条生产路径。

## Deferred from: code review of spec-4-3-fact-card-grounding-controlled-generation (2026-11-06)

- source_spec: `_bmad-output/implementation-artifacts/spec-4-3-fact-card-grounding-controlled-generation.md`
  summary: 编排器逐跳超时丢弃的执行线程无取消机制，llm_generate 慢调用被丢弃后 OpenAI 请求仍在后台跑完，产生纯浪费的 token 花费（无任何归因/记账）
  evidence: 1.2 deferred 条目已记「超时丢弃的执行线程无取消/复用机制……Epic 4 接入带副作用的真实过滤器后做超时风暴压测，重审执行器生命周期」；本 Story 影子接入真实生成过滤器后该场景首次可达。settle：与该挂账项同批——超时风暴压测时重审执行器生命周期（共享池/cancel/草稿丢弃通知），并评估是否需要为被丢弃的生成调用记 token 归因。
  status: closed-2026-11-10，Story 7.2 D4 决策落地「记账」：调用完成点归因（llm/client.py 超时放弃路径注册 done-callback，后台跑完且成功回传 usage 才计入，与成功口径互斥不重叠）——observability.metrics.DiscardedGenerationMetricsCollector（模块级单例 + 定时 discarded_generation_metrics 结构化日志）+ tracing.record_discarded_generation trace 事件（DEFAULT 级，fail-safe）；压测三方一致实测（96 请求风暴：慢调用完成 == 归因 calls，tokens == calls × 27）；回归 tests/test_executor_lifecycle.py。
- source_spec: `_bmad-output/implementation-artifacts/spec-4-3-fact-card-grounding-controlled-generation.md`
  summary: grounding 违禁清单的裸「最」「第一」子串匹配会误伤良性表述（如「最近」「最后」「最好」），可能丢弃合规草稿
  evidence: 该清单沿用 Story AC 既定违禁词与 4.1 `_FORBIDDEN_MATERIAL_FRAGMENTS`，fail-closed 方向安全（影子模式下代价仅为 token 浪费）；改用分词/豁免表等更精确匹配属业务违禁词口径定夺。settle：随 4.4 的审核通过率与 grounding_failed 监控数据观察误报率后定，若误报高再细化匹配语义。
- source_spec: `/Users/yechen/PycharmProjects/test/_bmad-output/implementation-artifacts/spec-4-6-model-routing-cost-guardrail.md`
  summary: `.env.example` 把新变量以空值交付，整体拷贝为 .env 即启动失败。
  evidence: 4.5 的 WEILIAO_LLM_BREAKER_* 变量同样留空（.env.example:27/33），空白交付是仓库既有惯例，非本 Story 引入；如要修应连 4.5 一起统一处理（blank=用缺省值或示例填具体值）。
- source_spec: `/Users/yechen/PycharmProjects/test/_bmad-output/implementation-artifacts/spec-4-6-model-routing-cost-guardrail.md`
  summary: 新 env 变量、/health 的 model_routing 快照与 ERROR 级 model_routing_alert 告警无 runbook/README 文档，运维只能读源码。
  evidence: 4.4/4.5 新增 env 与告警同样只写 .env.example（README 无任何 BREAKER 条目），文档缺口是逐 Story 累积的既有惯例，宜单独立项统一补运维文档。

## Deferred from: code review of spec-5-1-hard-trigger-handoff-judgment (2026-11-09)

- source_spec: `_bmad-output/implementation-artifacts/spec-5-1-hard-trigger-handoff-judgment.md`
  summary: handoff 状态经 mark_handoff 落盘 Redis 后无任何读取方——转人工后同 session 的后续消息仍重建信封走完整 AI 管道，"转人工后 Agent 响应路径关闭、避免人机混答"未在 5.1  enforced。
  evidence: 5.1 冻结块 Never 明确 Kafka 指令与买家安抚归 5.2/5.3，会话响应路径关闭（原子终态）属 Story 5.3 范畴；评审轮 1 triage #9 裁决 medium、route defer。settle：Story 5.3 实现原子终态时，在入口/编排器消费 handoff_status（权威值在 Redis，经 SessionManager 读取）短路后续消息，并补"转人工后零 AI 回复"的端到端测试。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-2-kafka-handoff-unidirectional-command.md`
  summary: 同 session 连续命中硬触发会产生多条转交指令，无每会话一次节流
  evidence: 5.2 盲审#4；转人工后 session 响应路径关闭属 Story 5.3 原子终态范畴（5.1 评审#9 已 defer 同根问题），5.3 收口后自然消除；当前每命中即触发符合 Epic「命中任一即立即触发」口径。

## Deferred from: code review of spec-5-4-ai-identity-disclosure-label (2026-11-09)

- source_spec: `_bmad-output/implementation-artifacts/spec-5-4-ai-identity-disclosure-label.md`
  summary: 无有效 trace_id 的 fail-closed 出口（请求校验失败等）永远记不出 ai.disclosure 事件——`_fallback_result` 回填的 `str(uuid.uuid4())` 是 36 位带横线串，过不了 1.5 起既有 `_recordable` 的 32-hex 校验，事件静默跳过。
  evidence: 5.4 盲审#3 + 边界#6 + 验证缺口层 other finding 同根（评审轮 1 triage 裁决 medium、route defer）。买家可见携标不受影响（兜底文案自带前缀）；AC1 只要求正常链留痕。settle：需人拍板——要么修订 I/O 矩阵 fail-closed 行的事件口径，要么改出口 trace_id 政策（如响应 trace_id 改用 `uuid.uuid4().hex`，会动 1.x 起的响应契约，须人类显式批准）。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-4-ai-identity-disclosure-label.md`
  summary: 空串/仅空白草稿未按「无草稿」处理——空串草稿会产出只有前缀没有正文的买家应答，前导空白+前缀的草稿会双标。
  evidence: 5.4 边界#1/#2 同根（评审轮 1 triage 裁决 medium、route defer）。两种行为与 5.4 改动前的内联组装逐字节一致（旧代码同样只判 `is not None`），非本 Story 引入；且任何「清洗草稿」的修法都会触碰冻结约束「过审草稿除加前缀外不得再被改写」。settle：单独立项，连同 risk_review 对空草稿的拦截口径一起拍板。

## Deferred from: code review of spec-6-1-coreference-filter-interface-pipeline-integration (2026-11-10)

- source_spec: `_bmad-output/implementation-artifacts/spec-6-1-coreference-filter-interface-pipeline-integration.md`
  summary: 会话状态写路径（含新增 write_coref 与既有 write_intent/record_transcript）全部为同步 Redis SET 且默认 socket 超时，Redis 慢/网络抖动时会拖住热路径过滤器，fail-safe 只在异常发生后生效。
  evidence: 6.1 盲审#4；write_intent 自 2.x 起同模式，非本 Story 引入。settle：单独立项评估会话写超时预算（如 WEILIAO_REDIS_SOCKET_TIMEOUT 组合根 fail-fast 配置），属 Epic 7 可运维性范畴。
  status: closed-2026-11-10，Story 7.2 按 settle 路径落地：WEILIAO_REDIS_SOCKET_TIMEOUT 组合根 fail-fast 配置（main.py 缺省 0.5s 现状值；非数值/非正/非有限 nan/inf/超上界 1.0s 拒绝启动且错误消息带变量名；上界 1.0s 远小于 2.5s 内部预算），合法值同时生效 redis 客户端 socket_timeout / socket_connect_timeout 两个 timeout 位；README 变量表 + .env.example + 守护测试（非法取值矩阵 6 值参数化）同步。
- source_spec: `_bmad-output/implementation-artifacts/spec-6-1-coreference-filter-interface-pipeline-integration.md`
  summary: coreference 过滤器自身延迟 P99 < 10ms 预算无任何自动化验证路径（仓库无压测设施，唯一延迟断言是符号非负检查）。
  evidence: 6.1 验证缺口层 filed disposition defer。P99 是压测属性，端到端延迟验收（含消解口径的 FR-1 P99 < 50ms）归 Story 6.3/FR-1 验收范围。
  status: closed-2026-11-10，重路由至 Story 7.2 压测设施，见 spec-6-3 deferred 条目（FR-1 含消解端到端 P99 < 50ms 一并实测）。
- source_spec: `_bmad-output/implementation-artifacts/spec-6-1-coreference-filter-interface-pipeline-integration.md`
  summary: sprint-status.yaml 头部注释的 `# last_updated: 2026-11-02` 与正文长期不一致（本 Story 更新正文时头部继续滞留旧值）。
  evidence: 6.1 盲审#12；头部注释在本 Story 之前即已陈旧，非本 Story 引入。settle：顺手修或纳入 Epic 7 运维文档治理。

## Deferred from: spec-6-2-house-coreference-normalization-default-impl (2026-11-10)

- source_spec: `_bmad-output/implementation-artifacts/spec-6-2-house-coreference-normalization-default-impl.md`
  summary: 人工标注消解语料未就绪，FR-11 ≥95% 房源指代消解准确率的真实验收门 deferred（spec 明令不虚构人工标注语料冒充验收门）。
  evidence: 本 Story 交付 `tests/eval_coref_resolver.py` 评测 harness（内置合成种子集冒烟 + 指代类占比 ≥20% 完整性校验），CSV 语料模式与 ≥95% 验收门已就位。settle：语料与 FR-1 测试集同期建设后，以标注 CSV（列 `text,has_house_coref`，可选 `expected_resolved`）复跑 `uv run python tests/eval_coref_resolver.py eval.csv` 执行真实验收门。
- source_spec: `_bmad-output/implementation-artifacts/spec-6-2-house-coreference-normalization-default-impl.md`
  summary: resolver 延迟 P99 < 10ms 预算无自动化验证路径（仓库无压测设施，6.1#15 同根同裁决）。
  evidence: 6.2 盲审#4，此前仅录于 spec-6-2 frontmatter deferred 列表，未落本台账——补录并关闭。resolver 为单趟正则扫描、微秒级，性能风险可忽略；分类器 45ms 硬超时守卫 + `latency_ms` 埋点已就位。
  status: closed-2026-11-10，重路由至 Story 7.2 压测设施，见 spec-6-3 deferred 条目（FR-1 含消解端到端 P99 < 50ms 一并实测）。

## Deferred from: spec-6-3-downstream-consumption-followup-retrofit (2026-11-10)

- source_spec: `_bmad-output/implementation-artifacts/spec-6-3-downstream-consumption-followup-retrofit.md`
  summary: FR-1「含消解端到端意图分类 P99 < 50ms」实测 deferred——仓库无压测设施，无法产出真实 P99 测量，不虚构数据冒充验收门（关闭 6.1#15 / 6.2 盲#4 同根挂账，重路由至 Story 7.2 压测设施）。
  evidence: 6.1 过滤器自身 P99 < 10ms、6.2 resolver 微秒级单趟规则扫描均已 defer 同根条目；分类器 45ms 硬超时守卫 + `latency_ms` 埋点已就位，唯一延迟断言为符号非负检查。settle：Story 7.2 压测故事落地后，以含 coreference → intent_router 全链的压测执行 FR-1 P99 < 50ms 实测验收。
  status: open-2026-11-10——审查 IA(a) 补丁重开：Story 7.2 压测设施已落地（scripts/loadtest_intent_chain.py：真实 RuleBasedCorefResolver + llm_zero_shot 全链，凭据门内测真实延迟、无 OPENAI_API_KEY 时显式 skip 退出码 0 不虚构数据），但 P99 < 50ms 实测门本环境未执行（无凭据）——配置 OPENAI_API_KEY 后复跑该脚本取得判定即关闭。
## Deferred from: code review of epic-1 (2026-11-10)

- source_spec: `_bmad-output/implementation-artifacts/spec-1-5-langfuse-tracing-scaffold.md`
  summary: 买家原文明文作 span input/metadata 发往 Langfuse，无脱敏/PII 数据边界决策，仅 defer 给部署时人类定夺——届时加 masking 是 breaking change 而非增强。
  evidence: 评审发现 `trace_message` 三文本字段（含买家原文）直接作根 span input/metadata；公开 API 形状 `trace_message(message=...)` 已在脚手架期写死。
- source_spec: `_bmad-output/implementation-artifacts/spec-1-4-health-check-endpoint.md`
  summary: 测试直接导入生产组合根模块存在顺序耦合——test_health 收集期 import main 即建真实 Redis 客户端（依赖惰性连接侥幸不炸），test_session 以 monkeypatch+reload 替换同一全局模块，两文件互不感知，xdist 并发下可能泄漏真实客户端。
  evidence: 评审发现 tests/test_health.py `from weiliao_agent.main import app`；tests/test_session.py `importlib.reload(main_module)`；deferred-work 亦确认 fakeredis 全局锁使并发测试无法在真实 Redis 下复现。

## Deferred from: code review of spec-2-2 (2026-11-10)

- source_spec: `_bmad-output/implementation-artifacts/spec-2-2-intent-classifier-default-impl.md`
  summary: 失败路径观测仅存在于 Langfuse——未配置 Langfuse 的部署中，分类全损事故（如 45ms 硬超时确定性触发）表现为零日志、零指标、零告警。
  evidence: Epic 2 评审 blind-hunter M2：tracing.record_classification_failure 等全部入口无 client 即 no-op，函数体内无 logger 调用；2.1 评审 defer 的「分类失败路径静默」未真正关闭，兑现物本身在无 client 时为 no-op。代码库 main/pipeline/http 等已用 stdlib logging，tracing 失败路径无一处 logger。
  status: open-2026-11-10——settle 路径：随可观测部署工作（1.5 deferred 同路径）统一决定失败事件是否加 logger 兜底；与 spec-2-2 Review Findings 的 45ms 预算决策联动（若选择默认降级，分类失败日志成为必选项）。

## Deferred from: code review of spec-2-2 (2026-11-10) — 追加

- source_spec: `_bmad-output/implementation-artifacts/spec-2-2-intent-classifier-default-impl.md`
  summary: session_id 无字符集约束，构造值（如 "a:intent:1"）可使会话 hash key 与另一会话的 per-turn 意图 string key 逐字节碰撞，触发 Redis WRONGTYPE 致两会话互相 fail-closed 降级。
  evidence: 评审发现 Envelope.session_id 仅 min_length=1；KEY_PREFIX 拼接 `weiliao:session:{sid}` 与 `weiliao:session:{sid}:intent:{turn}` 共享命名空间。
  status: open-2026-11-10——裁决：接受风险（构造输入才可达、后果为降级非越权）；settle：部署形态落地、确认上游 SpringBoot session_id 字符集后复审 charset 校验（选项 B）。

## Deferred from: code review of epic-3 (2026-11-10)

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-fact-card-schema-l0-cache.md`
  summary: 缓存击穿：热 key 过期瞬间并发 miss 无 single-flight/按 key 互斥，LLM 调用量随并发度线性放大。
  evidence: 评审发现 `src/weiliao_agent/filters/cache_lookup.py:61-78` miss 路径无任何请求合并或互斥。
  status: open-2026-11-10——settle：Epic 4 LLM 链路接线后随压测评估放大系数，若冲击 NFR-1 预算再引入请求合并。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-fact-card-schema-l0-cache.md`
  summary: 缓存雪崩：批量写入的 key 共享同一过期时刻，无 TTL jitter，与击穿叠加形成过期时刻 LLM 请求洪峰。
  evidence: 评审发现 `src/weiliao_agent/cache/l0_client.py:136-137` 卡片/索引 EXPIRE 固定 ttl_seconds，无 jitter 守卫。
  status: open-2026-11-10——settle：随压测观察周期性命中率塌缩，必要时对 TTL 加 ±10% jitter（与击穿条目同评估路径）。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-kafka-house-event-sync-consumer.md`
  summary: upsert 写路径最坏 36+ 次串行 Redis 往返，Redis 劣化时吞吐坍缩、分区 lag 堆积，滞后事件按入口时刻被批量误判 stale 转作废。
  evidence: 评审发现 `src/weiliao_agent/workers/house_sync.py:248-287` 每事件 9 intent 串行 Lua roundtrip（socket_timeout 0.5s/次），慢本身会把后续正常事件批量转失效；「超期按失效处理」本身合规 spec-3-2 AC，失效是安全降级非数据丢失。
  status: open-2026-11-10——settle：Epic 4 后压测评估消费者吞吐，必要时引入批量/并发写入或按房源合并 intent 写入。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-4-followup-cache-bypass-hit-rate.md`
  summary: 命中率 ≥80% 无任何可执行验证（压测不在本 Story、仓库无发布门），且 L0 命中挂信封的 fact_card 在 Epic 3 内无下游消费者，「命中=买家秒回事实应答」语义本体无测试锚点。
  evidence: 评审发现 `src/weiliao_agent/filters/cache_lookup.py:65`（挂卡片终点无下游）；spec-3-4「压测本身不在本 Story」。
  status: open-2026-11-10——settle：Epic 4 接线后补组合测试（预置卡片→应答来自卡片→/health hit_rate 读数正确）；≥80% 压测脚本化为可执行发布断言，随既有 fakeredis/压测设施条目一并 settle。

## Deferred from: code review of epic-5 (2026-11-10)

- source_spec: `_bmad-output/implementation-artifacts/spec-5-2-kafka-handoff-unidirectional-command.md`
  summary: Kafka 指令链路全程替身验证，跨进程契约（handoff_id 去重、version=1、key=session_id、at-least-once、P99<5s）无消费侧确认，仓库无真实 broker / testcontainers 测试。
  evidence: 评审发现 `src/weiliao_agent/handoff/command.py` `_KafkaTransport` 全部经 producer_factory 注入替身；spec-5-2 Implementation Notes 自认「真实 broker 行为仅经 transport 替身验证，无自动化 P99 证明」；tests/ 内无 localhost:9092 等真实连接路径。
  status: open-2026-11-10——settle：引入 testcontainers 或工作台侧 schema 契约测试的设施决策，随部署形态落地。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-2-kafka-handoff-unidirectional-command.md`
  summary: 重试耗尽后 DLQ 与 live topic 同集群，Kafka 整体故障时指令彻底丢失且无 DLQ 深度/指令失败计数指标，运维只能靠翻 ERROR 日志发现红线会话从未送达工作台。
  evidence: 评审发现 `src/weiliao_agent/handoff/command.py` `_exhausted` 用同一 transport 写 DLQ；5.2 评审 #17 以「超出直接修正」拒绝加指标。
  status: open-2026-11-10——settle：随可观测/告警设施统一决策（DLQ 深度指标 + 指令失败计数），与 spec-2-2 deferred 的可观测条目同路径。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-1-hard-trigger-handoff-judgment.md`
  summary: `handoff_conditions` 落盘 Redis（_LUA_MARK_HANDOFF HSET + begin_turn/read_snapshot 双解析路径 + SessionSnapshot 字段）后生产零消费方——5.2 载荷实际消费 `envelope.handoff.conditions`，snapshot 字段无人读，孤儿契约成纯负债。
  evidence: 评审发现 `src/weiliao_agent/session/manager.py:135,391-392`；全 src grep 确认无 snapshot.handoff_conditions 读取方；5.1 评审 #5 patch 的理由（「5.2/5.3 将被迫绕过 SessionManager 读裸 Redis」）未兑现。
  status: open-2026-11-10——settle：拍板存废（删除字段 + 双解析路径，或保留至工作台消费方落地）。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-1-hard-trigger-handoff-judgment.md`
  summary: 追问计数键格式变更（`followup:{TAG}` → `followup:{TAG}:{anchor}`，Story 6.3 锚定）无旧键兼容读取；30 分钟 TTL 窗口内滚动升级，升级前计数为 2 的会话从 1 重新累计，followup_threshold=3 硬触发被推迟。
  evidence: 评审发现 `src/weiliao_agent/session/manager.py:272`；tests/test_session.py 全部断言只建新格式键；manager.py「向后兼容」注释与旧键格式实际不符（旧键无 `:none` 后缀）。
  status: open-2026-11-10——settle：部署形态落地时拍板是否加一次性双读（TTL 窗口短，亦可接受瞬时不一致）。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-3-handoff-atomic-terminal-state-buyer-soothing.md`
  summary: `begin_turn` 对 Redis 损坏数值字段无防御：会话 hash 被外部写入损坏值时 Lua tonumber 返回 nil → Python int(None) TypeError，非 RedisError 族穿透 fail-closed 兜底为买家侧 5xx。
  evidence: 评审发现 `src/weiliao_agent/session/manager.py:227-235`（int(cast(...)) 直接解析）；`src/weiliao_agent/adapters/http.py:185-189` 仅收敛 redis.RedisError（刻意惯例：编程错误照常上抛）。
  status: open-2026-11-10——settle：拍板损坏数据语义（loud failure vs 防御归零），随 Redis 数据治理决策；5.3 已对称处理 handoff_status 未知值，数值字段未对称。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-1-hard-trigger-handoff-judgment.md`
  summary: 负面词/媒体硬触发评估基于 `resolved_message` 而非买家原文；Epic 6.2 规则 resolver 改写文本后若意外改变含负面词片段即漏判（5.1 矩阵写的是「message 命中词表」=买家原文语义）。
  evidence: 评审发现 `src/weiliao_agent/filters/intent_router.py:297-299` `_assess_handoff(...)` 两处调用点传 resolved_message。
  status: open-2026-11-10——settle：6.2 验收时明确拍板评估输入口径（原文 vs resolved）；当前 resolver 透传下两者等价，属潜伏契约漂移。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-5-handoff-response-path-closure.md`
  summary: HTTP 主流径每消息双重 handoff 状态读取（5.3 适配器 begin_turn 快照判定后，5.5 编排器闸门对同一请求再实读一次 HGET），99.9% 永不触发的会话多付一次 Redis RTT。
  evidence: 评审发现 `src/weiliao_agent/adapters/http.py:188-208` 与 `src/weiliao_agent/pipeline.py:235-248`；spec-5-5 Residual risks 已自认「每消息一次额外 HGET」。
  status: open-2026-11-10——settle：Story 7.2 压测实测闸门开销后再优化（快照透传或读跳过）。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-1-hard-trigger-handoff-judgment.md`
  summary: `record_followup` trace metadata 键 `per_intent_count` 更名为 `per_tag_anchor_count`（Story 6.3），按旧键名建好的 Langfuse 看板/告警规则升级后静默断流，diff 内无兼容期或迁移注记。
  evidence: 评审发现 `src/weiliao_agent/observability/tracing.py:395` 键名替换、旧键不再写出。
  status: open-2026-11-10——settle：首个看板落地前在 runbook 注记键名口径；项目尚无看板，影响为潜在性。
- source_spec: `_bmad-output/implementation-artifacts/spec-5-4-ai-identity-disclosure-label.md`
  summary: 零宽字符（U+200B）/BOM（U+FEFF）组成的草稿判空漏检：`is_blank_draft` 仅 str.strip()，LLM 产出恰为不可见字符草稿时携标下发无正文消息。
  evidence: 评审发现 `src/weiliao_agent/filters/base.py:12`、`src/weiliao_agent/filters/response_assembler.py:27`；spec frontmatter 已挂账 defer。
  status: open-2026-11-10——settle：随 4.7 deferred 条目一并拍板（strip 字符集扩展）。

## Deferred from: code review of epic-4 (2026-11-11)

- source_spec: `_bmad-output/implementation-artifacts/epic-4-context.md`
  summary: FR5（epics.md:27）「Follow-up Query 的 LLM 响应与首次缓存响应文本相似度 <0.8」零实现且未挂账；Story 4.7 重写 epic-4-context 时该约束被静默删除。追问禁走缓存（3.4）只保证进 LLM 链路，对「不复读缓存内容」无把守。
  evidence: 评审发现 `epics.md:27`（FR5）与 `:311`（story AC）仍有效；`src/` 全仓无相似度校验实现；`epic-4-context.md` 4.1 新增（review-epic4.diff:48）、4.7 删除（diff:9043）。
  status: open-2026-11-11——settle：FR5 相似度校验待评估误判率（阈值 0.8 与中文短文本复读的判定口径），排到后续优化轮拍板实现或走 correct-course 修订 epics.md 废止；不允许再从 context 文档静默移除。
