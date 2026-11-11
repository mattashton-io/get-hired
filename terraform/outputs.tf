output "project_id" {
  description = "Deployed Google Cloud Project ID"
  value       = var.project_id
}

output "region" {
  description = "Deployed Google Cloud Region"
  value       = var.region
}

output "data_bucket_name" {
  description = "Cloud Storage bucket mounted at /app/data via GCS FUSE"
  value       = google_storage_bucket.data_bucket.name
}

output "artifact_registry_image" {
  description = "Full Artifact Registry container image URI"
  value       = local.container_image
}

output "cloud_run_job_name" {
  description = "Scale-to-zero Cloud Run v2 Job name"
  value       = google_cloud_run_v2_job.runner.name
}

output "scheduler_jobs" {
  description = "Configured Cloud Scheduler cron triggers"
  value = {
    for k, v in google_cloud_scheduler_job.triggers : k => {
      schedule  = v.schedule
      time_zone = v.time_zone
    }
  }
}

output "manual_execute_command" {
  description = "gcloud command to execute an on-demand dry-run test"
  value       = "gcloud run jobs execute ${google_cloud_run_v2_job.runner.name} --region=${var.region} --project=${var.project_id} --args=\"dry-run\""
}

