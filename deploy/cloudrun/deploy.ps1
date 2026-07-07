# Quip worker → Google Cloud Run (Service = FastAPI API; Job = тяжёлый пайплайн на видео).
# Один образ, два деплоя. Service на POST /jobs триггерит Cloud Run Job execution (dispatch.spawn
# → RunJob), каждая джоба = свой контейнер (изоляция RAM/CPU между юзерами, scale-to-zero, до 3ч).
#
# gcloud НАТИВНО под Windows (WSL НЕ нужен). Запускать из PowerShell. Секреты (значения ключей)
# заливаешь ТЫ на шаге 5 — я их не ввожу. Перед запуском: gcloud auth login; gcloud config set project.
#
# ⚠️ Отредактируй переменные ниже под себя, затем выполняй БЛОКАМИ (не всё сразу — часть одноразовая).

# ─────────────────────────── ПЕРЕМЕННЫЕ ───────────────────────────
$PROJECT = "quip-project-500711"   # привязан к «My Billing Account» с $300-кредитом
$REGION  = "europe-west1"          # или us-central1 — ближе к юзерам/R2
$REPO    = "quip"                  # Artifact Registry репозиторий
$IMAGE   = "$REGION-docker.pkg.dev/$PROJECT/$REPO/quip:latest"
$SA      = "quip-run@$PROJECT.iam.gserviceaccount.com"   # один SA на Service+Job
$WORKER  = "$PSScriptRoot/../../services/worker"          # контекст сборки (Dockerfile тут)

# ─────────────────────────── 1. APIs + Artifact Registry (одноразово) ───────────────────────────
gcloud services enable run.googleapis.com artifactregistry.googleapis.com `
  cloudbuild.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION `
  --description="quip worker images"

# ─────────────────────────── 2. Service account + права (одноразово) ───────────────────────────
gcloud iam service-accounts create quip-run --display-name="Quip Cloud Run"
# Service триггерит и отменяет Job execution'ы (run.jobs.runWithOverrides + run.executions.cancel):
gcloud projects add-iam-policy-binding $PROJECT `
  --member="serviceAccount:$SA" --role="roles/run.developer"
# Доступ к секретам (Supabase/R2/ключи):
gcloud projects add-iam-policy-binding $PROJECT `
  --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"

# ─────────────────────────── 3. Сборка + пуш образа (повторяется при правках кода) ───────────────────────────
gcloud builds submit $WORKER --tag $IMAGE

# ─────────────────────────── 4. Секреты (ТЫ вставляешь значения; одноразово, потом versions) ───────────────────────────
# Значения — из старых Modal-секретов quip-worker/quip-billing или первоисточников (Supabase/R2/Polar).
# Пример (повтори для каждого; --data-file=- читает stdin, чтобы значение не светилось в истории):
#   "твой_ключ" | gcloud secrets create SUPABASE_SERVICE_ROLE_KEY --data-file=-
foreach ($s in @("SUPABASE_SERVICE_ROLE_KEY","R2_ACCESS_KEY_ID","R2_SECRET_ACCESS_KEY",
                 "DEEPGRAM_API_KEY","GEMINI_API_KEY","POLAR_WEBHOOK_SECRET")) {
  Write-Host "Создай секрет $s:  <значение> | gcloud secrets create $s --data-file=-"
}

# ─────────────────────────── 5. Общие env/секреты (не-секретный конфиг + ссылки на секреты) ───────────────────────────
# R2_* не-ключевые + Supabase URL + провайдеры + биллинг-флаги — обычные env.
# Полный проверенный набор (зеркало рабочего Railway-env; тюнинг REFRAME_ENGINE=B, SIGNED_URL_TTL=3600
# перенесены — это НЕ дефолты кода!). Мёртвые Railway-переменные (DATA_DIR/TMP_DIR/DB_PATH/COST_LOG_PATH/
# MAX_SOURCE_MINUTES) НЕ тащим — код их не читает. Studio-тариф в коде пока нет → его продукта тут нет.
$ENVVARS = "STORAGE_BACKEND=r2,TRANSCRIPTION_PROVIDER=deepgram,LLM_PROVIDER=gemini,BILLING_ENABLED=true," +
           "R2_PUBLIC_URL=https://cdn.quip.ink,R2_BUCKET=quip," +
           "R2_ACCOUNT_ID=31ed389e60b64c3c8dfa7e6c0955d278," +
           "R2_ENDPOINT=https://31ed389e60b64c3c8dfa7e6c0955d278.r2.cloudflarestorage.com," +
           "SUPABASE_URL=https://mwfnjbnsowiaywkdrlyz.supabase.co," +
           "POLAR_PRODUCT_STARTER=64607ae8-4d14-4eb2-a873-4c07a88cc83e," +
           "POLAR_PRODUCT_PRO=3d989817-94b9-41ee-958b-d1b56013c980," +
           "POLAR_PRODUCT_PAYG=e7966387-a84b-4e2e-847d-df99862cf2b6," +
           "REFRAME_ENGINE=B,SIGNED_URL_TTL=3600,TARGET_ASPECT=9:16," +
           "CLIP_MIN_SEC=20,CLIP_MAX_SEC=60,CAPTION_MAX_WORDS_PER_GROUP=5," +
           "LLM_MODEL=gemini-2.5-flash,LLM_MAX_OUTPUT_TOKENS=16000"
$SECRETS = "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SERVICE_ROLE_KEY:latest," +
           "R2_ACCESS_KEY_ID=R2_ACCESS_KEY_ID:latest,R2_SECRET_ACCESS_KEY=R2_SECRET_ACCESS_KEY:latest," +
           "DEEPGRAM_API_KEY=DEEPGRAM_API_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest," +
           "POLAR_WEBHOOK_SECRET=POLAR_WEBHOOK_SECRET:latest"

