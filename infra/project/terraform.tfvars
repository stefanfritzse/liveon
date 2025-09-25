# Example variables file for the Live On project bootstrap configuration.

project_id          = "live-on-473112"
project_name        = "Live On"
billing_account_id  = "01CCEE-C77C3F-9321F1"
# Exactly one of org_id or folder_id must be provided.
# org_id            = "123456789012"
# folder_id         = "folders/123456789012"
default_region      = "europe-north2"

# Repository that is allowed to impersonate the Terraform service account via Workload Identity Federation.
github_repository = "your-org/your-repo"
# github_default_branch = "main"

# Uncomment to override the default API enablement list.
# activate_apis = [
#   "cloudresourcemanager.googleapis.com",
#   "serviceusage.googleapis.com"
# ]
