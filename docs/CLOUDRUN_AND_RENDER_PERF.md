# Cloud Run миграция + рендер-перформанс — HANDOFF (2026-07-02)

> **Зачем этот файл:** цельный контекст сессии 2026-07-02 — как воркер переехал с забаненного Modal на
> Google Cloud Run, как устроен рендер сейчас, ГДЕ что смотреть в сервисах, и — главное — **какие есть
> рычаги, чтобы ускорить рендер** (следующая сессия занимается именно этим).
>
> Источник правды по «текущей реальности» — `docs/README.md`; полная история — `docs/JOURNAL.md`
> (записи `2026-07-02`); правила — `CLAUDE.md`; инвариант reframe — `docs/REFRAME_FPS_GRID_INVARIANT.md`.
> Этот файл — операционная карта + план ускорения, НЕ дублируй сюда правила.

---

## 0. TL;DR — что живо ПРЯМО СЕЙЧАС

| Что | Значение |
|-----|----------|
| Облако воркера | **Google Cloud Run**, проект `quip-project-500711`, регион `europe-west1` |
| Образ (один на всё) | `europe-west1-docker.pkg.dev/quip-project-500711/quip/quip:latest` — текущий digest `sha256:1ebad8b9945d18862b3aa693d398388746450d8f30f74a3a2826c047f0c4db26` |
| **quip-api** | Cloud Run **Service** (FastAPI/uvicorn, публичный). URL: `https://quip-api-487498137049.europe-west1.run.app`. Принимает `POST /jobs` (и т.п.) → триггерит job. |
| **quip-pipeline** | Cloud Run **Job**-координатор (8 vCPU / 32Gi). Делает import→transcribe→select→заливка source в R2→**фан-аут рендера**→preview→done. |
| **quip-render** | Cloud Run **Job** фан-аута (4 vCPU / 16Gi). ОДИН execution = ОДИН клип (reframe+render). Координатор спавнит по контейнеру на клип. |
| БД | **Supabase** (новый проект) `mwfnjbnsowiaywkdrlyz.supabase.co` — старый проект умер |
| Файлы | **Cloudflare R2**, бакет `quip`, CDN `https://cdn.quip.ink` |
| Биллинг | **Polar** (включён), продукты Starter/Pro/PAYG |
| Фронт | **Vercel** `quip-app` (автодеплой на push в `main`); `NEXT_PUBLIC_WORKER_URL` → URL quip-api |
| Modal | **МЁРТВ** (забанен по ToS). `deploy/modal/worker.py` больше ничего не деплоит. |

**Рендер сейчас = контейнер-на-клип (как было на Modal), клипы считаются параллельно.**
**Bottleneck рендера = reframe-CV (MediaPipe + Light-ASD + PySceneDetect) ~2–6 мин/клип** — вот что
надо ускорять (§4). Сам ffmpeg-энкод клипа ~25с (не он тормозит).

---

## 1. Хронология сессии — что за чем делали

1. **Стартовая задача:** починить упавший тест квоты (`test_polar.py`) + проверить фри-план лимиты.
   Пофикшено (минутная модель квоты, persist-DB dedup по job_id).
2. **Modal лёг → забанен.** Спенд-лимит Modal маскировался под CORS-ошибку; потом аккаунт заблокировали
   по ToS. Продукт встал.
3. **Срочная миграция воркера на Cloud Run.** Перебрали Beam/Railway/Render → выбрали **Cloud Run Jobs**
   (изоляция на джобу, scale-to-zero, до 3ч, есть $300 кредит). Railway пробовали — 1GB OOM на trial.
4. **Собрал инфру Cloud Run:** `Dockerfile` (порт Modal-образа: статик ffmpeg≥7, Deno+bgutil POT,
   MediaPipe/cv2, CPU-torch; `COPY app /root/app` = запекает рабочее дерево), `app/cloudrun_job.py`
   (entrypoint-диспетчер JOB_KIND→функция), `app/dispatch.py` (Cloud Run ветка spawn/cancel),
   `app/tasks.py` (`run_upload_job_from_r2`), `deploy/cloudrun/deploy.ps1` (весь деплой).
