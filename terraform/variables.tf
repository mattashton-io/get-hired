variable "project_id" {
  description = "Google Cloud Project ID (e.g., resolved via `gcloud config get-value project`)."
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run Jobs, Cloud Scheduler, Artifact Registry, and GCS (e.g., resolved via `gcloud config get-value run/region` or `compute/region`)."
  type        = string
}

variable "gemini_api_key" {
  description = "Gemini API key stored in Google Cloud Secret Manager and mounted into the Cloud Run Job."
  type        = string
  sensitive   = true
}

variable "gemini_model" {
  description = "Default Gemini Flash model used by the agent pipeline."
  type        = string
  default     = "gemini-3.8-flash"
}

variable "candidate_email" {
  description = "Candidate Gmail address for HITL approvals and EOD Daily Digests."
  type        = string
  default     = "Erykasmith@gmail.com"
}

variable "scheduler_timezone" {
  description = "IANA timezone for Cloud Scheduler triggers."
  type        = string
  default     = "America/Chicago"
}

variable "artifact_repo_name" {
  description = "Artifact Registry Docker repository ID."
  type        = string
  default     = "get-hired-repo"
}

variable "image_tag" {
  description = "Docker image tag inside Artifact Registry."
  type        = string
  default     = "latest"
}

variable "job_name" {
  description = "Name of the scale-to-zero Cloud Run v2 Job."
  type        = string
  default     = "get-hired-runner"
}

variable "dry_run" {
  description = "Whether the Cloud Run Job executes in dry-run mode ('true' or 'false')."
  type        = string
  default     = "false"
}

variable "cpu" {
  description = "CPU limit allocated per Cloud Run Job execution."
  type        = string
  default     = "1000m"
}

variable "memory" {
  description = "Memory limit allocated per Cloud Run Job execution."
  type        = string
  default     = "2Gi"
}

