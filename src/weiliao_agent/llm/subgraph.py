"""LangGraph 1.2.16 受控生成子图（Story 4.2，AD-2 同步接口）。

LLM 编排仅允许位于本进程内子图：编译后仅经 ``.invoke()`` 在本进程调用，
不依赖 LangGraph Runtime、不服务化。单节点「generate」链路：prompt 渲染
（固定三段）→ 客户端生成（预算守卫 + 至多 1 次重试）→ 成功经
``tracing.record_generation`` 记录模型名与 token 用量（fail-safe，不阻断
应答）→ 文本写回 state。

对外只暴露同步 ``LlmGenerationSubgraph.generate()``；输入仅消费
``resolved_message`` + ``fact_card`` + ``intent``（+追踪用 ``trace_id``），
空消息 fail-fast（reason=``invalid_input``），不触达模型。
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from weiliao_agent.llm.client import LlmGenerationError, OpenAiGenerationClient
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
) -> CompiledStateGraph[LlmSubgraphState, None, LlmSubgraphState, LlmSubgraphState]:
    """编译生成子图：START → generate → END（节点内组合 client 与埋点）。"""

    def _generate_node(state: LlmSubgraphState) -> dict[str, Any]:
        messages = build_generation_prompt(
            resolved_message=state["resolved_message"],
            fact_card=state["fact_card"],
            intent=state["intent"],
        )
        result = client.generate(messages)
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


class LlmGenerationSubgraph:
    """编译后的生成子图 + 同步入口（进程内 ``.invoke()``，AD-2）。"""

    def __init__(self, client: OpenAiGenerationClient | None = None) -> None:
        self._client = client or OpenAiGenerationClient()
        self._graph = build_generation_graph(self._client)

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
