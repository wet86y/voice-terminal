param(
  [string]$Python = "python",
  [string]$Config = "config/local.yaml",
  [ValidateSet("deepseek", "opencode_go")]
  [string]$LlmProvider = "",
  [switch]$NoWorker,
  [switch]$NoAnalysis,
  [switch]$NoWeb
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Push-Location $RepoRoot
$worker = $null
$analysisWorker = $null
$web = $null
$webPort = 5173
$webUrl = "http://127.0.0.1:$webPort/"
$previousLlmProvider = $env:VOICE_LLM_PROVIDER
$llmProviderOverridden = $false
if ($LlmProvider) {
  $env:VOICE_LLM_PROVIDER = $LlmProvider
  $llmProviderOverridden = $true
  Write-Host "LLM provider override: $LlmProvider"
}

function Get-ListeningProcessDetails([int]$Port) {
  $listenerProcessIds = @(
    Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique
  )
  foreach ($listenerProcessId in $listenerProcessIds) {
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerProcessId" -ErrorAction SilentlyContinue
    if ($processInfo) {
      [PSCustomObject]@{
        Id = $listenerProcessId
        CommandLine = $processInfo.CommandLine
        CreationDate = $processInfo.CreationDate
      }
    } else {
      [PSCustomObject]@{ Id = $listenerProcessId; CommandLine = "unknown"; CreationDate = "unknown" }
    }
  }
}

function Test-HttpReady([string]$Uri) {
  try {
    $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 2 -UseBasicParsing
    return $response.StatusCode -ge 200 -and $response.StatusCode -lt 400
  } catch {
    return $false
  }
}

function Stop-ProcessTree([int]$ProcessId) {
  $children = @(
    Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty ProcessId
  )
  foreach ($childId in $children) {
    Stop-ProcessTree $childId
  }
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

try {
  $serviceConfig = & $Python -c "import json,sys; from pathlib import Path; from voice_terminal.config import load_config; from urllib.parse import urlparse; c=load_config(Path('config/default.yaml'),Path(sys.argv[1]) if Path(sys.argv[1]).is_file() else None); a=urlparse(c.smart_turn.analysis_url); w=urlparse(c.asr.worker_url); print(json.dumps({'analysis_enabled':c.smart_turn.smart_turn or c.smart_turn.sensevoice,'analysis_smart_turn':c.smart_turn.smart_turn,'analysis_sensevoice':c.smart_turn.sensevoice,'analysis_health':'http://'+a.netloc+'/health','asr_url':c.asr.worker_url,'asr_host':w.hostname,'asr_port':w.port,'server_host':c.server.host,'server_port':c.server.port}))" $Config
  if ($LASTEXITCODE -ne 0) { throw "无法读取启动配置" }
  $serviceConfig = $serviceConfig | ConvertFrom-Json
  $existingServer = @(Get-ListeningProcessDetails ([int]$serviceConfig.server_port))
  if ($existingServer.Count -gt 0) {
    $details = $existingServer | ForEach-Object { "PID=$($_.Id) started=$($_.CreationDate) command=$($_.CommandLine)" }
    throw "Voice Server 端口 $($serviceConfig.server_port) 已有监听进程。为防止前端连接旧后端，启动已中止。`n$($details -join [Environment]::NewLine)"
  }
  if ($serviceConfig.analysis_enabled -and -not $NoAnalysis) {
    $existingAnalysis = $null
    try { $existingAnalysis = Invoke-RestMethod -Uri $serviceConfig.analysis_health -TimeoutSec 2 } catch { }
    if (-not $existingAnalysis) {
      $analysisWorker = Start-Process -FilePath $Python -ArgumentList @("-m", "apps.server.analysis_worker", "--config", $Config) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "analysis-worker.out.log") -RedirectStandardError (Join-Path $LogDir "analysis-worker.err.log") -PassThru
      for ($attempt = 0; $attempt -lt 180; $attempt++) {
        if ($analysisWorker.HasExited) { break }
        try {
          $existingAnalysis = Invoke-RestMethod -Uri $serviceConfig.analysis_health -TimeoutSec 1
          break
        } catch { Start-Sleep -Seconds 1 }
      }
    }
    $analysisReady = $true
    if (-not $existingAnalysis) { $analysisReady = $false }
    if ($analysisReady -and $serviceConfig.analysis_smart_turn -and $existingAnalysis.endpoint.status -ne "READY") { $analysisReady = $false }
    if ($analysisReady -and $serviceConfig.analysis_sensevoice -and $existingAnalysis.refine.status -ne "READY") { $analysisReady = $false }
    if (-not $analysisReady) { Write-Warning "Analysis Worker 不可用或模型未 READY，Voice Server 将使用 VAD / Paraformer 降级。" }
  }
  $workerOut = Join-Path $LogDir "asr-worker.out.log"
  $workerErr = Join-Path $LogDir "asr-worker.err.log"
  $probeOutput = & $Python -m voice_terminal.asr.probe --url $serviceConfig.asr_url --timeout 2 2>&1
  $probeExit = $LASTEXITCODE
  if ($probeExit -eq 0) {
    Write-Host "ASR Worker 已就绪：$serviceConfig.asr_url"
  } elseif ($NoWorker) {
    throw "指定了 -NoWorker，但 ASR Worker 未通过应用层 health：$serviceConfig.asr_url`n$probeOutput"
  } else {
    $portInUse = $false
    if ($serviceConfig.asr_host -and $serviceConfig.asr_port) {
      try {
        $portInUse = (Test-NetConnection -ComputerName $serviceConfig.asr_host -Port ([int]$serviceConfig.asr_port) -WarningAction SilentlyContinue -InformationLevel Quiet)
      } catch { $portInUse = $false }
    }
    if ($portInUse) {
      throw "ASR Worker 地址已有服务但应用层 health 未 READY：$serviceConfig.asr_url`n$probeOutput"
    }
    $worker = Start-Process -FilePath $Python -ArgumentList @("-m", "apps.server.asr_worker", "--config", $Config) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
      Start-Sleep -Milliseconds 500
      if ($worker.HasExited) { break }
      $probeOutput = & $Python -m voice_terminal.asr.probe --url $serviceConfig.asr_url --timeout 2 2>&1
      if ($LASTEXITCODE -eq 0) {
        $ready = $true
        break
      }
    }
    if (-not $ready) {
      $details = if (Test-Path -LiteralPath $workerErr) { Get-Content -Raw -LiteralPath $workerErr } else { "无 ASR Worker 日志" }
      throw "ASR Worker 未能通过应用层 health（60 秒）：$serviceConfig.asr_url`n探针：$probeOutput`n$details"
    }
  }

  if (-not $NoWeb) {
    $existingWeb = @(Get-ListeningProcessDetails $webPort)
    if ($existingWeb.Count -gt 0) {
      $details = $existingWeb | ForEach-Object { "PID=$($_.Id) started=$($_.CreationDate) command=$($_.CommandLine)" }
      throw "Web 前端端口 $webPort 已有监听进程。为防止继续使用未知或陈旧的前端构建，启动已中止。`n$($details -join [Environment]::NewLine)"
    }
    $npmCommand = Get-Command npm.cmd -ErrorAction Stop
    $webOut = Join-Path $LogDir "web-dev-current.out.log"
    $webErr = Join-Path $LogDir "web-dev-current.err.log"
    $web = Start-Process -FilePath $npmCommand.Source -ArgumentList @("run", "dev") -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $webOut -RedirectStandardError $webErr -PassThru
    $webReady = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
      Start-Sleep -Milliseconds 500
      if ($web.HasExited) { break }
      if (Test-HttpReady $webUrl) {
        $webReady = $true
        break
      }
    }
    if (-not $webReady) {
      $details = @()
      if (Test-Path -LiteralPath $webErr) { $details += Get-Content -Raw -LiteralPath $webErr }
      if (Test-Path -LiteralPath $webOut) { $details += Get-Content -Raw -LiteralPath $webOut }
      if ($details.Count -eq 0) { $details = @("无 Web 前端日志") }
      throw "Web 前端未能在 30 秒内通过 HTTP health：$webUrl`n$($details -join [Environment]::NewLine)"
    }
    Write-Host "Web 前端已就绪：$webUrl"
  }

  & $Python -m apps.server.main --config $Config
  exit $LASTEXITCODE
} finally {
  if ($llmProviderOverridden) {
    if ($null -eq $previousLlmProvider) {
      Remove-Item Env:VOICE_LLM_PROVIDER -ErrorAction SilentlyContinue
    } else {
      $env:VOICE_LLM_PROVIDER = $previousLlmProvider
    }
  }
  if ($analysisWorker -and -not $analysisWorker.HasExited) {
    Stop-Process -Id $analysisWorker.Id -Force -ErrorAction SilentlyContinue
  }
  if ($worker -and -not $worker.HasExited) {
    Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue
  }
  if ($web -and -not $web.HasExited) {
    Stop-ProcessTree $web.Id
  }
  Pop-Location
}
