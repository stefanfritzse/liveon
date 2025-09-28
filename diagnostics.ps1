param(
  [Parameter(Mandatory=$true)][string]$Cluster,
  [Parameter(Mandatory=$true)][string]$Region,
  [switch]$UseInternalIp,
  [switch]$AutoAddIp,
  [string]$Namespace = ""
)

$ErrorActionPreference = "Stop"

function Assert-Cli($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Required CLI '$name' not found on PATH: $name"
  }
}

function Exec-Json {
  param([string]$CmdLine) # runs external cmd that outputs JSON
  $out = & powershell -NoProfile -Command $CmdLine 2>$null
  if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($out)) {
    throw "Command failed (code $LASTEXITCODE): $CmdLine"
  }
  try { return $out | ConvertFrom-Json } catch { throw "JSON parse failed for: $CmdLine`n$out" }
}

function Exec-Text {
  param([string]$CmdLine, [switch]$IgnoreExit)
  $out = & powershell -NoProfile -Command $CmdLine
  if (-not $IgnoreExit -and $LASTEXITCODE -ne 0) {
    throw "Command failed (code $LASTEXITCODE): $CmdLine`n$out"
  }
  return $out
}

function Out-Section($title) {
  Write-Host ""
  Write-Host ("=" * 80)
  Write-Host $title -ForegroundColor Cyan
  Write-Host ("=" * 80)
}

# 0) Preconditions
Assert-Cli gcloud
Assert-Cli kubectl

$project = (Exec-Text "gcloud config get-value project").Trim()
if ($project -ne "live-on-473112") {
  throw "gcloud project is '$project' but expected 'live-on-473112'. Run: gcloud config set project live-on-473112"
}
$account = (Exec-Text "gcloud config get-value account").Trim()
Write-Host "Active account: $account"
Write-Host "Project: $project"
Write-Host "Cluster: $Cluster  Region: $Region"

# 1) Cluster describe + access
Out-Section "Cluster Summary (gcloud)"
$desc = Exec-Json "gcloud container clusters describe $Cluster --region $Region --format json"

$endpoint    = $desc.endpoint
$autopilot   = $false; if ($desc.autopilot -and $desc.autopilot.enabled) { $autopilot = $true }
$private     = $false; if ($desc.privateClusterConfig -and $desc.privateClusterConfig.enablePrivateNodes) { $private = $true }
$releaseCh   = if ($desc.releaseChannel) { $desc.releaseChannel.channel } else { "" }
$masterVer   = $desc.currentMasterVersion
$nodeVer     = $desc.currentNodeVersion
$status      = $desc.status
$net         = $desc.network
$subnet      = $desc.subnetwork
$manEnabled  = $false; if ($desc.masterAuthorizedNetworksConfig -and $desc.masterAuthorizedNetworksConfig.enabled) { $manEnabled = $true }
$manCidrs    = @()
if ($manEnabled -and $desc.masterAuthorizedNetworksConfig.cidrBlocks) {
  $manCidrs = @($desc.masterAuthorizedNetworksConfig.cidrBlocks | ForEach-Object { $_.cidrBlock })
}

Write-Host ("Status: {0}" -f $status)
Write-Host ("Endpoint (public): {0}" -f $endpoint)
Write-Host ("Autopilot: {0}" -f $autopilot)
Write-Host ("Private Nodes: {0}" -f $private)
Write-Host ("Release Channel: {0}" -f $releaseCh)
Write-Host ("Master Version: {0}  Node Version: {1}" -f $masterVer, $nodeVer)
Write-Host ("Network/Subnet: {0} / {1}" -f $net, $subnet)
Write-Host ("MAN enabled: {0}  Allowed CIDRs: {1}" -f $manEnabled, ($manCidrs -join ","))

# kube credentials
try {
  if ($UseInternalIp) {
    Exec-Text "gcloud container clusters get-credentials $Cluster --region $Region --internal-ip" | Out-Null
  } else {
    Exec-Text "gcloud container clusters get-credentials $Cluster --region $Region" | Out-Null
  }
  Exec-Text "kubectl version --short" -IgnoreExit | Out-Null
} catch {
  if ($AutoAddIp -and -not $UseInternalIp) {
    Out-Section "Control-plane access blocked; attempting to add this IP to MAN"
    try { $myIp = (Invoke-WebRequest -Uri "https://api.ipify.org" -UseBasicParsing).Content.Trim() } catch { throw "Could not detect public IP for MAN update." }
    $ranges = @()
    if ($manCidrs) { $ranges += $manCidrs }
    if ($ranges -notcontains "$myIp/32") { $ranges += "$myIp/32" }
    $rangeStr = ($ranges -join ",")
    Exec-Text "gcloud container clusters update $Cluster --region $Region --enable-master-authorized-networks --master-authorized-networks $rangeStr" | Out-Null
    Exec-Text "gcloud container clusters get-credentials $Cluster --region $Region" | Out-Null
  } else {
    throw $_
  }
}

