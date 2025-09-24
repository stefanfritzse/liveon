# Project Bootstrap Terraform

This configuration manages the Google Cloud project linkage to billing and enables foundational APIs required by later infrastructure stages.

## Prerequisites

- Terraform >= 1.5
- The Google Cloud CLI authenticated with permissions to view/update the target project and billing account.
- Either the organization ID or the folder ID that owns the project.

## Usage

1. Copy the example variables file and adjust it to your environment:

   ```powershell
   Copy-Item terraform.tfvars.example terraform.tfvars
   ```

   Update `terraform.tfvars` with the correct `project_id`, `billing_account_id`, and either `org_id` or `folder_id`.

2. Initialize Terraform:

   ```powershell
   terraform init
   ```

3. If the project `live-on-473112` already exists, import it into state before planning:

   ```powershell
   terraform import google_project.project live-on-473112
   ```

4. Review the proposed changes:

   ```powershell
   terraform plan
   ```

5. Apply the configuration when ready:

   ```powershell
   terraform apply
   ```

After the apply completes, the project will be attached to the billing account and the listed APIs will be enabled.
