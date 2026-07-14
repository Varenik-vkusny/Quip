"""ОДИН пулированный httpx-клиент на процесс + ограниченный retry/backoff (транспорт Supabase).

ЗАЧЕМ (прод-инцидент 2026-07-14, Cloud Run ``quip-api``: интермиттентные HTTP 500 с
``httpcore.ConnectTimeout: SSL handshake timed out`` / ``ReadTimeout``):
весь исходящий трафик к Supabase/PostgREST шёл через convenience-API httpx
(``httpx.get`` / ``httpx.post`` / ``httpx.patch``). Каждый такой вызов создаёт НОВЫЙ
``httpx.Client`` → новый TCP-коннект → ПОЛНЫЙ TLS-handshake → и тут же выбрасывает его.
Переиспользования соединений НЕТ вообще. Per-request IDOR-гейт даёт 4-5 таких вызовов на
HTTP-запрос; при ``containerConcurrency=80`` на 2 vCPU это до ~400 одновременных TLS-
handshake → connect-бюджет (``supa._TIMEOUT``: connect=5s) не выдерживает → ConnectTimeout → 500.

ЧТО ДЕЛАЕТ ЭТОТ МОДУЛЬ:
1. Держит ОДИН module-level ``httpx.Client`` с keep-alive-пулом → TLS-handshake платится один
   раз на соединение, дальше оно переиспользуется. ``httpx.Client`` потокобезопасен для
   запросов — важно, т.к. FastAPI гоняет sync-хендлеры в threadpool'е.
2. Даёт ограниченный retry с экспоненциальным backoff + full-jitter ТОЛЬКО на ТРАНЗИЕНТНЫХ
   сбоях (ConnectTimeout / ReadTimeout / ConnectError / RemoteProtocolError, а также 5xx/429).
   4xx НЕ ретраится никогда.

БЕЗОПАСНОСТЬ ПОВТОРА (важно — деньги/дубли):
* connect-фаза (``ConnectTimeout`` / ``ConnectError``) — соединение не встало, запрос ТОЧНО не
  доехал до сервера → повтор безопасен для ЛЮБОГО метода, включая POST и CAS-PATCH.
* потерянный ОТВЕТ (``ReadTimeout`` / ``RemoteProtocolError``) и 5xx — сервер МОГ применить
  запрос → повторяем только идемпотентное (GET/PATCH/PUT/DELETE), иначе POST /usage_events мог
  бы записать расход ДВАЖДЫ. Неидемпотентный POST можно явно пометить ``idempotent=True``
  (PostgREST-upsert: ``Prefer: resolution=merge-duplicates|ignore-duplicates``; merge-RPC).
* 429 — сервер ЯВНО не обработал запрос (rate-limit) → повтор безопасен и для POST.

⚠️ PATCH ИДЕМПОТЕНТЕН НЕ ВСЕГДА — УСЛОВНЫЙ (CAS) PATCH ОБЯЗАН НЕСТИ ``idempotent=False``:
Абсолютное тело (значение посчитано в Python, не ``+= n``) делает безвредным САМ ПОВТОР ЗАПИСИ —
но НЕ делает верным ВОЗВРАЩАЕМОЕ ЗНАЧЕНИЕ compare-and-swap'а. У CAS-PATCH'а от изменяемого
состояния зависит ФИЛЬТР (``params``), и первая же УСПЕШНАЯ попытка этот фильтр ОБЕСЦЕНИВАЕТ:

    consume_clip_credit: params={"clip_credits": "gte.1"}, json={"clip_credits": 0}
    попытка №1 доехала (1 → 0), ответ ПОТЕРЯН → replay: gte.1 матчит 0 строк → [] → False.
    Кредит СПИСАН, а вызыватель (main.extract_clip) отдаёт 402 «заплатите» → ДВОЙНАЯ оплата.

Т.е. ретрай, который эта политика должна была предотвратить, пролез через ФИЛЬТР, а не через
тело. Правило: **PATCH ретраится ⇔ его фильтр НЕ читает колонок, которые он же пишет**
(``id=eq.<uuid>`` — читается; ``version=eq.N`` / ``status=eq.running`` / ``clip_credits=gte.1``
— нет). Для второго класса → ``idempotent=False``: потерянный ответ ПОДНИМЕТ ошибку транспорта
(вызыватель отдаст 5xx — честно), вместо тихого ЛОЖНОГО «не применилось». Список таких
call-site'ов и гейт на них: ``tests/unit/test_retry_cas_safety.py``.

НИКАКИХ ТИХИХ ФОЛБЭКОВ (правило репо №8): ретраи ограничены ``MAX_ATTEMPTS``; исчерпали —
исходное исключение ПОДНИМАЕТСЯ. Ответ с плохим статусом возвращается КАК ЕСТЬ (не глотается),
чтобы ``raise_for_status()`` у вызывателя сработал ровно как раньше. Ни одно значение по
умолчанию тут не подставляется.

Тюнится через env (дефолты рассчитаны на Cloud Run 2 vCPU / concurrency 80):
``HTTP_MAX_CONNECTIONS``, ``HTTP_MAX_KEEPALIVE``, ``HTTP_KEEPALIVE_EXPIRY_SEC``,
``HTTP_RETRY_ATTEMPTS``, ``HTTP_RETRY_BASE_SEC``, ``HTTP_RETRY_CAP_SEC``.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger("app.http_client")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


# ── пул ──────────────────────────────────────────────────────────────────────────────────
# concurrency=80 × (последовательные 4-5 вызовов на запрос) → до ~80 одновременных исходящих.
# 100 коннектов = запас; 50 из них держим тёплыми (keep-alive) → handshake ≈ один на коннект.
MAX_CONNECTIONS = _env_int("HTTP_MAX_CONNECTIONS", 100)
MAX_KEEPALIVE = _env_int("HTTP_MAX_KEEPALIVE", 50)
KEEPALIVE_EXPIRY_SEC = _env_float("HTTP_KEEPALIVE_EXPIRY_SEC", 30.0)

# ── retry ────────────────────────────────────────────────────────────────────────────────
MAX_ATTEMPTS = max(1, _env_int("HTTP_RETRY_ATTEMPTS", 3))  # 1 попытка + 2 ретрая
BACKOFF_BASE_SEC = _env_float("HTTP_RETRY_BASE_SEC", 0.25)
BACKOFF_CAP_SEC = _env_float("HTTP_RETRY_CAP_SEC", 2.0)

# Дефолт клиента; каждый call-site всё равно передаёт СВОЙ бюджет (supa 5/10/10/5,
# cloud_state 10/30/30/10) — per-call ``timeout=`` перекрывает этот дефолт.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# Запрос ТОЧНО не доехал (соединение не встало) → повтор безопасен даже для POST.
_CONNECT_PHASE_EXC: tuple[type[Exception], ...] = (httpx.ConnectTimeout, httpx.ConnectError)
# Ответ потерян, но запрос МОГ примениться → повтор только для идемпотентного метода.
_REPLY_LOST_EXC: tuple[type[Exception], ...] = (httpx.ReadTimeout, httpx.RemoteProtocolError)
TRANSIENT_EXC: tuple[type[Exception], ...] = _CONNECT_PHASE_EXC + _REPLY_LOST_EXC

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"})

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def pool_limits() -> httpx.Limits:
    """Лимиты keep-alive-пула (env-тюнинг): соединения ТЁПЛЫЕ → TLS-handshake не на каждый вызов."""
    return httpx.Limits(
        max_connections=MAX_CONNECTIONS,
        max_keepalive_connections=MAX_KEEPALIVE,
        keepalive_expiry=KEEPALIVE_EXPIRY_SEC,
    )


def client() -> httpx.Client:
    """Пулированный process-wide ``httpx.Client`` (ленивая инициализация, потокобезопасно).

    Ровно ОДИН клиент на процесс → TCP+TLS переиспользуются между вызовами (корень инцидента —
    их отсутствие). ``httpx.Client`` безопасен для конкурентных запросов из threadpool'а FastAPI.
    """
    global _client
    if _client is None:
        with _client_lock:  # double-checked: гонка старта в threadpool'е не создаст 2 пула
            if _client is None:
                _client = httpx.Client(limits=pool_limits(), timeout=DEFAULT_TIMEOUT)
    return _client


def close() -> None:
    """Закрыть пул (shutdown / тесты). Следующий ``client()`` создаст новый."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