5. **Новый Supabase** (старый умер). Восстановил ПОЛНУЮ схему: миграции `0001–0015` НЕ содержали
   `job_artifacts`/`transcript_cache`/`clip_edits`/`runs` + половину колонок `jobs` (они жили только в
   инфра-плане `docs/superpowers/plans/2026-06-12-infra-phase-a-state-migration.md`). Собрал единый SQL
   (base_state + миграции + patch колонок jobs + seed аккаунтов) → прогнал в Supabase SQL Editor.
   После ALTER — `NOTIFY pgrst, 'reload schema';` (иначе PostgREST 400 на новых колонках).
6. **Пойманный баг upload-complete 500:** `dispatch._cloudrun_job_path` читал `os.environ["CLOUDRUN_PROJECT"]`,
   а Cloud Run его сам НЕ ставит → KeyError → чистый HTTPException(500) без трейса. Фикс — задал env
   `CLOUDRUN_PROJECT`. После этого аплоад заработал (202).
7. **Рендер медленный. Итерация A (потоки):** нашёл что на Cloud Run `modal_spawn_enabled()`=False →
   код шёл в «локальную» ветку = ПОСЛЕДОВАТЕЛЬНЫЙ рендер (клип за клипом). Сделал `ThreadPoolExecutor`
   (`RENDER_CONCURRENCY`) внутри контейнера. **Оказалось не быстрее** — на 8 vCPU 4 клипа переподписывают
   ядра (каждый клип уже cv2/ffmpeg-многопоточный): замер `render=77с` под нагрузкой против `25с` соло.
8. **Рендер. Итерация B (контейнерный фан-аут — ТЕКУЩЕЕ):** восстановил модель Modal — **контейнер на
   клип**. Новый job `quip-render`; `dispatch.map_render_clips_cloudrun` спавнит execution на клип
   (батчами ≤ `RENDER_FANOUT_MAX`), каждый пишет результат в R2, координатор собирает. Потоковый путь
   остался как ФОЛБЭК. Весь путь под try/except. Задеплоено, smoke-протестировано (образ грузится, код
   импортится), 1110 unit-тестов зелёные. **Боевой E2E-замер ещё не сделан — следующий шаг.**

> Побочно в сессии: правил REFRAME_ENGINE (случайно переключил B→A, откатил — A ухудшает кадрирование);
> добавил seed-аккаунты (akybaevtimur7 Pro+кредиты, askhat1601 Pro+1000мин) без нужды логина.

---

## 2. Карта сервисов — ГДЕ и КАК смотреть

> Во ВСЕХ PowerShell-командах сначала refresh PATH (иначе нет gcloud):
> `$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`
> и `$env:CLOUDSDK_CORE_DISABLE_PROMPTS="1"`. gcloud НАТИВНЫЙ под Windows (не WSL). Аккаунт `akybaevtimur7@gmail.com`.

### Cloud Run (главное)
- **Список джоб:** `gcloud run jobs list --region=europe-west1`
- **Список executions (кто бежит/упал):** `gcloud run jobs executions list --job=quip-pipeline --region=europe-west1 --limit=6 --format="value(metadata.name,status.runningCount,status.succeededCount,status.failedCount,metadata.creationTimestamp)"`
- **Конфиг джобы (образ/cpu/mem/env):** `gcloud run jobs describe quip-pipeline --region=europe-west1 --format=json | ConvertFrom-Json` → путь к контейнеру: `.spec.template.spec.template.spec.containers[0]` (KRM v1 формат; НЕ `.template.template`).
- **ЛОГИ джобы (все executions):**
  `gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=quip-render" --limit=60 --format=json --freshness=30m | ConvertFrom-Json` → группируй по метке `run.googleapis.com/execution_name` (в `.labels`), сортируй по `.timestamp`, смотри `.textPayload`.
  ⚠️ Строковый `--filter` с кавычками через label ЛОМАЕТСЯ о PowerShell — тяни JSON и фильтруй в PS (не в gcloud-filter).