$nsFlag = ""
if ($Namespace -and $Namespace.Trim() -ne "") { $nsFlag = "-n $Namespace" }

# 2) Nodes
Out-Section "Nodes Health"
$nodes = Exec-Json "kubectl get nodes -o json"
$nodeRows = @()
if ($nodes.items) {
  foreach ($n in $nodes.items) {
    $name = $n.metadata.name
    $readyCond = $null
    foreach ($c in $n.status.conditions) { if ($c.type -eq "Ready") { $readyCond = $c; break } }
    $ready = if ($readyCond) { $readyCond.status } else { "Unknown" }
    $labels = $n.metadata.labels
    $arch = if ($labels) { $labels."kubernetes.io/arch" } else { "" }
    $zone = if ($labels) { $labels."topology.kubernetes.io/zone" } else { "" }
    $allocCpu = $n.status.allocatable.cpu
    $allocMem = $n.status.allocatable.memory
    $nodeRows += [pscustomobject]@{ Node=$name; Ready=$ready; Zone=$zone; Arch=$arch; CPU=$allocCpu; Memory=$allocMem }
  }
  $nodeRows | Format-Table -AutoSize
} else {
  Write-Host "No nodes returned."
}

# 3) Pods + problem pods
Out-Section "Pods Overview"
$pods = Exec-Json ("kubectl get pods {0} -A -o json" -f $nsFlag)
$counts = @{}
$problemPods = @()

