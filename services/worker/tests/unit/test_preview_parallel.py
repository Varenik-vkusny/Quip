"""Preview-прокси + VideoMap ПАРАЛЛЕЛЬНО рендеру (не-Modal путь: Cloud Run / локально).

Раньше на Cloud Run оба шага были последовательным «хвостом» ПОСЛЕ всех клипов (координатор
фан-аута при этом простаивал в поллинге). Теперь они стартуют в фоновом ПОТОКЕ сразу после
заливки source в R2 и join'ятся ПЕРЕД финализацией (set_done у вызывателя). Контракты:

1. preview + video_map выполняются КОНКУРЕНТНО с рендером (не после) — event-ordering;
2. исключение внутри потока НЕ глотается: ре-рейзится на join → джоб падает как раньше;
3. артефакты/вызовы идентичны прежнему inline-хвосту (upload_preview, video_map, stages);
4. Modal-ветка (dispatch.spawn) НЕ тронута: поток не стартует, spawn'ы как были.

Реальный ffmpeg/Gemini не нужен — build_preview_proxy / generate_video_map_job мокаются
(паттерн харнесса — test_upload_defer.py).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.errors import JobError
from app.models import Segment, Transcript, Word


class _S:  # минимальный stub настроек (что читает run_pipeline на этом пути)
    transcript_cache_enabled = False
    preview_height = 540  # скраб-прокси (новый дефолт config.py; рендер идёт из source)
    preview_crf = 30
    transcription_provider = "deepgram"
    deepgram_model = "nova-3"


def _ok_result(idx: int) -> dict[str, Any]:
    return {
        "clip_id": f"clip_{idx:02d}", "clip_index": idx, "video_url": f"u{idx}",
        "reframe_lat": 0.0, "render_lat": 0.0, "face_found": True,
    }  # fmt: skip


def _setup_job(run_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_id: str) -> Path:
    """Полный кэш Stage 0–2 на диске (source.mp4 уже есть — yt/повторный прогон) + no-op db/storage.

    Возвращает out-каталог джоба. Тесты сверху перекрывают _render_all_clips /
    build_preview_proxy / upload_preview / generate_video_map_job под свой сценарий.
    """
    out = tmp_path / job_id
    out.mkdir(parents=True)
    meta = {
        "job_id": job_id, "source": "upload", "url": None, "title": "t",
        "duration": 120.0, "fps": 30.0, "width": 1920, "height": 1080,
    }  # fmt: skip
    (out / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (out / "source.mp4").write_bytes(b"mp4")
    tr = Transcript(language="en", duration=120.0, words=[Word(text="hi", start=0.0, end=0.5)])
    (out / "transcript.json").write_text(tr.model_dump_json(), encoding="utf-8")
    seg = Segment(start=0.0, end=10.0, reason="r", score=0.7, type="complete_thought",
                  hook="h", why_works="w")  # fmt: skip
    (out / "segments.json").write_text(json.dumps([seg.model_dump()]), encoding="utf-8")

    monkeypatch.setattr(run_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(run_mod, "get_settings", lambda: _S())
    monkeypatch.setattr(run_mod, "extract_loudness", lambda p: [])  # без ffmpeg
    monkeypatch.setattr(run_mod.dispatch, "modal_spawn_enabled", lambda: False)
    monkeypatch.setattr(run_mod.storage, "upload_source", lambda *a, **k: None)
    monkeypatch.setattr(run_mod.storage, "upload_preview", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "build_preview_proxy", lambda *a, **k: None)
    monkeypatch.setattr("app.tasks.generate_video_map_job", lambda jid: None)
    for fn in ("set_progress_detail", "put_job_artifact", "put_job_artifacts", "set_clips_pending"):
        monkeypatch.setattr(run_mod.db, fn, lambda *a, **k: None)
    return out


def test_preview_and_video_map_run_concurrently_with_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ГЛАВНЫЙ контракт: preview+video_map идут ВО ВРЕМЯ рендера, а не хвостом после.

    Фейковый рендер БЛОКИРУЕТСЯ, пока preview и video_map не отработают (event.wait с
    таймаутом). Старый серийный хвост (preview только ПОСЛЕ _render_all_clips) здесь
    падает по таймауту — превью в нём не может начаться, пока рендер не вернулся.
    """
    import app.run as run_mod

    out = _setup_job(run_mod, monkeypatch, tmp_path, "job_x")
    preview_done = threading.Event()
    map_done = threading.Event()
    order: list[str] = []

    def fake_preview(src: Path, dst: Path, *, height: int, crf: int) -> None:
        assert Path(src).exists(), "локальный source.mp4 обязан существовать на старте потока"
        assert (height, crf) == (540, 30)  # min(preview_height, meta.height)=540, crf как был
        Path(dst).write_bytes(b"proxy")
        order.append("preview")
        preview_done.set()

    def fake_map(jid: str) -> None:
        assert jid == "job_x"
        order.append("video_map")
        map_done.set()

    def fake_render_all(job_id, o, source_name, segments, m, user_id, subject_cxs=None):  # noqa: ANN001
        # Рендер «идёт» — preview и video_map обязаны отработать, ПОКА мы здесь.
        assert preview_done.wait(timeout=10), "preview не стартовал во время рендера (хвост!)"
        assert map_done.wait(timeout=10), "video_map не отработал во время рендера (хвост!)"
        order.append("render_end")
        return [_ok_result(1)]

    monkeypatch.setattr(run_mod, "build_preview_proxy", fake_preview)
    monkeypatch.setattr("app.tasks.generate_video_map_job", fake_map)
    monkeypatch.setattr(run_mod, "_render_all_clips", fake_render_all)

    job = run_mod.run_pipeline("job_x", source_url=None)

    assert order == ["preview", "video_map", "render_end"]  # конкурентно: хвост кончился ДО рендера
    assert job.status.value == "done"
    assert (out / "preview.mp4").exists()


