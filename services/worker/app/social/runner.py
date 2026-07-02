"""Due-post dispatcher — the orchestration a future Modal cron will call to fire scheduled posts.

DORMANT: nothing imports this yet and no cron is registered. It encodes the reliability rules
so they are unit-tested ahead of wiring:
  • idempotency — a non-pending post is never re-published
  • our-side per-platform daily cap — over cap → skipped, the platform is never called
  • rule #8 — a publish failure is recorded as ``failed`` with its real reason, never swallowed
  • batch isolation — one failing post does not abort the rest of the due batch

Dependencies are injected via Protocols so the logic is testable with fakes (no network/DB) and
the concrete PostgREST repo + per-platform posters can be wired in later without touching this.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.social.models import DAILY_CAPS, PostJob, PostStatus, SocialAccount


class SocialRepo(Protocol):
    """Persistence the runner needs (concrete impl: PostgREST, mirrors app.cloud_state)."""

    def due_posts(self, now: datetime) -> list[PostJob]: ...

    def get_account(self, account_id: str) -> SocialAccount: ...

    def published_count_today(self, account_id: str, now: datetime) -> int: ...

    def set_status(
        self,
        post_id: str,
        status: PostStatus,
        *,
        remote_id: str | None = None,
        error: str | None = None,
    ) -> None: ...


class Poster(Protocol):
    """Publishes one clip to one platform, returning the platform's id for the created post.

    The poster owns its platform-specific token handling (decrypt with ``token_key`` → refresh →
    resolve the clip URL → upload). It raises on any failure; the runner records that as failed.
    """

    def publish(
        self, *, account: SocialAccount, post: PostJob, token_key: str, now: datetime
    ) -> str: ...


def run_due_posts(now: datetime, *, repo: SocialRepo, poster: Poster, token_key: str) -> None:
    """Process every post whose ``due_at`` has arrived. One bad post never aborts the batch."""
    for post in repo.due_posts(now):
        process_due_post(post, repo=repo, poster=poster, now=now, token_key=token_key)


def process_due_post(
    post: PostJob, *, repo: SocialRepo, poster: Poster, now: datetime, token_key: str
) -> None:
    """Publish a single due post, enforcing idempotency, the daily cap, and loud failure."""
    if post.status != PostStatus.pending:
        return  # idempotency: already publishing/published/failed/skipped — never double-post

    cap = DAILY_CAPS[post.platform]
    if repo.published_count_today(post.account_id, now) >= cap:
        repo.set_status(
            post.id,
            PostStatus.skipped,
            error=f"daily cap {cap} reached for {post.platform.value}",
        )
        return

    repo.set_status(post.id, PostStatus.publishing)
    try:
        account = repo.get_account(post.account_id)
        remote_id = poster.publish(account=account, post=post, token_key=token_key, now=now)
    except Exception as e:  # noqa: BLE001 — record the real reason (rule #8) + isolate the batch
        repo.set_status(post.id, PostStatus.failed, error=str(e))
        return
    repo.set_status(post.id, PostStatus.published, remote_id=remote_id)