if ($pods.items) {
  foreach ($p in $pods.items) {
    $phase = $p.status.phase
    if (-not $counts.ContainsKey($phase)) { $counts[$phase] = 0 }
    $counts[$phase] = $counts[$phase] + 1

    $notReady = $false
    $restartSum = 0
    $reasonList = @()
    if ($p.status.containerStatuses) {
      foreach ($cs in $p.status.containerStatuses) {
        if ($cs.restartCount) { $restartSum += [int]$cs.restartCount }
        if (-not $cs.ready) { $notReady = $true }
        if ($cs.state) {
          if ($cs.state.waiting -and $cs.state.waiting.reason) { $reasonList += $cs.state.waiting.reason }
          if ($cs.state.terminated -and $cs.state.terminated.reason) { $reasonList += $cs.state.terminated.reason }
        }
        if ($cs.lastState -and $cs.lastState.terminated -and $cs.lastState.terminated.reason) {
          $reasonList += $cs.lastState.terminated.reason
        }
      }
    }
    if ($notReady -or $restartSum -gt 0) {
      $problemPods += [pscustomobject]@{
        Namespace = $p.metadata.namespace
        Pod       = $p.metadata.name
        Phase     = $phase
        Reason    = ($reasonList -join ",")
        Restarts  = $restartSum
        Node      = $p.spec.nodeName
        Age       = $p.status.startTime
      }
    }
  }

  Write-Host ("Pod phases: {0}" -f (($counts.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ", "))
  if ($problemPods.Count -gt 0) {
    Write-Host ""
    Write-Host "Problem pods:" -ForegroundColor Yellow
    $problemPods | Sort-Object Restarts -Descending | Select-Object -First 25 | Format-Table -AutoSize
  } else {
    Write-Host "No problem pods detected."
  }
} else {
  Write-Host "No pods returned."
}

# 4) CronJobs & Jobs
Out-Section "CronJobs & Jobs"
$cron = Exec-Json ("kubectl get cronjobs.batch {0} -A -o json" -f $nsFlag)
$jobs = Exec-Json ("kubectl get jobs.batch {0} -A -o json" -f $nsFlag)

$cronRows = @()
$jobItems = @()
if ($jobs.items) { $jobItems = $jobs.items }

if ($cron.items) {
  foreach ($cj in $cron.items) {
    $ns = $cj.metadata.namespace
    $name = $cj.metadata.name
    $sched = $cj.spec.schedule
    $suspend = $cj.spec.suspend
    $lastSched = if ($cj.status) { $cj.status.lastScheduleTime } else { $null }
    $active = 0; if ($cj.status -and $cj.status.active) { $active = @($cj.status.active).Count }

    # Find owned jobs
    $owned = @()
    foreach ($j in $jobItems) {
      $owners = $j.metadata.ownerReferences
      if ($owners) {
        foreach ($o in $owners) {
          if ($o.kind -eq "CronJob" -and $o.name -eq $name) { $owned += $j; break }
        }
      }
    }
    $recent = @()
    if ($owned.Count -gt 0) {
      $recent = $owned | Sort-Object { $_.metadata.creationTimestamp } -Descending | Select-Object -First 5
    }
    $succ = 0; $fail = 0
    foreach ($r in $recent) {
      if ($r.status -and $r.status.conditions) {
        foreach ($c in $r.status.conditions) {
          if ($c.type -eq "Complete" -and $c.status -eq "True") { $succ++ }
          if ($c.type -eq "Failed"   -and $c.status -eq "True") { $fail++ }
        }
      }
    }

    $cronRows += [pscustomobject]@{
      Namespace    = $ns
      CronJob      = $name
      Schedule     = $sched
      Suspend      = $suspend
      LastSchedule = $lastSched
      ActiveJobs   = $active
      RecentSuccess= $succ
      RecentFailed = $fail
    }
  }
}

if ($cronRows.Count -gt 0) {
  $cronRows | Sort-Object Namespace,CronJob | Format-Table -AutoSize
} else {
  Write-Host "No CronJobs found."
}

# 5) Events (warnings/errors tail)
Out-Section "Recent Warnings/Errors (Events)"
$events = Exec-Json "kubectl get events -A --sort-by=.lastTimestamp -o json"
$bad = @()
if ($events.items) {
  foreach ($e in $events.items) {
    $etype = $e.type
    $reason = $e.reason
    $msg = $e.message
    $isWarn = ($etype -eq "Warning")
    $matches = ($reason -match "Failed|BackOff|Error|Err|CreateContainerConfigError|ImagePull|DeadlineExceeded|FailedScheduling")
    if ($isWarn -or $matches) {
      $bad += [pscustomobject]@{
        Time   = $e.lastTimestamp
        Type   = $etype
        Reason = $reason
        ObjKind= $e.involvedObject.kind
        ObjNS  = $e.involvedObject.namespace
        ObjName= $e.involvedObject.name
        Msg    = $msg
      }
    }
  }
}
if ($bad.Count -gt 0) {
  $bad | Select-Object -Last 30 | Format-Table -AutoSize
} else {
  Write-Host "No recent warnings/errors."
}

# 6) Resource usage (optional)
Out-Section "Resource Usage (kubectl top) - optional"
try {
  if ($Namespace -and $Namespace.Trim() -ne "") {
    Exec-Text "kubectl top pods -n $Namespace" -IgnoreExit | Write-Host
  } else {
    Exec-Text "kubectl top nodes" -IgnoreExit | Write-Host
    Exec-Text "kubectl top pods -A" -IgnoreExit | Write-Host
  }
} catch {
  Write-Host "kubectl top not available (metrics-server may be missing)."
}

# 7) Save report
Out-Section "Saving report"
$ts   = (Get-Date).ToString("yyyyMMdd-HHmmss")
$base = "gke-health-$($Cluster)-$ts"

$report = [pscustomobject]@{
  Project      = $project
  Cluster      = $Cluster
  Region       = $Region
  Autopilot    = $autopilot
  PrivateNodes = $private
  ReleaseChan  = $releaseCh
  Versions     = @{ Master=$masterVer; Node=$nodeVer }
  Network      = @{ VPC=$net; Subnet=$subnet }
  MAN          = @{ Enabled=$manEnabled; CIDRs=$manCidrs }
  Nodes        = $nodeRows
  PodPhase     = $counts
  ProblemPods  = $problemPods
  CronJobs     = $cronRows
  Events       = $bad
}

$report | ConvertTo-Json -Depth 10 | Out-File "$base.json" -Encoding UTF8

# Markdown summary (ASCII only to avoid encoding issues on WinPS 5.x)
$mdLines = @()
$mdLines += "# GKE Health Report - $Cluster ($Region)"
$mdLines += "**Project:** $project"
$mdLines += "**Timestamp:** $(Get-Date -Format s)"
$mdLines += ""
$mdLines += "## Cluster"
$mdLines += "Status: $status"
$mdLines += "Autopilot: $autopilot"
$mdLines += "Private Nodes: $private"
$mdLines += "Release Channel: $releaseCh"
$mdLines += "Versions: master=$masterVer, node=$nodeVer"
$mdLines += "Network: $net / $subnet"
$mdLines += ("MAN: enabled={0}, CIDRs={1}" -f $manEnabled, ($manCidrs -join ", "))
$mdLines += ""
$mdLines += "## Pod Phases"
$mdLines += (($counts.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ", ")
$mdLines += ""
$mdLines += "## CronJobs (last 5 jobs)"
if ($cronRows -and $cronRows.Count -gt 0) {
  foreach ($row in $cronRows) {
    $line = ("- {0}/{1} - schedule={2}, suspend={3}, last={4}, active={5}, recent OK={6}, recent FAIL={7}" -f `
      $row.Namespace, $row.CronJob, $row.Schedule, $row.Suspend, $row.LastSchedule, $row.ActiveJobs, $row.RecentSuccess, $row.RecentFailed)
    $mdLines += $line
  }
} else {
  $mdLines += "- (none)"
}

$mdText = [string]::Join("`r`n", $mdLines)
$mdText | Out-File "$base.md" -Encoding UTF8

Write-Host ("Saved: {0}.json, {0}.md" -f $base)
Write-Host "Done."
