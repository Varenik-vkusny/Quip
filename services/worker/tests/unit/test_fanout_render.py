"""Unit-тесты контейнерного фан-аута рендера (Cloud Run: один execution на клип).

Тестируем ЧИСТУЮ координацию СКОЛЬЗЯЩЕГО ОКНА: ≤ RENDER_FANOUT_MAX executions в полёте,
освободился слот (клип дошёл до терминала) → СРАЗУ стартует следующий (не ждём остальных, как
ждали фикс-батчи); сборку результатов В ПОРЯДКЕ входа, синтез failed_clip_result при отсутствии
результата, таймаут (отмена в полёте + непроспавненные → failed), gating. Реальные run_job/
poll/R2 — I/O (замоканы на seam'ах dispatch — google-cloud либ локально нет); реальный рендер —
e2e (не здесь).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app import dispatch, storage


def _ok(idx: int) -> dict[str, Any]:
    return {
        "clip_id": f"clip_{idx:02d}",
        "clip_index": idx,
        "video_url": f"u{idx}",
        "reframe_lat": 1.0,
        "render_lat": 2.0,
        "face_found": True,
    }


class FakeCluster:
    """Симулятор Cloud Run Jobs для скользящего окна.

    Спавн (``run_job_on``) регистрирует execution как «в полёте» и снимает метрики
    (макс. одновременных, кто ещё не завершён В МОМЕНТ спавна). Завершения управляются
    СКРИПТОМ: каждый вызов ``sleep`` (= один поллинг-цикл координатора) завершает очередную
    порцию execution'ов из ``script`` (порядок ЛЮБОЙ — можно завершать не-FIFO, чтобы
    проверить «освободился слот → следующий клип стартует, НЕ дожидаясь остальных»).
    """

    def __init__(self, script: list[list[str]] | None = None) -> None:
        self.unfinished: set[str] = set()  # «в полёте» с точки зрения Cloud Run
        self.spawned: list[tuple[str, tuple[Any, ...]]] = []  # (fn_name, args) в порядке спавна
        self.unfinished_at_spawn: list[set[str]] = []  # снапшот «в полёте» ПЕРЕД каждым спавном
        self.max_in_flight = 0
        self.cancelled: list[str] = []
        self.script = list(script or [])
        self.sleeps = 0

    @staticmethod
    def exec_name(a: tuple[Any, ...]) -> str:
        return f"exec/{a[1]}"

    def run_job_on(self, job_path: str, fn_name: str, a: tuple[Any, ...]) -> str:
        name = self.exec_name(a)
        self.unfinished_at_spawn.append(set(self.unfinished))
        self.spawned.append((fn_name, a))
        self.unfinished.add(name)
        self.max_in_flight = max(self.max_in_flight, len(self.unfinished))
        return name

    def execution_finished(self, client: Any, name: str) -> bool:
        return name not in self.unfinished

    def cancel_execution(self, client: Any, name: str) -> None:
        self.cancelled.append(name)
        self.unfinished.discard(name)

    def sleep(self, _seconds: float) -> None:
        self.sleeps += 1
        assert self.sleeps < 1000, "fan-out loop did not terminate (test hang guard)"
        if self.script:
            for name in self.script.pop(0):
                assert name in self.unfinished, f"script completes unspawned/finished {name}"
                self.unfinished.discard(name)

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dispatch, "_run_job_on", self.run_job_on)
        monkeypatch.setattr(dispatch, "_executions_client", lambda: object())
        monkeypatch.setattr(dispatch, "_execution_finished", self.execution_finished)
        monkeypatch.setattr(dispatch, "_cancel_execution", self.cancel_execution)
        monkeypatch.setattr(time, "sleep", self.sleep)


def _args(n: int) -> list[tuple[Any, ...]]:
    return [("job_x", i, {"s": i}, {"m": 1}, "user_1") for i in range(1, n + 1)]


def test_fanout_max_in_flight_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RENDER_FANOUT_MAX", raising=False)
    assert dispatch._fanout_max_in_flight() == 8  # дефолт
    monkeypatch.setenv("RENDER_FANOUT_MAX", "3")
    assert dispatch._fanout_max_in_flight() == 3
    monkeypatch.setenv("RENDER_FANOUT_MAX", "0")  # clamp >= 1
    assert dispatch._fanout_max_in_flight() == 1
    monkeypatch.setenv("RENDER_FANOUT_MAX", "junk")  # мусор → дефолт 8
    assert dispatch._fanout_max_in_flight() == 8


def test_cloudrun_fanout_enabled_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDRUN_JOB", "quip-pipeline")  # cloudrun_enabled()
    monkeypatch.setenv("CLOUDRUN_RENDER_JOB", "quip-render")
    monkeypatch.delenv("CLOUDRUN_FANOUT", raising=False)
    assert dispatch.cloudrun_fanout_enabled() is True
    monkeypatch.setenv("CLOUDRUN_FANOUT", "0")  # kill-switch
    assert dispatch.cloudrun_fanout_enabled() is False
    monkeypatch.setenv("CLOUDRUN_FANOUT", "1")
    monkeypatch.delenv("CLOUDRUN_RENDER_JOB", raising=False)  # нет render-job → off
    assert dispatch.cloudrun_fanout_enabled() is False
    monkeypatch.setenv("CLOUDRUN_RENDER_JOB", "quip-render")
    monkeypatch.delenv("CLOUDRUN_JOB", raising=False)  # не Cloud Run → off
    assert dispatch.cloudrun_fanout_enabled() is False


def _prep_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDRUN_PROJECT", "p")
    monkeypatch.setenv("CLOUDRUN_REGION", "r")
    monkeypatch.setenv("CLOUDRUN_RENDER_JOB", "quip-render")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("RENDER_FANOUT_TIMEOUT", raising=False)


def test_map_render_clips_cloudrun_spawns_per_clip_collects_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prep_env(monkeypatch)
    monkeypatch.setenv("RENDER_FANOUT_MAX", "8")
    cluster = FakeCluster(script=[["exec/1", "exec/2"]])
    cluster.patch(monkeypatch)
    monkeypatch.setattr(storage, "download_fanout_result", lambda job_id, idx: _ok(idx))

    results = dispatch.map_render_clips_cloudrun("job_x", _args(2))
    assert [r["clip_index"] for r in results] == [1, 2]  # порядок входа
    assert [r["video_url"] for r in results] == ["u1", "u2"]
    assert len(cluster.spawned) == 2
    assert all(fn == "fanout_render_clip" for fn, _ in cluster.spawned)  # правильный JOB_KIND
    assert cluster.spawned[0][1][4] == "user_1"  # user_id несётся в клип-контейнер


def test_map_render_clips_cloudrun_missing_result_becomes_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prep_env(monkeypatch)
    monkeypatch.setenv("RENDER_FANOUT_MAX", "8")
    cluster = FakeCluster(script=[["exec/1", "exec/2"]])
    cluster.patch(monkeypatch)
    # клип 2 не оставил результата в R2 (execution упал/убит ДО записи) → None
    monkeypatch.setattr(
        storage, "download_fanout_result", lambda job_id, idx: None if idx == 2 else _ok(idx)
    )
    results = dispatch.map_render_clips_cloudrun("job_x", _args(2))
    assert results[0]["video_url"] == "u1"
    assert results[1]["failed"] is True  # синтезирован failed_clip_result
    assert results[1]["video_url"] == ""
    assert results[1]["clip_index"] == 2


def test_rolling_window_never_exceeds_max_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    _prep_env(monkeypatch)
    monkeypatch.setenv("RENDER_FANOUT_MAX", "2")  # окно 2, 5 клипов
    # каждый поллинг-цикл завершается ровно один execution → окно постоянно пополняется
    cluster = FakeCluster(
        script=[["exec/1"], ["exec/2"], ["exec/3"], ["exec/4"], ["exec/5"]],
    )
    cluster.patch(monkeypatch)
    monkeypatch.setattr(storage, "download_fanout_result", lambda job_id, idx: _ok(idx))

    results = dispatch.map_render_clips_cloudrun("job_x", _args(5))
    assert cluster.max_in_flight == 2  # НИ В ОДИН момент не больше RENDER_FANOUT_MAX
    # все 5 клипов заспавнены РОВНО по разу, в порядке входа
    assert [a[1] for _, a in cluster.spawned] == [1, 2, 3, 4, 5]
    assert [r["clip_index"] for r in results] == [1, 2, 3, 4, 5]  # контракт порядка не изменился


def test_rolling_window_starts_next_without_waiting_for_stragglers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Страглер (exec/1) висит, а сосед (exec/2) закончился → клип 3 стартует СРАЗУ.

    При старых фикс-батчах клип 3 ждал бы завершения ВСЕГО батча {1,2}.
    """
    _prep_env(monkeypatch)
    monkeypatch.setenv("RENDER_FANOUT_MAX", "2")
    cluster = FakeCluster(script=[["exec/2"], [], ["exec/3"], ["exec/1"]])
    cluster.patch(monkeypatch)
    monkeypatch.setattr(storage, "download_fanout_result", lambda job_id, idx: _ok(idx))

    results = dispatch.map_render_clips_cloudrun("job_x", _args(3))
    assert [a[1] for _, a in cluster.spawned] == [1, 2, 3]
    # в момент спавна клипа 3 страглер exec/1 ещё В ПОЛЁТЕ (не ждали его, батч бы ждал)
    assert cluster.unfinished_at_spawn[2] == {"exec/1"}
    assert [r["clip_index"] for r in results] == [1, 2, 3]


