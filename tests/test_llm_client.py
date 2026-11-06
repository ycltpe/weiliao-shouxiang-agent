"""OpenAI 生成客户端单测（Story 4.2，AC 第 2 条 + NFR-4）。

替身式覆盖 spec I/O & Edge-Case Matrix 错误分支：429/5xx/网络错误重试
恰 1 次后成功或抛统一异常；400 系不重试直接失败；超时由调用侧硬预算
守卫；别名注入三级口径；usage 提取与 invalid_output 归一。无网络。
"""

import time

import httpx
import openai
import pytest

from weiliao_agent.llm.client import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_MODEL_ALIAS,
    GenerationResult,
    LlmGenerationError,
    OpenAiGenerationClient,
)


class _StubResponse:
    """与 langchain AIMessage 同最小接口的响应替身。"""

    def __init__(self, content: object, usage_metadata: object = None) -> None:
        self.content = content
        self.usage_metadata = usage_metadata


class _ScriptedChat:
    """按剧本返回响应或抛异常的 chat 替身；记录调用次数。"""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls = 0

    def invoke(self, messages: object) -> _StubResponse:
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, _StubResponse)
        return item


def _api_status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(f"http {status}", response=response, body=None)


def _rate_limit_error() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def _api_timeout_error() -> openai.APITimeoutError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APITimeoutError(request)


def _connection_error() -> openai.APIConnectionError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIConnectionError(request=request)


def _client(
    chat: _ScriptedChat,
    *,
    model: str | None = None,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> OpenAiGenerationClient:
    return OpenAiGenerationClient(
        model, budget_seconds=budget_seconds, chat_model=chat
    )


def test_success_returns_text_model_and_usage() -> None:
    chat = _ScriptedChat(
        [
            _StubResponse(
                "您好，该房源价格约 120 万分桶档。",
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        ]
    )
    result = _client(chat).generate(_messages())

    assert isinstance(result, GenerationResult)
    assert result.text == "您好，该房源价格约 120 万分桶档。"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert chat.calls == 1


def test_model_alias_defaults_and_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEILIAO_LLM_MODEL", raising=False)
    assert _client(_ScriptedChat([])).model == DEFAULT_MODEL_ALIAS

    monkeypatch.setenv("WEILIAO_LLM_MODEL", "gpt-4o-mini")
    assert _client(_ScriptedChat([])).model == "gpt-4o-mini"
    # 构造参数优先于环境变量（分类器口径）。
    assert _client(_ScriptedChat([]), model="gpt-4o").model == "gpt-4o"


@pytest.mark.parametrize(
    "error",
    [
        _rate_limit_error(),
        _api_status_error(500),
        _api_status_error(503),
        _api_timeout_error(),
        _connection_error(),
        TimeoutError("timed out"),
        OSError("connection refused"),
    ],
    ids=["429", "500", "503", "api-timeout", "connection", "builtin-timeout", "oserror"],
)
def test_retryable_errors_retried_exactly_once_then_success(
    error: BaseException,
) -> None:
    chat = _ScriptedChat([error, _StubResponse("重试后成功")])

    result = _client(chat).generate(_messages())

    assert result.text == "重试后成功"
    assert chat.calls == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_deterministic_http_errors_fail_without_retry(status: int) -> None:
    chat = _ScriptedChat([_api_status_error(status)])

    with pytest.raises(LlmGenerationError) as exc_info:
        _client(chat).generate(_messages())

    assert exc_info.value.reason == f"http_{status}"
    assert chat.calls == 1


def test_persistent_rate_limit_exhausts_single_retry() -> None:
    first = _rate_limit_error()
    second = _rate_limit_error()
    chat = _ScriptedChat([first, second, _StubResponse("不应到达")])

    with pytest.raises(LlmGenerationError) as exc_info:
        _client(chat).generate(_messages())

    assert exc_info.value.reason == "rate_limit"
    assert chat.calls == 2  # 至多 1 次重试


def test_persistent_attempt_timeout_reports_attempt_timeout() -> None:
    chat = _ScriptedChat([_api_timeout_error(), _api_timeout_error()])

    with pytest.raises(LlmGenerationError) as exc_info:
        _client(chat).generate(_messages())

    assert exc_info.value.reason == "attempt_timeout"  # 与调用侧 budget 超时的 "timeout" 区分
    assert chat.calls == 2


def test_persistent_server_error_reports_server_reason() -> None:
    chat = _ScriptedChat([_api_status_error(500), _api_status_error(502)])

    with pytest.raises(LlmGenerationError) as exc_info:
        _client(chat).generate(_messages())

    assert exc_info.value.reason == "server_502"
    assert chat.calls == 2


def test_call_side_budget_guard_times_out_without_waiting() -> None:
    class _SlowChat:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages: object) -> _StubResponse:
            self.calls += 1
            time.sleep(2.0)  # 客户端级 timeout 无法中止，验证调用侧守卫
            return _StubResponse("不应到达")

    chat = _SlowChat()
    start = time.monotonic()
    with pytest.raises(LlmGenerationError) as exc_info:
        _client(chat, budget_seconds=0.1).generate(_messages())
    elapsed = time.monotonic() - start

    assert exc_info.value.reason == "timeout"
    assert elapsed < 1.0  # 调用侧不傻等执行线程


def test_empty_content_normalized_to_invalid_output() -> None:
    chat = _ScriptedChat([_StubResponse("  \n ")])

    with pytest.raises(LlmGenerationError) as exc_info:
        _client(chat).generate(_messages())

    assert exc_info.value.reason == "invalid_output"
    assert chat.calls == 1


def test_list_content_blocks_joined_into_text() -> None:
    chat = _ScriptedChat(
        [
            _StubResponse(
                [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]
            )
        ]
    )

    result = _client(chat).generate(_messages())

    assert result.text == "第一段第二段"


def test_unexpected_content_type_normalized_to_invalid_output() -> None:
    chat = _ScriptedChat([_StubResponse(123)])

    with pytest.raises(LlmGenerationError) as exc_info:
        _client(chat).generate(_messages())

    assert exc_info.value.reason == "invalid_output"
    assert chat.calls == 1


def test_generation_result_carries_model_and_usage() -> None:
    chat = _ScriptedChat(
        [
            _StubResponse(
                "应答",
                usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            )
        ]
    )
    client = OpenAiGenerationClient("gpt-4o-mini", chat_model=chat)

    result = client.generate(_messages())

    assert result.model == "gpt-4o-mini"
    assert result.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def test_missing_usage_metadata_tolerated() -> None:
    chat = _ScriptedChat([_StubResponse("无用量字段")])

    result = _client(chat).generate(_messages())

    assert result.text == "无用量字段"
    assert result.usage == {}


def test_lazy_construction_failure_wrapped_as_unified_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据缺失路径：惰性构造 ChatOpenAI 失败归一为 LlmGenerationError。

    以假 langchain_openai 模块模拟构造期抛错（不依赖真实凭据环境，
    OPENAI_API_KEY 是否设置都确定可达）。
    """
    import sys
    from types import ModuleType

    fake_module = ModuleType("langchain_openai")

    def _raising_chat_openai(**kwargs: object) -> object:
        msg = "The api_key client option must be set"
        raise RuntimeError(msg)

    fake_module.ChatOpenAI = _raising_chat_openai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)

    client = OpenAiGenerationClient()  # 不注入替身：走惰性构造

    with pytest.raises(LlmGenerationError) as exc_info:
        client.generate(_messages())

    assert exc_info.value.reason == "RuntimeError"


def _messages() -> list[object]:
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content="你好")]
