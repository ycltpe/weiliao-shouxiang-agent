---
title: 'Story 5.2 Kafka 转人工单向指令'
type: 'feature'
created: '2026-11-09'
status: 'done'
route: 'dispatch'
review_loop_iteration: 1
baseline_commit: 'd7f0bf0448f1f83a07e74978554af413856514ec'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Story 5.1 命中硬触发后只回复了固定安抚文案，经纪人工作台收不到任何转交指令——买家在等，经纪人却不知道有人要接管，红线会话实际上没有真正转出去。

**Approach:** 在管道编排器转人工出口分支内新增 Kafka 转交指令发送：严格先发送指令（→ SpringBoot 经纪人工作台）、后返回安抚文案（买家通知 = 5.1 既有出口文案，本 Story 不新增下发通道）。发送带重试与有界阻塞预算；重试 3 次失败写死信队列（独立 Kafka topic）并触发 ERROR 级告警日志。payload 携带会话上下文（最近 3 轮对话原文 + 命中条件），供经纪人接管时直接了解买家诉求。

**Decisions（人类拍板，2026-11-09）：**
- 会话上下文「最近 3 轮对话摘要」形态 = 原文最近 3 轮（Q1 选 A）：买家消息 + Agent 回复逐字原文，经新增会话转写 Redis 落盘原语提供，无 LLM 依赖，P99 可控；工作台侧自行展示，不生成摘要。

## Boundaries & Constraints

**Always:**
- 出口分支严格先指令后通知（AD-7）：发送完成（或重试耗尽）后才返回 ``HANDOFF_REPLY_TEXT``。
- 指令发送为单向出口：fire-and-forget 语义但有界阻塞（总预算远小于编排器 2.5s 内部预算，为 NFR-1 P99 < 3s 留余量）；触发到送达工作台 P99 < 5s。
- 重试 3 次（首次发送即第 1 次，间隔有界指数退避）后写 DLQ topic + ERROR 级结构化告警日志（沿用仓库惯例 ``event=<name>_alert``，无外部 paging 系统）。
- payload ``conditions`` 过滤 ``assessment_error`` 内部哨兵（5.1 评审处置 #4 口径：诊断词不外发）。
- 会话上下文只经 SessionManager 读写（AD-4 单写者）；新增转写记录/读取为 SessionManager 原语。
- 发送全过程记 Langfuse trace（指令发出/失败/DLQ，fail-safe no-op 模式）。

