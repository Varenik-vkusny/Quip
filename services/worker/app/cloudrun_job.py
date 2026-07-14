"""Entrypoint для Cloud Run Job (`python -m app.cloudrun_job`).

Один Docker-образ = web-Service (CMD `uvicorn app.main:app`) + Job (этот модуль). Cloud Run
Service на POST /jobs (и т.п.) триггерит job execution (`dispatch.spawn` → RunJob) с
per-execution env-override'ами `JOB_KIND` + `JOB_ARGS` (JSON-список позиционных аргументов).
Эта точка входа их читает и зовёт соответствующую тяжёлую функцию в СВОЁМ контейнере.

Статус джобы пишется в Supabase самими tasks-функциями (они ловят ошибки → set_failed, правило
№8), поэтому падение внутри = failed-статус в БД; здесь просто отражаем это кодом возврата
(для наблюдаемости execution в Cloud Run), не пряча.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable


def main() -> int:
    kind = os.environ.get("JOB_KIND", "").strip()
    try:
        args = json.loads(os.environ.get("JOB_ARGS", "[]"))
    except json.JSONDecodeError as e:
        print(f"[cloudrun_job] bad JOB_ARGS: {e}", file=sys.stderr)
        return 2

    from app.tasks import (
        agent_edit_job,
        fanout_render_clip_job,
        generate_video_map_job,
        rebake_reframe_regions_job,
        render_clip_edit_job,
        render_extracted_clip_job,
        run_pipeline_job,
        run_preview_job,
        run_upload_job_from_r2,
    )

    # JOB_KIND → функция. Совпадает с fn_name в dispatch.spawn(...) на стороне web-Service
    # (и с прежними Modal-обёртками deploy/modal/worker.py). ``fanout_render_clip`` — контейнерный
    # фан-аут рендера: координатор (``upload_job``/``run_job``) спавнит execution НА КАЖДЫЙ клип
    # (dispatch.map_render_clips_cloudrun), каждый рендерит свой клип и пишет результат в R2.
    # ``rebake_regions`` — тяжёлый reframe-CV промаха кэша /reframe: раньше он гонялся ВНУТРИ
    # HTTP-запроса web-Service'а (8Gi, FS=RAM) и убивал контейнер по OOM вместе со всеми соседними
    # запросами (инцидент 2026-07-13). Аргументы: (job_id, clip_id|null → ВСЕ клипы джоба).
    handlers: dict[str, Callable[..., None]] = {
        "run_job": run_pipeline_job,
        "upload_job": run_upload_job_from_r2,
        "render_job": render_clip_edit_job,
        "preview_job": run_preview_job,
        "agent_edit_job": agent_edit_job,
        "generate_video_map_job": generate_video_map_job,
        "fanout_render_clip": fanout_render_clip_job,
        "render_extracted_clip_job": render_extracted_clip_job,
        "rebake_regions": rebake_reframe_regions_job,
    }
    fn = handlers.get(kind)
    if fn is None:
        print(f"[cloudrun_job] unknown JOB_KIND={kind!r}", file=sys.stderr)
        return 2

    print(f"[cloudrun_job] start kind={kind} args={args}")
    fn(*args)
    print(f"[cloudrun_job] done kind={kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
