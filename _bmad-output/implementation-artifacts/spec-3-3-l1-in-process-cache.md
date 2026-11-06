---
title: 'Story 3.3 L1 进程内缓存与分层查找顺序'
type: 'feature'
created: '2026-11-05'
status: 'done'
baseline_commit: '8ffbe9e657f16ca0903e25c912fa65fd723776f4'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** AD-5 规定两级缓存体系：L0 Redis 存事实卡片（Story 3.1 已交付）、L1 进程内缓存存会话级热点——但仓库现状是：无 L1 实现（`cache/__init__.py` 仅有 L0 导出）、`cache_lookup` 过滤器仍是 Story 1.2 的透传占位、L0 客户端无任何 API 路径调用方，「查找顺序 L1 → L0 → LLM」仅存在于文档。

**Approach:** 新建 `L1CacheClient`（cachetools.TTLCache 封装，TTL ≤30s，锁保护线程安全），仅存三类会话热点：最近意图、追问计数快照、handoff 状态快照；intent_router 经注入的方法引用把分类结果与追问计数写入 L1（fail-safe 静默）；cache_lookup 过滤器注入 L0CacheClient，实现事实卡片查找挂点（L0 命中把卡片挂到信封，miss 留空给 Epic 4 的 LLM 层）；handoff 快照提供「写入后立即作废」的失效 API，为 Epic 5 的 mark_handoff 预置契约。

**Decisions（2026-11-06 人类决策）:**
- L0 查找结果出 cache_lookup 的方式为**信封新增 `fact_card: FactCard | None = None` 字段**（Q1 选 A）：查找顺序对管道真实可观测，Epic 4 的 response_assembler 直接消费该字段决定「要不要 LLM 生成」，Story 3.4 的命中率口径亦基于该字段统计；接受信封模型随之演进的代价（Story 2.3 加 `is_followup` 字段有先例）。
- 规格体量约 3600 tokens、超 1600 建议线，人类选择**保留全量**：3.1/3.2 交付规格同体量，且 3.3 的验收要求 L1、查找顺序、handoff 失效语义整体可验，拆分将破坏验收闭环。

## Boundaries & Constraints

**Always:**
- L1 仅存三类会话热点（最近意图 / 追问计数快照 / handoff 状态快照），类型签名层面就拒绝 `FactCard`；事实应答数据只能经 L0 进入信封，查找顺序 L1 → L0 → LLM 由 cache_lookup 单一落点承载。
- 追问计数权威值只存 Redis（AD-4）；L1 快照仅作读优化；硬触发判定（Epic 5，AD-12）一律读 Redis 权威值、不得读 L1 快照。
- handoff 快照失效语义：handoff 写入后 L1 快照同步作废（`invalidate_handoff` 立即删除），转人工判定不得读到过期快照。
- L1 是优化层：任何 L1 异常不得影响买家应答（静默 fail-safe、不记降级）；实例无状态约束（NFR-8）不变——L1 内容是易逝优化副本，任意实例可处理任意消息，进程重启即清零。
- 过滤器间仅经信封传递；缓存查找热路径手写纯 Python，不依赖 LangGraph Runtime。

