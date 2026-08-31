# deploy.ps1 -- build and deploy Live On to the local Minikube cluster.
#
# This used to be a second, independent implementation of the deploy: its own build, its own
# manifest apply, its own port-forward. Two scripts against one cluster is not a convenience,
# it is a bug generator. This one pointed at the *default* MINIKUBE_HOME while the cluster
# actually lives under C:\ProgramData\SambandsCentral\k8s\minikube, which produced a
# thoroughly convincing and completely wrong "the node's SSH credentials have drifted"
# diagnosis; and because both scripts deployed `:latest`, `kubectl apply` was a no-op and a
# rebuild silently kept serving the old pod.
#
# So there is now one implementation and this is a thin wrapper around it. Serving is not
# started here: it is a separate long-lived concern owned by liveon_k8s_serve.ps1, which
# Sambands Central supervises. A deploy no longer interrupts it.

param(
    # Override if sambandscentral lives somewhere else on this machine.
    [string]$ScriptsPath = (Join-Path (Join-Path $env:USERPROFILE "sambandscentral") "scripts"),
    [switch]$SkipServeHint
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$canonical = Join-Path $ScriptsPath "liveon_k8s_deploy.ps1"

if (-not (Test-Path $canonical)) {
    throw @"
Canonical deploy script not found at:
  $canonical

It lives in the sambandscentral repository, which also owns the app manifest that runs
Live On. Clone or fix that checkout, or pass -ScriptsPath pointing at its scripts folder.
"@
}

& $canonical -RepoPath $PSScriptRoot
if ($LASTEXITCODE -ne 0) {
    throw "Deploy failed with exit code $LASTEXITCODE."
}

if (-not $SkipServeHint) {
    Write-Host ""
    Write-Host "Deployed. Serving is separate and is not affected by this:"
    Write-Host "  - Sambands Central runs 'liveon' (liveon_k8s_serve.ps1), which keeps the"
    Write-Host "    liveon-proxy container pointed at the Service's NodePort."
    Write-Host "  - A rollout is invisible to it, so there is nothing to restart here."
}
