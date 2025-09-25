# Infrastructure

This directory contains Terraform configurations and supporting assets for the Live On platform.

## Layout

- `bootstrap/` – one-time configuration that provisions the remote state bucket and the
  Terraform runner service account.
- `environments/prod/` – production environment configuration that links the project to
  billing, enables foundational APIs, and provisions the VPC, Artifact Registry, GKE
  cluster, and GitHub Actions runner described in the Phase 1 implementation plan.

Additional environments can be added under `environments/` as the project evolves.
