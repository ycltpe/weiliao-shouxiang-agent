---
title: 'Story 1.3 会话状态单写者管理'
type: 'feature'
created: '2026-10-31'
status: 'done'
route: 'dispatch'
baseline_commit: 'f299822301ab34f8891d7f43342912524b677c72'
review_loop_iteration: 0
context:
  - '_bmad-output/implementation-artifacts/epic-1-context.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-weiliao-shouxiang-agent-2026-10-22/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 信封已有 `session_id` 但会话状态不存在：`turn` 恒为 None，追问计数与转人工状态无处存取，多实例部署下会话一致性（FR-10/AD-4）无载体；信封时区/非空两项校验缺口（deferred-work）也悬置未决。

**Approach:** 在 `session/` 实现唯一写入者 `SessionManager`：Redis hash 单键 + Lua 原子脚本单 roundtrip 完成入管道前快照（初始化/自增/刷新 TTL），入口适配器在信封入管道前调用并回填 `envelope.turn`；Redis 权威、实例无状态；Redis 故障 fail-closed 走既有兜底。顺带结算 deferred-work 两项信封校验。

## Boundaries & Constraints

**Always:**
- 会话状态仅 `SessionManager` 写（AD-4 单写者）；过滤器只读并传 `session_id`，本 Story 过滤器零新增会话写。
- `turn` 为会话内绝对消息序号，入管道前赋值；TTL 30 分钟（可配置），每次入口刷新；过期后重新发起从零初始化（turn 从 1 重新开始）。
- 追问计数 Redis 原子自增，权威值只存 Redis（AD-4）；并发 `begin_turn` 下 turn 唯一。
- 过期竞态语义：以消息入管道时的读取结果为准，处理中途过期不中断在途消息（handoff 终态除外，属 Epic 5）。
- Redis 故障 fail-closed：兜底应答 + trace_id，HTTP 非 5xx（复用 1.2 既有兜底模式）。

**Never:**
- 不实现追问识别逻辑（Epic 2），本 Story 只提供原子计数接口与其并发正确性。
- 不实现 handoff 写入与转人工分支（Epic 5）；状态结构仅预留字段。
- 不实现意图历史/coref 的 per-turn 键写入（Epic 2/3）；接口约定见 Design Notes。
- 不引入 L1 进程内缓存（Epic 3）；不改信封字段口径（仅补校验器）。
- 不部署/运维 Redis；连接串经环境变量配置，代码不假设部署形态。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 首条消息 | 会话 key 不存在 | 创建状态，turn=1，TTL 1800s | N/A |
| 连续消息 | 会话存在 | turn 自增 +1，TTL 刷新，返回快照 | N/A |
| 并发入口 | 同 session 并发 begin_turn | turn 各得唯一值，无竞态 | N/A |
| 过期重开 | key 已被 TTL 清除 | 从零初始化 turn=1，无残留 | N/A |
| 计数并发 | N 线程 increment_followup | 计数精确等于 N（原子） | N/A |
| Redis 故障 | 连接拒绝/超时 | 入口兜底应答 + trace_id，HTTP 200 | 捕获 redis 异常 → 兜底 |
| 过期瞬间在途 | 入管道后 key 过期 | 按入管道快照继续，不中断 | N/A |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/session/__init__.py` -- Structural Seed 既有空包；本 Story 导出新 API
- `src/weiliao_agent/session/manager.py` -- 新建：`SessionManager` + `SessionSnapshot` -- AD-4 核心交付
- `src/weiliao_agent/models/envelope.py` -- 补校验器（timestamp 须带时区；session_id/house_id/trace_id 非空），结算 deferred-work；字段口径不变
- `src/weiliao_agent/adapters/http.py` -- `create_app` 注入 SessionManager；`post_message` 信封校验后先 `begin_turn` 回填 `envelope.turn`，Redis 异常走 `_fallback_result`（既有 fail-closed 模式）
- `src/weiliao_agent/main.py` -- 从 env `WEILIAO_REDIS_URL`（默认 `redis://127.0.0.1:6379/0`）装配
- `src/weiliao_agent/pipeline.py` -- 只读消费 `envelope.turn`；不改
- `tests/test_session.py` -- 新建：fakeredis 驱动状态机 + 并发原子性测试 -- AC 独立验收载体
- `tests/test_envelope.py` -- 补 naive datetime / 空标识符拒绝用例
- `tests/test_pipeline.py` -- 既有四场景不应回归（timestamp 均已带时区）
- `pyproject.toml` -- dev 组新增 fakeredis（Lua 支持）；运行时依赖不变

## Tasks & Acceptance

