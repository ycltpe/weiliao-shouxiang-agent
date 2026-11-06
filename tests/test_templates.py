"""集中模板兜底库测试（Story 4.1）。"""

from typing import Any, cast, get_args

import pytest
import redis
from fastapi.testclient import TestClient

import weiliao_agent.adapters.http as http_module
import weiliao_agent.pipeline as pipeline_module
from weiliao_agent.adapters.http import create_app
from weiliao_agent.filters.base import Filter
from weiliao_agent.filters.templates import (
    AI_DISCLOSURE_PREFIX,
    render_fallback,
    render_placeholder,
)
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.models.intent import IntentTag
from weiliao_agent.pipeline import PipelineOrchestrator

INTENT_TAGS = get_args(IntentTag)
IMAGE_URL = "https://img.example/house-001/living-room.jpg"
ALBUM_URL = "https://album.example/house-001"
FORBIDDEN_TEXT = (
    "最",
    "第一",
    "价格可谈",
    "学区名额",
    "我是经纪人",
    "我是您的经纪人",
    "已为您记录",
    "已转达",
    "已索取",
    "已发送",
)


def _payload(intent: str = "PRICE_INQUIRY") -> dict[str, Any]:
    return {
        "session_id": "sess-001",
        "house_id": "house-001",
        "timestamp": "2026-11-06T10:00:00+08:00",
        "trace_id": "trace-001",
        "intent": intent,
    }


class _RaisingFilter(Filter):
    name = "risk_review"

    def process(self, envelope: Envelope) -> Envelope:
        msg = "review failed"
        raise RuntimeError(msg)


class _FailingSessionManager:
    def begin_turn(self, session_id: str, timestamp: Any) -> Any:
        msg = "Connection refused"
        raise redis.ConnectionError(msg)


def test_registry_covers_every_intent_tag_with_distinct_safe_copy() -> None:
    assert len(INTENT_TAGS) == 9
    generic = render_fallback(None)
    replies = [render_fallback(intent) for intent in INTENT_TAGS]

    assert len(set(replies)) == len(INTENT_TAGS)
    for reply in replies:
        assert reply.startswith(AI_DISCLOSURE_PREFIX)
        assert len(reply) > len(AI_DISCLOSURE_PREFIX)
        assert reply != generic
        assert all(forbidden not in reply for forbidden in FORBIDDEN_TEXT)


def test_unknown_is_distinct_from_generic_fallback() -> None:
    assert render_fallback("UNKNOWN") != render_fallback(None)


@pytest.mark.parametrize("intent", [None, cast(IntentTag, "NOT_AN_INTENT")])
def test_missing_or_runtime_unknown_intent_fails_closed_without_material_leak(
    intent: IntentTag | None,
) -> None:
    reply = render_fallback(intent, image_url=IMAGE_URL, album_url=ALBUM_URL)

    assert reply == render_fallback(None)
    assert reply.startswith(AI_DISCLOSURE_PREFIX)
    assert IMAGE_URL not in reply
    assert ALBUM_URL not in reply


def test_photo_request_with_image_returns_original_link() -> None:
    reply = render_fallback("PHOTO_REQUEST", image_url=IMAGE_URL)

    assert reply.startswith(AI_DISCLOSURE_PREFIX)
    assert "图片链接" in reply
    assert reply.endswith(IMAGE_URL)
    assert f"{IMAGE_URL}。" not in reply
    assert "相册链接" not in reply


@pytest.mark.parametrize("image_url", [None, " \n\t "])
def test_photo_request_with_album_only_returns_album_guidance(
    image_url: str | None,
) -> None:
    reply = render_fallback("PHOTO_REQUEST", image_url=image_url, album_url=ALBUM_URL)

    assert reply.startswith(AI_DISCLOSURE_PREFIX)
    assert "相册链接" in reply
    assert reply.endswith(ALBUM_URL)
    assert f"{ALBUM_URL}。" not in reply


def test_photo_request_prefers_image_over_album() -> None:
    reply = render_fallback("PHOTO_REQUEST", image_url=IMAGE_URL, album_url=ALBUM_URL)

    assert IMAGE_URL in reply
    assert ALBUM_URL not in reply


@pytest.mark.parametrize("material_field", ["image_url", "album_url"])
@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/photo.jpg",
        "javascript:alert(1)",
        "//example.com/photo.jpg",
        "https:///missing-host/photo.jpg",
        "https://example.com/photo with space.jpg",
        "https://example.com/photo\n.jpg",
        "https://example.com/\x00photo.jpg",
        "https://example.com/" + "a" * 2048,
    ],
)
def test_invalid_material_urls_are_treated_as_absent(
    material_field: str,
    url: str,
) -> None:
    reply = render_fallback("PHOTO_REQUEST", **{material_field: url})

    assert reply == render_fallback("PHOTO_REQUEST")
    assert url not in reply