- **Строки рендера в логах:** `clip_NN: <start>-<end> face=True regions=N fit=M render=Xs` — `render=` это ТОЛЬКО ffmpeg-энкод; reframe-CV не логируется отдельно, но виден как пауза между клипами.
- **Console:** https://console.cloud.google.com/run?project=quip-project-500711

### Supabase
- Проект `mwfnjbnsowiaywkdrlyz` (`https://mwfnjbnsowiaywkdrlyz.supabase.co`). Привязан к почте владельца.
- Ключевые таблицы: `jobs` (статус/клипы/прогресс), `job_artifacts` (meta/segments/transcript/**reframe_regions**/video_map/preview_moments), `profiles` (plan/payg_credits — квота/вотермарка), `clip_edits` (edit-state редактора), `transcript_cache`, `runs`, `usage` (квота).
- **reframe_regions** в `job_artifacts` — ЗАПЕЧЁННЫЕ регионы кадрирования; редактор/`/reframe` читают их БЕЗ пересчёта. Меняешь reframe-логику → чисти: `update public.job_artifacts set reframe_regions = null;` ПОСЛЕ деплоя (см. `CLAUDE.md` §Reframe).
- Схема живёт в файлах `services/worker/migrations/0001–0015*.sql` + base_state-блок (в комбинированном SQL, что прогоняли в SQL Editor). Меняешь схему → и файл-миграцию, и прогон в Supabase.

### Cloudflare R2 (файлы)
- Бакет `quip`, account `31ed389e60b64c3c8dfa7e6c0955d278`, endpoint `https://31ed389e60b64c3c8dfa7e6c0955d278.r2.cloudflarestorage.com`, публичный CDN `https://cdn.quip.ink`.
- Ключи (все — `app/storage.py`): `{job_id}/source.mp4`, `{job_id}/clips/{clip_id}.mp4`, `{job_id}/preview.mp4`, **`{job_id}/_fanout/clip_NN.json`** (result-dict фан-аута — координатор их собирает).
- Клиент — boto3 S3 (`storage._r2_client()`).

### Secret Manager (значения ключей — я их НЕ вижу, ставит владелец)
`SUPABASE_SERVICE_ROLE_KEY`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `DEEPGRAM_API_KEY`,
`GEMINI_API_KEY`, `POLAR_WEBHOOK_SECRET` — каждый смонтирован в оба job'а как `--set-secrets ...:latest`.
`gcloud secrets list`.

### Vercel + Polar
- Vercel `quip-app`: автодеплой на push в `main`. `NEXT_PUBLIC_WORKER_URL` ДОЛЖЕН указывать на URL quip-api
  (проверь в Vercel env; при смене URL сервиса — обнови + redeploy фронта). Supabase URL/anon тоже в Vercel env.
- Polar webhook URL = `<quip-api URL>/webhooks/polar`. Продукты: Starter `64607ae8-…`, Pro `3d989817-…`, PAYG `e7966387-…`.

---

## 3. Как работает рендер СЕЙЧАС (контейнерный фан-аут)

```
upload → quip-api (Service) → dispatch.spawn → RunJob → quip-pipeline execution (координатор)
  ├ stage0 import (source.mp4 отложен до после select)
  ├ stage1 transcribe (Deepgram/Groq, кэш по хэшу)
  ├ stage2 select (Gemini — сегменты клипов)
  ├ заливка source.mp4 в R2  (storage.upload_source)  ← ДО фан-аута
  ├ _render_all_clips → dispatch.map_render_clips_cloudrun(job_id, clip_spawn_args)
  │     └ БАТЧАМИ ≤ RENDER_FANOUT_MAX: спавнит quip-render execution НА КЛИП
  │          (JOB_KIND=fanout_render_clip, JOB_ARGS=(job_id,idx,seg,meta,user_id))
  │          quip-render: ensure_source (R2) → render_one_clip (reframe+render+upload+set_clip_ready)
  │                       → storage.upload_fanout_result (R2 json)
  │          координатор: _wait_executions (poll: succeeded+failed>=task_count)
  │                       → storage.download_fanout_result по clip_index → сборка в порядке сегментов
  │                       → нет результата (клип упал) = failed_clip_result (пустой url, джоб не валится)
  ├ preview-прокси (build_preview_proxy) + generate_video_map_job  ← ПОКА inline в координаторе ПОСЛЕ клипов
  └ set_done
```