def test_preview_thread_error_reraised_on_join_fails_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Правило №8 (никаких тихих фолбэков): ошибка в потоке preview НЕ глотается — ре-рейзится
    на join ТЕМ ЖЕ объектом исключения → джоб падает так же, как при прежнем inline-запуске.
    Рендер при этом успел завершиться (ошибка догоняет на join, не раньше).

    Действует ТОЛЬКО для пути БЕЗ on_ready (CLI/main): done ещё не сигналился → старая
    семантика. С on_ready (tasks.py) ошибка ловится ПОСЛЕ done → см. done-early тесты ниже."""
    import app.run as run_mod

    _setup_job(run_mod, monkeypatch, tmp_path, "job_e")
    boom = JobError("preview", "ffmpeg exit 1: proxy build failed")
    render_ran: list[bool] = []

    def fake_preview(*a: Any, **k: Any) -> None:
        raise boom

    def fake_render_all(job_id, o, source_name, segments, m, user_id, subject_cxs=None):  # noqa: ANN001
        render_ran.append(True)
        return [_ok_result(1)]

    monkeypatch.setattr(run_mod, "build_preview_proxy", fake_preview)
    monkeypatch.setattr(run_mod, "_render_all_clips", fake_render_all)

    with pytest.raises(JobError) as ei:
        run_mod.run_pipeline("job_e", source_url=None)

    assert ei.value is boom  # ре-рейз ТОГО ЖЕ исключения (семантика ошибок идентична inline)
    assert render_ran == [True]  # рендер прошёл; ошибка превью всё равно валит джоб
    # job.json НЕ записан (как и раньше: inline-провал случался ДО финализации)
    assert not (tmp_path / "job_e" / "job.json").exists()


def test_video_map_error_reraised_on_join_fails_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # То же для video_map-шага потока (второй участник хвоста).
    import app.run as run_mod

    _setup_job(run_mod, monkeypatch, tmp_path, "job_v")
    boom = RuntimeError("save_video_map: postgres down")

    def fake_map(jid: str) -> None:
        raise boom

    monkeypatch.setattr("app.tasks.generate_video_map_job", fake_map)
    monkeypatch.setattr(run_mod, "_render_all_clips", lambda *a, **k: [_ok_result(1)])

    with pytest.raises(RuntimeError) as ei:
        run_mod.run_pipeline("job_v", source_url=None)
    assert ei.value is boom


def test_preview_artifacts_and_results_identical_to_serial_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Результат идентичен прежнему хвосту: те же вызовы upload_preview/generate_video_map_job
    (те же аргументы), клипы в Job, stages.preview_proxy в runs.jsonl, job.json записан."""
    import app.run as run_mod

    out = _setup_job(run_mod, monkeypatch, tmp_path, "job_a")
    upload_calls: list[tuple[Path, str]] = []
    map_calls: list[str] = []

    def fake_preview(src: Path, dst: Path, *, height: int, crf: int) -> None:
        Path(dst).write_bytes(b"proxy")

    monkeypatch.setattr(run_mod, "build_preview_proxy", fake_preview)
    monkeypatch.setattr(
        run_mod.storage, "upload_preview", lambda p, jid: upload_calls.append((p, jid))
    )
    monkeypatch.setattr("app.tasks.generate_video_map_job", lambda jid: map_calls.append(jid))
    monkeypatch.setattr(run_mod, "_render_all_clips", lambda *a, **k: [_ok_result(1)])

    job = run_mod.run_pipeline("job_a", source_url=None)

    assert upload_calls == [(out / "preview.mp4", "job_a")]
    assert map_calls == ["job_a"]
    assert [c.video_url for c in job.clips] == ["u1"]
    assert job.status.value == "done"
    assert (out / "job.json").exists()
    run_line = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "preview_proxy" in run_line["stages"]  # телеметрия хвоста сохранена


