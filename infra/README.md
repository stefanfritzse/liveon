# Infrastructure

This directory contains Terraform configurations and supporting assets for the Live On platform.

## Layout

- `bootstrap/` – one-time configuration that provisions the remote state bucket and the
  Terraform runner service account.
- `project/` – project-level Terraform responsible for billing linkage and enabling core APIs.

Additional environments (network, cluster, etc.) will be added as the project evolves.
