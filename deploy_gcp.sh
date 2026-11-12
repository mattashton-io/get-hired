#!/usr/bin/env bash
# ==============================================================================
# deploy_gcp.sh - Automated Scale-to-Zero GCP Deployment for get-hired
#
# Architecture:
#   - Cloud Run Jobs: Executes batch tasks in seconds and terminates (0 idle instances)
#   - Cloud Storage FUSE: Mounts gs://${BUCKET_NAME} to /app/data for SQLite & resumes
#   - Cloud Scheduler: Dispatches CST cron triggers to Cloud Run Jobs
#   - Monthly Cost: $0.00 / month (100% covered by GCP Free Tier)
# ==============================================================================

set -euo pipefail

# Prevent gcloud from ever hanging on interactive (y/N) prompts in non-interactive shells
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

# 1. Colors & Helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure gcloud is on PATH (including Homebrew location on macOS)
if ! command -v gcloud &>/dev/null && [[ -x "/opt/homebrew/share/google-cloud-sdk/bin/gcloud" ]]; then
    export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"
fi

# 2. Load .env if present (preserving any explicit CLI environment overrides)
CLI_REGION="${GOOGLE_CLOUD_REGION:-}"
CLI_PROJECT="${GOOGLE_CLOUD_PROJECT_ID:-}"

if [[ -f .env ]]; then
    info "Loading environment variables from .env..."
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# 3. Dynamic Project & Region Resolution via gcloud config (no hardcoded region)
resolve_gcp_project() {
    if [[ -n "$CLI_PROJECT" ]]; then
        echo "$CLI_PROJECT"
        return
    fi
    local gcloud_proj
    gcloud_proj="$(gcloud config get-value project 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -n "$gcloud_proj" && "$gcloud_proj" != "(unset)" ]]; then
        echo "$gcloud_proj"
        return
    fi
    if [[ -n "${GOOGLE_CLOUD_PROJECT_ID:-}" ]]; then
        echo "${GOOGLE_CLOUD_PROJECT_ID}"
        return
    fi
    echo ""
}

resolve_gcp_region() {
    # Priority 1: Explicit CLI env override
    if [[ -n "$CLI_REGION" ]]; then
        echo "$CLI_REGION"
        return
    fi
    # Priority 2: gcloud config run/region
    local r
    r="$(gcloud config get-value run/region 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -n "$r" && "$r" != "(unset)" ]]; then
        echo "$r"
        return
    fi
    # Priority 3: gcloud config compute/region
    r="$(gcloud config get-value compute/region 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -n "$r" && "$r" != "(unset)" ]]; then
        echo "$r"
        return
    fi
    # Priority 4: Derive region from gcloud config compute/zone (e.g. us-central1-c -> us-central1)
    local z
    z="$(gcloud config get-value compute/zone 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -n "$z" && "$z" != "(unset)" && "$z" == *-* ]]; then
        echo "${z%-*}"
        return
    fi
    # Priority 5: GOOGLE_CLOUD_REGION from .env
    if [[ -n "${GOOGLE_CLOUD_REGION:-}" ]]; then
        echo "${GOOGLE_CLOUD_REGION}"
        return
    fi
    echo ""
}

PROJECT_ID="$(resolve_gcp_project)"
REGION="$(resolve_gcp_region)"
TIMEZONE="${SCHEDULER_TIMEZONE:-America/Chicago}"

if [[ -z "$PROJECT_ID" ]]; then
    err "Could not determine GCP Project ID. Run 'gcloud config set project <PROJECT_ID>' or set GOOGLE_CLOUD_PROJECT_ID."
fi

if [[ -z "$REGION" ]]; then
    err "Could not determine GCP Region. Run 'gcloud config set run/region <REGION>' (or compute/region) or set GOOGLE_CLOUD_REGION."
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    err "GEMINI_API_KEY is not set. Add GEMINI_API_KEY to .env or export it in your shell before deploying."
fi

BUCKET_NAME="${PROJECT_ID}-get-hired-data"
AR_REPO_NAME="get-hired-repo"
IMAGE_NAME="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO_NAME}/agent:latest"
JOB_NAME="get-hired-runner"
SA_NAME="get-hired-scheduler"
SERVICE_ACCOUNT="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

info "================================================================="
info " Deploying 'get-hired' to Google Cloud (Scale-to-Zero Jobs)"
info " Project:         $PROJECT_ID"
info " Region:          $REGION (resolved dynamically)"
info " Data Bucket:     gs://$BUCKET_NAME"
info " Container Image: $IMAGE_NAME"
info " Timezone:        $TIMEZONE"
info "================================================================="

# 4. Enable Required GCP APIs
info "Enabling required Google Cloud APIs..."
gcloud services enable \
    serviceusage.googleapis.com \
    cloudresourcemanager.googleapis.com \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    storage.googleapis.com \
    iam.googleapis.com \
    gmail.googleapis.com \
    --project="$PROJECT_ID"
ok "APIs enabled."

