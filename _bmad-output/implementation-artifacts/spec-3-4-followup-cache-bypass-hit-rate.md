---
title: 'Story 3.4 重复追问禁走缓存与命中率指标'
type: 'feature'
created: '2026-11-06'
status: 'done'
baseline_commit: '4781ebdd496925e0bbccc7abbda3e895b2a5a06d'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Epic 3 验收口径要求「重复追问禁走 L0、命中率 ≥80%」，但仓库现状是：cache_lookup 不看信封的 `is_followup`（追问照样查 L0、命中照样挂卡片，买家会收到复读缓存）；hit/miss trace 事件只是流水，没有任何汇总统计，命中率口径无落点；压测验收没有读数入口。

**Approach:** cache_lookup 在 `is_followup=True` 时整体跳过 L0 查询（bypass：不查、不挂卡片、记 `bypass` trace 事件、不计入命中率分母），非追问维持 3.3 行为并按 hit/miss 计入指标；新建进程内 `CacheMetricsCollector`（线程安全计数器 + snapshot 算命中率），口径：分子 = L0 命中（预留 L1 并入位），分母 = 全部非追问实际查询数；读数入口按决策 A 接线。

**Decisions（2026-11-06 人类决策）:**
- 命中率读数入口选 **A 方案**：`/health` 响应暴露 metrics snapshot + 定时结构化 stderr 日志；压测 `curl /health` 即读。
- 规格体量 3339 tokens、超 1600 建议线，人类选择**保留全量**：禁走与命中率是同一验收闭环（禁走把追问剔出分母，拆分后命中率口径无法自验）。

## Boundaries & Constraints

**Always:**
- 禁走判定只信信封 `is_followup` 字段（intent_router 上游已按 per-intent ≥2 写好）；cache_lookup 不自行做追问判定、不读 L1 快照或 Redis 计数。
- 命中率口径：分子 = L0 hit（L1 hit 预留并入位）；分母 = 非追问且 intent 非 None 的实际 L0 查询数（hit + miss，Redis 故障归并 miss 同 3.3 口径）；被 bypass 的追问完全不计入分子也不计入分母。
- bypass 必须 Langfuse 可观测：`bypass` trace 事件与既有 hit/miss 事件同一 taxonomy，「同一 session 同一意图第 2 次消息 100% 进入 LLM 链路」以 bypass 事件成对可验。
- 过滤器自身不抛、不影响买家应答；metrics 记录 fail-safe（再抛也吞掉，对齐 tracing 口径）。

