---
title: 'Story 3.1 事实卡片 Schema 与 L0 缓存客户端'
type: 'feature'
created: '2026-11-03'
status: 'done'
baseline_commit: 'b440b19fabccd0df1db576b397ab9367a84a708f'
route: 'dispatch'
review_loop_iteration: 3
context:
  - '_bmad-output/implementation-artifacts/epic-3-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-weiliao-shouxiang-agent-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Epic 3 要建立事实卡片缓存体系，但当前 `cache/` 目录仅为空壳占位、无事实卡片模型：房源事实没有结构化、可失效的缓存单元，L0 Redis 缓存（Epic 3 全部故事的地基）不存在，「首次查询秒回」无落地前提。

**Approach:** 定义单一 Pydantic 事实卡片模型（`FactCard`），并实现 L0 Redis 缓存客户端：key 结构 `weiliao:l0:{intent}:{house_id}:{status}:{price_bucket}`、TTL 5 分钟、JSON 序列化；失效采用逻辑作废（墓碑标记，非物理删除），客户端暴露 invalidate API——房源状态变更的主动失效触发方（Kafka 消费者）在 Story 3.2 接入，本 Story 只交付机制与权威语义。

## Boundaries & Constraints

**Always:**
- key 结构、TTL 300 秒、JSON 序列化严格按验收标准；反序列化经 `model_validate`，未知/损坏数据视为缓存未命中。
- 失效为逻辑作废：已作废房源的读取永不错误返回旧卡片；墓碑与数据同 TTL 语义（5 分钟），随写入刷新。
- 写入原子性：索引 + 卡片值 + TTL 刷新经 Lua 脚本单 roundtrip 完成（对齐架构并发行约定与 1.3 会话管理器的 Lua 先例）。
- 读路径对 miss 返回 `None`（上层走 LLM 链路）；Redis 故障 fail-safe：不抛异常、get 视为 miss、set 静默失败、Langfuse 记失败事件，应答链路零影响。
- 热路径手写纯 Python，不依赖 LangGraph Runtime；缓存命中、逻辑作废、故障均记录 Langfuse 事件（trace_id 可贯穿）。
- 既有行为零回归：占位过滤器、管道默认链、信封全部不动；`pytest` 全量绿、`mypy src` strict 干净、`ruff check src tests` 干净。

**Never:**
- 不实现 Kafka 消费者与事件驱动的主动失效接线（Story 3.2）、L1 cachetools 进程内缓存（Story 3.3）、追问禁走缓存路由与命中率指标（Story 3.4）。
- 不改 `filters/cache_lookup.py` 占位行为、不动 `pipeline.py` 默认链与信封模型；本 Story 不在 `main.py` 接线（无生产调用方，接线随 3.2/3.4）。
- 不以物理删除作为失效手段；不在进程内保留任何缓存副本（AD-5）。
- 不直写数据库（AD-6）；不对 intent 做特判（UNKNOWN 是否可查缓存由上层路由决定）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 写入新卡片 | set(intent, FactCard) | 索引键 + 卡片键落盘，TTL 300s，返回成功 | Redis 错误：静默失败 + trace 失败事件，不抛 |
| 命中读取 | get(intent, house_id)，索引与值均在 | 返回等价 FactCard（经 model_validate） | 数据损坏/校验失败：视为 miss，返回 None + trace 事件 |
| 未命中 | get(intent, house_id)，无索引 | 返回 None | N/A |
| 逻辑作废后读 | invalidate(house_id) 后再 get | 返回 None；旧值仍在 Redis 可审计（未物理删除） | N/A |
| 新版本覆盖 | 同 (intent, house_id) 二次 set，fact_version 更高 | 索引指向新值，读取返回新卡片；旧 key 自然过期 | N/A |
| Redis 故障 | 连接/命令抛 RedisError | get 返回 None、set 静默跳过，业务不感知 | 捕获 RedisError，trace 记失败事件 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/models/intent.py` -- `IntentTag`（9 类 Literal），缓存 key `{intent}` 段取值来源；模型风格参照。
- `src/weiliao_agent/models/envelope.py` -- `Envelope` 的 tz-aware timestamp 校验风格，`FactCard.as_of` 复用同款约定。
- `src/weiliao_agent/models/__init__.py` -- 包导出入口，需新增导出。
- `src/weiliao_agent/session/manager.py` -- KEY_PREFIX 约定（`weiliao:session:`）、`model_dump_json` 序列化、Lua 原子脚本、TTL 常量模式，L0 客户端对齐这些先例。
- `src/weiliao_agent/cache/__init__.py` -- 现仅 docstring 占位，需导出 L0 客户端。
- `src/weiliao_agent/cache/l0_client.py` -- 新建：L0 缓存客户端。
- `src/weiliao_agent/main.py` -- Redis 客户端创建处（composition root，`from_url` + 0.5s 超时）；本 Story 只参照、不接线。
- `src/weiliao_agent/observability/tracing.py` -- `record_*` fail-safe 事件模式，缓存事件复用。
- `src/weiliao_agent/filters/cache_lookup.py` -- 占位过滤器，本 Story 不修改。
- `tests/test_session.py` -- fakeredis 注入 `SessionManager` 的测试先例。
- `pyproject.toml` -- mypy strict、ruff 规则（行宽 100、py311）。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/models/fact_card.py` -- 新建 `HouseStatus` Literal（active/sold/offline）、`FactCard` 模型（house_id、status、price_bucket、school 可选、dialogue_summary 最近 3 轮、fact_version int 单调递增、as_of tz-aware）与 `price_to_bucket` 辅助函数 -- 事实卡片为唯一 schema
- [x] `tests/test_fact_card.py` -- 模型边界单测：status 枚举拒绝非法值、school 可缺省、dialogue_summary 上限 3 轮、as_of 须带时区、bucket 计算 -- 防 schema 漂移
- [x] `src/weiliao_agent/cache/l0_client.py` -- 新建 `L0CacheClient`：get / set / invalidate 三 API，set 用 Lua 原子写「索引+值+TTL」且**不清墓碑**（跨 intent 复活修复）、get 用 Lua 单 roundtrip 原子读「墓碑+索引+卡片」且按版本比较语义返回、UTF-8 解码损坏归入 corrupted、fact_version 乐观失效双重守卫、invalidate 的 fact_version 必填（缺失即抛 ValueError，不得静默空操作）、墓碑逻辑作废、Redis 故障 fail-safe、Langfuse 事件（含 set 成功 write 事件、stage 词表、metadata 保留键防覆盖） -- 核心交付
- [x] `tests/test_l0_cache.py` -- fakeredis 单测覆盖 I/O 矩阵全部场景（含作废后读取、损坏数据、Redis 故障注入、乱序守卫（低版本 set/invalidate、同版本重放）、高版本卡片不被低版本作废遮蔽、跨 intent 写入不清墓碑（其他 intent 仍拦截）、跨房源墓碑隔离、未缓存 intent 负控、写路径 fail-open 三分支（损坏墓碑 set/invalidate、损坏现存卡片 guard-b）、读路径损坏墓碑不遮蔽、非法 UTF-8 字节、索引存活卡片过期、invalidate 默认时间戳路径与缺版本抛错） -- AC 可验证
- [x] `src/weiliao_agent/models/__init__.py`、`src/weiliao_agent/cache/__init__.py` -- 导出新符号 -- 包接口

