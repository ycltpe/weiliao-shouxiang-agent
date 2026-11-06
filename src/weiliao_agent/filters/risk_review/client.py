"""风控中台审核客户端（Story 4.4，双审核第二道）。

最小 JSON 协议（真实中台接口文档暂缺，Checkpoint 1 决策）：POST
``{text, intent, trace_id, house_id}`` → 响应 ``{"pass": bool, "reason": str?}``。
生产传输用标准库 urllib（spec Never 约束：不引入新的第三方 HTTP 依赖）；
``transport`` 可注入替身，真实中台联调期再写适配层。

fail-closed 口径（spec Boundaries）：
- 未配置 ``WEILIAO_RISK_REVIEW_ENDPOINT``：任何草稿视为不通过
  （绝不默认放行），``endpoint_not_configured`` 归因；
- 网络错误 / 非 200 / JSON 损坏 / 缺 ``pass`` 字段：一律归一为不通过
  （outcome=``error``），绝不向上抛；
- 超时默认 500ms，``WEILIAO_RISK_REVIEW_TIMEOUT_MS`` 可调 300–800
  （越界 fail-fast，错误消息带变量名）；超时无法中止的执行线程由有界
  线程池（``_MAX_WORKERS``，同 OpenAiGenerationClient 口径）承接，
  调用侧 ``future.result(timeout)`` 先行返回，不傻等。

所有路径返回 ``RiskReviewVerdict``（passed / outcome / latency_ms /
reason），不抛异常。
"""

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Literal

# 审核结果归因词表（四值 Literal 权威定义，拼错即静态报错）。
RiskReviewOutcome = Literal["passed", "rejected", "timeout", "error"]

# 默认审核超时（AD-8：超时视为不通过）；可调范围 300–800ms。
DEFAULT_TIMEOUT_MS = 500
MIN_TIMEOUT_MS = 300
MAX_TIMEOUT_MS = 800

_TIMEOUT_ENV = "WEILIAO_RISK_REVIEW_TIMEOUT_MS"
_ENDPOINT_ENV = "WEILIAO_RISK_REVIEW_ENDPOINT"

# 超时的审核调用无法中止（执行线程待其自行结束），并发上限防御超时
# 风暴下的线程膨胀；调用侧守卫先行返回，不傻等（同分类器口径）。
_MAX_WORKERS = 8

# 归因文本长度上限（防异常文本膨胀指标与 trace）。
_REASON_LIMIT = 500


@dataclass(frozen=True)
class RiskReviewHttpResponse:
    """传输层最小响应替身接口：状态码 + 原始 body 文本。"""

    status: int
    body: str


@dataclass(frozen=True)
class RiskReviewVerdict:
    """单次审核归一结果：过滤器只消费本结构，不感知传输细节。

    outcome 取值：``passed``（中台放行）/ ``rejected``（中台显式拒绝，
    可带 reason）/ ``timeout``（超时视为不通过）/ ``error``（网络错/
    非 200/坏响应/endpoint 未配置）。后三者均为不通过。
    """

    passed: bool
    outcome: RiskReviewOutcome
    latency_ms: float
    reason: str | None = None


# 可注入传输协议：``(endpoint, payload, timeout_seconds) -> response``。
RiskReviewTransport = Callable[[str, Mapping[str, Any], float], RiskReviewHttpResponse]


