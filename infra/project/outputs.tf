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

output "vpc_network_name" {
  description = "Name of the dedicated VPC network hosting the platform."
  value       = google_compute_network.primary.name
}

output "gke_subnetwork_self_link" {
  description = "Self-link of the regional subnetwork allocated to the GKE cluster."
  value       = google_compute_subnetwork.primary.self_link
}

output "gke_secondary_ip_ranges" {
  description = "Secondary IP ranges dedicated to Pods and Services within the subnetwork."
  value = {
    pods     = { name = google_compute_subnetwork.primary.secondary_ip_range[0].range_name, cidr = google_compute_subnetwork.primary.secondary_ip_range[0].ip_cidr_range }
    services = { name = google_compute_subnetwork.primary.secondary_ip_range[1].range_name, cidr = google_compute_subnetwork.primary.secondary_ip_range[1].ip_cidr_range }
  }
}