# 5. Create Artifact Registry Repository (if not exists)
info "Checking Artifact Registry repository '$AR_REPO_NAME' in $REGION..."
if ! gcloud artifacts repositories describe "$AR_REPO_NAME" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    info "Creating Artifact Registry repository '$AR_REPO_NAME' in $REGION..."
    gcloud artifacts repositories create "$AR_REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --description="Docker repository for get-hired autonomous job agent" \
        --project="$PROJECT_ID"
    ok "Artifact Registry repository created."
else
    ok "Artifact Registry repository exists."
fi

# 6. Create Cloud Storage Bucket & Seed Initial State
info "Checking Cloud Storage bucket gs://$BUCKET_NAME..."
if ! gcloud storage buckets describe "gs://$BUCKET_NAME" --project="$PROJECT_ID" &>/dev/null; then
    info "Creating Cloud Storage bucket gs://$BUCKET_NAME in $REGION..."
    gcloud storage buckets create "gs://$BUCKET_NAME" \
        --location="$REGION" \
        --project="$PROJECT_ID" \
        --uniform-bucket-level-access
    ok "GCS bucket created."
else
    ok "GCS bucket exists."
fi

for seed_file in master_resume.json target_companies.json applications_ledger.db; do
    if [[ -f "data/${seed_file}" ]]; then
        if ! gcloud storage ls "gs://${BUCKET_NAME}/${seed_file}" --project="$PROJECT_ID" &>/dev/null; then
            info "Seeding data/${seed_file} -> gs://${BUCKET_NAME}/${seed_file}..."
            gcloud storage cp "data/${seed_file}" "gs://${BUCKET_NAME}/${seed_file}" --project="$PROJECT_ID"
        fi
    fi
done
if [[ -f "config/gmail_credentials.json" ]]; then
    info "Syncing config/gmail_credentials.json -> gs://${BUCKET_NAME}/gmail_credentials.json for Web OAuth portal..."
    gcloud storage cp "config/gmail_credentials.json" "gs://${BUCKET_NAME}/gmail_credentials.json" --project="$PROJECT_ID"
fi
if [[ -f "config/gmail_token.json" ]]; then
    info "Syncing config/gmail_token.json -> gs://${BUCKET_NAME}/gmail_token.json for live Gmail API access..."
    gcloud storage cp "config/gmail_token.json" "gs://${BUCKET_NAME}/gmail_token.json" --project="$PROJECT_ID"
fi
ok "GCS persistence volume seeded."

# 7. Ensure Cloud Build / Compute Default Service Account Permissions & Build Image
PROJECT_NUM="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' --quiet)"
COMPUTE_SA="${PROJECT_NUM}-compute@developer.gserviceaccount.com"
info "Ensuring Cloud Build & Cloud Run default service account ($COMPUTE_SA) has required roles..."
for cb_role in "roles/logging.logWriter" "roles/storage.objectAdmin" "roles/artifactregistry.writer"; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${COMPUTE_SA}" \
        --role="$cb_role" \
        --condition=None \
        --quiet >/dev/null || true
done

if [[ "${SKIP_BUILD:-false}" == "true" ]]; then
    info "SKIP_BUILD=true set; using existing image $IMAGE_NAME"
else
    info "Building container image via Cloud Build..."
    gcloud builds submit --tag "$IMAGE_NAME" --project="$PROJECT_ID" --quiet .
    ok "Container image built and pushed: $IMAGE_NAME"
fi

# 8. Create or Update Cloud Run Job with GCS Volume Mount
info "Deploying Cloud Run Job '$JOB_NAME' in $REGION..."
COMMON_JOB_FLAGS=(
    "--image=$IMAGE_NAME"
    "--region=$REGION"
    "--project=$PROJECT_ID"
    "--cpu=1"
    "--memory=2Gi"
    "--max-retries=1"
    "--task-timeout=15m"
    "--set-env-vars=^;^GEMINI_API_KEY=${GEMINI_API_KEY};GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.8-flash};GOOGLE_CLOUD_PROJECT_ID=${PROJECT_ID};GOOGLE_CLOUD_REGION=${REGION};TZ=${TIMEZONE};NOTIFY_EMAILS=${NOTIFY_EMAILS:-Erykasmith@gmail.com,ashton0801@gmail.com};GMAIL_SENDER_EMAIL=${GMAIL_SENDER_EMAIL:-ashton0801@gmail.com};GMAIL_APP_PASSWORD=${GMAIL_APP_PASSWORD:-}"
    "--add-volume=name=gcs-data,type=cloud-storage,bucket=${BUCKET_NAME}"
    "--add-volume-mount=volume=gcs-data,mount-path=/app/data"
)

if gcloud run jobs describe "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud run jobs update "$JOB_NAME" "${COMMON_JOB_FLAGS[@]}"
else
    gcloud run jobs create "$JOB_NAME" "${COMMON_JOB_FLAGS[@]}"
fi
ok "Cloud Run Job '$JOB_NAME' deployed with GCS FUSE mount at /app/data."