**Never:**
- 不做转人工后 session 响应路径关闭与原子终态收口（Story 5.3）。
- 不改硬触发判定逻辑、信封既有字段语义与 HANDOFF_REPLY_TEXT（5.1 冻结）。
- 不做 AI 身份标识注入（Story 5.4）；不新增买家下发适配器通道。
- 禁止发送失败改变买家应答：任何失败路径回复仍是固定转人工文案，绝不降级为模板兜底、禁止回流响应组装。
- 指令只出不进：不新增消费该 topic 的消费者。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常触发 | 硬触发命中，producer 可用 | 指令送达工作台（payload 含条件+上下文）→ 回固定文案 | N/A |
| Kafka 瞬时故障 | 第 1/2 次发送失败 | 有界退避重试，恢复则送达，回复不变 | 重试计数写 trace |
| 重试 3 次全败 | 持续发送失败 | 写 DLQ topic + ERROR 告警日志；回复仍为固定文案 | DLQ 写失败仅告警日志，回复不变 |
| 无历史转写 | 新会话首次消息即触发 | payload 上下文仅当前触发轮，指令照发 | Redis 读失败：上下文降级为空+当前轮，指令照发 + trace 事件 |
| producer 未配置 | WEILIAO_KAFKA_BROKERS 为空（本地开发） | 跳过发送，出口文案不变（与消费者门控同口径） | N/A |
| 发送阻塞超预算 | Kafka 长时间不响应 | 按重试耗尽处理（DLQ+告警），不拖垮编排器 deadline | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/pipeline.py:75` -- 编排器；5.1 短路分支与 ``_handoff_result``（L216 附近）为本 Story 发送挂点；出口分支需注入发送器。
- `src/weiliao_agent/session/manager.py` -- 单写者；新增转写原语挂点（``mark_handoff`` L200 附近为 Lua 原语先例；``_LUA_MARK_HANDOFF`` 模式可复制）。
- `src/weiliao_agent/handoff/` -- 5.1 评估模块所在包；发送器（producer 包装 + payload 构建）为同包新模块的自然落点。
- `src/weiliao_agent/observability/tracing.py:423` -- ``record_handoff_assessment`` / ``record_handoff_mark_failed``（L465）为 handoff trace helper 先例，新 helper 照此封装。
- `src/weiliao_agent/workers/house_sync.py` -- aiokafka 使用先例（0.15.0，mypy import-untyped 豁免写法可参照）；反向注意：本 Story 是 producer 不是 consumer。
- `src/weiliao_agent/main.py:446` -- 组合根 lifespan Kafka 门控（``WEILIAO_KAFKA_BROKERS`` 非空才装配）；新 env（topic/DLQ topic）在配置区（L400 附近 WEILIAO_HANDOFF_* 先例）解析，fail-fast。
- `src/weiliao_agent/models/envelope.py` -- ``HandoffAssessment``（含 conditions 与 assessment_error 哨兵语义）；payload 不直接复用信封模型（跨进程契约独立定义）。
- `src/weiliao_agent/adapters/http.py:142` -- 消息端点 ``post_message`` 尾部为回复落盘唯一可得点（envelope.message + PipelineResult.reply 齐备），转写记录调用挂此处；仅追加不改动既有 fail-closed 语义。
- `tests/` -- 扁平 ``test_<模块>.py``；fakeredis[lua] 支持 Lua 原语测试（conftest 共享 fixture）；5.1 ``test_handoff_composition_root.py`` 为装配测试先例。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/handoff/command.py`（新模块）-- 转交指令 payload 模型（独立 Pydantic 契约：handoff_id/version/session_id/house_id/trace_id/triggered_at/conditions/recent_turns）+ 发送器（aiokafka producer acks=all 显式指定，独立后台事件循环线程，同步 send() 经 run_coroutine_threadsafe，message key=session_id，重试 3 次有界退避且睡前重算剩余预算，耗尽写 DLQ topic + ERROR 告警，topic 与 DLQ topic 相同即构造拒绝，全程 fail-safe trace）-- 单向指令核心。
- [x] `src/weiliao_agent/session/manager.py` -- 新增 ``record_transcript``（Lua/单 roundtrip 写 per-turn 转写，list 截断保留最近若干轮，TTL 与会话一致）与 ``read_transcript(session_id, limit)`` 原语 -- AD-4 单写者口径，payload 上下文权威来源。
- [x] `src/weiliao_agent/pipeline.py` -- 出口分支注入发送器：先构建并发送指令（conditions 过滤哨兵），完成或耗尽后返回 HANDOFF_REPLY_TEXT；发送器为 None（未配置）时跳过 -- AD-7 严格先指令后通知。
- [x] `src/weiliao_agent/adapters/http.py` -- ``post_message`` 管道返回后经 SessionManager 记录本轮转写（buyer message + reply），写失败仅告警不影响应答 -- 上下文积累点。
- [x] `src/weiliao_agent/observability/tracing.py` -- 新增指令发送 trace helper（sent/failed/dlq 事件）-- 可观测。
- [x] `src/weiliao_agent/main.py` + `.env.example` -- 新 env：``WEILIAO_HANDOFF_KAFKA_TOPIC``（默认 handoff.commands）/ ``WEILIAO_HANDOFF_KAFKA_DLQ_TOPIC``（默认 handoff.commands.dlq），复用 ``WEILIAO_KAFKA_BROKERS`` 门控，fail-fast 校验，装配注入编排器 -- 灰度与装配通道。
- [x] `tests/test_handoff_command.py` + `tests/test_handoff_transcript.py`（命名可斟酌）-- 覆盖 I/O 矩阵全场景（重试/DLQ/超预算/无转写/未配置 producer）+ 会话转写读写原语 + 装配测试 -- 回归网。

**Acceptance Criteria:**
- Given 硬触发命中且 producer 可用，when 出口分支执行，then 转交指令（payload 含过滤后的命中条件与最近 3 轮对话上下文）先于安抚文案发送，触发到送达 P99 < 5s。
- Given 出口分支构建指令，then payload 恒携带 ``handoff_id``（单次触发唯一，UUID4）与 ``version`` 契约版本，Kafka message key = ``session_id``——投递语义为 at-least-once，重复指令由工作台消费者按 ``handoff_id`` 去重。
- Given 指令发送重试 3 次仍失败，when 出口分支继续，then 指令写 DLQ topic 且触发 ERROR 级告警日志，买家应答仍为固定转人工文案（非模板兜底、非降级）。
- Given 会话历史无转写或 Redis 读取失败，when 构建 payload，then 上下文降级为当前轮（或空），指令照发且记 trace 事件，发送主路径不受影响。
- Given ``WEILIAO_KAFKA_BROKERS`` 未配置，when 硬触发命中，then 跳过发送、出口文案语义不变（与消费者未配置门控同口径）。