**Ключевые файлы кода:**
| Файл | Что |
|------|-----|
| `services/worker/app/run.py` | `_render_all_clips` (ветки modal/cloudrun-fanout/in-process), `_render_clips_in_process` (фолбэк-потоки), `render_one_clip` (stages 3–5 одного клипа), `clip_spawn_args` |
| `services/worker/app/dispatch.py` | `map_render_clips_cloudrun`, `_run_job_on`, `_wait_executions`, `_chunk`, `cloudrun_fanout_enabled`, `_cloudrun_render_job_path` |
| `services/worker/app/tasks.py` | `fanout_render_clip_job` (что бежит в quip-render), `run_upload_job_from_r2` |
| `services/worker/app/cloudrun_job.py` | entrypoint: JOB_KIND→функция (handler `fanout_render_clip`) |
| `services/worker/app/storage.py` | `upload_fanout_result`/`download_fanout_result`/`fanout_result_key` |
| `services/worker/app/pipeline/stage3_reframe.py` | **reframe = BOTTLENECK** (MediaPipe faces + ASD + PySceneDetect) |
| `services/worker/app/pipeline/asd_reframe.py` | **Light-ASD** — самый тяжёлый кусок reframe (per-frame NN) |
| `services/worker/app/pipeline/stage5_render.py` | движки A (ffmpeg-expr, быстрый) / B (cv2 per-frame, плавный — ТЕКУЩИЙ) |

**Env фан-аута (на quip-pipeline):** `CLOUDRUN_JOB=quip-pipeline`, `CLOUDRUN_REGION=europe-west1`,
`CLOUDRUN_RENDER_JOB=quip-render`, `RENDER_FANOUT_MAX=6`, `RENDER_FANOUT_TIMEOUT=2600`,
`RENDER_CONCURRENCY=1` (фолбэк), `REFRAME_ENGINE=B`. Kill-switch фан-аута: `CLOUDRUN_FANOUT=0`.
> Внутри job'а `cloudrun_enabled()` требует `CLOUDRUN_JOB` — без него фан-аут выключен и рендер идёт
> in-process (потоки). Это была причина, почему потоки вообще запускались.

---

## 4. КАК УСКОРИТЬ (главная секция для следующей сессии) — по возрастанию риска

> ⚠️ **ОБНОВЛЕНИЕ 2026-07-02 (вечер): замер §4.0 СДЕЛАН — картина уточнилась.** Главный тормоз — НЕ
> reframe-CV в целом, а `_refine_cut_native` без `-t` капа (декод до конца source на каждую склейку,
> ∝ длине source за клипом). Полный отчёт: замеры / ранжированные рычаги / цены GPU / маржа —
> **`docs/archive/2026-07-02-render-speedup-research.md`** (он новее и точнее этой секции).

### 4.0 Сначала ЗАМЕРЬ (без замера не тюнь)
Загрузи одно и то же видео, в логах quip-render/quip-pipeline посчитай: (а) время от `select` до первого
клипа, (б) паузу между клипами = reframe одного клипа, (в) `render=` (энкод). Раздели: reframe vs энкод vs
cold-start контейнера. Дальше тюнь то, что реально доминирует (скорее всего reframe).

### 4.1 Дёшево и безопасно (env, без ребилда — `gcloud run jobs update … --update-env-vars`)
- **Проверь квоту CPU и подними `RENDER_FANOUT_MAX` 6→8–10.** Больше клипов параллельно = меньше волн.
  Сначала: `gcloud run jobs executions list` во время боевого прогона — если executions висят в
  `pending`/не стартуют, уперлись в квоту (тогда не поднимать / запросить quota increase в Console →
  IAM & Admin → Quotas → Cloud Run Admin API CPU). Если ок — поднимай.
- **`RENDER_FANOUT_TIMEOUT`** — если тяжёлые клипы режутся по таймауту, подними (сейчас 2600с).
- Фолбэк-путь: `RENDER_CONCURRENCY` (для in-process, если фан-аут отвалится) — оставь 1.

