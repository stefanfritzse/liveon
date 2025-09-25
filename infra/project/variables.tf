variable "project_id" {
  description = "ID of the Google Cloud project to manage."
  type        = string
}

variable "project_name" {
  description = "Display name for the project. Defaults to the project ID."
  type        = string
  default     = null
}

variable "billing_account_id" {
  description = "Billing account to associate with the project (format: XXXXXX-XXXXXX-XXXXXX)."
  type        = string
}

variable "org_id" {
  description = "Optional organization ID that owns the project. Set this or folder_id."
  type        = string
  default     = null
  nullable    = true
}

variable "folder_id" {
  description = "Optional folder ID that owns the project. Set this or org_id."
  type        = string
  default     = null
  nullable    = true
}

variable "default_region" {
  description = "Default region used by the Google provider."
  type        = string
  default     = "europe-north2"
}

variable "skip_project_deletion" {
  description = "Prevent Terraform from deleting the project."
  type        = bool
  default     = true
}

variable "activate_apis" {
  description = "List of Google APIs to activate on the project."
  type        = list(string)
  default = [
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com"
  ]
}

variable "terraform_service_account_id" {
  description = "Account ID of the Terraform service account created during bootstrap."
  type        = string
  default     = "sa-terraform"
}

variable "gke_nodes_service_account_id" {
  description = "Account ID to assign to the dedicated GKE node service account."
  type        = string
  default     = "sa-gke-nodes"
}

variable "gke_nodes_service_account_display_name" {
  description = "Display name for the dedicated GKE node service account."
  type        = string
  default     = "GKE Node Service Account"
}

variable "workload_identity_pool_id" {
  description = "Identifier to use for the GitHub Actions Workload Identity Pool."
  type        = string
  default     = "github-actions"
}

variable "workload_identity_pool_display_name" {
  description = "Display name for the Workload Identity Pool."
  type        = string
  default     = "GitHub Actions"
}

variable "workload_identity_provider_id" {
  description = "Identifier to use for the GitHub Actions Workload Identity Provider."
  type        = string
  default     = "github"
}

variable "workload_identity_provider_display_name" {
  description = "Display name for the Workload Identity Provider."
  type        = string
  default     = "GitHub"
}

variable "github_repository" {
  description = "GitHub repository (ORG/REPO) that is trusted to access the Terraform service account."
  type        = string
  default     = "your-org/your-repo"
}

variable "github_default_branch" {
  description = "Default GitHub branch that is authorized to use Workload Identity Federation."
  type        = string
  default     = "main"
}
