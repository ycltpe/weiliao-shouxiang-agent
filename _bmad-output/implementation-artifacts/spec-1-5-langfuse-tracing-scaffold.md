---
title: 'Story 1.5 Langfuse 全链路追踪脚手架'
type: 'feature'
created: '2026-11-01'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '2519ed16ac96dd8fd14d69eff13dabe5d4523aeb'
context:
  - '_bmad-output/implementation-artifacts/epic-1-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-test-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** trace_id 目前要求客户端在 payload 携带（`Envelope.trace_id` 必填），服务自己不生成，也没有 Langfuse 集成——全链路追踪与成本可观测（NFR-6、SM-5/SM-6 验收口径）无从落地。

**Approach:** 在 `observability/` 包实现追踪脚手架：编排器入口统一生成 trace_id 并回填信封，贯穿过滤器链；接入 Langfuse SDK 5.0.4（已锁定于 pyproject），按消息开 trace、按过滤器记 span、降级事件写入 trace；暴露成本追踪封装（token/费用）供 Epic 4 模型调用消费。未配置 Langfuse 时全链路自动 no-op，不影响应答与延迟口径。

## Boundaries & Constraints

**Always:**
- trace_id 契约（2026-11-10 人工重谈修订，取代「不信任客户端 payload 值」原口径）：缺失即由服务生成（编排器入口为单一权威点），客户端携带值透传（含非 32-hex 值——该值仅跳过 Langfuse 追踪，fail-safe）；响应必须携带最终生效的 trace_id。
- 追踪代码 fail-safe：Langfuse SDK 任何异常不得影响买家应答、不得超出管道时间预算；SDK 自身 flush 为后台线程，不得阻塞请求路径。
- 架构可观测约定落入本 Story：每消息记录三文本字段（买家原文/消解后消息/最终下发文本，后两者当前恒等于原文）；降级事件写 trace；成本追踪记 token 与费用（API 级，真实调用属 Epic 4）。

**Never:**
- 不改过滤器编排逻辑、错误兜底语义与会话单写者约定；不动 `/health` 判定口径。
- 不在过滤器线程内做 OTel context 传播（span 在主线程编排器侧记录）。
- 不接入真实模型调用、不部署 Langfuse 服务端；鉴权/限流/Kafka 接入不在范围。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常消息进管道 | 信封校验通过（trace_id 缺失或携带值） | 编排器入口对缺失值生成新 trace_id 回填信封、携带值透传（2026-11-10 人工重谈裁定：透传为正式契约，见 Boundaries）；响应携带之；trace 记买家原文与各过滤器 span | 非 32-hex 携带值仅跳过 Langfuse 追踪（fail-safe） |
| Langfuse 未配置 | 无 `LANGFUSE_PUBLIC_KEY` 等环境变量 | SDK 进入 disabled 态，全部追踪 no-op；应答/降级行为与 Story 1.4 完全一致 | 已验证：SDK 打印一次 warning 后自动禁用 |
| 过滤器异常降级 | 链中某过滤器抛异常 | 应答兜底 + trace_id 不变；trace 记录降级事件与归因过滤器名 | 降级事件记录本身再抛异常则吞掉，绝不二次影响应答 |
| SDK 调用抛错 | Langfuse client 方法抛任意异常 | 追踪跳过该条记录，管道结果不受影响 | observability 层统一 try/except 收敛 |

</frozen-after-approval>

## Open Questions

- OQ-1 信封 trace_id 与 Langfuse trace 的 id 映射 — **已决（2026-11-01 实现时）：A**。信封 trace_id 直接作为 Langfuse trace id（`TraceContext(trace_id=...)`）。SDK 5.0.4 路径已核实稳定，但自定义 id 必须是 32 位小写十六进制（SDK `_is_valid_trace_id` 口径，否则告警并抛 ValueError），故 `new_trace_id()` 生成 `uuid4().hex`。实现附带发现：非 32-hex 的 payload 携带值（如既有测试的 `"trace-001"`）不能作 Langfuse trace id，observability 层对这类 id 统一跳过记录（fail-safe，不告警不抛错）；trace_id 回填语义为「缺失即生成、携带值透传」——后者为兼容「既有 39 测试零回归」AC 与 Boundaries「不信任 payload 值」的调和，SM-5/SM-6 验收以服务端生成 id 为准。
- OQ-2 无效输入（校验失败，不进管道）是否也写 trace — **已决（2026-11-01 实现时）：B**。无效输入路径不写 trace：`adapters/http.py` 零改动，保持 Story 1.4 兜底语义与零依赖延迟；观测靠访问日志。AC 只承诺进入管道的消息。