**Never:**
- 不实现 Follow-up 禁走 L0（Story 3.4 交付）；`is_followup` 不影响本 Story 的查找行为。
- 不改 `L0CacheClient` 的 Lua 守卫与 get/set/invalidate 行为（Story 3.1 冻结，只调用）。
- 不实现 LLM 链路与应答组装（Epic 4）；命中卡片只挂信封，买家可见应答仍是占位模板，不得改变。
- 不实现 `mark_handoff`（Epic 5）；本 Story 只交付 handoff 快照的读写 / 作废 API 与失效契约。
- 不在 L1 存 per-intent 计数、turn 序号、消息文本等任何会话权威数据；L1 读失败（miss / 过期）一律回落 Redis 权威值，调用方自行裁决。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 期望输出 / 行为 | 错误处理 |
|------|------------|----------------|----------|
| L1 写读最近意图 | 分类成功 tag=PRICE_INQUIRY | `get_recent_intent` 返回该 tag，TTL ≤30s | 写入异常静默吞，不影响应答 |
| L1 追问快照 | record_followup 返回会话总数 3 | `get_followup_count` 返回 3 | 同上静默 |
| L1 条目过期 | 条目存活超过 TTL | get 返回 None（调用方回落 Redis 权威值） | N/A |
| L1 容量打满 | 条目数超 maxsize | 最旧条目 LRU 逐出，客户端不抛异常 | N/A |
| handoff 作废 | 已写入 handoff 快照后调用 `invalidate_handoff` | 同 session 快照立即读不到 | N/A |
| cache_lookup 无 L0 | `CacheLookupFilter()` 无注入 | 逐字节透传（既有占位测试锁定） | N/A |
| cache_lookup 命中 | 注入 L0（fakeredis 预置卡片），intent/house_id 匹配 | 下游信封带 `fact_card` 卡片 | N/A |
| cache_lookup miss | L0 无匹配卡片 | `fact_card=None`（留给 LLM 层） | Redis 故障经 L0 客户端 fail-safe 视 miss |
| cache_lookup intent 缺失 | 信封 `intent=None` | 跳过 L0 查询直接透传 | N/A |
| L1 并发读写 | 多线程同时写 / 读不同 session | 无竞态损坏（锁保护） | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/cache/l1_cache.py` -- 新建。`cache/__init__.py` docstring 已预告「L1 cachetools 封装」但无任何实现。
- `src/weiliao_agent/cache/__init__.py` -- 导出新符号（对齐现有 L0 导出风格）。
- `src/weiliao_agent/cache/l0_client.py` -- `L0CacheClient.get(intent, house_id, trace_id=...)`（L197）为唯一需要的公开 API；Lua 守卫只调用不改。
- `src/weiliao_agent/filters/cache_lookup.py` -- Story 1.2 透传占位，本 Story 注入式扩展；既有测试锁定无参构造行为。
- `src/weiliao_agent/models/envelope.py` -- 信封模型；按已决策的 A 方案新增 `fact_card` 字段；tz-aware 校验风格沿 L67-75。
- `src/weiliao_agent/models/fact_card.py` -- `FactCard` 模型（字段权威 48-60 行），信封字段类型引用。
- `src/weiliao_agent/filters/intent_router.py` -- 两个 L1 写挂点：分类成功处（`classified` 产出后）与 `_record_followup` 成功返回处；`intent_writer` / `followup_recorder` 方法引用注入先例。
- `src/weiliao_agent/observability/tracing.py` -- `record_cache_event`（L417）operation 词表扩展点（新增 `miss`，DEFAULT 级）。
- `src/weiliao_agent/main.py` -- 组合根：env 直读模式（L53 区）；`L0CacheClient` 现仅在 lifespan 内为消费者构建，本 Story 需在模块级另建实例注入 cache_lookup；`IntentRouterFilter` 装配区加 L1 方法引用。
- `pyproject.toml` -- `cachetools==7.2.4` 已锁定（L16），本 Story 零新依赖。
- `tests/test_classifier.py` / `tests/test_intent.py` -- IntentRouterFilter 现有测试所在文件，追加 L1 写回调用例。
- `tests/test_l0_cache.py` -- fakeredis 直注 + `_FakeLangfuse` 替身测试先例（L41-46、L111）。

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/cache/l1_cache.py` -- 新建 `L1CacheClient`：三个类型化快照 API（recent_intent: IntentTag / followup_count: int / handoff_status: str，按 session_id 键控），`threading.Lock` 包裹全部读写（TTLCache 非线程安全，管道跑在 ThreadPoolExecutor），`timer` 可注入供测试假时钟，ttl_seconds 缺省 30、传 >30 一律 fail-fast（AD-5 硬上限），maxsize 缺省 4096 LRU 逐出 -- 核心交付
- [x] `src/weiliao_agent/cache/__init__.py` -- 导出 `L1CacheClient` -- 包接口
- [x] `src/weiliao_agent/models/envelope.py` -- 新增 `fact_card: FactCard | None = None` 字段及字段口径注释（已决策 A 方案）-- 查找结果的信封通道
- [x] `src/weiliao_agent/observability/tracing.py` -- `record_cache_event` operation 词表新增 `miss`（DEFAULT 级，仅 corrupted/failure 为 ERROR 的口径不变）-- 查找可观测、3.4 命中率口径前置
- [x] `src/weiliao_agent/filters/cache_lookup.py` -- 注入 `L0CacheClient | None = None`：无注入保持透传；intent 为 None 跳过查询；否则 L0 get，命中把卡片挂信封、miss 记 `miss` 事件，Redis 故障经 L0 客户端 fail-safe 视 miss 不抛 -- 分层查找落点
- [x] `src/weiliao_agent/filters/intent_router.py` -- 新增可选注入 `recent_intent_writer` / `followup_snapshot_writer` 方法引用：分类成功写最近意图、record_followup 成功写会话追问快照；均 try 包裹静默 fail-safe（不记 trace、不应答降级）-- AD-5 会话热点入 L1
- [x] `src/weiliao_agent/main.py` -- 组合根：`L1CacheClient`（`WEILIAO_L1_TTL_SECONDS` 缺省 30）方法引用注入 intent_router；模块级 `L0CacheClient(_redis_client)` 注入 cache_lookup -- 接线
- [x] `tests/test_l1_cache.py` -- 新建：I/O 矩阵 L1 侧全场景（TTL 过期用假时钟、容量逐出、作废语义、并发读写、ttl>30 fail-fast、类型拒绝）-- AC 可验证
- [x] `tests/test_cache_lookup.py` -- 新建：查找矩阵全场景（无注入透传、intent 缺失、命中、miss、Redis 故障替身、事件断言）-- AC 可验证
- [x] `tests/test_classifier.py`（或现有路由测试文件）-- 追加：L1 writer 注入时分类成功 / record_followup 成功各触发一次写入；writer 抛异常时应答与信封不变 -- 写路径可验证
- [x] `README.md` -- cache/ 目录行补 L1 说明 + `WEILIAO_L1_TTL_SECONDS` env 文档（对齐 3.2 补 Kafka env 的口径）-- 运维可见