def _urllib_transport(
    endpoint: str, payload: Mapping[str, Any], timeout_seconds: float
) -> RiskReviewHttpResponse:
    """生产传输：标准库 urllib POST JSON；HTTPError 归一为状态码响应。"""
    data = json.dumps(dict(payload)).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", "replace")
            return RiskReviewHttpResponse(status=response.status, body=body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return RiskReviewHttpResponse(status=exc.code, body=body)


def _retrieve_future_exception(future: Future[RiskReviewHttpResponse]) -> None:
    """回收 Future 异常，避免 stderr ``Future exception was never retrieved``
    噪音：调用侧超时守卫放弃的执行线程若迟到失败，异常须被取走。"""
    if future.cancelled():
        return
    future.exception()


class RiskReviewClient:
    """风控中台审核客户端：超时守卫 + 全路径 fail-closed 归一。

    ``endpoint`` 缺省读 ``WEILIAO_RISK_REVIEW_ENDPOINT``（未配置即
    fail-closed，不视为通过）；``transport`` 可注入替身供无网络单测。
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        transport: RiskReviewTransport | None = None,
    ) -> None:
        if not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS:
            # 越界 fail-fast（AC 第 5 条：错误消息带变量名，同 main.py 口径）。
            msg = (
                f"{_TIMEOUT_ENV} must be within {MIN_TIMEOUT_MS}-{MAX_TIMEOUT_MS} ms, "
                f"got {timeout_ms!r}"
            )
            raise ValueError(msg)
        self._endpoint = endpoint if endpoint is not None else os.environ.get(_ENDPOINT_ENV) or None
        self._timeout_ms = timeout_ms
        self._transport = transport if transport is not None else _urllib_transport
        self._executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

    def review(
        self,
        *,
        text: str,
        intent: str | None,
        trace_id: str | None,
        house_id: str,
    ) -> RiskReviewVerdict:
        """同步审核草稿；超时/异常/坏响应一律归一为不通过，绝不抛。"""
        if self._endpoint is None:
            # fail-closed：任何环境不配置中台即不放行 LLM 内容。
            return RiskReviewVerdict(
                passed=False,
                outcome="error",
                latency_ms=0.0,
                reason="endpoint_not_configured",
            )
        payload: Mapping[str, Any] = {
            "text": text,
            "intent": intent,
            "trace_id": trace_id,
            "house_id": house_id,
        }
        started = _monotonic_ms()
        future = self._executor.submit(
            self._transport, self._endpoint, payload, self._timeout_ms / 1000
        )
        future.add_done_callback(_retrieve_future_exception)
        try:
            response = future.result(timeout=self._timeout_ms / 1000)
        except FutureTimeoutError:
            # 审核超时视为不通过；执行线程不傻等，归一为 timeout 归因。
            return RiskReviewVerdict(
                passed=False,
                outcome="timeout",
                latency_ms=_monotonic_ms() - started,
                reason=f"review exceeded {self._timeout_ms}ms",
            )
        except Exception as exc:
            # 传输层逃逸异常（连接错误等）：归一不通过，绝不向上抛。
            return RiskReviewVerdict(
                passed=False,
                outcome="error",
                latency_ms=_monotonic_ms() - started,
                reason=type(exc).__name__,
            )
        return _parse_response(response, latency_ms=_monotonic_ms() - started)


def _monotonic_ms() -> float:
    """单调时钟毫秒读数（latency_ms 口径）。"""
    return time.monotonic() * 1000


def _parse_response(response: RiskReviewHttpResponse, *, latency_ms: float) -> RiskReviewVerdict:
    """归一中台响应：非 200/坏 JSON/缺字段一律不通过（error 归因）。"""
    if response.status != 200:
        return RiskReviewVerdict(
            passed=False,
            outcome="error",
            latency_ms=latency_ms,
            reason=f"http_{response.status}",
        )
    try:
        data = json.loads(response.body)
    except ValueError:
        return RiskReviewVerdict(
            passed=False, outcome="error", latency_ms=latency_ms, reason="invalid_json"
        )
    if not isinstance(data, dict):
        return RiskReviewVerdict(
            passed=False, outcome="error", latency_ms=latency_ms, reason="invalid_body"
        )
    passed = data.get("pass")
    if not isinstance(passed, bool):
        return RiskReviewVerdict(
            passed=False, outcome="error", latency_ms=latency_ms, reason="missing_pass"
        )
    if passed:
        return RiskReviewVerdict(passed=True, outcome="passed", latency_ms=latency_ms)
    reason = data.get("reason")
    normalized = str(reason)[:_REASON_LIMIT] if reason is not None else None
    return RiskReviewVerdict(
        passed=False, outcome="rejected", latency_ms=latency_ms, reason=normalized
    )
