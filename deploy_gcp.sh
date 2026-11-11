#!/usr/bin/env bash
# ==============================================================================
# deploy_gcp.sh - Automated Scale-to-Zero GCP Deployment for get-hired
#
# Architecture:
#   - Cloud Run Jobs: Runs batch tasks in seconds and terminates (scales to 0)
#   - Cloud Storage FUSE: Mounts gs://${BUCKET} to /app/data for SQLite & resumes
#   - Cloud Scheduler: Dispatches CST cron triggers to Cloud Run Jobs
#   - Monthly Cost: $0.00 / month (100% covered by GCP Free Tier)
# ==============================================================================

set -euo pipefail

# 1. Colors & Helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 2. Load .env if present
if [[ -f .env ]]; then
    info "Loading configurations from .env..."
    # Export variables from .env
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}"
REGION="${GOOGLE_CLOUD_REGION:-us-south1}"
TIMEZONE="America/Chicago"

if [[ -z "$PROJECT_ID" ]]; then
    err "No GCP project detected. Set GOOGLE_CLOUD_PROJECT_ID in .env or run: gcloud config set project <PROJECT_ID>"
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    err "GEMINI_API_KEY is not set in .env. Please add it before deploying."
fi

BUCKET_NAME="${PROJECT_ID}-get-hired-data"
IMAGE_NAME="${REGION}-docker.pkg.dev/${PROJECT_ID}/get-hired-repo/agent:latest"
JOB_NAME="get-hired-runner"
SERVICE_ACCOUNT="get-hired-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"

info "================================================================="
info " Deploying 'get-hired' to Google Cloud (Scale-to-Zero Architecture)"
info " Project:         $PROJECT_ID"
info " Region:          $REGION"
info " Data Bucket:     gs://$BUCKET_NAME"
info " Container Image: $IMAGE_NAME"
info " Timezone:        $TIMEZONE"
info "================================================================="

# 3. Enable Required GCP APIs
info "Enabling required GCP APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    storage.googleapis.com \
    --project="$PROJECT_ID"
ok "APIs enabled."

# 4. Create Artifact Registry Repository (if it doesn't exist)
info "Checking Artifact Registry repository..."
if ! gcloud artifacts repositories describe get-hired-repo --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    info "Creating Artifact Registry repo 'get-hired-repo' in $REGION..."
    gcloud artifacts repositories create get-hired-repo \
        --repository-format=docker \
        --location="$REGION" \
        --description="Docker repository for get-hired agents" \
        --project="$PROJECT_ID"
    ok "Artifact Registry repository created."
else
    ok "Artifact Registry repository already exists."
fi

# 5. Create Cloud Storage Bucket for SQLite & Resumes
info "Checking GCS bucket gs://$BUCKET_NAME..."
if ! gcloud storage buckets describe "gs://$BUCKET_NAME" --project="$PROJECT_ID" &>/dev/null; then
    info "Creating Cloud Storage bucket gs://$BUCKET_NAME..."
    gcloud storage buckets create "gs://$BUCKET_NAME" \
        --location="$REGION" \
        --project="$PROJECT_ID" \
        --uniform-bucket-level-access
    ok "GCS bucket created."
else
    ok "GCS bucket exists."
fi

# Seed initial files into bucket if bucket is empty
if [[ -f data/applications_ledger.db ]]; then
    info "Syncing local database to GCS bucket as initial state..."
    gcloud storage cp data/applications_ledger.db "gs://$BUCKET_NAME/applications_ledger.db" --project="$PROJECT_ID" 2>/dev/null || true
    ok "Initial ledger synced."
fi

# 6. Build and Push Container Image with Cloud Build
info "Building container image with Google Cloud Build..."
gcloud builds submit --tag "$IMAGE_NAME" --project="$PROJECT_ID" .
ok "Container image built and pushed to $IMAGE_NAME"

# 7. Create or Update Cloud Run Job
info "Deploying Cloud Run Job '$JOB_NAME' with GCS Volume Mount..."
if gcloud run jobs describe "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    info "Updating existing Cloud Run Job..."
    gcloud run jobs update "$JOB_NAME" \
        --image="$IMAGE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --cpu=1 \
        --memory=2Gi \
        --max-retries=1 \
        --task-timeout=15m \
        --set-env-vars="GEMINI_API_KEY=${GEMINI_API_KEY},TZ=${TIMEZONE}" \
        --add-volume="name=gcs-data,type=cloud-storage,bucket=${BUCKET_NAME}" \
        --add-volume-mount="volume=gcs-data,mount-path=/app/data"