**Execution:**
- [x] `src/weiliao_agent/session/manager.py` -- `SessionSnapshot{turn, followup_count, handoff_status, last_message_at}` + `SessionManager`（redis 客户端注入；Lua 原子脚本：key 存在则 HINCRBY turn，否则初始化 turn=1/followup_count=0；每次入口 EXPIRE 1800 并刷新 last_message_at；`read_snapshot` 只读；`increment_followup` 原子自增并刷新 TTL）-- AD-4 单写者核心
- [x] `src/weiliao_agent/adapters/http.py` -- `create_app(orchestrator, session_manager=None)`；`post_message` 信封校验通过后先 `begin_turn` → `envelope.turn` 回填，再入管道；Redis 异常 → 兜底应答 + trace_id，非 5xx -- "入管道前赋值"字面落点
- [x] `src/weiliao_agent/main.py` -- 用 `WEILIAO_REDIS_URL` 装配 SessionManager（socket 超时配置须远小于 2.5s 内部预算）
- [x] `src/weiliao_agent/models/envelope.py` -- 两项校验器（timestamp tz-aware；三标识符 min_length=1）-- 结算 deferred-work 两条
- [x] `tests/test_session.py` -- fakeredis：首条/连续/过期重开/并发 turn 唯一/并发自增精确计数/端点层 Redis 故障兜底 -- AC 独立验收
- [x] `tests/test_envelope.py` -- 补拒绝 naive timestamp 与空标识符用例

**Acceptance Criteria:**
- Given Redis 可用且会话模块为唯一写入者，当任意消息入管道前调用 `begin_turn`，则信封获得会话内唯一绝对序号 `turn`，TTL 30 分钟被刷新
- Given 多实例并发处理同一 session 的消息，当并发 `begin_turn`/`increment_followup`，则 turn 唯一、追问计数精确，无竞态
- Given 会话 TTL 过期后买家重新发起对话，当 `begin_turn` 执行，则状态从零初始化（turn=1）且无残留字段
- Given Redis 连接失败或超时，当消息到达入口，则买家收到兜底应答且携带 trace_id，HTTP 非 5xx
- Given 会话过期瞬间有消息在管道中，当该消息处理中 key 过期，则以入管道时快照为准继续处理，不中断在途消息

### Review Findings

- [x] [Review][Patch] `read_snapshot` 对损坏 hash（turn 非数字 / last_message_at 非法 ISO）直接 `int()`/`fromisoformat` 抛 ValueError，"防御非本模块写入"承诺不完整，应 try 后返回 None [src/weiliao_agent/session/manager.py:378,384]
- [x] [Review][Patch] 构造器无 `ttl_seconds` 校验，`<=0` 时 Redis 立即删 key，turn 恒重置为 1、会话序号重复 [src/weiliao_agent/session/manager.py `__init__`]
- [x] [Review][Patch] `begin_turn` 公共 API 自身不校验 tz-aware（时区校验只在信封层），非 HTTP 调用方传入 naive datetime 会读回无偏移时间戳，跨跳比较崩溃/事件乱序 [src/weiliao_agent/session/manager.py]
- [x] [Review][Patch] 并发原子性（AC-2）仅在 fakeredis 全局串行下验证，真实 Redis 竞态回归不可检测；需补容器化集成测试（`pytest -m integration`），断言并发 begin_turn turn 集合恰为 1..N、increment_followup 计数精确 [tests/test_session.py:117,136]

## Implementation Notes

## Spec Change Log

## Review Triage Log

三层评审（blind-hunter / edge-case-hunter / verification-gap）共 23 项原始发现，逐条核验后记录如下。`route` 取值：patch / defer / reject。

