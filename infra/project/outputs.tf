output "project_id" {
  description = "Managed project ID."
  value       = google_project.project.project_id
}

output "project_number" {
  description = "Numeric identifier for the managed project."
  value       = google_project.project.number
}

output "enabled_services" {
  description = "APIs enabled on the project."
  value       = keys(google_project_service.services)
}

output "gke_node_service_account_email" {
  description = "Email address of the dedicated GKE node service account."
  value       = google_service_account.gke_nodes.email
}

output "workload_identity_pool_name" {
  description = "Fully-qualified name of the GitHub Actions Workload Identity Pool."
  value       = google_iam_workload_identity_pool.github.name
}

output "workload_identity_provider_name" {
  description = "Fully-qualified name of the GitHub Actions Workload Identity Provider."
  value       = google_iam_workload_identity_pool_provider.github.name
}