## Implementation Notes

2026-11-09 实现完成（子代理实现 + 主流程 diff 审计）：
- 发送器定参：``SEND_BUDGET_SECONDS=1.0``、``MAX_SEND_ATTEMPTS=3``、退避 0.05s→0.1s（封顶 0.2s）；``HandoffTransport`` Protocol 使重试/DLQ 逻辑可注入内存替身单测，aiokafka 实现在独立后台事件循环线程（producer 创建/start/stop 均绑定该 loop）。
- 组合根细节：producer start 失败不拖垮服务——ERROR 告警 + ``sender.disable()``（出口分支跳过发送，文案语义不变）；lifespan 读 env 走运行期门控（既有消费者测试 monkeypatch env 的回归已修复并锁定）。
- 转写上限 ``TRANSCRIPT_KEEP_TURNS=10``（设计 Notes「如 10 轮」落地）；payload 历史只读最近 2 条 + 当前信封轮拼 3 轮。
- 验证：``uv run pytest tests/ -q`` 642 passed；``uv run ruff check src tests`` 零告警；mypy strict 干净（仅 src）。
- 遗留风险：真实 broker 行为（线程生命周期、send_and_wait 异常）仅经 transport 替身验证，无自动化 P99 证明——有真实 broker 后建议补 soak 测试。

2026-11-09 评审轮 2 补丁（9 项，实现子代理执行）：
- 观测性：禁用发送器每命中一条 WARNING（含 session_id + handoff_id）；发送成功 INFO 日志；ERROR 告警串与 failed/dlq 事件均带 handoff_id；转写失败告警带 session_id/trace_id。
- payload 尺寸护栏：``PAYLOAD_FIELD_CHAR_CAP = 2000``，当前轮与历史轮的 buyer_message/agent_reply 截断，超大消息不再顶破 broker 上限。
- 健壮性：close() join 超时线程仍活补 WARNING、线程退出后 loop.close()；退避 base/cap 非负构造校验。
- 验证补强：发送器 start/close 委托测试；fakeredis SessionManager 作 transcript_reader 的集成测试（绑定替换为 lambda 即红）；真实编排器 + 真实发送器组合测试。
- 更正：全量验证实为 ``uv run pytest tests/ -q`` 658 passed；``ruff check`` 零告警；``mypy src`` 44 文件零问题（前注 642/652 为中间轮次计数，以此为准）。

## Spec Change Log

轮次 1（2026-11-09，评审回环）：盲审/边界/验证缺口三层评审发现跨进程契约定义不完整——payload 无 ``handoff_id``（消费者无法去重）、无 ``version``；未声明投递语义（``run_coroutine_threadsafe`` 超时不会取消在途发送，重试可能产生重复指令）。修订 Design Notes 补全契约（handoff_id / version / key=session_id / at-least-once + 消费者去重），并补录 Verification 的 mypy 命令行（与 Implementation Notes 声明对齐）。
触发发现：盲审 #2/#5、边界 #3、盲审 #8。
已知坏状态：契约字段缺失会让工作台侧静默收不到去重键，字段重命名/类型变更跨进程无版本可谈。
KEEP 指令（重推导必须保留的正面经验）：传输层线程模型（后台事件循环 + run_coroutine_threadsafe 桥接）与 HandoffTransport Protocol 注入测试模式；重试/DLQ/预算归一逻辑与其 I/O 矩阵测试；转写 Lua 原语与防御解析；组合根门控与 fail-fast 装配测试口径；trace helper 的保留键权威模式。

## Review Triage Log

评审轮次 1（2026-11-09，三层：盲审 13 / 边界 9 / 验证缺口 2）：