# ─────────────────────────── 6. JOB-КООРДИНАТОР (import→transcribe→select→ФАН-АУТ рендера) ───────────────────────────
# ЖИВОЙ конфиг: 8 vCPU/32Gi. Клипы НЕ рендерятся здесь: координатор спавнит контейнер-на-клип
# (quip-render, §6b) — CLOUDRUN_JOB/REGION/RENDER_JOB это включают (cloudrun_fanout_enabled).
# RENDER_FANOUT_MAX=6 — сколько клип-контейнеров держим параллельно (потолок под квоту CPU региона).
# RENDER_CONCURRENCY=1 = фолбэк in-process рендер (если фан-аут упал: квота/API).
# Ephemeral-disk (Part C, 2026-07-07): source.mp4 качается на ДИСК (`type=ephemeral-disk`, gen2 beta,
# mount=/root/data/tmp=TMPDIR), НЕ на tmpfs=RAM → большой source больше не давит на память (был OOM на
# 2ч/1440p / много клипов). size=32Gi требует quota-increase сверх дефолтных 10Gi (Cloud Run → Quotas);
# без квоты ставь size=10Gi (хватает source'у ≲9 ГБ). Применить к СУЩЕСТВУЮЩИМ job'ам — §6c (update, не create).
gcloud beta run jobs create quip-pipeline `
  --image=$IMAGE --region=$REGION --service-account=$SA `
  --command=python --args="-m,app.cloudrun_job" `
  --task-timeout=10800s --max-retries=1 --tasks=1 --parallelism=1 `
  --cpu=8 --memory=32Gi --execution-environment=gen2 `
  --add-volume=name=tmp,type=ephemeral-disk,size=32Gi `
  --add-volume-mount=volume=tmp,mount-path=/root/data/tmp `
  --set-env-vars=("$ENVVARS,TMPDIR=/root/data/tmp,CLOUDRUN_JOB=quip-pipeline,CLOUDRUN_REGION=$REGION," +
                  "CLOUDRUN_RENDER_JOB=quip-render,RENDER_FANOUT_MAX=6,RENDER_FANOUT_TIMEOUT=2600,RENDER_CONCURRENCY=1") `
  --set-secrets="$SECRETS"

# ─────────────────── 6b. JOB-РЕНДЕР (контейнер-на-клип, ФАН-АУТ) — параллелизм как на Modal ───────────────────
# Координатор спавнит ПО ОДНОМУ execution на клип (JOB_KIND=fanout_render_clip); каждый качает source
# из R2, рендерит СВОЙ клип (stages 3–5), пишет result-dict в R2. cpu=4 (Modal тоже 4). Source теперь на
# ephemeral-DISK (§6, не tmpfs=RAM) → mem=16Gi снова хватает (была заплатка 16→32Gi под OOM; с диском
# память освобождается). ⚠️ Свериться с ЖИВЫМ значением памяти render (могло остаться 32Gi). task-timeout 20м + 1 ретрай.
gcloud beta run jobs create quip-render `
  --image=$IMAGE --region=$REGION --service-account=$SA `
  --command=python --args="-m,app.cloudrun_job" `
  --task-timeout=1200s --max-retries=1 --tasks=1 --parallelism=1 `
  --cpu=4 --memory=16Gi --execution-environment=gen2 `
  --add-volume=name=tmp,type=ephemeral-disk,size=32Gi `
  --add-volume-mount=volume=tmp,mount-path=/root/data/tmp `
  --set-env-vars="$ENVVARS,TMPDIR=/root/data/tmp" --set-secrets="$SECRETS"

# ─────────────────── 6c. Применить ephemeral-disk к СУЩЕСТВУЮЩИМ job'ам (Part C, 2026-07-07) ───────────────────
# Job'ы уже созданы (§6/§6b одноразовые) → не пересоздавать, а ОБНОВИТЬ (нужен beta; для size>10Gi — quota-increase).
# gcloud beta run jobs update quip-pipeline --region=$REGION --add-volume=name=tmp,type=ephemeral-disk,size=32Gi --add-volume-mount=volume=tmp,mount-path=/root/data/tmp
# gcloud beta run jobs update quip-render   --region=$REGION --add-volume=name=tmp,type=ephemeral-disk,size=32Gi --add-volume-mount=volume=tmp,mount-path=/root/data/tmp

# ─────────────────────────── 7. SERVICE (FastAPI API) — знает имя Job'а для триггера ───────────────────────────
# CLOUDRUN_JOB/REGION → dispatch.spawn триггерит Job. GOOGLE_CLOUD_PROJECT Cloud Run ставит сам.
gcloud run deploy quip-api `
  --image=$IMAGE --region=$REGION --service-account=$SA `
  --command=uvicorn --args="app.main:app,--host,0.0.0.0,--port,8080" `
  --cpu=1 --memory=1Gi --allow-unauthenticated `
  --set-env-vars="$ENVVARS,CLOUDRUN_JOB=quip-pipeline,CLOUDRUN_REGION=$REGION" `
  --set-secrets="$SECRETS"

# ─────────────────────────── 8. URL сервиса → репойнт фронта + Polar ───────────────────────────
gcloud run services describe quip-api --region=$REGION --format="value(status.url)"
# → Vercel env NEXT_PUBLIC_WORKER_URL = этот URL (+ redeploy фронта)
# → Polar webhook URL = <этот URL>/webhooks/polar
