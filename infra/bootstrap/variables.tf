variable "project_id" {
  description = "The ID of the Google Cloud project to bootstrap."
  type        = string
}

variable "state_bucket_name" {
  description = "Globally unique name to assign to the Terraform state bucket."
  type        = string
}

variable "state_bucket_location" {
  description = "Location in which to create the Terraform state bucket."
  type        = string
  default     = "US"
}

variable "terraform_service_account_id" {
  description = "The account ID (name) for the Terraform service account."
  type        = string
  default     = "sa-terraform"
}

variable "terraform_service_account_display_name" {
  description = "The display name for the Terraform service account."
  type        = string
  default     = "Terraform Service Account"
}
