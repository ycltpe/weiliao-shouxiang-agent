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
