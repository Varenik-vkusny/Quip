"""FastAPI-входная точка воркера ClipFlow (этап J).

POST /jobs → создать задачу (BackgroundTask → tasks.run_pipeline_job), GET /jobs/{id} →
статус из SQLite (переживает рестарт), GET /healthz. Файлы клипов раздаются на /media.
CORS открыт для web (localhost:3000). Логика пайплайна — в app/pipeline/*, сюда не течёт.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__, auth, billing, db, dedup, dispatch, fast_export, media_token, storage
from app.config import bootstrap_env, get_settings

# Локально активируем auth/billing-гейты из .env (как env-vars в проде). Под pytest НЕ
# грузим — тесты держат dual-mode (open) через monkeypatch отдельных переменных.
if "pytest" not in sys.modules:
    bootstrap_env()
from app.editor import presets as presets_mod
from app.editor import store, style_prefs
from app.editor.captions_v2 import compile_ass, compile_srt
from app.editor.interchange import (
    HandoffClip,
    HandoffInterval,
    HandoffMarker,
    build_edl,
    build_fcpxml,
    build_manifest_json,
    build_markers_csv,
    build_vtt,
)
from app.editor.ops import (
    add_manual_cut,
    add_section,
    add_suppressed_cut,
    apply_extend,
    apply_trim,
    clear_crop_overrides,
    remove_manual_cut,
    remove_suppressed_cut,
    replace_edit_content,
    set_crop_override,
    set_interval,
)
from app.editor.store import EditConflict
from app.editor.timeline import build_timeline_data
from app.editor.timemap import ClipTimeMap
from app.errors import JobError
from app.models import (
    AgentEvent,
    AgentRun,
    CaptionPreset,
    CaptionStyle,
    CaptionTrack,
    ClipOut,
    CropOverride,
    HighlightStyle,
    RefreshClipBody,
    SilenceConfig,
    SourceInterval,
)
from app.pipeline.stage5_render import aspect_to_dims
from app.run import DATA_ROOT
from app.tasks import (
    render_clip_edit_job,
    render_edit_to_file,
    render_extracted_clip_job,
    run_pipeline_job,
    run_upload_job,
    run_upload_job_from_r2,
    segment_bounds,
)

_UPLOAD_CHUNK = 1024 * 1024  # 1 МБ потоковой записи (не держим весь файл в памяти)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    yield


app = FastAPI(title="clipflow-worker", version=__version__, lifespan=lifespan)

# CORS: прод-домены quip.ink / www.quip.ink / app.quip.ink (апекс переехал на проект
# quip-app, см. docs/SEO_STRATEGY.md §4) + любые vercel-превью + локальный dev. Регулярка
# (не список), т.к. preview-домены Vercel динамические. Bearer-токен в заголовке (не cookie).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://((app\.|www\.)?quip\.ink|([a-z0-9-]+\.)*vercel\.app)|http://localhost:3000",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(httpx.TransportError)
def _upstream_transport_error(_request: Request, exc: httpx.TransportError) -> JSONResponse:
    """Сетевой сбой К UPSTREAM'у (Supabase/PostgREST) → 503 «попробуй ещё», а НЕ 500.

    Прод-инцидент 2026-07-13: под нагрузкой (concurrency=80 на 2 vCPU, занятых CV) TLS-handshake к
    Supabase не укладывался в connect-бюджет → ``httpcore.ConnectTimeout: SSL handshake timed out``
    / ``ReadTimeout``. Ни один модуль их не ловил и глобального обработчика не было → любой блип
    БД становился жёстким 500 у пользователя. 500 = «сервер сломан» (клиент не ретраит); 503 +
    Retry-After = «занят, повтори» — честная семантика transient-сбоя.

    Это BACKSTOP, а не глушилка: транспорт уже ретраится с backoff'ом в app.http_client. Сюда
    доходит только ИСЧЕРПАВШИЙ ретраи сбой — и он остаётся ВИДИМЫМ (лог + явный 503, правило №8:
    без тихих фолбэков). Ловим httpx.TransportError — базовый класс Connect/Read/Write/Pool-
    таймаутов и сетевых/протокольных ошибок; HTTPStatusError (4xx/5xx) НЕ ловим — это не транспорт.
    """
    print(f"[upstream] transport error → 503: {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=503,
        content={"detail": "upstream temporarily unavailable (try again shortly)"},
        headers={"Retry-After": "2"},
    )


# Раздача артефактов: data/<job_id>/clips/<clip>.mp4 → /media/<job_id>/clips/<clip>.mp4
DATA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(DATA_ROOT)), name="media")


class CreateJobBody(BaseModel):
    source_type: str
    source_ref: str
    max_clips: int | None = Field(default=None, ge=1, le=30)  # UI-степпер; None → дефолт воркера
    language: str | None = None  # ISO-код языка (kk=казахский→Groq; None=авто-детект Deepgram)
    clip_min_sec: int | None = Field(default=None, ge=3, le=180)
    clip_max_sec: int | None = Field(default=None, ge=10, le=180)
    remove_pauses: bool = False  # global «убирать паузы» (opt-in) → сидит silence на каждый клип


@app.get("/healthz")
def healthz() -> dict[str, bool | str]:
    """Liveness-проба. Выход: ``{"ok": True, "version": "..."}``. Без auth."""
    return {"ok": True, "version": __version__}


def _billing_enabled() -> bool:
    """Включён ли гейт квоты. Читаем env напрямую (не get_settings) → не триггерим
    валидацию ключей провайдеров и оставляем гейт инертным в тестах/без секретов."""
    return os.environ.get("BILLING_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _resolve_user(authorization: str | None, x_user_id: str | None) -> str | None:
    """user_id запроса. Auth включён (SUPABASE_URL задан) → из проверенного Supabase-JWT
    (401 при отсутствии/невалиде токена). Иначе dual-mode dev → заголовок X-User-Id (или None).

    ⚠️ Заменяет прежний плейсхолдер X-User-Id настоящей валидацией JWT (JWKS проекта).
    """
    if auth.supabase_auth_enabled():
        try:
            return auth.resolve_user_id(authorization)
        except auth.AuthError as e:
            raise HTTPException(status_code=401, detail=f"unauthorized: {e}") from e
    return x_user_id


def _enforce_owner(job_id: str, user_id: str | None) -> dict[str, Any]:
    """Общий предикат ownership-гейта (IDOR-защита). Активен ТОЛЬКО при включённом auth
    (SUPABASE_URL): грузит строку джобы (404, если нет) и рейзит 403, если её владелец — НЕ
    текущий юзер. Legacy/ничьи джобы (owner=None) не привязаны → bypass (dual-mode, как в
    cancel_job). Без auth (локалка/тесты) — pass-through: by-job эндпоинты редактора читают
    артефакты с диска БЕЗ строки джобы, защищать некого → пустой dict (вызыватель на строку
    не полагается). Так продовый гейт не ломает dev-режим и существующие тесты."""
    if not auth.supabase_auth_enabled():
        return {}
    row = db.get_job_row(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if row.get("user_id") not in (None, user_id):
        raise HTTPException(status_code=403, detail="not your job")
    return row


def _require_owned_job(
    job_id: str, authorization: str | None, x_user_id: str | None
) -> tuple[dict[str, Any], str | None]:
    """Ownership-гейт для JSON by-job/clip эндпоинтов (Authorization header). Резолвит юзера
    (401 при отсутствии/невалиде токена, когда auth включён), энфорсит владельца (404/403),
    возвращает (строка джобы, user_id)."""
    user_id = _resolve_user(authorization, x_user_id)
    return _enforce_owner(job_id, user_id), user_id


def _require_media_access(
    job_id: str, authorization: str | None, x_user_id: str | None, mt: str | None
) -> None:
    """Ownership-гейт для media/export (грузятся через ``<video src>`` / нативный download).

    Доступ, если выполнено ЛЮБОЕ из:
      • валидный Authorization-header владельца джобы (fetch с заголовком) — обычный owner-гейт;
      • валидный SCOPED медиа-тикет ``?mt=`` (HMAC сервера, ``payload.job == job_id``, не истёк).
    Иначе 401/403. СЕССИОННЫЙ Supabase-JWT в URL БОЛЬШЕ НЕ принимаем — он утекал в access-логи/
    Referer (полный аккаунт-credential); ``?mt=`` — короткоживущий, привязанный к джобе, opaque.
    При ВЫКЛЮЧЕННОМ auth — pass-through (dev/локалка: media-эндпоинты и так открыты).
    """
    if not auth.supabase_auth_enabled():
        return
    # Header-путь: если заголовок прислан — он авторитетен (обычный owner-гейт: 401/403).
    if authorization is not None:
        _require_owned_job(job_id, authorization, x_user_id)
        return
    # Иначе — только по scoped-тикету (raw-JWT в URL не принимаем).
    if not mt:
        raise HTTPException(status_code=401, detail="missing media ticket")
    secret = media_token.media_token_secret()
    if secret is None:
        # Прод с auth, но нет ключа подписи → тикет проверить нечем: 500 (НЕ пропускаем молча).
        raise HTTPException(status_code=500, detail="media token signing key unavailable")
    try:
        media_token.verify(mt, job_id, secret)
    except media_token.MediaTokenError as e:
        raise HTTPException(status_code=403, detail=f"invalid media ticket: {e}") from e


def _no_referrer(resp: Response) -> Response:
    """``Referrer-Policy: no-referrer`` на media/export-ответ: URL с ``?mt=`` не утекает в Referer
    к третьим сторонам (redirect на R2 / встроенные ресурсы)."""
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _enforce_free_identity(authorization: str | None, user_id: str | None) -> None:
    """Анти-абьюз free-плана: verified email + не одноразовый домен (серверно, авторитетно).

    Бесплатный план (1 видео) абьюзят пачками непроверенных/одноразовых ящиков. Гейт активен
    при включённых auth (SUPABASE_URL) И billing; ТОЛЬКО для FREE-плана — платные мимо (заплатил
    → не абьюз). Никогда не доверяем клиенту: email/verified берём из проверенного JWT (а где
    неоднозначно — из админ-lookup email_confirmed_at). Ошибки lookup всплывают (502), не «молча
    разрешить». Google OAuth уже verified Google → не блокируется (email_is_verified → True).

    Отказ 403 с actionable-сообщением: непроверенный email → «verify your inbox»; одноразовый
    домен → «use a real email». Зеркальная UX-валидация — на фронте (AuthForm), сервер — авторитет.
    """
    if not user_id or not _billing_enabled() or not auth.supabase_auth_enabled():
        return
    profile = db.get_profile(user_id)
    if billing.resolve_plan(profile["plan"]).id != "free":
        return  # платный план — анти-абьюз free-гейт не применяется
    try:
        # Сюда не должны дойти (quota-гейт уже валидировал токен), но без тихого пропуска.
        claims = auth.resolve_claims(authorization)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=f"unauthorized: {e}") from e
    email = auth.email_from_claims(claims)
    if email and billing.is_disposable_email(email):
        raise HTTPException(
            status_code=403,
            detail=(
                "Disposable email addresses aren't allowed on the free plan. "
                "Please sign up with a real email."
            ),
        )
    try:
        verified = auth.email_is_verified(claims)
    except Exception as e:  # noqa: BLE001 — админ-lookup упал → видимая ошибка, НЕ молчаливый пропуск
        raise HTTPException(status_code=502, detail=f"could not verify email status: {e}") from e
    if not verified:
        raise HTTPException(
            status_code=403,
            detail=(
                "Verify your email to use the free plan — check your inbox for the "
                "confirmation link, then try again."
            ),
        )


def _enforce_quota(user_id: str | None) -> None:
    """Create-time гейт: быстрый отказ, если месячные минуты + PAYG ПОЛНОСТЬЮ исчерпаны.

    Длину видео тут НЕ знаем (duration известна только после probe) → реальный лимит по
    длине применяется ПОСЛЕ импорта (``_quota_gate_after_probe`` в tasks.py, до транскрипции).
    No-op без BILLING_ENABLED/user_id. НИЧЕГО не списывает — списание только по факту
    готовых клипов (record_usage после set_done).
    """
    if not user_id or not _billing_enabled():
        return
    profile = db.get_profile(user_id)
    used = db.get_monthly_usage(user_id, billing.current_month())
    plan = billing.resolve_plan(profile["plan"])
    monthly_remaining = billing.plan_monthly_minutes(plan) - float(used["minutes"])
    payg_minutes = int(profile["payg_credits"]) * billing.MINUTES_PER_VIDEO
    if monthly_remaining <= 0 and payg_minutes <= 0:
        raise HTTPException(
            status_code=402,
            detail=(
                f"You're out of minutes on {plan.name} this month. "
                f"Upgrade your plan or top up to keep creating clips."
            ),
        )


@app.get("/usage")
def get_usage(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Живой расход для UsageMeter: план + остаток видео-кредитов в этом месяце + PAYG.

    Auth включён → данные пользователя из profiles/usage_events. Без юзера (dev) → дефолт free.
    """
    user_id = _resolve_user(authorization, x_user_id)
    plan = billing.resolve_plan(db.get_profile(user_id)["plan"] if user_id else "free")
    used_minutes = (
        float(db.get_monthly_usage(user_id, billing.current_month())["minutes"]) if user_id else 0.0
    )
    payg_videos = int(db.get_profile(user_id)["payg_credits"]) if user_id else 0
    return _usage_payload(plan, used_minutes, payg_videos)


def _usage_payload(
    plan: billing.PlanLimits, used_minutes: float, payg_videos: int
) -> dict[str, Any]:
    """Сборка ответа /usage в МИНУТАХ и «видео» (= минуты/60) для UsageMeter. PURE-ish."""
    monthly_minutes = billing.plan_monthly_minutes(plan)
    remaining_minutes = max(0.0, monthly_minutes - used_minutes)
    payg_minutes = payg_videos * billing.MINUTES_PER_VIDEO
    return {
        "plan": plan.id,
        "plan_name": plan.name,
        "monthly_videos": plan.monthly_videos,
        "monthly_minutes": monthly_minutes,
        "used_minutes": round(used_minutes, 1),
        "remaining_minutes": round(remaining_minutes, 1),
        "remaining_videos": billing.minutes_to_videos(remaining_minutes),
        "payg_videos": payg_videos,
        "payg_minutes": payg_minutes,
    }


