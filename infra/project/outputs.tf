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
