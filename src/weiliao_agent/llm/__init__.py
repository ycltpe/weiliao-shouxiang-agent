"""LLM 生成：LangGraph 子图、OpenAI client、prompt 管理。

Story 4.2 对外契约：仅同步 ``generate()`` 入口 + 统一错误
``LlmGenerationError``（自报 ``reason``）；模块级入口惰性装配默认子图
（OpenAI 凭据缺失等初始化异常在首次调用时归一抛出）。组合根不接线——
买家可见路径仍走模板兜底，管道接入归 Story 4.3/4.4/4.5。
"""

from threading import Lock

from weiliao_agent.llm.client import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_ALIAS,
    GenerationResult,
    LlmGenerationError,
    OpenAiGenerationClient,
)
from weiliao_agent.llm.model_router import (
    DEFAULT_BUDGET_TOKENS as ROUTING_DEFAULT_BUDGET_TOKENS,
)
from weiliao_agent.llm.model_router import (
    DEFAULT_MINI_MODEL_ALIAS,
    KNOWN_INTENTS,
    ModelRouter,
    RoutingDecision,
)
from weiliao_agent.llm.model_router import (
    DEFAULT_SIMPLE_INTENTS as ROUTING_DEFAULT_SIMPLE_INTENTS,
)
from weiliao_agent.llm.model_router import (
    DEFAULT_WINDOW_SECONDS as ROUTING_DEFAULT_WINDOW_SECONDS,
)
from weiliao_agent.llm.prompts import build_generation_prompt
from weiliao_agent.llm.subgraph import (
    GENERATE_NODE,
    LlmGenerationSubgraph,
    LlmSubgraphState,
    build_generation_graph,
)
from weiliao_agent.models.fact_card import FactCard
from weiliao_agent.models.intent import IntentTag

__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL_ALIAS",
    "DEFAULT_MINI_MODEL_ALIAS",
    "GENERATE_NODE",
    "GenerationResult",
    "KNOWN_INTENTS",
    "LlmGenerationError",
    "LlmGenerationSubgraph",
    "LlmSubgraphState",
    "ModelRouter",
    "OpenAiGenerationClient",
    "ROUTING_DEFAULT_BUDGET_TOKENS",
    "ROUTING_DEFAULT_SIMPLE_INTENTS",
    "ROUTING_DEFAULT_WINDOW_SECONDS",
    "RoutingDecision",
    "build_generation_graph",
    "build_generation_prompt",
    "generate",
]

_default_subgraph: LlmGenerationSubgraph | None = None
# 惰性装配互斥：并发首调用不得重复建子图（每个子图自带客户端线程池，
# 重复装配即泄漏 executor）；双重检查锁口径。
_default_lock = Lock()


def generate(
    resolved_message: str,
    *,
    fact_card: FactCard | None = None,
    intent: IntentTag | None = None,
    trace_id: str | None = None,
) -> str:
    """模块级同步生成入口（惰性装配默认子图）。

    定制装配（替身客户端/自定义预算）走 ``LlmGenerationSubgraph(client)``；
    本入口与默认客户端均为惰性——模块导入不依赖 OpenAI 凭据可用性。
    """
    global _default_subgraph
    if _default_subgraph is None:
        with _default_lock:
            if _default_subgraph is None:
                _default_subgraph = LlmGenerationSubgraph()
    return _default_subgraph.generate(
        resolved_message, fact_card=fact_card, intent=intent, trace_id=trace_id
    )