| # | 发现（层 · 位置） | verdict | 证据 | route |
|---|---|---|---|---|
| 1 | Lua 以 turn==1 判新会话，increment-first 时静默清零 followup_count；partial hash 无防御（B1/B2/F1/F2 同根因） | medium | 实测复现：increment_followup 返回 1 后 begin_turn 快照 followup_count 变 0；接口本 Story 起即对 Epic 2 公开，静默数据丢失为真 | patch |
| 2 | http.py 兜底捕获裸 Exception，掩盖编程错误（B3） | medium | 确证：except Exception 会把 Lua 返回值解析 TypeError 等编程错误降级为兜底应答，缺陷不可见 | patch |
| 3 | session_manager=None 时信任 payload 自带 turn（F5） | low | 确证：None 分支不清除 envelope.turn，伪造 turn 可入管道；仅测试/未注入路径可达，生产 main.py 恒注入 | patch |
| 4 | 生产组合根 main.py 装配无测试（L1，预验证） | medium | 采信：删除 SessionManager 参数整套测试仍绿，turn 恒 None 无信号 | patch |
| 5 | increment_followup 的 TTL 刷新无断言（L3，预验证） | medium | 采信：删掉 Lua 中 EXPIRE 全绿 | patch |
| 6 | 回填测试 payload 均不带 turn，"尊重客户端 turn"回归不可见（L4，预验证） | medium | 采信：改为条件回填后现有测试仍通过 | patch |
| 7 | sprint-status 与 spec frontmatter 状态不一致（B4） | low | 确证：yaml in-progress vs frontmatter in-review，流程看板矛盾 | patch |
| 8 | WEILIAO_REDIS_URL 未文档化（B6） | low | 确证：grep 全仓无 README/.env 样例提及 | patch |
| 9 | 并发原子性仅在全局串行的 fakeredis 下验证，非原子实现同样过测（B10/L2 同根因） | medium（未对真实 Redis 验证） | 采信 fakeredis 2.40 全局锁源码证据；收口需真实 Redis 集成测试，与本 Story"不部署/运维 Redis"边界冲突 | defer |
| 10 | TTL"可配置"但 main.py 无 env 接线（B5） | low | 确证：配置面仅构造参数；修复需新增 env 面，超出直接修正 | reject |
| 11 | fail-closed 路径无日志/指标/可观测挂点（B7） | low | 确证：静默降级无信号；加日志/指标超出直接修正，且 Langfuse 追踪属 Story 1.5 | reject |
| 12 | min_length=1 放行纯空白标识符（B8/F6） | low | 确证：" " 可通过；但 spec 任务字面指定 min_length=1 结算 deferred-work"非空"，实现与 spec 一致，改口径需动 spec | reject |
| 13 | begin_turn 成功后管道异常的状态消耗语义未声明（B9） | false | begin_turn 成功即代表消息已受理，turn 消耗与兜底应答并存语义上可接受，无具名受害方 | reject |
| 14 | spec 三章节空置、决策错位至 Design Notes（B11） | low | 修复即编辑本 build 的 spec 文件，处置规则禁止 | reject |
| 15 | fakeredis[lua] 引入 lupa 膨胀 lockfile（B12） | low | Lua 单 roundtrip 原子性系 spec Design Notes 明定的验证方案，膨胀是固有代价，无具名受害 | reject |
| 16 | read_snapshot 遇畸形 last_message_at 崩（F3） | false | 所有写入方均为 SessionManager Lua（timestamp.isoformat()），畸形值需外部篡改 Redis，无可达路径；此时响亮失败属正确行为 | reject |
| 17 | WEILIAO_REDIS_URL 畸形时 import 崩且无诊断（F4） | false | from_url 抛 ValueError 自带 traceback 即诊断；启动期配置错误响亮失败属正确行为 | reject |
| 18 | ttl_seconds<=0 时 EXPIRE 运行期报错（F7） | false | 该参数仅组合根以默认值 1800 注入，无 env 接线，无可达路径；非法配置响亮失败属正确行为 | reject |

路由汇总：patch #1–#8（#1–#6 交还原实现子代理，#7–#8 本流程直接修正）；defer #9 → deferred-work.md；reject #10–#18。无 intent_gap / bad_spec，不触发回环。

## Design Notes

- Key 结构：`weiliao:session:{session_id}` Redis hash，字段 `turn`（int）、`followup_count`（int）、`handoff_status`（str，本 Story 恒 "none"）、`last_message_at`（ISO8601 字符串），TTL 1800s。与架构一致性约定的 per-turn 工件键（`...:intent:{turn}`、`...:coref:{turn}`）区分：后者由 Epic 2/3 经 SessionManager 写方法落盘，本 Story 只定归属、不定实现。
- 接口一次定准（Epic 2/3/5 直接消费）：`begin_turn(session_id, timestamp) -> SessionSnapshot`、`increment_followup(session_id) -> int`、`read_snapshot(session_id) -> SessionSnapshot | None`；未来的 `write_intent(turn, label)` / `write_coref(turn, result)` / `mark_handoff(...)` 同为 SessionManager 方法（本 Story 不实现，避免无调用方的死代码）。
- 原子性落点：Lua 脚本单 roundtrip（读旧态/初始化/turn 自增/刷新 last_message_at/EXPIRE 一次完成），满足延迟预算且并发安全；fakeredis（带 Lua 支持）验证脚本语义，无外部服务依赖。
- Redis 故障 fail-closed 依据：AD-4 禁止进程内缓存作判定依据 + 架构"实例无状态" + 1.2 错误兜底约定三者共同收口，无可行降级中间态。

## Verification

**Commands:**
- `uv sync` -- expected: lock 仅新增 fakeredis 开发依赖
- `uv run pytest` -- expected: 全绿（含并发与端点兜底用例）
- `uv run ruff check src tests` -- expected: 无告警
- `uv run mypy src` -- expected: strict 无错误
