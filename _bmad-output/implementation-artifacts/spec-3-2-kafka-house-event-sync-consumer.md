---
title: 'Story 3.2 Kafka 房源事件同步消费者'
type: 'feature'
created: '2026-11-05'
status: 'done'
baseline_commit: '22de14b72b1ce7c546c3f01b081bd57256a8cf0a'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** AD-6 规定事实卡片以 Kafka `house.sync.trigger` 事件为唯一写入路径，但仓库现状是：无 Kafka 客户端依赖、无 broker（docker-compose 无 Kafka/Redpanda）、`workers/` 仅一行 docstring、事件 payload 契约无任何权威定义——缓存事实无法与房源主数据同步。

**Approach:** 定义 `house.sync.trigger` 事件 Pydantic 契约，新建 `workers/house_sync` 异步消费者（aiokafka）：事件反序列化 → 构建/作废 `FactCard` 经 L0 客户端写入；按事件时间戳判定超期（>500ms）按失效处理并告警；消费者作为 asyncio 任务由服务 lifespan 托管，与 API 请求路径零共享状态。

**Decisions（2026-11-05 人类决策）:**
- 事件 payload 契约为**全量快照**：payload 直接携带事实全字段（house_id/status/price_yuan/school 可选/dialogue_summary 可选/fact_version/occurred_at/event_id/event_type），消费端薄转换、无 DB 依赖。
- 告警落点为 **Langfuse ERROR 事件 + 结构化 stderr 日志**：复用现有 fail-safe 观测栈，不引入外部告警系统。

## Boundaries & Constraints

**Always:**
- 事件消费是事实卡片的唯一写入路径；消费者不得直写 DB（AD-6）。
- 消费延迟预算 500ms 自事件 `occurred_at` 起算至 L0 写完成；超期事件按失效处理并触发告警。
- at-least-once：offset 在 L0 写成功后手动提交；重放幂等由 Story 3.1 的 fact_version 乱序守卫保证。
- 消费者 fail-safe：解析失败、Redis 故障、Kafka 断连均不得使消费者崩溃或影响 API 路径；观测一律走 Langfuse fail-safe 模式。
- L0 同步调用（redis-py 同步客户端）经 `asyncio.to_thread` 卸载，不阻塞事件循环。
- 沿用 Story 3.1 单实例 Redis 约束。

