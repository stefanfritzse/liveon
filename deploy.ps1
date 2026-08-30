# deploy.ps1
# Robust deploy med Minikube (docker driver) + port-forward i bakgrunden till http://127.0.0.1:8080

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---- Config ----
$ImageName        = 'longevity-coach:latest'
$DeploymentFile   = Join-Path $PSScriptRoot 'deployment.yaml'
$ServiceFile      = Join-Path $PSScriptRoot 'service.yaml'
$PvcFile          = Join-Path $PSScriptRoot 'pvc.yaml'
$DeploymentName   = 'longevity-coach-deployment'
$ServiceName      = 'longevity-coach-service'
$LocalForwardPort = 8080        # lokal port på Windows
$TargetPort       = 8080        # container/service targetPort
$HealthPath       = '/healthz'
$RolloutTimeout   = '180s'
$HealthTimeoutSec = 60

function Test-MinikubeRunning {
  try {
    $s = minikube status --output=json | ConvertFrom-Json
    if ($s -and $s.Host -eq "Running" -and $s.Kubelet -eq "Running" -and $s.APIServer -eq "Running") {
      return $true
    }
  } catch { }

  # `minikube status` probes the node over SSH, and that handshake can fail while the
  # cluster itself is perfectly healthy — which sent this script off to "start" a Minikube
  # that was already up. What actually matters for deploying is whether the API server
  # answers, so ask it directly.
  try {
    kubectl get nodes --request-timeout=10s *> $null
    return ($LASTEXITCODE -eq 0)
  } catch { return $false }
}

function Start-MinikubeSafe {
  Write-Host "Starting Minikube..."
  # Vidarebefordra ev proxy-var
  $dockerEnvArgs = @()
  foreach ($k in @('HTTP_PROXY','HTTPS_PROXY','NO_PROXY','http_proxy','https_proxy','no_proxy')) {
    # Get-Item returns nothing when the variable is unset, and Set-StrictMode makes
    # reading .Value off that a terminating error — so an unproxied machine could not
    # start Minikube at all. GetEnvironmentVariable just returns $null.
    $v = [Environment]::GetEnvironmentVariable($k)
    if ($v) { $dockerEnvArgs += "--docker-env=$k=$v" }
  }
  # Sätt DNS i nodcontainern (motverkar registry-DNS-strul)
  $dockerOptArgs = @("--docker-opt=dns=8.8.8.8","--docker-opt=dns=1.1.1.1")
  minikube start --driver=docker --force @dockerEnvArgs @dockerOptArgs
}

if (-not (Test-MinikubeRunning)) {
  Start-MinikubeSafe
} else {
  Write-Host "Minikube already running; skipping start."
}

# Minikube keeps its own credentials for the node container -- an SSH key and Docker TLS
# certs -- and those can drift out of sync with the node while the cluster itself is
# perfectly healthy. `docker-env` then cannot reach the node's daemon at all. That is a
# broken toolchain, not a broken cluster: the API server answers and the node still runs
# whatever images it has. So try the fast path, and fall back to building on the host
# daemon and handing the finished image to the node.
$usingNodeDaemon = $false
Write-Host "Pointing Docker to Minikube's daemon..."
try {
  $envScript = minikube docker-env --shell=powershell
  if ($LASTEXITCODE -eq 0 -and $envScript) {
    $envScript | Invoke-Expression
    docker info *> $null
    $usingNodeDaemon = ($LASTEXITCODE -eq 0)
  }
} catch { }

if (-not $usingNodeDaemon) {
  Write-Warning "Cannot reach Minikube's Docker daemon; building on the host and loading the image into the node instead."
  # A half-applied docker-env would point the build at an unreachable daemon.
  foreach ($k in @('DOCKER_TLS_VERIFY','DOCKER_HOST','DOCKER_CERT_PATH','MINIKUBE_ACTIVE_DOCKERD')) {
    Remove-Item "Env:$k" -ErrorAction SilentlyContinue
  }
}

Write-Host "Building Docker image $ImageName ..."
docker build -t $ImageName .
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

