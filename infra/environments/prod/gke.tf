resource "google_container_cluster" "primary" {
  provider            = google-beta
  name                = var.gke_cluster_name
  location            = var.default_region
  project             = google_project.project.project_id
  description         = "Autopilot GKE cluster for the Longevity Coach platform."
  enable_autopilot    = true
  networking_mode     = "VPC_NATIVE"
  deletion_protection = false

  network    = google_compute_network.primary.id
  subnetwork = google_compute_subnetwork.primary.id

  release_channel { channel = "REGULAR" }

  node_config_defaults {
    service_account = google_service_account.gke_nodes.email
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = var.gke_enable_private_endpoint
    master_ipv4_cidr_block  = var.gke_master_ipv4_cidr_block
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pod_ip_range_name
    services_secondary_range_name = var.service_ip_range_name
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.gke_master_authorized_networks) > 0 ? [var.gke_master_authorized_networks] : []
    content {
      dynamic "cidr_blocks" {
        for_each = master_authorized_networks_config.value
        content {
          display_name = cidr_blocks.value.name
          cidr_block   = cidr_blocks.value.cidr_block
        }
      }
    }
  }

  master_auth {
    client_certificate_config { issue_client_certificate = false }
  }

  workload_identity_config {
    workload_pool = "${google_project.project.project_id}.svc.id.goog"
  }

  depends_on = [google_project_service.services]
}
