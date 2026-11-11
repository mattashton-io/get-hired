# Terraform Infrastructure for `get-hired` (Cloud Run Jobs + Cloud Scheduler)

Provisions the serverless, **scale-to-zero** Google Cloud infrastructure for `get-hired`:
- **Google Artifact Registry** (`get-hired-repo`) for container images
- **Google Cloud Storage** (`${project_id}-get-hired-data`) mounted via **GCS FUSE** to `/app/data` (persisting `applications_ledger.db`, compiled resumes, screenshots, and outbox items)
- **Google Secret Manager** (`get-hired-gemini-api-key`) storing `GEMINI_API_KEY` encrypted at rest
- **Least-Privilege Service Accounts** (`get-hired-job-runner` and `get-hired-scheduler`)
- **Cloud Run v2 Job** (`get-hired-runner`) that executes batch tasks and immediately scales to `0`
- **5 Cloud Scheduler Triggers** (`America/Chicago` CST):
  - `06:00 CST (Mon–Fri)` $\rightarrow$ `source`
  - `06:15 CST (Mon–Fri)` $\rightarrow$ `score`
  - `08:00–16:00 CST Hourly (Mon–Fri)` $\rightarrow$ `process-queue --max-items 2`
  - `*/15m 08:00–20:00 CST (Mon–Fri)` $\rightarrow$ `poll-hitl`
  - `18:00 CST (Mon–Fri)` $\rightarrow$ `digest`

---

## Usage (Dynamic Region & Project Resolution via `gcloud`)

```bash
# 1. Export Project & Region dynamically from gcloud config (no hardcoded region)
export TF_VAR_project_id="$(gcloud config get-value project)"
export TF_VAR_region="$(gcloud config get-value run/region 2>/dev/null | grep -v '(unset)' || gcloud config get-value compute/region 2>/dev/null | grep -v '(unset)' || gcloud config get-value compute/zone 2>/dev/null | sed 's/-[a-z]$//')"
export TF_VAR_gemini_api_key="$(grep '^GEMINI_API_KEY=' ../.env | cut -d= -f2-)"

# 2. Build & Push Container Image to Artifact Registry first (or after creating the repo)
cd ..
gcloud builds submit --tag "${TF_VAR_region}-docker.pkg.dev/${TF_VAR_project_id}/get-hired-repo/agent:latest" --project="${TF_VAR_project_id}" .

# 3. Initialize & Apply Terraform
cd terraform
terraform init
terraform plan
terraform apply
```

