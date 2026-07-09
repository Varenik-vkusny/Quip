"""Диспатч тяжёлых задач: отдельный контейнер на джобу (prod) или FastAPI BackgroundTask (dev).

Три бэкенда, выбираются по env:
  • **Cloud Run Jobs** (боевой, ``CLOUDRUN_JOB`` задан): web-Service триггерит Cloud Run Job
    execution на КАЖДУЮ джобу (свой контейнер, scale-to-zero, изоляция RAM/CPU между юзерами,
    таймаут до 24ч). Параметры летят per-execution env-override'ами (JOB_KIND+JOB_ARGS),
    их читает entrypoint ``app.cloudrun_job``. Возвращаем имя execution → отмена (Stop) по нему.
  • **Modal** (legacy, ``MODAL_SPAWN=1``): ``run_job.spawn(...)`` в долгоживущей функции. Отмена —
    ``modal.FunctionCall.from_id(id).cancel()``. (Аккаунт забанен; ветка для совместимости.)
  • **local/dev** (ничего не задано): вызыватель делает ``bg.add_task`` в процессе.

⚠️ POST-эндпоинт на scale-to-zero web-Service ОБЯЗАН триггерить job (не BackgroundTask): после
ответа web-контейнер гаснет и фон-таск умирает на полпути нарезки. Job живёт отдельно.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

_MODAL_APP = "quip-worker"


def modal_spawn_enabled() -> bool:
    """Спавнить ли через Modal (legacy). True только внутри Modal-образа (``MODAL_SPAWN=1``)."""
    return os.environ.get("MODAL_SPAWN", "").strip() == "1"


def cloudrun_enabled() -> bool:
    """Диспатчить ли через Cloud Run Jobs. True, если задан ``CLOUDRUN_JOB`` (имя job'а)."""
    return bool(os.environ.get("CLOUDRUN_JOB", "").strip())


def spawn_enabled() -> bool:
    """Есть ли внешний бэкенд для тяжёлой джобы (Cloud Run / Modal). Иначе — ``bg.add_task``."""
    return cloudrun_enabled() or modal_spawn_enabled()


def _cloudrun_job_path() -> str:
    """``projects/{project}/locations/{region}/jobs/{job}`` из env. ``GOOGLE_CLOUD_PROJECT`` на
    Cloud Run проставляется автоматически; регион/имя задаём в деплое."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ["CLOUDRUN_PROJECT"]
    region = os.environ["CLOUDRUN_REGION"]
    job = os.environ["CLOUDRUN_JOB"]
    return f"projects/{project}/locations/{region}/jobs/{job}"


def _cloudrun_render_job_path() -> str:
    """Путь выделенного job'а контейнерного фан-аута рендера (``CLOUDRUN_RENDER_JOB``): ОДИН
    контейнер на клип, ПАРАЛЛЕЛЬНО (как Modal). Отдельный job — меньше CPU/клип (главный job с
    import/тяжёлым CV не множим). Проект/регион общие с главным."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ["CLOUDRUN_PROJECT"]
    region = os.environ["CLOUDRUN_REGION"]
    job = os.environ["CLOUDRUN_RENDER_JOB"]
    return f"projects/{project}/locations/{region}/jobs/{job}"


def _is_retryable_spawn_error(exc: BaseException) -> bool:
    """Транзитивная ли это ошибка RunJob, которую БЕЗОПАСНО повторить. ``ResourceExhausted`` =
    rate-лимит квоты ``Job run requests`` (60/мин/регион) или конкурентный лимит: запрос ОТКЛОНЁН
    ДО создания execution → повтор не даёт двойного спавна. Матчим по имени в MRO, чтобы не тянуть
    ``google.api_core.exceptions`` локально (её в unit-окружении нет). Прочие ошибки (неверный
    путь/permission/логика) НЕ ретраибл — летят наверх (правило №8, не глотаем)."""
    return any(t.__name__ == "ResourceExhausted" for t in type(exc).__mro__)


def _spawn_retries() -> int:
    """Сколько ПОПЫТОК спавна делаем при rate-лимите (вкл. первую). ``RENDER_SPAWN_RETRIES``,
    деф.4; clamp >= 1 (1 = без ретрая). Kill-switch: поставить 1."""
    try:
        return max(1, int(os.environ.get("RENDER_SPAWN_RETRIES", "4")))
    except ValueError:
        return 4


def _run_job_with_retry(call: Callable[[], Any]) -> Any:
    """Вызвать ``call`` (RunJob) с экспоненциальным backoff на транзитивном rate-лимите
    (``_is_retryable_spawn_error``). Всплеск спавнов при N одновременных джобах пробивает
    ``Job run requests``=60/мин → ``ResourceExhausted``; несколько секунд паузы дают токенам
    накопиться. Исчерпали попытки ИЛИ ошибка не-ретраибл → пробрасываем (fallback в run.py)."""
    import time

    attempts = _spawn_retries()
    for i in range(attempts):
        try:
            return call()
        except Exception as e:  # noqa: BLE001 — решаем по типу: ретраибл rate-лимит vs проброс
            if i == attempts - 1 or not _is_retryable_spawn_error(e):
                raise
            delay = 2.0 * (2**i)
            print(
                f"[fanout] RunJob rate-limited ({type(e).__name__}); "
                f"retry {i + 1}/{attempts - 1} in {delay:.0f}s"
            )
            time.sleep(delay)


def _run_job_on(job_path: str, fn_name: str, args: tuple[Any, ...]) -> str:
    """Запустить execution job'а ``job_path`` с per-execution env-override ``JOB_KIND``+``JOB_ARGS``
    (мёржатся с базовым env job'а → секреты/конфиг деплоя выживают). Возвращает имя execution
    (``projects/.../executions/...``) для поллинга/отмены. Общее ядро ``spawn()`` и фан-аута.
    Сам RunJob-вызов обёрнут в ретрай на rate-лимит (см. ``_run_job_with_retry``)."""
    from google.cloud import run_v2

    client = run_v2.JobsClient()
    req = run_v2.RunJobRequest(
        name=job_path,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[
                        run_v2.EnvVar(name="JOB_KIND", value=fn_name),
                        run_v2.EnvVar(name="JOB_ARGS", value=json.dumps(args)),
                    ],
                )
            ],
        ),
    )
    op = _run_job_with_retry(lambda: client.run_job(request=req))
    # metadata.name несёт имя execution сразу (не блокируемся на .result() — тот ждёт конца).
    return str(op.metadata.name)


