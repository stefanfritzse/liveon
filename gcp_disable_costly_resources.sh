#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${STATE_FILE:-$ROOT/gcp_cost_toggle_state.json}"
PROJECT="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$PROJECT" && "$PROJECT" != "(unset)" ]] || { echo "Set PROJECT_ID or configure gcloud with a project." >&2; exit 1; }
mkdir -p "$(dirname "$STATE_FILE")"
update_state(){ python - "$STATE_FILE" <<'PY'
import json,os,sys
path=sys.argv[1]
state={}
if os.path.exists(path):
  with open(path) as f: state=json.load(f)
state['project']=os.environ['PROJECT']
state['last_action']='disable'
for key in ('gke','runner','services_disabled'):
  val=os.environ.get(key.upper())
  if val:
    state[key]=json.loads(val)
with open(path,'w') as f: json.dump(state,f,indent=2,sort_keys=True)
PY
}
CLUSTER_NAME="${GKE_CLUSTER_NAME:-gke-longevity-primary}"
cluster_location="$(gcloud container clusters list --project "$PROJECT" --filter="name=$CLUSTER_NAME" --format="value(location)" || true)"
GKE="{}"
if [[ -n "$cluster_location" ]]; then
  loc_type=region
  [[ "$cluster_location" =~ ^[a-z]+-[a-z0-9]+-[a-z]$ ]] && loc_type=zone
  flag="--$loc_type"
  cluster_json="$(gcloud container clusters describe "$CLUSTER_NAME" "$flag" "$cluster_location" --project "$PROJECT" --format=json)"
  export PROJECT CLUSTER_JSON="$cluster_json" CLUSTER_LOCATION="$cluster_location" CLUSTER_KIND="$loc_type"
  GKE="$(python - <<'PY'
import json,os
c=json.loads(os.environ['CLUSTER_JSON'])
state={'cluster':{
  'name':c.get('name'),
  'location':os.environ['CLUSTER_LOCATION'],
  'location_type':os.environ['CLUSTER_KIND'],
  'network':c.get('network'),
  'subnetwork':c.get('subnetwork'),
  'release_channel':(c.get('releaseChannel') or {}).get('channel'),
  'workload_pool':(c.get('workloadIdentityConfig') or {}).get('workloadPool'),
  'private_nodes':(c.get('privateClusterConfig') or {}).get('enablePrivateNodes'),
  'private_endpoint':(c.get('privateClusterConfig') or {}).get('enablePrivateEndpoint'),
  'master_ipv4_cidr':(c.get('privateClusterConfig') or {}).get('masterIpv4CidrBlock'),
  'cluster_secondary_range':(c.get('ipAllocationPolicy') or {}).get('clusterSecondaryRangeName'),
  'services_secondary_range':(c.get('ipAllocationPolicy') or {}).get('servicesSecondaryRangeName'),
  'authorized_networks':[{'name':b.get('displayName') or b.get('cidrBlock'),'cidr':b.get('cidrBlock')} for b in (c.get('masterAuthorizedNetworksConfig') or {}).get('cidrBlocks',[])],
  'workload_identity_pool':(c.get('workloadIdentityConfig') or {}).get('workloadPool')
}}
print(json.dumps(state))
PY
)"
  gcloud container clusters delete "$CLUSTER_NAME" "$flag" "$cluster_location" --project "$PROJECT" --quiet
else
  echo "GKE cluster $CLUSTER_NAME not found; skipping deletion." >&2
fi
RUNNER_NAME="${RUNNER_INSTANCE_NAME:-gha-runner}"
runner_zone="${RUNNER_ZONE:-}"
if [[ -z "$runner_zone" ]]; then
  zone_ref="$(gcloud compute instances list --project "$PROJECT" --filter="name=$RUNNER_NAME" --format="value(zone)" || true)"
  [[ -n "$zone_ref" ]] && runner_zone="${zone_ref##*/}"
fi
RUNNER="{}"
if [[ -n "$runner_zone" ]]; then
  runner_json="$(gcloud compute instances describe "$RUNNER_NAME" --zone "$runner_zone" --project "$PROJECT" --format=json 2>/dev/null || true)"
  if [[ -n "$runner_json" ]]; then
    status="$(echo "$runner_json" | jq -r '.status')"
    [[ "$status" != "TERMINATED" ]] && gcloud compute instances stop "$RUNNER_NAME" --zone "$runner_zone" --project "$PROJECT" --quiet
    export RUNNER_JSON="$runner_json" RUNNER_ZONE="$runner_zone"
    RUNNER="$(python - <<'PY'
import json,os
r=json.loads(os.environ['RUNNER_JSON'])
print(json.dumps({'name':r.get('name'),'zone':os.environ['RUNNER_ZONE'],'status_before_stop':r.get('status')}))
PY
)"
  else
    echo "Runner $RUNNER_NAME not found in zone $runner_zone; skipping stop." >&2
  fi
else
  echo "Runner zone not found; skipping runner stop." >&2
fi
RUNNER="${RUNNER:-{}}"
SERVICES_DISABLED="{}"
if [[ -n "${SERVICES_TO_DISABLE:-1}" ]]; then
  mapfile -t services < <(printf '%s\n' container.googleapis.com aiplatform.googleapis.com cloudbuild.googleapis.com)
  disabled=()
  for svc in "${services[@]}"; do
    if gcloud services list --project "$PROJECT" --enabled --filter="NAME:$svc" --format="value(name)" | grep -q "$svc"; then
      if gcloud services disable "$svc" --project "$PROJECT" --quiet; then
        disabled+=("\"$svc\"")
      fi
    fi
  done
  [[ ${#disabled[@]} -gt 0 ]] && SERVICES_DISABLED="{\"services_disabled\":[${disabled[*]}]}"
fi
export PROJECT GKE RUNNER SERVICES_DISABLED
update_state
echo "Cost-intensive resources disabled. State stored in $STATE_FILE"
