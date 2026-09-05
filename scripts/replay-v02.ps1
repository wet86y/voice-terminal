param(
  [string]$Python = "python",
  [string]$Config = "config/v0.2.yaml",
  [Parameter(Mandatory = $true)]
  [string[]]$Audio,
  [int]$PauseMs = 800,
  [string]$Output = "logs/evaluation/replay-worker.json",
  [int]$ReadyTimeoutSec = 180,
  [switch]$NoWorker,
  [switch]$NoAnalysis
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Push-Location $RepoRoot
$owned = @()

function Get-ConfigInfo {
  $json = & $Python -c "import json,sys; from pathlib import Path; from voice_terminal.config import load_config; from urllib.parse import urlparse; root=Path.cwd(); c=load_config(root/'config/default.yaml',Path(sys.argv[1])); a=urlparse(c.v02.analysis_url); print(json.dumps({'asr_url':c.asr.worker_url,'analysis_url':c.v02.analysis_url,'analysis_health':'http://'+a.netloc+'/health','analysis_enabled':c.v02.enabled and (c.v02.smart_turn or c.v02.sensevoice)}))" $Config
  if ($LASTEXITCODE -ne 0) { throw "无法读取回放配置：$Config" }
  return ($json | ConvertFrom-Json)
}

function Resolve-RepoPath([string]$PathValue) {
  if ([IO.Path]::IsPathRooted($PathValue)) { return [IO.Path]::GetFullPath($PathValue) }
  return [IO.Path]::GetFullPath((Join-Path $RepoRoot $PathValue))
}

function Invoke-AsrProbe([string]$Url) {
  $value = & $Python -m voice_terminal.asr.probe --url $Url --timeout 2 2>&1
  return [pscustomobject]@{ Ready = ($LASTEXITCODE -eq 0); Output = ($value -join "`n") }
}

function Test-AnalysisReady([string]$Url) {
  try {
    $value = Invoke-RestMethod -Uri $Url -TimeoutSec 2
    return ($value.endpoint.status -eq "READY" -and $value.refine.status -eq "READY")
  } catch {
    return $false
  }
}

function Stop-OwnedProcesses {
  foreach ($process in $owned) {
    try {
      if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit(3000)
      }
    } catch { }
  }
}

try {
  $info = Get-ConfigInfo
  $configPath = Resolve-RepoPath $Config
  $outputPath = Resolve-RepoPath $Output
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null

  if (-not $NoAnalysis -and $info.analysis_enabled) {
    if (-not (Test-AnalysisReady $info.analysis_health)) {
      $analysisOut = Join-Path $LogDir "analysis-worker-replay.out.log"
      $analysisErr = Join-Path $LogDir "analysis-worker-replay.err.log"
      $process = Start-Process -FilePath $Python -ArgumentList @("-m", "apps.server.analysis_worker", "--config", $configPath) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $analysisOut -RedirectStandardError $analysisErr -PassThru
      $owned += $process
      $analysisReady = $false
      for ($attempt = 0; $attempt -lt $ReadyTimeoutSec; $attempt++) {
        if ($process.HasExited) { break }
        if (Test-AnalysisReady $info.analysis_health) { $analysisReady = $true; break }
        Start-Sleep -Seconds 1
      }
      if (-not $analysisReady) {
        $details = if (Test-Path -LiteralPath $analysisErr) { Get-Content -Raw -LiteralPath $analysisErr } else { "无 Analysis Worker 日志" }
        throw "Analysis Worker 未能在 $ReadyTimeoutSec 秒内 READY。`n$details"
      }
    }
  }

  $probe = Invoke-AsrProbe $info.asr_url
  if (-not $probe.Ready) {
    if ($NoWorker) { throw "现有 ASR Worker 未 READY，且指定了 -NoWorker。`n$($probe.Output)" }
    $workerOut = Join-Path $LogDir "asr-worker-replay.out.log"
    $workerErr = Join-Path $LogDir "asr-worker-replay.err.log"
    $process = Start-Process -FilePath $Python -ArgumentList @("-m", "apps.server.asr_worker", "--config", $configPath) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr -PassThru
    $owned += $process
    $ready = $false
    for ($attempt = 0; $attempt -lt ($ReadyTimeoutSec * 2); $attempt++) {
      if ($process.HasExited) { break }
      Start-Sleep -Milliseconds 500
      $probe = Invoke-AsrProbe $info.asr_url
      if ($probe.Ready) { $ready = $true; break }
    }
    if (-not $ready) {
      $details = if (Test-Path -LiteralPath $workerErr) { Get-Content -Raw -LiteralPath $workerErr } else { "无 ASR Worker 日志" }
      throw "ASR Worker 未能在 $ReadyTimeoutSec 秒内通过应用层 health：$info.asr_url`n探针：$($probe.Output)`n$details"
    }
  }

  $arguments = @("-m", "voice_terminal.analysis.replay", "--config", $configPath, "--asr-mode", "worker", "--asr-url", $info.asr_url, "--pause-ms", $PauseMs, "--output", $outputPath)
  foreach ($path in $Audio) { $arguments += (Resolve-RepoPath $path) }
  & $Python @arguments
  if ($LASTEXITCODE -ne 0) { throw "回放失败，报告保留在：$outputPath" }
} finally {
  Stop-OwnedProcesses
  Pop-Location
}