## Code Map

- `src/weiliao_agent/models/envelope.py` -- `Envelope.trace_id` 现为必填 `Field(min_length=1)`，需放宽为 `str | None = None`（生成责任移交编排器）；docstring 字段口径同步。
- `src/weiliao_agent/pipeline.py` -- `PipelineOrchestrator.run()` 为 trace_id 生成与 span 记录挂点；`_degraded()` 为降级事件挂点；过滤器在 ThreadPoolExecutor 线程执行，span 须在提交 future 的主线程侧记录。
- `src/weiliao_agent/adapters/http.py` -- `_fallback_result(intent, trace_id)` 已接受 `str | None`；无效输入路径视 OQ-2 定；正常响应 `to_dict()` 已含 trace_id，结构不改。
- `src/weiliao_agent/observability/` -- 空包（仅 `__init__.py`），脚手架落点；Langfuse SDK 5.0.4 已锁定并安装。
- `src/weiliao_agent/main.py` -- 组合根；Langfuse 走 SDK 默认环境变量配置（`LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST`），预期无需改动。
- SDK 行为已验证：无 key 时 `get_client()` 告警一次后全方法 no-op；主入口 `start_as_current_observation(...)`、`propagate_attributes(...)`；`types.TraceContext` 承载 trace 级上下文。
- `tests/` -- 39 测试全绿为回归基线；`test_pipeline.py` 降级路径替身风格可参照。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/models/envelope.py` -- `trace_id` 放宽为 `str | None = None` 并更新字段口径 docstring（服务端权威生成） -- 契约变更最小面，其余校验规则不动。注：落地为 `str | None = Field(default=None, min_length=1)`，空串拒绝规则保留（"其余校验规则不动"）。
- [x] `src/weiliao_agent/observability/tracing.py` -- 新建：客户端惰性单例（可注入替身）、`new_trace_id()`、消息级 trace 上下文管理器（trace_id/session_id/买家原文入 trace）、过滤器 span 记录、降级事件、成本追踪封装（供 Epic 4）；公开方法全部 fail-safe -- 脚手架核心。
- [x] `src/weiliao_agent/observability/__init__.py` -- 导出公共 API（trace_id 生成、trace/span、成本封装） -- 对外稳定接口面。
- [x] `src/weiliao_agent/pipeline.py` -- `run()` 入口：信封 trace_id 为空则生成回填；每过滤器执行包 span（主线程侧）；降级路径写降级事件 -- AC 主挂点；不改编排与兜底语义。
- [x] `tests/test_tracing.py` -- 新增：trace_id 生成回填且响应携带；每过滤器 span 记录；降级事件含归因；注入抛错 client 时管道结果不受影响；无 key disabled 冒烟 -- 覆盖 I/O 矩阵四行。

**Acceptance Criteria:**
- Given 校验通过的消息（payload 不带 trace_id），when 经 `POST /v1/messages` 进管道，then 响应 `trace_id` 非空且系服务端生成（与 payload 无关）。
- Given 过滤器链中任一过滤器抛异常，when 管道降级兜底，then 应答 `trace_id` 与正常路径同口径，trace 中可检索到该降级事件及归因过滤器名。
- Given Langfuse 未配置（无环境变量），when 任意消息处理，then 行为与 Story 1.4 逐字节一致（应答、降级、健康检查均不变），仅多一条禁用 warning 日志。
- Given 成本追踪 API 被调用（脚手架级假数据），when 传入 token 用量与费用，then 记录挂到当前消息 trace（验证以替身 client 断言为准，Epic 4 接真实调用）。
- 既有 39 测试零回归；`ruff check src tests` 无告警；`mypy src` strict 无错误。

### Review Findings

- [x] [Review][Decision] trace_id 信任边界矛盾 — 已决（2026-11-10 人工重谈）：维持携带值透传为正式契约；冻结块 Boundaries 与 Matrix 行 1 已同步修订 [src/weiliao_agent/pipeline.py:226]
- [x] [Review][Patch] 兜底路径（校验失败/Redis 故障/编排器逃逸）trace_id 为 36 位带连字符 uuid，与编排器 32-hex 口径分裂，错误应答在 Langfuse 不可检索；且 tests/test_tracing.py:403 断言 `_TRACE_ID_HEX.fullmatch(...) is None` 把缺陷固化为期望行为，修复时需同步改测试 [src/weiliao_agent/adapters/http.py:71, tests/test_tracing.py:403]
- [x] [Review][Patch] 根 span 在降级/异常路径 `__exit__(None, None, None)` 正常退出，过滤器 span 标 ERROR 而根 span 恒绿，Langfuse 按根 span 状态筛失败消息为零 [src/weiliao_agent/observability/tracing.py:177,189]
- [x] [Review][Patch] `record_delivery` 的 `client.update_current_span(metadata={"delivered_text": reply})` 无测试替身实现（`_FakeLangfuse` 只有 start_as_current_observation/create_event），该路径在所有测试中从未成功执行（AttributeError 被 suppress 吞掉），删除该行全套件仍绿 [tests/test_tracing.py:63, src/weiliao_agent/observability/tracing.py:296]
- [x] [Review][Defer] 买家原文明文作 span input/metadata 发往 Langfuse，无脱敏/PII 数据边界决策，仅 defer 给部署——届时加 masking 是 breaking change — deferred, pre-existing [src/weiliao_agent/observability/tracing.py trace_message]

## Implementation Notes

- 2026-11-01 实现期决策（trace_id 回填语义调和）：spec 内部存在张力——Boundaries「不信任客户端 payload 值」/ Matrix 行 1「伪造即重生成」 vs AC「既有 39 测试零回归」（Story 1.2 测试断言 `"trace-001"` 透传）。落地为「缺失即生成、携带值透传」：任何 trace_id（含伪造）在管道与响应中正确流转，仅非 32-hex 的 id 跳过 Langfuse 记录（SDK `_is_valid_trace_id` 口径，fail-safe 不告警不抛错）。所有 AC 逐字满足；Matrix 行 1「伪造」分支按此口径执行。若后续要求严格 always-regenerate，需重谈 AC-5 并改 2 例 1.2 测试。
- 实现发现：SDK 5.0.4 自定义 trace id 仅接受 32 位小写十六进制，否则告警并抛 ValueError——故 `new_trace_id()` 生成 `uuid4().hex`，observability 层预校验跳过非法 id，避免异常驱动控制流。
- 降级/下发记录走 SDK `create_event`（公开 typed API）；`as_type="event"` 观测在 SDK 5.0.4 typed overloads 中不存在。
- 评审补丁（三层评审后）：trace_message/filter_span 分段 enter 补失败清理（防部分 enter 泄漏 span）；`_run_chain` 的 `or` 假防御改 assert；docstring 信任边界表述改为「缺失即生成、携带值透传」；record_delivery 同步 delivered_text 到根 span metadata（修过期 None）；新增 4 测试（非 hex 透传跳过、负预算 slow_filter 归因、真超时 ERROR span、Redis 故障无 trace_id 兜底）；disabled 冒烟补 /health 断言。最终验证：50 测试全绿、ruff/mypy 干净。

## Spec Change Log

## Review Triage Log

blind-hunter / edge-case-hunter / verification-gap 三层评审 20 项发现，逐条核验记录如下。`route` 取值：patch / defer / reject。

1. `trace_message()` 部分 enter 泄漏：`root.__enter__()` 成功而 `propagate_attributes`/`attrs.__enter__()` 抛错时，`except: yield; return` 分支不调用 `root.__exit__`（tracing.py:120-125，E1 同址）— low（确证，读码验证）— patch：enter 各步独立收敛，失败路径补退出已 enter 的根 span。
2. `filter_span()` 同类部分 enter 泄漏：`manager.__enter__()` 抛错时 manager 未 `__exit__`（tracing.py:160-163）— low（确证）— patch：同 1 一并修复（同根因：CM 分段 enter 无失败清理）。
3. 「实现违反权威 id 契约」：Matrix 行 1「伪造即重生成」与实现透传不符（B2，E4 同 claim，E5 为其 docstring 变体）— false：契约的可执行部分是 AC——AC-5「既有 39 测试零回归」+ test_pipeline.py:79/100/124/183 四处断言 `result.trace_id == "trace-001"` 锁定透传语义，always-regenerate 反而违反 AC；所引 Boundary/Matrix 措辞被显式 AC 覆盖。残余危害（伪造 32-hex 注入 Langfuse trace）经评估不成立：trace id 为 128 位随机 hex，猜测他户 trace 不可行，买家侧零影响。
4. envelope/pipeline docstring「不被信任为权威来源」与透传语义不符（E5）— low（确证，开发者阅读误导）— patch：按「缺失即生成、携带值透传」重写 docstring 信任边界表述。与 3 不同根因（文档措辞 vs 行为契约），单独处理。
5. `_run_chain` 防御性 `envelope.trace_id or new_trace_id()` 死分支：若可达则与 `run()` 已开的 trace 分裂成两个 id（B3）— low（确证死代码；`run()` 必先回填）— patch：改为赋值 + `assert`（保留 mypy 收窄，消除假分支）。
6. sprint-status `in-progress` 与 spec `in-review` 不一致（B4）— low（流程产物）— patch：按 finalize 口径推进至 `review`。
7. 缺非 32-hex 透传且跳过记录的锁定测试（B5）— low（verification 缺口，实现注记已声明该口径但无测试钉住）— patch：补测试（payload `"trace-001"` → 管道/响应透传、observability 零调用）。
8. 缺超时/预算耗尽路径的 `slow_filter` 归因测试（B6，V2 同址）— low（verification 缺口；`record_degradation` 仅 `failed_filter` 有断言，`slow_filter` 无任何断言钉住）— patch：负预算确定性触发 `remaining<=0` 分支，断言降级事件带 `slow_filter` 归因。
9. Matrix 行 2 承诺「健康检查均不变」但 disabled 测试只覆盖 `/v1/messages`（B7）— low（AC 子句未验证）— patch：disabled 冒烟补 `GET /health` 断言 200。
10. 「空串仍拒绝」无测试（B8）— false：`test_blank_identifier_is_rejected`（tests/test_envelope.py:88-94，parametrize 含 `trace_id`）已钉住 `min_length=1`。
11. PII/数据驻留：买家消息文本发送至 Langfuse 无脱敏/自托管说明（B9）— low（真实顾虑但非本 Story 引入：架构「可观测」约定本身规定三文本字段入 Langfuse，Story 1.5 只是落地架构）— defer → deferred-work.md：架构级数据边界决策，需人类就脱敏/自托管口径定夺。
12. disabled warning 计数断言脆（B10）— false：fixture 经 `set_langfuse_client(None)` 重置单例，caplog 窗口限定 with 块内，套件无 xdist；干扰路径未证实。
13. 根 span metadata 的 `delivered_text` 恒为 None，三文本字段分裂在 span metadata 与 delivery 事件两处（B11）— low（确证；SM-5/SM-6 消费者须自行合并两处，metadata 含过期 None 易误读）— patch：`record_delivery` 经 `update_current_span(metadata=...)` 同步最终下发文本到根 span。
14. `record_generation` 零时长、无 input/output，docstring 未标注限制（B12）— low — patch：docstring 补局限说明（Epic 4 需 start/end 或 update 语义）。
15. Redis 故障兜底路径携带 `trace_id=None` 无测试：信封放宽后该分支可达，「错误应答携带 trace_id」契约可被静默破坏（V1，verification-gap 预验证，含演示：去掉 `_fallback_result` 的 None 合并全套件仍绿）— medium（bad outcome 真实：Redis 故障期间无 trace_id 的合法 payload 会拿到 null trace_id，恰是兜底路径存在的场景）— patch：补兄弟用例（删 `trace_id` + SessionManager 抛 `RedisError`，断言响应 trace_id 非空且非客户端值）。
16. 追踪开销不计入预算，可能挤出后续跳预算触发「假」slow_filter（E3）— false：deadline 本就是「端到端」口径（pipeline.py docstring），追踪耗时占用后续跳预算是 deadline 按设计工作，非假降级；且 SDK flush 后台化，同步 exporter 不在请求路径。
17. `_fallback_result` 的 uuid 带连字符 vs 编排器 `uuid4().hex` 双格式并存（V-other）— low（真实但 http.py 按 spec 零改动，属预存状态）— defer → deferred-work.md：未来统一兜底 trace_id 生成口径时一并定夺。
18.（B1 已在 1 记录；E1 同 1、E4 同 3、E5 单列 4、V2 同 8。）

分组路由：G{1,2}=patch；3=reject(false)；4=patch；5=patch；6=patch；7=patch；8=patch；9=patch；10=reject(false)；11=defer；12=reject(false)；13=patch；14=patch；15=patch；16=reject(false)；17=defer。无 intent_gap / bad_spec 存活，不触发 loopback。

## Design Notes

trace_id 生成放编排器入口而非 HTTP 适配器：Kafka 接入（Epic 3）与单测直通编排器时同样获得权威 id。span 在主线程记录：过滤器跑在 `ThreadPoolExecutor` 线程，OTel context 跨线程传播成本高；过滤器纯函数式（信封进/信封出），主线程在 `future.result()` 前后已持全部记录所需信息。成本封装只定 API 形态（如 `record_generation(usage, cost)`），内部走 generation 观测，以 SDK 5.0.4 真实签名为准。

## Verification

**Commands:**
- `.venv/bin/python -m pytest tests -q` -- 全绿（39 既有 + 新增用例）
- `.venv/bin/ruff check src tests` -- 无告警
- `.venv/bin/mypy src` -- strict 无错误
