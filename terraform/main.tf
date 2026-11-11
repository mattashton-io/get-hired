locals {
  required_services = toset([
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ])

  bucket_name     = "${var.project_id}-get-hired-data"
  container_image = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo_name}/agent:${var.image_tag}"
  job_run_uri     = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.runner.name}:run"

  schedules = {
    "get-hired-daily-source" = {
      cron        = "0 6 * * 1-5"
      description = "06:00 CST morning certified employer discovery (SBA 8a, SDVOSB, HUB, WBENC, NMSDC)"
      args        = ["source"]
    }
    "get-hired-daily-score" = {
      cron        = "15 6 * * 1-5"
      description = "06:15 CST morning requisition scraping and deterministic 0-100 scoring"
      args        = ["score"]
    }
    "get-hired-hourly-queue" = {
      cron        = "0 8-16 * * 1-5"
      description = "08:00-16:00 CST hourly application pipeline worker (tailor, verify, compile, apply/HITL)"
      args        = ["process-queue", "--max-items", "2"]
    }
    "get-hired-poll-hitl" = {
      cron        = "*/15 8-20 * * 1-5"
      description = "08:00-20:00 CST every-15m Gmail HITL approval reply poller"
      args        = ["poll-hitl"]
    }
    "get-hired-daily-digest" = {
      cron        = "0 18 * * 1-5"
      description = "18:00 CST End-of-Day HTML Daily Digest email"
      args        = ["digest"]
    }
  }
}

# ==============================================================================
# 1. Enable Required Google Cloud APIs
# ==============================================================================
resource "google_project_service" "services" {
  for_each           = local.required_services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ==============================================================================
# 2. Artifact Registry Docker Repository
# ==============================================================================
resource "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = var.artifact_repo_name
  description   = "Docker container repository for get-hired autonomous job agent"
  format        = "DOCKER"

  depends_on = [google_project_service.services["artifactregistry.googleapis.com"]]
}

# ==============================================================================
# 3. Cloud Storage Bucket (GCS FUSE Volume for SQLite Ledger & Compiled Resumes)
# ==============================================================================
resource "google_storage_bucket" "data_bucket" {
  name                        = local.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.services["storage.googleapis.com"]]
}

resource "google_storage_bucket_object" "master_resume_seed" {
  name         = "master_resume.json"
  bucket       = google_storage_bucket.data_bucket.name
  source       = "${path.module}/../data/master_resume.json"
  content_type = "application/json"

  lifecycle {
    ignore_changes = [content, source, detect_md5hash, md5hash]
  }
}

resource "google_storage_bucket_object" "target_companies_seed" {
  name         = "target_companies.json"
  bucket       = google_storage_bucket.data_bucket.name
  source       = "${path.module}/../data/target_companies.json"
  content_type = "application/json"

  lifecycle {
    ignore_changes = [content, source, detect_md5hash, md5hash]
  }
}

# ==============================================================================
# 4. Secret Manager for Gemini API Key
# ==============================================================================
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "get-hired-gemini-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "gemini_api_key_version" {
  secret      = google_secret_manager_secret.gemini_api_key.id
  secret_data = var.gemini_api_key
}

# ==============================================================================
# 5. Service Accounts & Least-Privilege IAM
# ==============================================================================
resource "google_service_account" "job_runner" {
  account_id   = "get-hired-job-runner"
  display_name = "Get Hired Cloud Run Job Execution Service Account"

  depends_on = [google_project_service.services["iam.googleapis.com"]]
}

resource "google_storage_bucket_iam_member" "runner_gcs_access" {
  bucket = google_storage_bucket.data_bucket.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.job_runner.email}"
}

resource "google_secret_manager_secret_iam_member" "runner_secret_access" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.job_runner.email}"
}

resource "google_service_account" "scheduler_invoker" {
  account_id   = "get-hired-scheduler"
  display_name = "Get Hired Cloud Scheduler Invoker Service Account"

  depends_on = [google_project_service.services["iam.googleapis.com"]]
}

resource "google_project_iam_member" "scheduler_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_job_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.runner.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

# ==============================================================================
# 6. Cloud Run v2 Job (Scale-to-Zero Batch Runner + GCS FUSE Volume Mount)
# ==============================================================================
resource "google_cloud_run_v2_job" "runner" {
  name                = var.job_name
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = google_service_account.job_runner.email
      max_retries     = 1
      timeout         = "900s"

      volumes {
        name = "gcs-data"
        gcs {
          bucket    = google_storage_bucket.data_bucket.name
          read_only = false
        }
      }

      containers {
        image = local.container_image

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        volume_mounts {
          name       = "gcs-data"
          mount_path = "/app/data"
        }

        env {
          name  = "GEMINI_MODEL"
          value = var.gemini_model
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "GOOGLE_CLOUD_REGION"
          value = var.region
        }
        env {
          name  = "CANDIDATE_EMAIL"
          value = var.candidate_email
        }
        env {
          name  = "DRY_RUN"
          value = var.dry_run
        }
        env {
          name  = "TZ"
          value = var.scheduler_timezone
        }
        env {
          name = "GEMINI_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.gemini_api_key.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.services["run.googleapis.com"],
    google_storage_bucket_iam_member.runner_gcs_access,
    google_secret_manager_secret_iam_member.runner_secret_access,
    google_secret_manager_secret_version.gemini_api_key_version,
  ]
}

# ==============================================================================
# 7. Cloud Scheduler Triggers (America/Chicago CST Business Schedule)
# ==============================================================================
resource "google_cloud_scheduler_job" "triggers" {
  for_each    = local.schedules
  name        = each.key
  region      = var.region
  description = each.value.description
  schedule    = each.value.cron
  time_zone   = var.scheduler_timezone

  http_target {
    http_method = "POST"
    uri         = local.job_run_uri

    headers = {
      "Content-Type" = "application/json"
    }

    body = base64encode(jsonencode({
      overrides = {
        containerOverrides = [
          {
            args = each.value.args
          }
        ]
      }
    }))

    oauth_token {
      service_account_email = google_service_account.scheduler_invoker.email
    }
  }

  depends_on = [
    google_project_service.services["cloudscheduler.googleapis.com"],
    google_cloud_run_v2_job_iam_member.scheduler_job_invoker,
    google_project_iam_member.scheduler_run_developer,
  ]
}

