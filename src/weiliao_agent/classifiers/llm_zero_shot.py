"""LLM zero-shot 意图分类默认实现（Story 2.2，OQ-1 选 B）。

ChatOpenAI 结构化输出（单一标签 + 自评置信度），满足 AD-3「单一标签 +
置信度」契约；实现实例无分类状态，可安全复用 2.1 的线程池执行模型
（组合根单例注入过滤器，过滤器跑在编排器线程池内）。

硬超时守卫（spec Design Notes）：客户端级 timeout 设在预算内
（默认 45ms，经构造参数/环境配置注入），并以调用侧
``future.result(timeout)`` 双保险——超时/网络错误/结构化输出校验
失败一律抛 ``IntentClassificationError``（自报 ``reason`` 供过滤器
trace 归因），由过滤器 fail-closed 为 UNKNOWN，不给规则兜底（保持
单一来源，避免双实现口径漂移）。

置信度口径：取 LLM 结构化输出的自评分数；模型不自评（confidence 缺省）
时以固定口径 0.8 回填——不回填 1.0，为 Story 2.3 追问归并保留信号
空间。模型名经配置注入（脊柱：模型名禁硬编码，gpt-4o 系 2027-01-14
关停需 alias 可换）：构造参数 > ``WEILIAO_INTENT_MODEL`` 环境变量 >
默认 alias（gpt-4o-mini）。
"""

import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from weiliao_agent.models.intent import IntentClassification, IntentTag

# 默认模型 alias（gpt-4o 系 2027-01-14 API 关停前可经配置整体迁移，
# 脊柱 Stack 表迁移路径见 Deferred）；可被构造参数/环境变量覆盖。
DEFAULT_MODEL_ALIAS = "gpt-4o-mini"

# 分类延迟硬预算：P99 < 50ms 端到端口径（含前置指代消解）的实现内守卫，
# 不依赖编排器 deadline 兜底。
DEFAULT_TIMEOUT_SECONDS = 0.045

# 模型不自评置信度时的固定回填值（spec Design Notes，不回填 1.0）。
FALLBACK_CONFIDENCE = 0.8

# 超时的 LLM 调用无法中止（执行线程待其自行结束），并发上限防御
# 超时风暴下的线程膨胀；调用侧守卫先行返回，不傻等。
_MAX_WORKERS = 8


class IntentClassificationError(Exception):
    """分类失败统一异常：自报 ``reason`` 供过滤器 trace 失败归因。

    reason 取值：``timeout``（硬预算耗尽）/ ``invalid_output``（结构化
    输出校验拒绝）/ 底层异常类型名（网络错误等）。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


class _LlmIntentOutput(BaseModel):
    """LLM 结构化输出 schema：单标签 + 可缺省自评置信度。"""

    tag: IntentTag
    confidence: float | None = None


class LlmZeroShotClassifier:
    """默认意图分类器：ChatOpenAI 结构化输出 + 硬超时守卫。

    ``structured_model`` 可注入与 ``with_structured_output`` 同接口的
    替身（``.invoke(messages)`` 返回 ``_LlmIntentOutput``），供无网络
    单测覆盖超时守卫与输出解析；生产路径缺省经 ``WEILIAO_INTENT_MODEL``
    惰性构造 ChatOpenAI（首次 classify 才建客户端——组合根导入不依赖
    OpenAI 凭据可用性）。
    """

    # trace 实现来源自报值（过滤器 record_classification 消费）。
    source = "llm_zero_shot"

    def __init__(
        self,
        model: str | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        structured_model: Any = None,
    ) -> None:
        self._model = model or os.environ.get(
            "WEILIAO_INTENT_MODEL", DEFAULT_MODEL_ALIAS
        )
        self._timeout_seconds = timeout_seconds
        self._structured_model = structured_model
        self._executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        """对单条消息输出单一意图标签与置信度（超时/失败抛统一异常）。"""
        future = self._executor.submit(self._classify_once, message, context)
        try:
            return future.result(timeout=self._timeout_seconds)
        except FutureTimeoutError:
            # 硬预算耗尽：执行线程无法中止，待其自行结束（不傻等）。
            msg = f"llm classification exceeded {self._timeout_seconds * 1000:.0f}ms"
            raise IntentClassificationError("timeout", msg) from None
        except IntentClassificationError:
            raise
        except Exception as exc:
            raise IntentClassificationError(type(exc).__name__, str(exc)) from exc

    def _classify_once(
        self, message: str, context: Mapping[str, Any]
    ) -> IntentClassification:
        output = self._structured().invoke(_prompt(message, context))
        confidence = output.confidence
        if confidence is None:
            confidence = FALLBACK_CONFIDENCE
        try:
            return IntentClassification(tag=output.tag, confidence=confidence)
        except ValidationError as exc:
            # 结构化输出绕开 Literal 的兜底防线：置信度越界等在此拒绝。
            raise IntentClassificationError("invalid_output", str(exc)) from exc

    def _structured(self) -> Any:
        """惰性构造生产模型（注入替身优先）；OpenAI 凭据缺失等初始化
        异常在首次调用时抛出，由过滤器 fail-closed 承接。"""
        if self._structured_model is not None:
            return self._structured_model
        from langchain_openai import ChatOpenAI

        chat = ChatOpenAI(
            model=self._model,
            timeout=self._timeout_seconds,
            max_retries=0,
            temperature=0,
        )
        self._structured_model = chat.with_structured_output(_LlmIntentOutput)
        return self._structured_model


def _prompt(message: str, context: Mapping[str, Any]) -> list[Any]:
    """zero-shot prompt：9 类标签清单 + 会话上下文 + 单标签/置信度口径。

    保持短小（延迟预算 45ms）：标签清单为唯一权威枚举说明，示例省略——
    结构化输出 schema 本身即约束。
    """
    tags = (
        "GREETING（打招呼寒暄）/ PRICE_INQUIRY（价格询问）/ "
        "AVAILABILITY（在租在售、看房可得性）/ LOCATION（位置交通）/ "
        "PHOTO_REQUEST（图片索取）/ NEGOTIATION（砍价议价）/ "
        "LOAN_POLICY（贷款政策）/ COMPLAINT（投诉不满）/ UNKNOWN（无法判断）"
    )
    system_message = (
        "你是房产微聊意图分类器。将买家消息分类为以下唯一意图之一："
        f"{tags}。"
        "输出 JSON：tag（意图标签）与 confidence（0.0-1.0 自评置信度）。"
        "只输出最高置信度的一个标签；确实无法判断时输出 UNKNOWN。"
    )
    human_message = (
        f"会话上下文：session_id={context.get('session_id', '')}, "
        f"house_id={context.get('house_id', '')}, turn={context.get('turn', '')}\n"
        f"买家消息：{message}"
    )
    return [SystemMessage(content=system_message), HumanMessage(content=human_message)]
