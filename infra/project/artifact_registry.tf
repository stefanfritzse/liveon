resource "google_artifact_registry_repository" "app" {
  project       = google_project.project.project_id
  location      = var.artifact_registry_location
  repository_id = var.artifact_registry_repository_id
  description   = var.artifact_registry_repository_description
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }
}