def test_rolling_window_all_clips_spawned_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _prep_env(monkeypatch)
    monkeypatch.setenv("RENDER_FANOUT_MAX", "3")
    # завершения вразнобой (не-FIFO) — каждый клип всё равно спавнится ровно один раз
    cluster = FakeCluster(
        script=[["exec/3"], ["exec/1", "exec/2"], ["exec/5", "exec/4"], ["exec/6", "exec/7"]],
    )
    cluster.patch(monkeypatch)
    monkeypatch.setattr(storage, "download_fanout_result", lambda job_id, idx: _ok(idx))

    results = dispatch.map_render_clips_cloudrun("job_x", _args(7))
    spawned_indices = [a[1] for _, a in cluster.spawned]
    assert sorted(spawned_indices) == [1, 2, 3, 4, 5, 6, 7]
    assert len(spawned_indices) == len(set(spawned_indices))  # без дублей
    assert spawned_indices == sorted(spawned_indices)  # очередь — в порядке входа
    assert [r["clip_index"] for r in results] == [1, 2, 3, 4, 5, 6, 7]
    assert cluster.max_in_flight <= 3


def test_rolling_window_timeout_cancels_in_flight_and_fails_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Таймаут фан-аута: висящие executions отменяются, функция НЕ бросает (как раньше);
    клипы без результата в R2 (в т.ч. так и не заспавненные) → failed_clip_result."""
    _prep_env(monkeypatch)
    monkeypatch.setenv("RENDER_FANOUT_MAX", "2")
    monkeypatch.setenv("RENDER_FANOUT_POLL", "8")
    monkeypatch.setenv("RENDER_FANOUT_TIMEOUT", "10")
    cluster = FakeCluster(script=[])  # НИКТО не завершается — все висят до таймаута
    cluster.patch(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def sleep_advances(seconds: float) -> None:
        cluster.sleep(seconds)
        clock[0] += seconds

    monkeypatch.setattr(time, "sleep", sleep_advances)
    monkeypatch.setattr(storage, "download_fanout_result", lambda job_id, idx: None)

    results = dispatch.map_render_clips_cloudrun("job_x", _args(4))  # НЕ бросает
    assert sorted(cluster.cancelled) == ["exec/1", "exec/2"]  # висевшие отменены
    assert [a[1] for _, a in cluster.spawned] == [1, 2]  # 3,4 после дедлайна НЕ спавним
    assert [r["clip_index"] for r in results] == [1, 2, 3, 4]  # контракт порядка сохранён
    assert all(r["failed"] is True and r["video_url"] == "" for r in results)


def test_rolling_window_spawn_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой спавна (квота/API) НЕ глотается — летит наверх, fallback живёт в run.py."""
    _prep_env(monkeypatch)
    monkeypatch.setenv("RENDER_FANOUT_MAX", "2")
    cluster = FakeCluster()
    cluster.patch(monkeypatch)

    def boom(job_path: str, fn_name: str, a: tuple[Any, ...]) -> str:
        raise RuntimeError("quota exhausted")

    monkeypatch.setattr(dispatch, "_run_job_on", boom)
    with pytest.raises(RuntimeError, match="quota exhausted"):
        dispatch.map_render_clips_cloudrun("job_x", _args(3))
