resource "google_compute_network" "primary" {
  name                    = var.network_name
  project                 = google_project.project.project_id
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  description             = "Primary VPC network for the Longevity Coach platform."
}

resource "google_compute_subnetwork" "primary" {
  name          = var.subnetwork_name
  project       = google_project.project.project_id
  region        = var.default_region
  network       = google_compute_network.primary.id
  ip_cidr_range = var.subnetwork_cidr_range

  private_ip_google_access = true

  secondary_ip_range {
    range_name    = var.pod_ip_range_name
    ip_cidr_range = var.pod_ip_cidr_range
  }

  secondary_ip_range {
    range_name    = var.service_ip_range_name
    ip_cidr_range = var.service_ip_cidr_range
  }
}
