"""OpenAI 受控生成客户端（Story 4.2）。

沿用分类器（``classifiers/llm_zero_shot.py``）口径：模型名 alias 注入
（构造参数 > ``WEILIAO_LLM_MODEL`` 环境变量 > 默认 ``gpt-4o``，禁硬编码）、
惰性构造（首次 ``generate`` 才建客户端，组合根导入不依赖凭据可用性）、
调用侧 ``future.result(timeout)`` 超时双保险、统一错误自报 ``reason``、
可注入替身。temperature=0、max_tokens 默认 512（可配置）。

端到端预算默认 1.8s（NFR-4），含至多 1 次重试：429 / 5xx / 超时 / 网络
错误重试恰 1 次；400 系与凭据错误为确定性失败，立即抛出不在其上烧预算。
客户端级 timeout 按 ``预算 / (1 + 重试次数)`` 摊到单次调用，调用侧
``future.result(预算)`` 兜底总预算——超时无法中止的执行线程由有界线程池
（``_MAX_WORKERS=8``，同分类器口径）承接，调用侧不傻等。所有失败归一为
``LlmGenerationError``（自报 ``reason`` 供 trace 归因），不抛裸底层异常。
"""

import os
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from weiliao_agent.observability import tracing
from weiliao_agent.observability.metrics import get_discarded_generation_metrics

# 默认模型 alias（脊柱：模型名禁硬编码，gpt-4o 系关停前可经配置整体迁移）；
# 可被构造参数 / ``WEILIAO_LLM_MODEL`` 环境变量覆盖。
DEFAULT_MODEL_ALIAS = "gpt-4o"

# 生成端到端硬预算（含至多 1 次重试），经构造参数可调。
DEFAULT_BUDGET_SECONDS = 1.8

# 单次生成 token 上限（经构造参数可调）。
DEFAULT_MAX_TOKENS = 512

# 至多 1 次重试（spec I/O 矩阵：429 / 5xx / 超时 / 网络错误重试恰 1 次）。
_MAX_RETRIES = 1

# 超时的 LLM 调用无法中止（执行线程待其自行结束），并发上限防御超时风暴
# 下的线程膨胀；调用侧守卫先行返回，不傻等（同分类器口径）。
_MAX_WORKERS = 8


