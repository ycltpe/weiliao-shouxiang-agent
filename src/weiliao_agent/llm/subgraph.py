"""LangGraph 1.2.16 受控生成子图（Story 4.2，AD-2 同步接口）。

LLM 编排仅允许位于本进程内子图：编译后仅经 ``.invoke()`` 在本进程调用，
不依赖 LangGraph Runtime、不服务化。单节点「generate」链路：prompt 渲染
（固定三段）→ 客户端生成（预算守卫 + 至多 1 次重试）→ 成功经
``tracing.record_generation`` 记录模型名与 token 用量（fail-safe，不阻断
应答）→ 文本写回 state。

对外只暴露同步 ``LlmGenerationSubgraph.generate()``；输入仅消费
``resolved_message`` + ``fact_card`` + ``intent``（+追踪用 ``trace_id``），
空消息 fail-fast（reason=``invalid_input``），不触达模型。

Story 4.6：可选注入模型路由（``llm.model_router.ModelRouter``，None=
单模型既有路径逐字节不变）——generate 节点经 router 按意图复杂度与
滑窗预算护栏选定 client 生成，路由事件（primary/mini/forced_mini/
recovered）记 trace、成功 usage 由 router 记账（失败不计）；路由判定/
记账自身全部 fail-safe：护栏异常回退主模型放行处理，绝不阻断生成。
"""

from contextlib import suppress
from typing import Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from weiliao_agent.llm.client import (
    GenerationResult,
    LlmGenerationError,
    OpenAiGenerationClient,
)
from weiliao_agent.llm.model_router import ModelRouter
from weiliao_agent.llm.prompts import build_generation_prompt
from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.models.intent import IntentTag
from weiliao_agent.observability import tracing

# 生成节点名（子图 span 口径：TTFT/端到端延迟以本节点起止观测）。
GENERATE_NODE = "generate"


class LlmSubgraphState(TypedDict):
    """子图状态：输入（消息 + 事实 + 意图 + trace）与节点产出。"""

    resolved_message: str
    fact_card: FactCard | None
    intent: IntentTag | None
    trace_id: str | None
    text: str
    model: str
    usage: dict[str, int]


def build_generation_graph(
    client: OpenAiGenerationClient,
    router: ModelRouter | None = None,
) -> CompiledStateGraph[LlmSubgraphState, None, LlmSubgraphState, LlmSubgraphState]:
    """编译生成子图：START → generate → END（节点内组合 client 与埋点）。"""

    def _generate_node(state: LlmSubgraphState) -> dict[str, Any]:
        messages = build_generation_prompt(
            resolved_message=state["resolved_message"],
            fact_card=state["fact_card"],
            intent=state["intent"],
        )
        if router is not None:
            result = _generate_via_router(router, client, messages, state)
        else:
            result = client.generate(messages, trace_id=state["trace_id"])
        # fail-safe 追踪：record_generation 内部吞异常，不阻断应答链路；
        # cost 不在本 Story 计算（由 Langfuse 按模型自算，Story 4.6 细化）。
        tracing.record_generation(
            state["trace_id"], model=result.model, usage=result.usage, cost={}
        )
        return {"text": result.text, "model": result.model, "usage": result.usage}

    graph: StateGraph[LlmSubgraphState] = StateGraph(LlmSubgraphState)
    graph.add_node(GENERATE_NODE, _generate_node)
    graph.add_edge(START, GENERATE_NODE)
    graph.add_edge(GENERATE_NODE, END)
    return graph.compile()


def _generate_via_router(
    router: ModelRouter,
    fallback: OpenAiGenerationClient,
    messages: list[BaseMessage],
    state: LlmSubgraphState,
) -> GenerationResult:
    """路由路径生成：护栏判定/记账 fail-safe，异常回退主模型放行。

    判定自身异常吞掉并按常规路由（主模型）放行——预算护栏是优化层，
    故障时退化回既有路径，绝不反过来阻断生成；生成失败（LlmGenerationError）
    不在此吞掉，照常上抛由过滤器既有语义归一（失败不计入预算）。
    """
    try:
        decision = router.select(state["intent"])
    except Exception:
        decision = None
    if decision is None:
        return fallback.generate(messages, trace_id=state["trace_id"])
    result = decision.client.generate(messages, trace_id=state["trace_id"])
    # fail-safe 追踪（tracing 内部吞异常）：单条消息可复盘路由 outcome；
    # 收紧→恢复迁移的判定瞬间补记 recovered 事件。
    tracing.record_model_routing_event(
        state["trace_id"], outcome=decision.outcome, intent=state["intent"]
    )
    if decision.transition is not None:
        tracing.record_model_routing_event(
            state["trace_id"], outcome=decision.transition, intent=state["intent"]
        )
    # 记账 fail-safe：预算护栏不得阻断已成功生成。
    with suppress(Exception):
        router.account(decision, result.usage)
    return result


class LlmGenerationSubgraph:
    """编译后的生成子图 + 同步入口（进程内 ``.invoke()``，AD-2）。"""

    def __init__(
        self,
        client: OpenAiGenerationClient | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        # router 可选注入（Story 4.6）：None=单模型既有路径逐字节不变；
        # 组合根注入时 client 须为路由主模型 client（护栏 fail-safe 回退用）。
        self._client = client or OpenAiGenerationClient()
        self._router = router
        self._graph = build_generation_graph(self._client, router=router)

    def peek_model(self, intent: IntentTag | None) -> str:
        """纯读预览本次将使用的模型 alias（code review Epic 4 补丁）。

        无副作用（router.peek_model 不做窗口剔除/状态迁移），供过滤器层
        熔断器按 alias 分键；无 router 时为主模型 client 的 alias。
        """
        if self._router is not None:
            return self._router.peek_model(intent)
        return self._client.model

    def generate(
        self,
        resolved_message: str,
        *,
        fact_card: FactCard | None = None,
        intent: IntentTag | None = None,
        trace_id: str | None = None,
    ) -> str:
        """同步受控生成：返回生成文本；失败抛 ``LlmGenerationError``。

        空/空白消息 fail-fast（reason=``invalid_input``），在子图调用前
        拒绝，不触达模型。``fact_card=None``（L0 miss）照常生成，事实段
        由 prompt 层明示「暂无事实」。
        """
        if not resolved_message or not resolved_message.strip():
            raise LlmGenerationError("invalid_input", "resolved_message is empty")
        final_state = self._graph.invoke(
            {
                "resolved_message": resolved_message,
                "fact_card": fact_card,
                "intent": intent,
                "trace_id": trace_id,
                "text": "",
                "model": "",
                "usage": {},
            }
        )
        text: str = final_state["text"]
        return text
