Always read the ./infra/environments/prod/terraform_creation_log (which is created after Terraform Apply command) for context about:
Project metadata: project ID/number.
API enablement: list of Google APIs turned on.
Resource identifiers/URIs: self-links and full resource paths (GCE instance, subnet, Artifact Registry).
GKE cluster basics: cluster name, control-plane endpoint.
GKE networking: secondary CIDR ranges for Pods/Services and the subnetwork used.
Identity & access: service accounts (GKE nodes, runner VM) and Workload Identity pool/provider details.
Network topology: VPC network name.
CI/CD infra details: GitHub Actions runner VM (internal IP, self-link).
Artifact storage: Artifact Registry repository path.
"""
In ./logs you can find the following information that is fetched from the running app in GCP:
resources.csv — project-wide inventory (asset type, name, location, labels) [optional if SKIP_CAI=1]
compute_instances.csv — VMs: zone, machine type, internal/external IPs, status, attached service account, tags
networks.csv — VPC networks: subnet mode, auto-subnets flag, routing mode
subnets.csv — Subnets: region, parent network, CIDR, Private Google Access
firewall_rules.csv — Firewall rules: direction, priority, sources, allowed ports/protocols, target tags
addresses.csv — Static/reserved IPs: address, region, status, purpose, subnetwork (if applicable)
forwarding_rules.csv — Load balancer/DNAT frontends: IP, protocol, scheme, port range, target, tier
gke_clusters.csv — GKE clusters: location, endpoint, control-plane/node versions, release channel, Autopilot, WI pool
cloud_run_services.csv — Cloud Run services: region, ingress, service account, URL
workflows.csv — Workflows: location, state, service account
cloudsql.csv — Cloud SQL instances: region, engine version, public IP (if any), activation policy
service_accounts.csv — Service accounts: email, display name, disabled flag
project_iam.csv — Project IAM bindings flattened: role → member pairs