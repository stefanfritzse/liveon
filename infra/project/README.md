# Project Bootstrap Terraform


This configuration manages the Google Cloud project linkage to billing and enables foundational APIs required by later infrastructure stages.

## Prerequisites

- Terraform >= 1.5
- The Google Cloud CLI authenticated with permissions to view/update the target project and
  billing account.
- Either the organization ID or the folder ID that owns the project.
- The remote state bucket and `sa-terraform` service account created with
  `../bootstrap`.

## Usage

1. Copy the example variables file and adjust it to your environment:

   ```powershell
   Copy-Item terraform.tfvars.example terraform.tfvars
   ```

   Update `terraform.tfvars` with the correct `project_id`, `billing_account_id`, and either
   `org_id` or `folder_id`. Set `github_repository` to the `<owner>/<repo>` string for the
   GitHub repository whose workflows should be allowed to assume the Terraform service account.

2. Configure the backend to use the remote state bucket created by the bootstrap step. Either
   update `backend.tf.example` with your bucket name and rename it to `backend.tf`, or pass
   the bucket and prefix via `terraform init -backend-config` flags:

   ```powershell
   Copy-Item backend.tf.example backend.tf
   (Get-Content backend.tf).Replace("<STATE_BUCKET_NAME>", "live-on-473112-tf-state") | Set-Content backend.tf
   ```

3. Initialize Terraform:

   ```powershell
   terraform init -migrate-state
   ```

4. If the project `live-on-473112` already exists, import it into state before planning:


   ```powershell
   terraform import google_project.project live-on-473112
   ```

5. Review the proposed changes:

   ```powershell
   terraform plan
   ```

6. Apply the configuration when ready:


   ```powershell
   terraform apply
   ```

After the apply completes, the project will be attached to the billing account and the listed
APIs will remain enabled under remote state management.

## Managed Resources

This stage of the configuration now provisions the following foundational resources in
addition to the project and IAM automation described above:

- A dedicated VPC network (`google_compute_network.primary`) with a regional subnetwork
  tailored for Autopilot GKE workloads.
- Two secondary IP ranges on the subnetwork reserved for Kubernetes Pods and Services,
  enabling VPC-native cluster networking.