| # | 发现 | 裁决 | 处置 |
|---|------|------|------|
| 1 | payload 无 handoff_id/无 version/无投递语义声明，超时在途发送可能实际送达产生重复指令（盲审#2/#5 + 边界#3，同根因：跨进程契约定义不完整） | medium（契约缺口，工作台去重与排障能力受损） | bad_spec（回环） |
| 2 | spec Verification 缺 mypy 命令行，与 Implementation Notes 声明不一致（盲审#8） | low（属实，直接补录） | bad_spec（随回环一并修订） |
| 3 | AIOKafkaProducer 全默认参数，acks 未显式指定（盲审#6） | low（属实；合规关键指令应显式 acks=all） | patch（随回环重做） |
| 4 | 退避睡眠用发送前陈旧的剩余预算，总阻塞可超预算上限 0.2s（边界#4） | low（属实，直接修正：睡前重算剩余预算） | patch |
| 5 | topic == dlq_topic 配置可通过校验，DLQ 写进 live topic 且告警误导（边界#5） | low（属实，构造器直接拦截） | patch |
| 6 | start 的 ready 超时分支不清理线程，与 producer 失败分支清理不对称（边界#6） | low（属实，直接修正：raise 前 close） | patch |
| 7 | 编排器逃逸兜底路径的降级轮次不落转写，后续 handoff 上下文缺轮（盲审#12 + 边界#8，同根因） | low（属实，except 分支补记转写） | patch |
| 8 | lifespan 的 start 失败 disable 与 shutdown close 无测试（验证缺口#1，预验证：删 disable 全套件仍绿） | medium（属实） | patch（随回环重做） |
| 9 | _KafkaTransport 生命周期零覆盖（验证缺口#2 + 盲审#11，同根因） | medium（属实） | patch（随回环重做） |
| 10 | transcript 与会话 hash TTL 漂移致旧会话转写泄漏（盲审#3） | false（驳回：两 key 均随本轮刷新，漂移 ≤ 管道时长；fallback 路径不刷新转写反而使其更早过期，所述泄漏窗口不成立） | reject |
| 11 | http.py 转写只捕获 redis.RedisError，非 Redis 异常逃逸成 5xx（盲审#1 + 边界#1/#9，同根因） | false（驳回：与 begin_turn 既有收窄捕获同口径——「编程错误照常上抛，不静默降级掩盖缺陷」是同文件既定设计；spec「写失败」指 Redis 写失败） | reject |
| 12 | result.to_dict() 移出 try，序列化异常将 500（边界#2） | false（驳回：frozen dataclass 的 asdict 对 str/bool/None 字段不可达抛错；loudly-fails-on-unreachable 非缺陷） | reject |
| 13 | _build_command 抛异常违反 send_command 绝不抛出契约（边界#7） | false（驳回：生产 transcript_reader 为 SessionManager.read_transcript，不返回畸形数据；编排器防御层已保买家应答） | reject |
| 14 | sprint-status 与 spec 状态不一致（盲审#7） | false（驳回：工作流状态机自身——step-03 置 in-progress、step-04 置 in-review，非代码缺陷，同 5.1 评审#14 裁决） | reject |
| 15 | Spec Change Log / Review Triage Log 空节像被跳过（盲审#9） | false（驳回：模板规定两节分别由回环与评审步填充，正由本步填充） | reject |
| 16 | topic/DLQ fail-fast 在 broker 未配置时也拦截启动（盲审#10） | false（驳回：无条件配置 fail-fast 是仓库惯例——5.1 词表/阈值同口径；门控针对网络触达不针对配置校验） | reject |
| 17 | 无指令健康聚合指标，DLQ 累积只能翻日志（盲审#13） | low 拒绝（修复需新增指标面，超出直接修正；ERROR 告警已逐事件存在） | reject |
| 18 | 同 session 连续红线消息产生 N 条转交指令（盲审#4） | low（属实；每命中即触发符合 Epic 口径，会话响应路径关闭归 5.3——5.1 评审#9 已 defer 同根问题） | defer |

评审轮次 2（2026-11-09，回环后重推导代码再审，三层：盲审 12 / 边界 4 / 验证缺口 2）：

