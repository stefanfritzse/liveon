# Bootstrap Remote State and Terraform Service Account

This configuration performs the one-time bootstrap required before the rest of the
infrastructure can be managed automatically. It creates:

- A dedicated Google Cloud Storage bucket with object versioning enabled for Terraform
  remote state.
- The `sa-terraform` service account with the IAM roles required for the CI/CD pipeline to
  manage infrastructure.

Run this module locally with elevated permissions (a user who can create buckets and grant
project-level IAM) before switching the other Terraform configurations to the remote GCS
backend.

## Prerequisites

- Terraform >= 1.5
- Google Cloud CLI authenticated as a user with permissions to create service accounts,
  grant project IAM roles, and manage Cloud Storage.
- The target project ID (e.g., `live-on-473112`).

## Usage

1. Copy the example variable file and update it with a globally unique bucket name and the
   desired bucket location (for example, `us` or `europe-west1`):

   ```powershell
   Copy-Item terraform.tfvars.example terraform.tfvars
   ```

   Update `terraform.tfvars` with your chosen values.

2. Initialize Terraform:

   ```powershell
   terraform init
   ```

3. Review and apply the configuration:

   ```powershell
   terraform plan
   terraform apply
   ```

   The outputs will show the bucket name and the email of the new Terraform service account.

4. Grant trusted operators the ability to impersonate the new service account if they need to
   run Terraform manually:

   ```powershell
   gcloud iam service-accounts add-iam-policy-binding sa-terraform@<PROJECT_ID>.iam.gserviceaccount.com `
     --member=user:<YOUR_EMAIL> `
     --role=roles/iam.serviceAccountTokenCreator
   ```

5. Update the remaining Terraform configurations (for example `infra/environments/prod`) to use the new
   GCS backend. A sample backend block is shown below—ensure that the bucket name matches
   the value you created in step 1:

   ```hcl
   terraform {
     backend "gcs" {
       bucket = "your-tf-state-bucket"
       prefix = "prod"
     }
   }
   ```

   After saving the backend configuration, run `terraform init -migrate-state` in each
   Terraform directory to move local state into the remote bucket.

## IAM Roles Granted to `sa-terraform`

The service account receives the minimum roles required to provision the resources described
in Phase 1 of the implementation plan:

- `roles/resourcemanager.projectIamAdmin`
- `roles/container.admin`
- `roles/artifactregistry.admin`
- `roles/iam.serviceAccountUser`
- `roles/storage.admin`

These bindings allow the GitHub Actions pipeline (via Workload Identity Federation) to manage
project IAM, GKE, Artifact Registry, and the Terraform state bucket without granting
unnecessary permissions.