**Acceptance Criteria:**
- Given fakeredis 注入的 `L0CacheClient`，when set 后 get 同一 (intent, house_id)，then 返回与写入等价的 `FactCard` 且两个键的 TTL 均为 300 秒
- Given 已执行 invalidate 的 house_id，when get 任意 intent，then 返回 None 且旧卡片值在 Redis 中仍可读取（逻辑作废非物理删除）
- Given Redis 客户端抛 `RedisError`，when get/set，then 方法不抛异常、get 返回 None、set 静默失败、Langfuse 记录失败事件
- Given 已以 fact_version=2 invalidate 的 house_id，when 迟到的 set 携带 fact_version=1，then 写入被拒绝且 get 仍返回 None（旧事件不得复活已作废房源）
- Given 索引已指向 fact_version=2 的卡片，when set 携带 fact_version=1 的卡片，then 索引不重指、get 仍返回 v2 卡片（旧事件不得回指旧事实）
- Given 已缓存 house A 且已 invalidate house B，when get house A，then 正常命中且不受他房墓碑影响（墓碑以 house_id 为粒度隔离）
- Given 已 set fact_version=3 的卡片，when 迟到的 invalidate(fact_version=2)，then get 仍返回 v3 卡片（作废不遮蔽更新事实）
- Given 已 invalidate(fact_version=2)，when 同版本 set(fact_version=2)，then 写入被拒且 get 返回 None（同版本重放不得撤销作废）
- Given 墓碑 payload 损坏（合法 JSON 但 fact_version 缺失/非数值），when set/invalidate，then 按「无版本信息」fail-open，不抛错、不阻塞
- Given 调用 invalidate 不传 invalidated_at，when 执行成功，then 墓碑 JSON 可解析且 fact_version 正确
- Given 多 intent 已缓存且 invalidate(fact_version=2)，when 单一 intent 写入 fact_version=3，then 其他 intent 的读取仍返回 None（墓碑不得被清）
- Given 调用 invalidate 不传 fact_version，when 调用，then 抛 ValueError 且不写任何 Redis 键
- Given 全部任务完成，when 运行 `pytest` 全量 + `mypy src` + `ruff check src tests`，then 全部通过且既有测试零回归

## Implementation Notes