| # | 发现 | 裁决 | 处置 |
|---|------|------|------|
| 19 | 状态文件 in-progress 与 spec in-review 不一致（盲审#1） | false（驳回：同轮 1 #14——工作流状态机自身，step-05 收口对齐） | reject |
| 20 | 发送器禁用后每次命中静默丢指令，零观测（盲审#2） | low（属实，补每命中一条 WARNING） | patch |
| 21 | 发送成功无本地日志，未配 Langfuse 时无从查证（盲审#3） | low（属实，补 INFO 级发送日志） | patch |
| 22 | 告警与 failed/dlq 事件缺 handoff_id，无法与 DLQ 记录关联（盲审#4） | medium（属实，告警串与事件 metadata 各补一行） | patch |
| 23 | 转写失败告警缺 session_id，日志不可检索（盲审#5） | low（属实，补变量） | patch |
| 24 | 降级/兜底回复原文落盘无标记，工作台或误读为真实回答（盲审#6） | low 拒绝（驳回：转写记录即买家所见原文，逐字准确；加标记需动契约字段，超出直接修正） | reject |
| 25 | payload 无尺寸护栏，超大消息可顶破 Kafka max_request_size 致指令丢失（盲审#7） | medium（属实，_build_command 内对消息/回复字段截断上限） | patch |
| 26 | 无编排器→真实发送器→SessionManager 组合测试，协议与实现接线漂移不可察（盲审#8 + 验证缺口#4，同根因） | medium（属实） | patch |
| 27 | 发送器 start()/close() 委托 transport 无测试，改空方法全套件仍绿（验证缺口#3） | medium（属实） | patch |
| 28 | TRANSCRIPT_KEEP_TURNS=10 保留无消费者（盲审#9） | low 拒绝（无害保留，读窗口留宽不违背任何约束） | reject |
| 29 | close() join 超时线程泄漏无日志、loop 对象未关闭（盲审#10 + 边界#2，同根因） | low（属实：join 后线程仍活补 WARNING；关停后 loop.close()） | patch |
| 30 | 退避 base/cap 负值未校验，time.sleep(负数) 可违反绝不抛出契约（边界#1） | low（属实，构造器非负校验） | patch |
| 31 | 门控 import 期与运行期不对称（盲审#12） | low 拒绝（驳回：注释已载明系轮 0 回归修复的既定取舍；生产 env 静态，所述分叉仅测试场景） | reject |
| 32 | 超预算路径不写 DLQ 与矩阵行「DLQ+告警」表述歧义（边界#3，claim） | false（驳回：超预算即无剩余可阻塞预算，强行写 DLQ 必然违背同行「不拖垮 deadline」；告警如实记 dlq_written=False，是唯一自洽读法） | reject |
| 33 | Implementation Notes 测试计数 642 过期（实际 652）（边界#4，claim） | low（属实，追加更正记录） | patch |

## Design Notes

- 跨进程契约（轮次 1 补全，评审发现驱动）：payload 恒携带 ``handoff_id``（UUID4，单次触发唯一——编排器出口分支每次触发生成，消费者据此去重）与 ``version: int = 1``（契约版本，字段演进协商基础）；producer 发送带 ``key=session_id``（同会话指令分区保序）。投递语义 = at-least-once：``run_coroutine_threadsafe`` 超时不取消在途发送，首次尝试可能实际送达而重试再发一条——重复指令由工作台消费者按 ``handoff_id`` 去重（消费侧实现归工作台 Story，本服务只保证去重键随 payload 送达）。
- 发送器线程模型：管道是同步执行（FastAPI 同步端点 + ThreadPoolExecutor），aiokafka producer 为 async——发送器自持有后台事件循环线程，对外暴露同步 ``send()``（``run_coroutine_threadsafe(...).result(timeout)``），与编排器现有同步语义零侵入。
- 阻塞预算：单次 send 超时 + 退避总量设上限（建议 ≤1s），保证编排器 2.5s deadline 内剩余过滤器预算不被挤占；超预算按重试耗尽归一（DLQ+告警），不等 Kafka。
- 转写形态：per-turn 一条（JSON：turn/buyer_message/agent_reply/timestamp），Redis list ``weiliao:session:{id}:transcript`` 右进左出截断（保留上限如 10 轮，读取只取最近 2 条 + 当前信封轮拼成 3 轮）；TTL 与会话一致，key 过期即从零开始。
- payload 字段值为跨进程契约（SpringBoot 工作台消费），独立 Pydantic 模型定义在本服务侧，不引用信封模型，避免管道内部结构外泄。

## Verification

**Commands:**
- `cd /Users/yechen/PycharmProjects/test && uv run pytest tests/ -q` -- expected: 全部通过（含新增用例，无回归）
- `uv run ruff check src tests` -- expected: 零告警
- `uv run mypy src` -- expected: 零告警（仓库 mypy strict 口径，仅扫 src）
