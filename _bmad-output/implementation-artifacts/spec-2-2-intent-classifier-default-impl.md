---
title: 'Story 2.2 意图分类默认实现'
type: 'feature'
created: '2026-11-02'
status: 'done'
baseline_commit: 'c6796554824a6fb7bafcafc0275029668c091223'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-2-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-weiliao-shouxiang-agent-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Story 2.1 已固化 `IntentClassifier` 接口与 9 类标签模型，但无默认实现：组合根装配的是未注入分类器的占位过滤器，所有消息一律 UNKNOWN，意图分类能力（FR-1）实际为零。

**Approach:** 交付一个满足「单一标签 + 置信度」契约的默认分类器实现，经配置在组合根装配切换（路由链路零改动）；分类结果经会话单写者落入 Redis per-turn 键 `weiliao:session:{session_id}:intent:{turn}`（JSON：tag + confidence，TTL 与会话一致）；分类与写入经 Langfuse trace 可验证（含 2.1 评审 defer 的分类失败观测挂点）。

**Decisions（Checkpoint 答复，2026-11-02）：**
- 默认实现选 **LLM zero-shot**（gpt-4o-mini 经 langchain-openai，硬超时守卫 + 失败 fail-closed UNKNOWN）；规则引擎/微调小模型不交付（OQ-1 关闭，选 B）。
- 准确率验收：**≥90%·≥1000 条标注语料的验收门 deferred**——本 Story 交付评测 harness + 每类数十条合成种子用例仅做冒烟，语料就绪后复跑验收（选 B）。

## Boundaries & Constraints

**Always:**
- 分类器只消费消息文本 + 会话上下文（`Mapping`），输出单一 `IntentClassification`；不读信封其他字段、不直接碰 Redis。
- 意图写入只经 `SessionManager.write_intent`（AD-4 单写者）；过滤器注入 writer callable，写失败 fail-safe：应答不降级、信封保留分类结果、trace 记失败事件。
- 分类延迟硬超时守卫：实现内部自带硬超时（默认 800ms，构造参数 > `WEILIAO_INTENT_TIMEOUT_SECONDS` env > 默认；超时 fail-closed UNKNOWN 并记 trace），不依赖编排器 deadline 兜底。原 P99 < 50ms 预算与真实 LLM 网络 RTT 物理不相容，2026-11-10 评审决策记为不可达 NFR 缺口（见 Review Findings 决策项），真实 P99 经 `latency_ms` 埋点 + 压测设施实测。
- 既有 fail-closed 行为零回归：空文本不调分类器、分类异常 UNKNOWN、未注入分类器占位行为逐字节保留（既有 73 例测试锁定）。
- `mypy src` strict 与 `ruff check src tests` 保持干净；切换实现只动配置与组合根（`main.py`），`pipeline.py` 默认链不改。