def spawn(fn_name: str, *args: Any) -> str | None:
    """Запустить тяжёлую задачу ``fn_name`` с аргументами ``args`` во ВНЕШНЕМ контейнере.

    Cloud Run → триггерит job execution (env-override ``JOB_KIND``+``JOB_ARGS``), возвращает имя
    execution (``projects/.../executions/...``) для отмены. Modal → ``FunctionCall.object_id``.
    local/dev (нет бэкенда) → ``None`` (вызыватель делает ``bg.add_task``).
    """
    if cloudrun_enabled():
        return _run_job_on(_cloudrun_job_path(), fn_name, args)
    if modal_spawn_enabled():
        import modal

        fn = modal.Function.from_name(_MODAL_APP, fn_name)
        return str(fn.spawn(*args).object_id)
    return None


def cancel(handle: str) -> None:
    """Отменить запущенную задачу по её ``handle`` (Stop-кнопка). Cloud Run execution-имя
    (содержит ``/executions/``) → ``ExecutionsClient.cancel_execution``; иначе Modal
    ``FunctionCall.from_id(handle).cancel()``. Бросает при сбое (не молча — правило №8)."""
    if "/executions/" in handle:
        from google.cloud import run_v2

        client = run_v2.ExecutionsClient()
        client.cancel_execution(request=run_v2.CancelExecutionRequest(name=handle))
        return
    import modal

    modal.FunctionCall.from_id(handle).cancel(terminate_containers=False)


