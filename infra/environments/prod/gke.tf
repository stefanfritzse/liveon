############################################
# GKE cluster (Autopilot) – behåller /32-IP
############################################

locals {
  # IP:en du vill behålla oavsett vad som ligger i var.gke_master_authorized_networks
  pinned_authorized_networks = [
    {
      name       = "gha-runner"
      cidr_block = "34.51.203.221/32"
    }
  ]

  # Slå ihop användarens nät + pinned och dedupla på cidr_block
  effective_authorized_networks_map = {
    for n in concat(var.gke_master_authorized_networks, local.pinned_authorized_networks) :
    n.cidr_block => n
  }

  # Stabil, sorterad lista (sorterar på CIDR-nyckeln)
  effective_authorized_networks_sorted = [
    for cidr in sort(keys(local.effective_authorized_networks_map)) :
    local.effective_authorized_networks_map[cidr]
  ]
}

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

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = var.gke_enable_private_endpoint
    master_ipv4_cidr_block  = var.gke_master_ipv4_cidr_block
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pod_ip_range_name
    services_secondary_range_name = var.service_ip_range_name
  }

  # Rendera MAN-blocket endast om det finns minst ett nät
  dynamic "master_authorized_networks_config" {
    for_each = length(local.effective_authorized_networks_sorted) > 0 ? [local.effective_authorized_networks_sorted] : []
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
