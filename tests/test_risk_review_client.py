"""风控中台审核客户端单测（Story 4.4，AC 第 2/5 条 + I/O 矩阵）。

替身子传输（不触达网络）覆盖 spec I/O & Edge-Case Matrix 审核行：
通过 / 显式拒绝（含 reason）/ 超时（不傻等）/ 异常（网络错、非 200、
坏 JSON、缺 pass 字段）/ endpoint 未配置（fail-closed）；外加超时配置
越界 fail-fast（错误消息带变量名）与请求载荷元数据口径。全部无网络。
"""

import time
from collections.abc import Mapping
from typing import Any

import pytest

from weiliao_agent.filters.risk_review.client import (
    MAX_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    RiskReviewClient,
    RiskReviewHttpResponse,
)


class _ScriptedTransport:
    """按剧本返回响应或抛错的传输替身；记录收到的调用参数。"""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, endpoint: str, payload: Mapping[str, Any], timeout_seconds: float
    ) -> RiskReviewHttpResponse:
        self.calls.append(
            {"endpoint": endpoint, "payload": dict(payload), "timeout": timeout_seconds}
        )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, RiskReviewHttpResponse)
        return item


def _response(body: str, status: int = 200) -> RiskReviewHttpResponse:
    return RiskReviewHttpResponse(status=status, body=body)


def _client(
    transport: _ScriptedTransport,
    *,
    endpoint: str = "http://risk-center.example/review",
    timeout_ms: int = 500,
) -> RiskReviewClient:
    return RiskReviewClient(endpoint, timeout_ms=timeout_ms, transport=transport)


def test_pass_response_returns_passed_verdict() -> None:
    transport = _ScriptedTransport([_response('{"pass": true}')])
    client = _client(transport)

    verdict = client.review(text="草稿", intent="PRICE_INQUIRY", trace_id="t-1", house_id="h-1")

    assert verdict.passed is True
    assert verdict.outcome == "passed"
    assert verdict.latency_ms >= 0
    assert verdict.reason is None
    # 审核协议载荷：text + intent + trace_id + house_id 元数据。
    sent = transport.calls[0]["payload"]
    assert sent == {
        "text": "草稿",
        "intent": "PRICE_INQUIRY",
        "trace_id": "t-1",
        "house_id": "h-1",
    }


def test_reject_response_returns_rejected_verdict_with_reason() -> None:
    transport = _ScriptedTransport([_response('{"pass": false, "reason": "违禁表述"}')])
    client = _client(transport)

    verdict = client.review(text="草稿", intent=None, trace_id=None, house_id="h-1")

    assert verdict.passed is False
    assert verdict.outcome == "rejected"
    assert verdict.reason == "违禁表述"


def test_reject_without_reason_normalizes_to_none() -> None:
    transport = _ScriptedTransport([_response('{"pass": false}')])
    client = _client(transport)

    verdict = client.review(text="草稿", intent=None, trace_id=None, house_id="h-1")

    assert verdict.outcome == "rejected"
    assert verdict.reason is None


@pytest.mark.parametrize(
    "response",
    [
        _response("not json at all"),
        _response("[1, 2, 3]"),
        _response('{"reason": "缺 pass 字段"}'),
        _response('{"pass": "yes"}'),
        _response('{"error": "boom"}', status=500),
        _response('{"pass": true}', status=404),
    ],
    ids=["invalid_json", "non_dict_body", "missing_pass", "non_bool_pass", "http_500", "http_404"],
)
def test_bad_response_normalizes_to_error_not_passed(response: RiskReviewHttpResponse) -> None:
    transport = _ScriptedTransport([response])
    client = _client(transport)

    verdict = client.review(text="草稿", intent=None, trace_id=None, house_id="h-1")

    assert verdict.passed is False
    assert verdict.outcome == "error"
    assert verdict.reason is not None


def test_transport_exception_normalizes_to_error_and_does_not_raise() -> None:
    transport = _ScriptedTransport([ConnectionRefusedError("connection refused")])
    client = _client(transport)

    verdict = client.review(text="草稿", intent=None, trace_id=None, house_id="h-1")

    assert verdict.passed is False
    assert verdict.outcome == "error"
    assert verdict.reason == "ConnectionRefusedError"


def test_slow_transport_times_out_without_waiting() -> None:
    """矩阵「审核超时」行：慢审核在调用侧超时归一，执行线程不傻等。"""

    def _slow_transport(
        endpoint: str, payload: Mapping[str, Any], timeout_seconds: float
    ) -> RiskReviewHttpResponse:
        time.sleep(0.6)  # 远超 timeout_ms=300；短睡避免拖慢 pytest 进程退出
        return _response('{"pass": true}')

    client = RiskReviewClient(
        "http://risk-center.example/review",
        timeout_ms=MIN_TIMEOUT_MS,
        transport=_slow_transport,
    )

    started = time.monotonic()
    verdict = client.review(text="草稿", intent=None, trace_id=None, house_id="h-1")
    elapsed = time.monotonic() - started

    assert verdict.passed is False
    assert verdict.outcome == "timeout"
    assert "300" in (verdict.reason or "")
    assert elapsed < 0.5  # 不傻等慢执行线程


def test_unconfigured_endpoint_fails_closed_without_calling_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEILIAO_RISK_REVIEW_ENDPOINT", raising=False)
    transport = _ScriptedTransport([])
    client = RiskReviewClient(None, timeout_ms=500, transport=transport)

    verdict = client.review(text="草稿", intent=None, trace_id=None, house_id="h-1")

    assert verdict.passed is False
    assert verdict.outcome == "error"
    assert verdict.reason == "endpoint_not_configured"
    assert transport.calls == []  # 未配置 endpoint 绝不触达传输层


@pytest.mark.parametrize("timeout_ms", [MIN_TIMEOUT_MS - 1, MAX_TIMEOUT_MS + 1])
def test_out_of_range_timeout_fails_fast_with_variable_name(timeout_ms: int) -> None:
    with pytest.raises(ValueError, match="WEILIAO_RISK_REVIEW_TIMEOUT_MS"):
        RiskReviewClient("http://risk-center.example/review", timeout_ms=timeout_ms)


def test_timeout_env_override_used_when_param_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """endpoint 缺省读 WEILIAO_RISK_REVIEW_ENDPOINT（组合根不显式传参路径）。"""
    monkeypatch.setenv("WEILIAO_RISK_REVIEW_ENDPOINT", "http://risk-center.example/env-review")
    transport = _ScriptedTransport([_response('{"pass": true}')])
    client = RiskReviewClient(timeout_ms=500, transport=transport)

    verdict = client.review(text="草稿", intent=None, trace_id=None, house_id="h-1")

    assert verdict.passed is True
    assert transport.calls[0]["endpoint"] == "http://risk-center.example/env-review"