当前设计状态综述（评审循环 4 补丁后，供 Story 3.2 直接引用）：
- **key 三结构**（单实例 Redis，Lua 多键脚本假定同槽位，无 cluster hash tag）：卡片键 `weiliao:l0:{intent}:{house_id}:{status}:{price_bucket}`（JSON，`model_dump_json` 口径）、索引键 `weiliao:l0:idx:{intent}:{house_id}`（存当前完整卡片键）、墓碑键 `weiliao:l0:tomb:{house_id}`（JSON：`fact_version` + `invalidated_at`）。全部 TTL 300s。
- **读路径**：单条 Lua 单 roundtrip 取「墓碑版本标记 + 卡片 payload」，Python 侧裁决——无墓碑或卡片版本 > 墓碑版本才返回卡片；墓碑标记为 `v:<n>` 哨兵字符串（Lua 数组前导 nil 塌缩与 cjson 非法 UTF-8 两大陷阱的规避），字节原样透传，解码损坏归 corrupted。
- **写路径 set**：单 Lua 原子「守卫 (a) 墓碑版本 ≥ 新卡片版本拒写；守卫 (b) 现存卡片版本 ≥ 新卡片版本不重指索引；通过则写卡片键 + 重指索引 + 双双 EXPIRE」；**不清墓碑**（跨 intent 复活修复，墓碑随 TTL 自然过期）。`tonumber` 一律 nil 检查，损坏按「无版本信息」fail-open。
- **作废 invalidate**：`fact_version` 必填（缺省抛 ValueError，快速失败优于静默空操作）；墓碑版本取现存与传入之最大者；Lua 返回生效版本入事件 metadata；解析生效版本失败（浮点版本）归 corrupted 仍返回 True。
- **损坏数据口径**：卡片侧（非法 UTF-8 / 非法 JSON / schema 不过）与墓碑侧（非法 UTF-8 / 非整数版本）一律 corrupted 事件（ERROR）+ 视为 miss，fail-safe 覆盖一切损坏数据，不限于 JSON 层。
- **事件 taxonomy**：operation ∈ hit / write / invalidated / corrupted / failure；stage ∈ set / get / invalidate 区分作废动作与读拦截；仅 corrupted / failure 为 ERROR；调用方 metadata 嵌套于独立子键，不可覆盖保留键。
- **fail-safe**：一切 `redis.RedisError` 不抛——get 视 miss、set/invalidate 静默返回 False + failure 事件（ERROR）。

## Spec Change Log

- **2026-11-03 评审循环 1 — 读路径原子性 + fact_version 乐观失效**
  - 触发发现：盲审#1/#3、边界#4/#5（get 三次独立往返，invalidate/set 可在读取中途插入，作废后竞态返回旧卡片，违背冻结区「永不错误返回旧卡片」）；盲审#2、验证缺口#2、边界#3（墓碑 payload 从不回读；乱序低版本 set 盲清墓碑复活旧事实、迟到的低版本 invalidate 拉低墓碑遮蔽新事实，epic 语境的「fact_version 乐观失效依据」未落地）。
  - 修订内容：Design Notes 读路径改为单条 Lua 单 roundtrip 原子读「墓碑→索引→卡片」；set Lua 写入前增加 fact_version 双重守卫（墓碑版本高于新卡片则拒写、现存卡片版本不低于新卡片则不重指索引）；invalidate 墓碑版本取「现存与传入之最大者」。Tasks 与 AC 同步补充乱序守卫、跨房源隔离、write 事件。
  - 避免的已知坏状态：作废竞态读旧卡片；迟到 Kafka 事件以旧 fact_version 复活已作废房源或回指旧事实。
  - KEEP 指令（再推导必须保留）：① key 三结构（索引/卡片/墓碑）、TTL 300 双双过期、JSON 序列化、墓碑逻辑作废（不物理删卡片值，旧值留 Redis 可审计）；② Redis 故障 fail-safe（RedisError 不抛、get 视 miss、set/invalidate 静默失败 + ERROR 事件）与 `record_cache_event` fail-safe 模式；③ 既有全部测试保留且全量绿；④ set 成功补记 `operation="write"` 事件（Story 3.2 写入路径可观测）；tracing docstring 口径修正为仅 `corrupted`/`failure` 为 ERROR；⑤ 新增测试：跨房源墓碑隔离、已缓存房源未缓存 intent 负控、乱序低版本 set/invalidate 守卫；⑥ `__init__` 导出与 `price_to_bucket` floor 口径不变；⑦ 纯 Python 热路径、不依赖 LangGraph Runtime。

- **2026-11-04 评审循环 2 — 乱序版本语义权威定义**
  - 触发发现：盲审#3、验证缺口#1/#other（组 G：乱序/同版本事件的版本比较语义未定义——同版本 set 重放可撤销作废；低版本/同版本 invalidate 经墓碑优先读遮蔽更新的活卡片，已实测演示）；盲审#1、边界#1/#2（Lua `tonumber` nil 比较错误，注释声称 fail-open 实际抛错，invalidate 可被整体丢弃）；盲审#6（`invalidated` 双语义、stage 词表未文档化）；盲审#10、边界#4（invalidate 丢弃 Lua 返回的生效版本）；验证缺口#2/#3（生产默认 `invalidated_at=None` 路径与三个 fail-open 分支无测试）。
  - 修订内容：Design Notes 增加乱序语义权威定义——墓碑版本 V 表示「fact_version ≤ V 的事实均已作废」；get 仅在「无墓碑或卡片版本 > 墓碑版本」时返回卡片；set 守卫 (a) 收紧为「墓碑版本 ≥ 新卡片版本即拒写」；三个写路径 Lua 的 tonumber 结果一律 nil 检查后按「无版本信息」fail-open；invalidate 事件 metadata 记录 Lua 返回的生效版本；`record_cache_event` docstring 列明 stage 词表（set/get/invalidate）。Tasks/AC 补充对应场景。
  - 避免的已知坏状态：同版本重放撤销作废；低版本作废遮蔽高版本新事实（命中率被无谓侵蚀）；损坏 payload 触发 Lua 错误致 invalidate 整体丢弃；事件版本 metadata 误导 3.2 观测。
  - KEEP 指令（再推导必须保留）：① 评审循环 1 全部 KEEP；② 三 Lua 结构（读/写/作废）与守卫 (b)（现存卡片版本不低于新卡片不重指索引）、invalidate max 守卫；③ 跨房源隔离、未缓存 intent 负控、乱序低版本守卫测试与事件 taxonomy（hit/write/invalidated/corrupted/failure）；④ fakeredis 测试风格与既有测试全保留；⑤ `__init__` 导出、key 结构、TTL 300、JSON 序列化、fail-safe 行为不变。