@app.post("/jobs", status_code=202)
def create_job(
    body: CreateJobBody,
    bg: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Создать задачу: auth (JWT) → анти-абьюз free → гейт квоты → queued + фоновый прогон."""
    user_id = _resolve_user(authorization, x_user_id)
    _enforce_free_identity(authorization, user_id)
    _enforce_quota(user_id)
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    db.insert_job(
        job_id, body.source_type, body.source_ref,
        user_id=user_id, remove_pauses=body.remove_pauses,
    )  # fmt: skip
    # На Modal: spawn отдельной долгоживущей CPU-функции (web scale-to-zero убил бы фон-таск).
    # Локально: BackgroundTask, как в Phase 0.
    if dispatch.spawn_enabled():
        # spawn упал ДО старта пайплайна → джоб застрял бы в "queued" навсегда. Помечаем
        # failed и поднимаем 500 (правило №8): юзер видит причину, а не вечную очередь.
        try:
            fc_id = dispatch.spawn(
                "run_job",
                job_id,
                body.source_type,
                body.source_ref,
                body.max_clips,
                user_id,
                body.language,
                body.clip_min_sec,
                body.clip_max_sec,
            )
            db.set_function_call_id(job_id, fc_id)  # для Stop-кнопки (отмена джоба)
        except Exception as e:  # noqa: BLE001 — любой сбой диспатча = видимый failed
            db.set_failed(job_id, f"dispatch failed: {e}")
            raise HTTPException(status_code=500, detail=f"job dispatch failed: {e}") from e
    else:
        bg.add_task(
            run_pipeline_job,
            job_id,
            body.source_type,
            body.source_ref,
            body.max_clips,
            user_id,
            body.language,
            body.clip_min_sec,
            body.clip_max_sec,
        )
    return {"id": job_id, "status": "queued", "stage": "queued", "progress": 0}


@app.post("/jobs/upload", status_code=202)
async def create_upload_job(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    max_clips: int | None = Form(default=None, ge=1, le=30),
    language: str | None = Form(default=None),
    clip_min_sec: int | None = Form(default=None, ge=3, le=180),
    clip_max_sec: int | None = Form(default=None, ge=10, le=180),
    remove_pauses: bool = Form(default=False),
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Создать задачу из ЗАГРУЖЕННОГО файла: стримим на диск → фон-импорт → пайплайн.

    Файл пишется чанками в data/<job_id>/upload.<ext> (не держим в памяти); затем
    run_upload_job готовит source.mp4/wav/meta и гоняет тот же пайплайн, что и URL-путь.
    clip_min_sec/clip_max_sec — опциональные настройки длины клипов (None → дефолт конфига).
    """
    user_id = _resolve_user(authorization, x_user_id)
    _enforce_free_identity(authorization, user_id)
    _enforce_quota(user_id)
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    out = DATA_ROOT / job_id
    out.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "upload.mp4"
    suffix = Path(filename).suffix.lower() or ".mp4"
    upload_path = out / f"upload{suffix}"
    with upload_path.open("wb") as fh:
        while chunk := await file.read(_UPLOAD_CHUNK):
            fh.write(chunk)
    db.insert_job(job_id, "upload", filename, user_id=user_id, remove_pauses=remove_pauses)
    if dispatch.spawn_enabled():
        try:
            storage.upload_source(upload_path, job_id)
            dispatch.spawn(
                "upload_job",
                job_id,
                filename,
                max_clips,
                user_id,
                language,
                clip_min_sec,
                clip_max_sec,
            )
        except Exception as e:  # noqa: BLE001
            db.set_failed(job_id, f"dispatch failed: {e}")
            raise HTTPException(status_code=500, detail=f"upload dispatch failed: {e}") from e
    else:
        bg.add_task(
            run_upload_job,
            job_id,
            str(upload_path),
            filename,
            max_clips,
            user_id,
            language,
            clip_min_sec,
            clip_max_sec,
        )
    return {"id": job_id, "status": "queued", "stage": "queued", "progress": 0}


# Part C1: серверный потолок веса аплоада (defense-in-depth). Клиентский guard (SourceForm)
# обходится (прямой POST), а гигантский source давит пайплайн по памяти (download в tmpfs=RAM на
# Cloud Run). 10 ГБ — честный поддерживаемый предел операционного «конверта» (см. limits-отчёт);
# выше — 413 с понятным сообщением, а не молчаливый OOM на обработке. Держим в синхроне с
# клиентским SourceForm.MAX_UPLOAD_MB.
MAX_UPLOAD_BYTES = 10 * 1024**3


class UploadUrlBody(BaseModel):
    filename: str = "upload.mp4"
    max_clips: int | None = Field(default=None, ge=1, le=30)
    # Размер файла (байты) — известен браузеру. >5 ГБ нельзя одним PUT (лимит R2), а крупные и
    # вообще выгоднее multipart (параллельные части + resume). None/малый → single-PUT (как раньше).
    size: int | None = Field(default=None, ge=0)
    clip_min_sec: int | None = Field(default=None, ge=3, le=180)
    clip_max_sec: int | None = Field(default=None, ge=10, le=180)


@app.post("/jobs/upload-url")
def create_upload_url(
    body: UploadUrlBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Прямая загрузка браузер→R2: presigned PUT (мелкие) ИЛИ multipart-план (крупные) ИЛИ local.

    Большие видео через ОДИН долгий POST на Modal web рвались. Тут браузер льёт файл ПРЯМО в R2:
    - size ≤ part_size → один presigned PUT (как раньше);
    - size > part_size → multipart: создаём upload, отдаём presigned-URL на КАЖДУЮ часть; браузер
      грузит части параллельно и зовёт upload-complete с их ETag'ами → R2 собирает объект.
    Локально (нет R2) → {"local": true} → фронт шлёт обычный multipart POST /jobs/upload (dev ок).
    Квота гейтится ЗДЕСЬ (до загрузки). Джоб в БД создаётся в upload-complete (после PUT) —
    отменённая загрузка не оставляет «queued»-сироту.

    PERF: identity-гейт (verified email / disposable — несколько раунд-трипов в Supabase, вкл.
    Admin API) тут НЕ зовём — он висел на горячем пути и задерживал старт загрузки (presigned-URL
    сам по себе локальный). Защита НЕ ослаблена: identity отрабатывает в upload-complete (строка
    ниже) ДО spawn'а платной джобы — без подтверждённого email обработка не запустится, а presigned
    PUT отдаём мгновенно (байты в R2 пошли сразу). Квоту оставляем тут (fail-fast до заливки).
    """
    user_id = _resolve_user(authorization, x_user_id)
    _enforce_quota(user_id)
    # Part C1: серверный потолок веса (до минтинга URL, любой backend) — fail-fast, а не OOM позже.
    if body.size is not None and body.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large: the maximum supported upload size is "
                f"{MAX_UPLOAD_BYTES // 1024**3} GB. Please trim or compress the video and retry."
            ),
        )
    if get_settings().storage_backend != "r2":
        return {"local": True}
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    size = body.size or 0
    # Решение multipart-vs-single по ПОРОГУ (не по размеру части): 30–100 МБ файлы тоже режутся
    # на части → параллельная заливка + resume. part_size вычисляется (обычно 16 МБ, растёт лишь
    # на гигантских файлах, чтобы не пробить лимит R2 в 10000 частей) и ЯВНО отдаётся клиенту —
    # браузер нарезает файл ровно по нему ([i*part_size, (i+1)*part_size)).
    if size > storage.MULTIPART_THRESHOLD:
        upload_id = storage.create_multipart_upload(job_id)
        part_size = storage.plan_part_size(size)
        n_parts = storage.plan_part_count(size, part_size)
        parts = [
            {"part_number": i, "url": storage.presigned_upload_part_url(job_id, upload_id, i)}
            for i in range(1, n_parts + 1)
        ]
        return {
            "id": job_id,
            "upload_id": upload_id,
            "part_size": part_size,
            "parts": parts,
        }
    return {"id": job_id, "put_url": storage.presigned_put_url(job_id)}


class CompletedPart(BaseModel):
    part_number: int = Field(ge=1, le=10000)
    etag: str


class UploadCompleteBody(BaseModel):
    filename: str = "upload.mp4"
    max_clips: int | None = Field(default=None, ge=1, le=30)
    language: str | None = None
    clip_min_sec: int | None = Field(default=None, ge=3, le=180)
    clip_max_sec: int | None = Field(default=None, ge=10, le=180)
    remove_pauses: bool = False  # global «убирать паузы» (opt-in с загрузки)
    # Multipart-сборка (если грузили частями): upload_id + ETag'и частей. None → single-PUT
    # (объект уже в R2, собирать нечего).
    upload_id: str | None = None
    parts: list[CompletedPart] | None = None
    # Отпечаток source ``<size>:<sha256>`` для будущего per-user дедупа (клиент шлёт опционально).
    # None → не персистим. Малформед → non-fatal (лог + пропуск): загрузка не должна падать из-за
    # опционального сайд-канала дедупа.
    source_fingerprint: str | None = None


def _spawn_upload_job(
    bg: BackgroundTasks,
    job_id: str,
    filename: str,
    max_clips: int | None,
    user_id: str | None,
    language: str | None,
    clip_min_sec: int | None,
    clip_max_sec: int | None,
) -> dict[str, Any]:
    """Запустить upload_job на уже застейдженный в R2 source (общий хвост upload-complete и
    check-duplicate). Modal/Cloud Run → spawn долгоживущей джобы (web scale-to-zero убил бы
    фон-таск); always-on без spawn → bg.add_task in-process (download из R2 + run_upload_job).
    Сбой диспатча = видимый failed + 500 (правило №8, не вечная очередь)."""
    if dispatch.spawn_enabled():
        try:
            fc_id = dispatch.spawn(
                "upload_job",
                job_id,
                filename,
                max_clips,
                user_id,
                language,
                clip_min_sec,
                clip_max_sec,
            )
            db.set_function_call_id(job_id, fc_id)  # для Stop-кнопки (отмена джоба)
        except Exception as e:  # noqa: BLE001 — сбой диспатча = видимый failed, не вечная очередь
            db.set_failed(job_id, f"dispatch failed: {e}")
            raise HTTPException(status_code=500, detail=f"upload dispatch failed: {e}") from e
    else:
        bg.add_task(
            run_upload_job_from_r2,
            job_id,
            filename,
            max_clips,
            user_id,
            language,
            clip_min_sec,
            clip_max_sec,
        )
    return {"id": job_id, "status": "queued", "stage": "queued", "progress": 0}


@app.post("/jobs/{job_id}/upload-complete")
def complete_upload(
    job_id: str,
    body: UploadCompleteBody,
    bg: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Браузер залил исходник в R2 (presigned PUT) → запустить пайплайн (spawn upload_job).

    upload_job скачает исходник из R2 (тот же ключ source) и гоняет run_upload_job — как раньше.

    Безопасность: квоту гейтим ЗДЕСЬ ТОЖЕ (не только в upload-url) — спавн платной джобы не должен
    проходить без проверки (consistency со старым POST /jobs/upload). Авторизация по job_id: id —
    серверный случайный uuid из upload-url, а залить файл в ``job_id/source.mp4`` можно только по
    per-job presigned PUT оттуда же → чужой job_id не угадать/не подделать (джоба всегда на своего).
    """
    user_id = _resolve_user(authorization, x_user_id)
    _enforce_free_identity(authorization, user_id)
    _enforce_quota(user_id)
    db.insert_job(
        job_id, "upload", body.filename, user_id=user_id, remove_pauses=body.remove_pauses
    )
    # Отпечаток source (для будущего per-user дедупа) — ОПЦИОНАЛЬНЫЙ сайд-канал. Малформед здесь
    # НЕ фатален: логируем и пропускаем (загрузка не должна падать из-за дедуп-метаданных).
    if body.source_fingerprint is not None:
        if dedup.is_valid_fingerprint(body.source_fingerprint):
            db.set_source_fingerprint(job_id, body.source_fingerprint)
        else:
            print(f"[upload-complete] WARN: ignoring malformed source_fingerprint for job {job_id}")
    # Multipart: сначала собрать объект из частей (иначе upload_job скачает пусто/битое).
    if body.upload_id and body.parts:
        try:
            storage.complete_multipart_upload(
                job_id,
                body.upload_id,
                [{"PartNumber": p.part_number, "ETag": p.etag} for p in body.parts],
            )
        except Exception as e:  # noqa: BLE001 — сборка не удалась → видимый failed, не тихо
            db.set_failed(job_id, f"multipart complete failed: {e}")
            raise HTTPException(status_code=500, detail=f"multipart complete failed: {e}") from e
    # Modal: spawn долгоживущей upload_job. Always-on хостинг (Railway) без spawn → bg.add_task
    # in-process (download исходника из R2 + тот же run_upload_job). Симметрично create_job.
    return _spawn_upload_job(
        bg,
        job_id,
        body.filename,
        body.max_clips,
        user_id,
        body.language,
        body.clip_min_sec,
        body.clip_max_sec,
    )


class CheckDuplicateBody(BaseModel):
    """Запрос per-user дедупа: конфиг джобы (как в upload-complete) + ОБЯЗАТЕЛЬНЫЙ отпечаток."""

    filename: str = "upload.mp4"
    max_clips: int | None = Field(default=None, ge=1, le=30)
    language: str | None = None
    clip_min_sec: int | None = Field(default=None, ge=3, le=180)
    clip_max_sec: int | None = Field(default=None, ge=10, le=180)
    remove_pauses: bool = False  # global «убирать паузы» (opt-in с загрузки)
    source_fingerprint: str


@app.post("/jobs/check-duplicate")
def check_duplicate(
    body: CheckDuplicateBody,
    bg: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """«Я это уже грузил?» — если ДА (своя прошлая джоба с тем же отпечатком), переиспользуем её
    R2-source (server-side копия) и сразу запускаем обработку, минуя повторную заливку байтов.

    Гейты — как upload-complete (auth → анти-абьюз free → квота): дедуп СПАВНИТ платную джобу,
    поэтому те же проверки. Дедуп СТРОГО per-user (``find_duplicate_source`` фильтрует по
    ``user_id`` — кросс-юзерное «уже существует» было бы IDOR-оракулом существования файла).

    Исходы: не r2-бэкенд (нет server-side копии) / нет матча / пропавший R2-source →
    ``{"duplicate": false}`` (клиент грузит обычным путём); малформед отпечатка → 400.
    """
    user_id = _resolve_user(authorization, x_user_id)
    _enforce_free_identity(authorization, user_id)
    _enforce_quota(user_id)
    if get_settings().storage_backend != "r2":
        return {"duplicate": False}
    if not dedup.is_valid_fingerprint(body.source_fingerprint):
        raise HTTPException(status_code=400, detail="invalid source fingerprint")
    match = db.find_duplicate_source(user_id, body.source_fingerprint) if user_id else None
    if match is None:
        return {"duplicate": False}
    new_job_id = f"job_{uuid.uuid4().hex[:12]}"
    try:
        storage.copy_source(match, new_job_id)
    except storage.SourceNotFoundError:
        # R2-source найденной джобы уже вымело ретеншном → не дубликат: клиент грузит обычным путём.
        print(f"[check-duplicate] source for match {match} gone from R2 — treating as no duplicate")
        return {"duplicate": False}
    db.insert_job(
        new_job_id, "upload", body.filename, user_id=user_id, remove_pauses=body.remove_pauses
    )
    db.set_source_fingerprint(new_job_id, body.source_fingerprint)
    result = _spawn_upload_job(
        bg,
        new_job_id,
        body.filename,
        body.max_clips,
        user_id,
        body.language,
        body.clip_min_sec,
        body.clip_max_sec,
    )
    return {**result, "duplicate": True}


class UploadAbortBody(BaseModel):
    upload_id: str


@app.post("/jobs/{job_id}/upload-abort")
def abort_upload(
    job_id: str,
    body: UploadAbortBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Браузер отменил/уронил multipart-загрузку → удалить залитые части из R2 (не копить мусор).

    Best-effort: чистка не должна ронять и без того ошибочный флоу — сбой самой abort возвращаем
    как ``{"ok": false}`` (а не 500). Auth обязателен (чужой upload не отменить).

    Ownership: строка джобы создаётся в upload-complete (ПОСЛЕ заливки) — при аборте её обычно
    ещё НЕТ, поэтому гейтим ТОЛЬКО если строка уже есть (иначе нечего защищать — пропускаем без
    404). owner=None (legacy) — bypass, как в остальных гейтах.
    """
    user_id = _resolve_user(authorization, x_user_id)
    if auth.supabase_auth_enabled():
        row = db.get_job_row(job_id)
        if row is not None and row.get("user_id") not in (None, user_id):
            raise HTTPException(status_code=403, detail="not your job")
    try:
        storage.abort_multipart_upload(job_id, body.upload_id)
    except Exception:  # noqa: BLE001 — cleanup best-effort; незавершённые части подберёт lifecycle R2
        return {"ok": False}
    return {"ok": True}


class UploadPartsBody(BaseModel):
    upload_id: str
    # Явный список нужных частей (renewal ОДНОЙ протухшей URL) → mint'им ровно их. Опущен (resume) →
    # mint'им ВСЕ части 1..plan_part_count(size), КРОМЕ уже завершённых (из R2 ListParts).
    part_numbers: list[int] | None = None
    # Размер файла — вывести число частей на resume-пути (клиент знает его из исходного плана).
    size: int | None = Field(default=None, ge=0)


@app.post("/jobs/{job_id}/upload-parts")
def upload_parts(
    job_id: str,
    body: UploadPartsBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Resume + presigned-URL renewal для прямой browser→R2 multipart-загрузки.

    Браузер зовёт этот эндпоинт, когда (а) часть протухла (403 → нужна свежая URL) или (б) загрузка
    возобновляется после обрыва/перезагрузки страницы. Отвечаем:
      • ``completed`` — части, УЖЕ лежащие в R2 (authoritative, из ``storage.list_parts``);
      • ``parts`` — СВЕЖИЕ presigned PUT-URL: для ``part_numbers`` (если заданы — точечный renewal),
        иначе для всех ещё-НЕ-завершённых частей ``1..plan_part_count(size)`` (resume «хвоста»);
      • ``part_size`` — эхо размера части (``plan_part_size(size)``) для сверки клиентом.

    Auth — как upload-complete (``_resolve_user``: 401 при включённом auth без валидного токена).
    Квоту тут НЕ списываем и НЕ гейтим — джоб ещё не спавнится (строка джобы появляется только в
    upload-complete), resume живёт на R2 через ListParts. Гоняемый ``job_id`` — серверный uuid из
    upload-url, залить в ``job_id/source.mp4`` можно лишь по per-job presigned PUT оттуда же.
    """
    _resolve_user(authorization, x_user_id)  # 401 при включённом auth без валидного токена
    try:
        completed = storage.list_parts(job_id, body.upload_id)
    except storage.MultipartGoneError as e:
        # Multipart genuinely gone (unknown/expired upload_id) -> 404, NOT an opaque 500. The client
        # reads 404 as "discard the checkpoint, restart fresh"; every other error it retries while
        # keeping the uploaded parts (no silent restart-from-0% on an auth/server blip).
        raise HTTPException(status_code=404, detail="multipart upload not found") from e
    done = {p["part_number"] for p in completed}
    size = body.size or 0
    part_size = storage.plan_part_size(size)
    if body.part_numbers is not None:
        target = body.part_numbers
    else:
        n_parts = storage.plan_part_count(size, part_size)
        target = [n for n in range(1, n_parts + 1) if n not in done]
    parts = [
        {"part_number": n, "url": storage.presigned_upload_part_url(job_id, body.upload_id, n)}
        for n in target
    ]
    return {
        "part_size": part_size,
        "completed": [{"part_number": p["part_number"], "etag": p["etag"]} for p in completed],
        "parts": parts,
    }


@app.post("/webhooks/polar")
async def polar_webhook(request: Request) -> dict[str, Any]:
    """Вебхук оплаты Polar.sh → profiles.plan. Проверяет подпись Standard Webhooks
    (webhook-id/timestamp/signature по POLAR_WEBHOOK_SECRET), маппит product→план, пишет
    set_user_plan (service-role на Postgres). Не сконфигурирован → 503; плохая подпись →
    401; чужой/непонятный ивент → 200 applied=false (Polar не ретраит).
    См. docs/SUPABASE_SETUP.md §4.
    """
    import json

    from app import billing, polar

    secret = os.environ.get("POLAR_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Polar webhook not configured")
    body = await request.body()
    if not polar.verify_signature(
        secret,
        body,
        webhook_id=request.headers.get("webhook-id", ""),
        webhook_timestamp=request.headers.get("webhook-timestamp", ""),
        signature_header=request.headers.get("webhook-signature", ""),
    ):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="invalid JSON body") from e
    change = polar.parse_plan_change(
        payload,
        product_starter=os.environ.get("POLAR_PRODUCT_STARTER", ""),
        product_pro=os.environ.get("POLAR_PRODUCT_PRO", ""),
    )
    if change is not None:
        user_id, plan = change
        db.set_user_plan(user_id, plan)
        return {"ok": True, "applied": True, "user_id": user_id, "plan": plan}

    # Разовая оплата PAYG → начислить не сгорающие кредиты.
    payg = polar.parse_payg_order(
        payload,
        product_payg=os.environ.get("POLAR_PRODUCT_PAYG", ""),
        credits_per_order=billing.PAYG_CREDITS_PER_ORDER,
    )
    if payg is not None:
        user_id, credits = payg
        db.add_payg_credits(user_id, credits)
        return {"ok": True, "applied": True, "user_id": user_id, "payg_credits": credits}

    # Разовая оплата «лишнего клипа» ($1) → начислить клип-кредиты (ОТДЕЛЬНЫЙ баланс, тратится
    # только на извлечение клипа за free-cap). Матч по СВОЁМУ product_id → не путается с PAYG.
    extra = polar.parse_extra_clip_order(
        payload,
        product_extra_clip=os.environ.get("POLAR_PRODUCT_EXTRA_CLIP", ""),
        credits_per_order=billing.EXTRACT_CLIP_CREDITS_PER_ORDER,
    )
    if extra is not None:
        user_id, credits = extra
        db.add_clip_credits(user_id, credits)
        return {"ok": True, "applied": True, "user_id": user_id, "clip_credits": credits}

    return {"ok": True, "applied": False}


def _job_list_item(row: dict[str, Any]) -> dict[str, Any]:
    """СЫРАЯ строка джобы → лёгкий элемент дашборд-списка (без ре-подписи клип-URL). ``clips``
    приходят jsonb-списком (Postgres) ИЛИ строкой ``clips_json`` (SQLite); ярлык = сохранённый
    ``source_ref`` (youtube-url / имя загруженного файла). НЕ выдумываем новых колонок БД."""
    clips = row.get("clips")
    if clips is None:
        raw = row.get("clips_json")
        clips = json.loads(raw) if raw else []
    source_kind = "upload" if row.get("source_type") == "upload" else "youtube"
    return {
        "id": row["id"],
        "label": str(row.get("source_ref") or ""),
        "status": row.get("status") or "",
        "nclips": len(clips or []),
        "created_at": row.get("created_at"),
        "source_kind": source_kind,
    }


@app.get("/jobs")
def list_jobs(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Список джоб текущего пользователя (newest-first) — замена клиентских localStorage-recents.

    Auth включён → 401 без валидного токена, скоуп строго по user_id из JWT (чужие джобы не
    видны). Auth выключен (dev) → скоуп по X-User-Id (если задан), иначе самые свежие джобы.
    Лёгкий (поллится дашбордом): без ре-подписи клип-URL, nclips считается из строки.
    """
    user_id = _resolve_user(authorization, x_user_id)
    rows = db.list_jobs_by_user(user_id)
    return {"jobs": [_job_list_item(r) for r in rows]}


@app.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Статус задачи (wire-Job) из SQLite. 404, если задачи нет. Owner-only (IDOR-гейт)."""
    _require_owned_job(job_id, authorization, x_user_id)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Stop-кнопка: отменить джоб во FREE-фазе (download/probe, до транскрипции) — $0 заряда.

    Отмена возможна ТОЛЬКО пока ``cancellable`` (воркер гасит флаг перед платной транскрипцией).
    Идемпотентна на done/failed/cancelled. На Modal отменяем ОТДЕЛЬНУЮ долгоживущую функцию по
    её ``function_call_id`` (``terminate_containers=False`` → внутри неё рейзится
    ``InputCancellation``, она выходит ДО set_done/_meter → ничего не списывается).
    """
    # Ownership-гейт (IDOR): при включённом auth отменить может только владелец (403), legacy/
    # ничьи джобы (owner=None) не блокируем. При выключенном auth гейт — pass-through ({} строка);
    # cancel'у нужны РЕАЛЬНЫЕ поля строки (status/cancellable/function_call_id) → дозагружаем её.
    row, _user_id = _require_owned_job(job_id, authorization, x_user_id)
    if not row:
        fetched = db.get_job_row(job_id)
        if fetched is None:
            raise HTTPException(status_code=404, detail="job not found")
        row = fetched
    status = row.get("status")
    if status in ("done", "failed", "cancelled"):
        # Уже в терминале → отменять нечего (идемпотентно, не ошибка).
        return {"id": job_id, "status": status, "cancelled": False}
    if not row.get("cancellable"):
        raise HTTPException(
            status_code=409,
            detail="job has entered a paid stage and can no longer be stopped",
        )
    fc_id = row.get("function_call_id")
    if fc_id:
        try:
            dispatch.cancel(str(fc_id))  # Cloud Run execution cancel / Modal FunctionCall cancel
        except Exception as e:  # noqa: BLE001 — отмена Modal не удалась → НЕ молча (правило №8)
            # Всё равно фиксируем cancelled в БД (поллинг остановится), но сигналим 502, чтобы
            # фронт знал: фон-функция могла не остановиться (нет тихого фолбэка).
            db.set_cancelled(job_id)
            raise HTTPException(status_code=502, detail=f"cancel failed: {e}") from e
    db.set_cancelled(job_id)
    return {"id": job_id, "status": "cancelled", "cancelled": True}


@app.get("/jobs/{job_id}/media-token")
def get_media_token(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Выдать SCOPED короткоживущий opaque медиа-тикет для ``<video src>`` / download (замена
    raw-JWT ``?token=``). Owner-only (401/403/404 через ``_require_owned_job``). Тикет привязан к
    ЭТОЙ джобе и живёт ~5 мин → в access-логи/Referer уходит бесполезный-на-чужой-джобе тикет, а
    НЕ сессионный аккаунт-JWT.

    Ключ подписи: ``MEDIA_TOKEN_SECRET`` или дериват из server-only секрета (см. media_token).
    Нет ключа при включённом auth → 500 (НЕ выдаём неподписанный/угадываемый тикет). При
    выключенном auth (dev) media-эндпоинты и так открыты → тикет-плейсхолдер (не проверяется).
    """
    _require_owned_job(job_id, authorization, x_user_id)
    secret = media_token.media_token_secret()
    if secret is None:
        if auth.supabase_auth_enabled():
            raise HTTPException(
                status_code=500,
                detail="media token signing key unavailable (set MEDIA_TOKEN_SECRET)",
            )
        secret = b"dev-unsigned"  # auth off: тикет косметический, media-путь не верифицирует
    return {"token": media_token.mint(job_id, secret), "expires_in": media_token.DEFAULT_TTL}


@app.get("/jobs/{job_id}/source.mp4")
def get_source(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """Полный исходник (редактор-рендер / фолбэк превью). local → диск; r2 → 302 на CDN (или
    presigned). Раньше всегда presigned-origin без кэша = медленно; теперь CDN при R2_PUBLIC_URL.

    Owner-only (IDOR): грузится через ``<video src>`` → Authorization-header часто нельзя, поэтому
    принимаем ЛИБО header владельца, ЛИБО scoped-тикет ``?mt=`` (см. ``_require_media_access``)."""
    _require_media_access(job_id, authorization, x_user_id, mt)
    s = get_settings()
    if s.storage_backend == "r2":
        from fastapi.responses import RedirectResponse

        from app.storage import source_read_url

        return _no_referrer(RedirectResponse(source_read_url(job_id)))
    path = DATA_ROOT / job_id / "source.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="source not found")
    return _no_referrer(FileResponse(str(path), media_type="video/mp4"))


@app.get("/jobs/{job_id}/preview.mp4")
def get_preview(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """Лёгкий preview-прокси для БЫСТРОЙ загрузки видео в редакторе (≤720p H.264 faststart, пара МБ
    вместо 50-100МБ source; H.264 = hw-декод, не софт-AV1). Нет прокси (старый джоб до фичи) →
    прозрачный фолбэк на полный source. local → диск; r2 → 302 на CDN/presigned.

    Owner-only (IDOR): header ЛИБО scoped-тикет ``?mt=`` (грузится через ``<video src>``)."""
    _require_media_access(job_id, authorization, x_user_id, mt)
    s = get_settings()
    if s.storage_backend == "r2":
        from fastapi.responses import RedirectResponse

        from app.storage import preview_read_url

        return _no_referrer(RedirectResponse(preview_read_url(job_id)))
    prev = DATA_ROOT / job_id / "preview.mp4"
    if prev.exists():
        return _no_referrer(FileResponse(str(prev), media_type="video/mp4"))
    src = DATA_ROOT / job_id / "source.mp4"
    if src.exists():
        return _no_referrer(FileResponse(str(src), media_type="video/mp4"))
    raise HTTPException(status_code=404, detail="preview/source not found")


@app.get("/jobs/{job_id}/timeline")
def get_timeline(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """TimelineData: длительность источника + ВСЕ кандидаты ИИ + слова (для таймлайн-редактора).

    Собирается из готовых meta/segments/transcript (диск локально, Postgres на Modal).
    Дорогих ИИ-вызовов НЕТ. 404, если артефактов нет (как /analysis). Owner-only (IDOR).
    """
    _require_owned_job(job_id, authorization, x_user_id)
    from app import artifacts

    try:
        meta = artifacts.load_meta(job_id)
        segments = artifacts.load_segments(job_id)
        words = artifacts.load_transcript_words(job_id)
    except JobError as e:
        raise HTTPException(status_code=404, detail="timeline data not found") from e
    return build_timeline_data(meta.duration, segments, words).model_dump()


@app.get("/jobs/{job_id}/chapters")
def get_chapters(
    job_id: str,
    bg: BackgroundTasks,
    retry: bool = False,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """AI-карта видео (главы с описаниями). Кэш data/<job>/chapters.json. Owner-only (IDOR).

    Файла нет → пишем pending + стартуем фон-генерацию (Gemini, ~$0.01-0.03,
    платится один раз); фронт поллит до done/failed. 404 — нет транскрипта.
    Повторный GET при pending вторую генерацию НЕ стартует.

    retry=true (T4 #9): если кэш — failed (квота Gemini free-tier 20/день), перезапускаем
    генерацию (перетираем failed → pending). На done/pending retry игнорируется (не дёргаем).
    """
    _require_owned_job(job_id, authorization, x_user_id)
    from app import artifacts
    from app import tasks as tasks_mod
    from app.editor.chapters import load_chapters, save_chapters
    from app.models import ChaptersData

    out = artifacts.job_dir(job_id)
    out.mkdir(parents=True, exist_ok=True)
    try:
        artifacts.load_transcript(job_id)  # 404, если транскрипта нет нигде (диск/Postgres)
    except JobError as e:
        raise HTTPException(status_code=404, detail="transcript not found") from e
    # NB: кэш chapters.json — на scratch-диске контейнера; на Modal может перегенериться на
    # холодном web-контейнере (Gemini-вызов). Перенос кэша в Postgres — follow-up (Phase C).
    cached = load_chapters(out)
    if cached is not None and not (retry and cached.status == "failed"):
        return cached.model_dump()
    pending = ChaptersData(status="pending")
    save_chapters(out, pending)
    bg.add_task(tasks_mod.generate_chapters_job, job_id)
    return pending.model_dump()


@app.get("/jobs/{job_id}/video-map")
def get_video_map(
    job_id: str,
    bg: BackgroundTasks,
    retry: bool = False,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Нарративная VideoMap (главы+моменты+связный разбор). Durable в Postgres (cross-container).

    Нет карты → пишем pending + триггерим генерацию (Gemini, платится один раз); фронт поллит до
    done/failed. 404 — нет транскрипта. Повторный GET при pending вторую генерацию НЕ стартует.

    ⚠️ Cloud: генерация идёт в ОТДЕЛЬНОМ Modal-контейнере (dispatch.spawn — bg.add_task сдох бы на
    scale-to-zero web). save/load_video_map round-trip'ят через Postgres job_artifacts, поэтому этот
    web-контейнер видит результат чужого контейнера. Локально (нет Modal) — bg.add_task + диск.

    retry=true: кэш failed → перезапуск (перетираем failed → pending). На done/pending игнор.
    """
    _require_owned_job(job_id, authorization, x_user_id)
    from app import artifacts
    from app import tasks as tasks_mod
    from app.editor.video_map import load_video_map, save_video_map
    from app.models import VideoMap

    try:
        artifacts.load_transcript(job_id)  # 404, если транскрипта нет нигде (диск/Postgres)
    except JobError as e:
        raise HTTPException(status_code=404, detail="transcript not found") from e
    cached = load_video_map(job_id)
    if cached is not None and not (retry and cached.status == "failed"):
        return cached.model_dump()
    pending = VideoMap(status="pending")
    save_video_map(job_id, pending)
    if dispatch.spawn_enabled():
        dispatch.spawn("generate_video_map_job", job_id)
    else:
        bg.add_task(tasks_mod.generate_video_map_job, job_id)
    return pending.model_dump()


@app.get("/jobs/{job_id}/preview-moments")
def get_preview_moments(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Косметические co-watch-маркеры (Part 4): список {t,kind,intensity} из job_artifacts.

    ⚠️ ЧИСТО ВИЗУАЛЬНО — НЕ влияет на отбор клипов (LLM-отбор от них не зависит). Пусто, пока
    transcribe не закончил, на старых джобах, или в локальном dev (артефакты — только в облаке).
    Owner-only (IDOR).
    """
    _require_owned_job(job_id, authorization, x_user_id)
    moments = db.get_job_artifact(job_id, "preview_moments")
    return {"moments": moments or []}


# ──────────────────── On-demand clip extraction (POST /jobs/{id}/clips) ────────────────────
# Вырезать НОВЫЙ клип из произвольного source-диапазона (un-clipped момент AI-карты ИЛИ drag-
# выделение на таймлайне) с ТЕМ ЖЕ качеством границ, что у авто-клипа. Гейт денег СЕРВЕРНЫЙ
# (обойти с фронта нельзя): извлекать могут ТОЛЬКО pro/studio (403 иначе); в рамках уже оплаченного
# «до 30 клипов на видео» бесплатно, за cap → per-clip пейволл (402). Persist + рендер идут ровно
# тем же путём, что авто-клипы (append_pending_clip → render_one_clip → set_clip_ready).


class ExtractClipBody(BaseModel):
    """Тело ``POST /jobs/{job_id}/clips``: произвольный source-диапазон [start, end] (секунды)."""

    start: float
    end: float


def _extract_seed_from_map(job_id: str, start: float, end: float) -> dict[str, Any]:
    """Засеять hook/why_works/reason из БЛИЖАЙШЕГО перекрывающего момента AI-карты (best-effort).

    Нет карты / нет пересечения / любой сбой → пустые дефолты (извлечённый клип всё равно чистый —
    поля объяснимости опциональны). Момент с максимальным перекрытием [start,end] выигрывает."""
    from app.editor.video_map import load_video_map
    from app.models import VideoMoment

    try:
        vm = load_video_map(job_id)
    except Exception as e:  # noqa: BLE001 — карта — опциональное обогащение, не роняем извлечение
        print(f"[extract_clip] WARN video map load failed job={job_id}: {e}")
        return {}
    if vm is None or vm.status != "done":
        return {}
    best: VideoMoment | None = None
    best_overlap = 0.0
    for ch in vm.chapters:
        for mo in ch.moments:
            overlap = min(end, mo.end) - max(start, mo.start)
            if overlap > best_overlap:
                best_overlap = overlap
                best = mo
    if best is None or best_overlap <= 0.0:
        return {}
    return {
        "reason": best.why or best.label,
        "hook": best.label or None,
        "why_works": best.why or None,
    }


@app.post("/jobs/{job_id}/clips")
def extract_clip(
    job_id: str,
    body: ExtractClipBody,
    bg: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> ClipOut:
    """Вырезать НОВЫЙ клип из source-диапазона [start,end] и запустить его рендер в фоне.

    Гейты (СЕРВЕРНЫЕ, обойти с клиента нельзя): владелец джоба (401/403/404) → план владельца
    ``pro``/``studio`` иначе 403 ``{"error":"upgrade_required"}`` → источник в 60-дневном окне
    иначе 409 ``{"error":"source_expired"}`` → идемпотентность (тот же диапазон → тот же клип, 200)
    → per-clip пейволл за cap: 402 ``{"error":"payment_required","price_usd":…}``. На успех: клип
    добавляется pending (пустой ``video_url``), рендер спавнится в фоне (публикует ``video_url``
    через ``set_clip_ready`` РОВНО как авто-клип), 200 с pending ``ClipOut`` (фронт стримит его как
    обычный ещё-не-готовый клип). Границы кроятся с тем же качеством, что авто-клип
    (range_to_segment переиспользует selection-path снап).
    """
    from app import artifacts
    from app.editor.extract_clip import range_to_segment, segment_key
    from app.pipeline.preview_moments import extract_loudness
    from app.run import build_clip_out

    # 1. Ownership-гейт (401/403/404). В dev (auth off) строка пустая → дозагружаем сырую строку
    #    (нужен реальный owner для резолва плана + счётчик клипов), как в cancel_job.
    row, _requester_id = _require_owned_job(job_id, authorization, x_user_id)
    if not row:
        fetched = db.get_job_row(job_id)
        if fetched is None:
            raise HTTPException(status_code=404, detail="job not found")
        row = fetched

    # 2. Гейт плана владельца (money-path, серверный): извлекать могут ТОЛЬКО pro/studio. План
    #    резолвится из profiles.plan владельца (jobs.user_id) — как resolve_clip_render_policy.
    #    Нет владельца (локальный dev / ничья джоба) → как local_dev: извлечение разрешено (pro).
    owner_id = row.get("user_id") or None
    plan = db.get_user_plan(owner_id) if owner_id else "pro"
    if not billing.can_extract_clip(plan):
        raise HTTPException(status_code=403, detail={"error": "upgrade_required"})

    # 4. Транскрипт (слова + длина источника) + гейт доступности источника (60-дневное окно).
    try:
        transcript = artifacts.load_transcript(job_id)
    except JobError as e:
        raise HTTPException(status_code=404, detail="transcript not found") from e
    words = transcript.words
    source_dur = transcript.duration
    try:
        src_path = artifacts.ensure_source(job_id)  # источника нет → окно истекло
    except JobError as e:
        raise HTTPException(status_code=409, detail={"error": "source_expired"}) from e
    # Кривую громкости пересчитываем best-effort (уточняет снап краёв к вдоху/обрезку филлеров).
    # Сбой ТОЛЬКО пересчёта loudness → None (клип всё равно чисто снапнут по словам); НЕ глотаем
    # посторонние ошибки — ловим узко вокруг extract_loudness.
    try:
        loudness = extract_loudness(str(src_path)) or None
    except Exception as e:  # noqa: BLE001 — только пересчёт loudness опционален (правило №8: лог)
        print(f"[extract_clip] WARN loudness recompute failed job={job_id}: {e}")
        loudness = None

    # 5. Чистый Segment (тот же снап, что selection-path) + посев объяснимости из момента карты.
    seed = _extract_seed_from_map(job_id, body.start, body.end)
    seg = range_to_segment(
        body.start,
        body.end,
        words,
        loudness=loudness,
        source_dur=source_dur,
        reason=seed.get("reason", ""),
        hook=seed.get("hook"),
        why_works=seed.get("why_works"),
    )

    # 6. Идемпотентность: тот же (округлённый) диапазон уже извлечён → отдаём тот клип, не рендерим.
    key = segment_key(seg.start, seg.end)
    current_clips = list((db.get_job(job_id) or {}).get("clips") or [])
    for c in current_clips:
        if segment_key(float(c.get("start", -1.0)), float(c.get("end", -1.0))) == key:
            return ClipOut.model_validate(c)

    # 7. Пейволл за cap. До free-cap извлечение бесплатно (в рамках оплаченного «до 30 клипов»);
    #    ≥ cap → нужен оплаченный клип-кредит ($1-заказ Polar): есть у владельца — списываем один и
    #    продолжаем, нет — 402 (фронт ведёт на checkout). Consume ДО append (шаг 8): подтверждаем
    #    оплату прежде чем создать клип. Без владельца (локальный dev / ничья джоба, plan=pro выше)
    #    биллинга нет → не капим. Идемпотентный повтор (шаг 6) выходит раньше и кредит НЕ тратит.
    settings = get_settings()
    over_cap = billing.extract_requires_payment(len(current_clips), cap=settings.extract_free_cap)
    if owner_id and over_cap and not db.consume_clip_credit(owner_id):
        raise HTTPException(
            status_code=402,
            detail={"error": "payment_required", "price_usd": settings.extract_clip_price_usd},
        )

    # 8. Segment → ClipOut (id = следующий clip_NN), pending (video_url="") → append в строку джоба.
    next_index = len(current_clips) + 1
    clip_out = build_clip_out(f"clip_{next_index:02d}", seg, words, "", loudness=loudness)
    idx = db.append_pending_clip(job_id, clip_out.model_dump())

    # 9. Фоновый рендер клипа тем же путём, что пайплайн (reframe→render→upload→set_clip_ready).
    #    На Cloud Run/Modal — spawn отдельного контейнера; локально/dev — bg.add_task. Не блокируем
    #    ответ. Сбой спавна НЕ роняет уже добавленный клип (остаётся pending), но логируется (№8).
    meta = artifacts.load_meta(job_id)
    seg_dump = seg.model_dump()
    meta_dump = meta.model_dump()
    if dispatch.spawn_enabled():
        try:
            dispatch.spawn("render_extracted_clip_job", job_id, idx, seg_dump, meta_dump, owner_id)
        except Exception as e:  # noqa: BLE001 — сбой диспатча видим (правило №8), клип уже pending
            print(f"[extract_clip] WARN render dispatch failed job={job_id} idx={idx}: {e}")
    else:
        bg.add_task(render_extracted_clip_job, job_id, idx, seg_dump, meta_dump, owner_id)

    # 10. Pending ClipOut (video_url="") → фронт стримит его как обычный ещё-не-готовый клип.
    return clip_out


# ──────────────────────────── Editor endpoints ────────────────────────────


class PatchEditBody(BaseModel):
    version: int
    captions: CaptionTrack


class TrimBody(BaseModel):
    version: int
    word_indices: list[int]


class AddSectionBody(BaseModel):
    version: int
    source_start: float
    source_end: float
    at_index: int


class ExtendBody(BaseModel):
    version: int
    edge: str  # "start" | "end"
    new_value: float


class CropBody(BaseModel):
    version: int
    source_start: float
    source_end: float
    mode: Literal["fill", "fit", "auto"]  # split удалён (MVP); auto = снять override (вернуть авто)
    center: float | None = Field(default=None, ge=0.0, le=1.0)
    center_b: float | None = Field(default=None, ge=0.0, le=1.0)  # legacy split (не используется)


class SetIntervalBody(BaseModel):
    version: int
    source_start: float
    source_end: float


class RegenerateHookBody(BaseModel):
    version: int


class AgentStartBody(BaseModel):
    message: str


def _save_or_409(job_id: str, clip_id: str, new_edit: Any, version: int) -> dict[str, Any]:
    try:
        return store.save_edit(job_id, clip_id, new_edit, expected_version=version).model_dump()
    except EditConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _load_or_404(job_id: str, clip_id: str) -> Any:
    edit = store.load_edit(job_id, clip_id)
    if edit is None:
        raise HTTPException(status_code=404, detail="edit not found")
    return edit


def _op_or_400(fn: Callable[[], Any]) -> Any:
    """Прогнать pure-операцию редактора (ops.py), переведя её доменный JobError в HTTP 400.

    ops.py рейзит JobError на НЕВАЛИДНЫЙ ввод (индекс вне диапазона, перевёрнутый интервал,
    неизвестный край). Без перехвата это всплывало 500 (баг сервера) вместо 400 (ошибка
    клиента). Зеркалит трансляцию GET-хендлеров (JobError → 404 там, где это «не найдено»)."""
    try:
        return fn()
    except JobError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/jobs/{job_id}/clips/{clip_id}/edit")
def get_clip_edit(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """ClipEdit клипа (создаёт дефолт из сегмента при первом обращении). Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    try:
        return store.ensure_edit(job_id, clip_id).model_dump()
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e


@app.get("/jobs/{job_id}/clips/{clip_id}/ass")
def get_clip_ass(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> Response:
    """ASS субтитров текущего edit-state (для libass-превью в браузере).

    Тот же компилятор, что и финальный экспорт (captions_v2.compile_ass) → превью
    субтитров через libass.wasm = экспорт пиксель-в-пиксель. Тайминги в КЛИП-времени.
    Owner-only (IDOR).
    """
    _require_owned_job(job_id, authorization, x_user_id)
    try:
        edit = store.ensure_edit(job_id, clip_id)
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
    words = store.load_transcript_words(job_id)
    cmap = ClipTimeMap(edit.source_intervals)
    pw, ph = aspect_to_dims(edit.aspect)  # T5: PlayRes = размеры выхода аспекта
    ass = compile_ass(edit.captions, words, cmap, play_w=pw, play_h=ph)
    return Response(content=ass, media_type="text/plain; charset=utf-8")


@app.get("/jobs/{job_id}/clips/{clip_id}/export.srt")
def export_clip_srt(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """SRT субтитров текущего edit-state (экспорт-свобода: унести в любой редактор).

    compile_srt зеркалит compile_ass (те же реплики/тайминги) → скачанный SRT
    совпадает с прожжённым видео. Content-Disposition: attachment → браузер скачивает.
    Owner-only (IDOR): header ЛИБО scoped-тикет ``?mt=`` (нативный download).
    """
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        edit = store.ensure_edit(job_id, clip_id)
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
    words = store.load_transcript_words(job_id)
    cmap = ClipTimeMap(edit.source_intervals)
    srt = compile_srt(edit.captions, words, cmap)
    return _no_referrer(
        Response(
            content=srt,
            media_type="application/x-subrip; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{clip_id}.srt"'},
        )
    )


@app.get("/jobs/{job_id}/clips/{clip_id}/export/clean.mp4")
def export_clip_clean_mp4(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """Чистый mp4 БЕЗ прожжённых субтитров (экспорт-свобода: пере-монтаж где угодно).

    FAST PATH (Level 1, 2026-07-14): геометрия нетронута → 302-РЕДИРЕКТ на запечённый чистый клип
    ``clips/{clip}.mp4`` на CDN. api-бокс НЕ гоняет байты и НЕ считает CV. Это тот же фикс, что
    captioned получил 2026-07-10 — clean по недосмотру остался на полном пути и был
    ГАРАНТИРОВАННЫМ отказом: он ВСЕГДА качал весь source в RAM-backed FS Cloud Run (ФС=RAM → OOM,
    503) и ВСЕГДА пересчитывал CV + энкодил (> 300s request-timeout → 504). Composite-fast-path
    его не спасал — тот закрыт условием ``if with_subtitles:`` (tasks.render_edit_to_file).
    Гейт СТРОГИЙ (fast_export.should_serve_baked_clean): любая правка геометрии / удаление пауз →
    полный рендер, иначе молча отдали бы НЕ ТО видео (правило №8).

    Иначе рендерит текущий edit-state с ass_name=None в clips/{clip}_clean.mp4 и отдаёт файл.
    Синхронно (FastAPI крутит sync-эндпоинт в threadpool). Сбой рендера → HTTP 500 (правило №8).
    Owner-only (IDOR): header ЛИБО scoped-тикет ``?mt=`` (нативный download).
    """
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        edit = store.ensure_edit(job_id, clip_id)  # из грида (без редактора) тоже работает
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
    # Запечённый clean (variant="") пишет пайплайн (run.render_one_clip) и НИКОГДА не перетирает
    # прожжённый экспорт (storage.upload_clip) → объект неизменяем, протухшее отдать нельзя.
    _seg = segment_bounds(job_id, clip_id)
    if _seg is not None and fast_export.should_serve_baked_clean(
        edit, seg_start=_seg[0], seg_end=_seg[1]
    ):
        try:
            return _no_referrer(
                RedirectResponse(storage.clip_read_url(job_id, clip_id), status_code=302)
            )
        except Exception as e:  # noqa: BLE001 — редирект best-effort; падаем в честный рендер
            print(f"[export] baked clean redirect failed {job_id}/{clip_id}: {e}")
    out_rel = f"clips/{clip_id}_clean.mp4"
    try:
        render_edit_to_file(job_id, clip_id, with_subtitles=False, out_rel=out_rel)
    except JobError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    path = store.data_root() / job_id / out_rel
    if not path.exists():
        raise HTTPException(status_code=500, detail="render produced no clean mp4")
    return _no_referrer(
        FileResponse(str(path), media_type="video/mp4", filename=f"{clip_id}_clean.mp4")
    )


@app.get("/jobs/{job_id}/clips/{clip_id}/export/captioned.mp4")
def export_clip_captioned_mp4(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """Прожжённый mp4 С субтитрами текущего edit-state (download «С субтитрами»).

    D1-унификация: рендерит ТЕКУЩИЕ правки (тот же ASS, что в libass-превью) → скачанный
    файл всегда совпадает с превью (WYSIWYG), доступен без отдельного «Рендер» и НЕ трогает
    чистый ``clips/{clip}.mp4``. Синхронно (FastAPI threadpool). Сбой рендера → HTTP 500
    (правило №8). Один путь и для грида, и для редактора — нет расхождения «с субтитрами».
    Owner-only (IDOR): header ЛИБО scoped-тикет ``?mt=`` (нативный download).
    """
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        store.ensure_edit(job_id, clip_id)
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
    out_rel = f"clips/{clip_id}_captioned.mp4"
    dst = store.data_root() / job_id / out_rel
    # FAST PATH (Level 1): un-edited clip with a baked captioned in R2 → 302-REDIRECT to the public
    # CDN URL. The api box transfers ZERO bytes → can't OOM. (Serving it THROUGH the box, whether
    # download-to-tmpfs or streaming, OOM-killed it: Cloud Run local FS is RAM; 40MB × concurrency
    # exhausted it → empty-body 500s.) Clip is already public on the CDN (CORS on) → both fetch and
    # native download follow the redirect. Stale-proof gate; best-effort → falls through to render.
    _cap_row = db.get_clip_edit_row(job_id, clip_id)
    if fast_export.should_serve_cached_captioned(_cap_row):
        try:
            # ?v=<version> cache-bust (как get_render): без него Cloudflare-edge мог отдать
            # УСТАРЕВШИЙ baked-captioned (тот же R2-ключ) после ре-рендера → скачанный фрейминг
            # отставал от превью. version инкрементится на каждой правке → гарантированный CDN-miss.
            url = storage.with_cache_bust(
                storage.clip_read_url(job_id, clip_id, variant="captioned"),
                (_cap_row or {}).get("version"),
            )
            return _no_referrer(RedirectResponse(url, status_code=302))
        except Exception as e:  # noqa: BLE001 — cache best-effort; fall through to full render
            print(f"[export] cached captioned redirect failed {job_id}/{clip_id}: {e}")
    try:
        render_edit_to_file(job_id, clip_id, with_subtitles=True, out_rel=out_rel)
    except JobError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not dst.exists():
        raise HTTPException(status_code=500, detail="render produced no captioned mp4")
    return _no_referrer(FileResponse(str(dst), media_type="video/mp4", filename=f"{clip_id}.mp4"))


# ──────────────── Editor handoff (EDL / FCPXML / markers / VTT / package.zip) ────────────────
# «Скачать для редактора»: пользователь уносит нарезку Quip в pro-NLE (Premiere/Resolve/FCP/CapCut)
# как SOURCE-таймкод-таймлайн (пере-нарезка из оригинала) + самодостаточные субтитры/манифест.
# Pure-билдеры — в app.editor.interchange (тайм-математика в app.editor.timecode); здесь только I/O
# (чтение артефактов + сборка zip) и гейт доступа (как SRT-экспорт). Гейт: _require_media_access
# (header ЛИБО scoped ?mt=) + Content-Disposition: attachment + Referrer-Policy: no-referrer.

# Цвет маркера DaVinci Resolve по типу момента карты видео (косметика; неизвестный тип → Green).
_MOMENT_COLORS = {
    "tension": "Red",
    "quote": "Cyan",
    "emotional": "Purple",
    "insight": "Yellow",
    "funny": "Green",
}


def _build_handoff(
    job_id: str,
) -> tuple[
    Any,
    dict[str, Any],
    list[HandoffClip],
    dict[str, Any],
    list[HandoffMarker],
    list[dict[str, Any]],
    list[HandoffMarker],
]:
    """Собрать intermediate хендофа джоба. Возвращает
    ``(meta, edits, clips, edits_by_id, markers_all, chapters, moment_markers)``.

    Core-артефакты (meta/segments/transcript) отсутствуют → JobError (наверх → 404; правило №8:
    явно, а не тихий пустой пакет). Карта видео (главы/моменты) — ОПЦИОНАЛЬНОЕ обогащение: нет
    (или ещё pending) → пустые chapters/markers (это не ошибка). Интервалы берём из edit-state
    (реальная правка юзера), падая на сегмент, если правок ещё нет.
    """
    from app import artifacts
    from app.editor.video_map import load_video_map

    meta = artifacts.load_meta(job_id)  # JobError → 404
    segments = artifacts.load_segments(job_id)  # JobError → 404
    artifacts.load_transcript_words(job_id)  # fail-fast: транскрипт обязателен для субтитров

    clips: list[HandoffClip] = []
    clip_markers: list[HandoffMarker] = []
    edits_by_id: dict[str, Any] = {}
    for i, seg in enumerate(segments, start=1):
        clip_id = f"clip_{i:02d}"
        edit = store.ensure_edit(job_id, clip_id)
        edits_by_id[clip_id] = edit
        ivs = list(edit.source_intervals) or [
            SourceInterval(source_start=seg.start, source_end=seg.end)
        ]
        clips.append(
            HandoffClip(
                name=clip_id,
                source_in_s=ivs[0].source_start,
                source_out_s=ivs[-1].source_end,
                reason=seg.reason,
                hook=seg.hook,
                why_works=seg.why_works,
                score=seg.score,
                type=seg.type.value,
                tone=seg.tone,
                key_quote=seg.key_quote,
                intervals=[
                    HandoffInterval(source_in_s=iv.source_start, source_out_s=iv.source_end)
                    for iv in ivs
                ],
            )
        )
        clip_markers.append(
            HandoffMarker(
                t_s=ivs[0].source_start,
                name=seg.hook or clip_id,
                note=seg.reason,
                color="Blue",
                kind="clip",
            )
        )

    chapters: list[dict[str, Any]] = []
    chapter_markers: list[HandoffMarker] = []
    moment_markers: list[HandoffMarker] = []
    vm = load_video_map(job_id)
    if vm is not None and vm.status == "done":
        for ch in vm.chapters:
            chapters.append(
                {"start": ch.start, "end": ch.end, "title": ch.title, "summary": ch.summary}
            )
            chapter_markers.append(
                HandoffMarker(
                    t_s=ch.start, name=ch.title, note=ch.summary, color="Purple", kind="chapter"
                )
            )
            for mo in ch.moments:
                moment_markers.append(
                    HandoffMarker(
                        t_s=mo.start,
                        name=mo.label,
                        note=mo.why,
                        color=_MOMENT_COLORS.get(mo.kind, "Green"),
                        kind="moment",
                    )
                )
    markers_all = clip_markers + chapter_markers + moment_markers
    return meta, meta.model_dump(), clips, edits_by_id, markers_all, chapters, moment_markers


def _handoff_readme(meta: Any, n_clips: int, include_clips: bool) -> str:
    """Человекочитаемый README.txt пакета (что внутри + как импортировать). English (editor-UI)."""
    df = "drop-frame" if abs(meta.fps - round(meta.fps)) > 0.001 else "non-drop"
    lines = [
        "Quip — Editor Handoff Package",
        "=============================",
        "",
        f"Source: {meta.title}",
        f"Frame rate: {meta.fps:.6f} fps ({df})",
        f"Resolution: {meta.width}x{meta.height}",
        f"Clips: {n_clips}",
        "",
        "Contents",
        "--------",
        "  manifest.json   Everything Quip knows: every clip's in/out (source timecode AND",
        "                  seconds), hook, why-it-works, score, type, tone, key quote, plus",
        "                  chapters and markers. The durable source of truth for this package.",
        "  timeline.edl    CMX3600 EDL in SOURCE timecode — re-cut from your original media.",
        "  timeline.fcpxml FCPXML timeline (DaVinci Resolve / Final Cut) with frame-accurate",
        "                  rational times, drop-frame flag, hook + chapter markers.",
        "  markers.csv     DaVinci Resolve markers (Timecode In, Name, Comment, Color).",
        "  captions/       Per-clip .srt and .vtt subtitles in CLIP time (import into CapCut,",
        "                  Premiere, Resolve, or a web player).",
    ]
    if include_clips:
        lines.append("  clips/          The rendered vertical clips (self-contained).")
    lines += [
        "",
        "How to import",
        "-------------",
        "  DaVinci Resolve : File > Import > Timeline > Pre-Conform, pick timeline.edl or",
        "                    timeline.fcpxml, then relink to your source file. Or import",
        "                    markers.csv onto a clip's timeline.",
        "  Premiere Pro    : File > Import, choose timeline.edl (interpret footage to match fps).",
        "  Final Cut Pro   : File > Import > XML, choose timeline.fcpxml.",
        "  CapCut / others : Drop the captions/*.srt onto your clip.",
        "",
        "All timecodes in the EDL / FCPXML / markers are in the ORIGINAL source's timecode,",
        "so a re-cut lands exactly where Quip cut. Subtitles are in per-clip time.",
    ]
    return "\n".join(lines) + "\n"


def _build_package_zip(job_id: str, include_clips: bool) -> bytes:
    """Собрать editor-handoff пакет в in-memory zip (bytes). JobError, если нет core-артефактов.

    include_clips=True добавляет УЖЕ ОТРЕНДЕРЕННЫЕ клипы С ДИСКА (captioned → clean), НЕ рендерит
    заново (эндпоинт дешёвый, денег/пайплайна не тратит; отсутствующие на диске клипы просто
    пропускаются — в облаке они в R2, скачивание тут не делаем)."""
    meta, meta_d, clips, edits_by_id, markers_all, chapters, moment_markers = _build_handoff(job_id)
    fps = meta.fps
    words = store.load_transcript_words(job_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "manifest.json",
            build_manifest_json({"id": job_id}, clips, markers_all, meta_d, chapters=chapters),
        )
        z.writestr("timeline.edl", build_edl(clips, fps, meta.title))
        # FCPXML: hook-маркеры идут ИЗ клипов, поэтому в аргумент markers кладём только моменты
        # (иначе clip-старт-маркеры продублировали бы hook-маркеры). Главы → chapter-marker.
        z.writestr(
            "timeline.fcpxml", build_fcpxml(clips, moment_markers, meta_d, chapters=chapters)
        )
        z.writestr("markers.csv", build_markers_csv(markers_all, fps))
        for c in clips:
            edit = edits_by_id[c.name]
            cmap = ClipTimeMap(edit.source_intervals)
            z.writestr(f"captions/{c.name}.srt", compile_srt(edit.captions, words, cmap))
            z.writestr(f"captions/{c.name}.vtt", build_vtt(edit.captions, words, cmap))
        if include_clips:
            clips_dir = store.data_root() / job_id / "clips"
            for c in clips:
                for cand in (f"{c.name}_captioned.mp4", f"{c.name}.mp4"):
                    p = clips_dir / cand
                    if p.exists():
                        z.write(str(p), f"clips/{c.name}.mp4")
                        break
        z.writestr("README.txt", _handoff_readme(meta, len(clips), include_clips))
    return buf.getvalue()


def _text_attachment(content: str, filename: str, media_type: str) -> Response:
    """Текстовый экспорт как attachment. Referrer-Policy no-referrer (как media-экспорт)."""
    return _no_referrer(
        Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    )


@app.get("/jobs/{job_id}/export.edl")
def export_edl(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """CMX3600 EDL проекта в SOURCE-таймкоде (пере-нарезка из оригинала). Owner-only (IDOR)."""
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        meta, _md, clips, *_ = _build_handoff(job_id)
    except JobError as e:
        raise HTTPException(status_code=404, detail="job artifacts not found") from e
    edl = build_edl(clips, meta.fps, meta.title)
    return _text_attachment(edl, f"{job_id}.edl", "application/octet-stream; charset=utf-8")


@app.get("/jobs/{job_id}/export.fcpxml")
def export_fcpxml(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """FCPXML-таймлайн (DaVinci Resolve / Final Cut) в SOURCE-времени. Owner-only (IDOR)."""
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        _meta, meta_d, clips, _edits, _markers, chapters, moment_markers = _build_handoff(job_id)
    except JobError as e:
        raise HTTPException(status_code=404, detail="job artifacts not found") from e
    xml = build_fcpxml(clips, moment_markers, meta_d, chapters=chapters)
    return _text_attachment(xml, f"{job_id}.fcpxml", "application/xml; charset=utf-8")


@app.get("/jobs/{job_id}/export/markers.csv")
def export_markers_csv(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """Маркеры проекта (клипы + главы + моменты) как DaVinci-CSV. Owner-only (IDOR)."""
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        meta, _md, _clips, _edits, markers_all, *_ = _build_handoff(job_id)
    except JobError as e:
        raise HTTPException(status_code=404, detail="job artifacts not found") from e
    csv_text = build_markers_csv(markers_all, meta.fps)
    return _text_attachment(csv_text, f"{job_id}_markers.csv", "text/csv; charset=utf-8")


@app.get("/jobs/{job_id}/clips/{clip_id}/export.vtt")
def export_clip_vtt(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """WebVTT субтитров клипа (сиблинг SRT, клип-время). Owner-only (IDOR): header ЛИБО ?mt=."""
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        edit = store.ensure_edit(job_id, clip_id)
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
    words = store.load_transcript_words(job_id)
    cmap = ClipTimeMap(edit.source_intervals)
    vtt = build_vtt(edit.captions, words, cmap)
    return _text_attachment(vtt, f"{clip_id}.vtt", "text/vtt; charset=utf-8")


@app.get("/jobs/{job_id}/export/package.zip")
def export_package_zip(
    job_id: str,
    include_clips: bool = False,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    mt: str | None = None,
) -> Response:
    """Весь editor-handoff пакет одним zip: manifest + EDL + FCPXML + markers.csv + per-clip
    SRT/VTT + README (+ отрендеренные клипы при ``include_clips=true``). Owner-only (IDOR):
    header ЛИБО scoped ``?mt=``. 404, если нет core-артефактов джоба."""
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        data = _build_package_zip(job_id, include_clips)
    except JobError as e:
        raise HTTPException(status_code=404, detail="job artifacts not found") from e
    return _no_referrer(
        Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="quip_{job_id}_editor_package.zip"'
            },
        )
    )


@app.patch("/jobs/{job_id}/clips/{clip_id}/edit")
def patch_clip_edit(
    job_id: str,
    clip_id: str,
    body: PatchEditBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Прямая правка субтитров (стиль/текст/highlight). Интервалы не трогает. Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    return _save_or_409(
        job_id, clip_id, edit.model_copy(update={"captions": body.captions}), body.version
    )


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/trim")
def op_trim(
    job_id: str,
    clip_id: str,
    body: TrimBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    words = store.load_transcript_words(job_id)
    new = _op_or_400(lambda: apply_trim(edit, body.word_indices, words))
    return _save_or_409(job_id, clip_id, new, body.version)


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/add-section")
def op_add_section(
    job_id: str,
    clip_id: str,
    body: AddSectionBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    words = store.load_transcript_words(job_id)
    new = _op_or_400(
        lambda: add_section(edit, body.source_start, body.source_end, body.at_index, words)
    )
    return _save_or_409(job_id, clip_id, new, body.version)


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/extend")
def op_extend(
    job_id: str,
    clip_id: str,
    body: ExtendBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    words = store.load_transcript_words(job_id)
    new = _op_or_400(
        lambda: apply_extend(edit, edge=body.edge, new_value=body.new_value, words=words)
    )
    return _save_or_409(job_id, clip_id, new, body.version)


def _tight_lock_cx(job_id: str, start: float, end: float) -> float | None:
    """Лок-центр лица для ручного tight-override: анализ лиц диапазона source → cx говорящего.
    Best-effort: сбой CV / нет лиц → None (вызывающий оставит 0.5). НЕ трогает кадровую сетку
    (только значение cx). Тяжёлый путь (ensure_source + ASD); фронт показывает «пересчитываю»."""
    from app import artifacts
    from app.pipeline.asd_reframe import score_tracks_in_segment
    from app.pipeline.stage3_reframe import locked_cx_for_tracks

    s = get_settings()
    try:
        meta = artifacts.load_meta(job_id)
        out = artifacts.ensure_source(job_id).parent
        tracks = score_tracks_in_segment(
            out / "source.mp4", meta.width, meta.height, start, end, s.reframe_face_fps,
            crop_scale=s.reframe_speaker_crop_scale,
        )  # fmt: skip
        return locked_cx_for_tracks(
            tracks, meta.width, meta.height,
            speak_threshold=s.reframe_speak_threshold, smoothing=s.reframe_smoothing,
        )  # fmt: skip
    except Exception as e:  # noqa: BLE001 — анализ best-effort; сбой → None (0.5), save не рушим
        print(f"[op_crop] WARN tight face-lock failed job={job_id} [{start:.2f},{end:.2f}]: {e}")
        return None


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/crop")
def op_crop(
    job_id: str,
    clip_id: str,
    body: CropBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    if body.mode == "auto":
        new = clear_crop_overrides(edit, body.source_start, body.source_end)
        return _save_or_409(job_id, clip_id, new, body.version)
    # Ручной tight (fill) БЕЗ явного центра → лочим кроп на ЛИЦО говорящего (иначе 0.5 = пол-лица
    # на off-center субъекте). Анализ best-effort; нет лиц/сбой → None (честный 0.5-фолбэк).
    center = body.center
    if body.mode == "fill" and center is None:
        center = _tight_lock_cx(job_id, body.source_start, body.source_end)
    ov = CropOverride(
        source_start=body.source_start,
        source_end=body.source_end,
        mode=body.mode,
        center=center,
        center_b=body.center_b,
    )
    return _save_or_409(job_id, clip_id, set_crop_override(edit, ov), body.version)


class CutBody(BaseModel):
    version: int
    source_time: float  # SOURCE seconds where to add/remove a shot boundary
    action: Literal["split", "merge", "unsplit", "unmerge"]


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/cut")
def op_cut(
    job_id: str,
    clip_id: str,
    body: CutBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """P3: ручное редактирование границ шотов. split = добавить склейку, которую AI пропустил;
    merge = погасить лишнюю AI-склейку (объединить соседние шоты); unsplit/unmerge = отмена.
    Границы frame-snapятся в apply_cuts_to_regions (Δ=0 инвариант). Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    if body.action == "split":
        new = add_manual_cut(edit, body.source_time)
    elif body.action == "merge":
        new = add_suppressed_cut(edit, body.source_time)
    elif body.action == "unsplit":
        new = remove_manual_cut(edit, body.source_time)
    else:  # unmerge
        new = remove_suppressed_cut(edit, body.source_time)
    return _save_or_409(job_id, clip_id, new, body.version)


class AspectBody(BaseModel):
    version: int
    aspect: Literal["9:16", "1:1", "4:5", "16:9"]


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/aspect")
def op_set_aspect(
    job_id: str,
    clip_id: str,
    body: AspectBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Сменить соотношение сторон клипа (T5). Меняет только выход — reframe-регионы (cx)
    переносятся, временная кадровая сетка не трогается (Δ=0 инвариант цел). Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    return _save_or_409(
        job_id, clip_id, edit.model_copy(update={"aspect": body.aspect}), body.version
    )


class RestoreEditBody(BaseModel):
    version: int
    source_intervals: list[SourceInterval]
    captions: CaptionTrack
    reframe_overrides: list[CropOverride]
    aspect: str
    lock_per_shot: bool


@app.put("/jobs/{job_id}/clips/{clip_id}/edit/restore")
def op_restore_edit(
    job_id: str,
    clip_id: str,
    body: RestoreEditBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Atomically replace the whole edit CONTENT (undo/redo). Optimistic-lock on version
    (409 on mismatch). Does NOT restore the old version NUMBER — version keeps climbing;
    undo/redo are forward writes carrying historical content. Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    new = replace_edit_content(
        edit,
        source_intervals=body.source_intervals,
        captions=body.captions,
        reframe_overrides=body.reframe_overrides,
        aspect=body.aspect,
        lock_per_shot=body.lock_per_shot,
    )
    return _save_or_409(job_id, clip_id, new, body.version)


class MotionBody(BaseModel):
    version: int
    lock_per_shot: bool


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/motion")
def op_set_motion(
    job_id: str,
    clip_id: str,
    body: MotionBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Наведение клипа: lock_per_shot=True (статичный кроп на шот) / False (слежение, follower).
    RENDER-REQUIRED — меняет ПИКСЕЛИ (пан vs статика) → клип нужно пере-рендерить (фронт ставит
    pixelDirty + предлагает Render). Превью /reframe пересчитывается (follow обходит fast-path).
    Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    return _save_or_409(
        job_id, clip_id, edit.model_copy(update={"lock_per_shot": body.lock_per_shot}), body.version
    )


class SilenceBody(BaseModel):
    version: int
    silence_config: SilenceConfig


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/silence")
def op_set_silence(
    job_id: str,
    clip_id: str,
    body: SilenceBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Удаление пауз (спека 2026-07-12): вкл/выкл + плотность (min_pause) на клип. Недеструктивно
    (toggle = флип enabled). RENDER-REQUIRED — меняет тайминг/пиксели → фронт ставит pixelDirty +
    предлагает Render. Границы дырок frame-snapятся в рендере (Δ=0). Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    edit = _load_or_404(job_id, clip_id)
    return _save_or_409(
        job_id,
        clip_id,
        edit.model_copy(update={"silence_config": body.silence_config}),
        body.version,
    )


@app.post("/jobs/{job_id}/clips/{clip_id}/edit/set-interval")
def op_set_interval(
    job_id: str,
    clip_id: str,
    body: SetIntervalBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Заменить первичный интервал клипа окном [start,end] (двигать/resize на таймлайне).

    Границы клампятся в [0,duration] и в [clip_min_sec, clip_max_sec] (set_interval, PURE).
    Optimistic-lock (409 при version mismatch). Owner-only (IDOR).
    """
    _require_owned_job(job_id, authorization, x_user_id)
    from app import artifacts

    edit = _load_or_404(job_id, clip_id)
    words = store.load_transcript_words(job_id)
    meta = artifacts.load_meta(job_id)
    s = get_settings()
    new = _op_or_400(
        lambda: set_interval(
            edit,
            body.source_start,
            body.source_end,
            words,
            duration=meta.duration,
            min_sec=s.clip_min_sec,
            max_sec=s.clip_max_sec,
        )
    )
    return _save_or_409(job_id, clip_id, new, body.version)


@app.post("/jobs/{job_id}/clips/{clip_id}/hook/regenerate")
def op_regenerate_hook(
    job_id: str,
    clip_id: str,
    body: RegenerateHookBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """W4: перегенерировать ТЕКСТ хука под текущий интервал клипа (узкий Gemini-вызов, НЕ чат).

    Хук — хранимый текст; сдвиг клипа на таймлайне его не меняет → устаревает. Эта ручка
    (явная кнопка в редакторе = opt-in, не перетираем молча) берёт транскрипт ТОЛЬКО этого клипа
    (слова в его интервалах) и просит Gemini новый хук в стиле W2. Меняем лишь ``hook.text`` —
    стиль/позицию/плашку (W5) НЕ трогаем. Цена sub-cent → НЕ метрим (как прочие правки редактора).
    Optimistic-lock (409 при version mismatch).
    """
    _require_owned_job(job_id, authorization, x_user_id)
    from app.editor.hook_ops import regenerate_hook_for_clip

    edit = _load_or_404(job_id, clip_id)
    try:
        new_edit, _hook_text = regenerate_hook_for_clip(job_id, edit)
    except JobError as e:
        # нет слов (400) / Gemini недоступен (502) — явная причина, не тихий фолбэк (правило №8).
        code = 400 if "no words" in str(e) else 502
        raise HTTPException(status_code=code, detail=f"hook regeneration failed: {e}") from e
    return _save_or_409(job_id, clip_id, new_edit, body.version)


# ─────────────────────────── W3: агентный чат-редактор клипа ───────────────────────────

_AGENT_TERMINAL = ("done", "failed", "cancelled")
_AGENT_MSG_MAX = 2000
_AGENT_HISTORY_SEED_MAX = 40  # сколько прошлых событий ленты подсевать в новый run (история чата)


def _seed_events(prior: dict[str, Any] | None) -> list[dict[str, Any]]:
    """События прошлого прогона клипа для подсева в новый run (история чата, #1). Кап последних
    N → UI показывает непрерывный тред без безграничного роста ленты."""
    if not prior:
        return []
    evs = prior.get("events") or []
    return list(evs[-_AGENT_HISTORY_SEED_MAX:])


def _agent_payload(run: dict[str, Any]) -> dict[str, Any]:
    """Ряд agent_run → wire-AgentRun (без внутреннего function_call_id)."""
    return AgentRun(
        run_id=run["run_id"],
        job_id=run["job_id"],
        clip_id=run["clip_id"],
        status=run["status"],
        events=[AgentEvent(**e) for e in run["events"]],
        error=run.get("error"),
        cancellable=run["cancellable"],
    ).model_dump()


def _run_agent_bg(run_id: str) -> None:
    from app.agent.clip_agent import run_clip_agent

    run_clip_agent(run_id)


@app.post("/jobs/{job_id}/clips/{clip_id}/agent/start")
def agent_start(
    job_id: str,
    clip_id: str,
    body: AgentStartBody,
    bg: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """W3: запустить агента на клипе. Один активный run на клип (идемпотентно). Спавн как у джоба;
    отмена — `function_call_id`. Биллинг не трогаем. Сбой диспатча → failed.

    Owner-only (IDOR): раньше читался ТОЛЬКО X-User-Id и JWT НЕ валидировался — теперь идём
    через _require_owned_job (реальная проверка токена + владельца при включённом auth)."""
    from app.agent import runs_store

    _row, user_id = _require_owned_job(job_id, authorization, x_user_id)
    try:
        store.ensure_edit(job_id, clip_id)  # создаём дефолт из сегмента, если ещё нет
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="empty message")
    msg = msg[:_AGENT_MSG_MAX]
    existing = runs_store.running_run(job_id, clip_id)
    if existing is not None:
        return _agent_payload(existing)  # уже работает → не плодим (дабл-клик/гонка)
    # История чата (#1): прошлый прогон клипа ДО создания нового (latest_run иначе вернёт пустой
    # новый). Засеваем его ленту в новый run → UI = непрерывный тред, агент = память (run_clip_agent
    # берёт prior из ленты). Берём ДО create_run.
    prior = runs_store.latest_run(job_id, clip_id)
    run_id = runs_store.new_run_id()
    runs_store.create_run(run_id, job_id, clip_id, user_id)
    for ev in _seed_events(prior):
        runs_store.append_event(run_id, ev)
    runs_store.append_event(run_id, AgentEvent(role="user", text=msg).model_dump())
    if dispatch.spawn_enabled():
        try:
            fc_id = dispatch.spawn("agent_edit_job", run_id)
        except Exception as e:  # noqa: BLE001 — сбой диспатча видим (правило №8)
            runs_store.set_status(run_id, "failed", f"dispatch failed: {e}")
            raise HTTPException(status_code=500, detail=f"agent dispatch failed: {e}") from e
        if fc_id:
            runs_store.set_function_call_id(run_id, fc_id)
    else:
        bg.add_task(_run_agent_bg, run_id)
    run = runs_store.get_run(run_id)
    assert run is not None
    return _agent_payload(run)


@app.get("/jobs/{job_id}/clips/{clip_id}/agent/active")
def agent_active(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any] | None:
    """Активный (running) прогон для клипа — реконнект после перезагрузки вкладки. Owner-only."""
    _require_owned_job(job_id, authorization, x_user_id)
    from app.agent import runs_store

    run = runs_store.running_run(job_id, clip_id)
    return _agent_payload(run) if run is not None else None


@app.get("/jobs/{job_id}/clips/{clip_id}/agent/{run_id}")
def agent_get(
    job_id: str,
    clip_id: str,
    run_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Статус+лента прогона (поллинг фронтом). Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    from app.agent import runs_store

    run = runs_store.get_run(run_id)
    if run is None or run["job_id"] != job_id or run["clip_id"] != clip_id:
        raise HTTPException(status_code=404, detail="agent run not found")
    return _agent_payload(run)


@app.post("/jobs/{job_id}/clips/{clip_id}/agent/{run_id}/cancel")
def agent_cancel(
    job_id: str,
    clip_id: str,
    run_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Stop: отменить прогон агента. Идемпотентно на терминале. $0 (агент минут не списывает).
    Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    from app.agent import runs_store

    run = runs_store.get_run(run_id)
    if run is None or run["job_id"] != job_id or run["clip_id"] != clip_id:
        raise HTTPException(status_code=404, detail="agent run not found")
    if run["status"] in _AGENT_TERMINAL:
        return _agent_payload(run)
    fc_id = run.get("function_call_id")
    if fc_id:
        try:
            dispatch.cancel(str(fc_id))  # Cloud Run execution cancel / Modal FunctionCall cancel
        except Exception as e:  # noqa: BLE001 — отмена не удалась → НЕ молча (правило №8)
            runs_store.set_cancelled(run_id)
            raise HTTPException(status_code=502, detail=f"cancel failed: {e}") from e
    runs_store.set_cancelled(run_id)
    updated = runs_store.get_run(run_id)
    assert updated is not None
    return _agent_payload(updated)


@app.post("/jobs/{job_id}/clips/{clip_id}/render", status_code=202)
def post_render(
    job_id: str,
    clip_id: str,
    bg: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Async-рендер mp4 из edit-state. Статус — GET …/render. Owner-only (IDOR).

    На Modal — spawn отдельной CPU-функции (web scale-to-zero убил бы фон-рендер); локально — bg.
    """
    _require_owned_job(job_id, authorization, x_user_id)
    _load_or_404(job_id, clip_id)
    db.set_render_status(job_id, clip_id, "rendering", None, None)
    if dispatch.spawn_enabled():
        # spawn может упасть (modal import/lookup) ДО старта рендера → клип застрял бы в
        # "rendering" навсегда. Переводим в failed и поднимаем 500 (правило №8, не молча).
        try:
            dispatch.spawn("render_job", job_id, clip_id)
        except Exception as e:  # noqa: BLE001 — любой сбой диспатча = видимый failed
            db.set_render_status(job_id, clip_id, "failed", None, f"dispatch failed: {e}")
            raise HTTPException(status_code=500, detail=f"render dispatch failed: {e}") from e
    else:
        bg.add_task(render_clip_edit_job, job_id, clip_id)
    return {"status": "rendering"}


@app.get("/jobs/{job_id}/clips/{clip_id}/render")
def get_render(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_owned_job(job_id, authorization, x_user_id)
    from app import storage

    row = db.get_clip_edit_row(job_id, clip_id)
    if row is None:
        raise HTTPException(status_code=404, detail="clip not found")
    url = row.get("render_url")
    # D6: долговечный маркер ключа r2://<key> → СВЕЖИЙ presign на чтении (не протухает);
    # абсолютный http (R2 CDN/presigned) — как есть; относительный → воркер-раздача /media.
    if url:
        url = str(url)
        if storage.is_r2_key_ref(url):
            url = storage.resolve_media_url(url)
        elif not url.startswith("http"):
            url = f"media/{job_id}/{url}"
        # Cache-bust the stable CDN URL by the edit version so a RE-render (same R2 key,
        # overwritten) isn't served stale from Cloudflare's edge cache → the new hook size /
        # caption edits actually reach the download. No-op for relative/local URLs.
        url = storage.with_cache_bust(url, row.get("version"))
    return {
        "status": row.get("render_status"),
        "video_url": url or None,
        "error": row.get("render_error"),
    }


# ── /reframe: тяжёлый CV НЕ живёт в HTTP-запросе web-Service'а (прод-OOM 2026-07-13) ──
# Инцидент: промах кэша регионов → ВНУТРИ запроса качался source.mp4 в RAM-FS (8Gi, filesystem=RAM)
# и гонялся PySceneDetect + ffmpeg-JPG@25fps + MediaPipe + torch-ASD (80–195с) → «Memory limit of
# 8192 MiB exceeded» → контейнер убит ВМЕСТЕ со всеми in-flight запросами (массовые 503). CV теперь
# уезжает в Cloud Run Job (32Gi, 8 CPU, реальный диск), а здесь остаются два предохранителя.

# Гвардия от стампида спавнов: (job, clip) → monotonic-время последнего спавна rebake-job'а. Фронт
# поллит тот же GET, и БЕЗ этого каждый поллинг спавнил бы новый Job. ⚠️ ПРОЦЕССНАЯ и best-effort:
# Cloud Run держит N контейнеров, у каждого свой дикт → дубль-спавн между контейнерами возможен
# (безвреден — rebake идемпотентен, перепечёт тот же payload). Фронт дедупит со своей стороны.
_REBAKE_SPAWNED: dict[tuple[str, str], float] = {}
_REBAKE_LOCK = threading.Lock()

# Потолок ОДНОВРЕМЕННЫХ in-process CV (остался только edited-кейс: трим/follow — его persisted
# payload обслужить не может). Ленивая инициализация — размер из settings в рантайме (тест может
# подменить сам объект).
_CV_SEMAPHORE: threading.Semaphore | None = None
_CV_SEM_LOCK = threading.Lock()


def _cv_semaphore() -> threading.Semaphore:
    """Семафор in-process reframe-CV (``REFRAME_API_CV_CONCURRENCY``, деф. 2). Процессный."""
    global _CV_SEMAPHORE
    with _CV_SEM_LOCK:
        if _CV_SEMAPHORE is None:
            _CV_SEMAPHORE = threading.Semaphore(max(1, get_settings().reframe_api_cv_concurrency))
        return _CV_SEMAPHORE


def _claim_rebake_spawn(job_id: str, clip_id: str, ttl_sec: float) -> bool:
    """True — этот запрос ИМЕЕТ ПРАВО заспавнить rebake-job для клипа (иначе он уже печётся).

    Best-effort, ПРОЦЕССНЫЙ (см. ``_REBAKE_SPAWNED``): защищает от стампида поллингов ОДНОГО
    контейнера, а не от дубля между контейнерами. Протухшие записи (> ttl) чистим тут же.
    """
    now = time.monotonic()
    with _REBAKE_LOCK:
        for k, ts in list(_REBAKE_SPAWNED.items()):
            if now - ts > ttl_sec:
                del _REBAKE_SPAWNED[k]
        key = (job_id, clip_id)
        if key in _REBAKE_SPAWNED:
            return False
        _REBAKE_SPAWNED[key] = now
        return True


def _drop_rebake_claim(job_id: str, clip_id: str) -> None:
    """Снять заявку на спавн (спавн упал) → следующий поллинг честно попробует снова."""
    with _REBAKE_LOCK:
        _REBAKE_SPAWNED.pop((job_id, clip_id), None)


@app.get("/jobs/{job_id}/clips/{clip_id}/reframe", response_model=None)
def get_clip_reframe(
    job_id: str,
    clip_id: str,
    compute: bool = True,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any] | Response:
    """Reframe-план клипа (fit/fill + центры) для честного превью кадра в редакторе.

    Контракт (после прод-OOM 2026-07-13 — тяжёлый CV выселен из web-контейнера):
      • **200** ``{"regions": [...]}`` — fast-path (запечённые регионы дефолтного интервала:
        мгновенно, без CV и без скачивания source) ИЛИ успешный in-process CV edited-кейса.
      • **202** ``{"status": "computing"}`` — промах кэша на ДЕФОЛТНОМ интервале: спавним Cloud Run
        Job ``rebake_regions`` (32Gi, реальный диск) и просим клиента поллить ЭТОТ ЖЕ GET. CV в API
        НЕ гоняем. Локально/dev (нет ``dispatch.spawn_enabled()``) — прежнее синхронное поведение.
      • **204** — ``compute=false`` и регионов нет: neighbour-prefetch редактора не платит за CV.
      • **503** — CV честно упал ИЛИ семафор in-process CV занят (+ ``Retry-After``); молчаливого
        фолбэка в центр-кроп нет (правило №8) — состояние видно.

    D2 (историческое): фронт когда-то тянул ``media/<job>/reframe_<clip>.json`` со StaticFiles — на
    облаке этого файла нет (он на scratch batch-контейнера) → превью откатывалось в центр-кроп ≠
    рендер. План считает ЕДИНЫЙ frame-accurate путь ``resolve_regions_accurate`` (тот же, что у
    рендера) → превью-план == рендер-план в обеих средах и после трима/сдвига интервала.
    """
    _require_owned_job(job_id, authorization, x_user_id)
    from app import artifacts
    from app.editor.reframe_cache import (
        build_persist_payload,
        intervals_match_default,
        regions_from_persisted,
        regions_to_clip_time,
        resolve_regions_accurate,
    )

    try:
        edit = store.ensure_edit(job_id, clip_id)
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e

    # ── Домен 1: FAST-PATH — реальные границы шотов из persist (без CV, без скачивания source) ──
    # batch-пайплайн (или rebake-job) уже посчитал PySceneDetect-границы дефолтного интервала и
    # сохранил их. Если клип НЕ тримили/не сдвигали (один интервал == дефолт), отдаём их МГНОВЕННО
    # (+ перекрас под ручные override'ы/cut'ы — их накладывает читатель, payload остаётся чистым).
    persisted = db.get_reframe_regions(job_id, clip_id)
    # meta.json — лёгкий JSON-ридер (НЕ скачивание source); нужен fast-path'у для frame-snap
    # границ override-split в нативном fps (инвариант кадровой сетки). Тяжёлый ensure_source
    # остаётся ниже, на slow-path.
    meta = artifacts.load_meta(job_id)
    # follow (lock_per_shot=False) НЕ может брать запечённые регионы (они LOCKED) → пересчёт ниже.
    if (
        edit.lock_per_shot
        and persisted
        and intervals_match_default(
            edit.source_intervals,
            persisted.get("default_start", -1),
            persisted.get("default_end", -1),
        )
    ):
        iv = edit.source_intervals[0]
        return {
            "regions": regions_from_persisted(
                persisted,
                iv,
                edit.reframe_overrides,
                fps=meta.fps,
                manual_cuts=edit.manual_cuts,
                suppressed_cuts=edit.suppressed_cuts,
            )
        }

    # ── Кэш промахнулся. Платить ли за CV — решаем ДО того, как трогать source ──
    # compute=false (neighbour-prefetch соседних клипов в редакторе): регионов нет → 204, и ни
    # спавна, ни CV. Клип, который юзер не открыл, не должен стоить ни одного контейнера.
    if not compute:
        return Response(status_code=204)

    s = get_settings()
    seg = segment_bounds(job_id, clip_id)
    # ДЕФОЛТНЫЙ кейс (клип не тримили, lock): именно его и обслуживает persisted-payload — значит
    # его можно испечь ОДИН раз в большом контейнере и отдавать fast-path'ом всем следующим
    # запросам. Считать это в web-контейнере = OOM (инцидент). → спавним Job, отвечаем 202.
    bakeable = (
        edit.lock_per_shot
        and persisted is None
        and seg is not None
        and intervals_match_default(edit.source_intervals, seg[0], seg[1])
    )
    if bakeable and dispatch.spawn_enabled():
        if _claim_rebake_spawn(job_id, clip_id, s.reframe_rebake_guard_sec):
            try:
                dispatch.spawn("rebake_regions", job_id, clip_id)
            except Exception as e:  # noqa: BLE001 — сбой диспатча ВИДИМ (правило №8), не молча
                _drop_rebake_claim(job_id, clip_id)  # следующий поллинг попробует снова
                raise HTTPException(
                    status_code=500, detail=f"reframe rebake dispatch failed: {e}"
                ) from e
        return JSONResponse(status_code=202, content={"status": "computing"})

    # ── Честный in-process CV: edited-кейс (трим/follow/сдвиг), который persisted обслужить НЕ
    # может, + локальный/dev-режим без job-бэкенда. Под СЕМАФОРОМ: N параллельных CV складывают
    # source.mp4 (RAM-FS) + MediaPipe/torch → тот самый OOM. Слот не освободился за
    # reframe_api_cv_wait_sec → честный 503 + Retry-After (не копим очередь в 300с request-timeout
    # Cloud Run, где клиент всё равно получит 504).
    sem = _cv_semaphore()
    if not sem.acquire(timeout=s.reframe_api_cv_wait_sec):
        raise HTTPException(
            status_code=503,
            detail="reframe analysis busy (too many concurrent computations)",
            headers={"Retry-After": "5"},
        )
    try:
        out = artifacts.ensure_source(job_id).parent
        try:
            region_lists = resolve_regions_accurate(
                out / "source.mp4",
                edit.source_intervals,
                edit.reframe_overrides,
                src_w=meta.width,
                src_h=meta.height,
                fps=meta.fps,
                clip_id=clip_id,
                out_dir=out,
                cache_dir=out / "analysis",
                mode_setting=s.reframe_mode,
                speaker_crop_scale=s.reframe_speaker_crop_scale,
                face_fps=s.reframe_face_fps,
                smoothing=s.reframe_smoothing,
                lock_per_shot=edit.lock_per_shot,
                min_hold_sec=s.reframe_min_hold_sec,
                speak_threshold=s.reframe_speak_threshold,
                scene_threshold=s.reframe_scene_threshold,
                min_scene_sec=s.reframe_min_scene_sec,
                split_enabled=s.reframe_split_enabled,
                # WYSIWYG: тот же гибрид-порог, что у рендера/батча (render_edit_to_file его
                # передаёт). Без него превью бралось дефолтом resolve_regions_accurate (0.3) ≠
                # настройке → широкий план в гриде показан горизонтально (fit), а в
                # редакторе-превью — кропом (fill).
                wide_speak_min=s.reframe_wide_speak_min,
                manual_cuts=edit.manual_cuts,
                suppressed_cuts=edit.suppressed_cuts,
            )
        except Exception as e:  # noqa: BLE001 — тяжёлый CV мог упасть/таймаутнуть на холодном
            # Явная ошибка вместо непрозрачного 500 (правило №8): фронт отличит «не смогли
            # посчитать» от «считается». Без тихого фолбэка — состояние видно.
            raise HTTPException(
                status_code=503, detail="reframe analysis unavailable (try again shortly)"
            ) from e
    finally:
        sem.release()

    # Backfill (локальный/dev-путь и старые джобы): только что посчитали реальные регионы on-demand
    # → сохраним, чтобы следующий (холодный) контейнер взял fast-path. ЖЁСТКИЕ условия безопасности:
    #   • persisted is None — НИКОГДА не перетираем канонический batch-entry (или чужой shift);
    #   • без reframe_overrides — region_lists ЧИСТЫЕ авто-регионы (fast-path наложит override сам);
    #   • один интервал — payload хранит ровно один дефолтный интервал.
    # Персистим как канонические ТОЛЬКО locked-регионы (follow НЕ перетирает baked-дефолт).
    if (
        edit.lock_per_shot
        and persisted is None
        and not edit.reframe_overrides
        and len(region_lists) == 1
    ):
        iv = edit.source_intervals[0]
        try:
            db.put_reframe_regions(
                job_id,
                clip_id,
                build_persist_payload(
                    region_lists[0], iv.source_start, iv.source_end, fps=meta.fps
                ),
            )
        except Exception as e:  # noqa: BLE001 — backfill best-effort, ответ важнее
            print(
                f"[get_clip_reframe] WARN backfill persist failed job={job_id} clip={clip_id}: {e}"
            )

    return {"regions": regions_to_clip_time(region_lists, edit.source_intervals)}


@app.get("/jobs/{job_id}/clips/{clip_id}/analysis")
def get_analysis(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Интервалы + слова клипа (для клиент-превью субтитров/таймлайна). Owner-only (IDOR)."""
    _require_owned_job(job_id, authorization, x_user_id)
    try:
        edit = store.ensure_edit(job_id, clip_id)
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
    words = store.load_transcript_words(job_id)
    in_clip = [
        w.model_dump()
        for w in words
        if any(iv.source_start <= w.start < iv.source_end for iv in edit.source_intervals)
    ]
    return {"intervals": [iv.model_dump() for iv in edit.source_intervals], "words": in_clip}


@app.post("/jobs/{job_id}/clips/{clip_id}/refresh-analysis")
def refresh_clip_analysis(
    job_id: str,
    clip_id: str,
    body: RefreshClipBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> ClipOut:
    """Re-run tone/key_quote/score analysis for a single clip after user edits its boundaries."""
    from app import artifacts
    from app.models import Transcript
    from app.pipeline.stage2_select import select_segments
    from app.run import build_clip_out

    _require_owned_job(job_id, authorization, x_user_id)  # 401/403/404 (owner-only, IDOR)

    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    existing = next((c for c in (job.get("clips") or []) if c.get("id") == clip_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="clip not found")

    try:
        transcript = artifacts.load_transcript(job_id)
        meta = artifacts.load_meta(job_id)
    except JobError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    sliced_words = [w for w in transcript.words if body.start <= w.start and w.end <= body.end]
    if not sliced_words:
        raise HTTPException(status_code=422, detail="no words found in requested range")

    transcript_slice = Transcript(
        language=transcript.language,
        duration=body.end - body.start,
        words=sliced_words,
    )
    try:
        segments = select_segments(transcript_slice, meta.title, max_clips=1, usage_sink={})
    except JobError as e:
        raise HTTPException(status_code=502, detail=f"analysis failed: {e}") from e
    if not segments:
        raise HTTPException(status_code=422, detail="model returned no segment for this range")

    video_url = existing.get("video_url") or ""
    clip_out = build_clip_out(clip_id, segments[0], transcript.words, video_url)
    clip_out = clip_out.model_copy(update={"thumbnail_url": existing.get("thumbnail_url")})

    db.patch_clip_analysis(
        job_id,
        clip_id,
        {
            "score": clip_out.score,
            "reason": clip_out.reason,
            "type": clip_out.type if isinstance(clip_out.type, str) else clip_out.type.value,
            "hook": clip_out.hook,
            "why_works": clip_out.why_works,
            "hook_style": clip_out.hook_style,
            "tone": clip_out.tone,
            "key_quote": clip_out.key_quote,
        },
    )
    return clip_out


# ──────────────────────────── Preset endpoints ────────────────────────────


class SavePresetBody(BaseModel):
    name: str
    style: CaptionStyle
    highlight: HighlightStyle | None = None


class ApplyPresetBody(BaseModel):
    version: int
    preset_id: str


@app.get("/presets")
def get_presets() -> list[dict[str, Any]]:
    return [p.model_dump() for p in presets_mod.list_presets()]


@app.post("/presets")
def create_preset(body: SavePresetBody) -> dict[str, Any]:
    preset = CaptionPreset(
        id=f"preset_{uuid.uuid4().hex[:8]}",
        name=body.name,
        style=body.style,
        highlight=body.highlight,
    )
    return presets_mod.save_preset(preset).model_dump()


@app.post("/jobs/{job_id}/clips/{clip_id}/apply-preset")
def apply_preset_to_clip(
    job_id: str,
    clip_id: str,
    body: ApplyPresetBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_owned_job(job_id, authorization, x_user_id)
    preset = presets_mod.get_preset(body.preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    edit = _load_or_404(job_id, clip_id)
    return _save_or_409(job_id, clip_id, presets_mod.apply_preset(edit, preset), body.version)


# ──────────────────────── Style memory (domain 5) ────────────────────────


class StylePrefBody(BaseModel):
    """Saved/applied LOOK = caption style + karaoke highlight + hook style fields (partial dict)."""

    style: CaptionStyle
    highlight: HighlightStyle | None = None
    hook_style: dict[str, Any] | None = None


@app.post("/jobs/{job_id}/apply-style-all")
def apply_style_all(
    job_id: str,
    body: StylePrefBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Apply ONE clip's look (caption style + highlight + hook style) to EVERY clip of the job.

    Per-clip content & position are preserved (replies, intervals, reframe, margins, hook text).
    Owner-only. Returns how many clips were updated.
    """
    # Общий гейт: резолв юзера (401), загрузка строки (404) + 403 на ЧУЖОГО владельца. Массовая
    # мутация, однако, СТРОЖЕ обычного read/single-clip гейта: fail-CLOSED и на owner=None (ничья/
    # legacy-джоба никому не принадлежит → править ВСЕ её клипы нельзя, иначе IDOR). Обычный гейт
    # делает None-owner BYPASS → добиваем None отдельной проверкой ниже (поведение сохранено).
    row, user_id = _require_owned_job(job_id, authorization, x_user_id)
    if not row:  # auth off → гейт pass-through; грузим строку для списка клипов (+ 404 в dev)
        fetched = db.get_job_row(job_id)
        if fetched is None:
            raise HTTPException(status_code=404, detail="job not found")
        row = fetched
    if auth.supabase_auth_enabled() and (row.get("user_id") or None) != user_id:
        raise HTTPException(status_code=403, detail="not your job")
    job = db.get_job(job_id) or {}
    clip_ids = [c["id"] for c in (job.get("clips") or []) if c.get("id")]
    applied = 0
    for clip_id in clip_ids:
        # ensure_edit so a not-yet-opened clip also gets the look (lazy default created first).
        try:
            edit = store.ensure_edit(job_id, clip_id)
        except (FileNotFoundError, KeyError, JobError):
            continue  # clip without a segment artifact → skip (explicit, not silent fallback)
        styled = style_prefs.apply_style_to_edit(edit, body.style, body.highlight, body.hook_style)
        try:
            store.save_edit(job_id, clip_id, styled, expected_version=edit.version)
        except EditConflict:
            # Concurrent edit on this clip → refetch once and reapply on the fresh version.
            fresh = store.load_edit(job_id, clip_id)
            if fresh is None:
                continue
            store.save_edit(
                job_id,
                clip_id,
                style_prefs.apply_style_to_edit(fresh, body.style, body.highlight, body.hook_style),
                expected_version=fresh.version,
            )
        applied += 1
    return {"applied": applied, "total": len(clip_ids)}


class TemplateBody(StylePrefBody):
    """Save a NAMED style template = the look (caption style + highlight + hook style) + a name
    + an optional flag to make it the default that seeds new clips of future videos."""

    id: str | None = None  # present → overwrite THAT template in place (Update); absent → mint new
    name: str = "My style"
    set_default: bool = False


class DefaultFlagBody(BaseModel):
    is_default: bool = True


@app.get("/me/templates")
def list_my_templates(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """The user's saved style templates + which one (if any) seeds new clips of future videos."""
    user_id = _resolve_user(authorization, x_user_id)
    if not user_id:
        return {"templates": [], "default_id": None}
    blob = db.get_style_preference(user_id)
    return {
        "templates": style_prefs.list_templates(blob),
        "default_id": style_prefs.default_template_id(blob),
    }


@app.post("/me/templates")
def save_my_template(
    body: TemplateBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Save the current clip's look as a NAMED template (reusable on any clip / whole video)."""
    user_id = _resolve_user(authorization, x_user_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to save a template")
    # Client-supplied id (Update button) overwrites that template in place via upsert_template;
    # absent → mint a new id (Save). Bogus ids only affect the user's own style_preferences blob.
    tid = body.id or uuid.uuid4().hex[:12]
    name = body.name.strip()[:60] or "My style"
    tpl = style_prefs.build_template(tid, name, body.style, body.highlight, body.hook_style)
    blob = style_prefs.upsert_template(db.get_style_preference(user_id), tpl)
    if body.set_default:
        blob = style_prefs.set_default_template(blob, tid)
    db.set_style_preference(user_id, blob)
    return {"template": tpl, "default_id": style_prefs.default_template_id(blob)}


@app.delete("/me/templates/{template_id}")
def delete_my_template(
    template_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Delete a saved style template."""
    user_id = _resolve_user(authorization, x_user_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in")
    blob = style_prefs.delete_template(db.get_style_preference(user_id), template_id)
    db.set_style_preference(user_id, blob)
    return {"ok": True}


@app.put("/me/templates/{template_id}/default")
def set_my_default_template(
    template_id: str,
    body: DefaultFlagBody,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Flag (or unflag) a template as the default that seeds new clips of future videos."""
    user_id = _resolve_user(authorization, x_user_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in")
    blob = db.get_style_preference(user_id)
    blob = style_prefs.set_default_template(blob, template_id if body.is_default else None)
    db.set_style_preference(user_id, blob)
    return {"ok": True}
