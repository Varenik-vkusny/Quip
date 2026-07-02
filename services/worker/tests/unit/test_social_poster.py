"""Tests for app.social.youtube_poster — the concrete YouTube Poster (deps injected, faked).

The poster glues decrypt→refresh→fetch-clip→build-resource→upload. Network/disk are injected
seams; we assert the load-bearing decisions: the refresh token is decrypted, the clip is
scheduled at its due_at as a PRIVATE upload (so YouTube's native publishAt fires), and a refresh
failure propagates (so the runner records it as failed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.errors import JobError
from app.social import tokens, youtube
from app.social.models import Platform, PostJob, PostStatus, SocialAccount
from app.social.youtube_poster import YouTubePoster


def test_safe_title_strips_brackets_truncates_and_falls_back() -> None:
    assert youtube.safe_title("a<b>c") == "abc"
    assert len(youtube.safe_title("x" * 200)) == 100
    assert youtube.safe_title("   ") == "Short"


def _post(when: datetime, caption: str = "hello #shorts") -> PostJob:
    return PostJob(
        id="p1",
        user_id="u1",
        account_id="a1",
        platform=Platform.youtube,
        clip_ref="r2://job/clip_01_captioned.mp4",
        caption=caption,
        due_at=when,
        status=PostStatus.pending,
    )


def test_publish_decrypts_refreshes_and_schedules_at_due_at(tmp_path: Path) -> None:
    key = tokens.generate_key()
    acc = SocialAccount(
        id="a1",
        user_id="u1",
        platform=Platform.youtube,
        refresh_token_enc=tokens.encrypt_token("REFRESH-TOK", key),
    )
    when = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    seen: dict[str, Any] = {}

    def fake_refresh(rt: str, *, client_id: str, client_secret: str) -> str:
        seen["rt"] = rt
        return "ACCESS-TOK"

    def fake_fetch(clip_ref: str) -> Path:
        seen["clip"] = clip_ref
        return tmp_path / "c.mp4"

    def fake_upload(*, access_token: str, video_path: Path, resource: dict[str, Any]) -> str:
        seen["access"] = access_token
        seen["resource"] = resource
        return "VIDEO-123"

    poster = YouTubePoster(
        client_id="cid",
        client_secret="sec",
        refresh_fn=fake_refresh,
        fetch_clip_fn=fake_fetch,
        upload_fn=fake_upload,
    )
    vid = poster.publish(account=acc, post=_post(when), token_key=key, now=when)

    assert vid == "VIDEO-123"
    assert seen["rt"] == "REFRESH-TOK"  # decrypted from the stored ciphertext
    assert seen["access"] == "ACCESS-TOK"
    assert seen["clip"] == "r2://job/clip_01_captioned.mp4"
    # scheduled, not posted immediately: private + native publishAt at the slot
    assert seen["resource"]["status"]["privacyStatus"] == "private"
    assert seen["resource"]["status"]["publishAt"] == "2026-07-01T12:00:00Z"


def test_refresh_failure_propagates(tmp_path: Path) -> None:
    key = tokens.generate_key()
    acc = SocialAccount(
        id="a1",
        user_id="u1",
        platform=Platform.youtube,
        refresh_token_enc=tokens.encrypt_token("RT", key),
    )

    def boom(rt: str, *, client_id: str, client_secret: str) -> str:
        raise JobError("social_youtube", "token refresh failed: 400 invalid_grant")

    when = datetime(2026, 7, 1, tzinfo=UTC)
    poster = YouTubePoster(client_id="cid", client_secret="sec", refresh_fn=boom)
    with pytest.raises(JobError):
        poster.publish(account=acc, post=_post(when), token_key=key, now=when)