### 4.2 Средне (правки кода вне reframe — низкий риск для кадровой сетки)
- **Preview + video_map увести в ФАН-АУТ** (сейчас `run.py` строит их INLINE в координаторе ПОСЛЕ клипов —
  серийный хвост, добавляет секунды-минуты). На Modal они спавнились параллельно. Сделать `preview_job`/
  `generate_video_map_job` отдельными execution'ами (как fanout_render_clip) и не ждать их в критическом пути.
- **Cold-start клип-контейнеров.** Каждый quip-render execution холодный (тянет образ + импорт
  cv2/mediapipe/torch ~30–60с). Варианты: `--min-instances`/warmpool (стоит денег), либо облегчить импорт
  (ленивая загрузка torch — он не нужен для reframe engine B?), либо смириться (это разово на клип, параллельно).
- **Крупнее клип-контейнеры:** `quip-render` cpu 4→8 (быстрее reframe на клип), но ×2 квоту → баланс с `RENDER_FANOUT_MAX`.

### 4.3 Дорого/рискованно — но тут самый большой выигрыш (reframe = ~2/3 времени клипа)
> ⚠️ **HIGH RISK: reframe трогает кадровую сетку.** ОБЯЗАТЕЛЬНО читай `docs/REFRAME_FPS_GRID_INVARIANT.md`
> ПЕРЕД любой правкой `stage3_reframe`/`stage5_render`/`asd_reframe`; проверяй ПОКАДРОВО на реальном
> рендере; чисти `reframe_regions` кэш после деплоя. Есть готовый пропозал: `docs/archive/2026-06-30-render-optimization-proposal.md`.
- **Light-ASD (`asd_reframe.py`) — самый тяжёлый.** Он гоняет per-frame нейросеть по КАЖДОМУ треку лица.
  Рычаги: понизить fps сэмплинга ASD; **скипать ASD для одно-лицевых клипов** (ASD нужен только для выбора
  активного спикера при 2+ лицах — для одного лица он лишний); ограничить число треков.
- **`reframe_face_fps` / сэмплинг MediaPipe** — ниже fps = быстрее, риск дёрганого трекинга.
- **PySceneDetect** — downscale входа / порог; обычно не главный расход, но проверь замером.
- **GPU на Cloud Run** (L4) — может радикально ускорить ASD/MediaPipe/энкод (NVENC). Нужен GPU-квота,
  CUDA-образ (torch-cuda вместо cpu), проверка что MediaPipe/ASD реально грузят GPU. Большой переезд, но
  потенциально самый быстрый рендер. Cloud Run поддерживает GPU для Jobs.
- **Движок A вместо B** для рендера (`REFRAME_ENGINE=A`) — быстрее энкод (один ffmpeg-проход vs cv2
  per-frame), НО кадрирование дёрганее (владелец против; в этой сессии откатывали A→B). Компромисс — не
  бесплатный по качеству.
- **Кэш source между клипами.** Сейчас КАЖДЫЙ quip-render качает ПОЛНЫЙ source из R2 (для 86-мин видео
  ~300МБ × N контейнеров). Идея: клип-контейнер качает только нужный [start,end] диапазон (ffmpeg range
  request по presigned URL) вместо всего файла — меньше egress и RAM. Тонко (seek-точность), но реально.

### 4.4 Инфра-нюанс
- **source на tmpfs=RAM** (нет ephemeral-disk volume — GA gcloud не умеет, нужен beta-компонент). Для
  крупных видео (2ч/1440p ~10ГБ) это давит на RAM. quip-render 16Gi это учитывает, но при высокой
  конкуренции/4K подними beta gcloud + `--add-volume type=ephemeral-disk`.

---

## 5. Команды — билд/деплой/тюнинг (PowerShell, с refresh PATH)

```powershell
# refresh PATH (в КАЖДОМ вызове)
$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$env:CLOUDSDK_CORE_DISABLE_PROMPTS="1"; $env:PYTHONIOENCODING="utf-8"

# 1) Ребилд образа (из рабочего дерева; ~5 мин). Даёт НОВЫЙ digest.
gcloud builds submit "C:\Users\user\Desktop\Quip\services\worker" `
  --tag "europe-west1-docker.pkg.dev/quip-project-500711/quip/quip:latest" --region=europe-west1