- **2026-11-04 评审循环 3 — 跨 intent 墓碑复活 + invalidate 无版本静默空操作**
  - 触发发现：盲审R3#1、边界R3#1/#C1（组 G：`_LUA_SET` 成功写入即 `DEL` 墓碑——单 intent 高版本写入清掉整房墓碑，其他 intent 的已作废旧卡片复活可命中；已实测复现：set(PRICE,v1)+set(AVAIL,v1)+invalidate(v2)+set(PRICE,v3) → get(AVAIL) 返回 v1  stale 卡）。盲审R3#1、边界R3#2/#C2（invalidate 的 fact_version 默认 None 写无版本墓碑，读路径 fail-open 不遮蔽——默认调用形态下作废返回 True 却形同虚设，两条 AC 被证伪）。
  - 修订内容：Design Notes 修订——set 守卫通过后**不再 DEL 墓碑**（墓碑随 TTL 自然过期；get 按版本比较，高版本卡片本就不被低版本墓碑遮蔽，DEL 既无必要又有害）；invalidate 的 fact_version 改为**必填**，缺失即抛 ValueError（静默无操作的作废比快速失败更危险，生产者缺陷须在 3.2 消费者侧暴露）；删除循环 1 遗留的「墓碑存在即返回 None」陈旧句（与循环 2 权威语义矛盾）；get 的 UTF-8 解码损坏归入 corrupted；`record_cache_event` 的 metadata 保留键（event/operation/stage）防调用方覆盖（嵌套或过滤）；补充 fail-open 三分支测试、读路径损坏墓碑不遮蔽测试、跨 intent 不清墓碑测试、invalidate 缺版本抛错测试、非法 UTF-8 测试、record_cache_event 直测。明确单实例 Redis 约束（key 结构冻结，无 cluster hash tag）。
  - 避免的已知坏状态：跨 intent 已作废事实复活（违背冻结区「永不错误返回旧卡片」）；默认形态 invalidate 静默失效；陈旧 Design Notes 误导后续再推导。
  - KEEP 指令（再推导必须保留）：① 循环 1/2 全部 KEEP 中未与本循环冲突的部分；② 三 Lua 单 roundtrip 结构、守卫 (b)、invalidate max 守卫、nil 检查 fail-open、生效版本入事件；③ 事件 taxonomy 与 stage 词表；④ fakeredis 测试风格与既有测试全保留；⑤ `__init__` 导出、key 三结构、TTL 300、JSON 序列化、fail-safe（RedisError 不抛）不变。

## Review Triage Log

