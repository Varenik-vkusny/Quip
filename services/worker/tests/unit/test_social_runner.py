"""Tests for app.social.runner — the due-post dispatcher orchestration (deps injected, faked).

This is where the reliability rules live, so it is unit-tested with fakes (no network/DB):
  • idempotency — a non-pending post is never re-published
  • our-side per-platform daily cap — over cap → skipped, poster NOT called
  • rule #8 — a poster failure is recorded as failed (loud), never swallowed
  • isolation — one failing post does not stop the rest of the batch
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.errors import JobError
from app.social import models, runner
from app.social.models import Platform, PostStatus

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _post(
    pid: str, *, status: PostStatus = PostStatus.pending, platform: Platform = Platform.youtube
) -> models.PostJob:
    return models.PostJob(
        id=pid,
        user_id="u1",
        account_id="acc1",
        platform=platform,
        clip_ref="r2://job1/clip_01_captioned.mp4",
        caption="hi #shorts",
        due_at=NOW,
        status=status,
    )


@dataclass
class FakeRepo:
    posts: list[models.PostJob]
    published_today: int = 0
    status_calls: list[tuple[str, PostStatus, Any]] = field(default_factory=list)

    def due_posts(self, now: datetime) -> list[models.PostJob]:
        return [p for p in self.posts if p.status == PostStatus.pending and p.due_at <= now]

    def get_account(self, account_id: str) -> models.SocialAccount:
        return models.SocialAccount(
            id=account_id, user_id="u1", platform=Platform.youtube, refresh_token_enc="enc"
        )

    def published_count_today(self, account_id: str, now: datetime) -> int:
        return self.published_today

    def set_status(
        self,
        post_id: str,
        status: PostStatus,
        *,
        remote_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.status_calls.append((post_id, status, remote_id or error))


@dataclass
class FakePoster:
    platform: Platform = Platform.youtube
    raises: bool = False
    remote_id: str = "vid123"
    published: list[str] = field(default_factory=list)

    def publish(
        self, *, account: models.SocialAccount, post: models.PostJob, token_key: str, now: datetime
    ) -> str:
        if self.raises:
            raise JobError("social_youtube", "token refresh failed: 400 invalid_grant")
        self.published.append(post.id)
        return self.remote_id


def test_happy_path_publishes_and_marks_published() -> None:
    repo = FakeRepo(posts=[_post("p1")])
    poster = FakePoster()
    runner.run_due_posts(NOW, repo=repo, poster=poster, token_key="k")
    assert poster.published == ["p1"]
    assert (("p1", PostStatus.publishing, None)) in repo.status_calls
    assert (("p1", PostStatus.published, "vid123")) in repo.status_calls


def test_non_pending_post_is_not_republished() -> None:
    repo = FakeRepo(posts=[])
    poster = FakePoster()
    runner.process_due_post(
        _post("p1", status=PostStatus.published), repo=repo, poster=poster, now=NOW, token_key="k"
    )
    assert poster.published == []  # idempotency: already done
    assert repo.status_calls == []


def test_over_daily_cap_skips_without_publishing() -> None:
    cap = models.DAILY_CAPS[Platform.youtube]
    repo = FakeRepo(posts=[_post("p1")], published_today=cap)  # already at the cap
    poster = FakePoster()
    runner.run_due_posts(NOW, repo=repo, poster=poster, token_key="k")
    assert poster.published == []
    assert repo.status_calls[-1][1] == PostStatus.skipped


def test_poster_failure_is_recorded_not_swallowed() -> None:
    repo = FakeRepo(posts=[_post("p1")])
    poster = FakePoster(raises=True)
    runner.run_due_posts(NOW, repo=repo, poster=poster, token_key="k")
    failed = [c for c in repo.status_calls if c[1] == PostStatus.failed]
    assert len(failed) == 1
    assert "invalid_grant" in str(failed[0][2])  # the real reason is persisted, not lost


def test_one_failure_does_not_stop_the_batch() -> None:
    repo = FakeRepo(posts=[_post("p1"), _post("p2"), _post("p3")])

    class Selective(FakePoster):
        def publish(
            self, *, account: Any, post: models.PostJob, token_key: str, now: datetime
        ) -> str:
            if post.id == "p2":
                raise JobError("social_youtube", "upload bytes failed: 503")
            self.published.append(post.id)
            return self.remote_id

    poster = Selective()
    runner.run_due_posts(NOW, repo=repo, poster=poster, token_key="k")
    assert poster.published == ["p1", "p3"]  # p2 failed but p1/p3 still went out
    assert ("p2", PostStatus.failed) in [(c[0], c[1]) for c in repo.status_calls]
