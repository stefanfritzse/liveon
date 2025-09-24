output "state_bucket_name" {
  description = "Name of the Google Cloud Storage bucket storing the Terraform state."
  value       = google_storage_bucket.terraform_state.name
}

output "terraform_service_account_email" {
  description = "Email address of the Terraform service account."
  value       = google_service_account.terraform.email
}
