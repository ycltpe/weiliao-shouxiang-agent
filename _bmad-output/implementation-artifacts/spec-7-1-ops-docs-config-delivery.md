---
title: 'Story 7.1 运维文档与配置交付'
type: 'feature'
created: '2026-11-10'
status: 'done'
route: 'dispatch'
baseline_commit: '93b2194fd80a6761036d63b605d450278df4288a'
review_loop_iteration: 1
context: ['{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 运维交付物三处缺口：(1) 配置失真——代码消费 23 个 WEILIAO_* 环境变量，.env.example 现有 18 个条目全部取值为空（os.environ.get 对空串不回落缺省，数值型/路径型直接启动 fail-fast），其中 3 个是代码不消费的失效变量（LLM_MODEL_ID/LLM_API_KEY/LLM_BASE_URL，代码实际走 OpenAI SDK 默认的 OPENAI_API_KEY/OPENAI_BASE_URL），另有 8 个消费变量缺失；(2) 无 runbook——/health 的 model_routing 快照与 ERROR 级 model_routing_alert 等告警无任何处置文档；(3) 代码无 .env 加载机制，干净 checkout 拷贝后无法直接启动。

**Approach:** 以组合根 main.py 为唯一事实源重建 .env.example（取消注释的条目全部非空、分组注释；对未配置更安全的变量以注释形态保留占位示例）并补齐 README 环境变量章节（说明/缺省值/调参影响，覆盖 WEILIAO_LLM_BREAKER_* 系列）；新建 docs/runbook.md 覆盖 metrics 通道告警的触发条件、判读与处置步骤；README 补启动指引与 compose 定位说明。

**决策（OQ1=B）：** 采用组合根启动时 dotenv 自动加载 `.env`（不覆盖已存在的进程环境变量），`uvicorn weiliao_agent.main:app` 裸命令即满足「cp .env.example .env 后直接启动」；python-dotenv 提为直接依赖。

**决策（G1-A，2026-11-10 审查回退）：** `.env.example` 的非空规则放宽为「取消注释的条目必须非空」；`OPENAI_BASE_URL`、`WEILIAO_RISK_REVIEW_ENDPOINT`、`WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH` 三个变量以注释形态保留占位示例——照抄 `.env` 后它们处于未配置态，走代码缺省（官方 API / 干净 fail-closed / 内置绝对路径词表），避免占位主机导致的熔断跳闸、审核超时噪音与异 cwd 启动失败。守护测试的「消费 ⊆ 声明」断言相应放宽为「消费 ⊆ 声明 ∪ 注释 opt-out 集」。

## Boundaries & Constraints

**Always:** 环境变量清单以代码（src/weiliao_agent/main.py 组合根及各模块 env 读取点）为唯一事实源；文档全部用中文；`cp .env.example .env` 后按 README 指引可直接启动成功。

**Never:** 不改动任何过滤器业务逻辑与既有超时/阈值参数值本身；不动 docker-compose.yml 数据服务定义；不写入任何真实密钥（示例值一律用占位符）；不触碰 Story 7.2 的执行器生命周期与压测内容；不修改用户实际的 .env（gitignored，不属于交付物）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 干净 checkout 启动 | cp .env.example .env 后按 README 命令启动 | 服务就绪，/health 返回 200 ready | 缺依赖服务时 README 指引先起 compose 数据服务 |
| 数值型变量为空串 | .env 中 WEILIAO_* 数值变量设为空 | 启动 fail-fast，错误消息带变量名（既有行为） | README 变量表注明"留空即启动失败，需删行恢复缺省" |
| 风控审核未配置 | WEILIAO_RISK_REVIEW_ENDPOINT 未设置 | fail-closed 回模板兜底（设计内安全行为） | .env.example 以注释形态保留占位示例（决策 G1-A），照抄后即未设置态 |
| Kafka 未配置 | WEILIAO_KAFKA_BROKERS 未设置 | 不启动消费者（设计内） | README 注明该变量缺省行为 |

</frozen-after-approval>

## Code Map

- `src/weiliao_agent/main.py:149-521` -- 组合根，全部 WEILIAO_* env 解析 + fail-fast 校验的唯一事实源；含 lifespan 指标定时循环（486-499）
- `src/weiliao_agent/llm/client.py:124` -- WEILIAO_LLM_MODEL 惰性读取点
- `src/weiliao_agent/classifiers/llm_zero_shot.py:87` -- WEILIAO_INTENT_MODEL 读取点
- `src/weiliao_agent/filters/risk_review/client.py:128` -- WEILIAO_RISK_REVIEW_ENDPOINT 兜底读取点
- `src/weiliao_agent/observability/metrics.py:443-496` -- ModelRoutingMetricsCollector.snapshot() 与 model_routing_alert 唯一发射点（ERROR 级 JSON 单行 stderr）；同通道另有 llm_breaker 告警（368-371）与 risk_review 审核率告警（247-260）
- `src/weiliao_agent/health/probe.py:32,70-73` -- /health 就绪判定与 model_routing 快照挂点
- `src/weiliao_agent/adapters/http.py:120-167` -- /health 端点实现，快照独立 fail-safe
- `README.md:36-103` -- 现有「环境变量」「开发」章节，待补全并新增启动/runbook 指引
- `.env.example` -- 现有 18 个条目取值全空：3 个失效名待删（LLM_MODEL_ID/LLM_API_KEY/LLM_BASE_URL），8 个代码消费变量待补
- `docker-compose.yml` -- 本地数据服务合集（redis/redpanda 等），应用本体不在其中，README 需说明这一定位
- `tests/` -- 既有测试布局；新增 .env.example 守护测试放此处

## Tasks & Acceptance

**Execution:**
- [x] `.env.example` -- 重写：保留现有注释口径（各条目已带 Story 出处/缺省/语义），删除 3 个失效变量（改注 OPENAI_API_KEY 为凭据变量，SDK 默认读取；OPENAI_BASE_URL 按 G1-A 注释占位），补齐 8 个缺失变量（WEILIAO_LLM_MODEL、WEILIAO_INTENT_CLASSIFIER、WEILIAO_INTENT_MODEL、WEILIAO_L1_TTL_SECONDS、WEILIAO_METRICS_LOG_SECONDS、WEILIAO_KAFKA_BROKERS、WEILIAO_KAFKA_TOPIC、WEILIAO_KAFKA_GROUP_ID）；取消注释条目全部非空；WEILIAO_RISK_REVIEW_ENDPOINT 与 WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH 按 G1-A 注释占位 -- 修配置失真，AC3 前置
- [x] `src/weiliao_agent/main.py` + `pyproject.toml` -- 组合根启动早期 dotenv 加载 .env：显式锚定项目根路径 `load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)`（审查 G2：无参 find_dotenv 在安装/异 cwd/父目录 stray .env 场景静默失效），.env 不存在时静默跳过；python-dotenv 按锁内版本精确 pin（==1.2.2）并更新 uv.lock -- OQ1=B 决策落地，AC3 核心
- [x] `README.md` -- 「环境变量」章节扩为全量分组表格（变量/缺省/说明/调参影响，含 WEILIAO_LLM_BREAKER_* 与 WEILIAO_LLM_ROUTING_* 系列）；新增启动指引（compose 数据服务 + `uvicorn weiliao_agent.main:app`，注明 .env 由服务自动加载、须从仓库根目录启动）与 runbook 链接；快速启动把「替换 OPENAI_API_KEY 占位符」列为显式必需步骤（审查 B4：凭据惰性读取，就绪不代表可用）-- NFR9 AC1
- [x] `docs/runbook.md` -- 新建运维手册：metrics stderr JSON 通道说明、三类告警（model_routing_alert=tightened 收紧、llm_breaker 非 closed、risk_review_alert 审核率跌破阈值）各自的触发条件、/health 快照字段判读、curl/grep 验证示例与处置步骤；日志抓取示例补部署形态说明（审查 B16：docker logs 为容器假设，给出本地前台进程等价命令）-- AC2
- [x] `tests/test_env_example.py` -- 守护测试（审查 G4+G5 加固）：用 python-dotenv 的 dotenv_values 解析样例（B14）；引号字面量 + 未加引号形态双扫描消费点（B5/E4）；反向死变量检测——声明的 WEILIAO_* ⊆ 消费集 ∪ G1-A 注释 opt-out 集（B5）；重复条目检测（E5）；子进程超时诊断（E6）；数值变量拒 inf/nan 并按 README 文档边界校验（B6）；空串 fail-fast 用例参数化全部数值变量（B7）；README 环境变量表覆盖断言（B15）；新增 .env 文件加载 sentinel 用例——临时以 sentinel 内容写仓库根 .env（备份/恢复放 finally），子进程裸导入断言文件取值生效（V1，旧测试只注入进程 env、绕过 dotenv 文件加载路径）
- [x] `tests/conftest.py` -- 收集早期 scrub 进程 env 中全部 WEILIAO_*（审查 G3/B10/V2），消除导入期 load_dotenv 导致的本机 .env 与 CI 行为分叉；不得影响显式 monkeypatch.setenv 的用例

**Acceptance Criteria:**
- Given 全部 env 变量（含 4.4/4.5/4.6 引入的 WEILIAO_LLM_BREAKER_* 系列），when 查阅 README，then 每个变量有说明、缺省值与调参影响（NFR9）
- Given /health 的 model_routing 快照与 ERROR 级 model_routing_alert 告警，when 告警触发，then runbook 中有对应处置步骤可查（触发条件、判读字段、处置动作）
- Given 干净 checkout 的仓库，when `cp .env.example .env` 后按 README 指引启动，then 服务直接启动成功且无空值导致的启动失败
- Given 守护测试落地，when `uv run pytest`，then 全绿且 .env.example 与代码变量集合一致性有测试背书

## Implementation Notes

- WEILIAO_KAFKA_BROKERS 样例取值为 `localhost:9092`（对应 compose redpanda），
  无 broker 时启动仍就绪（消费者后台重试、发送器启动失败禁用，均为既有
  fail-safe 行为，冒烟验证确认 /health 200）；README 注明删行即零 broker。
- WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH 样例为相对路径（`src/weiliao_agent/handoff/negative_words.txt`，
  以启动 cwd 解析）；README 快速启动指引要求仓库根目录启动，缺省行为
  （删行回落内置绝对路径）不变。
- 守护测试的代码消费集合经「引号内 WEILIAO_* 字面量」扫描（覆盖
  os.environ.get 读点与 _ENDPOINT_ENV 等模块级常量；docstring 反引号
  形态与 .txt 词表天然排除），已确认 23 个消费变量 ⊆ .env.example 25 个条目。
- 矩阵测试审计补两例子进程测试（实现子代理只做了手动冒烟，未注册自动化用例）：
  `test_clean_checkout_import_with_example_env`（干净启动行）与
  `test_blank_numeric_var_fails_fast_with_var_name`（空串 fail-fast 行），
  以 .env.example 取值注入子进程导入组合根，与「cp 后 dotenv 加载」同口径。
- 主代理亲验：临时以 .env.example 覆盖 .env 后裸跑 `uvicorn
  weiliao_agent.main:app --port 8123`，/health 返回 200 ready（四快照齐全），
  随后原 .env 已恢复（md5 校验一致）。
- 再推导（round 2，审查回退 #1 后）：G1-A 落实为 OPENAI_BASE_URL /
  WEILIAO_RISK_REVIEW_ENDPOINT / WEILIAO_HANDOFF_NEGATIVE_WORDS_PATH 三行
  注释占位；load_dotenv 显式锚定 `Path(__file__).resolve().parents[2] /
  ".env"`；python-dotenv==1.2.2 精确 pin；tests/conftest.py 收集早期 scrub
  WEILIAO_*；守护测试扩至 16 函数（参数化后 23 用例），含
  test_dotenv_file_loading_sentinel（备份/恢复仓库 .env，finally 兜底）。
  主代理亲验：守护 23 passed、全量 817 passed、ruff/mypy 干净、裸启动冒烟
  /health 200（端口 8124，.env 恢复 md5 一致）。Ampere 提示的残余风险：
  sentinel/conftest 时序依赖 pytest conftest-first 保证；runbook 的 curl
  网关检查假定运维已替换真实 endpoint（文中已注明）。
- 评审补丁轮（round 2 审查后，回派 Ampere 一处修复 11 项）：sentinel 用例改
  _temporary_repo_env（有真实 .env 即 skip）；conftest 三层处置（快照→scrub→
  autouse 重 scrub→session 恢复）；override=False 优先级变体用例；AC3 服务
  半边自动化（TestClient lifespan + /health 四快照断言）；int 型 is_integer
  校验；正数必需变量左开区间；tokenize 剥注释后双扫描；字符串 fail-fast 变量
  参数化；runbook 补 handoff_command_alert 章（含 redrive 处置）与覆盖守卫；
  README 补重启生效规则与占位密钥症状注记；DOCUMENTED_BOUNDS 镜像注释。
  主代理终验：守护 28 passed + 2 skipped（设计内）、全量 822 passed +
  2 skipped、ruff 干净、mypy 干净（conftest:71 存量 note 与本次无关）。

## Spec Change Log

- 2026-11-10 审查回退 #1（intent_gap G1）：触发发现 B2/B3/B11/E3——冻结块「全量变量、
  非空值」与矩阵行「填占位示例值」迫使 OPENAI_BASE_URL / RISK_REVIEW_ENDPOINT /
  NEGATIVE_WORDS_PATH 以活动占位值出货，照抄 .env 后分别导致 SDK 指向不存在主机
  （熔断跳闸 + llm_breaker_alert 洪泛）、每次草稿 500ms 审核超时 + 样本 ≥20 后
  risk_review_alert 常亮、异 cwd 启动 fail-fast 误导。已按人类决策 G1-A 修正冻结块：
  非空规则放宽为「取消注释条目非空」，三变量注释形态占位，矩阵行同步，守卫断言
  放宽为「消费 ⊆ 声明 ∪ 注释 opt-out 集」。审查 patch 组 G2-G6 已并入 Tasks。
  KEEP（回退前实现中经审查认可、再推导必须保留）：.env.example 分组注释结构与
  Story 出处口径；README 全量分组变量表（23 变量 + 凭据）的内容与表述；runbook
  三章结构与 metrics.py/probe.py 逐条核对结论；守护测试 12 用例与子进程冒烟口径
  （另按 V1 补文件加载用例）；load_dotenv(override=False) 的位置（B8 已证伪，
  子模块 env 读取全在构造器内，无需上移）；uv.lock 与 pyproject 的依赖更新方式。
- 2026-11-10：Story 7.1 实现完成。交付 .env.example 全量重写（23 个消费变量
  + OPENAI_API_KEY/OPENAI_BASE_URL 占位条目，取值全非空）、main.py 组合根
  dotenv 加载（OQ1-B）、README 快速启动 + 全量分组变量表、docs/runbook.md
  三类告警处置手册、tests/test_env_example.py 守护测试（12 用例）。
  冻结 Intent 节零改动。

## Review Triage Log

第 1 轮审查（2026-11-10，三层：blind 16 条 / edge 6 条 / verification-gap 1+1 条）。
分组与路由：**G1 intent_gap**（B2/B3/B11/E3，最高 medium）→ 回退代码后问人；
**G2-G6 patch**（load 路径锚定、测试 hermeticity、.env 文件加载用例、守卫测试加固、
杂项小修）；false：B1/B8/B13。patch 组在 G1 解决后的再推导中一并落实。

| # | 层 | 位置 | verdict | 证据 |
|---|----|------|---------|------|
| B1 | blind | uv.lock | false | uv.lock 已更新（git diff 可见 python-dotenv 入 weiliao-agent 依赖与 requires-dist）；仅因审查 diff 组合时排除该文件而未呈现 |
| B2 | blind | .env.example OPENAI_BASE_URL | medium | 活动占位指向不存在主机，SDK 静默指向坏地址 → G1（frozen「非空值」规则根因） |
| B3 | blind | .env.example RISK_REVIEW_ENDPOINT | medium | fail-closed 语义下占位=每次草稿 500ms 超时 + 样本 ≥20 后 risk_review_alert 常亮 → G1 |
| B4 | blind | README 快速启动 | low | 未把替换 OPENAI_API_KEY 列为必需步骤（凭据惰性读取，/health 就绪不代表可用）→ G6 patch |
| B5 | blind | tests/test_env_example.py | low | 守卫扫描仅引号字面量、无死变量反向检测 → G5 patch |
| B6 | blind | tests/test_env_example.py | low | 数值测试不拒 inf、不校验文档边界 → G5 patch |
| B7 | blind | tests/test_env_example.py | low | 空串 fail-fast 仅测 L1_TTL 一个变量，应参数化 → G5 patch |
| B8 | blind | main.py load_dotenv 位置 | false | 子模块 env 读取全在构造器内（llm/client.py:124、llm_zero_shot.py:87、risk_review/client.py:128），无模块级读取点先于 load_dotenv 执行；当前位置在全部组合根读取之前，功能正确 |
| B9 | blind | main.py:152 load_dotenv | medium | 无显式路径：源码 checkout 内可靠，安装到 site-packages 或异 cwd 场景静默失效 → G2 patch |
| B10 | blind | main.py 导入副作用 | medium | 导入期 load_dotenv 写 os.environ，进程内测试套件耦合本机 .env（CI 分叉、先 cp 后跑测试即触发）→ G3 patch |
| B11 | blind | .env.example NEGATIVE_WORDS_PATH | medium | 样例为 cwd 相对路径，异 cwd 启动 fail-fast 且报错误导 → G1（同根：frozen 非空值规则） |
| B12 | blind | pyproject.toml | low | python-dotenv 用 >= 违反全仓 == 惯例；锁内版本 1.2.2 → G6 patch |
| B13 | blind | sprint-status.yaml | false | 两套台账由工作流分步维护：sprint-status 由 step-03 sync-sprint-status 置 in-progress，spec 生命周期状态按 build 步骤推进，非缺陷 |
| B14 | blind | tests _parse_env_example | low | 手写解析与 dotenv 语义分歧（行内注释场景假阳/假阴）→ G5 patch（改用 dotenv_values） |
| B15 | blind | README 环境变量表 | low | .env.example↔源码一致性有守卫，README 漂移无守卫 → G5 patch |
| B16 | blind | docs/runbook.md | low | docker logs 示例假设容器部署，与仓库实际形态（compose 仅数据服务、无 Dockerfile）不符 → G6 patch（加部署形态说明） |
| E1 | edge | main.py:152 | medium | site-packages 安装场景 cwd 旁 .env 找不到 → G2 patch（与 B9 同根因） |
| E2 | edge | main.py:152 | medium | 无项目根 .env 时 find_dotenv 向上可能命中父目录（如 $HOME）stray .env → G2 patch（显式锚定同解） |
| E3 | edge | .env.example:46 | medium | 照抄副本把占位 endpoint 当已配置 → G1（与 B3 同） |
| E4 | edge | tests:40 | low | f-string/拼接构造 env 名逃逸引号扫描 → G5 patch（补未加引号扫描） |
| E5 | edge | tests:43-52 | low | .env.example 重复条目无检测 → G5 patch |
| E6 | edge | tests:120 | low | 子进程超时无诊断信息 → G5 patch |
| V1 | vgap | main.py:152 | medium | 采信（该层预验证）：.env 文件加载路径无自动化背书——子进程用例注入的是进程 env，绕过了 dotenv 文件加载；删掉/移位该行全量测试仍绿 → G4 patch |
| V2 | vgap | main.py 导入副作用 | medium | 同 B10：本机 .env 经 load_dotenv 进程级常驻，importlib.reload 组合根测试读到本机值 → G3 patch |

第 2 轮审查（2026-11-10，round-2 再推导后：blind 14 / edge 7 / vgap 2+1）。
路由：**全部幸存者为 patch**（G-scrub 测试 hermeticity 修正、G-tests 守卫加固、
G-vgap 优先级与 AC3 服务半边自动化、G-docs 文档补记）；false：R2-B1（carried B1）、
R2-B3（carried B13）、R2-B5、R2-B13；拒绝的 low：R2-B6、E4、E5（理由见各行）。

| # | 层 | 位置 | verdict | 证据 |
|---|----|------|---------|------|
| R2-B1 | blind | uv.lock | false（carried B1） | 工作树 uv.lock 已更新（git diff 可见 python-dotenv==1.2.2 入依赖与 requires-dist）；仅审查 diff 组合排除该文件 |
| R2-B2 | blind | spec Verification 节 | low | round-1 数字（14/808）与 round-2 Implementation Notes（23/817）并存过时——agent-owned 记账滞后 → G-docs patch（主代理自行更新，不回派实现代理） |
| R2-B3 | blind | sprint-status.yaml | false（carried B13） | 两套台账由工作流分步维护；任务勾选与 spec 状态属 build 生命周期，sprint 流转在收口步处理 |
| R2-B4 | blind | .env.example OPENAI_API_KEY | low | 占位密钥确会 401→熔断跳闸，但注释掉无意义（无密钥同样失败），且 G1-A 枚举的三变量不含它；最小修复=README/runbook 补症状注记 → G-docs patch |
| R2-B5 | blind | .env.example KAFKA_BROKERS | false | 示例值与 README 快速启动第 3 步（compose redpanda，9092）一致，非死占位；跳过 compose 的场景 README 已注明删行；无 redline 流量则 DLQ 告警不触发 |
| R2-B6 | blind | main.py 导入副作用 | low（拒绝） | 修复需把全部模块级 env 读取重构进 lifespan/factory，远超最小修复；仓库内无非服务器 main 导入方，组件复用走子模块不触发 |
| R2-B7 | blind | tests sentinel | medium | 备份仅存内存、finally 不防 SIGKILL/断电——测试中途崩溃会毁掉开发者真实 .env → G-tests patch（.env 已存在则 skip，CI 无 .env 照跑） |
| R2-B8 | blind | tests/conftest.py | medium | scrub 无差别删 WEILIAO_*（连运维/CI 刻意导出的一起删）且无 opt-out → G-scrub |
| R2-B9 | blind | docs/runbook.md | low | handoff_command_alert（ERROR 级、发送器通道）无处置章，runbook 覆盖断言也不含它 → G-docs patch |
| R2-B10 | blind | README | low | 未写「改 .env 须重启进程生效」（load_dotenv 仅导入期一次；--reload 只监听代码）→ G-docs patch |
| R2-B11 | blind | tests 空串参数化 | low | 仅数值型；文档明载 strip-empty fail-fast 的字符串变量（MINI_MODEL/两个 topic/REDLINE）未覆盖 → G-tests patch |
| R2-B12 | blind | tests DOCUMENTED_BOUNDS | low | 边界第三份手写副本，与 README/main.py 漂移不会变红 → G-docs patch（表头注释标明镜像同步义务） |
| R2-B13 | blind | 审查 diff 文件 | false | epic-7-context 双份出现在审查 diff 组合产物中；交付物仓库内仅一份 |
| E1 | edge | tests:50 | low | int 型变量未拒非整数样例（3.7 守卫绿、启动 int() 崩）→ G-tests patch（is_integer 校验） |
| E2 | edge | tests:62-75 | low | 已验证：正数必需变量（COOLDOWN/WINDOW/RECOVERY_STEP/BUDGET）用闭区间 low<=，0 会通过守卫但 main.py 非正 fail-fast → G-tests patch（左开区间） |
| E3 | edge | tests/conftest.py | medium | importlib.reload 组合根重跑 load_dotenv 重新注入本机 .env，一次性 scrub 失效 → G-scrub |
| E4 | edge | main.py:155 | low（拒绝） | PermissionError 自带文件名已足够响亮，日常不可达，包一层无新增可行动信息 |
| E5 | edge | main.py:155 | low（拒绝） | 仓库无安装包部署形态文档；该场景静默跳过=纯环境变量配置，符合 12-factor 预期 |
| E6 | edge | tests:79,95 | low | 未剥离注释行，纯注释提及 WEILIAO_X 会被计为消费→假阳 → G-tests patch |
| E7 | edge | tests/conftest.py（claim） | medium | 与 V3 同：一次性 scrub 未兑现 V2 承诺（test_session.py:677 场景可复现失败）→ G-scrub |
| V1 | vgap | main.py:155 | medium（采信） | override=False「进程 env 胜 .env」优先级无测试；翻成 override=True 干净 CI 全绿 → G-vgap patch（env 与文件冲突的变体用例） |
| V2 | vgap | AC3 服务半边 | medium（采信） | 自动化仅到 import；lifespan/就绪未覆盖；TestClient lifespan + /health 断言可补 → G-vgap patch |
| V3 | vgap | tests/conftest.py（other） | medium | scrub 对 reload 用例失效且 scrub 自身无测试钉住 → G-scrub |

## Verification

**Commands:**
- `uv run pytest tests/test_env_example.py -v` -- 28 passed + 2 skipped（2026-11-10
  最终轮；skip 为设计内行为——开发机存在真实 .env 时两个 sentinel 用例
  pytest.skip 保护数据，CI 无 .env 照跑）
- `uv run pytest` -- 822 passed, 2 skipped，无新增失败（2026-11-10 最终轮）
- `uv run ruff check src tests` -- All checks passed（2026-11-10；裸 `ruff check`
  全仓 302 个告警全部位于 BMad scaffolding 与 docs/fine-tuning-guide，均为存量）
- `uv run mypy src/weiliao_agent/main.py tests/test_env_example.py` -- no issues found（2026-11-10）；`tests/conftest.py:71` 有一处存量 arg-type note（baseline 即存在的 _FakeLangfuse 类型不匹配，与本次变更无关，未触碰）

**Manual checks (if no CLI):**
- 已做：以 .env.example 全部取值作为进程 env 导入组合根模块（模拟 cp 后
  dotenv 加载）——IMPORT_OK，全部 fail-fast 校验通过；uvicorn 冒烟
  （port 8123，无 Redis/broker）/health 返回 200 ready，四份快照齐全；
  反向验证 WEILIAO_L1_TTL_SECONDS='' 启动 fail-fast 且错误消息带变量名。
- 已做：runbook 处置口径逐条对照 metrics.py（model_routing_alert=tightened、
  llm_breaker state!=closed、risk_review 样本 ≥20 跌破阈值）与 probe.py 快照
  字段名一致。
- 已做（主代理复验 AC3）：干净 checkout 场景——备份原 .env → `cp .env.example
  .env` → 裸 `uvicorn weiliao_agent.main:app --port 8123` → /health 200 ready
  （cache/risk_review/llm_breaker/model_routing 四快照齐全）→ 恢复原 .env
  （md5 一致）。全程未导出任何 WEILIAO_* 变量，dotenv 自动加载路径生效。
