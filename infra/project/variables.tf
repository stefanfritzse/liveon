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
  default     = "us-central1"
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