**Acceptance Criteria:**
- Given L1 客户端 ttl=30s，when 写入三类会话快照，then TTL 内按类读取返回写入值，超过 TTL 一律 None（调用方回落 Redis 权威值）
- Given handoff 快照已写入，when `invalidate_handoff(session_id)`，then 该 session 快照立即读不到
- Given cache_lookup 已注入 L0 且存在匹配卡片，when 处理 intent/house_id 匹配的信封，then 下游信封带该卡片；L0 无卡片则 `fact_card=None`
- Given cache_lookup 未注入 L0，when 处理任意信封，then 逐字节透传（既有占位测试零回归）
- Given 分类成功且 turn 非 None，when intent_router 处理，then L1 最近意图快照更新为分类 tag；record_followup 成功后追问快照更新为会话总数
- Given L1 writer 抛异常（测试替身），when intent_router 处理，then 应答与信封结果不变、无 trace 失败事件（优化层静默）
- Given 全部任务完成，when `pytest` 全量 + `mypy src` + `ruff check src tests`，then 全部通过且既有 168 个测试零回归

## Implementation Notes

（规划期留白；实现期 append-only 记录。）

- 2026-11-06 实现完成：L1 三类快照经单缓存 `(kind, session_id)` 复合键 + 单把 `threading.Lock`；类型拒绝为运行时 fail-fast（IntentTag 枚举外值 / 非 int / 非 str 一律 TypeError），调用方 intent_router 经 `contextlib.suppress` 静默吞。fakeredis 2.40 已移除 `connected=False` 故障开关，Redis 故障用例改以 `mock.patch.object` 令 get 的 Lua 脚本抛 `redis.RedisError`（仍走 L0 客户端真实 fail-safe 分支）。miss 事件在 cache_lookup 侧统一记录（Redis 故障与纯 miss 同路径，failure 事件由 L0 客户端另记，二者成对）。基线测试数 206（spec 写作时 168，3.1/3.2 交付后增长），230 = 206 + 24 新增，零回归。

## Spec Change Log

- 2026-11-06 评审循环 1：记录实现口径与冻结文本的两处差异（规格文本未改动）——① 冻结 Boundaries「类型签名层面就拒绝 FactCard」落实为运行时 TypeError（_INTENT_TAGS 白名单 + isinstance 校验），因 Python 类型签名无法在运行期拦截；② 三类快照共用单一缓存、以 `(kind, session_id)` 复合键区分（规划期未明的实现选择，Implementation Notes 已记）。

## Review Triage Log

评审循环 1（2026-11-06，三层：blind-hunter 15 条 / edge-case-hunter 3 条 / verification-gap 1 条）：