**Never:**
- 不改 `cache/l0_client.py` 既有行为，其全部既有测试保持绿色（key 三结构、Lua、守卫语义冻结）。
- 不引入服务直写 DB 路径；不做 Kafka 集群/分区生产拓扑设计（本地 redpanda 单节点仅供开发联调）。
- 不做转人工指令消费（Story 5.2）、L1 缓存、缓存查找接线（Story 3.3/3.4）。
- 不改信封模型、过滤器链、HTTP 适配层与会话管理模块。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| upsert 事件 | 合法 `HouseSyncEvent`(upsert) | `price_yuan` 经 `price_to_bucket` 转桶，L0 set 成功，提交 offset，记录 write 事件（含延迟 metadata） | L0 set 返回 False（Redis 故障）：不提交 offset（可重放）+ ERROR 事件 |
| invalidate 事件 | 合法 `HouseSyncEvent`(invalidate) | L0 invalidate（fact_version 必填路径），提交 offset，记录 invalidated 事件 | 同 upsert |
| 超期事件 | `occurred_at` 距消费时刻 >500ms | 不写入事件内容；按失效处理（invalidate 该 house + 事件 fact_version）+ 告警事件 | 告警 = Langfuse ERROR 事件 + 结构化 stderr 日志（已决策） |
| 非法 payload | JSON 损坏 / schema 校验失败 | 跳过该消息、offset 提交（毒消息不阻塞消费）、记录 corrupted 事件 + 结构化日志 | 不崩溃、不重试 |
| 乱序 / 重放 | 低版本或同版本事件重复到达 | Story 3.1 守卫拒写或幂等接受，无旧事实复活，offset 正常提交 | 版本冲突计入事件 metadata |
| Kafka 断连 | broker 不可达 | 消费循环指数退避重连，API 路径不受影响 | ERROR 事件 + 结构化日志 |
| 启动 / 关停 | 服务启动 / SIGTERM | lifespan 启动消费任务；关停时停止拉取、关闭 consumer（aiokafka 自动提交在途 offset 或处理完在途消息） | 关停超时强退，不悬挂进程 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/workers/__init__.py` -- 现仅 docstring 占位（"Kafka 消费者：房源事件同步、转人工指令"），需新增导出。
- `src/weiliao_agent/cache/l0_client.py` -- `L0CacheClient.get/set/invalidate` 公开 API（set/invalidate 返回 bool；invalidate 的 fact_version 缺失抛 ValueError）；三 Lua 守卫（墓碑/索引/乱序）是重放幂等的根基，只调用不改。
- `src/weiliao_agent/models/fact_card.py` -- `FactCard`、`HouseStatus`、`price_to_bucket`（fact_card.py:48-60 字段权威）；事件模型与卡片构建的复用来源。
- `src/weiliao_agent/models/envelope.py` -- tz-aware timestamp 校验风格，`occurred_at` 复用同款约定。
- `src/weiliao_agent/observability/tracing.py` -- `record_cache_event`（约 L437）fail-safe 与替身注入模式（`set_langfuse_client`）先例；新增消费者事件函数对齐此模式。
- `src/weiliao_agent/main.py` -- composition root：L39-45 `redis.Redis.from_url(os.environ["WEILIAO_REDIS_URL"] 缺省 127.0.0.1:6379/0, 0.5s 超时)`；L53 env 直读配置模式（`WEILIAO_INTENT_CLASSIFIER` 空值回落、非法 fail-fast）；消费者任务在此托管。
- `src/weiliao_agent/health/probe.py` -- 就绪探针不纳入 Redis/Kafka 连通性（fail-closed 逐消息降级先例），消费者健康不挂 /health。
- `pyproject.toml` -- 主依赖 L9-18 与 dev 组 L20-28 均无 Kafka 客户端；pytest L40-47 `asyncio_mode=auto`、fakeredis[lua] 已备。
- `docker-compose.yml` -- 现有 redis/postgres/mysql/mongo/neo4j/qdrant/milvus 等，无 Kafka；新增 redpanda 单节点。
- `tests/test_l0_cache.py` -- fakeredis 直注（L41-46）+ `_FakeLangfuse` 替身（L111）测试先例。
- `README.md` -- L181/186 目录约定：adapters/=Kafka consumer/producer 外部服务适配、workers/=Kafka 消费者（房源事件同步、转人工指令）。

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- 主依赖新增 aiokafka -- 异步 Kafka 客户端（注意 mypy strict 兼容，必要时配最小化 type: ignore 并注释原因）
- [x] `docker-compose.yml` -- 新增 redpanda 单节点服务并创建 `house.sync.trigger` topic -- 本地开发 broker
- [x] `src/weiliao_agent/models/house_event.py` -- 新建 `EventType` Literal（house_upsert/house_invalidate）、`HouseSyncEvent` 模型（event_id、event_type、house_id、fact_version、occurred_at tz-aware；upsert 专属 status/price_yuan/school 可选/dialogue_summary 可选 ≤3；invalidate 仅需 house_id+fact_version）-- 事件契约唯一 schema
- [x] `tests/test_house_event.py` -- 事件模型边界单测：event_type 枚举拒绝、occurred_at 须带时区、upsert 缺 status/price 拒绝、dialogue_summary 上限、invalidate 形态 -- 防契约漂移
- [x] `src/weiliao_agent/observability/tracing.py` -- 新增 `record_house_sync_event` fail-safe 事件函数（operation 词表 consumed/stale_alert/corrupted/failure，仅后三者为 ERROR）-- 消费可观测
- [x] `src/weiliao_agent/workers/house_sync.py` -- 新建 `HouseSyncConsumer`：aiokafka 订阅、反序列化、超期判定（>500ms 转失效+告警）、upsert→`FactCard`+L0 set（to_thread）、invalidate→L0 invalidate（to_thread）、写成功后才提交 offset、毒消息跳过并提交、断连指数退避重连 -- 核心交付
- [x] `src/weiliao_agent/workers/__init__.py` -- 导出新符号 -- 包接口
- [x] `src/weiliao_agent/main.py` -- lifespan 托管消费者任务：`WEILIAO_KAFKA_BROKERS` 非空才启动（缺省不启动，测试与本地无 broker 零噪音）；复用 composition root 的 Redis 客户端构建 L0CacheClient；优雅关停 -- 进程生命周期
- [x] `tests/test_house_sync.py` -- 消费矩阵单测：消息源可注入（内存队列替身，不连真 Kafka）+ fakeredis L0 + FakeLangfuse；覆盖 I/O 矩阵全场景（含 offset 提交/不提交断言、超期转失效、毒消息、乱序重放、断连重连）-- AC 可验证

**Acceptance Criteria:**
- Given 合法 upsert 事件（fact_version=5），when 消费完成，then L0 各 intent get 返回 v5 卡片，且 Langfuse 事件记录 occurred_at→写完成延迟 ≤500ms
- Given 合法 invalidate 事件（fact_version=2，该 house 已有 v1 缓存），when 消费完成，then 该 house 全部 intent 读取返回 None 且记录 invalidated 事件
- Given `occurred_at` 超 500ms 的 upsert 事件，when 消费，then 事件内容不写入缓存、该 house 按失效处理且产生告警（ERROR 级）事件
- Given 非法 JSON 或 schema 校验失败的事件，when 消费，then 该消息被跳过、offset 仍提交、后续消息正常消费、记录 corrupted 事件
- Given 已缓存 v5 卡片，when 迟到的 v4 upsert 事件到达，then 写入被拒（复用 3.1 守卫）、get 仍返回 v5、offset 正常提交
- Given Redis 故障（L0 set/invalidate 返回 False），when 消费，then offset 不提交（事件可重放）且记录 ERROR 事件
- Given `WEILIAO_KAFKA_BROKERS` 未设置，when 服务启动，then API 正常就绪且消费者任务不创建
- Given 全部任务完成，when `pytest` 全量 + `mypy src` + `ruff check src tests`，then 全部通过且既有 165 个测试零回归

## Implementation Notes

（规划期留白；实现期 append-only 记录。）

2026-11-05 实现期注记：
- **守卫拒写 vs Redis 故障的两段诊断**（评审前自查发现的契约缺口）：
  L0 `set` 返回 False 不区分成因，而矩阵要求「守卫拒写 → offset 提交」
  「Redis 故障 → 不提交」。消费者先 `get` 裁决索引守卫（读到 ≥ 事件
  版本卡片即冲突提交）；`get` 为 None 时以 `invalidate(event 版本)` 作
  健康探针——墓碑守卫下现存墓碑版本必 ≥ 事件版本，探针幂等不重
  （生效版本取最大）；探针失败即 Redis 故障（不提交）；探针成功而重试
  `set` 仍被拒即确认守卫拒写（提交），重试同时兜住 Redis 瞬断恢复的
  补写窗口（metadata 记 `probe_retry`）。探针副作用仅限墓碑 TTL 刷新。
- **逐 record 精确提交**：aiokafka `commit()` 提交「已消费位置」会把
  已拉取未处理的消息一并标记，崩溃即丢 at-least-once 承诺；故
  `KafkaMessageSource` 按 `{TopicPartition: offset+1}` 单条提交。
- **lifespan 不改 HTTP 适配层**：`adapters/http.py` 冻结，main.py 以
  `app.router.lifespan_context = _lifespan` 覆盖式接入消费者任务托管。
- aiokafka 0.15.0 无 `py.typed`：import 处配最小化
  `type: ignore[import-untyped]`（mypy strict 兼容，spec 已预留口径）。
- 已知残余风险：Redis 故障发生在 upsert 多 intent 写入中途时，已写
  intent 与未写 intent 短暂不一致，重放由守卫逐 intent 收敛；探针路径
  可能在 Redis 恢复窗口内为「实际未写入」的事件提交 offset（重试补写
  已尽量收窄该窗口）——两者均不违反「永不错误返回旧卡片」优先级。

## Spec Change Log

（评审循环前为空。）

## Review Triage Log

2026-11-05 第 1 轮评审（盲审 Halley / 边界猎手 Dewey / 验证缺口 Einstein）：

| # | 发现 | verdict | 证据 | 路由 |
| 1 | consumed 事件 latency_ms 在 _handle_message 入口计算，非 L0 写完成时刻 | medium | 实测 house_sync.py：latency 于分发前计算并随 metadata 记录；AC 要求记录 occurred_at→写完成延迟 | patch（A） |
| 2 | redpanda 只广告 localhost:9092，init 容器经 redpanda:9092 引导后拿到 localhost 广告地址连不上自己；且 `|| true` 吞掉一切创建失败 | medium | compose 实测：--advertise-kafka-addr=PLAINTEXT://localhost:9092 与 init --brokers redpanda:9092 矛盾；|| true 无差别收敛 | patch（B） |
| 3 | Kafka tombstone 记录（value=None）使 json.loads 抛 TypeError 未被捕获 → 该消息永不提交、分区无限重试卡死 | medium | house_sync.py _deserialize 只捕 ValueError/UnicodeDecodeError；TypeError 上抛至 run 退避循环，违反矩阵「毒消息不阻塞」 | patch（C） |
| 4 | KafkaMessageSource 逐 record 提交逻辑零测试（缺口层预验证） | low | rg 实测：tests 仅 spy 替换，无 AIOKafkaConsumer 桩测试 | patch（D） |
| 5 | 墓碑探针诊断两分支（重试成功 probe_retry / 仍拒 version_conflict 提交）无测试（缺口层预验证） | low | rg probe_retry 于 tests 无命中；现有测试只覆盖索引守卫与探针失败 | patch（E） |
| 6 | lifespan 关停强退路径无测试，且 except Exception 吞掉消费者任务异常不记日志（缺口层预验证） | low | main.py 实测裸 except 无日志；现有关停测试只走即时停止路径 | patch（F） |
| 7 | 组合根 topic/group 默认值与 env 覆盖无断言（缺口层预验证） | low | _SpySource.kwargs 已记录但无人断言 | patch（G） |
| 8 | record_house_sync_event 的 operation 为裸 str，拼错 ERROR 词会静默降级 DEFAULT | low | tracing.py 实测 operation: str，level 判定依赖词汇表 | patch（H） |
| 9 | Message.commit docstring「L0 写成功后才允许调用」与毒消息跳过路径矛盾 | low | house_sync.py 实测：corrupted 路径无 L0 写也提交 | patch（I） |
| 10 | stale 告警先于 invalidate 发出，invalidate 失败重放时对同一 event_id 重复告警 | low | _handle_stale 实测：alert 在 to_thread invalidate 之前；Redis 故障时 offset 不提交必重放 | patch（J） |
| 11 | test_upsert 的 0<=latency<=500 断言依赖墙上时钟，高负载下可翻进 stale 路径 flaky | low | 测试 occurred_at=构造时刻，消费在毫秒后；CI 负载可超 500ms | patch（K） |
| 12 | aiokafka `>=0.15.0` 无上限，与仓库运行时依赖精确锁版本策略不一致 | low | pyproject 实测：redis/langfuse/cachetools 均 == 精确锁 | patch（L） |
| 13 | 三个 Kafka env vars 未文档化，spec 手工联调步骤未提设置 WEILIAO_KAFKA_BROKERS | low | README 无相关内容；不设变量则消费者不启动、联调无反应 | patch（M） |
| 14 | 模块 docstring「探针副作用仅限墓碑 TTL 刷新」不准确：无现存墓碑时探针会新建事件版本墓碑；探针路径「提交未写入事件」窗口属已记录残余风险 | low | house_sync.py 实测：探针即 invalidate，无墓碑时立新墓碑；提交窗口已在 Implementation Notes 声明接受 | patch（N，仅改 docstring） |
| 15 | sprint-status.yaml（in-progress）与 spec frontmatter（in-review）状态不一致 | low | 两文件实测不一致；工作流分步写入所致 | patch（O，sync 至 review） |
| 16 | docker-compose redpanda 服务无任何自动化校验 | low | spec 仅列手工检查；仓库无 CI；补测需新增 yaml 解析依赖 | defer |
| 17 | fact_version 可为布尔/负数 | low（reject） | 布尔半为 false：pydantic v2 的 int 拒绝 bool；负半为真但上游系内部可信生产者、日常不可达，且修复须加新约束属加护栏 | reject |
| 18 | house_id 可为空白字符串 | low（reject） | min_length=1 确实放行空白，但需上游先出契约 bug 才可达；修复须加 validator 护栏 | reject |
| 19 | 「结构化 stderr 日志」决策与 key=value 格式字符串实现之间有差距 | low（reject） | key=value 即仓库可接受的结构化口径（他处 logger 均为自由文本），配 formatter 超直接修正范围 | reject |
| 20 | spec 未记录 pytest/mypy/ruff 实际执行结果 | reject | 修复手段是改本 spec 文件，评审规则明令拒绝；验证结果由工作流向人类呈现 | reject |
| 21 | _FlakyL0 仅全操作失败配置，探针瞬断（set/get 正常、invalidate 失败）状态无假件 | low | 作为 #5 补测的设计上下文一并处理，不单独成行修复 | patch（E 附） |

## Design Notes

- 超期口径权威定义：延迟 = 消费处理时刻 − `occurred_at`，>500ms 即超期。超期事件不写入其内容（可能已被更新事件取代），一律按失效处理（invalidate + 事件 fact_version）+ 告警——「永不错误返回旧卡片」优先于命中率。
- offset 提交点在 L0 写成功之后：Redis 故障不提交 → at-least-once 重放由 fact_version 守卫幂等；解析失败则提交（毒消息不阻塞分区）。
- 消费为独立 asyncio task，与 API 路径零共享状态；Redis 客户端（redis-py 同步、线程安全）经 `asyncio.to_thread` 调用。
- 消费者启动门控：`WEILIAO_KAFKA_BROKERS` 非空才启动——本地开发与测试默认无 broker、零噪音，fail-safe 语义不变。
- 事件 `dialogue_summary` 对房源主数据属外来字段，契约中设为可选（缺省空列表），由上游有能力时携带。

## Verification

**Commands:**
- `pytest tests/test_house_event.py tests/test_house_sync.py` -- expected: 全绿
- `pytest` -- expected: 全量零回归（基线 2026-11-04：165 passed）
- `mypy src` -- expected: strict 无错误
- `ruff check src tests` -- expected: 无告警

**Manual checks:**
- `docker compose up redpanda` 后向 `house.sync.trigger` 发送示例事件（redpanda 控制台或 rpk），观察服务日志与 Langfuse 事件形态
