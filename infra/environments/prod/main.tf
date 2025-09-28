locals {
  parent_references    = compact([var.org_id, var.folder_id])
  project_display_name = coalesce(var.project_name, var.project_id)
  gke_node_sa_roles = [
    "roles/monitoring.viewer",
    "roles/logging.logWriter",
    "roles/artifactregistry.reader",
    "roles/datastore.user",
  ]
  runner_sa_roles = [
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/container.developer",
    "roles/artifactregistry.reader",
  ]
  liveon_app_sa_roles = [
    "roles/datastore.user",
    "roles/secretmanager.secretAccessor",
    "roles/monitoring.viewer",
  ]
}

resource "google_project" "project" {
  project_id      = var.project_id
  name            = local.project_display_name
  billing_account = var.billing_account_id
  org_id          = var.org_id
  folder_id       = var.folder_id
  skip_delete     = var.skip_project_deletion

  lifecycle {
    prevent_destroy = true

    #precondition {
    #condition     = length(local.parent_references) == 1
    #  error_message = "Exactly one of org_id or folder_id must be provided."
    #}
  }
}

resource "google_project_service" "services" {
  for_each = toset(var.activate_apis)

  project = google_project.project.project_id
  service = each.key

  disable_dependent_services = true
}

resource "google_firestore_database" "primary" {
  project     = google_project.project.project_id
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.services]
}

data "google_service_account" "terraform" {
  project    = google_project.project.project_id
  account_id = var.terraform_service_account_id
}

resource "google_service_account" "gke_nodes" {
  project      = google_project.project.project_id
  account_id   = var.gke_nodes_service_account_id
  display_name = var.gke_nodes_service_account_display_name
}

resource "google_service_account" "liveon_app" {
  count = var.create_liveon_app_service_account ? 1 : 0

  project      = google_project.project.project_id
  account_id   = var.liveon_app_service_account_id
  display_name = var.liveon_app_service_account_display_name
}

data "google_service_account" "liveon_app" {
  count = var.create_liveon_app_service_account ? 0 : 1

  project    = google_project.project.project_id
  account_id = var.liveon_app_service_account_id
}

locals {
  liveon_app_service_account_email = var.create_liveon_app_service_account ? google_service_account.liveon_app[0].email : data.google_service_account.liveon_app[0].email
}

resource "google_project_iam_member" "gke_node_sa_roles" {
  for_each = toset(local.gke_node_sa_roles)

  project = google_project.project.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "liveon_app_sa_roles" {
  for_each = toset(local.liveon_app_sa_roles)

  project = google_project.project.project_id
  role    = each.value
  member  = "serviceAccount:${local.liveon_app_service_account_email}"
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = google_project.project.project_id
  workload_identity_pool_id = var.workload_identity_pool_id # ex: "github-actions-pool"
  display_name              = var.workload_identity_pool_display_name
  description               = "Federated identity pool for GitHub Actions workflows."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = google_project.project.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = var.workload_identity_provider_id # ex: "github-provider"
  display_name                       = var.workload_identity_provider_display_name
  description                        = "OIDC provider configuration for GitHub Actions."

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  # Mappar claims -> attribut
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Begränsa till repo + default branch (använder mappade attribut)
  attribute_condition = "attribute.repository == \"${var.github_repository}\" && attribute.ref == \"refs/heads/${var.github_default_branch}\""
}

# Tillåt WIF att agera som Terraform-SA:t
resource "google_service_account_iam_member" "terraform_wif" {
  service_account_id = data.google_service_account.terraform.name
  role               = "roles/iam.workloadIdentityUser"

  # principalSet som pekar på poolen, filtrerad på repo-attribut
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}
