"""Concrete YouTube Poster — turns a due PostJob into a scheduled YouTube upload.

Structurally implements ``app.social.runner.Poster``. Glues the pure youtube builders + token
crypto + R2 clip fetch behind injectable seams, so the orchestration is unit-tested while the
default seams do real I/O. Each clip is uploaded **private** with ``status.publishAt = due_at``,
so YouTube's native scheduler flips it public at the slot (survives Quip downtime, no extra cron).
DORMANT — nothing wires this yet; a future Modal cron will construct it from app.social.config.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.social import tokens, youtube
from app.social.models import PostJob, SocialAccount

_FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _download_clip(clip_ref: str) -> Path:
    """Resolve the stored clip marker to a live URL and stream it to a temp .mp4. Integration."""
    from app import storage

    url = storage.resolve_media_url(clip_ref)
    fd, name = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    dest = Path(name)
    with httpx.stream("GET", url, timeout=_FETCH_TIMEOUT) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest


@dataclass
class YouTubePoster:
    """Publishes a clip to a connected YouTube channel, scheduled at the post's ``due_at``."""

    client_id: str
    client_secret: str
    # Injectable seams (defaults do real I/O; tests pass fakes).
    refresh_fn: Callable[..., str] = youtube.refresh_access_token
    fetch_clip_fn: Callable[[str], Path] = _download_clip
    upload_fn: Callable[..., str] = youtube.insert_video

    def publish(
        self, *, account: SocialAccount, post: PostJob, token_key: str, now: datetime
    ) -> str:
        refresh_token = tokens.decrypt_token(account.refresh_token_enc, token_key)
        access_token = self.refresh_fn(
            refresh_token, client_id=self.client_id, client_secret=self.client_secret
        )
        video_path = self.fetch_clip_fn(post.clip_ref)
        resource: dict[str, Any] = youtube.build_video_resource(
            title=youtube.safe_title(post.caption),
            description=post.caption,
            privacy="private",
            publish_at=post.due_at,
        )
        return self.upload_fn(access_token=access_token, video_path=video_path, resource=resource)