| # | 来源 | 位置 | 结论 | 证据与路由 |
|---|------|------|------|-----------|
| 1 | 盲审#1 | l0_client.py get | medium | 实：EXISTS 墓碑、GET 索引、GET 卡片三次独立往返，invalidate 落在间隙即返回旧卡片，违背「永不」。根因在 Design Notes 的读路径设计（非冻结区）→ 组 A → bad_spec。 |
| 2 | 盲审#2 | l0_client.py invalidate/set | medium | 实：墓碑 payload（fact_version）从不回读，set 盲清墓碑；迟到的低版本事件可复活/遮蔽新事实。epic 语境明言 fact_version 为乐观失效依据。根因在 Design Notes → 组 B → bad_spec。 |
| 3 | 盲审#3 | l0_client.py get | medium | 实：热路径三次顺序往返，与 #1 同根因（非原子读设计）→ 组 A → bad_spec。 |
| 4 | 盲审#4 | l0_client.py set | low | 实：成功写入无 trace 事件，3.2 后写入路径不可观测。但冻结区 Always 仅枚举命中/逻辑作废/故障三类事件。最小修复平凡（补 write 事件）→ 组 D → patch，因 bad_spec 循环回退而并入 KEEP 指令④。 |
| 5 | 盲审#5 | l0_client.py __init__ | false | 已证伪：`redis.Redis.register_script` 仅本地构造 `Script` 对象（核实 redis-py 源码），无 SCRIPT LOAD 网络往返；失败注入测试也证明构造不触网。加载延迟到首次调用并走 NOSCRIPT 兜底。 |
| 6 | 盲审#6 | tracing.py record_cache_event | low | 实：docstring 称「后三类」ERROR，代码仅两类。docstring 修正即可 → 组 C → patch，并入 KEEP 指令④。 |
| 7 | 盲审#7 | sprint-status.yaml vs spec frontmatter | false | 非缺陷：spec 状态由本工作流在各步骤转换，sprint-status 在状态同步点跟随，当前差异是步骤间的瞬态。 |
| 8 | 盲审#8 | spec 空章节 | false | 非缺陷：Review Triage Log 由本步填写；无修订时 Change Log/Implementation Notes 为空属正常。 |
| 9 | 盲审#9 | tests/test_l0_cache.py | low | 实：缺跨房源墓碑隔离与未缓存 intent 负控，「墓碑以 house_id 为粒度」的设计决策无测试守护 → 组 E → patch，并入 KEEP 指令⑤。 |
| 10 | 盲审#10a | fact_card.py price_to_bucket | 拒绝（low） | 唯一消费路径经 FactCard 校验（price_bucket ge=0） loudly fail，未演示可达的坏结果；加守卫属额外复杂度。 |
| 11 | 盲审#10b | tests fixture teardown | 拒绝（low） | 当前无任何 fixture 组合注入 Langfuse client，无实际危害；既有测试均通过。 |
| 12 | 验证缺口#1 | tracing.py docstring | low | 与盲审#6 同 claim（ERROR 口径 miscount）→ 组 C。该层其余结论：无回归、无缺失采用、无破损验证缺口。 |
| 13 | 验证缺口#2 | l0_client.py set Lua | medium | 与盲审#2 同 claim（乱序 set-after-invalidate 复活旧卡片，无测试覆盖）→ 组 B。 |
| 14 | 边界#1 | fact_card.py price_to_bucket | 拒绝（low） | 同盲审#10a。 |
| 15 | 边界#2 | l0_client.py __init__ ttl_seconds<=0 | 拒绝（low） | 构造参数仅未来组合根（3.2）传入，未演示可达；默认 300 合法。 |
| 16 | 边界#3 | l0_client.py set | medium | 与盲审#2/验证缺口#2 同根因（低版本 set 回指旧事实）→ 组 B。 |
| 17 | 边界#4 | l0_client.py get | medium | 与盲审#1 同 claim（invalidate 落在墓碑检查后）→ 组 A。 |
| 18 | 边界#5 | l0_client.py get | medium | 与盲审#1 同根因（读路径非原子，并发 set 重指索引窗口）→ 组 A。 |
| 19 | 边界#6 | l0_client.py get | false | 已证伪：索引与卡片由同一 Lua 原子写入，代码内无任何路径产生跨房索引；未演示可达（需人工篡改 Redis）。 |
| 20 | 边界#7 | tracing.py record_cache_event | 拒绝（low） | 内部 API，仅 l0_client 以常量调用，operation 序列已被测试断言，拼写错误会被测试捕获；守卫属额外复杂度。 |
| 21 | 盲审R2#1、边界R2#1/#2 | _LUA_SET / _LUA_INVALIDATE | low | 实：`pcall(cjson.decode)` 未防护 `tonumber(t.fact_version)` 返回 nil 的比较错误（仅损坏 payload 可达，自有写入路径恒为数值）。最小修复平凡（nil 检查）→ 并入循环 2 修订（Design Notes 明文规定 nil 检查 fail-open）。 |
| 22 | 盲审R2#2 | 墓碑 TTL 窗口 | 拒绝（low） | 未演示可达：Kafka 消费延迟预算 ≤500ms（epic 语境）对墓碑 TTL 300s，窗口内闭环成立；超出预算的事件按 epic 语境「超期按失效处理并告警」归 3.2 管。 |
| 23 | 盲审R2#3、验证R2#other | set/invalidate 乱序语义 | medium | 实且已演示：同版本 set-after-invalidate 写入并清墓碑（守卫 (a) 严格大于）；invalidate(v≤卡片版本) 经墓碑优先读遮蔽活卡片。根因在 Design Notes 未定义版本比较语义 → 组 G → bad_spec，触发循环 2。 |
| 24 | 盲审R2#4 | set 被拒无事件 | 拒绝（low） | 守卫拒绝时 set 返回 False，Story 3.2 消费者侧可观测；spec 未要求拒绝事件，观测面归 3.2 接线时定。 |
| 25 | 盲审R2#5 | miss 事件缺失 | false | 已证伪：命中率口径分母为「全部首次消息」，可由消息 trace 计数推导，不依赖 miss 事件；冻结区 Always 明确枚举记录「命中、逻辑作废、故障」三类事件。 |
| 26 | 盲审R2#6 | record_cache_event stage 词表 | low | 实：`invalidated` 混用动作与读拦截两语义、stage 仅 metadata 携带且 docstring 未列词表。最小修复为文档化 → 并入循环 2 修订。 |
| 27 | 盲审R2#7 | sprint-status vs spec 状态 | false | 非缺陷：同循环 1 第 7 行——spec 状态由工作流步骤转换驱动，sprint-status 在同步点跟随，step-05 交付时统一。 |
| 28 | 盲审R2#8 | I/O 矩阵未补行 | false | 矩阵在冻结区内不可编辑（human-owned intent）；循环新增行为已由非冻结区 AC 行钉死，回归对照以 AC 为准。 |
| 29 | 盲审R2#9 | Verification/Implementation Notes 章节 | false | spec 模板即「命令 + 期望值」；实际执行结果由本工作流每轮亲自重跑验证（146 测试/mypy/ruff 全绿）并记录于会话。 |
| 30 | 盲审R2#10a、边界R2#4 | invalidate 事件版本 metadata | low | 实：Python 侧丢弃 Lua 返回的生效版本，事件记录传入版本，低版本 invalidate 后事件误导 3.2 观测。最小修复平凡 → 并入循环 2 修订。 |
| 31 | 盲审R2#10b | 索引存活卡片过期 / _decode_pair | 拒绝（low） | 索引存活卡片过期中途态补测并入循环 2 测试清单；`_decode_pair` 异常形状分支经 Redis 协议不可达（evalsha 返回恒为数组），防御代码不加测试。 |
| 32 | 盲审R2#10c、验证R2#3 | 写路径 fail-open 分支无测试 | low | 实：三个「损坏视为无版本信息」分支无测试守护 → 测试清单并入循环 2 修订（验证R2 自 filed 为 defer，因本循环已触碰同一 Lua 而就地关闭）。 |
| 33 | 边界R2#2 | ttl_seconds <= 0 | 拒绝（low） | carried：同第 15 行（同位置同 claim，代码仍旧）——构造参数仅组合根（3.2）传入，未演示可达。 |
| 34 | 验证R2#1 | 高版本 set 后低版本 invalidate 遮蔽 | medium | 同第 23 行组 G（已实测演示：set(v3)→invalidate(v2)→get=None）→ bad_spec。该层其余结论：无回归、无缺失采用、无破损验证缺口。 |
| 35 | 验证R2#2 | invalidate 默认时间戳路径无测试 | low | 实：生产调用形态（不传 invalidated_at）无测试，一行反转即 `None.isoformat()` 崩溃且不在 RedisError 捕获内 → 测试清单并入循环 2 修订。 |
| 36 | 盲审R3#1、边界R3#1、边界R3#C1 | _LUA_SET DEL tomb_key | high | 实且已实测复现：单 intent 高版本 set 清整房墓碑 → 其他 intent 已作废旧卡片复活可命中（set(PRICE,v1)+set(AVAIL,v1)+invalidate(v2)+set(PRICE,v3) → get(AVAIL)=v1 stale），违背冻结区「永不错误返回旧卡片」。根因在 Design Notes（循环 2 明文「清墓碑」）→ 组 G → bad_spec，触发循环 3。 |
| 37 | 盲审R3#2 | get/set 非 RedisError 异常逃逸 | 拒绝（low） | Redis 协议保证 evalsha 返回形状（数字/数组），调用方与本仓客户端均不产生畸形返回；未演示可达，加宽 catch 只会吞真 bug。 |
| 38 | 盲审R3#3 | Design Notes 陈旧句 | low | 实（循环 1「墓碑存在即返回 None」与循环 2 权威语义矛盾，误导再推导）→ 修正并入循环 3 修订。 |
| 39 | 盲审R3#4、验证R2#3 | fail-open 分支测试不全 | low | 实：guard-(b) 损坏现存卡片、读路径损坏墓碑两分支无测试 → 测试清单并入循环 3 修订。 |
| 40 | 盲审R3#5 | record_cache_event 保留键被 metadata 覆盖 | low | 实：`payload.update(metadata)` 允许调用方覆盖 event/operation；当前唯一调用方不触发，但属公共 API 契约缺陷 → 修复并入循环 3 修订（嵌套或过滤）。 |
| 41 | 盲审R3#6 | record_cache_event 无直测 | low | 实：level 映射 / trace_id=None 短路 / 异常吞掉契约仅靠间接口径 → 直测并入循环 3 测试清单。 |
| 42 | 盲审R3#7 | triage 第 5 行证据夸大 | low | 实：register_script 不触网的证据是源码核实（redis-py `Script` 延迟加载），非「失败注入测试证明」；证据表述修正，结论不变（false 维持）。 |
| 43 | 盲审R3#8 | sprint-status vs spec 状态不一致 | false | 同第 7/27 行：spec 状态由工作流步骤转换驱动（step-04 置 in-review，step-05 交付时同步 sprint-status 至 review/done），快照差异是流程瞬态。 |
| 44 | 盲审R3#9 | price_to_bucket 负价格 | 拒绝（low） | carried：同第 10/14 行（同 claim）。本次补充：函数已导出为公共 API，docstring 将写明「入参须为非负价格（元）」前置条件（并入循环 3 修订），数值守卫仍不加。 |
| 45 | 盲审R3#10 | Redis Cluster hash tag | 拒绝 | key 结构由冻结区验收标准钉死（`weiliao:l0:{intent}:{house_id}:{status}:{price_bucket}`），改动需人类重谈意向；单实例约束已在循环 3 Design Notes 明文。 |
| 46 | 盲审R3#11 | 测试访问私有成员 `_client` | low | 实：一例测试绕过 client fixture 直读 `l0._client`；最小修复为改用公共 fixture → 并入循环 3 测试清单。 |
| 47 | 盲审R3#12 | FactCard 字符串字段无 min_length | 拒绝（low） | schema 字段口径由冻结区/epic 语境钉死（未要求非空约束）；空串事实无害且未演示可达坏结果，加约束属超出意向的表面收紧。 |
| 48 | 边界R3#2、边界R3#C2 | invalidate 默认 fact_version=None | medium | 实：写无版本墓碑 → 读路径 fail-open 不遮蔽 → 默认调用形态作废返回 True 却形同虚设（已读代码证实空串版本路径）。根因是 spec 未钉死无版本语义 → 组 G → bad_spec。fact_version 改必填、缺省抛 ValueError。 |
| 49 | 边界R3#3 | 非法 UTF-8 字节逃逸 | low | 实：`bytes.decode()` 抛 UnicodeDecodeError 不在 RedisError 捕获内，破坏 fail-safe → 并入循环 3 修订（解码损坏归入 corrupted）。 |
| 50 | 边界R3#4 | fact_version 为负 | 拒绝（low） | 未演示可达：生产者为本仓 Kafka 消费者（3.2），事件版本恒为正；负版本守卫属额外复杂度。 |
| 51 | 边界R3#5 | ttl_seconds <= 0 | 拒绝（low） | carried：同第 15/33 行。 |
| 52 | 边界R3#6 | price_to_bucket 负价格 | 拒绝（low） | carried：同第 44 行。 |
| 53 | 盲审R4#1/#2/#3、边界R4#1/#C1 | _decode_pair 墓碑侧解码/解析 | medium | 实且已读代码确认：marker 的 UTF-8 解码与 `int(marker[2:])` 均无守卫——墓碑为非法字节或浮点版本（`{"fact_version": 2.5}` → marker `v:2.5`）时 `get` 抛 UnicodeDecodeError/ValueError 逃逸，违背冻结区 fail-safe。守卫状态已演示 → 最小修复平凡（try/except 归 corrupted + fail-open）→ 组 G4 → patch。盲审#3 死分支随修复自然可达。 |
| 54 | 边界R4#2 | invalidate 的 int(effective) 无守卫 | medium | 实：int() 在只捕获 RedisError 的 try 内，现存浮点版本墓碑令 invalidate 抛 ValueError → 组 G4 → patch。 |
| 55 | 边界R4#3 | 生效版本断言张冠李戴 | low | 实：`test_late_lower_version_invalidate_...` 在 get 后取 `_last_event`（hit 事件），invalidate 的生效版本断言靠巧合通过 → patch。 |
| 56 | 盲审R4#8 | guard-(b) pcall 非 JSON 分支无测试 | low | 实：现测试仅 `fact_version: null` 合法 JSON；`pcall(cjson.decode)` ok=false 形状无覆盖 → patch（参数化）。 |
| 57 | 盲审R4#4 | MAX_DIALOGUE_SUMMARY_ITEMS 死常量 | low | 实：常量定义与 Field 硬编码 3 并存可漂移 → patch（Field 引用常量）。 |
| 58 | 盲审R4#11 | fact_card.py docstring 断行缩进 | low | 实：「客户端归并」行悬挂缩进 → patch（重排）。 |
| 59 | 盲审R4#12 | happy-path 零 ERROR 断言缺失 | low | 实：corrupted/failure 的 ERROR 映射仅在失败测试中正向验证，无负向断言 → patch。 |
| 60 | 盲审R4#5 | record_cache_event 保留键覆盖 | false | 已证伪：循环 3 修订已实现嵌套（调用方 metadata 挂独立子键）并有直测（level 映射、保留键嵌套、trace_id 短路、SDK 异常吞掉），验证缺口层复核确认。 |
| 61 | 盲审R4#9 | invalidated 双语义无区分字段 | false | 已证伪：stage 词表（set/get/invalidate）已在每个事件的 metadata 中携带（代码 grep 确认），作废动作与读拦截靠 stage 区分。 |
| 62 | 盲审R4#10 | key 段无转义 | 拒绝（low） | key 结构由冻结区验收标准钉死（段间 `:` 分隔），转义改动需重谈意向；id 字符集由内部生产者（3.2 Kafka 消费者）控制，未演示可达。 |
| 63 | 盲审R4#6 | sprint-status vs spec 状态 | 流程修复 | 第 4 次出现，不再以瞬态驳回：立即将 sprint-status 的 3-1 同步为 review 与 spec 对齐。 |
| 64 | 盲审R4#7、#10 | Verification 无实测记录 / Implementation Notes 空 | 流程修复 | 接受：Verification 记录本轮实测结果；Implementation Notes 补当前设计状态综述（key 布局、守卫语义、fail-open 分支），供 3.2 直接引用。 |

