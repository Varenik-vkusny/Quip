"""Internal value objects for the dormant auto-posting backend.

NOT contract types — these never go through models.py→contract.json→types.ts codegen, so they
do not touch the web/worker shared schema (anti-drift stays green). Plain frozen dataclasses +
str enums, fully mypy-strict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Platform(StrEnum):
    youtube = "youtube"
    instagram = "instagram"
    tiktok = "tiktok"


class PostStatus(StrEnum):
    pending = "pending"  # scheduled, not yet due / not yet sent
    publishing = "publishing"  # claimed by the runner, upload in flight
    published = "published"  # live on the platform (remote_id set)
    failed = "failed"  # error recorded (rule #8) — needs attention/retry
    skipped = "skipped"  # deliberately not sent (e.g. our-side daily cap)


# Our-side SAFETY caps (posts per account per day) — defensive guards BELOW each platform's real
# ceiling so the scheduler never hammers an account. NOTE: YouTube's true limit is ~100 uploads/
# day PROJECT-WIDE (shared across ALL connected users), not per-account — at scale that needs a
# global daily counter; this per-account value is only a floor guard, not the project quota.
DAILY_CAPS: dict[Platform, int] = {
    Platform.youtube: 90,
    Platform.instagram: 25,
    Platform.tiktok: 15,
}


@dataclass(frozen=True)
class SocialAccount:
    """A connected social account; ``refresh_token_enc`` is Fernet-encrypted at rest."""

    id: str
    user_id: str
    platform: Platform
    refresh_token_enc: str
    external_id: str | None = None  # channel/account id on the platform
    display_name: str | None = None
    needs_reconnect: bool = False  # set when a refresh returns invalid_grant


@dataclass(frozen=True)
class PostJob:
    """One scheduled publication of one clip to one account at ``due_at``."""

    id: str
    user_id: str
    account_id: str
    platform: Platform
    clip_ref: str  # stored video_url marker (e.g. r2://job/clip_01_captioned.mp4)
    caption: str
    due_at: datetime
    status: PostStatus
    remote_id: str | None = None  # platform's id once published
    error: str | None = None