def test_modal_spawn_branch_untouched_no_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Modal-ветка как была: preview_job/generate_video_map_job спавнятся контейнерами,
    inline/поточный preview НЕ выполняется (build_preview_proxy не зовётся)."""
    import app.run as run_mod

    _setup_job(run_mod, monkeypatch, tmp_path, "job_m")
    monkeypatch.setattr(run_mod.dispatch, "modal_spawn_enabled", lambda: True)
    spawns: list[str] = []
    monkeypatch.setattr(run_mod.dispatch, "spawn", lambda fn, jid: spawns.append(fn))
    monkeypatch.setattr(
        run_mod, "build_preview_proxy",
        lambda *a, **k: pytest.fail("preview must NOT run in-process on Modal"),
    )  # fmt: skip
    monkeypatch.setattr(
        "app.tasks.generate_video_map_job",
        lambda jid: pytest.fail("video_map must NOT run in-process on Modal"),
    )
    monkeypatch.setattr(run_mod, "_render_all_clips", lambda *a, **k: [_ok_result(1)])

    job = run_mod.run_pipeline("job_m", source_url=None)

    assert spawns == ["preview_job", "generate_video_map_job"]
    assert job.status.value == "done"


# ─────────────────────────── done-early: preview НЕ держит set_done ───────────────────────────
# Хвост-киллер (Cloud Run): на длинном источнике клипы готовы на минуте ~5, а done приходил на
# ~11.6 — join preview-потока стоял ПЕРЕД возвратом run_pipeline (set_done у вызывателя). Новый
# шов: run_pipeline(on_ready=...) зовёт on_ready(job) СРАЗУ по готовности клипов (tasks.py внутри
# него делает set_done+_meter), а preview/video_map-поток джойнится ПОСЛЕ — но ДО возврата
# (Cloud Run Job убивает контейнер на выходе процесса — недожатый поток = недокачанный preview).
# Ошибка хвоста ПОСЛЕ done НЕ валит джоб: это документированная деградация
# storage.preview_read_url (редактор фолбэчит на source URL, пока preview.mp4 нет в R2).


def test_on_ready_fires_without_waiting_for_preview_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ГЛАВНЫЙ контракт done-early: on_ready (=set_done) НЕ ждёт работающий preview-поток.

    Фейковый preview БЛОКИРУЕТСЯ, пока on_ready не отработал (release-event ставится ТОЛЬКО
    в on_ready). Старый порядок (join ПЕРЕД финализацией) здесь дедлочится до таймаута —
    done не мог случиться раньше конца превью."""
    import app.run as run_mod

    _setup_job(run_mod, monkeypatch, tmp_path, "job_de")
    release = threading.Event()
    order: list[str] = []

    def fake_preview(src: Path, dst: Path, *, height: int, crf: int) -> None:
        assert release.wait(timeout=10), "on_ready не вызван — done снова ждёт preview (хвост!)"
        Path(dst).write_bytes(b"proxy")
        order.append("preview_end")

    def on_ready(job: Any) -> None:
        assert job.status.value == "done"  # клипы готовы, Job целиком собран
        order.append("on_ready")
        release.set()

    monkeypatch.setattr(run_mod, "build_preview_proxy", fake_preview)
    monkeypatch.setattr(run_mod, "_render_all_clips", lambda *a, **k: [_ok_result(1)])

    job = run_mod.run_pipeline("job_de", source_url=None, on_ready=on_ready)

    # done СТРОГО раньше конца превью; превью при этом ДОЖАТО до возврата (join после done).
    assert order == ["on_ready", "preview_end"]
    assert job.status.value == "done"


