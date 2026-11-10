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

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-pipeline-orchestration-error-fallback.md`
  summary: 信封模型未设 frozen，超时丢弃的线程若在原地变更信封可能污染编排器持有的引用
  evidence: Envelope 无 frozen 是 Story 1.1 既有状态（pre-existing），非本 Story 引入；危害需"带副作用过滤器 + 超时丢弃"组合，当前占位过滤器只读或整信封替换（model_copy），不可达。随 Epic 4 真实过滤器落地与上条同批重审。

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
