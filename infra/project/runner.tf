locals {
  runner_startup_script_default = <<-EOT
    #!/bin/bash
    set -euxo pipefail

    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release

    # Install Google Cloud SDK
    install -d -m 755 /usr/share/keyrings
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/google-cloud-sdk.gpg
    echo "deb [signed-by=/usr/share/keyrings/google-cloud-sdk.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      | tee /etc/apt/sources.list.d/google-cloud-sdk.list

    # Install Kubernetes apt repo for kubectl
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/kubernetes-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/kubernetes-archive-keyring.gpg] https://apt.kubernetes.io/ kubernetes-xenial main" \
      | tee /etc/apt/sources.list.d/kubernetes.list

    apt-get update
    apt-get install -y google-cloud-cli kubectl docker.io

    systemctl enable --now docker

    if id -u ${var.runner_os_user} >/dev/null 2>&1; then
      usermod -aG docker ${var.runner_os_user}
    fi
  EOT
}

resource "google_service_account" "runner" {
  count        = var.enable_runner ? 1 : 0
  project      = google_project.project.project_id
  account_id   = var.runner_service_account_id
  display_name = var.runner_service_account_display_name
}

resource "google_project_iam_member" "runner_sa_roles" {
  for_each = var.enable_runner ? toset(local.runner_sa_roles) : toset([])

  project = google_project.project.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runner[0].email}"
}

resource "google_compute_instance" "runner" {
  count        = var.enable_runner ? 1 : 0
  name         = var.runner_instance_name
  project      = google_project.project.project_id
  zone         = var.runner_zone
  machine_type = var.runner_machine_type

  allow_stopping_for_update = true
  labels                    = var.runner_labels
  tags                      = var.runner_network_tags

  boot_disk {
    initialize_params {
      image = var.runner_boot_image
      size  = var.runner_disk_size_gb
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.primary.self_link

    dynamic "access_config" {
      for_each = var.runner_enable_public_ip ? [1] : []
      content {}
    }
  }

  metadata = var.runner_metadata

  metadata_startup_script = coalesce(var.runner_startup_script, local.runner_startup_script_default)

  service_account {
    email  = google_service_account.runner[0].email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  depends_on = [google_project_service.services]
}
