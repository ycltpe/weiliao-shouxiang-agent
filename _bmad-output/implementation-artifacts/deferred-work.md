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