**Never:**
- 不做指代消解（resolved_message 优先、缺省退化 message 的口径沿用 2.1，切换点不改）。
- 不改信封六字段契约（置信度不进信封）；不动过滤器编排、超时预算、降级兜底语义与 `templates.py` 文案。
- 不引入 LangGraph Runtime 依赖（AD-2）；分类器实现不新增进程内缓存状态（实例无状态，可安全复用 2.1 的线程池执行模型）。
- 不在本 Story 做追问计数/ Follow-up 识别（Story 2.3）与转人工信号（Epic 5）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 合法消息分类 | 注入分类器的过滤器处理非空消息 | 信封 intent = 分类标签；`weiliao:session:{sid}:intent:{turn}` 写入 JSON（tag+confidence），EX=会话 TTL；trace 有分类事件（标签/置信度/来源/延迟） | 无错误 |
| 分类超时（LLM 类实现） | 实现内部调用超过硬超时预算 | fail-closed UNKNOWN，trace 记分类失败事件（reason=timeout） | 实现内守卫，过滤器契约不变 |
| Redis 写入失败 | writer 抛 redis.RedisError | 应答正常不降级；信封保留分类结果；trace 记 intent_write_failed 事件 | writer 调用包 try，fail-safe |
| turn=None / 未注入 writer | 测试替身路径（无 SessionManager） | 跳过 Redis 写入，仅分类 + trace | 无错误 |
| 空文本消息 | message/resolved_message 均为 None 或空串 | 不调分类器，直接 UNKNOWN（2.1 行为逐字节保留） | 既有守卫 |
| 配置切换实现 | `WEILIAO_INTENT_CLASSIFIER` 改值重启 | 组合根装配另一实现，路由链路代码零改动 | 非法配置值 fail-fast 拒绝启动 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/filters/intent_router.py` -- `IntentClassifier` Protocol 与注入构造（2.1 交付）；本 Story 增加可选 `intent_writer` 注入与分类后写回钩子，分类成功/失败处补 trace 挂点。分类与写回逻辑本身不改。
- `src/weiliao_agent/models/intent.py` -- `IntentTag` / `IntentClassification`（置信度 0.0–1.0 校验）；写 Redis 的值序列化口径即 `model_dump_json()`。
- `src/weiliao_agent/session/manager.py` -- `SessionManager` 单写者；模块 docstring 明示 `write_intent` 与 per-turn 键落盘是 Epic 2 预留缺口，本 Story 补齐：独立 key `weiliao:session:{sid}:intent:{turn}`，`SET` + `EX ttl_seconds` 单 roundtrip，无需 Lua。
- `src/weiliao_agent/observability/tracing.py` -- 新增 `record_classification` / `record_classification_failure` 事件封装（fail-safe 口径同既有 `record_degradation`）；`trace_message` 入口的 intent 是 preset 值（分类前），分类结果只能经新事件挂 trace。
- `src/weiliao_agent/main.py` -- 组合根：读 `WEILIAO_INTENT_CLASSIFIER` 选实现、装配 `IntentRouterFilter(classifier=..., intent_writer=session_manager.write_intent)`；非法值 fail-fast。
- `src/weiliao_agent/pipeline.py` -- 默认链构造不改；`run()` 的 trace 入口 intent=preset 口径不动。
- `tests/test_intent.py` / `tests/test_session.py` -- 2.1/1.3 既有锁定用例（73 例）零回归的参照。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/classifiers/__init__.py` -- 新建：按配置名构造分类器实现的工厂（`create_classifier(name: str) -> IntentClassifier`），包级统一入口。
- [x] `src/weiliao_agent/classifiers/llm_zero_shot.py` -- 新建 LLM zero-shot 默认实现（ChatOpenAI 结构化输出单一标签 + 置信度）；含硬超时守卫（默认 800ms，2026-11-10 评审放宽，原 45ms；env 可调）与模型名经配置注入（脊柱：模型名禁硬编码，gpt-4o 系 2027-01-14 关停需 alias 可换）。
- [x] `src/weiliao_agent/session/manager.py` -- 新增 `write_intent(session_id, turn, classification)`：`SET {prefix}{sid}:intent:{turn} {model_dump_json()} EX ttl`；docstring 更新为完成口径。
- [x] `src/weiliao_agent/filters/intent_router.py` -- 构造可选注入 `intent_writer: Callable[[str, int, IntentClassification], None] | None`；分类成功后 `turn` 非 None 且 writer 存在时调用，try 包裹 fail-safe；分类成功/失败各补 trace 事件调用。
- [x] `src/weiliao_agent/observability/tracing.py` -- 新增 `record_classification`（tag/confidence/source/latency_ms）与 `record_classification_failure`（reason），fail-safe 吞异常。
- [x] `src/weiliao_agent/main.py` -- 组合根装配：env 选实现 + 注入 writer；非法配置值 fail-fast 拒绝启动。
- [x] `tests/test_classifier.py` -- 新建：I/O 矩阵场景单测（写回成功、写失败 fail-safe、turn=None 跳过、超时守卫、配置切换工厂）；`tests/test_session.py` 补 `write_intent` 用例（fakeredis）。
- [x] `tests/eval_intent_classifier.py`（或 `scripts/`）-- 评测 harness：读标注 CSV（text,label）逐条跑分类器输出准确率/混淆矩阵，≥1000 条与 9 类覆盖校验；附每类数十条合成种子用例做冒烟（≥90%·≥1000 真实验收门 deferred，语料就绪后复跑）。

