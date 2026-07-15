"""Unit-тесты ГЛОБАЛЬНОГО бюджета рендер-контейнеров (Фаза 1c, spec 2026-07-15-multiuser-scaling).

Проблема: окно фан-аута ``RENDER_FANOUT_MAX`` — ПЕР-ВИДЕО. N координаторов (N юзеров) держат
каждый своё окно → суммарно до N×window контейнеров: большой аплоад забивает пул, мелкий
голодает. Глобальный бюджет = общий счётчик слотов в Supabase (CAS + TTL-reclaim): суммарно
≤ ``RENDER_GLOBAL_BUDGET`` рендеров по ВСЕМ джобам.

Тестируем ЧИСТУЮ логику УЧЁТА в ``_rolling_fanout`` на seam'ах ``_global_slot_acquire`` /
``_global_slot_release`` / ``_execution_finished`` / ``_run_job_on`` (реального Supabase локально
нет). Инварианты: (1) НИКОГДА больше бюджета одновременно; (2) acquire == release (слот не утёк);
(3) все клипы в итоге заспавнены; (4) при ВЫКЛЮЧЕННОМ бюджете БД не трогаем; (5) на таймауте
висящие слоты освобождаются.
"""

from __future__ import annotations

import time

import pytest

from app import dispatch


class _FakeBudget:
    """Ин-мемори имитация Supabase-счётчика: ``acquire`` отдаёт slot_id или None (полно),
    ``release`` чистит. Пишем peak (макс. одновременно занятых) и балансы для инвариантов."""

    def __init__(self, max_slots: int) -> None:
        self.max = max_slots
        self.held = 0
        self.peak = 0
        self.acquires = 0
        self.releases = 0

    def acquire(self, budget: int, job_id: str | None = None) -> str | None:
        if self.held >= self.max:
            return None
        self.held += 1
        self.acquires += 1
        self.peak = max(self.peak, self.held)
        return f"slot-{self.acquires}"

    def release(self, slot_id: str) -> None:
        assert slot_id, "release должен получать НЕпустой slot_id"
        self.held -= 1
        self.releases += 1


def _make_spawn(spawned: list) -> object:
    def run_job_on(job_path: str, fn_name: str, arg: tuple) -> str:
        spawned.append(arg)
        return f"exec-{arg[1]}"

    return run_job_on


def _make_finished(finish_after: int = 1) -> object:
    """Фейковый ``_execution_finished``: помечает execution завершённым на N-м опросе (деф. 1)."""
    polls: dict[str, int] = {}

    def finished(_client: object, name: str) -> bool:
        polls[name] = polls.get(name, 0) + 1
        return polls[name] >= finish_after

    return finished


def _args(n: int) -> list[tuple]:
    return [("job_x", i, None, None, "user") for i in range(n)]


def test_global_budget_caps_concurrent_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setenv("RENDER_GLOBAL_BUDGET", "3")
    monkeypatch.setenv("RENDER_FANOUT_MAX", "8")  # окно ШИРЕ бюджета → ограничитель именно бюджет
    fb = _FakeBudget(3)
    spawned: list[tuple] = []
    monkeypatch.setattr(dispatch, "_global_slot_acquire", fb.acquire)
    monkeypatch.setattr(dispatch, "_global_slot_release", fb.release)
    monkeypatch.setattr(dispatch, "_run_job_on", _make_spawn(spawned))
    monkeypatch.setattr(dispatch, "_executions_client", lambda: None)
    monkeypatch.setattr(dispatch, "_execution_finished", _make_finished(1))

    dispatch._rolling_fanout("render/job", _args(10), job_id="job_x")

    assert len(spawned) == 10  # все клипы заспавнены
    assert fb.peak <= 3  # НИКОГДА больше бюджета одновременно
    assert fb.acquires == 10  # по слоту на клип
    assert fb.releases == 10  # ничего не утекло
    assert fb.held == 0


def test_disabled_budget_never_touches_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.delenv("RENDER_GLOBAL_BUDGET", raising=False)  # ВЫКЛ
    monkeypatch.setenv("RENDER_FANOUT_MAX", "4")

    def boom(*_a: object, **_k: object) -> str:
        raise AssertionError("при выключенном бюджете БД-слоты вызываться НЕ должны")

    spawned: list[tuple] = []
    monkeypatch.setattr(dispatch, "_global_slot_acquire", boom)
    monkeypatch.setattr(dispatch, "_global_slot_release", boom)
    monkeypatch.setattr(dispatch, "_run_job_on", _make_spawn(spawned))
    monkeypatch.setattr(dispatch, "_executions_client", lambda: None)
    monkeypatch.setattr(dispatch, "_execution_finished", _make_finished(1))

    dispatch._rolling_fanout("render/job", _args(6))

    assert len(spawned) == 6  # все клипы заспавнены, окно = RENDER_FANOUT_MAX


def test_rolling_fanout_passes_custom_fn_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fn_name`` = JOB_KIND спавна: дефолт ``fanout_render_clip`` (обратная совместимость), но
    ре-бейк captioned (Фаза 1b: apply-silence-all/бэкфилл) фанаутит ``render_job`` под тем же
    глобальным бюджетом."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.delenv("RENDER_GLOBAL_BUDGET", raising=False)
    kinds: list[str] = []

    def run_job_on(_job_path: str, fn_name: str, arg: tuple) -> str:
        kinds.append(fn_name)
        return f"exec-{arg[1]}"

    monkeypatch.setattr(dispatch, "_run_job_on", run_job_on)
    monkeypatch.setattr(dispatch, "_executions_client", lambda: None)
    monkeypatch.setattr(dispatch, "_execution_finished", _make_finished(1))

    dispatch._rolling_fanout("render/job", _args(3), job_id="j", fn_name="render_job")
    assert kinds == ["render_job", "render_job", "render_job"]

    kinds.clear()
    dispatch._rolling_fanout("render/job", _args(2), job_id="j")  # дефолт
    assert kinds == ["fanout_render_clip", "fanout_render_clip"]


def test_timeout_releases_held_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setenv("RENDER_GLOBAL_BUDGET", "2")
    monkeypatch.setenv("RENDER_FANOUT_MAX", "2")
    monkeypatch.setenv("RENDER_FANOUT_TIMEOUT", "150")

    # Управляемые «часы»: каждый вызов monotonic += 100с. start=100, iter1 check=200 (Δ100, ок),
    # iter2 check=300 (Δ200 > 150 → таймаут). За iter1 успели заспавнить 2 (никогда не финишируют).
    clock = {"t": 0.0}

    def monotonic() -> float:
        clock["t"] += 100.0
        return clock["t"]

    fb = _FakeBudget(2)
    cancelled: list[str] = []
    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(dispatch, "_global_slot_acquire", fb.acquire)
    monkeypatch.setattr(dispatch, "_global_slot_release", fb.release)
    monkeypatch.setattr(dispatch, "_run_job_on", _make_spawn([]))
    monkeypatch.setattr(dispatch, "_executions_client", lambda: None)
    monkeypatch.setattr(
        dispatch, "_execution_finished", lambda _c, _n: False
    )  # никогда не финишируют
    monkeypatch.setattr(dispatch, "_cancel_execution", lambda _c, name: cancelled.append(name))

    dispatch._rolling_fanout("render/job", _args(5), job_id="job_x")

    assert len(cancelled) == 2  # висящие executions отменены
    assert fb.releases == fb.acquires == 2  # их слоты освобождены — не утекли
    assert fb.held == 0