# ─────────────────────────── PURE: политика повторов (тестируется) ───────────────────────────


def backoff_delay(
    attempt: int,
    *,
    base: float = BACKOFF_BASE_SEC,
    cap: float = BACKOFF_CAP_SEC,
    rand: Callable[[], float] = random.random,
) -> float:
    """Задержка перед ретраем ``attempt`` (0-based): full-jitter ``U(0, min(cap, base·2^attempt))``.

    Full jitter (AWS-рецепт) размазывает повторные попытки во времени — иначе 80 конкурентных
    запросов, упавших на одном и том же блипе, синхронно долбят сервер одной волной. PURE.
    """
    window: float = min(cap, base * (2.0 ** max(0, attempt)))
    return window * rand()


def should_retry_exception(exc: Exception, method: str, *, idempotent: bool) -> bool:
    """Повторять ли транзиентное исключение транспорта? PURE (см. «безопасность повтора» выше)."""
    if isinstance(exc, _CONNECT_PHASE_EXC):
        return True  # соединение не встало → запрос не применился → повтор безопасен всегда
    if isinstance(exc, _REPLY_LOST_EXC):
        return idempotent  # ответ потерян → сервер МОГ применить → только идемпотентное
    return False  # нетранзиентное (напр. баг в коде) — не маскируем ретраями