# 2) ⚠️ Cloud Run ПИНИТ digest на деплое — :latest сам НЕ подтянется. После ребилда ОБНОВИ ОБА джоба
#    (и Service) на новый digest (@sha256:… из вывода билда):
$IMG = "europe-west1-docker.pkg.dev/quip-project-500711/quip/quip@sha256:<НОВЫЙ_DIGEST>"
gcloud run jobs update quip-pipeline --region=europe-west1 --image=$IMG
gcloud run jobs update quip-render   --region=europe-west1 --image=$IMG
gcloud run services update quip-api  --region=europe-west1 --image=$IMG

# 3) Тюнинг БЕЗ ребилда (env):
gcloud run jobs update quip-pipeline --region=europe-west1 --update-env-vars="RENDER_FANOUT_MAX=8"

# 4) Гейт перед деплоем (из services/worker): just check (ruff+mypy+tsc+eslint+unit+anti-drift) ЗЕЛЁНЫЙ.
#    Быстро: uv run ruff check app; uv run mypy app; uv run pytest tests/unit -q
```
Полный первичный деплой (создание джоб/секретов/IAM) — **`deploy/cloudrun/deploy.ps1`** (актуализирован
под живой конфиг, включая quip-render §6b). Секреты (значения) ставит владелец.

---

## 6. Грабли и инварианты — НЕ сломай

- **CLOUDRUN_PROJECT обязателен** в env джобы (Cloud Run НЕ ставит `GOOGLE_CLOUD_PROJECT` сам в Job) — иначе KeyError→500.
- **CLOUDRUN_JOB на самом job'е** нужен, чтобы внутри включился `cloudrun_enabled()` (иначе фан-аут выкл, рендер in-process).
- **Digest-пиннинг:** после `builds submit` ОБЯЗАТЕЛЬНО `jobs/services update --image=@sha256:…` на ОБА джоба.
- **REFRAME_ENGINE=B** (плавный). A — быстрее, но дёрганее; владелец против. Не переключай без спроса.
- **reframe_regions кэш:** после правки reframe-логики — `update job_artifacts set reframe_regions=null;` ПОСЛЕ деплоя.
- **Кадровая сетка (`REFRAME_FPS_GRID_INVARIANT.md`)** — reframe/render правь только прочитав; проверяй покадрово на реальном видео.
- **Movable: НЕ трогать `deploy/modal/worker.py`** как источник правды — Modal мёртв (но код — эталон логики фан-аута).
- **PowerShell:** кириллица в git-commit → файл + `git commit -F`; gcloud-filter с кавычками ломается → тяни JSON и фильтруй в PS; `2>&1` на нативном gcloud даёт ложный exit -1 (смотри текст вывода, не код).
- **Тесты:** `tests/unit/test_fanout_render.py` + `test_parallel_render.py` (fanout+фолбэк). Мокают run_v2/R2 (google.cloud стоит только в образе, локально нет → импорты ленивые).

---

## 7. Открытые пункты (что НЕ сделано)

- [ ] **Боевой E2E-замер фан-аута** — задеплоено+smoke-тест (образ грузится, код импортится, 1110 unit
      зелёные), но реальный аплоад с замером скорости ещё не прогнан. ПЕРВЫЙ шаг следующей сессии: залить
      видео, посмотреть логи, подтвердить что клипы реально считаются параллельно + замерить (§4.0).
- [ ] Подтвердить CPU-квоту региона → поднять `RENDER_FANOUT_MAX` 6→8–10.
- [ ] Preview/video_map увести в фан-аут (сейчас inline-хвост в координаторе).
- [ ] Reframe-ускорение (§4.3) — главный выигрыш, но HIGH RISK.
- [ ] **Не закоммичено:** вся работа сессии в рабочем дереве (образ строится из него). Закоммитить, чтобы не потерять.
- [ ] Ephemeral-disk volume вместо tmpfs (beta gcloud) для крупных source.