## Design Notes

key 含 `status` 与 `price_bucket` 段，但读取方事先不知道这两值，因此引入二级索引键 `weiliao:l0:idx:{intent}:{house_id}` 存当前完整卡片 key。读路径原子性（评审循环 1 修订）：`get` 经单条 Lua 脚本单 roundtrip 完成「墓碑检查 → 索引解析 → 卡片取值」，一次返回墓碑版本与卡片 payload，Python 侧按循环 2 版本比较语义裁决（无索引或无卡片视为未命中）。读取全程无客户端往返间隙，invalidate/set 无法在读取中途插入，保证「已作废房源的读取永不错误返回旧卡片」。反序列化仍在 Python 侧经 `model_validate_json`，损坏视为 miss。

set 用 Lua 原子写「索引 + 值 + 双双 EXPIRE 300s」，写入前执行 fact_version 乐观失效双重守卫（评审循环 1 修订，epic 语境「fact_version 作为乐观失效依据」的落地）：(a) 墓碑存在且其 fact_version 不低于新卡片 → 拒绝写入（迟到的低版本事件不得复活、同版本重放不得撤销作废）；(b) 索引指向的现存卡片 fact_version 不低于新卡片 → 不重指索引（旧事件不得回指旧事实）。守卫通过才写卡片键、重指索引、双双 EXPIRE（评审循环 3 修订：**不清墓碑**——墓碑随 TTL 自然过期，get 按版本比较本就不遮蔽更高版本卡片，DEL 只会造成跨 intent 已作废事实复活）。invalidate 写墓碑键 `weiliao:l0:tomb:{house_id}`（存 fact_version 与作废时间，TTL 同 300s），墓碑版本取「现存墓碑版本与传入版本之最大者」——迟到的低版本 invalidate 不得拉低墓碑；**fact_version 必填**，调用方缺省即抛 ValueError（无版本墓碑在读路径 fail-open 下不遮蔽任何卡片，静默空操作的作废比快速失败更危险）。

