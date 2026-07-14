"""app.http_client: ОДИН пулированный httpx-клиент + ограниченный retry/backoff.

Прод-инцидент (2026-07-14): каждый Supabase/PostgREST-вызов шёл через convenience-API
(``httpx.get`` / ``httpx.post`` / ``httpx.patch``) → НОВЫЙ Client + новый TCP + ПОЛНЫЙ TLS-
handshake на КАЖДЫЙ вызов. IDOR-гейт даёт 4-5 таких вызовов на запрос; при
containerConcurrency=80 на 2 vCPU это до ~400 одновременных TLS-handshake → connect-бюджет
(5s) взрывается → ``ConnectTimeout``/``ReadTimeout`` → HTTP 500.

Эти тесты фиксируют контракт: пул переиспользуется, retry ТОЛЬКО на транзиентных сбоях,
4xx НИКОГДА не ретраится, а исчерпанный retry ПОДНИМАЕТ ошибку (никаких тихих фолбэков).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app import http_client

# ─────────────────────────── пул: один клиент на процесс ───────────────────────────


class TestPooledClient:
    def test_client_is_a_module_level_singleton(self) -> None:
        # Два вызова → ТОТ ЖЕ объект (иначе нет переиспользования соединений = корень инцидента).
        assert http_client.client() is http_client.client()

    def test_client_configures_a_keepalive_pool(self) -> None:
        limits = http_client.pool_limits()
        assert limits.max_connections is not None and limits.max_connections >= 20
        assert limits.max_keepalive_connections is not None
        assert limits.max_keepalive_connections >= 10  # keep-alive → handshake не на каждый вызов
        # …и ЖИВОЙ клиент реально построен с этим пулом (httpcore.ConnectionPool под транспортом).
        pool = http_client.client()._transport._pool  # type: ignore[attr-defined]
        assert pool._max_connections == limits.max_connections
        assert pool._max_keepalive_connections == limits.max_keepalive_connections

    def test_per_call_timeout_overrides_client_default(self, monkeypatch: Any) -> None:
        # Бюджеты таймаутов разные у supa (5/10/10/5) и cloud_state (10/30/30/10) — они
        # ОБЯЗАНЫ доезжать до транспорта через per-call timeout=, а не подменяться дефолтом.
        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=[])

        _use_mock(monkeypatch, handler)
        supa_budget = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
        http_client.get("https://x/rest/v1/t", timeout=supa_budget)
        assert seen and seen[0]["connect"] == 5.0 and seen[0]["read"] == 10.0


# ─────────────────────────── PURE: политика retry ───────────────────────────


class TestBackoffDelay:
    def test_full_jitter_never_exceeds_window(self) -> None:
        for attempt in range(5):
            d = http_client.backoff_delay(attempt, base=0.25, cap=2.0, rand=lambda: 1.0)
            assert 0.0 <= d <= 2.0

    def test_grows_exponentially_until_cap(self) -> None:
        one = (lambda: 1.0,)  # rand=1.0 → верхняя граница окна (детерминизм)
        d0 = http_client.backoff_delay(0, base=0.25, cap=2.0, rand=one[0])
        d1 = http_client.backoff_delay(1, base=0.25, cap=2.0, rand=one[0])
        d2 = http_client.backoff_delay(2, base=0.25, cap=2.0, rand=one[0])
        d9 = http_client.backoff_delay(9, base=0.25, cap=2.0, rand=one[0])
        assert (d0, d1, d2) == (0.25, 0.5, 1.0)
        assert d9 == 2.0  # cap

    def test_jitter_is_applied(self) -> None:
        assert http_client.backoff_delay(3, base=0.25, cap=2.0, rand=lambda: 0.0) == 0.0
        assert http_client.backoff_delay(3, base=0.25, cap=2.0, rand=lambda: 0.5) == 1.0


class TestShouldRetryException:
    def test_connect_phase_always_retried(self) -> None:
        # Соединение не встало → запрос ТОЧНО не доехал → повтор безопасен даже для POST.
        for exc in (httpx.ConnectTimeout("t"), httpx.ConnectError("c")):
            assert http_client.should_retry_exception(exc, "POST", idempotent=False) is True
            assert http_client.should_retry_exception(exc, "GET", idempotent=True) is True

    def test_reply_lost_retried_only_when_idempotent(self) -> None:
        # ReadTimeout/RemoteProtocolError: запрос МОГ примениться на сервере → повторяем только
        # идемпотентное (GET/PATCH), иначе POST мог бы вставить строку ДВАЖДЫ.
        for exc in (httpx.ReadTimeout("t"), httpx.RemoteProtocolError("p")):
            assert http_client.should_retry_exception(exc, "PATCH", idempotent=True) is True
            assert http_client.should_retry_exception(exc, "POST", idempotent=False) is False

    def test_non_transient_not_retried(self) -> None:
        assert (
            http_client.should_retry_exception(ValueError("boom"), "GET", idempotent=True) is False
        )


class TestShouldRetryStatus:
    def test_4xx_never_retried(self) -> None:
        for status in (400, 401, 403, 404, 409, 422):
            assert http_client.should_retry_status(status, "GET", idempotent=True) is False

    def test_429_retried_even_for_post(self) -> None:
        # Rate-limit = сервер ЯВНО не обработал запрос → повтор не даст дубля.
        assert http_client.should_retry_status(429, "POST", idempotent=False) is True

    def test_5xx_retried_only_when_idempotent(self) -> None:
        for status in (500, 502, 503, 504):
            assert http_client.should_retry_status(status, "GET", idempotent=True) is True
            assert http_client.should_retry_status(status, "POST", idempotent=False) is False

    def test_2xx_not_retried(self) -> None:
        assert http_client.should_retry_status(200, "GET", idempotent=True) is False
        assert http_client.should_retry_status(201, "POST", idempotent=False) is False


# ─────────────────────────── поведение retry (через MockTransport) ───────────────────────────


def _use_mock(monkeypatch: Any, handler: Any) -> None:
    """Подменить пулированный клиент на httpx.Client с MockTransport + убрать реальный sleep."""
    c = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "client", lambda: c)
    monkeypatch.setattr(http_client, "_sleep", lambda _s: None)


class TestRetryBehaviour:
    def test_get_retries_connect_timeout_then_succeeds(self, monkeypatch: Any) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ConnectTimeout("SSL handshake timed out")
            return httpx.Response(200, json=[{"ok": True}])

        _use_mock(monkeypatch, handler)
        r = http_client.get("https://ref.supabase.co/rest/v1/profiles")
        assert r.status_code == 200 and r.json() == [{"ok": True}]
        assert len(calls) == 3  # 1 попытка + 2 ретрая

    def test_raises_after_retries_exhausted(self, monkeypatch: Any) -> None:
        # ЖЕЛЕЗНОЕ ПРАВИЛО: никаких тихих фолбэков — исчерпали ретраи → ошибка ВСПЛЫВАЕТ.
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ConnectTimeout("SSL handshake timed out")

        _use_mock(monkeypatch, handler)
        with pytest.raises(httpx.ConnectTimeout):
            http_client.get("https://ref.supabase.co/rest/v1/profiles")
        assert len(calls) == http_client.MAX_ATTEMPTS  # ограничено, не бесконечно

    def test_retries_503_then_succeeds(self, monkeypatch: Any) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=[])

        _use_mock(monkeypatch, handler)
        assert http_client.get("https://x/rest/v1/jobs").status_code == 200
        assert len(calls) == 2

    def test_does_not_retry_404(self, monkeypatch: Any) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(404, text="no")

        _use_mock(monkeypatch, handler)
        r = http_client.get("https://x/rest/v1/jobs")
        assert r.status_code == 404 and len(calls) == 1  # 4xx = не транзиент

    def test_exhausted_5xx_is_returned_so_caller_raise_for_status_fires(
        self, monkeypatch: Any
    ) -> None:
        # Возвращаем ОТВЕТ (а не глотаем): raise_for_status() у вызывателя поднимет ошибку —
        # контракт call-site'ов не меняется (insert_job ещё и логирует тело).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        _use_mock(monkeypatch, handler)
        r = http_client.get("https://x/rest/v1/jobs")
        assert r.status_code == 500
        with pytest.raises(httpx.HTTPStatusError):
            r.raise_for_status()

    def test_post_does_not_replay_read_timeout(self, monkeypatch: Any) -> None:
        # Ответ потерян ≠ запрос не применён: повтор POST /usage_events мог бы списать дважды.
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadTimeout("slow")

        _use_mock(monkeypatch, handler)
        with pytest.raises(httpx.ReadTimeout):
            http_client.post("https://x/rest/v1/usage_events", json={"a": 1})
        assert len(calls) == 1  # НЕ ретраим неидемпотентный POST

    def test_post_retries_connect_timeout(self, monkeypatch: Any) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectTimeout("SSL handshake timed out")
            return httpx.Response(201)

        _use_mock(monkeypatch, handler)
        assert http_client.post("https://x/rest/v1/jobs", json={"id": "j"}).status_code == 201
        assert len(calls) == 2  # connect-фаза → запрос не доехал → повтор безопасен

    def test_post_marked_idempotent_replays_read_timeout(self, monkeypatch: Any) -> None:
        # upsert'ы (Prefer: merge-duplicates / ignore-duplicates) и RPC-мерджи безопасно повторять.
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(201)

        _use_mock(monkeypatch, handler)
        r = http_client.post("https://x/rest/v1/job_artifacts", json={}, idempotent=True)
        assert r.status_code == 201 and len(calls) == 2

    def test_patch_retries_read_timeout(self, monkeypatch: Any) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(200, json=[{"id": "u1"}])

        _use_mock(monkeypatch, handler)
        r = http_client.patch("https://x/rest/v1/profiles", json={"plan": "pro"})
        assert r.json() == [{"id": "u1"}] and len(calls) == 2

    def test_backoff_is_slept_between_attempts(self, monkeypatch: Any) -> None:
        slept: list[float] = []
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json=[])

        c = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(http_client, "client", lambda: c)
        monkeypatch.setattr(http_client, "_sleep", slept.append)
        http_client.get("https://x/rest/v1/jobs")
        assert len(slept) == 2  # backoff перед каждым ретраем
        assert all(0.0 <= s <= http_client.BACKOFF_CAP_SEC for s in slept)


# ─────────────────────────── anti-drift: ни одного convenience-вызова ───────────────────────────

_APP = Path(__file__).resolve().parents[2] / "app"


@pytest.mark.parametrize(
    "rel",
    ["cloud_state.py", "supa.py", "agent/runs_store.py", "social/repo.py"],
)
def test_no_top_level_httpx_convenience_calls(rel: str) -> None:
    """Корень инцидента: ``httpx.get(...)`` строит НОВЫЙ клиент (+TLS) на каждый вызов.

    Эти модули обязаны ходить через пулированный ``app.http_client`` — гейт, чтобы регрессия
    не вернулась незаметно новым call-site'ом.
    """
    src = (_APP / rel).read_text(encoding="utf-8")
    for verb in ("get", "post", "patch", "put", "delete", "request"):
        assert f"httpx.{verb}(" not in src, f"{rel}: httpx.{verb}() = новый TLS-handshake на вызов"