**Never:**
- 不得为 L1 发明事实卡片读路径（AD-5 严禁 L1 存事实卡片；L1 当前无读消费者，分子贡献为 0 是现状而非缺陷）。
- 文本相似度 < 0.8 的验证依赖 Epic 4 的 LLM 链路，本 Story 不实现相似度比对、不约束 Epic 4 的 LLM 消费方式。
- 不得改动 `is_followup` 的判定语义（归 Story 2.3：per-intent ≥2；UNKNOWN 不参与）。
- 压测本身（造负载、验证 ≥80% 达标）不在本 Story；本 Story 只交付口径、禁走路由与读数入口。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 追问禁走 | `is_followup=True` 信封（无论 L0 是否有匹配卡片） | 不调 L0 `get`；`fact_card=None`；记 `bypass` trace 事件；metrics `bypassed` +1，分母不变 | N/A |
| 非追问命中 | `is_followup=False`、intent 非 None、L0 有卡 | 挂 `fact_card`；L0 客户端记 hit 事件；metrics `hit` +1 | N/A |
| 非追问 miss | 同上，L0 无卡 | `fact_card=None`；记 `miss` 事件；metrics `miss` +1 | Redis 故障经 L0 客户端 fail-safe 归并 miss（3.3 口径） |
| 无 intent | `intent=None`（占位/UNKNOWN 早退路径） | 透传、不查不计（3.3 锁定行为不变） | N/A |
| 未注入 L0 | `l0_client=None` | 逐字节透传（Story 1.2 锁定） | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/filters/cache_lookup.py` -- 禁走与计数落点；3.3 已注入 L0、hit/miss 事件成对；既有测试逐字节锁定透传与 intent=None 行为，不可回归。
- `src/weiliao_agent/observability/tracing.py` -- `record_cache_event`（L417）operation 词表扩展点：新增 `bypass`（DEFAULT 级，仅 corrupted/failure 为 ERROR 的口径不变）。
- `src/weiliao_agent/observability/metrics.py` -- 新建；observability/ 现仅 tracing.py 单文件，metrics 同层新增（横切关注点，与过滤器间「仅经信封传递」约束不冲突——tracing 直调已有先例）。
- `src/weiliao_agent/filters/intent_router.py` -- `is_followup` 唯一写方（`_record_followup`，per-intent ≥2）；本 Story 只读信封字段，零改动。
- `src/weiliao_agent/models/envelope.py` -- `is_followup`（默认 False）与 `fact_card` 字段；只读。
- `src/weiliao_agent/main.py` -- 组合根装配区（L116-123：IntentRouterFilter → CacheLookupFilter 顺序已保证 is_followup 先于 cache_lookup 就位）；health 接线与日志定时器托管在此。
- `src/weiliao_agent/health/` -- Story 1.4 健康检查端点；响应附 metrics snapshot（A 方案变更面）。
- `tests/test_cache_lookup.py` -- 3.3 查找矩阵测试所在；追加禁走场景。
- `tests/conftest.py` -- `_FakeLangfuse` 共享替身先例（3.3 评审补丁提升至此）。
- `README.md` -- 运维文档；补命中率口径与读数方式（对齐 3.2/3.3 补 env 文档的口径）。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/observability/tracing.py` -- `record_cache_event` operation 词表新增 `bypass`（DEFAULT 级，docstring taxonomy 同步）-- 「100% 进入 LLM 链路 Langfuse 可验证」的落点
- [x] `src/weiliao_agent/observability/metrics.py` -- 新建 `CacheMetricsCollector`：`threading.Lock` 保护计数器（`l0_hit` / `l0_miss` / `bypassed` / `l1_hit` 预留位），`record_lookup(hit: bool)` / `record_bypass()`，`snapshot()` 返回分子/分母/`hit_rate`（分母 0 时 None）；模块级单例 + 可注入重置（测试隔离）-- 命中率口径权威落点
- [x] `src/weiliao_agent/filters/cache_lookup.py` -- `is_followup=True` 分支：跳过 L0 `get`、`fact_card` 保持 None、记 `bypass` 事件、`record_bypass()`；非追问分支在 3.3 行为上挂 `record_lookup(hit=卡片是否命中)`；调用点 try 包裹 fail-safe -- 禁走与计数落点
- [x] `src/weiliao_agent/main.py` + `src/weiliao_agent/health/` -- health 响应附 metrics snapshot（A 方案）+ 定时结构化 stderr 日志（间隔经 env `WEILIAO_METRICS_LOG_SECONDS` 缺省 60，非正整数 fail-fast 带变量名）-- 压测可读
- [x] `tests/test_cache_lookup.py` -- 追加禁走场景：追问时 L0 get 零调用（替身断言）、bypass 事件断言、不计入分母断言；非追问 hit/miss 计数断言 -- 禁走行为可验证
- [x] `tests/test_cache_metrics.py` -- 新建：口径全场景（hit_rate 计算、分母 0 返回 None、并发计数、重置隔离）-- 口径可验证
- [x] `README.md` -- 补命中率口径（分子/分母/禁走不计入）与读数方式说明 -- 运维可见

**Acceptance Criteria:**
- Given L0 存在匹配卡片且信封 `is_followup=True`，when cache_lookup 处理，then L0 `get` 零调用、`fact_card=None`、trace 有 `bypass` 事件、metrics `bypassed` +1 且命中率分母不变
- Given 非追问信封（`is_followup=False`、intent 非 None），when L0 命中/未命中，then metrics 相应 `hit`/`miss` +1，`snapshot()` 的 `hit_rate` = hits/(hits+misses)
- Given 非追问查询遇 Redis 故障（L0 客户端 fail-safe 归并 miss），when 处理完成，then 计 `miss` 且过滤器不抛（与 3.3 事件口径一致）
- Given `intent=None` 或未注入 L0，when 处理任意信封，then 行为与 3.3 逐字节一致、不计数（既有测试零回归）
- Given 全部任务完成，when `uv run pytest -q` + `uv run mypy src` + `uv run ruff check src tests`，then 全部通过且既有 236 个测试零回归

## Implementation Notes

（规划期留白；实现期 append-only 记录。）

- 2026-11-06 实现完成：`record_cache_event` docstring 词表加 `bypass`（DEFAULT
  级，代码层 level 逻辑本就仅 corrupted/failure 为 ERROR，零改动）；新增
  `observability/metrics.py`（`CacheMetricsCollector` + 模块级单例 +
  `reset_cache_metrics` 注入重置 + `emit_cache_metrics_log` 结构化 JSON）；
  cache_lookup 追问分支不查不挂卡、记 bypass 事件、fail-safe 计数；
  `/health` 就绪响应经 `health/probe.cache_metrics_payload` 附 snapshot
  （payload 逻辑落 health/，端点仅一行 merge，取数自身 try 兜底）；
  lifespan 托管 `_metrics_log_loop`，`WEILIAO_METRICS_LOG_SECONDS` 缺省 60、
  非正整数 import 期 fail-fast 带变量名。既有断言按 A 方案响应形状收窄
  两处（test_health / test_tracing 的 /health 精确等值改 status 锁定）。
  验证：253 测试全过（236 既有零回归 + 17 新增）、mypy 零错、ruff 零告警。

