locals {
  parent_references    = compact([var.org_id, var.folder_id])
  project_display_name = coalesce(var.project_name, var.project_id)
  gke_node_sa_roles = [
    "roles/monitoring.viewer",
    "roles/logging.logWriter",
    "roles/artifactregistry.reader",
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

data "google_service_account" "terraform" {
  project    = google_project.project.project_id
  account_id = var.terraform_service_account_id
}

resource "google_service_account" "gke_nodes" {
  project      = google_project.project.project_id
  account_id   = var.gke_nodes_service_account_id
  display_name = var.gke_nodes_service_account_display_name
}

resource "google_project_iam_member" "gke_node_sa_roles" {
  for_each = toset(local.gke_node_sa_roles)

  project = google_project.project.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_iam_workload_identity_pool" "github" {
  provider = google-beta

  project                   = google_project.project.project_id
  location                  = "global"
  workload_identity_pool_id = var.workload_identity_pool_id
  display_name              = var.workload_identity_pool_display_name
  description               = "Federated identity pool for GitHub Actions workflows."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  provider = google-beta

  project                            = google_project.project.project_id
  location                           = "global"
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = var.workload_identity_provider_id
  display_name                       = var.workload_identity_provider_display_name
  description                        = "OIDC provider configuration for GitHub Actions."

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  attribute_condition = "assertion.repository == '${var.github_repository}' && assertion.ref == 'refs/heads/${var.github_default_branch}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "terraform_wif" {
  service_account_id = data.google_service_account.terraform.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}