乱序版本语义权威定义（评审循环 2 修订，根因修复）：**墓碑版本 V 的语义为「fact_version ≤ V 的事实均已作废」**。据此：
- get 返回卡片的条件为「无墓碑，或卡片 fact_version > 墓碑版本」；墓碑存在且卡片版本不高于墓碑版本时返回 None（作废不遮蔽更新事实，实测演示的遮蔽坏状态消灭）。冻结区矩阵「逻辑作废后读返回 None」以默认场景（作废版本 ≥ 卡片版本）为准，与「永不错误返回旧卡片」权威保证一致。
- set 守卫 (a) 收紧为「墓碑存在且墓碑版本 ≥ 新卡片版本即拒写」——同版本事件重放不得撤销作废。
- 三个写路径 Lua 中 `tonumber` 结果一律先作 nil 检查：解码出的 fact_version 缺失或非数值时按「无版本信息」fail-open（与脚本注释口径一致，不得触发 Lua 比较错误、不得令 invalidate 整体失败）。
- invalidate 的 trace 事件 metadata 记录 Lua 返回的实际生效版本（而非传入版本）。
- `record_cache_event` 的 docstring 列明 stage 词表（set / get / invalidate）。

健壮性补充（评审循环 3 修订）：get 的 Python 侧反序列化前，原始字节的 UTF-8 解码损坏（非法字节）归入 corrupted 视为 miss，不得逃逸异常（fail-safe 承诺覆盖一切损坏数据，不限于 JSON/schema 层）；`record_cache_event` 的调用方 metadata 不得覆盖保留键（event/operation/stage），实现采用嵌套（caller metadata 挂于独立子键）或等价过滤；部署约束为单实例 Redis（key 结构按冻结区验收标准固定，不含 cluster hash tag，Lua 多键脚本假定同槽位）。