| # | 来源 | 结论 | 证据 |
|---|------|------|------|
| 1 | blind-01 规格验收句「168 测试」过时 | reject | 修正需编辑本规格自身文本，工作流禁止以编辑本 build 的 spec 作为 finding 修复；真实基线 206/230 已记录于 Implementation Notes。呈报人类决策。 |
| 2 | blind-02 + edge-01 ttl_seconds ≤ 0 无守卫 | medium → patch（已修） | 已实测：TTLCache(ttl=0) 写入后 len=0、get=None——配置错误静默得到永不命中的死缓存。构造器加下界守卫。 |
| 3 | blind-03 + edge-03 main.py 裸 int(env) | low → patch（已修） | 非数字 WEILIAO_L1_TTL_SECONDS 在 import 期抛未命名 ValueError；修复须带变量名。负值由 #2 守卫兜底。 |
| 4 | blind-04 maxsize 无 env 旋钮 | false | 未演示实际伤害：LRU 逐出自管理，4096 满足矩阵；意图未要求可配置。 |
| 5 | blind-05 _INTENT_TAGS 手工重复 Literal | low → patch（已修） | IntentTag 是 Literal，typing.get_args 可消重；漂移时 mypy 放行、运行期拒绝、writer 静默吞——新增标签即静默丢快照。 |
| 6 | blind-06 bool 通过 followup_count 的 int 校验 | low → patch（已修） | isinstance(True, int) 为真，类型拒绝契约有洞；最小修复排除 bool。 |
| 7 | blind-07 Spec Change Log 空白 | low → patch（已修） | 补 append-only 一条，记录运行期拒绝机制与冻结文本「类型签名层面」的口径差异。 |
| 8 | blind-08 handoff API 无生产调用方 | reject | 冻结 Never 明确：mark_handoff 归 Epic 5，本 Story 只交付读写/作废 API 与失效契约；Design Notes 已记缺读者 rationale。 |
| 9 | blind-09 intent=None 路径无 miss 事件 | reject | 矩阵该行情形期望列为 N/A；命中率口径归 Story 3.4 规格决策（转达 3.4 规划）。 |
| 10 | blind-10 record_cache_event 未 fail-safe 包裹 | false | 函数体 create_event 已有 with suppress(Exception)（tracing.py record_cache_event 函数体），docstring 明示「绝不二次影响买家应答」——所述保证会破不成立。 |
| 11 | blind-11 _FakeLangfuse 复制进两个测试文件 | low → patch（已修） | 提升到 tests/conftest.py 共享，消除漂移面。 |
| 12 | blind-12 writer 负路径（UNKNOWN/记录失败）无测试 | false | 代码已查证：writer 调用点位于 UNKNOWN/turn=None/recorder 异常早退之后（intent_router.py _record_followup），跳过行为结构性保证；规格未要求该等测试。 |
| 13 | blind-13 并发测试「构造上不可能失败」 | reject | 单锁使竞态损坏不可能；矩阵行已由通过测试覆盖；加固超出直接修正。 |
| 14 | blind-14 常量钉住测试同义反复 | reject | 装饰性；钉住常量值属项目既有测试风格。 |
| 15 | blind-15 frontmatter 状态信号冲突 | reject（已消解） | 评审基于 in-progress 快照；状态评审前已置 in-review。 |
| 16 | edge-02 maxsize ≤ 0 未守卫 | medium → patch（已修，并入 #2 同根因） | 已实测：maxsize=0 首次写入即抛 cachetools 内部 ValueError「value too large」，破坏不抛约定且报错不可读。 |
| 17 | vgap-01 组合根接线零测试锁定 | high → patch（已修） | verification-gap 层预验证证据：撤销 CacheLookupFilter(l0_client=…) 与两个 writer kwarg 后全套件仍绿——Story 3.3 交付行为可在生产静默消失。补组合根冒烟测试（test_session.py 既有风格）。 |

## Design Notes

- **L1 读者为何在 3.3 缺位**：追问快照与 handoff 快照的消费方（硬触发评估 Epic 5、LLM 上下文 Epic 4）尚未交付，且 AD-12 明确硬触发判定禁读 L1 快照。本 Story 交付写入路径、查找落点与失效语义三类权威契约，读者随消费方就位后免费获得 L1 加速；不为凑「读者」发明无消费者的读路径。
- **L1 写失败为何静默且不记 trace**：`intent_writer` 记失败事件是因为那是 Redis 权威写；L1 是进程内优化层，写失败只可能是程序错误（如不可哈希键），记 trace 反而给热路径加噪音、且买家应答绝无受益。静默吞掉是刻意的非对称设计。
- **锁的粒度**：单把 `threading.Lock` 包全部读写。TTLCache 操作是 O(1)，热路径持锁时间可忽略；分片锁增加复杂度却无实测竞争依据（YAGNI）。
- **miss 事件为何现在加**：3.4 的命中率口径需要 hit/miss 成对可统计，且「查找顺序可验证」要求 miss 可观测；词表加一个 DEFAULT 级 operation 是最小改动。

## Verification

**Commands:**
- `uv run pytest -q` -- expected: 既有 168 测试零回归 + 新增测试全过
- `uv run mypy src` -- expected: 零错误（aiokafka 的 `type: ignore[import-untyped]` 先例口径不变）
- `uv run ruff check src tests` -- expected: 零告警
