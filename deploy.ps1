# deploy.ps1
# Robust deploy med Minikube (docker driver) + port-forward i bakgrunden till http://127.0.0.1:8080

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---- Config ----
$ImageName        = 'longevity-coach:latest'
$DeploymentFile   = Join-Path $PSScriptRoot 'deployment.yaml'
$ServiceFile      = Join-Path $PSScriptRoot 'service.yaml'
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
    return ($s.Host -eq "Running" -and $s.Kubelet -eq "Running" -and $s.APIServer -eq "Running")
  } catch { return $false }
}

function Start-MinikubeSafe {
  Write-Host "Starting Minikube..."
  # Vidarebefordra ev proxy-var
  $dockerEnvArgs = @()
  foreach ($k in @('HTTP_PROXY','HTTPS_PROXY','NO_PROXY','http_proxy','https_proxy','no_proxy')) {
    $v = (Get-Item "Env:$k" -ErrorAction SilentlyContinue).Value
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

Write-Host "Pointing Docker to Minikube's daemon..."
(minikube docker-env --shell=powershell) | Invoke-Expression

Write-Host "Building Docker image $ImageName ..."
docker build -t $ImageName .

Write-Host "Applying Kubernetes manifests..."
kubectl apply -f $DeploymentFile
kubectl apply -f $ServiceFile

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
    $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3
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
