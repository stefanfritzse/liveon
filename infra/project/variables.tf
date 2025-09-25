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

variable "network_name" {
  description = "Name of the primary VPC network for the platform."
  type        = string
  default     = "vpc-longevity"
}

variable "subnetwork_name" {
  description = "Name of the regional subnetwork dedicated to the GKE cluster."
  type        = string
  default     = "subnet-gke-primary"
}

variable "subnetwork_cidr_range" {
  description = "Primary IPv4 CIDR range allocated to the regional subnetwork."
  type        = string
  default     = "10.10.0.0/20"
}

variable "pod_ip_range_name" {
  description = "Identifier for the secondary IP range dedicated to Kubernetes Pods."
  type        = string
  default     = "gke-pods"
}

variable "pod_ip_cidr_range" {
  description = "CIDR block assigned to the Kubernetes Pod secondary IP range."
  type        = string
  default     = "10.20.0.0/16"
}

variable "service_ip_range_name" {
  description = "Identifier for the secondary IP range dedicated to Kubernetes Services."
  type        = string
  default     = "gke-services"
}

variable "service_ip_cidr_range" {
  description = "CIDR block assigned to the Kubernetes Service secondary IP range."
  type        = string
  default     = "10.30.0.0/20"
}

variable "artifact_registry_location" {
  description = "Region where the Artifact Registry repository will be created."
  type        = string
  default     = "europe-north2"
}

variable "artifact_registry_repository_id" {
  description = "Identifier to assign to the Artifact Registry repository."
  type        = string
  default     = "longevity-app"
}

variable "artifact_registry_repository_description" {
  description = "Human-readable description for the Artifact Registry repository."
  type        = string
  default     = "Container images for the Live On platform."
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
