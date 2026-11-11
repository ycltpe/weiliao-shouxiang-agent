"""消息信封 —— 管道-过滤器架构全链路唯一通信模型。

信封随管道逐跳填充：每个过滤器仅消费上游信封、产出下游信封。
任意来源的 dict 必须经 ``Envelope.model_validate`` 校验后方可进入管道；
缺失必填字段、类型错误一律 fail-closed（抛 ``pydantic.ValidationError``），
由调用方兜底。

字段口径（架构一致性约定）：
- ``session_id`` / ``house_id`` / ``timestamp`` 必填；
- ``trace_id`` 缺省为 None：由服务在编排器入口生成并回填信封
  （Story 1.5，32 位小写十六进制，即 Langfuse trace id 口径）。
  回填语义为「缺失即生成、携带值透传」（既有测试锁定透传）：
  payload 携带值原样流经管道与响应，但仅 32-hex 值可作为 Langfuse
  trace id 被消费，非 32-hex 值跳过追踪（见 observability.tracing）；
  空串仍拒绝；
- ``turn``（会话内绝对消息序号，由会话管理在入管道前赋值）允许为 None，
  但非 None 时类型必须正确；
- ``intent`` 已收窄为 ``IntentTag | None``（Story 2.1，9 类 Literal 枚举，
  权威定义在 models.intent，由分类器填写）：未知值经 ``model_validate``
  拒绝即 fail-closed 到模板兜底；
- ``is_followup``（Story 2.3，重复追问标记）：同一 session 内同一意图第
  2 次起为 True，由 intent_router 经注入的会话单写者回调判定后写回；
  默认 False（含分类 fail-closed / 记录失败 fail-safe 路径）。Epic 3 的
  cache_lookup 据此禁走 L0 缓存；
- ``fact_card``（Story 3.3，缓存查找结果挂点，决策 A 方案）：L0 事实卡片
  查找的下游通道——cache_lookup 命中把卡片挂到信封，miss 保持 None
  留给 Epic 4 的 LLM 层；查找顺序 L1 → L0 → LLM 对管道真实可观测，
  Story 3.4 的命中率口径亦基于本字段统计；
- ``generated_reply``（Story 4.3，受控生成草稿挂点，影子接入决策 A）：
  llm_generate 过滤器把通过确定性 grounding 校验的 LLM 草稿挂到本
  可选字段；红线意图/非文本/空消息跳过、生成异常、校验未过一律保持
  None。买家可见应答仍由 response_assembler 模板兜底产出——4.4 审核
  就位前 LLM 内容不得触达买家；
- ``handoff``（Story 5.1，硬触发判定结果挂点，AD-12）：intent_router
  内单一评估模块产出——未评估为 None、评估未触发为 ``triggered=False``
  （区分「没评估」与「评估了没命中」）；编排器只认 ``triggered is True``
  短路下游（cache/llm/risk/assembler 零执行）走转人工出口分支；
- ``message`` / ``resolved_message`` / ``coref`` / ``media_type`` 默认 None；
  ``message`` 为买家原文，已由 coreference 过滤器透传消费（拷贝至
  ``resolved_message``），Story 6.3 起 intent_router / llm_generate
  决策输入唯一来源为 ``resolved_message``，原文仅留痕性消费；
  ``resolved_message`` / ``coref`` 为 Story 6.1 收窄后的指代消解挂点
  （AD-11）——coreference 过滤器（链首）唯一写入，信封为唯一真相源；
  ``coref`` 为 None 保留「过滤器未执行」语义；``media_type`` 自
  Story 5.1 起由硬触发评估消费（None / "text" 视为文本，其余一律判
  触发）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from weiliao_agent.models.coref import CorefRef
from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.models.intent import IntentTag


class HandoffAssessment(BaseModel):
    """Story 5.1 硬触发判定结果（AD-12 单一评估点产出）。

    ``conditions`` 为命中条件清单（标识词与负面词条，判定顺序固定），
    未命中为空清单；评估代码自身异常时 fail-closed 记
    ``triggered=True`` + ``conditions=["assessment_error"]``（调用方口径）。
    """

    triggered: bool = False
    conditions: list[str] = Field(default_factory=list)


class Envelope(BaseModel):
    """管道内传递的标准化消息信封。"""

    # Story 7.2（D3 决策落地，收口 1.2 frozen 挂账）：信封全链所有权冻结
    # ——过滤器已全链 model_copy 整信封替换（唯一合法演进形态），原地
    # 写仅余编排器 trace_id 权威回填与 http 入口适配器 3 处（均已改
    # model_copy，见 pipeline.py / adapters/http.py）；frozen 使「超时
    # 丢弃线程原地变更信封污染编排器引用」从隐患变为构造期 loud
    # failure（未来过滤器误用即开发期爆开，绝不带病进生产）。
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    house_id: str = Field(min_length=1)
    timestamp: datetime
    # Story 1.5：trace_id 放宽为可选——服务端权威生成（编排器入口
    # 回填），其余校验规则不动（空串仍 min_length=1 拒绝）。
    trace_id: str | None = Field(default=None, min_length=1)
    turn: int | None = None
    intent: IntentTag | None = None
    # Story 2.3：Follow-up Query 标记（同意图第 2 次起为 True，Epic 3
    # cache_lookup 消费）；默认 False——分类 fail-closed 与追问记录失败
    # fail-safe 路径均不置位。
    is_followup: bool = False
    # Story 3.3：L0 查找结果挂点（决策 A 方案）——cache_lookup 命中挂
    # 卡片，miss 保持 None 留给 Epic 4 LLM 层；买家可见应答仍由
    # response_assembler 的占位模板产出，本字段仅携带结构化事实。
    fact_card: FactCard | None = None
    # Story 4.3：受控生成草稿挂点（影子接入，决策 A 方案）——仅
    # llm_generate 写入（model_copy 追加可选字段，既有 model_validate
    # 兼容）；任何故障路径保持 None，买家可见语义不变。
    generated_reply: str | None = None
    # Story 5.4：响应组装产出挂点（AD-10）——response_assembler 是
    # AI 身份披露前缀的唯一注入载体，本字段为其权威产出（草稿加
    # 前缀 / 无草稿走模板兜底）；编排器最终返回点只读消费，防御性
    # 回退 render_fallback，绝不改写已过审草稿之外的文本。
    assembled_reply: str | None = None
    # Story 5.1：硬触发判定结果挂点（AD-12）——intent_router 内单一
    # 评估模块写入；未评估为 None、评估未触发为 triggered=False（区分
    # 「没评估」与「评估了没命中」），下游短路只认 triggered is True。
    handoff: HandoffAssessment | None = None
    # 以下字段口径：message 为买家原文（coreference 透传其拷贝至
    # resolved_message；Story 6.3 起决策输入唯一来源为 resolved_
    # message，原文仅留痕性消费——pipeline trace / http 转写 /
    # handoff 载荷）；resolved_message / coref 为 Story 6.1 收窄后
    # 的指代消解挂点（AD-11，权威定义 models.coref.CorefRef）——
    # coreference 过滤器唯一写入（默认透传：resolved_message 为原文
    # 拷贝、coref 为 []）；coref 为 None 即「过滤器未执行」。media_
    # type 仍为架构预留字段。
    message: str | None = None
    resolved_message: str | None = None
    coref: list[CorefRef] | None = None
    media_type: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _timestamp_must_be_tz_aware(cls, value: datetime) -> datetime:
        """结算 deferred-work：时间戳必须带时区（naive datetime 拒绝）。"""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            msg = "timestamp must be timezone-aware"
            raise ValueError(msg)
        return value

    @field_validator("session_id", "house_id", "trace_id")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        """code review 补丁：min_length=1 不挡纯空白串（" "），空白
        标识会生成 ``weiliao:session: `` 类脏 key 致会话路由静默失效。"""
        if value is not None and not value.strip():
            msg = "field must not be blank"
            raise ValueError(msg)
        return value