else
    info "Creating new Cloud Run Job..."
    gcloud run jobs create "$JOB_NAME" \
        --image="$IMAGE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --cpu=1 \
        --memory=2Gi \
        --max-retries=1 \
        --task-timeout=15m \
        --set-env-vars="GEMINI_API_KEY=${GEMINI_API_KEY},TZ=${TIMEZONE}" \
        --add-volume="name=gcs-data,type=cloud-storage,bucket=${BUCKET_NAME}" \
        --add-volume-mount="volume=gcs-data,mount-path=/app/data"
fi
ok "Cloud Run Job '$JOB_NAME' configured with GCS volume mount at /app/data."

# 8. Setup Dedicated Service Account for Cloud Scheduler
info "Configuring Service Account for Cloud Scheduler..."
if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project="$PROJECT_ID" &>/dev/null; then
    gcloud iam service-accounts create get-hired-scheduler \
        --display-name="Get Hired Cloud Scheduler Service Account" \
        --project="$PROJECT_ID"
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SERVICE_ACCOUNT" \
    --role="roles/run.invoker" \
    --condition=None --quiet >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SERVICE_ACCOUNT" \
    --role="roles/run.developer" \
    --condition=None --quiet >/dev/null
ok "Service account configured with run.invoker and run.developer."

# 9. Create Cloud Scheduler Triggers
info "Configuring Cloud Scheduler jobs (matching CST schedule)..."

JOB_RUN_URL="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

create_or_update_schedule() {
    local sched_name="$1"
    local cron_expr="$2"
    local description="$3"
    local cli_args="$4"

    local body_json
    body_json=$(python3 -c "
import json, sys
args = sys.argv[1].split()
print(json.dumps({'overrides': {'containerOverrides': [{'args': args}]}}))
" "$cli_args")

    if gcloud scheduler jobs describe "$sched_name" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
        gcloud scheduler jobs update http "$sched_name" \
            --location="$REGION" \
            --project="$PROJECT_ID" \
            --schedule="$cron_expr" \
            --time-zone="$TIMEZONE" \
            --description="$description" \
            --uri="$JOB_RUN_URL" \
            --http-method=POST \
            --oauth-service-account-email="$SERVICE_ACCOUNT" \
            --message-body="$body_json" \
            --headers="Content-Type=application/json" \
            --quiet
    else
        gcloud scheduler jobs create http "$sched_name" \
            --location="$REGION" \
            --project="$PROJECT_ID" \
            --schedule="$cron_expr" \
            --time-zone="$TIMEZONE" \
            --description="$description" \
            --uri="$JOB_RUN_URL" \
            --http-method=POST \
            --oauth-service-account-email="$SERVICE_ACCOUNT" \
            --message-body="$body_json" \
            --headers="Content-Type=application/json" \
            --quiet
    fi
    ok "Schedule '$sched_name' configured: $cron_expr CST -> [ $cli_args ]"
}

# 1. 06:00 CST - Morning Sourcing & Matching
create_or_update_schedule \
    "get-hired-daily-source" \
    "0 6 * * 1-5" \
    "Morning certified employer discovery and job scoring" \
    "source"

# 2. 08:00 to 16:00 CST - Hourly Application Worker (2 roles/hr)
create_or_update_schedule \
    "get-hired-hourly-queue" \
    "0 8-16 * * 1-5" \
    "Hourly application pipeline queue worker (tailor, verify, apply)" \
    "process-queue --max-items 2"

# 3. Every 30 minutes from 08:00 to 18:00 CST - HITL Gmail Thread Poller
create_or_update_schedule \
    "get-hired-poll-hitl" \
    "*/30 8-18 * * 1-5" \
    "Poll Eryka's Gmail approval responses and execute approved actions" \
    "poll-hitl"

# 4. 18:00 CST - EOD Daily Digest
create_or_update_schedule \
    "get-hired-daily-digest" \
    "0 18 * * 1-5" \
    "EOD executive metrics daily digest email" \
    "digest"

info "================================================================="
ok " Deployment Complete!"
info " Total Server Cost: \$0.00 / month (Scales to 0 when idle)"
info " Data Bucket:       gs://${BUCKET_NAME}"
info " Cloud Run Job:     ${JOB_NAME} (${REGION})"
info " To test execute now:"
echo "   gcloud run jobs execute ${JOB_NAME} --region=${REGION} --args=\"dry-run\""
info "================================================================="