**Acceptance Criteria:**
- Given 配置选定的默认实现已装配，when 处理一条非空买家消息，then 信封 intent 为分类标签（单一标签 + 0.0–1.0 置信度），且 `weiliao:session:{session_id}:intent:{turn}` 存有含 tag 与 confidence 的 JSON，TTL 等于会话 TTL。
- Given 分类成功且 trace_id 为 32-hex，when 管道执行完毕，then Langfuse 消息 trace 可检索到分类事件（标签、置信度、实现来源、延迟毫秒）。
- Given writer 抛 `redis.RedisError`，when 分类成功，then 买家应答不降级、信封保留分类结果、trace 记录写入失败事件。
- Given `WEILIAO_INTENT_CLASSIFIER` 切换为另一合法实现，when 服务重启，then 路由链路代码零改动完成切换（工厂 + 装配测试锁定）；非法值启动即失败。
- Given 分类实现内部超时，when 调用超过硬预算，then 输出 UNKNOWN 且过滤器不向上抛（fail-closed 契约不变）。
- Given `uv run pytest tests -q`，when 全量执行，then 既有 73 例 + 新增用例全部通过，零回归；`ruff check src tests` 与 `mypy src`（strict）无告警。

## Implementation Notes

- 超时守卫双保险：ChatOpenAI 客户端级 timeout=解析后预算（Design Notes
  口径；默认 800ms）+ 调用侧 `future.result(timeout)`（同一预算，超时另
  对未启动任务 `future.cancel()` 防队列积压——2026-11-10 评审补丁）——测试替身延迟亦可触发
  `reason=timeout` 失败事件，守卫先行返回不傻等（超时的 LLM 线程无法
  中止，实例级 ThreadPoolExecutor 上限 8 防御线程膨胀）。
- ChatOpenAI 客户端惰性构造（首次 classify）：组合根导入不依赖 OpenAI
  凭据可用性；凭据缺失/网络错误在调用期抛 `IntentClassificationError`
  （reason=底层异常类型名），过滤器 fail-closed UNKNOWN。
- 置信度回填 0.8 落在分类器实现内（模型不自评时），过滤器侧不感知——
  与「过滤器只消费 IntentClassification 契约」边界一致。
- prompt 以 `SystemMessage`/`HumanMessage` 直接构造（不经 Runnable
  invoke）：避免环境级 LangSmith tracing 在 prompt 组装期引入网络 I/O，
  单测可无网络确定性运行。
- 工厂合法值 `llm_zero_shot`（默认）/ `none`（占位模式，无 LLM 凭据
  环境降级运行）；`none` 使「配置切换」AC 有第二个合法值可测，非法值
  一律 `ValueError` fail-fast。
- 评测 harness 离线冒烟替身（关键词规则）仅验证 harness 机制（覆盖率/
  混淆/报告/exit code），非交付实现；真实验收以 `--live`（种子集）或
  ≥1000 条标注 CSV 复跑为准（exit 1 = 门未过）。

## Spec Change Log

（空——首个评审循环前由 step-04 填充）

## Review Triage Log

