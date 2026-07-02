"""Tests for app.social.youtube — PURE request builders + the refresh I/O seam.

Network calls are integration-only, but the request SHAPES and the policy invariants
(publishAt requires private, Shorts auto-qualify on aspect+duration, refresh never silently
returns a bad token) are pure/seam-injected and fully unit-tested here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from app.errors import JobError
from app.social import youtube

# ─────────────────────────── OAuth URL + bodies ───────────────────────────


def test_build_auth_url_forces_offline_consent_and_scopes() -> None:
    url = youtube.build_auth_url(
        client_id="cid.apps.googleusercontent.com",
        redirect_uri="https://quip.ink/api/social/youtube/callback",
        state="xyz",
    )
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"]  # required to receive a refresh_token
    assert q["prompt"] == ["consent"]  # force refresh_token re-issue
    assert q["state"] == ["xyz"]
    assert "youtube.upload" in q["scope"][0]


def test_refresh_body_has_grant_type_refresh_token() -> None:
    body = youtube.refresh_body(refresh_token="rt", client_id="cid", client_secret="sec")
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "rt"


def test_token_exchange_body_has_authorization_code_grant() -> None:
    body = youtube.token_exchange_body(
        code="abc", client_id="cid", client_secret="sec", redirect_uri="https://q/cb"
    )
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "abc"
    assert body["redirect_uri"] == "https://q/cb"


# ─────────────────────────── Shorts auto-qualification ───────────────────────────


@pytest.mark.parametrize(
    ("w", "h", "dur", "expected"),
    [
        (1080, 1920, 60.0, True),  # vertical, short → Short
        (1080, 1080, 30.0, True),  # square, short → Short
        (1920, 1080, 30.0, False),  # landscape → not a Short
        (1080, 1920, 200.0, False),  # vertical but > 3 min → not a Short
        (1080, 1920, 0.0, False),  # zero/unknown duration → not a Short
    ],
)
def test_is_short(w: int, h: int, dur: float, expected: bool) -> None:
    assert youtube.is_short(width=w, height=h, duration_sec=dur) is expected


# ─────────────────────────── videos.insert resource ───────────────────────────


def test_build_video_resource_default_is_private_with_kids_and_synthetic_flags() -> None:
    res = youtube.build_video_resource(title="My clip", description="desc")
    assert res["snippet"]["title"] == "My clip"
    assert res["status"]["privacyStatus"] == "private"
    assert "selfDeclaredMadeForKids" in res["status"]  # COPPA declaration is mandatory
    assert res["status"]["containsSyntheticMedia"] is True  # AI-altered disclosure


def test_publish_at_sets_iso_z_and_keeps_private() -> None:
    when = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    res = youtube.build_video_resource(title="t", privacy="private", publish_at=when)
    assert res["status"]["publishAt"] == "2026-07-01T12:00:00Z"
    assert res["status"]["privacyStatus"] == "private"


def test_publish_at_with_public_privacy_raises_not_silent() -> None:
    # YouTube ignores publishAt unless privacyStatus=private — refuse the silent no-op.
    when = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        youtube.build_video_resource(title="t", privacy="public", publish_at=when)


# ─────────────────────────── refresh_access_token (injected transport) ───────────────────────────


class _Resp:
    def __init__(self, status_code: int, payload: dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def test_refresh_access_token_returns_token_on_200() -> None:
    calls: list[Any] = []

    def fake_post(url: str, **kw: Any) -> _Resp:
        calls.append((url, kw))
        return _Resp(200, {"access_token": "ya29.NEW", "expires_in": 3599})

    tok = youtube.refresh_access_token(
        "rt", client_id="cid", client_secret="sec", http_post=fake_post
    )
    assert tok == "ya29.NEW"
    assert calls[0][0] == youtube.GOOGLE_TOKEN_URL


def test_refresh_access_token_raises_on_error_status() -> None:
    def fake_post(url: str, **kw: Any) -> _Resp:
        return _Resp(400, {"error": "invalid_grant"}, text='{"error":"invalid_grant"}')

    with pytest.raises(JobError):  # revoked/expired → loud failure, never a junk token
        youtube.refresh_access_token(
            "rt", client_id="cid", client_secret="sec", http_post=fake_post
        )


def test_refresh_access_token_raises_when_no_token_in_body() -> None:
    def fake_post(url: str, **kw: Any) -> _Resp:
        return _Resp(200, {"expires_in": 3599})  # 200 but missing access_token

    with pytest.raises(JobError):
        youtube.refresh_access_token(
            "rt", client_id="cid", client_secret="sec", http_post=fake_post
        )