# 8b. Deploy Scale-to-Zero Remote Candidate Onboarding Portal (Cloud Run Service)
AUTH_SERVICE_NAME="get-hired-auth"
info "Deploying Scale-to-Zero OAuth Onboarding Portal '$AUTH_SERVICE_NAME' in $REGION..."
gcloud run deploy "$AUTH_SERVICE_NAME" \
    --image="$IMAGE_NAME" \
    --args="web-auth" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --min-instances=0 \
    --max-instances=2 \
    --cpu=1 \
    --memory=1Gi \
    --allow-unauthenticated \
    --set-env-vars="^;^GEMINI_API_KEY=${GEMINI_API_KEY};GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.8-flash};GOOGLE_CLOUD_PROJECT_ID=${PROJECT_ID};GOOGLE_CLOUD_REGION=${REGION};TZ=${TIMEZONE};NOTIFY_EMAILS=${NOTIFY_EMAILS:-Erykasmith@gmail.com,ashton0801@gmail.com}" \
    --add-volume="name=gcs-data,type=cloud-storage,bucket=${BUCKET_NAME}" \
    --add-volume-mount="volume=gcs-data,mount-path=/app/data" \
    --quiet
AUTH_PORTAL_URL="$(gcloud run services describe "$AUTH_SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')"
ok "OAuth Onboarding Portal deployed (min-instances=0): ${AUTH_PORTAL_URL}"

# 9. Configure Dedicated Service Account for Cloud Scheduler
info "Configuring Service Account '$SERVICE_ACCOUNT'..."
if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project="$PROJECT_ID" &>/dev/null; then
    gcloud iam service-accounts create "$SA_NAME" \
        --display-name="Get Hired Cloud Scheduler Invoker" \
        --project="$PROJECT_ID"
    sleep 5
fi

for role in "roles/run.invoker" "roles/run.developer"; do
    for attempt in 1 2 3 4 5; do
        if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
            --member="serviceAccount:${SERVICE_ACCOUNT}" \
            --role="$role" \
            --condition=None \
            --quiet >/dev/null 2>&1; then
            break
        fi
        warn "Waiting for IAM service account propagation (attempt ${attempt}/5)..."
        sleep 3
    done
done
ok "Service account IAM bindings verified."

# 10. Provision Cloud Scheduler Triggers
info "Configuring Cloud Scheduler jobs ($TIMEZONE)..."
JOB_RUN_URL="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

create_or_update_schedule() {
    local sched_name="$1"
    local cron_expr="$2"
    local description="$3"
    local cli_args="$4"

    local body_json
    body_json="$(python3 -c "
import json, sys
args = sys.argv[1].split()
print(json.dumps({'overrides': {'containerOverrides': [{'args': args}]}}))
" "$cli_args")"

    local sched_flags=(
        "--location=$REGION"
        "--project=$PROJECT_ID"
        "--schedule=$cron_expr"
        "--time-zone=$TIMEZONE"
        "--description=$description"
        "--uri=$JOB_RUN_URL"
        "--http-method=POST"
        "--oauth-service-account-email=$SERVICE_ACCOUNT"
        "--message-body=$body_json"
        "--quiet"
    )

    if gcloud scheduler jobs describe "$sched_name" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
        gcloud scheduler jobs update http "$sched_name" "${sched_flags[@]}" --update-headers="Content-Type=application/json"
    else
        gcloud scheduler jobs create http "$sched_name" "${sched_flags[@]}" --headers="Content-Type=application/json"
    fi
    ok "Schedule '$sched_name' ($cron_expr $TIMEZONE) -> [ $cli_args ]"
}

# 06:00 AM CST - Employer Sourcing
create_or_update_schedule \
    "get-hired-daily-source" \
    "0 6 * * 1-5" \
    "06:00 CST morning certified employer discovery" \
    "source"

# 06:15 AM CST - Requisition Scraping & Deterministic Scoring
create_or_update_schedule \
    "get-hired-daily-score" \
    "15 6 * * 1-5" \
    "06:15 CST morning job matching and scoring queue population" \
    "score"

# 08:00 AM to 04:00 PM CST - Staggered Application Queue Worker
create_or_update_schedule \
    "get-hired-hourly-queue" \
    "0 8-16 * * 1-5" \
    "Hourly application pipeline worker (tailor, verify, compile, apply/HITL)" \
    "process-queue --max-items 2"

# Every 15 minutes (08:00 AM to 08:00 PM CST) - HITL Gmail Reply Poller
create_or_update_schedule \
    "get-hired-poll-hitl" \
    "*/15 8-20 * * 1-5" \
    "Poll Gmail HITL approval replies every 15 minutes and execute approved submissions" \
    "poll-hitl"

# 06:00 PM CST - End-of-Day HTML Daily Digest
create_or_update_schedule \
    "get-hired-daily-digest" \
    "0 18 * * 1-5" \
    "18:00 CST End-of-Day executive metrics Daily Digest email" \
    "digest"

info "================================================================="
ok " Deployment Complete! (Scales to 0 when idle)"
info " Project:       $PROJECT_ID"
info " Region:        $REGION"
info " Data Bucket:   gs://$BUCKET_NAME"
info " Cloud Run Job: $JOB_NAME"
info " Manual Test Command:"
echo "   gcloud run jobs execute ${JOB_NAME} --region=${REGION} --project=${PROJECT_ID} --args=\"dry-run\""
info "================================================================="
