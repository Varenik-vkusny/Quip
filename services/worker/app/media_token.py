"""Scoped, short-lived, opaque media-тикеты (замена raw-Supabase-JWT в ?token=).

Media/export эндпоинты грузятся через ``<video src>`` / нативный download, где Authorization-
заголовок часто выставить нельзя → токен приходится класть в URL. Раньше туда шёл СЕССИОННЫЙ
Supabase JWT — полный аккаунт-credential, который утекал в access-логи и заголовок Referer.
Тикет ниже это чинит:
  • **SCOPED** — привязан к КОНКРЕТНОЙ джобе (``payload.job``): на чужой джобе бесполезен;
  • **short-lived** — ``exp`` ~5 минут (узкое окно утечки);
  • **opaque / self-contained** — HMAC-подпись сервера; НЕ несёт аккаунт-данных, клиентом не
    читается как credential.

Формат: ``b64url(json(payload)) + "." + b64url(hmac_sha256(secret, b64url_payload))``.
PURE (mint/verify на переданном ключе) — покрыто unit-тестами; секрет резолвится из env.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

DEFAULT_TTL = 1200  # сек (20 мин): переживает медленную отдачу/on-demand рендер → юзер не видит
# «expired» посреди скачивания (2026-07-11); тикет job-scoped + opaque → окно утечки всё ещё узкое
_DERIVE_LABEL = b"quip-media-token-v1"  # доменная метка деривации (см. media_token_secret)


class MediaTokenError(Exception):
    """Невалидный/просроченный/чужой тикет → 401/403 на уровне эндпоинта."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def media_token_secret() -> bytes | None:
    """Ключ подписи медиа-тикетов. Приоритет: явный ``MEDIA_TOKEN_SECRET``; иначе — СТАБИЛЬНЫЙ
    дериват из server-only секрета, который воркер и так грузит и НИКОГДА не шлёт клиенту
    (``SUPABASE_SERVICE_ROLE_KEY``, затем ``R2_SECRET_ACCESS_KEY``). ``None``, если НИ ОДНОГО нет
    → вызыватель решает: dev (auth off) пропустить подпись, прод — 500 (НЕ выдаём неподписанный/
    угадываемый тикет; правило №8 — без тихого фолбэка).

    Дериват (не сам секрет) через HMAC с фикс-меткой домена: раскрытие медиа-ключа НЕ раскрывает
    исходный service-role/R2-секрет.
    """
    explicit = os.environ.get("MEDIA_TOKEN_SECRET", "").strip()
    if explicit:
        return explicit.encode("utf-8")
    for env_name in ("SUPABASE_SERVICE_ROLE_KEY", "R2_SECRET_ACCESS_KEY"):
        base = os.environ.get(env_name, "").strip()
        if base:
            return hmac.new(base.encode("utf-8"), _DERIVE_LABEL, hashlib.sha256).digest()
    return None


def mint(job_id: str, secret: bytes, *, ttl: int = DEFAULT_TTL, now: int | None = None) -> str:
    """SCOPED тикет для джобы: ``payload={"job": job_id, "exp": now+ttl}`` + HMAC-SHA256-подпись."""
    exp = (now if now is not None else int(time.time())) + ttl
    payload = _b64url(json.dumps({"job": job_id, "exp": exp}, separators=(",", ":")).encode())
    sig = _b64url(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify(token: str, job_id: str, secret: bytes, *, now: int | None = None) -> None:
    """Проверить тикет: корректная подпись (constant-time) + ``payload.job == job_id`` + не истёк.

    Бросает ``MediaTokenError`` на ЛЮБОЙ провал (вызыватель → 401/403). Подпись сверяем ПЕРВОЙ
    (до парсинга payload) — не доверяем attacker-controlled телу до аутентификации.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError as e:
        raise MediaTokenError("malformed token") from e
    expected = _b64url(hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig_b64, expected):
        raise MediaTokenError("bad signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as e:  # noqa: BLE001 — битый payload = невалидный тикет (не тихий проглот)
        raise MediaTokenError("bad payload") from e
    if payload.get("job") != job_id:
        raise MediaTokenError("job scope mismatch")
    exp = payload.get("exp")
    cur = now if now is not None else int(time.time())
    if not isinstance(exp, int) or cur >= exp:
        raise MediaTokenError("expired")
