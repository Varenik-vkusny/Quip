# Quip PROD deploy (repeatable, run SUPERVISED). Deploys the CURRENT checkout (services/worker + apps/web).
# Prereqs: gcloud auth (akybaevtimur7, project quip-project-500711) + vercel auth (varenik-vkusny).
# NOTE: this deploys whatever branch is checked out.
#
# ⚠️ RUN PENDING DB MIGRATIONS **BEFORE** THIS SCRIPT. The pipeline writes columns on every run; a
#    missing column makes PostgREST 400 → EVERY JOB FAILS. (Bit us with 0016; 0019_baked_policy is
#    the current one.) Check with:
#      select column_name from information_schema.columns
#      where table_schema='public' and table_name='job_artifacts' and column_name='baked_policy';
#
# ⚠️ WORKER AND FRONTEND SHIP TOGETHER (this script does both, in order). Since 2026-07-14 the API
#    answers 202 {"status":"computing"} on a reframe cache miss; an OLD frontend does not understand
#    202 and would paint an empty shot strip.
$ErrorActionPreference = "Stop"
$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\user\Desktop\Quip"

$PROJECT = "quip-project-500711"
$REGION  = "europe-west1"
$IMAGE   = "$REGION-docker.pkg.dev/$PROJECT/quip/quip:latest"

# 1. Build + push the worker image.
gcloud builds submit services/worker --tag $IMAGE --project $PROJECT
if ($LASTEXITCODE -ne 0) { throw "build failed" }

# 2. Resolve the immutable digest and pin all three targets to it.
$DIGEST = (gcloud artifacts docker images describe $IMAGE --format="value(image_summary.digest)" --project $PROJECT)
$PINNED = "$REGION-docker.pkg.dev/$PROJECT/quip/quip@$DIGEST"
Write-Host "Pinned image: $PINNED"

gcloud run jobs update quip-pipeline --image=$PINNED --region=$REGION --project $PROJECT
gcloud run jobs update quip-render   --image=$PINNED --region=$REGION --project $PROJECT
gcloud run deploy quip-api           --image=$PINNED --region=$REGION --project $PROJECT

# 3. Verify the API is healthy. NOTE: /healthz has a GFE-404 quirk on Cloud Run — /openapi.json is
#    the reliable liveness probe (it is served by FastAPI itself).
$URL = (gcloud run services describe quip-api --region=$REGION --format="value(status.url)" --project $PROJECT)
Write-Host "API URL: $URL"
try {
    $r = Invoke-WebRequest "$URL/openapi.json" -UseBasicParsing -TimeoutSec 30
    Write-Host "openapi.json -> HTTP $($r.StatusCode)  (API alive)"
} catch {
    Write-Host "LIVENESS FAILED: $_"
    Write-Host "Roll back:  gcloud run services update-traffic quip-api --region=$REGION --project $PROJECT --to-revisions <PREV_REVISION>=100"
    throw "API is not healthy after deploy"
}

# 4. Frontend (aliases to quip.ink). MUST follow the worker — see the 202 contract note at the top.
vercel deploy --prod --scope timurkas-projects

# 5. ☠️ AFTER A REFRAME-LOGIC CHANGE: clearing the baked regions is NOT free — it REMOVES A SAFETY
#    FUSE. `update public.job_artifacts set reframe_regions = null;` makes EVERY editor open of EVERY
#    clip fall into a FULL CV recompute. That is exactly what caused the 2026-07-13 prod incident
#    (OOM / mass 503s); migration 0013 exists to PREVENT it. This script used to end by cheerfully
#    telling you to run it, with no mention of the consequence.
#    → If you clear the cache, RE-BAKE IMMEDIATELY (Cloud Run Job `rebake_regions`, clip_id=null =
#      whole job) and do NOT leave prod sitting with an empty cache.
#    Details: docs/archive/2026-07-14-api-oom-incident.md + the reframe rule in CLAUDE.md.
Write-Host ""
Write-Host "Reframe cache: DO NOT 'set reframe_regions = null' without immediately re-baking (Job 'rebake_regions')."
Write-Host "Doing so re-arms the OOM that took prod down on 2026-07-13. See docs/archive/2026-07-14-api-oom-incident.md"
