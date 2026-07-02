"""YouTube Data API v3 client: OAuth + resumable videos.insert + native publishAt scheduling.

A "Short" is just an ordinary ``videos.insert`` upload that YouTube auto-classifies from
aspect ratio + duration — there is no Shorts endpoint. Native scheduling works via
``status.publishAt`` (only when ``privacyStatus='private'``). videos.insert costs 1 unit of
its quota bucket; the connected user must already have a channel.

Design: PURE request builders (auth URL, token bodies, the video resource, Shorts check) are
fully unit-tested; the HTTP wrappers take an injectable ``http_post`` so the refresh/exchange
contract is testable without the network. Real resumable byte upload is integration-only.
Rule #8: every failure raises a JobError carrying the HTTP status + body snippet (the LinkedIn
postmortem lesson — log the full response, never swallow).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.errors import JobError

GOOGLE_AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — public OAuth endpoint, not a secret
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
# Minimal scope set (Google rewards least-privilege at verification). readonly lets us read the
# connected channel id/title so the user can confirm which channel they linked.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
# Shorts auto-qualify at <= 3 minutes (since Oct 2024) + vertical/square aspect.
SHORTS_MAX_SEC = 180.0

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


# ─────────────────────────── PURE: OAuth URL + token bodies ───────────────────────────


def build_auth_url(
    *, client_id: str, redirect_uri: str, state: str, scopes: Sequence[str] = SCOPES
) -> str:
    """Google consent URL. ``access_type=offline`` + ``prompt=consent`` are REQUIRED to receive
    a refresh_token (Google only re-issues it when consent is forced). PURE."""
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_BASE}?{urlencode(params)}"


def token_exchange_body(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict[str, str]:
    """POST body to exchange an auth code for tokens. PURE."""
    return {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }


def refresh_body(*, refresh_token: str, client_id: str, client_secret: str) -> dict[str, str]:
    """POST body to mint a fresh access token from a stored refresh token. PURE."""
    return {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    }


# ─────────────────────────── PURE: Shorts check + video resource ───────────────────────────


def is_short(*, width: int, height: int, duration_sec: float) -> bool:
    """True ⇔ YouTube auto-classifies this as a Short (vertical/square, <=3 min). PURE."""
    if width <= 0 or height <= 0 or duration_sec <= 0:
        return False
    return height >= width and duration_sec <= SHORTS_MAX_SEC


def format_publish_at(dt: datetime) -> str:
    """ISO-8601 UTC with a trailing 'Z' (the format status.publishAt expects). PURE."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def safe_title(text: str) -> str:
    """A YouTube-safe video title: strip <>, trim, cap at 100 chars, fallback 'Short'. PURE."""
    cleaned = text.replace("<", "").replace(">", "").strip()[:100]
    return cleaned or "Short"


def build_video_resource(
    *,
    title: str,
    description: str = "",
    privacy: str = "private",
    publish_at: datetime | None = None,
    tags: Sequence[str] | None = None,
    category_id: str = "22",  # People & Blogs
    made_for_kids: bool = False,
    contains_synthetic_media: bool = True,  # Quip clips are AI-edited → disclose
) -> dict[str, Any]:
    """The ``videos.insert`` request body (snippet + status). PURE.

    Rule #8: ``publish_at`` with a non-private privacy raises — YouTube silently ignores
    publishAt unless privacyStatus='private', and a silently-unscheduled upload would post
    immediately instead of at the user's slot.
    """
    if publish_at is not None and privacy != "private":
        raise ValueError(
            "publishAt requires privacyStatus='private' (YouTube ignores it otherwise)"
        )
    snippet: dict[str, Any] = {
        "title": title,
        "description": description,
        "categoryId": category_id,
    }
    if tags:
        snippet["tags"] = list(tags)
    status: dict[str, Any] = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": made_for_kids,  # COPPA: must be declared per upload
        "containsSyntheticMedia": contains_synthetic_media,
    }
    if publish_at is not None:
        status["publishAt"] = format_publish_at(publish_at)
    return {"snippet": snippet, "status": status}


# ─────────── I/O: token exchange / refresh (seam-injected) ───────────


def _require(payload: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if not payload.get(k)]
    if missing:
        raise JobError("social_youtube", f"OAuth response missing {missing}: {payload}")


def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    http_post: Callable[..., Any] = httpx.post,
) -> dict[str, Any]:
    """Exchange the consent ``code`` for access + refresh tokens. Raises loudly on error."""
    resp = http_post(
        GOOGLE_TOKEN_URL,
        data=token_exchange_body(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        ),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise JobError(
            "social_youtube", f"code exchange failed: {resp.status_code} {resp.text[:300]}"
        )
    data: dict[str, Any] = resp.json()
    _require(data, "access_token", "refresh_token")
    return data


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str,
    client_secret: str,
    http_post: Callable[..., Any] = httpx.post,
) -> str:
    """Mint a fresh access token from a stored refresh token. Raises on error (never a junk token).

    invalid_grant here means the user revoked access or the token lapsed → the caller flags the
    connection 'needs reconnect' and pauses that schedule, instead of posting with a dead token.
    """
    resp = http_post(
        GOOGLE_TOKEN_URL,
        data=refresh_body(
            refresh_token=refresh_token, client_id=client_id, client_secret=client_secret
        ),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise JobError(
            "social_youtube", f"token refresh failed: {resp.status_code} {resp.text[:300]}"
        )
    data: dict[str, Any] = resp.json()
    token = data.get("access_token")
    if not token:
        raise JobError("social_youtube", f"token refresh: no access_token in response: {data}")
    return str(token)


# ─────────────────────────── I/O: resumable upload (integration-only) ───────────────────────────


def insert_video(*, access_token: str, video_path: Path, resource: dict[str, Any]) -> str:
    """Resumable ``videos.insert``: init session → PUT bytes → return the new videoId.

    Integration-only (real HTTP + bytes); the request SHAPES it relies on are unit-tested via
    the pure builders above. Clips are small (a few MB) so a single PUT is fine. Costs 1 unit
    of the videos.insert quota bucket. JobError on any non-2xx (rule #8)."""
    size = video_path.stat().st_size
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size),
        "X-Upload-Content-Type": "video/*",
    }
    try:
        with httpx.Client(timeout=_UPLOAD_TIMEOUT) as client:
            init = client.post(
                f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
                headers=init_headers,
                content=json.dumps(resource),
            )
            if init.status_code not in (200, 201):
                raise JobError(
                    "social_youtube", f"upload init failed: {init.status_code} {init.text[:300]}"
                )
            session_uri = init.headers.get("location") or init.headers.get("Location")
            if not session_uri:
                raise JobError("social_youtube", "upload init returned no session Location header")
            put = client.put(
                session_uri,
                headers={"Content-Type": "video/*", "Content-Length": str(size)},
                content=video_path.read_bytes(),
            )
            if put.status_code not in (200, 201):
                raise JobError(
                    "social_youtube", f"upload bytes failed: {put.status_code} {put.text[:300]}"
                )
            video_id = put.json().get("id")
            if not video_id:
                raise JobError("social_youtube", f"upload ok but no videoId: {put.text[:300]}")
            return str(video_id)
    except JobError:
        raise
    except Exception as e:  # noqa: BLE001 — wrap any transport error, never swallow (rule #8)
        raise JobError("social_youtube", f"videos.insert failed: {e}") from e
