"""Регресс-гейт: УСЛОВНЫЙ (CAS) PATCH ПОВТОРЯТЬ НЕЛЬЗЯ — replay даёт ЛОЖНОЕ «не применилось».

Баг (ревью 2026-07-14, до релиза пула ``app.http_client``): PATCH стоит в
``IDEMPOTENT_METHODS`` и повторяется на ПОТЕРЯННОМ ОТВЕТЕ (ReadTimeout / RemoteProtocolError /
5xx). Обоснование в http_client было: «тело PATCH'а АБСОЛЮТНОЕ (посчитано в Python, не ``+= n``)
→ replay = no-op». Для ТЕЛА это верно. Но НЕ для CAS-PATCH'а (compare-and-swap), где от
изменяемого состояния зависит ФИЛЬТР:

    supa.consume_clip_credit:            params={"clip_credits": "gte.1"}   ← self-invalidating
    cloud_state.update_clip_edit_if_version: params={"version": "eq.N"}     ← optimistic lock
    agent.runs_store.set_cancelled:      params={"status": "eq.running"}    ← guard

Попытка №1 ДОЕЗЖАЕТ до сервера (баланс 1 → 0), ответ ТЕРЯЕТСЯ. Клиент повторяет. Попытка №2
фильтруется по УЖЕ ИЗМЕНЁННОМУ состоянию (0) → матчит 0 строк → ``return=representation``
отдаёт ``[]`` → функция возвращает False. Кредит СПИСАН, а вызыватель (``main.extract_clip``)
превращает False в HTTP 402 «payment_required» → юзер платит ВТОРОЙ раз и не получает клип.
Ретрай, который policy обязана была не допустить, пролез через ФИЛЬТР, а не через тело.

Правило (закреплено тестами ниже): PATCH ретраится ⇔ его фильтр НЕ читает колонки, которые он
же пишет. CAS-PATCH помечается ``idempotent=False`` → потерянный ответ ПОДНИМАЕТ ошибку
транспорта (вызыватель отдаст 5xx — правило «никаких тихих фолбэков»), а не врёт False.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import httpx
import pytest

from app import cloud_state, http_client, supa
from app.agent import runs_store


def _use_mock(monkeypatch: Any, handler: Any) -> None:
    """Пулированный клиент → httpx.Client с MockTransport; sleep между ретраями убран."""
    c = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "client", lambda: c)
    monkeypatch.setattr(http_client, "_sleep", lambda _s: None)


def _supa_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("BILLING_ENABLED", "true")


def _cloud_env(monkeypatch: Any) -> None:
    _supa_env(monkeypatch)
    monkeypatch.setenv("STORAGE_BACKEND", "r2")  # cloud_state.cloud_enabled() → runs_store в облако


class _LostReplyServer:
    """Мини-PostgREST: попытка №1 ПРИМЕНЯЕТ запрос, а потом «теряет» ответ (ReadTimeout).

    Именно так выглядит прод-сбой, из-за которого ввели ретраи: сервер обработал PATCH, но
    ответ не доехал. Фильтр (``params``) моделируется честно — как в PostgREST, по ТЕКУЩЕМУ
    (уже изменённому первой попыткой) состоянию строки.
    """

    def __init__(self, row: dict[str, Any], *, lose_reply_on: int = 1) -> None:
        self.row = dict(row)
        self.lose_reply_on = lose_reply_on
        self.patches = 0

    def _matches(self, params: httpx.QueryParams) -> bool:
        """Фильтр PostgREST по строке: поддерживаем eq./gte. на колонках (id/*_id — identity)."""
        for key, raw in params.multi_items():
            if key not in self.row:  # id / job_id / clip_id / run_id → строка одна, всегда матч
                continue
            op, _, value = raw.partition(".")
            current = self.row[key]
            if op == "eq" and str(current) != value:
                return False
            if op == "gte" and int(current) < int(value):
                return False
        return True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[self.row])
        if request.method != "PATCH":
            raise AssertionError(f"unexpected method {request.method}")
        self.patches += 1
        rows: list[dict[str, Any]] = []
        if self._matches(request.url.params):
            self.row.update(json.loads(request.content))  # ЗАПРОС ПРИМЕНЁН
            rows = [dict(self.row)]
        if self.patches == self.lose_reply_on:
            raise httpx.ReadTimeout("reply lost")  # …а ОТВЕТ потерян
        return httpx.Response(200, json=rows)


# ─────────── ДЕНЬГИ: consume_clip_credit не имеет права вернуть ложный False ───────────


class TestConsumeClipCreditLostReply:
    def test_lost_reply_raises_instead_of_reporting_no_credit(self, monkeypatch: Any) -> None:
        # Попытка №1 списала кредит (1 → 0), ответ потерян. Если PATCH повторить, фильтр
        # clip_credits=gte.1 матчит 0 строк → [] → False → main.extract_clip отдаёт 402:
        # кредит списан, клипа нет, юзеру предлагают заплатить ЕЩЁ раз.
        _supa_env(monkeypatch)
        server = _LostReplyServer({"plan": "free", "payg_credits": 0, "clip_credits": 1})
        _use_mock(monkeypatch, server)

        with pytest.raises(httpx.ReadTimeout):  # честная транспортная ошибка → 5xx у вызывателя
            supa.consume_clip_credit("u1")

        assert server.patches == 1  # CAS-PATCH НЕ повторён
        assert server.row["clip_credits"] == 0  # списание реально применилось на сервере

    def test_connect_phase_is_still_retried(self, monkeypatch: Any) -> None:
        # ConnectTimeout ≠ потерянный ответ: соединение не встало → запрос ТОЧНО не применился →
        # состояние не менялось → повтор не может дать ложный False. Ретрай тут ОБЯЗАН остаться
        # (иначе блип TLS = 5xx на ровном месте — ради этого пул с ретраями и вводили).
        _supa_env(monkeypatch)
        state = {"clip_credits": 1}
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json=[{"plan": "free", "clip_credits": 1}])
            if calls.count("PATCH") == 1:
                raise httpx.ConnectTimeout("SSL handshake timed out")  # до сервера не доехало
            state["clip_credits"] = 0
            return httpx.Response(200, json=[{"id": "u1", "clip_credits": 0}])

        _use_mock(monkeypatch, handler)
        assert supa.consume_clip_credit("u1") is True
        assert calls.count("PATCH") == 2 and state["clip_credits"] == 0


# ─────────── те же грабли, меньше радиус: optimistic lock и guard-отмена ───────────


class TestOptimisticLockLostReply:
    def test_update_clip_edit_if_version_lost_reply_raises(self, monkeypatch: Any) -> None:
        # Replay: фильтр version=eq.1 после успешной первой попытки (version уже 2) матчит 0
        # строк → lock_applied([]) = False → store.save_edit отдаёт 409 «данные изменились,
        # перезагрузите» на правку, которая НА САМОМ ДЕЛЕ сохранилась.
        _supa_env(monkeypatch)
        server = _LostReplyServer({"version": 1, "edit": {}})
        _use_mock(monkeypatch, server)

        with pytest.raises(httpx.ReadTimeout):
            cloud_state.update_clip_edit_if_version(
                "j1", "clip_01", {"captions": []}, expected_version=1, new_version=2
            )

        assert server.patches == 1
        assert server.row["version"] == 2  # правка сохранена — 409 был бы ложью


class TestAgentRunCancelLostReply:
    def test_set_cancelled_lost_reply_raises(self, monkeypatch: Any) -> None:
        # Replay: фильтр status=eq.running после успешной отмены (status='cancelled') матчит 0
        # строк → False → «не удалось отменить» на РЕАЛЬНО отменённом прогоне.
        _cloud_env(monkeypatch)
        server = _LostReplyServer({"status": "running", "cancellable": True})
        _use_mock(monkeypatch, server)

        with pytest.raises(httpx.ReadTimeout):
            runs_store.set_cancelled("ar_1")

        assert server.patches == 1
        assert server.row["status"] == "cancelled"


# ─────────── БЕЗУСЛОВНЫЕ PATCH'и (фильтр = identity) ретраятся КАК И РАНЬШЕ ───────────


class TestUnconditionalPatchesStillRetried:
    def test_add_clip_credits_replays_absolute_body_without_double_credit(
        self, monkeypatch: Any
    ) -> None:
        # Фильтр id=eq.u1 НЕ читает clip_credits → replay матчит ту же строку и пишет ТО ЖЕ
        # абсолютное значение (0+2=2, не 4). Ровно тот случай, ради которого PATCH идемпотентен.
        _supa_env(monkeypatch)
        server = _LostReplyServer({"id": "u1", "plan": "free", "clip_credits": 0})
        _use_mock(monkeypatch, server)

        supa.add_clip_credits("u1", 2)

        assert server.patches == 2  # потерянный ответ → повтор
        assert server.row["clip_credits"] == 2  # начислено ОДИН раз (тело абсолютное)

    def test_set_user_plan_is_retried(self, monkeypatch: Any) -> None:
        _supa_env(monkeypatch)
        server = _LostReplyServer({"id": "u1", "plan": "free"})
        _use_mock(monkeypatch, server)

        supa.set_user_plan("u1", "pro")

        assert server.patches == 2 and server.row["plan"] == "pro"

    def test_deduct_payg_is_retried(self, monkeypatch: Any) -> None:
        _supa_env(monkeypatch)
        server = _LostReplyServer({"id": "u1", "payg_credits": 5})
        _use_mock(monkeypatch, server)

        supa.deduct_payg("u1", 2)

        assert server.patches == 2 and server.row["payg_credits"] == 3  # списано один раз

    def test_cloud_state_update_status_is_retried(self, monkeypatch: Any) -> None:
        _supa_env(monkeypatch)
        server = _LostReplyServer({"id": "j1", "status": "queued", "progress": 0})
        _use_mock(monkeypatch, server)

        cloud_state.update_status("j1", "rendering", 80)

        assert server.patches == 2 and server.row["status"] == "rendering"


# ─────────── surface: явный opt-out ретраев на уровне http_client ───────────


class TestIdempotentOptOut:
    def test_patch_idempotent_false_is_not_replayed(self, monkeypatch: Any) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadTimeout("reply lost")

        _use_mock(monkeypatch, handler)
        with pytest.raises(httpx.ReadTimeout):
            http_client.patch("https://x/rest/v1/profiles", json={"a": 1}, idempotent=False)
        assert len(calls) == 1

    def test_patch_idempotent_false_still_retries_connect_phase(self, monkeypatch: Any) -> None:
        # Запрос не доехал → повтор безопасен ДАЖЕ для CAS: состояние сервера не менялось.
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json=[{"ok": True}])

        _use_mock(monkeypatch, handler)
        r = http_client.patch("https://x/rest/v1/profiles", json={"a": 1}, idempotent=False)
        assert r.status_code == 200 and len(calls) == 2

    def test_patch_idempotent_false_does_not_replay_5xx(self, monkeypatch: Any) -> None:
        # 5xx: сервер МОГ применить запрос частично → для CAS повтор так же ядовит, как ReadTimeout.
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, text="upstream")

        _use_mock(monkeypatch, handler)
        r = http_client.patch("https://x/rest/v1/profiles", json={"a": 1}, idempotent=False)
        assert r.status_code == 503 and len(calls) == 1  # без ретрая; raise_for_status у вызывателя


# ─────────── anti-drift: CAS-call-site'ы обязаны нести idempotent=False ───────────


@pytest.mark.parametrize(
    "fn",
    [
        supa.consume_clip_credit,
        cloud_state.update_clip_edit_if_version,
        runs_store.set_cancelled,
    ],
    ids=["consume_clip_credit", "update_clip_edit_if_version", "runs_store.set_cancelled"],
)
def test_cas_patch_call_sites_opt_out_of_retries(fn: Any) -> None:
    """Фильтр этих PATCH'ей зависит от изменяемого состояния → повтор врёт «0 строк»."""
    assert "idempotent=False" in inspect.getsource(fn), (
        f"{fn.__name__}: CAS-PATCH обязан быть idempotent=False (replay = ложный False)"
    )