## Spec Change Log

（评审循环前为空。）

## Review Triage Log

（评审循环前为空。）

评审循环 1（2026-11-06，三层：blind-hunter 10 条 / edge-case-hunter 1 条 / verification-gap 1 条）：

1. blind-01 日志行非纯 JSON — low → patch（已修）。消息带 `cache_metrics ` 前缀，与 README「结构化 JSON 单行」口径不符、测试靠剥前缀解析；改为消息体即纯 JSON，断言同步简化。
2. blind-02 /health 静默吞快照失败 — low → patch（已修）。`except Exception: payload = {}` 无日志，观测功能无声消失无迹可查；except 分支补 warning 级日志（exc_info），响应行为不变。
3. blind-03 多实例计数不全局 — low → patch（已修）。进程内单例是 A 方案固有特性，README 缺限定说明；补「多实例各自独立计数、/health 为被采样实例读数」一句。
4. blind-04 sprint-status 与 spec 状态不一致 — false。工作流进行中的正常状态：3-4 于 step-03 置 in-progress，spec 于 step-04 置 in-review，完成时由 present 步骤同步。非代码缺陷。
5. blind-05 缺 intent_router→cache_lookup 链式测试 — reject。2.3 既有测试守护 `is_followup` 写入（test_intent.py L286-406，L359 明示「若丢失 is_followup 守卫，本条即红」），3.4 单测守护消费端，链式回归不会静默；补链式集成测试超出直接修正。
6. blind-06 + vgap-01 定时日志 lifespan 接线零测试守护 — high → patch（已修）。vgap 预验证证据：删 `create_task(_metrics_log_loop())` 一行 253 测试全绿，stderr 读数入口可静默脱落；按 test_house_sync.py 既有 spy 风格补 lifespan 用例（patch 间隔 0.05s、caplog 断言 JSON 发射、退出后断言任务 cancel），生产代码零改动。
7. blind-07 循环死亡无检测/重启 — false。循环体仅 `asyncio.sleep` + `emit_cache_metrics_log`（后者 suppress 全部异常），现状无任何可抛路径；所述伤害依赖未来改动移除 fail-safe，非本 diff 的坏结果。
8. blind-08 test_tracing 收窄断言无补偿 — false。test_health.py 就绪响应对 `cache_metrics` 结构有更强断言（lookups/hits/hit_rate），载荷形状已被守护；test_tracing 的关注点是 Langfuse 禁用行为，与 metrics 载荷无关。
9. blind-09 「100% 禁走」无可验钩子 — false。AC 的可验口径即 Langfuse 逐 trace 的 `bypass` 事件（成对可查询）；metrics `bypassed` 计数是其进程内镜像，两者都在。
10. blind-10 重启清零无警告 — low → patch（已修）。与 #3 同根（进程内计数窗口 = 进程生命周期）；README 补「重启清零」说明一句。
11. edge-01 超大 WEILIAO_METRICS_LOG_SECONDS 致 sleep 溢出杀任务 — false。已实测：`asyncio.sleep(10**30)` 任务正常 pending、无 OverflowError，fail-fast 上界无实际伤害；循环死亡路径不存在（同 #7）。

## Design Notes

- **分母为何是「实际查询数」而非「全部消息」**：口径原文「基数为全部首次消息，重复追问不计入分母」。intent=None 的消息从未发生查询（3.3 起不记 miss），把无 key 消息计入分母只会稀释命中率且无法归因 intent，故分母 = 非追问实际查询数。这是口径原文与工程可实现之间的最小差异，记录于此备查。
- **L1 分子预留位**：AD-5 严禁 L1 存事实卡片，3.3 亦未交付 L1 读路径，L1 hit 当前恒 0；计数器预留 `l1_hit` 位，未来 L1 承接读路径时并入分子，口径不变。
- **bypass 为何走 `record_cache_event` 词表而非新函数**：事件形状、保留键、fail-safe 口径全部复用，词表加一个 DEFAULT 级 operation 是最小改动（同 3.3 加 `miss` 的模式）。

## Verification

**Commands:**
- `uv run pytest -q` -- expected: 既有 236 测试零回归 + 新增测试全过
- `uv run mypy src` -- expected: 零错误
- `uv run ruff check src tests` -- expected: 零告警
