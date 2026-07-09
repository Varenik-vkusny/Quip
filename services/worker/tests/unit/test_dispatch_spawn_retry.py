"""Unit-тесты устойчивости СПАВНА рендер-контейнеров к транзитивному rate-лимиту.

Cloud Run квота ``Job run requests`` = 60/мин на регион. При ВСПЛЕСКЕ (несколько юзеров стартуют
джобы разом: N координаторов × окно фан-аута спавнов за секунды) RunJob может отдать
``ResourceExhausted`` — запрос ОТКЛОНЁН ДО создания execution → повтор безопасен (нет двойного
спавна). Без ретрая одна такая осечка роняла ВЕСЬ фан-аут в run.py → джоб деградировал в медленный
in-process рендер. Ретрай с backoff держит его на быстром контейнерном пути.

Тестируем ЧИСТУЮ логику ретрая на seam'е callable (реальный run_v2/google-cloud локально нет).
"""

from __future__ import annotations

import time

import pytest

from app import dispatch


def test_is_retryable_spawn_error_matches_resource_exhausted_by_name() -> None:
    # Локальные классы с тем же ИМЕНЕМ, что google.api_core ResourceExhausted (импорта либы не
    # требуем — матчим по имени в MRO, ловит и подклассы).
    class ResourceExhausted(Exception):
        pass

    class SubExhausted(ResourceExhausted):
        pass

    assert dispatch._is_retryable_spawn_error(ResourceExhausted("rate")) is True
    assert dispatch._is_retryable_spawn_error(SubExhausted("rate")) is True
    # НЕ ретраибл: прочие ошибки (неверный путь/permission/логика) — не глотаем, летят наверх.
    assert dispatch._is_retryable_spawn_error(ValueError("bad path")) is False
    assert dispatch._is_retryable_spawn_error(RuntimeError("quota exhausted")) is False


def test_run_job_with_retry_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # без реальных пауз
    monkeypatch.delenv("RENDER_SPAWN_RETRIES", raising=False)  # дефолт (>=3 попытки)

    class ResourceExhausted(Exception):
        pass

    calls = {"n": 0}

    def call() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ResourceExhausted("rate limited")
        return "exec/ok"

    assert dispatch._run_job_with_retry(call) == "exec/ok"
    assert calls["n"] == 3  # две осечки, третья удачна


def test_run_job_with_retry_gives_up_after_attempts_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setenv("RENDER_SPAWN_RETRIES", "3")

    class ResourceExhausted(Exception):
        pass

    calls = {"n": 0}

    def call() -> str:
        calls["n"] += 1
        raise ResourceExhausted("still rate limited")

    with pytest.raises(ResourceExhausted):  # исчерпали попытки → летит наверх (fallback в run.py)
        dispatch._run_job_with_retry(call)
    assert calls["n"] == 3  # ровно RENDER_SPAWN_RETRIES попыток


def test_run_job_with_retry_non_retryable_propagates_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def call() -> str:
        calls["n"] += 1
        raise ValueError("bad job path")  # не rate-лимит → НЕ ретраим

    with pytest.raises(ValueError):
        dispatch._run_job_with_retry(call)
    assert calls["n"] == 1  # ровно один вызов, без ретраев


def test_spawn_retries_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RENDER_SPAWN_RETRIES", raising=False)
    assert dispatch._spawn_retries() == 4  # дефолт
    monkeypatch.setenv("RENDER_SPAWN_RETRIES", "2")
    assert dispatch._spawn_retries() == 2
    monkeypatch.setenv("RENDER_SPAWN_RETRIES", "0")  # clamp >= 1 (0 = без ретрая = одна попытка)
    assert dispatch._spawn_retries() == 1
    monkeypatch.setenv("RENDER_SPAWN_RETRIES", "junk")  # мусор → дефолт
    assert dispatch._spawn_retries() == 4