class LlmGenerationError(Exception):
    """生成失败统一异常：自报 ``reason`` 供过滤器 trace 失败归因。

    reason 取值：``invalid_input``（resolved_message 为空）/
    ``attempt_timeout``（单次调用超时，可重试 1 次）/ ``timeout``
    （调用侧硬预算耗尽）/ ``rate_limit``（429）/ ``server_<status>``（5xx）/
    ``network``（连接错误）/ ``http_<status>``（400 系确定性失败）/
    ``invalid_output``（响应内容异常）/ 底层异常类型名（凭据缺失等）。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        # 实际出错的模型 alias（client 在抛出点回填；供熔断器按 alias
        # 分键，code review Epic 4 补丁）。手工构造的异常缺省 None。
        self.model: str | None = None


@dataclass
class GenerationResult:
    """单次生成结果：文本 + 模型名 + token 用量（usage 供追踪埋点）。"""

    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


def _classify_error(exc: BaseException) -> tuple[bool, str]:
    """归一底层异常为 (是否可重试, reason)。

    可重试：429 / 5xx / 超时 / 网络错误；确定性失败（400 系、凭据缺失、
    内容过滤等）不重试，直接抛出以免烧预算。
    """
    if isinstance(exc, RateLimitError):
        return True, "rate_limit"
    if isinstance(exc, APITimeoutError):
        return True, "attempt_timeout"
    if isinstance(exc, APIConnectionError):
        return True, "network"
    if isinstance(exc, APIStatusError):
        if exc.status_code >= 500:
            return True, f"server_{exc.status_code}"
        return False, f"http_{exc.status_code}"
    if isinstance(exc, TimeoutError):
        return True, "attempt_timeout"
    if isinstance(exc, OSError):
        return True, "network"
    return False, type(exc).__name__


def _retrieve_future_exception(future: Future[GenerationResult]) -> None:
    """回收 Future 异常，避免 stderr ``Future exception was never retrieved``
    噪音：调用侧预算守卫放弃的执行线程若迟到失败，异常须被取走；已取消
    的 Future 无异常可取。成功完成的 Future ``exception()`` 返回 None。"""
    if future.cancelled():
        return
    future.exception()


class OpenAiGenerationClient:
    """OpenAI 受控生成客户端：预算守卫 + 至多 1 次重试 + 统一错误。

    ``chat_model`` 可注入与 ``ChatOpenAI`` 同接口的替身
    （``.invoke(messages)`` 返回带 ``content`` / ``usage_metadata`` 的
    响应），供无网络单测覆盖重试矩阵与超时守卫；生产路径缺省经
    ``WEILIAO_LLM_MODEL`` 惰性构造（首次 ``generate`` 才建客户端——
    凭据缺失等初始化异常归一为 ``LlmGenerationError``）。
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        chat_model: Any = None,
    ) -> None:
        if budget_seconds <= 0:
            # 非正预算会让每次 generate 都超时失败（错误归因误导），
            # 配置错误在构造时 fail-fast（错误消息带变量名，同 main.py 口径）。
            msg = f"budget_seconds must be positive, got {budget_seconds!r}"
            raise ValueError(msg)
        self._model = model or os.environ.get("WEILIAO_LLM_MODEL", DEFAULT_MODEL_ALIAS)
        self._budget_seconds = budget_seconds
        self._max_tokens = max_tokens
        self._chat_model = chat_model
        self._executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

    @property
    def model(self) -> str:
        """实际生效的模型 alias（追踪埋点消费）。"""
        return self._model

    def generate(
        self,
        messages: Sequence[BaseMessage],
        *,
        trace_id: str | None = None,
    ) -> GenerationResult:
        """预算内同步生成（至多 1 次重试）；失败抛 ``LlmGenerationError``。

        ``trace_id`` 可选透传（Story 7.2）：仅用于被丢弃调用的完成归因
        事件（``tracing.record_discarded_generation``），缺省 None 时
        归因记账照常、trace 事件按既有不可记录口径跳过。
        """
        future = self._executor.submit(self._generate_with_retry, list(messages))
        future.add_done_callback(_retrieve_future_exception)
        try:
            return future.result(timeout=self._budget_seconds)
        except FutureTimeoutError:
            # 硬预算耗尽：执行线程无法中止，待其自行结束（不傻等）。
            # Story 7.2（收口 4.3 挂账）：被丢弃的调用在后台跑完后经
            # 完成回调记账 token 归因（区别于成功口径，互斥不重叠）——
            # 回调自身 fail-safe，绝不阻塞应答路径。
            future.add_done_callback(_discarded_completion_callback(trace_id))
            msg = f"llm generation exceeded {self._budget_seconds:.2f}s budget"
            error = LlmGenerationError("timeout", msg)
            error.model = self._model
            raise error from None
        except LlmGenerationError as exc:
            # 回填实际出错的模型 alias（熔断器按 alias 分键消费）。
            if exc.model is None:
                exc.model = self._model
            raise
        except Exception as exc:
            # 惰性构造异常等未归类失败归一为统一错误（自报底层类型名）。
            error = LlmGenerationError(type(exc).__name__, str(exc))
            error.model = self._model
            raise error from exc

    def _generate_with_retry(self, messages: list[BaseMessage]) -> GenerationResult:
        """执行线程内：惰性构造 + 至多 1 次重试（仅可重试错误）。"""
        chat = self._chat(self._budget_seconds / (_MAX_RETRIES + 1))
        attempt = 0
        while True:
            try:
                response = chat.invoke(messages)
                return _to_result(response, model=self._model)
            except LlmGenerationError:
                raise
            except Exception as exc:
                retryable, reason = _classify_error(exc)
                if not retryable or attempt >= _MAX_RETRIES:
                    raise LlmGenerationError(reason, str(exc)) from exc
                attempt += 1

    def _chat(self, attempt_timeout: float) -> Any:
        """惰性构造生产模型（注入替身优先）；OpenAI 凭据缺失等初始化
        异常在首次调用时抛出，由 ``generate`` 归一为统一错误。"""
        if self._chat_model is not None:
            return self._chat_model
        from langchain_openai import ChatOpenAI

        chat = ChatOpenAI(
            model=self._model,
            timeout=attempt_timeout,
            max_retries=0,
            temperature=0,
            max_completion_tokens=self._max_tokens,
        )
        self._chat_model = chat
        return chat


def _discarded_completion_callback(
    trace_id: str | None,
) -> Callable[[Future[GenerationResult]], None]:
    """构造「被丢弃调用完成」回调：成功回传 usage 才记账（失败无 token）。

    仅超时放弃路径注册本回调，故回调内任何成功完成都属于被丢弃口径
    （成功口径在调用侧正常返回时由路由层记账，互斥不重叠）。记账/
    trace 两动作全部吞异常：归因本身绝不影响任何线程。
    """

    def _callback(future: Future[GenerationResult]) -> None:
        with suppress(Exception):
            result = future.result()
            tokens = result.usage.get("total_tokens", 0)
            get_discarded_generation_metrics().record_discarded(tokens)
            tracing.record_discarded_generation(
                trace_id, model=result.model, usage=result.usage
            )

    return _callback


def _to_result(response: Any, *, model: str) -> GenerationResult:
    """从模型响应提取生成文本与 token 用量；内容异常归一 ``invalid_output``。"""
    content: Any = getattr(response, "content", None)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # 纯 str 块（部分兼容网关/中转对 text part 的返回形态）直接取用；
        # 其余未知类型块不静默丢弃——文本为空时归一 invalid_output 上抛
        # （code review Epic 4 补丁：静默截断比抛错更糟）。
        text = "".join(
            block
            if isinstance(block, str)
            else str(block.get("text", ""))
            for block in content
            if isinstance(block, (str, dict))
        )
    else:
        msg = f"unexpected response content type: {type(content).__name__}"
        raise LlmGenerationError("invalid_output", msg)
    if not text.strip():
        raise LlmGenerationError("invalid_output", "empty generation content")
    return GenerationResult(text=text, model=model, usage=_extract_usage(response))


def _extract_usage(response: Any) -> dict[str, int]:
    """从 ``usage_metadata`` 提取标量 token 用量（缺失/替身缺省容忍为空）。"""
    meta = getattr(response, "usage_metadata", None)
    if not isinstance(meta, Mapping):
        return {}
    return {
        str(key): int(value)
        for key, value in meta.items()
        if isinstance(value, (int, float))
    }
