"""Tests for app.social.repo PURE helpers (PostgREST row mapping + day boundary).

The httpx wrappers are thin I/O (integration, like app.cloud_state); the row→model mappers and
the UTC day-start used for the per-account daily cap are pure and unit-tested here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.social import repo
from app.social.models import Platform, PostStatus


def test_row_to_account_maps_fields() -> None:
    row = {
        "id": "a1",
        "user_id": "u1",
        "platform": "youtube",
        "refresh_token_enc": "enc",
        "external_id": "ch1",
        "display_name": "My Channel",
        "needs_reconnect": False,
    }
    acc = repo.row_to_account(row)
    assert acc.id == "a1"
    assert acc.platform is Platform.youtube
    assert acc.refresh_token_enc == "enc"
    assert acc.external_id == "ch1"


def test_row_to_post_maps_fields_and_parses_due_at() -> None:
    row = {
        "id": "p1",
        "user_id": "u1",
        "account_id": "a1",
        "platform": "youtube",
        "clip_ref": "r2://job/clip.mp4",
        "caption": "hi",
        "due_at": "2026-07-01T12:00:00+00:00",
        "status": "pending",
    }
    p = repo.row_to_post(row)
    assert p.id == "p1"
    assert p.status is PostStatus.pending
    assert p.platform is Platform.youtube
    assert p.due_at == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_row_to_post_parses_z_suffix() -> None:
    row = {
        "id": "p2",
        "user_id": "u1",
        "account_id": "a1",
        "platform": "youtube",
        "clip_ref": "r2://x",
        "caption": "",
        "due_at": "2026-07-01T12:00:00Z",
        "status": "published",
    }
    assert repo.row_to_post(row).due_at == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_day_start_utc_floors_to_midnight() -> None:
    now = datetime(2026, 7, 1, 15, 30, 45, tzinfo=UTC)
    assert repo.day_start_utc(now) == datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
