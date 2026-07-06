# Quip PROD deploy (repeatable, run SUPERVISED). Deploys the CURRENT checkout (services/worker + apps/web).
# Prereqs: gcloud auth (akybaevtimur7, project quip-project-500711) + vercel auth (varenik-vkusny).
# NOTE: this deploys whatever branch is checked out. Merge feat/clip-quality-recall-and-editor-export
#       into main first if you want main to match prod.
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

# 3. Verify the API is healthy.
$URL = (gcloud run services describe quip-api --region=$REGION --format="value(status.url)" --project $PROJECT)
Write-Host "API URL: $URL"
try { (Invoke-WebRequest "$URL/healthz" -UseBasicParsing).Content | Write-Host } catch { Write-Host "healthz FAILED: $_" }

# 4. Frontend (aliases to quip.ink).
vercel deploy --prod --scope timurkas-projects

# 5. *** REQUIRED after any reframe-logic change *** clear baked reframe regions so the new
#    dead-zone follower / face-centering show on EXISTING clips (fresh clips get it automatically):
#    Supabase SQL editor -> run:  update public.job_artifacts set reframe_regions = null;
Write-Host "REMEMBER: run 'update public.job_artifacts set reframe_regions = null;' in Supabase SQL."
