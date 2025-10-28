#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${STATE_FILE:-$ROOT/gcp_cost_toggle_state.json}"
[[ -f "$STATE_FILE" ]] || { echo "State file $STATE_FILE not found. Run the disable script first." >&2; exit 1; }
PROJECT="${PROJECT_ID:-$(jq -r '.project // empty' "$STATE_FILE")}" 
[[ -n "$PROJECT" && "$PROJECT" != "null" ]] || { echo "Set PROJECT_ID or ensure state file contains a project." >&2; exit 1; }
update_state(){ python - "$STATE_FILE" <<'PY'
import json,os,sys
path=sys.argv[1]
with open(path) as f: state=json.load(f)
state['project']=os.environ['PROJECT']
state['last_action']='enable'
with open(path,'w') as f: json.dump(state,f,indent=2,sort_keys=True)
PY
}
SERVICES="$(jq -r '.services_disabled.services_disabled[]? // empty' "$STATE_FILE")"
if [[ -n "$SERVICES" ]]; then
  while IFS= read -r svc; do
    [[ -n "$svc" ]] || continue
    gcloud services enable "$svc" --project "$PROJECT" --quiet
  done <<< "$SERVICES"
fi
cluster_name="$(jq -r '.gke.cluster.name // empty' "$STATE_FILE")"
if [[ -n "$cluster_name" ]]; then
  existing="$(gcloud container clusters list --project "$PROJECT" --filter="name=$cluster_name" --format="value(name)" || true)"
  if [[ -z "$existing" ]]; then
    location="$(jq -r '.gke.cluster.location' "$STATE_FILE")"
    loc_type="$(jq -r '.gke.cluster.location_type // "region"' "$STATE_FILE")"
    args=(container clusters create-auto "$cluster_name" "--project" "$PROJECT" "--$loc_type" "$location")
    network="$(jq -r '.gke.cluster.network // empty' "$STATE_FILE")"
    subnetwork="$(jq -r '.gke.cluster.subnetwork // empty' "$STATE_FILE")"
    [[ -n "$network" && "$network" != "null" ]] && args+=("--network" "$network")
    [[ -n "$subnetwork" && "$subnetwork" != "null" ]] && args+=("--subnetwork" "$subnetwork")
    csr="$(jq -r '.gke.cluster.cluster_secondary_range // empty' "$STATE_FILE")"
    ssr="$(jq -r '.gke.cluster.services_secondary_range // empty' "$STATE_FILE")"
    [[ -n "$csr" && "$csr" != "null" ]] && args+=("--cluster-secondary-range-name" "$csr")
    [[ -n "$ssr" && "$ssr" != "null" ]] && args+=("--services-secondary-range-name" "$ssr")
    master_cidr="$(jq -r '.gke.cluster.master_ipv4_cidr // empty' "$STATE_FILE")"
    [[ -n "$master_cidr" && "$master_cidr" != "null" ]] && args+=("--master-ipv4-cidr" "$master_cidr")
    release="$(jq -r '.gke.cluster.release_channel // empty' "$STATE_FILE")"
    [[ -n "$release" && "$release" != "null" ]] && args+=("--release-channel" "$release")
    pool="$(jq -r '.gke.cluster.workload_identity_pool // empty' "$STATE_FILE")"
    [[ -n "$pool" && "$pool" != "null" ]] && args+=("--workload-pool" "$pool")
    priv_nodes="$(jq -r '.gke.cluster.private_nodes' "$STATE_FILE")"
    [[ "$priv_nodes" == "true" ]] && args+=("--enable-private-nodes")
    priv_ep="$(jq -r '.gke.cluster.private_endpoint' "$STATE_FILE")"
    [[ "$priv_ep" == "true" ]] && args+=("--enable-private-endpoint")
    mapfile -t auths < <(jq -r '.gke.cluster.authorized_networks[]? | "\(.name)=\(.cidr)"' "$STATE_FILE")
    if [[ ${#auths[@]} -gt 0 ]]; then
      args+=("--enable-master-authorized-networks" "--master-authorized-networks" "$(IFS=,; echo "${auths[*]}")")
    fi
    gcloud "${args[@]}"
  else
    echo "Cluster $cluster_name already exists; skipping creation." >&2
  fi
fi
runner_name="$(jq -r '.runner.name // empty' "$STATE_FILE")"
runner_zone="$(jq -r '.runner.zone // empty' "$STATE_FILE")"
if [[ -n "$runner_name" && -n "$runner_zone" ]]; then
  if gcloud compute instances describe "$runner_name" --zone "$runner_zone" --project "$PROJECT" --format="value(name)" &>/dev/null; then
    gcloud compute instances start "$runner_name" --zone "$runner_zone" --project "$PROJECT" --quiet
  else
    echo "Runner $runner_name not found in zone $runner_zone; skipping start." >&2
  fi
fi
export PROJECT
update_state
echo "Cost-intensive resources restored where possible."
