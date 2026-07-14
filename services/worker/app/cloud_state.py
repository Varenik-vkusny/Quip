"""Облачный стейт воркера в Supabase Postgres через PostgREST (service_role).

Зеркало диск/SQLite-стейта (`app/db.py`) для boevoy-режима (Modal): джобы, артефакты
пайплайна (meta/segments/transcript), правки клипов и content-addressed transcript-кэш —
всё в Supabase. ``service_role`` обходит RLS → стейт пишет ТОЛЬКО сервер; фронт читает свои
строки по RLS (политики `own jobs` и т.п. уже накатаны в проде).

Активен ⇔ ``cloud_enabled()`` (STORAGE_BACKEND=r2 + SUPABASE_URL + SERVICE_ROLE_KEY) — так
локальный dev остаётся на SQLite+диске без облака, а на Modal автоматически идёт в Postgres.
Тот же стиль, что ``app/supa.py`` (биллинг): тонкие httpx-обёртки + PURE-хелперы под тесты.

Схема (накатана в проде, ref qiagetbnsssvbiowuxpp): jobs / job_artifacts / clip_edits /
transcript_cache (см. docs или Supabase Table Editor).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

import httpx

from app import http_client as http

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


def cloud_enabled() -> bool:
    """Писать ли стейт в Supabase (vs локальный SQLite/диск). Гейт = r2 + URL + service_role."""
    backend = os.environ.get("STORAGE_BACKEND", "").strip().lower()
    return bool(
        backend == "r2"
        and os.environ.get("SUPABASE_URL")
        and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    )


def _base() -> str:
    return f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1"


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    return headers


# ─────────────────────────── PURE-хелперы (тестируемые) ───────────────────────────


def first_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """PostgREST всегда отдаёт массив → первая строка или None. PURE."""
    return rows[0] if rows else None


def lock_applied(rows: list[dict[str, Any]]) -> bool:
    """Атомарный optimistic-lock: PATCH ...&version=eq.X с return=representation вернул строку?

    Пустой массив → версия не совпала (никакая строка не подошла под фильтр) → конфликт. PURE.
    """
    return len(rows) == 1


_COLUMN_RE = re.compile(r"[a-z_][a-z0-9_]*")


def _column(name: str) -> str:
    """Валидное имя колонки (``[a-z_][a-z0-9_]*``) или ValueError. PURE.

    Имена приходят ИЗ КОДА (не из пользовательского ввода), но они склеиваются в URL-параметры
    PostgREST → валидируем ЯВНО, чтобы ``select``/фильтр нельзя было расширить опечаткой или
    случайной склейкой (``"meta,transcript"`` одной строкой прошло бы как «колонка»).
    """
    if not _COLUMN_RE.fullmatch(name):
        raise ValueError(f"bad column name: {name!r}")
    return name


def select_clause(columns: Sequence[str]) -> str:
    """Колонки → PostgREST ``?select=`` (``("meta",)`` → ``"meta"``). PURE, ЕДИНЫЙ источник.

    ЗАЧЕМ: строка ``job_artifacts`` несёт ВЕСЬ транскрипт (86-мин видео = 2.18 МБ на проводе /
    ~33 МБ пиковой RAM на парсинг; 6-часовое ≈ 113 МБ). Читатель, которому нужен только
    ``meta.fps`` (fast-path ``GET /reframe``!), ОБЯЗАН просить ТОЛЬКО ``meta``.

    Пустой список — это НЕ «все колонки», а баг вызывателя → ValueError (правило №8: явная
    ошибка вместо тихого ``select=*``, который вернул бы нас к полной строке).
    """
    cols = [_column(c.strip()) for c in columns]
    if not cols:
        raise ValueError("select_clause: empty column list (did you mean ARTIFACT_COLUMNS?)")
    return ",".join(cols)


# Суффикс follow-варианта плана в ключе jsonb-словаря job_artifacts.reframe_regions.
_FOLLOW_KEY_SUFFIX = "#follow"


def reframe_regions_key(clip_id: str, *, lock_per_shot: bool = True) -> str:
    """Ключ плана клипа в jsonb-словаре ``job_artifacts.reframe_regions``. PURE, ЕДИНЫЙ источник.

    Locked и follow — ДВА РАЗНЫХ CV-плана одного клипа (статичный кроп на шот vs следящий пан).
    Раньше персистился только locked, поэтому follow-клип НИКОГДА не попадал в fast-path → полный
    CV на КАЖДОЕ открытие редактора (прод: все OOM-клипы были follow). Теперь оба живут в ТОЙ ЖЕ
    колонке под РАЗНЫМИ ключами: RPC ``merge_reframe_regions`` пишет ``jsonb_build_object(p_clip_id,
    …)`` — ключ = что передали, миграция не нужна.

    ⚠️ Locked-ключ = ГОЛЫЙ ``clip_id`` (байт-в-байт как раньше) — уже запечённые прод-данные и
    batch-персист (``run.py``) обязаны продолжать читаться. Follow — ``<clip_id>#follow``.
    """
    return clip_id if lock_per_shot else f"{clip_id}{_FOLLOW_KEY_SUFFIX}"


# ─────────────────────────── jobs ───────────────────────────


def insert_job(
    job_id: str,
    source_type: str,
    source_ref: str,
    *,
    user_id: str | None = None,
    remove_pauses: bool = False,
) -> None:
    r = http.post(
        f"{_base()}/jobs",
        headers=_headers({"Prefer": "return=minimal"}),
        json={
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "source_type": source_type,
            "source_ref": source_ref,
            "user_id": user_id,
            "cancellable": True,  # Stop-кнопка: новый джоб стартует во FREE-фазе
            "remove_pauses": remove_pauses,  # global «убирать паузы» (opt-in с загрузки)
        },
        timeout=_TIMEOUT,
    )
    if r.status_code >= 400:  # НЕ молча (правило №8): логируем тело ответа PostgREST
        import logging

        logging.getLogger("app.cloud_state").error(
            "insert_job POST /jobs -> %s: %s", r.status_code, r.text[:1000]
        )
    r.raise_for_status()


def update_status(job_id: str, status: str, progress: int) -> None:
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"status": status, "stage": status, "progress": progress},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def set_done(
    job_id: str,
    clips: list[dict[str, Any]],
    cost_usd: float,
    duration_sec: float,
    elapsed_sec: float,
) -> None:
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={
            "status": "done",
            "stage": "done",
            "progress": 100,
            "clips": clips,
            "cost_usd": cost_usd,
            "duration_sec": duration_sec,
            "elapsed_sec": elapsed_sec,
        },
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def set_clips_pending(
    job_id: str, clips: list[dict[str, Any]], progress: int = 80, status: str = "rendering"
) -> None:
    """Записать ВСЕ клипы (метаданные, пустой video_url) в строку джоба СРАЗУ после Select.

    Та же колонка ``clips`` (jsonb), что пишет ``set_done``; каждый клип несёт ``video_url=""``
    (pending). Параллельные фан-аут-контейнеры затем проставляют свой video_url через
    ``set_clip_ready`` (атомарный jsonb_set). Так GET /jobs отдаёт клипы по мере готовности.

    ``status`` управляет тем, КОГДА это зовут: на границе render → "rendering" (как раньше);
    СРАЗУ после select → "selecting" (клипы видны на ~60%, статус честный, грид встаёт по
    наличию клипов, не по статусу). Дефолт "rendering" = старое поведение.
    """
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"status": status, "stage": status, "progress": progress, "clips": clips},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def set_progress_detail(
    job_id: str,
    *,
    source_minutes: float | None = None,
    transcript_words: int | None = None,
    moments_found: int | None = None,
) -> None:
    """Проставить live-narration счётчики (0011), ТОЛЬКО переданные ключи. Косметика → НЕ должно
    валить рендер: при сбое логируем и продолжаем (явный лог, НЕ тихий проглот — правило №8)."""
    fields = {
        k: v
        for k, v in (
            ("source_minutes", source_minutes),
            ("transcript_words", transcript_words),
            ("moments_found", moments_found),
        )
        if v is not None
    }
    if not fields:
        return
    try:
        r = http.patch(
            f"{_base()}/jobs",
            params={"id": f"eq.{job_id}"},
            headers=_headers({"Prefer": "return=minimal"}),
            json=fields,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — счётчики косметические, рендер важнее их записи
        print(f"[set_progress_detail] WARN job={job_id}: {e}")


def patch_clip_analysis(job_id: str, clip_id: str, fields: dict[str, Any]) -> None:
    """Update analysis fields for one clip (read-modify-write; safe post-pipeline)."""
    r = http.get(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}", "select": "clips"},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    row = first_row(r.json())
    if row is None:
        return
    clips = list(row.get("clips") or [])
    for clip in clips:
        if clip.get("id") == clip_id:
            clip.update(fields)
            break
    p = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"clips": clips},
        timeout=_TIMEOUT,
    )
    p.raise_for_status()


def set_clip_ready(job_id: str, idx: int, url: str) -> None:
    """Атомарно проставить ``clips[idx].video_url`` ОДНОГО клипа (per-clip контейнер закончил).

    Вызывает Postgres-RPC ``set_clip_video_url`` (migrations/0010): jsonb_set внутри ОДНОГО
    серверного UPDATE атомарен (Postgres сериализует UPDATE'ы строки), поэтому параллельные
    контейнеры, пишущие РАЗНЫЕ индексы, не теряют запись друг друга — в отличие от PATCH целой
    колонки (read-modify-write → гонка). ``idx`` 0-based (позиция в массиве). RPC возвращает
    число обновлённых строк; 0 → джоба/индекса нет (логируем, не глотаем — правило №8).
    """
    r = http.post(
        f"{_base()}/rpc/set_clip_video_url",
        headers=_headers(),
        json={"p_job_id": job_id, "p_idx": idx, "p_url": url},
        timeout=_TIMEOUT,
        idempotent=True,  # jsonb_set ФИКСИРОВАННОГО url → повтор даёт тот же результат
    )
    r.raise_for_status()
    updated = r.json()
    if not updated:
        print(f"[set_clip_ready] WARN: no row updated for job={job_id} idx={idx}")


def append_pending_clip(job_id: str, clip: dict[str, Any]) -> int:
    """Append ONE pending clip (empty ``video_url`` + metadata) to ``jobs.clips`` (on-demand
    extraction). Returns the NEW clip's 1-based index — the same addressing ``set_clip_ready``
    expects (it writes ``clips[clip_index-1]``), so the follow-up render write lands on it.

    Best-effort read-modify-write of the WHOLE ``clips`` column (there is no atomic append RPC).
    # TODO(atomic): a Postgres append RPC + migration would remove the rare concurrent-append race
    """
    from app.errors import JobError

    r = http.get(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}", "select": "clips"},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    row = first_row(r.json())
    if row is None:
        raise JobError("persist", f"append_pending_clip: no job row {job_id}")
    clips = list(row.get("clips") or [])
    clips.append(clip)
    p = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"clips": clips},
        timeout=_TIMEOUT,
    )
    p.raise_for_status()
    return len(clips)


def set_failed(job_id: str, error: str) -> None:
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"status": "failed", "stage": "failed", "error": error},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def set_function_call_id(job_id: str, fc_id: str) -> None:
    """Сохранить id Modal-``FunctionCall`` (для отмены джоба). Только эта колонка."""
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"function_call_id": fc_id},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def set_cancellable(job_id: str, value: bool) -> None:
    """Переключить флаг отмены (воркер гасит в False при входе в платную стадию)."""
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"cancellable": value},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def set_cancelled(job_id: str) -> None:
    """Пометить джоб отменённым (Stop). Guard ``status!=done & status!=failed`` — не
    перетираем завершённый джоб (гонка отмены с финишем пайплайна)."""
    # Дублирующийся ключ ``status`` (neq.done И neq.failed) — httpx сериализует список
    # значений в повторённые query-параметры, что PostgREST трактует как AND по колонке.
    #
    # УСЛОВНЫЙ PATCH, но ретрай тут БЕЗОПАСЕН (проверено, НЕ путать с CAS-ами, помеченными
    # idempotent=False — см. шапку http_client): (1) фильтр не self-invalidating — записанный
    # 'cancelled' по-прежнему УДОВЛЕТВОРЯЕТ guard'у (neq.done И neq.failed), т.е. повтор матчит
    # ту же строку и переписывает ТЕ ЖЕ абсолютные значения; (2) число затронутых строк НИКУДА
    # не возвращается (return=minimal, функция -> None), так что «0 строк» на повторе не может
    # превратиться в ложный ответ юзеру. ⚠️ Поменяете guard на eq-фильтр (напр. status=eq.running)
    # или начнёте возвращать факт применения — СРАЗУ ставьте idempotent=False.
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}", "status": ["neq.done", "neq.failed"]},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"status": "cancelled", "stage": "cancelled", "cancellable": False},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def get_job_row(job_id: str) -> dict[str, Any] | None:
    r = http.get(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}", "select": "*"},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return first_row(r.json())


def list_jobs_by_user(user_id: str | None, limit: int = 50) -> list[dict[str, Any]]:
    """Джобы пользователя (newest-first) для дашборд-списка. Тянем только нужные колонки
    (лёгкий poll): id/status/source_type/source_ref/created_at + clips (для nclips). ``user_id
    is None`` (dev/auth off) → самые свежие джобы без фильтра владельца."""
    params: dict[str, Any] = {
        "select": "id,status,source_type,source_ref,created_at,clips",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if user_id is not None:
        params["user_id"] = f"eq.{user_id}"
    r = http.get(f"{_base()}/jobs", params=params, headers=_headers(), timeout=_TIMEOUT)
    r.raise_for_status()
    return list(r.json())


# ─────────────────────────── per-user source dedup ───────────────────────────


def find_duplicate_source(user_id: str, fingerprint: str) -> str | None:
    """id самой свежей прошлой джобы ЭТОГО юзера с тем же отпечатком source (или None).

    Per-user (фильтр ``user_id=eq.`` — IDOR-оракул закрыт), сортировка сервером по свежести,
    выбор чистой ``dedup.pick_duplicate_candidate`` (тай-брейк по id → детерминизм)."""
    from app import dedup

    r = http.get(
        f"{_base()}/jobs",
        params={
            "user_id": f"eq.{user_id}",
            "source_fingerprint": f"eq.{fingerprint}",
            "select": "id,created_at",
            "order": "created_at.desc",
            "limit": "20",
        },
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return dedup.pick_duplicate_candidate(list(r.json()))


def set_source_fingerprint(job_id: str, fingerprint: str) -> None:
    """Записать отпечаток source джобы (для будущего per-user дедупа). Только эта колонка."""
    r = http.patch(
        f"{_base()}/jobs",
        params={"id": f"eq.{job_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"source_fingerprint": fingerprint},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


# ─────────────────────────── job_artifacts (meta/segments/transcript) ───────────────────────────

# Core-артефакты пайплайна: три jsonb-колонки ОДНОЙ строки job_artifacts. Дефолт get_job_artifacts
# (обратная совместимость), но НЕ дефолт для читателей — те сужают до своей колонки.
ARTIFACT_COLUMNS: tuple[str, ...] = ("meta", "segments", "transcript")


def put_job_artifacts(
    job_id: str, meta: dict[str, Any], segments: list[Any], transcript: dict[str, Any]
) -> None:
    r = http.post(
        f"{_base()}/job_artifacts",
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json={"job_id": job_id, "meta": meta, "segments": segments, "transcript": transcript},
        timeout=_TIMEOUT,
        idempotent=True,  # merge-duplicates = upsert по PK job_id → повтор безопасен
    )
    r.raise_for_status()


def get_job_artifacts(
    job_id: str, columns: Sequence[str] = ARTIFACT_COLUMNS
) -> dict[str, Any] | None:
    """Строка ``job_artifacts`` — ТОЛЬКО запрошенные ``columns``. None, если строки нет.

    ⚠️ ``columns`` — не косметика, а бюджет памяти/трафика: дефолт (все три) тянет транскрипт
    (мегабайты, см. ``select_clause``). Вызыватель, которому нужен один артефакт, ОБЯЗАН сузить:
    ``get_job_artifacts(job_id, columns=("meta",))``. Один запрос на колонку дешевле, чем один
    запрос на всё: 33 МБ пиковой RAM ради ``meta.fps`` — это не экономия round-trip'а.
    """
    r = http.get(
        f"{_base()}/job_artifacts",
        params={"job_id": f"eq.{job_id}", "select": select_clause(columns)},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return first_row(r.json())


def put_job_artifact(job_id: str, key: str, value: Any) -> None:
    """Upsert ОДНОЙ jsonb-колонки строки job_artifacts (напр. video_map), не трогая остальные.

    merge-duplicates по PK job_id: строка обычно уже создана put_job_artifacts (run.py до
    фан-аута), поэтому это UPDATE одной колонки. Cross-container: video_map_job (отдельный
    Modal-контейнер) пишет сюда, а /video-map (web-контейнер) читает get_job_artifact.
    Требует колонку job_artifacts.<key> (jsonb) в Postgres.
    """
    r = http.post(
        f"{_base()}/job_artifacts",
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json={"job_id": job_id, key: value},
        timeout=_TIMEOUT,
        idempotent=True,  # merge-duplicates = upsert по PK job_id → повтор безопасен
    )
    r.raise_for_status()


def get_job_artifact(job_id: str, key: str) -> Any:
    """Прочитать ОДНУ jsonb-колонку job_artifacts[key]; None если строки/значения нет.

    Тонкая обёртка над ``get_job_artifacts(columns=(key,))`` — ОДИН путь чтения строки артефактов
    (правило «дублируется логика → выноси в ОДНУ функцию»), а не второй свой GET.
    """
    row = get_job_artifacts(job_id, columns=(key,))
    return row.get(key) if row else None


def has_job_artifact(job_id: str, key: str) -> bool:
    """Есть ли в ``job_artifacts[key]`` НЕ-null значение — БЕЗ выкачивания самого блоба.

    Серверный фильтр ``<key>=not.is.null`` + ``select=job_id``: по проводу едет ОДИН id вместо
    мегабайтного транскрипта. Нужен 404-гейтам ``/chapters`` и ``/video-map``, которым важно
    ТОЛЬКО «транскрипт существует?» — они парсили его ЦЕЛИКОМ ради этого булева.
    """
    r = http.get(
        f"{_base()}/job_artifacts",
        params={"job_id": f"eq.{job_id}", _column(key): "not.is.null", "select": "job_id"},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return first_row(r.json()) is not None


def merge_reframe_regions(
    job_id: str, clip_id: str, value: dict[str, Any] | None, *, lock_per_shot: bool = True
) -> int:
    """Атомарно вписать reframe-регионы ОДНОГО клипа в job_artifacts.reframe_regions (домен 1).

    ``value=None`` → в ключ варианта пишется jsonb ``null`` (``jsonb_build_object`` превращает SQL
    NULL в JSON null) → ``get_reframe_regions`` вернёт None = «записи нет». Так СНИМАЕТСЯ маркер
    провала по Retry (RPC умеет только merge; отдельного delete-пути не заводим — DRY).

    RPC ``merge_reframe_regions`` (migrations/0013): server-side ``||``-merge внутри одного UPDATE
    сериализуется блокировкой строки → параллельные фан-аут-контейнеры, пишущие РАЗНЫЕ clip_id,
    не теряют запись друг друга (как set_clip_video_url). Возвращает число обновлённых строк
    (0 = строки артефактов нет → вызыватель логирует, не глотает, правило №8).

    ``lock_per_shot`` выбирает КЛЮЧ записи (``reframe_regions_key``): locked → ``clip_id``
    (канонический batch-entry), follow → ``clip_id#follow``. Разные ключи ⇒ ``||``-merge их НЕ
    перетирает — оба плана живут в одной колонке."""
    r = http.post(
        f"{_base()}/rpc/merge_reframe_regions",
        headers=_headers(),
        json={
            "p_job_id": job_id,
            "p_clip_id": reframe_regions_key(clip_id, lock_per_shot=lock_per_shot),
            "p_value": value,
        },
        timeout=_TIMEOUT,
        idempotent=True,  # server-side ``||``-merge ТОГО ЖЕ значения → повтор = no-op
    )
    r.raise_for_status()
    return int(r.json())


def get_reframe_regions(
    job_id: str, clip_id: str, *, lock_per_shot: bool = True
) -> dict[str, Any] | None:
    """Персистнутый reframe-payload ОДНОГО клипа (или None). Читает job_artifacts.reframe_regions
    (словарь {key: payload}) и достаёт запись ВАРИАНТА (locked → clip_id, follow → clip_id#follow —
    ``reframe_regions_key``). Locked-план НИКОГДА не отдаётся follow-читателю и наоборот: это
    РАЗНЫЕ геометрии (статика vs пан), подмешивание = «то залипло, то поехало» на одном клипе.

    Читает через ``get_job_artifact`` (тот же ЕДИНЫЙ однo-колоночный путь) — запрос байт-в-байт
    тот же (``select=reframe_regions``), транскрипт не задевается."""
    blob = get_job_artifact(job_id, "reframe_regions")
    if not isinstance(blob, dict):
        return None
    entry = blob.get(reframe_regions_key(clip_id, lock_per_shot=lock_per_shot))
    return entry if isinstance(entry, dict) else None


# ─────────────────────────── transcript_cache (бережёт Deepgram) ───────────────────────────


def get_cached_transcript(audio_sha: str, provider: str, model: str) -> dict[str, Any] | None:
    r = http.get(
        f"{_base()}/transcript_cache",
        params={
            "audio_sha": f"eq.{audio_sha}",
            "provider": f"eq.{provider}",
            "model": f"eq.{model}",
            "select": "transcript",
        },
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    row = first_row(r.json())
    return row["transcript"] if row else None


def put_cached_transcript(
    audio_sha: str, provider: str, model: str, transcript: dict[str, Any]
) -> None:
    r = http.post(
        f"{_base()}/transcript_cache",
        headers=_headers({"Prefer": "resolution=ignore-duplicates,return=minimal"}),
        json={
            "audio_sha": audio_sha,
            "provider": provider,
            "model": model,
            "transcript": transcript,
        },
        timeout=_TIMEOUT,
        idempotent=True,  # ignore-duplicates = on-conflict-do-nothing → повтор безопасен
    )
    r.raise_for_status()


# ─────────────────────────── clip_edits (атомарный optimistic-lock) ───────────────────────────


def get_clip_edit_row(job_id: str, clip_id: str) -> dict[str, Any] | None:
    r = http.get(
        f"{_base()}/clip_edits",
        params={"job_id": f"eq.{job_id}", "clip_id": f"eq.{clip_id}", "select": "*"},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return first_row(r.json())


def insert_clip_edit(job_id: str, clip_id: str, edit: dict[str, Any], version: int) -> None:
    """Первичная вставка edit-state. on conflict do nothing (ignore-duplicates)."""
    r = http.post(
        f"{_base()}/clip_edits",
        headers=_headers({"Prefer": "resolution=ignore-duplicates,return=minimal"}),
        json={"job_id": job_id, "clip_id": clip_id, "version": version, "edit": edit},
        timeout=_TIMEOUT,
        idempotent=True,  # ignore-duplicates = on-conflict-do-nothing → повтор безопасен
    )
    r.raise_for_status()


def update_clip_edit_if_version(
    job_id: str, clip_id: str, edit: dict[str, Any], *, expected_version: int, new_version: int
) -> bool:
    """Атомарный optimistic-lock: PATCH ...&version=eq.expected. True если строка обновилась.

    ``idempotent=False`` — CAS: ФИЛЬТР (``version=eq.expected``) читает ту самую колонку, которую
    этот PATCH пишет (``version=new_version``), т.е. успешная попытка САМА его обесценивает.
    Повтор после ПОТЕРЯННОГО ответа (ReadTimeout/5xx) матчил бы 0 строк → ``lock_applied([])`` =
    False → ``store.save_edit`` отдал бы 409 «данные изменились, перезагрузите» на правку,
    которая НА САМОМ ДЕЛЕ сохранилась. Не ретраим — ошибка транспорта всплывает честно.
    """
    r = http.patch(
        f"{_base()}/clip_edits",
        params={
            "job_id": f"eq.{job_id}",
            "clip_id": f"eq.{clip_id}",
            "version": f"eq.{expected_version}",
        },
        headers=_headers({"Prefer": "return=representation"}),
        json={"edit": edit, "version": new_version},
        timeout=_TIMEOUT,
        idempotent=False,
    )
    r.raise_for_status()
    return lock_applied(r.json())


def set_render_status(
    job_id: str, clip_id: str, status: str, url: str | None, error: str | None
) -> None:
    r = http.patch(
        f"{_base()}/clip_edits",
        params={"job_id": f"eq.{job_id}", "clip_id": f"eq.{clip_id}"},
        headers=_headers({"Prefer": "return=minimal"}),
        json={"render_status": status, "render_url": url, "render_error": error},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