| # | 发现 | 裁决 | 证据 / 路由 |
|---|------|------|------------|
| B1 | `llm_zero_shot.create_default()` 为未调用、绕过工厂的第二入口 | low → patch (G4) | 全仓 rg 仅定义处一处；与「包级统一工厂唯一入口」矛盾；最小修法删除 |
| B2 | 工厂饿汉 import langchain，`none` 模式依赖缺失即崩 | false | pyproject 声明 langchain-openai 为硬依赖；依赖缺失时整个包本就不可用，降级场景不成立 |
| B3 | 失败 trace 事件丢弃 detail 文本 | low → reject | 修法需给 `record_classification_failure` 新增参数，超出直接修正；spec 契约即 reason |
| B4 | ThreadPoolExecutor 无关闭钩子 | false → reject | 有界（8 workers、组合根单例、atexit join），无实证危害 |
| B5 | 组合根测试 reload 后污染 `weiliao_agent.main` 模块状态 | low → patch (G2) | 模块保留 FakeRedis 绑定的 app，后续导入该模块的测试读到过期状态；最小修法加 finalizer 恢复 |
| B6 | 默认 `llm_zero_shot` 组合路径无 e2e 测试 | medium → patch (G1) | 与 V1/V2 同根因：组合后默认链路行为无任何断言 |
| B7 | 评测门禁可被绕过/CSV 静默走 live | low → reject | 修法需 per-class 下限、拒行报告等守卫，超出直接修正；harness 系 deferred 门禁工具 |
| B8 | harness 无延迟测量 | low → reject | spec 定义的 harness 范围即准确率/覆盖率；P99<50ms 由 45ms 硬守卫测试锁定 |
| B9 | 新依赖未声明 | false | pyproject 已声明 langchain-openai（运行时）与 fakeredis（测试） |
| B10 | 失败事件缺 session/turn 上下文 | low → reject | 修法需新增参数，超出直接修正；spec 契约即 reason |
| B11 | `write_intent` 全量 TTL 可能长于会话剩余 TTL | false | spec 明确「TTL 等于会话 TTL」，代码逐字符合冻结意图 |
| B12 | skip 路径（turn=None/未注入/none 模式）无 trace 事件 | low → reject | spec 只约定分类成功/失败事件；补事件超出直接修正 |
| E1 | 分类成功路径在 try 外访问 `classification.confidence`，非一致性分类器 AttributeError 逃逸 | medium → patch (G5) | 2.1 防御口径（model_validate 拒绝非一致性分类器）被成功路径旁路；违反过滤器不向上抛契约 |
| E2 | `WEILIAO_INTENT_CLASSIFIER=""` 启动崩溃而非用默认值 | medium → patch (G6) | docker-compose 空值惯用写法触发；改 `or "llm_zero_shot"` 即修 |
| E3 | eval `_LiveClassifier` 单条失败中断整个评测 | medium → patch (G3) | 与 V3 同根因；docstring 承诺 fail-closed UNKNOWN 但无 try |
| E4 | reload 不恢复 sys.modules（同 B5） | low → patch (G2) | 同 B5 根因 |
| E5 | `timeout_seconds<=0` 无构造校验 | low → reject | 仅构造参数误用可达（env 不注入 timeout）；为未实证状态加守卫 |
| V1 | 组合根 writer 接线删除后全仓测试仍绿（pre-verified） | medium → patch (G1) | 采信该层举证：writer 仅替身测、`:intent:` 键仅直调测，两组合测试走 none/非法分支 |
| V2 | 默认配置值未锁定（pre-verified） | medium → patch (G1) | 采信该层举证：默认改 "none" 全仓仍绿；一行 isinstance 断言即修 |
| V3 | `_LiveClassifier` 不 fail-closed（同 E3） | medium → patch (G3) | 同 E3 根因 |
| V4 | 组合根 reload 污染（同 B5/E4） | low → patch (G2) | 同 B5 根因 |

分组路由：G1（B6+V1+V2，组合根默认链路无测试锁定）/ G2（B5+E4+V4，测试污染主模块）/ G3（E3+V3，eval live 不 fail-closed）/ G4（B1，死代码）/ G5（E1，confidence 访问逃逸）/ G6（E2，空 env 崩溃）→ 全部 patch；无 intent_gap / bad_spec，不触发回环。

### Review Findings

（2026-11-10 Epic 级 code review：四层原始 16 项合并去重后 15 项，本分录 2 项 decision + 1 项 defer + 7 项 patch；spec-2-1 / spec-2-3 各 1 项。测试基线：842 passed, 4 skipped）