@pytest.mark.parametrize("material_field", ["image_url", "album_url"])
@pytest.mark.parametrize("forbidden", FORBIDDEN_TEXT)
def test_material_urls_containing_forbidden_text_are_treated_as_absent(
    material_field: str,
    forbidden: str,
) -> None:
    url = f"https://example.com/{forbidden}"

    reply = render_fallback("PHOTO_REQUEST", **{material_field: url})

    assert reply == render_fallback("PHOTO_REQUEST")
    assert url not in reply


def test_invalid_image_url_falls_back_to_valid_album() -> None:
    reply = render_fallback(
        "PHOTO_REQUEST",
        image_url="javascript:alert(1)",
        album_url=ALBUM_URL,
    )

    assert "相册链接" in reply
    assert reply.endswith(ALBUM_URL)
    assert "javascript:alert(1)" not in reply


@pytest.mark.parametrize(
    ("image_url", "album_url"),
    [
        (None, None),
        ("", ""),
        (" \n\t ", "  "),
    ],
)
def test_photo_request_without_valid_material_returns_no_photo_copy(
    image_url: str | None,
    album_url: str | None,
) -> None:
    reply = render_fallback("PHOTO_REQUEST", image_url=image_url, album_url=album_url)

    assert reply == render_fallback("PHOTO_REQUEST")
    assert "暂时没有可用的房源图片或相册链接" in reply
    assert "已索取" not in reply
    assert "已发送" not in reply


@pytest.mark.parametrize("intent", [tag for tag in INTENT_TAGS if tag != "PHOTO_REQUEST"])
def test_non_photo_intents_ignore_material(intent: IntentTag) -> None:
    reply = render_fallback(intent, image_url=IMAGE_URL, album_url=ALBUM_URL)

    assert reply == render_fallback(intent)
    assert IMAGE_URL not in reply
    assert ALBUM_URL not in reply


def test_all_photo_variants_obey_copy_safety_constraints() -> None:
    replies = (
        render_fallback("PHOTO_REQUEST"),
        render_fallback("PHOTO_REQUEST", image_url=IMAGE_URL),
        render_fallback("PHOTO_REQUEST", album_url=ALBUM_URL),
        render_fallback(None),
    )

    for reply in replies:
        assert reply.startswith(AI_DISCLOSURE_PREFIX)
        assert all(forbidden not in reply for forbidden in FORBIDDEN_TEXT)


def test_placeholder_compatibility_wrapper_delegates_all_photo_states() -> None:
    for intent in (*INTENT_TAGS, None):
        assert render_placeholder(intent) == render_fallback(intent)
    assert render_placeholder("PHOTO_REQUEST", image_url=IMAGE_URL) == render_fallback(
        "PHOTO_REQUEST", image_url=IMAGE_URL
    )
    assert render_placeholder("PHOTO_REQUEST", album_url=ALBUM_URL) == render_fallback(
        "PHOTO_REQUEST", album_url=ALBUM_URL
    )


def test_pipeline_success_uses_render_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[IntentTag | None, str | None, str | None]] = []

    def spy(
        intent: IntentTag | None,
        image_url: str | None = None,
        album_url: str | None = None,
    ) -> str:
        calls.append((intent, image_url, album_url))
        return render_fallback(intent, image_url=image_url, album_url=album_url)

    monkeypatch.setattr(pipeline_module, "render_fallback", spy)
    result = PipelineOrchestrator(filters=[]).run(Envelope.model_validate(_payload()))

    assert result.degraded is False
    assert result.reply == render_fallback("PRICE_INQUIRY")
    assert calls == [("PRICE_INQUIRY", None, None)]


def test_pipeline_filter_failure_uses_render_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[IntentTag | None, str | None, str | None]] = []

    def spy(
        intent: IntentTag | None,
        image_url: str | None = None,
        album_url: str | None = None,
    ) -> str:
        calls.append((intent, image_url, album_url))
        return render_fallback(intent, image_url=image_url, album_url=album_url)

    monkeypatch.setattr(pipeline_module, "render_fallback", spy)
    result = PipelineOrchestrator(filters=[_RaisingFilter()]).run(
        Envelope.model_validate(_payload())
    )

    assert result.degraded is True
    assert result.failed_filter == "risk_review"
    assert result.reply == render_fallback("PRICE_INQUIRY")
    assert calls == [("PRICE_INQUIRY", None, None)]


def test_http_redis_failure_uses_render_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[IntentTag | None, str | None, str | None]] = []

    def spy(
        intent: IntentTag | None = None,
        image_url: str | None = None,
        album_url: str | None = None,
    ) -> str:
        calls.append((intent, image_url, album_url))
        return render_fallback(intent, image_url=image_url, album_url=album_url)

    monkeypatch.setattr(http_module, "render_fallback", spy)
    app = create_app(session_manager=_FailingSessionManager())  # type: ignore[arg-type]
    response = TestClient(app).post("/v1/messages", json=_payload())

    assert response.status_code == 200
    assert response.json()["degraded"] is True
    assert response.json()["trace_id"] == "trace-001"
    assert response.json()["reply"] == render_fallback("PRICE_INQUIRY")
    assert calls == [("PRICE_INQUIRY", None, None)]
