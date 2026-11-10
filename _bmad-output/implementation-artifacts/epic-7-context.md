# Epic 7 Context: 可运维性与过载韧性

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

本 Epic 把系统推到"可交付运维"状态，分两条线。第一条是运维文档与配置交付：全部环境变量（含断路器系列的 WEILIAO_LLM_BREAKER_*）、/health 端点的 model_routing 快照与 ERROR 级 model_routing_alert 告警，都要有 runbook/README 可查，.env.example 不留空值变量，干净 checkout 拷贝后即能启动，目标是运维排障与交接不依赖读源码。第二条是过载韧性：编排器逐跳超时丢弃的执行线程目前无取消/复用机制（Story 1.2/4.3 挂账同根），在 Epic 4 真实过滤器落地后执行超时风暴压测，重审执行器生命周期并落地决策，使超时风暴下服务不劣化、资源不膨胀，被丢弃的生成调用浪费的 token 有归因。

## Stories

- Story 7.1: 运维文档与配置交付
- Story 7.2: 超时风暴压测与执行器生命周期重审

## Requirements & Constraints

运维文档与配置（NFR9）：

- 每个新增 env 变量在 README/runbook 中有说明、缺省值与调参影响；必须覆盖 Story 4.4/4.5/4.6 引入的 WEILIAO_LLM_BREAKER_* 系列。
- /health 的 model_routing 快照与 ERROR 级 model_routing_alert 告警，触发时 runbook 中有对应处置步骤可查。
- .env.example 不留空值变量（blank 一律用缺省值或具体示例值填充）；干净 checkout 下 cp .env.example .env 后直接启动成功，无空值导致的启动失败。

超时风暴与执行器（NFR10）：

- 压测用可控慢过滤器驱动（llm_generate 以 stub 控制慢速），前提是 Epic 4 真实过滤器已落地。
- 压测验收口径：执行线程不积累，P99 不劣化超预算。
- 重审执行器生命周期的三个决策点：共享池 / 取消机制 / 信封所有权（信封是否 frozen 的评估），需有明确决策记录并落地。
- 被超时丢弃的生成调用：token 记账落地，或记录"不做"的决策理由。
- 完成后 deferred-work 中 1.2/4.3 的执行器相关挂账条目关闭。
- 另有两项挂账随本 Epic 处置：会话写路径的 Redis 同步 SET 超时预算评估（如 WEILIAO_REDIS_SOCKET_TIMEOUT 组合根 fail-fast 配置）；意图分类 FR-1 端到端 P99 < 50ms 的实测验收，待 7.2 压测落地后以含 coreference → intent_router 全链的压测执行。

## Technical Decisions

- 管道-过滤器架构：信封为单一 Pydantic 模型，过滤器之间仅通过信封传递，禁止跨过滤器直接调用；每个过滤器只消费上游信封、产出下游信封。
- 编排器对每跳设置超时，超时即丢弃该 hop 的执行并降级到兜底；当前实现为每请求新建 ThreadPoolExecutor，被丢弃线程无法取消或复用——这正是本 Epic 要重审的对象。
- 信封模型当前未设 frozen：超时丢弃的线程若原地变更信封可能污染编排器持有的引用，是否冻结是信封所有权决策的核心权衡。
- 热路径（意图分类、路由、缓存、风控、响应组装、下发）为手写纯 Python，不依赖 LangGraph Runtime；LLM 子图对外仅暴露同步生成接口。
- 统一错误兜底：任一过滤器异常或超时即切换模板兜底，未审核内容禁止发买家，错误响应携带 trace_id，兜底应答时限受端到端延迟预算（P99 < 3s）约束。
- 健康检查端点 GET /health 挂在响应组装过滤器，就绪返回 200。
- 可观测性：Langfuse trace_id 贯穿全链路，成本追踪记录 token 消耗与模型调用费用；降级事件、告警均记录 trace 并接入监控告警——这些就是 7.1 runbook 需要覆盖的排障对象。

## Cross-Story Dependencies

- Story 7.2 依赖 Epic 4 真实过滤器（带副作用）已落地；压测通过 llm_generate 的 stub 慢速模式驱动。
- Story 7.1 的文档对象（WEILIAO_LLM_BREAKER_*、model_routing 快照、model_routing_alert）由 Story 4.4/4.5/4.6 的断路器与降级能力产生，需在那些 Story 交付后核对变量清单完整性。
- 关闭 deferred-work 中与编排器线程积累、信封 frozen、被丢弃生成调用 token 浪费相关的挂账条目，是 7.2 的验收组成部分。
