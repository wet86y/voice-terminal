param(
  [string]$Python = "python",
  [string]$Config = "config/local.yaml",
  [switch]$NoWorker,
  [switch]$NoAnalysis
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Push-Location $RepoRoot
$worker = $null
$analysisWorker = $null
try {
  $serviceConfig = & $Python -c "import json,sys; from pathlib import Path; from voice_terminal.config import load_config; from urllib.parse import urlparse; c=load_config(Path('config/default.yaml'),Path(sys.argv[1])); a=urlparse(c.v02.analysis_url); w=urlparse(c.asr.worker_url); print(json.dumps({'analysis_enabled':c.v02.enabled and (c.v02.smart_turn or c.v02.sensevoice),'analysis_smart_turn':c.v02.enabled and c.v02.smart_turn,'analysis_sensevoice':c.v02.enabled and c.v02.sensevoice,'analysis_health':'http://'+a.netloc+'/health','asr_url':c.asr.worker_url,'asr_host':w.hostname,'asr_port':w.port}))" $Config
  if ($LASTEXITCODE -ne 0) { throw "无法读取启动配置" }
  $serviceConfig = $serviceConfig | ConvertFrom-Json
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

  & $Python -m apps.server.main --config $Config
  exit $LASTEXITCODE
} finally {
  if ($analysisWorker -and -not $analysisWorker.HasExited) {
    Stop-Process -Id $analysisWorker.Id -Force -ErrorAction SilentlyContinue
  }
  if ($worker -and -not $worker.HasExited) {
    Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue
  }
  Pop-Location
}