def should_retry_status(status: int, method: str, *, idempotent: bool) -> bool:
    """Повторять ли ответ с таким HTTP-статусом? 4xx — НИКОГДА (кроме 429). PURE."""
    if status not in RETRY_STATUS:
        return False
    if status == 429:
        return True  # rate-limit: сервер ЯВНО не обработал запрос → дубля не будет
    return idempotent  # 5xx: мог обработать частично → повтор только идемпотентного


def _is_idempotent(method: str) -> bool:
    return method.upper() in IDEMPOTENT_METHODS


def _sleep(seconds: float) -> None:
    """Seam для тестов (чтобы ретраи не спали по-настоящему)."""
    time.sleep(seconds)


# ─────────────────────────── I/O: обёртки поверх пула ───────────────────────────


def request(
    method: str, url: str, *, idempotent: bool | None = None, **kwargs: Any
) -> httpx.Response:
    """Выполнить запрос через ПУЛИРОВАННЫЙ клиент с ограниченным retry/backoff.

    ``idempotent`` — ЯВНОЕ переопределение политики повтора (в обе стороны); ``None`` → выводится
    из метода (GET/PATCH/PUT/DELETE — да; POST — нет):
    * ``True`` для POST'ов, которые на самом деле upsert'ы (``Prefer:
      resolution=merge-duplicates|ignore-duplicates``) или merge-RPC — их повтор не даёт дубля;
    * ``False`` (opt-OUT) для УСЛОВНОГО (CAS) PATCH'а, чей ФИЛЬТР зависит от изменяемого
      состояния — повтор после потерянного ответа вернул бы ЛОЖНОЕ «0 строк» (см. шапку модуля).

    Влияет ТОЛЬКО на потерянный ответ (ReadTimeout/RemoteProtocolError) и 5xx. Connect-фаза и 429
    ретраятся всегда: запрос ТОЧНО не применился, состояние сервера не менялось → ложного
    «не применилось» быть не может.

    Возвращает ``httpx.Response`` (в т.ч. с плохим статусом, если ретраи исчерпаны) — вызыватель
    как и раньше зовёт ``raise_for_status()``. Транзиентное исключение после всех попыток
    ПОДНИМАЕТСЯ (никаких тихих фолбэков).
    """
    m = method.upper()
    safe = _is_idempotent(m) if idempotent is None else idempotent
    for attempt in range(MAX_ATTEMPTS):
        last = attempt == MAX_ATTEMPTS - 1
        try:
            resp = client().request(m, url, **kwargs)
        except TRANSIENT_EXC as exc:
            if last or not should_retry_exception(exc, m, idempotent=safe):
                raise  # исчерпали / небезопасно повторять → ошибка всплывает наверх
            delay = backoff_delay(attempt)
            log.warning(
                "transient %s on %s %s (attempt %d/%d) → retry in %.2fs",
                type(exc).__name__,
                m,
                url,
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
            )
            _sleep(delay)
            continue
        if not last and should_retry_status(resp.status_code, m, idempotent=safe):
            delay = backoff_delay(attempt)
            log.warning(
                "transient HTTP %d on %s %s (attempt %d/%d) → retry in %.2fs",
                resp.status_code,
                m,
                url,
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
            )
            _sleep(delay)
            continue
        return resp
    raise RuntimeError("unreachable: retry loop exited without a response")  # pragma: no cover


