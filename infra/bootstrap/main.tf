locals {
  terraform_service_account_roles = [
    "roles/resourcemanager.projectIamAdmin",
    "roles/container.admin",
    "roles/artifactregistry.admin",
    "roles/iam.serviceAccountUser",
    "roles/storage.admin",
  ]
}

resource "google_storage_bucket" "terraform_state" {
  name                        = var.state_bucket_name
  location                    = var.state_bucket_location
  uniform_bucket_level_access = true
  force_destroy               = false
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }
}

resource "google_service_account" "terraform" {
  account_id   = var.terraform_service_account_id
  display_name = var.terraform_service_account_display_name
}

resource "google_project_iam_member" "terraform_sa_roles" {
  for_each = toset(local.terraform_service_account_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.terraform.email}"
}
