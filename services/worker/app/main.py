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
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__, auth, billing, db, dispatch, media_token, storage
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
    add_section,
    apply_extend,
    apply_trim,
    clear_crop_overrides,
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
    SourceInterval,
)
from app.pipeline.stage5_render import aspect_to_dims
from app.run import DATA_ROOT
from app.tasks import (
    render_clip_edit_job,
    render_edit_to_file,
    run_pipeline_job,
    run_upload_job,
    run_upload_job_from_r2,
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

# Раздача артефактов: data/<job_id>/clips/<clip>.mp4 → /media/<job_id>/clips/<clip>.mp4
DATA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(DATA_ROOT)), name="media")


class CreateJobBody(BaseModel):
    source_type: str
    source_ref: str
    max_clips: int | None = Field(default=None, ge=1, le=30)  # UI-степпер; None → дефолт воркера
    language: str | None = None  # ISO-код языка (kk=казахский→Groq; None=авто-детект Deepgram)


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

    Бесплатный план (2 видео) абьюзят пачками непроверенных/одноразовых ящиков. Гейт активен
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
    db.insert_job(job_id, body.source_type, body.source_ref, user_id=user_id)
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
        )
    return {"id": job_id, "status": "queued", "stage": "queued", "progress": 0}


@app.post("/jobs/upload", status_code=202)
async def create_upload_job(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    max_clips: int | None = Form(default=None, ge=1, le=30),
    language: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Создать задачу из ЗАГРУЖЕННОГО файла: стримим на диск → фон-импорт → пайплайн.

    Файл пишется чанками в data/<job_id>/upload.<ext> (не держим в памяти); затем
    run_upload_job готовит source.mp4/wav/meta и гоняет тот же пайплайн, что и URL-путь.
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
    db.insert_job(job_id, "upload", filename, user_id=user_id)
    # На Modal: web — scale-to-zero ASGI-контейнер; BackgroundTask умер бы на полпути (как и у
    # YouTube-пути). Поэтому стейджим исходник в R2 (upload_source) и spawn'им долгоживущую
    # upload_job (она скачает исходник из R2 на своём контейнере → run_upload_job). Локально/dev
    # (MODAL_SPAWN не задан) — BackgroundTask на этом же процессе, как в Phase 0.
    if dispatch.spawn_enabled():
        try:
            storage.upload_source(upload_path, job_id)  # r2-режим; иначе no-op
            dispatch.spawn("upload_job", job_id, filename, max_clips, user_id, language)
        except Exception as e:  # noqa: BLE001 — любой сбой диспатча = видимый failed, не вечная очередь
            db.set_failed(job_id, f"dispatch failed: {e}")
            raise HTTPException(status_code=500, detail=f"upload dispatch failed: {e}") from e
    else:
        bg.add_task(
            run_upload_job, job_id, str(upload_path), filename, max_clips, user_id, language
        )
    return {"id": job_id, "status": "queued", "stage": "queued", "progress": 0}


class UploadUrlBody(BaseModel):
    filename: str = "upload.mp4"
    max_clips: int | None = Field(default=None, ge=1, le=30)
    # Размер файла (байты) — известен браузеру. >5 ГБ нельзя одним PUT (лимит R2), а крупные и
    # вообще выгоднее multipart (параллельные части + resume). None/малый → single-PUT (как раньше).
    size: int | None = Field(default=None, ge=0)


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
    # Multipart-сборка (если грузили частями): upload_id + ETag'и частей. None → single-PUT
    # (объект уже в R2, собирать нечего).
    upload_id: str | None = None
    parts: list[CompletedPart] | None = None


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
    db.insert_job(job_id, "upload", body.filename, user_id=user_id)
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
    if dispatch.spawn_enabled():
        try:
            fc_id = dispatch.spawn(
                "upload_job", job_id, body.filename, body.max_clips, user_id, body.language
            )
            db.set_function_call_id(job_id, fc_id)  # для Stop-кнопки (отмена джоба)
        except Exception as e:  # noqa: BLE001 — сбой диспатча = видимый failed, не вечная очередь
            db.set_failed(job_id, f"dispatch failed: {e}")
            raise HTTPException(status_code=500, detail=f"upload dispatch failed: {e}") from e
    else:
        bg.add_task(
            run_upload_job_from_r2, job_id, body.filename, body.max_clips, user_id, body.language
        )
    return {"id": job_id, "status": "queued", "stage": "queued", "progress": 0}


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

    Рендерит текущий edit-state с ass_name=None в clips/{clip}_clean.mp4 и отдаёт файл.
    Синхронно: рендер ~секунды (FastAPI крутит sync-эндпоинт в threadpool). Очередь/статус —
    на этапе масштаба (infra-план §2.3). Сбой рендера → HTTP 500 (правило №8, не тихо).
    Owner-only (IDOR): header ЛИБО scoped-тикет ``?mt=`` (нативный download).
    """
    _require_media_access(job_id, authorization, x_user_id, mt)
    try:
        store.ensure_edit(job_id, clip_id)  # из грида (без открытия редактора) тоже работает
    except (FileNotFoundError, KeyError, JobError) as e:
        raise HTTPException(status_code=404, detail="clip/segment not found") from e
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
    try:
        render_edit_to_file(job_id, clip_id, with_subtitles=True, out_rel=out_rel)
    except JobError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    path = store.data_root() / job_id / out_rel
    if not path.exists():
        raise HTTPException(status_code=500, detail="render produced no captioned mp4")
    return _no_referrer(FileResponse(str(path), media_type="video/mp4", filename=f"{clip_id}.mp4"))


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


@app.get("/jobs/{job_id}/clips/{clip_id}/reframe")
def get_clip_reframe(
    job_id: str,
    clip_id: str,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Reframe-план клипа (fit/fill/split + центры) для честного превью кадра в редакторе.

    D2: раньше фронт тянул ``media/<job>/reframe_<clip>.json`` напрямую со StaticFiles —
    на облаке (Modal/R2) этот файл лежит только на scratch-диске batch-контейнера → 404 →
    превью откатывалось в центр-кроп (≠ рендер). Теперь план считает ЕДИНЫЙ frame-accurate
    путь ``resolve_regions_accurate`` (тот же, что у рендера; кэш ``analysis/acc_*.json``,
    источник из R2 через artifacts.ensure_source) для ТЕКУЩИХ интервалов edit-state →
    превью-план == рендер-план, и в обоих средах, и после сдвига/трима интервала.
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
    # batch-пайплайн уже посчитал PySceneDetect-границы дефолтного интервала и сохранил их. Если
    # клип НЕ тримили/не сдвигали (один интервал == дефолт), отдаём их МГНОВЕННО (+ перекрас под
    # ручные override'ы). Это и есть фикс «полоса шотов = равные чанки»: на холодном контейнере
    # /reframe больше не гоняет тяжёлый CV (который тормозил/падал 500) для нетронутого клипа.
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
            "regions": regions_from_persisted(persisted, iv, edit.reframe_overrides, fps=meta.fps)
        }

    out = artifacts.ensure_source(job_id).parent
    s = get_settings()
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
            # WYSIWYG: тот же гибрид-порог, что у рендера/батча (render_edit_to_file его передаёт).
            # Без него превью бралось дефолтом resolve_regions_accurate (0.3) ≠ настройке → широкий
            # план в гриде показан горизонтально (fit), а в редакторе-превью — кропом (fill).
            wide_speak_min=s.reframe_wide_speak_min,
        )
    except Exception as e:  # noqa: BLE001 — тяжёлый CV мог упасть/таймаутнуть на холодном
        # Явная ошибка вместо непрозрачного 500 (правило №8): фронт отличит «не смогли посчитать»
        # от «считается». Без тихого фолбэка — состояние видно.
        raise HTTPException(
            status_code=503, detail="reframe analysis unavailable (try again shortly)"
        ) from e

    # Backfill (старые джобы до миграции): только что посчитали реальные регионы on-demand →
    # сохраним, чтобы следующий (холодный) контейнер взял fast-path. ЖЁСТКИЕ условия безопасности:
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
                build_persist_payload(region_lists[0], iv.source_start, iv.source_end),
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