if (-not $usingNodeDaemon) {
  $tar = Join-Path $env:TEMP 'liveon-image.tar'
  Write-Host "Saving image to $tar ..."
  docker save $ImageName -o $tar
  if ($LASTEXITCODE -ne 0) { throw "docker save failed" }

  # PowerShell 5.1 has no '<' redirection and corrupts binary data sent through a
  # pipeline, so cmd feeds the tar to the node's daemon on stdin. `docker cp` is not an
  # alternative here: it exits 0 while copying nothing when handed a Windows path.
  Write-Host "Loading image into the Minikube node..."
  cmd /c "docker exec -i minikube docker load < `"$tar`""
  if ($LASTEXITCODE -ne 0) { throw "loading the image into the Minikube node failed" }
  Remove-Item $tar -ErrorAction SilentlyContinue
}

Write-Host "Applying Kubernetes manifests..."
if (Test-Path $PvcFile) {
  kubectl apply -f $PvcFile
}
kubectl apply -f $DeploymentFile
kubectl apply -f $ServiceFile

# The manifest pins `longevity-coach:latest`, so a rebuilt image leaves the Deployment
# spec byte-identical and `kubectl apply` is a no-op: the old pod keeps running and the
# rollout "succeeds" without the new image ever being picked up. Restarting forces it.
Write-Host "Restarting deployment so the rebuilt image is picked up..."
kubectl rollout restart deploy/$DeploymentName

Write-Host "Waiting for deployment rollout..."
kubectl rollout status deploy/$DeploymentName --timeout=$RolloutTimeout

# ---- Döda ev. gamla port-forward-processer (kubectl.exe) ----
Write-Host "Ensuring no stale port-forward is running..."
try {
  $pattern = "port-forward\s+svc/$ServiceName\s+${LocalForwardPort}:${TargetPort}"
  Get-CimInstance Win32_Process -Filter "Name='kubectl.exe'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern) } |
    ForEach-Object {
      Stop-Process -Id $_.ProcessId -Force
      Write-Host "Killed old kubectl port-forward pid=$($_.ProcessId)"
    }
} catch { }

# ---- Stoppa ev. bakgrundsjobb med samma namn (utan -Force, för PS 5.1-kompat.) ----
$pfJobName = "pf-$ServiceName-$LocalForwardPort"
$existing = Get-Job -Name $pfJobName -ErrorAction SilentlyContinue
if ($existing) {
  try { Stop-Job -Id $existing.Id -ErrorAction SilentlyContinue } catch { }
  try { Remove-Job -Id $existing.Id -ErrorAction SilentlyContinue } catch { }
}

# ---- Starta port-forward som bakgrundsjobb ----
Write-Host "Starting port-forward: svc/$ServiceName => http://127.0.0.1:$LocalForwardPort ..."
$pfJob = Start-Job -Name $pfJobName -ScriptBlock {
  $ErrorActionPreference = 'Stop'
  kubectl port-forward "svc/$using:ServiceName" "$($using:LocalForwardPort):$($using:TargetPort)"
}

Start-Sleep -Seconds 2

# ---- Health check mot forwarded port ----
$healthUrl = "http://127.0.0.1:$LocalForwardPort$HealthPath"
$deadline  = (Get-Date).AddSeconds($HealthTimeoutSec)
$ok = $false

while ((Get-Date) -lt $deadline) {
  try {
    # -UseBasicParsing is not optional: without it PowerShell 5.1 routes the response
    # through the Internet Explorer engine, which needs IE's first-run configuration and
    # throws "PowerShell is in NonInteractive mode" when it is missing. The service is
    # then healthy and the script reports it as broken.
    $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing
    if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) {
      $ok = $true
      break
    }
  } catch {
    Start-Sleep -Milliseconds 750
  }
}

if ($ok) {
  Write-Host "Service available at http://127.0.0.1:$LocalForwardPort  (health OK on $HealthPath)"
} else {
  Write-Warning "Port-forward started, but health check failed at $healthUrl"
  Write-Warning "Inspect logs with: kubectl logs -l app=longevity-coach --tail=100"
}

Write-Host "Tip: stop the port-forward later with:"
Write-Host "  Get-Job -Name '$pfJobName' | Stop-Job; Remove-Job -Name '$pfJobName'"
Write-Host "Deployment complete!"