def get(url: str, **kwargs: Any) -> httpx.Response:
    """GET через пул (идемпотентен → ретраится на всех транзиентных сбоях)."""
    return request("GET", url, **kwargs)


def post(url: str, *, idempotent: bool = False, **kwargs: Any) -> httpx.Response:
    """POST через пул. По умолчанию НЕ повторяется при потерянном ответе (может дать дубль);
    ``idempotent=True`` — для PostgREST-upsert'ов и merge-RPC (повтор безопасен)."""
    return request("POST", url, idempotent=idempotent, **kwargs)


def patch(url: str, *, idempotent: bool | None = None, **kwargs: Any) -> httpx.Response:
    """PATCH через пул. По умолчанию ПОВТОРЯЕТСЯ: PostgREST-PATCH пишет АБСОЛЮТНЫЕ значения
    (``add_payg_credits``/``add_clip_credits`` считают новый баланс в Python и PATCH'ат его
    целиком, не ``+= n``) → сама ПЕРЕЗАПИСЬ при повторе безвредна, дважды не начислит.

    ⚠️ ``idempotent=False`` ОБЯЗАТЕЛЕН для УСЛОВНОГО (CAS) PATCH'а — где ФИЛЬТР читает колонку,
    которую этот же PATCH пишет (``clip_credits=gte.1`` / ``version=eq.N`` / ``status=eq.running``).
    Абсолютное тело делает безвредным ПОВТОР ЗАПИСИ, но НЕ делает верным ВОЗВРАЩАЕМОЕ ЗНАЧЕНИЕ:
    успешная-но-потерянная попытка №1 обесценивает фильтр → replay матчит 0 строк → вызыватель
    читает это как «не применилось» и врёт юзеру (402 «оплатите ещё раз» / 409 «перезагрузите»).
    Подробности и список call-site'ов — в шапке модуля и ``tests/unit/test_retry_cas_safety.py``.
    """
    return request("PATCH", url, idempotent=idempotent, **kwargs)


def put(url: str, *, idempotent: bool | None = None, **kwargs: Any) -> httpx.Response:
    """PUT через пул (по умолчанию идемпотентен; ``idempotent=False`` — opt-out, см. ``patch``)."""
    return request("PUT", url, idempotent=idempotent, **kwargs)


def delete(url: str, *, idempotent: bool | None = None, **kwargs: Any) -> httpx.Response:
    """DELETE через пул (идемпотентен; ``idempotent=False`` — opt-out, как в ``patch``)."""
    return request("DELETE", url, idempotent=idempotent, **kwargs)