`price_bucket` 为 int，定义 `price_to_bucket(price_yuan) = price_yuan // 100_000`（向下取整，例如 1,250,000 → 12）；bucket 入 key 前转字符串。`dialogue_summary: list[str]` 上限 3 条。失效以 house_id 为粒度（状态变更影响该房源全部 intent 的缓存）。

## Verification

**Commands:**
- `pytest tests/test_fact_card.py tests/test_l0_cache.py` -- expected: 全绿 -- actual (2026-11-04, 循环 4 补丁后): 51 passed
- `pytest` -- expected: 全量含既有测试零回归 -- actual (2026-11-04): 165 passed（41→51 新测试，既有 114 零回归）
- `mypy src` -- expected: strict 无错误 -- actual (2026-11-04): Success: no issues found in 28 source files
- `ruff check src tests` -- expected: 无告警 -- actual (2026-11-04): All checks passed!

### Review Findings

（2026-11-10 Epic 级 code review：blind-hunter 9 / edge-case-hunter 4 / verification-gap 6 / acceptance-auditor 2，共 21 项原始发现，合并去重并驳回 1 项后 18 项——decision-needed 3 / patch 10 / defer 4 / dismiss 1。本分录 2 项 patch + 2 项 defer；decision-needed 2 项见 spec-3-3，patch 5 项见 spec-3-2，decision 1 项 + patch 2 项 + defer 1 项见 spec-3-4。驳回 1 项：`record_cache_event` 的 `invalidated` 兼指「写作废动作」与「读拦截」——docstring 已自认双语义，属文档化已知项。）

- [x] [Review][Patch] 损坏墓碑读路径 fail-open 放行旧卡片、写路径 fail-close 拒新写入（medium；blind-hunter 层，已读码证实） [`src/weiliao_agent/cache/l0_client.py` `_decode_pair`] — 墓碑标记被写成浮点版本（手工修复/上游 bug，如 `v:2.5`）时：读路径 Python 侧 `int("2.5")` 抛 ValueError 被吞 → tomb_version=None → 照常返回旧卡片；写路径 Lua `tonumber` 得 2.5 → `2.5 >= 2` → 拒写。同一 Redis 状态两条路径裁决相反，形成「读得到但 300s 内写不进」的卡死窗口。**已修复（2026-11-11）**：读路径版本解析改为 float 与 Lua `tonumber` 对齐，浮点版本参与 `card.fact_version <= tomb_version` 比较（fail-close 遮蔽），`_decode_pair` 返回类型同步为 `float | None`。
- [x] [Review][Patch] `L0CacheClient` 构造器 `ttl_seconds` 无正数校验（low；edge-case-hunter 层，已读码证实） [`src/weiliao_agent/cache/l0_client.py` `__init__`] — 与 L1 构造期 fail-fast 不对称；`ttl_seconds=0` 时写入静默恒败 → 消费者永不提交 offset → 同一事件无限重放，故障形态为日志噪音而非启动 fail-fast。**已修复（2026-11-11）**：构造器加 `ttl_seconds <= 0` 抛 ValueError，与 L1 对称。
- [x] [Review][Defer] 缓存击穿：热 key 过期瞬间并发 miss 无 single-flight/按 key 互斥（low；edge-case-hunter 层） [`src/weiliao_agent/filters/cache_lookup.py:61-78`] — deferred，非本 Epic 规格要求（cache-aside 常规形态）；settle：Epic 4 LLM 链路接线后随压测评估 LLM 调用放大系数，若冲击 NFR-1 预算再引入请求合并。
- [x] [Review][Defer] 缓存雪崩：批量写入 key 共享同一过期时刻，无 TTL jitter（low；edge-case-hunter 层） [`src/weiliao_agent/cache/l0_client.py:136-137`] — deferred，与击穿条目同根因评估路径；settle：随压测观察周期性命中率塌缩，必要时对 TTL 加 ±10% jitter。
