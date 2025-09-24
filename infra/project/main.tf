locals {
  parent_references = compact([var.org_id, var.folder_id])
  project_display_name = coalesce(var.project_name, var.project_id)
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