- [x] [Review][Decision→Patch] 45ms 硬超时与真实 LLM 网络 RTT 物理不相容（high；blind-hunter 层） [`src/weiliao_agent/classifiers/llm_zero_shot.py:38` `DEFAULT_TIMEOUT_SECONDS = 0.045`] — 已回 96ec504 核实：timeout 仅构造参数可调，无 env 注入路径；真实部署下分类确定性超时 UNKNOWN → 追问/hard trigger 整体失效，fail-closed 把全损伪装成正常降级。**裁决（2026-11-10）：选 A 放宽硬预算**——`DEFAULT_TIMEOUT_SECONDS` 0.045 → 0.8（300–800ms 区间上沿，仍在全链路 P99 < 3s / AD-9 的 1.8s LLM 预算内），同时新增 `WEILIAO_INTENT_TIMEOUT_SECONDS` env 注入（构造参数 > env > 默认）并加正值校验；P99 < 50ms 分类延迟 NFR 明示为不可达缺口，epic-2-context 与 spec-2-2 延迟口径同步修订；`latency_ms` 埋点保留，真实 P99 待 Story 7.2 压测设施实测回填（deferred-work 已有 6-3 挂账）。**已修复（2026-11-10）：代码 + `.env.example` / README 声明 + `scripts/loadtest_intent_chain.py` FR1_P99_BUDGET_MS 50→800 一并落地，全套件 859 例绿。**
- [x] [Review][Decision→Defer] session_id 无格式约束可致 Redis key 命名空间碰撞（low；blind-hunter 层） [`src/weiliao_agent/models/envelope.py:40` + `src/weiliao_agent/session/manager.py` `KEY_PREFIX`] — 信封仅 `min_length=1` 无字符集约束；`session_id="a:intent:1"` 的会话 hash key 与会话 `"a"` 的 turn 1 意图 string key 逐字节同串 → `WRONGTYPE` 持续 fail-closed 降级，TTL 内自我维持。**裁决（2026-11-10）：选 A 接受风险，defer**——理由：仅构造输入可达、后果为 fail-closed 降级非越权，加 charset 校验需先确认上游 SpringBoot session_id 字符集；已记录 deferred-work.md，settle：部署形态落地、确认上游 session_id 字符集后复审。
- [x] [Review][Defer] 失败路径观测仅存在于 Langfuse——未配置 Langfuse 的部署中分类全损事故完全静默（medium；blind-hunter 层） [`src/weiliao_agent/observability/tracing.py` `record_classification_failure`] — defer，pre-existing（1.5 既定替身/无 client no-op 模式）：2.1 评审 defer 的「分类失败路径静默」未真正关闭，只是兑现物本身在无 client 时为 no-op；代码库多处已用 stdlib logging（main/pipeline/http 等），tracing 失败路径无一处 logger。settle 路径：随可观测部署工作（1.5 deferred 同路径）统一决定失败事件是否加 logger 兜底；与上一条 45ms 决策联动——若选 D（默认降级），分类失败日志成为必选项。已记录 deferred-work.md。
- [x] [Review][Patch] 超时丢弃的 LLM 任务不 cancel、executor 无界队列——超时风暴下积压内存 + 恢复后僵尸洪峰（medium；blind-hunter+edge-case-hunter 两层同根因合并） [`src/weiliao_agent/classifiers/llm_zero_shot.py` `classify()`] — `future.result(timeout)` 超时后仅抛异常，不 cancel future：排队中任务无法取消（`_MAX_WORKERS=8` 只防线程膨胀不防队列膨胀），积压速率=RPS、消化速率=8/调用时长；LLM 恢复后队列中数百条针对已应答消息的陈旧分类一次性执行（无效 token 消耗洪峰），期间真实消息分类吞吐接近归零。修法：FutureTimeoutError 路径加 `future.cancel()`（对未启动任务有效，一行）；可选 `threading.BoundedSemaphore(_MAX_WORKERS)` submit 前限流。**已修复（2026-11-10）：`classify()` FutureTimeoutError 路径加 `future.cancel()`（最小修法；BoundedSemaphore 限流未采纳——cancel 已覆盖排队中任务，信号量属未实证复杂度，同 E5 裁决口径）。**
- [x] [Review][Patch] eval harness `_load_csv` 不容忍 UTF-8 BOM（low；edge-case-hunter 层） [`tests/eval_intent_classifier.py` `_load_csv`] — Excel/WPS 另存 CSV 普遍带 BOM，首列名解析为 `"\ufefftext"` 致 `SystemExit("CSV must have columns text,label")`（报错有误导性）；目标使用路径（运营导出语料跑验收门）必踩。修法：改 `encoding="utf-8-sig"`。**已修复（2026-11-10）。**
- [x] [Review][Patch] eval 门禁逻辑零自动化验证（low；verification-gap 层，经变异论证实证） [`tests/eval_intent_classifier.py` `_evaluate`] — 全仓无任何测试 import 该模块（文件名非 `test_` 前缀不在收集路径）；变异：`>= GATE_ACCURACY` 改 `>`、删 coverage 条件、`MIN_EVAL_ROWS` 改 10、deferred 分支 `return 0` 改 `return 1`——全套件全绿。该门禁是 AC「≥90%·≥1000 真实验收门」（deferred 至语料就绪）的唯一执行体。修法：单测 import 模块 + stub 分类器走 `_evaluate` 四分支（999 行 deferred / 1000 行单类 coverage 不足 deferred / acc=0.85 exit 1 / acc=0.92 exit 0）+ `_load_csv` 非法行过滤两例，全程无网络。**已修复（2026-11-10）：新建 `tests/test_eval_intent_classifier.py` 6 例（四分支 + BOM/非法行 + 缺列拒绝）。**
- [x] [Review][Patch] `intent.classification_failure` 事件 level/event 判别字段无断言（low；verification-gap 层） [`src/weiliao_agent/observability/tracing.py` + `tests/test_classifier.py` / `tests/test_intent.py`] — 对照 `intent.followup_hard_trigger` 的 `level=="WARNING"` 有断言（test_intent.py），三处失败事件断言只查 name 与 metadata["reason"]；变异删 `level="ERROR"` 或 event 字段全套件绿，下游按级别告警的观测配置静默失效。修法：三处各补一行 `assert failure[0]["level"] == "ERROR"`（及一处 event 字段），零新测试成本。**已修复（2026-11-10）：三处补 level 断言（writer 失败 / 分类超时 / recorder 失败）；event 判别字段断言未加——`name` 与 `metadata["event"]` 同面冗余，name 已断言，按最小修法收口。**
- [x] [Review][Patch] `create_classifier` 规范化与 env 空串回落无测试（low；verification-gap 层） [`src/weiliao_agent/classifiers/__init__.py` + `src/weiliao_agent/main.py`] — `name.strip().lower()` 归一化与 `os.environ.get(...) or "llm_zero_shot"` 空串回落均只有文档承诺、无测试锁定（变异删除归一化/改 `.get(default)` 形式全套件绿）。修法：两个参数化用例 `create_classifier(" LLM_ZERO_SHOT ")` / `create_classifier(" None ")` + 一个 `setenv("")` 组合根 reload 用例断言装配出 `LlmZeroShotClassifier`。**已修复（2026-11-10）：test_session.py 补 4 例（规范化参数化 ×2 + none 归一化 ×2 + 空 env 组合根回落）。**
- [x] [Review][Patch] 组合根显式过滤器链构成无测试钉住（low；verification-gap 层） [`src/weiliao_agent/main.py`] — diff 将 `PipelineOrchestrator()` 默认链改为显式清单（生产链权威），既有组合根测试只断言行为不命名链构成；变异删除/重排任一过滤器全套件绿——risk_review 是合规闸挂点，生产链与默认链悄然分叉无信号。修法：组合根测试补一条经 `pipeline` 只读属性断言过滤器名清单（注意：工作树 HEAD 链成员已含后续 Epic 过滤器，断言以 HEAD 实际清单为准）。**已修复（2026-11-10）：`test_main_composition_root_filter_chain_is_pinned` 经 create_app 间谍捕获编排器，断言 HEAD 实际链六成员与顺序（coreference → intent_router → cache_lookup → llm_generate → risk_review → response_assembler）。**
- [x] [Review][Patch] eval harness `ALL_TAGS` 手抄 9 类枚举，未按 2.1 既定模式派生（low；acceptance-auditor 层） [`tests/eval_intent_classifier.py:25`] — 2.1 评审已将 test_intent.py 同类问题裁定为 patch 并改 `typing.get_args(IntentTag)` 派生；harness 未套用——`IntentTag` 日后增删类时覆盖率/门禁口径静默漂移（`_load_csv` 的 label 过滤与 GATE coverage 检查均以此为准）。修法：改 `ALL_TAGS: tuple[IntentTag, ...] = typing.get_args(IntentTag)`。**已修复（2026-11-10）。**