def test_preview_failure_after_done_keeps_job_done_and_logs_loud(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ошибка preview ПОСЛЕ done: джоб НЕ падает (клипы валидны, редактор живёт на
    source-фолбэке storage.preview_read_url), но лог — ГРОМКИЙ (не тихий фолбэк)."""
    import app.run as run_mod

    out = _setup_job(run_mod, monkeypatch, tmp_path, "job_pf")
    ready_jobs: list[Any] = []

    def fake_preview(*a: Any, **k: Any) -> None:
        raise JobError("preview", "ffmpeg exit 1: proxy build failed")

    monkeypatch.setattr(run_mod, "build_preview_proxy", fake_preview)
    monkeypatch.setattr(run_mod, "_render_all_clips", lambda *a, **k: [_ok_result(1)])

    job = run_mod.run_pipeline("job_pf", source_url=None, on_ready=ready_jobs.append)  # НЕ рейзит

    assert ready_jobs and ready_jobs[0].status.value == "done"  # done сигнализирован
    assert job.status.value == "done"
    assert (out / "job.json").exists()  # финализация состоялась, джоб цел
    assert "[preview] FAILED after done: " in capsys.readouterr().out  # громко, не молча


def test_video_map_failure_after_done_keeps_job_done_and_logs_loud(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Второй участник хвоста (video_map: save упал → исключение из потока) — та же деградация:
    # джоб done, /video-map retry-able, лог громкий.
    import app.run as run_mod

    _setup_job(run_mod, monkeypatch, tmp_path, "job_vf")

    def fake_map(jid: str) -> None:
        raise RuntimeError("save_video_map: postgres down")

    monkeypatch.setattr("app.tasks.generate_video_map_job", fake_map)
    monkeypatch.setattr(run_mod, "_render_all_clips", lambda *a, **k: [_ok_result(1)])

    job = run_mod.run_pipeline("job_vf", source_url=None, on_ready=lambda j: None)

    assert job.status.value == "done"
    assert "[preview] FAILED after done: " in capsys.readouterr().out


def test_preview_thread_joined_before_run_pipeline_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Поток ВСЕГДА дожат до возврата run_pipeline (Cloud Run Job умирает с процессом:
    вернуться с живым потоком = оборванная заливка preview в R2)."""
    import app.run as run_mod

    _setup_job(run_mod, monkeypatch, tmp_path, "job_j")
    finished = threading.Event()

    def fake_preview(src: Path, dst: Path, *, height: int, crf: int) -> None:
        time.sleep(0.2)  # медленный энкод — вернуться раньше него нельзя
        Path(dst).write_bytes(b"proxy")
        finished.set()

    monkeypatch.setattr(run_mod, "build_preview_proxy", fake_preview)
    monkeypatch.setattr(run_mod, "_render_all_clips", lambda *a, **k: [_ok_result(1)])

    job = run_mod.run_pipeline("job_j", source_url=None, on_ready=lambda j: None)

    assert finished.is_set(), "run_pipeline вернулся, не дожав preview-поток (join потерян)"
    assert job.status.value == "done"
