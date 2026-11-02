"""意图分类器包（Story 2.2）：包级统一工厂按配置名构造实现。

组合根唯一装配入口：``create_classifier(name)`` 按
``WEILIAO_INTENT_CLASSIFIER`` 取值构造实现；实现切换只动配置与本
工厂，路由链路代码零感知（spec AC 第 4 条）。合法值：

- ``llm_zero_shot``（默认）：LLM zero-shot 实现
  （weiliao_agent.classifiers.llm_zero_shot）；
- ``none``：不装配分类器——过滤器保留 Story 1.2 占位行为（意图缺失
  置 UNKNOWN），用于无 LLM 凭据环境的降级运行。

非法值 fail-fast：抛 ``ValueError``，组合根（main.py 导入期）拒绝启动。
"""

from weiliao_agent.classifiers.llm_zero_shot import (
    IntentClassificationError,
    LlmZeroShotClassifier,
)
from weiliao_agent.filters.intent_router import IntentClassifier

__all__ = [
    "IntentClassificationError",
    "IntentClassifier",
    "LlmZeroShotClassifier",
    "create_classifier",
]

# 合法配置名 -> 构造说明（工厂 docstring 同步维护）。
_KNOWN_NAMES = ("llm_zero_shot", "none")


def create_classifier(name: str) -> IntentClassifier | None:
    """按配置名构造分类器实现；非法值 fail-fast（ValueError）。

    返回 None 表示不装配分类器（"none"），过滤器走占位行为。
    """
    normalized = name.strip().lower()
    if normalized == "none":
        return None
    if normalized == "llm_zero_shot":
        return LlmZeroShotClassifier()
    msg = (
        f"unknown intent classifier: {name!r} "
        f"(expected one of: {', '.join(_KNOWN_NAMES)})"
    )
    raise ValueError(msg)