## Design Notes

**writer 注入而非过滤器直写 Redis**：AD-4 要求意图只能经会话管理模块写入。过滤器注入的是 `SessionManager.write_intent` 方法引用（callable），满足单写者且过滤器不持有 Redis 客户端；测试路径不注入即跳过，天然兼容既有 73 例的替身装配。

**trace 可验证口径**：编排器入口 `trace_message` 的 intent 是 preset 值（分类尚未发生），故分类结果只能以事件形式补挂同一 trace_id——这正是 2.1 评审 defer 的「分类失败路径观测挂点」，本 Story 一并兑现（成功事件 + 失败事件 + 写失败事件）。

**LLM 方案延迟守卫**（已选定；2026-11-10 评审修订）：客户端级 timeout 设在预算内（默认 800ms，原 45ms 与真实网络 RTT 物理不相容，P99 < 50ms NFR 记为不可达缺口），超时/网络错误一律返回 UNKNOWN + trace 失败事件；不给规则兜底（保持单一来源，避免双实现口径漂移）。置信度取 LLM 结构化输出的自评分数；若模型不自评，由过滤器侧以固定口径回填（如 0.8），不回填 1.0——为 2.3 追问归并保留信号空间。

## Verification

**Commands:**
- `uv run pytest tests -q` -- expected: 既有 73 例 + 新增全部通过，零回归。
- `uv run ruff check src tests` -- expected: 无告警。
- `uv run mypy src` -- expected: strict 无错误。
- `uv run python tests/eval_intent_classifier.py`（无参）-- expected: 内置合成种子集冒烟通过；带 CSV 参数时输出 9 类覆盖率与准确率报告（≥90%·≥1000 真实验收 deferred 至语料就绪）。
