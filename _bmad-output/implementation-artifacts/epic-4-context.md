# Epic 4 Context: LLM 受控应答与风控兜底

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

实现 LangGraph LLM 子图与 OpenAI 受控生成、后置风控双审核、OpenAI 故障降级与模板兜底，保证买家获得自然、合规、事实准确的 AI 应答，任何故障（审核不通过、OpenAI 429/5xx/超时、生成空草稿）都不中断服务。本 Epic 承接所有未命中缓存、非硬触发的消息，是系统"剩余 20% 复杂问题由 LLM 受控生成"这一产品立论的核心，也是可用性目标（99.95%，NFR-2）的主要承载。

## Stories

- Story 4.1: 模板兜底应答库
- Story 4.2: LLM 子图与 OpenAI 客户端
- Story 4.3: 事实卡片 Grounding 受控生成
- Story 4.4: 后置风控双审核
- Story 4.5: OpenAI 故障降级与步进恢复
- Story 4.6: 模型路由与成本护栏
- Story 4.7: 空草稿拦截与兜底承接

## Requirements & Constraints

- LLM 应答必须基于事实卡片中的房源数据，禁止虚构价格、面积、学区信息；生成文本不得承诺价格可谈、学区名额，不得使用"最""第一"等违禁词，不得包含"我是这套房的经纪人"或等价冒充表述。
- 100% 的 LLM 生成内容须通过风控中台审核后才可发送买家；审核不通过（含超时）时切模板兜底，服务无中断。审核通过率须 ≥95%（上线前压测基线，全量放行门槛），且需持续阈值监控告警，不能只在压测时验证。
- 延迟预算：首 token 到达 P99 < 1.5s；OpenAI 端到端 ≤1.8s（含 1 次重试），是全链路 P99 < 3s 倒推的有意从严。风控审核超时默认 500ms（可调 300-800ms），超时视为不通过。
- OpenAI 故障（429 / 5xx / 超时 >1.8s）自动切模板兜底，切换 <100ms；恢复后按 10% 步进逐步切回。降级期间服务可用性不降级（不向买家返回 5xx）；降级事件写 Langfuse trace 并触发监控告警。
- 模型路由（gpt-4o / gpt-4o-mini 按意图复杂度、置信度分流）配置化、可灰度；模型选择记录 Langfuse trace，单条消息的模型与费用可复盘。成本超限时自动收紧路由（提高 mini 占比）并告警，不能仅事后看报表。
- 空草稿（空串或仅空白，strip 后判空）由 risk_review / 响应组装入口统一按"无草稿"拦截，走模板兜底路径；拦截发生在草稿过审之前，且不得改写非空草稿（过审草稿除加前缀外冻结）。
- 模板库为 9 类意图（含 UNKNOWN）各提供合规文案，供错误兜底、审核不通过、OpenAI 故障统一复用；转人工分支禁止使用模板兜底文案。
- prompt 模板集中管理（llm/ 下），结构为事实卡片、对话上下文、生成约束三段。

## Technical Decisions

- LLM 生成过滤器内部允许 LangGraph 1.2.16 子图（生成 + 后置审核 + 兜底切换），但对外仅暴露同步生成接口；热路径不依赖 LangGraph Runtime（AD-2）。
- 风控审核位于响应组装之后、下发之前：审核通过直接下发，不通过切模板兜底（AD-8 超时视为不通过）；禁止任何未审核内容到达买家侧。
- 降级策略（AD-9）：断路器判定阈值为 429/5xx/超时 >1.8s，切换 <100ms，恢复按 10% 步进；LLM 过滤器内部重试 ≤1 次；降级事件全部落 Langfuse trace 并告警（Story 4.6 的路由护栏有 WEILIAO_LLM_BREAKER_* 系列 env 变量，与 /health 的 model_routing 快照、model_routing_alert 告警配套，运维交付归 Epic 7）。
- 模型名经配置注入（alias 指向当前快照，禁硬编码）：gpt-4o 全系列将于 2027-01-14 从 API 关停，官方替代 gpt-6-luna，迁移须在关停前完成替代模型验证与灰度（复用降级恢复步进机制）。
- 信封统一消费 resolved_message（消解后消息），禁止直接读取买家原始文本；意图标签为 9 类 Literal 枚举，未知值 fail-closed 到模板兜底。
- 错误处理统一约定：所有过滤器异常降级至模板兜底（转人工分支除外）；错误响应携带 trace_id。
- 可观测：Langfuse trace_id 贯穿全链路，降级事件、审核结果、模型选择与费用均记录 trace；验收口径以 Langfuse trace 数据为准。

## Cross-Story Dependencies

- Story 4.1（模板库）被 4.4、4.5、4.7 及全局错误兜底（Story 1.2）复用，应先落地。
- Story 4.2 是 4.3、4.5、4.6 的前提（子图与客户端先行）。
- Story 4.6 依赖 Story 1.5 的成本追踪脚手架与 4.2 的 OpenAI 客户端。
- Story 4.7 与 response_assembler、risk_review 的拦截口径必须一致（两处判定同一标准）。
- 编排器"逐跳超时丢弃执行线程无取消/复用机制"的问题在 Epic 4 落地后统一处理（挂账 Epic 7 的 NFR10 压测与执行器生命周期重审）。
