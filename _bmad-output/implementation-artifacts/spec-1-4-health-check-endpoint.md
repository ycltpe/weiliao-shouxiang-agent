---
title: 'Story 1.4 健康检查端点'
type: 'feature'
created: '2026-11-01'
status: 'done'
route: 'oneshot'
baseline_commit: '0cf812d762b7afc68de245c8e393df304922fec3'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-1-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-weiliao-shouxiang-agent-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 服务无就绪探针：负载均衡与发布流程无法安全摘挂实例（NFR-7），部署后实例是否可接流量无从感知。

**Approach:** 按架构"健康检查端点挂在响应组装过滤器"的约定，在 `health/` 包实现就绪探针（判定口径：编排器过滤器链完整、含 `response_assembler`），经 `create_app` 暴露 `GET /health`：就绪返回 200，未就绪返回 503。就绪判定仅覆盖"管道编排完成"（AC 的 Given 原文），不纳入 Redis 连通性——Redis 故障按 1.3 fail-closed 设计逐消息降级应答，服务仍可受理流量，探针不应因此摘除全部实例。

</frozen-after-approval>

### Review Findings

- [x] [Review][Decision] 就绪探针判据是否收紧 — 已决（2026-11-10）：维持现状（符合 AC 字面口径，Redis 故障不摘除实例的设计不变）[src/weiliao_agent/health/probe.py:38]
- [x] [Review][Defer] 测试直接导入生产组合根模块存在顺序耦合（test_health 收集期 import main 建真实 Redis 客户端；test_session 以 monkeypatch+reload 替换，两文件互不感知，xdist 下可能泄漏） — deferred, pre-existing [tests/test_health.py, tests/test_session.py]

## Implementation Notes

- 就绪判定落点：`health/` 包新增探针函数，只读检查编排器过滤器链含 `response_assembler`（架构约定"挂在响应组装过滤器"的字面实现）。`PipelineOrchestrator` 补只读访问器（如 `filter_names` 属性）供探针消费；不改编排逻辑。
- 端点：`adapters/http.py` 的 `create_app` 注册 `GET /health`，就绪 200（如 `{"status": "ready"}`），未就绪 503（`{"status": "not_ready"}`）；健康检查非买家消息，不带 trace_id/兜底语义，也不经过滤器链。
- `main.py` 只需更新 docstring（"日后挂同一 app" 的表述过时）；装配结构不变。
- 测试：新增 `tests/test_health.py`，用 TestClient 覆盖 就绪 200 / 链不完整 503 两场景；既有测试不应回归。
- 实现决策：`health/probe.py` 的 `is_ready(orchestrator)` 依赖 `PipelineOrchestrator.filter_names` 只读属性（本次新增）；`READY_FILTER_NAME = "response_assembler"` 常量集中判定键。`main.py` 装配结构未动，`/health` 随 `create_app` 自动可用。验证：37 测试全绿（新增 2 例）、`ruff check src tests` 无告警、`mypy src` strict 无错误。
- 评审补丁：`READY_FILTER_NAME` 改为引用 `ResponseAssemblerFilter.name` 类属性（防重命名失同步）；`/health` 捕获探针异常按未就绪收敛 503（防健康端点 500 触发负载均衡误处置）；补探针异常 503 与生产组合根 `main.app` 冒烟两例。最终验证：39 测试全绿、ruff/mypy 干净。

## Review Triage Log

blind-hunter 单层评审 7 项发现，逐条核验记录如下。`route` 取值：patch / defer / reject。

1. `READY_FILTER_NAME` 字面量与过滤器类属性重复，重命名可失同步 — low（确证，两值当前一致但为独立字面量）— patch：改为引用 `ResponseAssemblerFilter.name`，简单纠正。
2. 就绪判定是静态构造期不变量，生产路径永不 503，探针无运行时信号 — false：确证静态，但 AC 的 Given 原文即"服务已启动且管道编排完成"，静态判定是规格本身；运行时自检属改规格口径（human-owned intent）。
3. 探针自身抛错时 `/health` 无异常处理，FastAPI 默认 500 — low（确证，畸形编排器注入可达）— patch：try/except 收敛 503；负载均衡对 500/503 处置不同，修复系简单纠正。
4. 就绪翻转无可观测（无日志/指标）— low — reject：与 Story 1.3 评审 #11 同类先例，日志/追踪挂点属 Story 1.5（Langfuse），超出本 Story 直接修正。
5. 无针对生产组合根 `main.app` 的测试，装配回归不可见 — medium（采信，先例同 Story 1.3 评审 #4）— patch：补 `main.app` 冒烟用例（Redis 客户端惰性连接，导入即装配安全）。
6. sprint-status 显示 in-progress 与 spec 验证声称矛盾 — false：流程产物，spec 先行记录验证结果，finalize 按 sync 规则将看板推进至 review，非代码缺陷。
7. 无 liveness 对位端点，死锁/线程饥饿时探针仍 200 — medium（若发生，但超出本 Story 口径）— defer → deferred-work.md：NFR-7/AC 只规定就绪端点，死锁检测留待部署拓扑决策后按平台惯例补。