def map_render_clips(args: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Фан-аут per-clip рендера на Modal: один контейнер на клип (``starmap``). ТОЛЬКО Modal —
    на Cloud Run см. ``map_render_clips_cloudrun`` (контейнер-на-клип), локально
    — in-process пул. Результаты В ПОРЯДКЕ входа (стабильный ``clip_index``). ``args`` =
    ``(job_id, clip_index, seg_dict, meta_dict, user_id)`` (см. ``run.clip_spawn_args``)."""
    import modal

    fn = modal.Function.from_name(_MODAL_APP, "reframe_render_clip")
    return list(fn.starmap(args))


def cloudrun_fanout_enabled() -> bool:
    """Рендерить ли клипы КОНТЕЙНЕРНЫМ фан-аутом (execution на клип) вместо in-process пула.
    Требует Cloud Run + заданный ``CLOUDRUN_RENDER_JOB``; kill-switch ``CLOUDRUN_FANOUT=0``
    (тогда деградация в in-process рендер главного контейнера)."""
    return (
        cloudrun_enabled()
        and bool(os.environ.get("CLOUDRUN_RENDER_JOB", "").strip())
        and os.environ.get("CLOUDRUN_FANOUT", "1").strip() != "0"
    )


def _fanout_max_in_flight() -> int:
    """Сколько клип-контейнеров держим ПАРАЛЛЕЛЬНО (потолок скользящего окна — под квоту CPU
    региона, чтобы не заспавнить сразу 20 execution'ов → ResourceExhausted).
    ``RENDER_FANOUT_MAX``, деф.8."""
    try:
        return max(1, int(os.environ.get("RENDER_FANOUT_MAX", "8")))
    except ValueError:
        return 8


def _executions_client() -> Any:
    """Клиент поллинга/отмены execution'ов (lazy-import: google-cloud есть только в образе)."""
    from google.cloud import run_v2

    return run_v2.ExecutionsClient()


def _execution_finished(client: Any, name: str) -> bool:
    """Дошёл ли execution до терминала: ``succeeded_count + failed_count >= task_count``
    (task_count=1 на клип). failed — ТОЖЕ терминал: слот окна освобождается, результата в R2
    не будет → координатор синтезирует failed_clip_result (провал НЕ прячем, правило №8)."""
    ex = client.get_execution(name=name)
    return bool((ex.succeeded_count + ex.failed_count) >= max(1, ex.task_count))


def _cancel_execution(client: Any, name: str) -> None:
    """Отменить execution по имени (таймаут фан-аута). Бросает при сбое — вызыватель логирует."""
    from google.cloud import run_v2

    client.cancel_execution(request=run_v2.CancelExecutionRequest(name=name))


def _rolling_fanout(render_job: str, args: list[tuple[Any, ...]]) -> None:
    """СКОЛЬЗЯЩЕЕ ОКНО спавна клип-executions: ≤ ``RENDER_FANOUT_MAX`` в полёте одновременно;
    как только ЛЮБОЙ дошёл до терминала (succeeded ИЛИ failed) — сразу стартует следующий клип
    из очереди (в порядке входа). Заменяет фикс-батчи: там весь следующий батч ждал ХУДШЕГО
    клипа предыдущего (замер prod 2026-07-02: страглер держал батч 16.5 мин при 5 готовых
    соседях), плюс хвостовой мини-батч ловил лишний cold-start.

    Поллинг терминальности — каждые ``RENDER_FANOUT_POLL`` (деф.8с; ≤ окно get_execution-вызовов
    на цикл, спавн-refill ≤1 RunJob на завершение — далеко от лимита 180 RunJob/60с).
    ``RENDER_FANOUT_TIMEOUT`` (деф.1800с) — ОБЩИЙ дедлайн всего фан-аута: по истечении висящие
    executions отменяются, недоспавненные клипы НЕ стартуют (результата в R2 не будет →
    координатор синтезирует failed_clip_result; НЕ бросаем — как прежний таймаут). Сбой самого
    спавна (квота/API) НЕ глотаем — летит наверх (fallback в in-process рендер живёт в run.py).
    Блокируется до терминала/отмены ВСЕХ заспавненных executions."""
    import time

    poll = float(os.environ.get("RENDER_FANOUT_POLL", "8"))
    timeout = float(os.environ.get("RENDER_FANOUT_TIMEOUT", "1800"))
    window = _fanout_max_in_flight()
    client = _executions_client()
    queue = list(args)
    in_flight: list[str] = []
    start = time.monotonic()
    while queue or in_flight:
        if time.monotonic() - start > timeout:
            for name in in_flight:
                try:
                    _cancel_execution(client, name)
                except Exception as e:  # noqa: BLE001 — отмена best-effort, таймаут уже решён
                    print(f"[fanout] cancel of timed-out execution failed: {e}")
            if queue:
                print(
                    f"[fanout] timeout: {len(queue)} clip(s) never spawned "
                    "(no R2 result -> failed_clip_result)"
                )
            return
        while queue and len(in_flight) < window:
            in_flight.append(_run_job_on(render_job, "fanout_render_clip", queue.pop(0)))
        in_flight = [n for n in in_flight if not _execution_finished(client, n)]
        # слот освободился и очередь не пуста → НЕ спим, сразу пополняем окно
        if in_flight and (not queue or len(in_flight) >= window):
            time.sleep(poll)


def map_render_clips_cloudrun(job_id: str, args: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Контейнерный фан-аут per-clip рендера на Cloud Run: ОДИН execution job'а
    ``CLOUDRUN_RENDER_JOB`` на клип, СКОЛЬЗЯЩИМ ОКНОМ ≤ ``RENDER_FANOUT_MAX`` в полёте
    (см. ``_rolling_fanout``: завершился один — сразу стартует следующий, страглер не держит
    соседей). Cloud Run execution'ы не возвращают значение (в отличие от Modal RPC) → каждый
    клип пишет result-dict в R2 (``storage.upload_fanout_result``); дождавшись конца фан-аута,
    координатор читает результаты и собирает список В ПОРЯДКЕ входа (стабильный ``clip_index``).
    Нет результата (execution упал/убит ДО записи, таймаут) → ``failed_clip_result`` (пустой
    ``video_url``). Так восстанавливаем параллелизм Modal (контейнер-на-клип) вместо in-process
    пула на 8 vCPU. ``args`` = ``(job_id, clip_index, seg, meta, user_id)``
    (см. ``run.clip_spawn_args``)."""
    from app import storage
    from app.run import failed_clip_result

    _rolling_fanout(_cloudrun_render_job_path(), args)
    results: dict[int, dict[str, Any]] = {}
    for a in args:
        clip_index = int(a[1])
        r = storage.download_fanout_result(job_id, clip_index)
        results[clip_index] = r or failed_clip_result(
            clip_index, "container fan-out: no result (clip execution crashed/killed)"
        )
    return [results[int(a[1])] for a in args]
