"""PostgREST persistence for the dormant auto-posting backend (mirrors app.cloud_state).

PURE row→model mappers + the UTC day boundary (used for the per-account daily cap) are
unit-tested; the httpx wrappers are thin I/O (integration, like app.cloud_state). Structurally
implements the ``app.social.runner.SocialRepo`` protocol. Uses the same env as cloud_state
(SUPABASE_URL + service_role); inert until the worker is configured. Tables: migration 0015.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.errors import JobError
from app.social.models import Platform, PostJob, PostStatus, SocialAccount

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


def _base() -> str:
    return f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1"


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    return headers


# ─────────────────────────── PURE helpers (tested) ───────────────────────────


def _parse_dt(value: str) -> datetime:
    """Parse a PostgREST timestamptz (``...+00:00`` or ``...Z``) → aware UTC. PURE."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def day_start_utc(now: datetime) -> datetime:
    """Midnight UTC of ``now``'s day — the lower bound for 'published today' counting. PURE."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def row_to_account(row: dict[str, Any]) -> SocialAccount:
    """Map a ``social_accounts`` row to a SocialAccount. PURE."""
    return SocialAccount(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        platform=Platform(row["platform"]),
        refresh_token_enc=str(row["refresh_token_enc"]),
        external_id=row.get("external_id"),
        display_name=row.get("display_name"),
        needs_reconnect=bool(row.get("needs_reconnect", False)),
    )


def row_to_post(row: dict[str, Any]) -> PostJob:
    """Map a ``post_jobs`` row to a PostJob. PURE."""
    return PostJob(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        account_id=str(row["account_id"]),
        platform=Platform(row["platform"]),
        clip_ref=str(row["clip_ref"]),
        caption=str(row.get("caption") or ""),
        due_at=_parse_dt(str(row["due_at"])),
        status=PostStatus(row["status"]),
        remote_id=row.get("remote_id"),
        error=row.get("error"),
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# ─────────────────────────── thin httpx I/O (integration) ───────────────────────────


class PostgrestSocialRepo:
    """Concrete SocialRepo over Supabase PostgREST (service_role bypasses RLS)."""

    def due_posts(self, now: datetime) -> list[PostJob]:
        r = httpx.get(
            f"{_base()}/post_jobs",
            params={
                "status": "eq.pending",
                "due_at": f"lte.{_iso(now)}",
                "order": "due_at.asc",
                "select": "*",
            },
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return [row_to_post(x) for x in r.json()]

    def get_account(self, account_id: str) -> SocialAccount:
        r = httpx.get(
            f"{_base()}/social_accounts",
            params={"id": f"eq.{account_id}", "select": "*"},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            raise JobError("social_repo", f"social account {account_id} not found")
        return row_to_account(rows[0])

    def published_count_today(self, account_id: str, now: datetime) -> int:
        r = httpx.get(
            f"{_base()}/post_jobs",
            params={
                "account_id": f"eq.{account_id}",
                "status": "eq.published",
                "updated_at": f"gte.{_iso(day_start_utc(now))}",
                "select": "id",
            },
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows: list[Any] = r.json()
        return len(rows)

    def set_status(
        self,
        post_id: str,
        status: PostStatus,
        *,
        remote_id: str | None = None,
        error: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"status": status.value}
        if remote_id is not None:
            body["remote_id"] = remote_id
        if error is not None:
            body["error"] = error
        r = httpx.patch(
            f"{_base()}/post_jobs",
            params={"id": f"eq.{post_id}"},
            headers=_headers({"Prefer": "return=minimal"}),
            json=body,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
