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
  $analysisConfig = & $Python -c "import json,sys; from pathlib import Path; from voice_terminal.config import load_config; from urllib.parse import urlparse; c=load_config(Path('config/default.yaml'),Path(sys.argv[1])); u=urlparse(c.v02.analysis_url); print(json.dumps({'enabled':c.v02.enabled and (c.v02.smart_turn or c.v02.sensevoice),'health':'http://'+u.netloc+'/health'}))" $Config
  if ($LASTEXITCODE -ne 0) { throw "无法读取启动配置" }
  $analysisConfig = $analysisConfig | ConvertFrom-Json
  if ($analysisConfig.enabled -and -not $NoAnalysis) {
    $existingAnalysis = $null
    try { $existingAnalysis = Invoke-RestMethod -Uri $analysisConfig.health -TimeoutSec 2 } catch { }
    if (-not $existingAnalysis) {
      $analysisWorker = Start-Process -FilePath $Python -ArgumentList @("-m", "apps.server.analysis_worker", "--config", $Config) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "analysis-worker.out.log") -RedirectStandardError (Join-Path $LogDir "analysis-worker.err.log") -PassThru
      for ($attempt = 0; $attempt -lt 180; $attempt++) {
        if ($analysisWorker.HasExited) { break }
        try {
          $existingAnalysis = Invoke-RestMethod -Uri $analysisConfig.health -TimeoutSec 1
          break
        } catch { Start-Sleep -Seconds 1 }
      }
    }
    if (-not $existingAnalysis) { Write-Warning "Analysis Worker 不可用，Voice Server 将使用 VAD / Paraformer 降级。" }
  }
  if (-not $NoWorker) {
    $workerOut = Join-Path $LogDir "asr-worker.out.log"
    $workerErr = Join-Path $LogDir "asr-worker.err.log"
    $worker = Start-Process -FilePath $Python -ArgumentList @("-m", "apps.server.asr_worker", "--config", $Config) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
      Start-Sleep -Milliseconds 500
      if ($worker.HasExited) { break }
      try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $client.Connect("127.0.0.1", 8766)
        $client.Close()
        $ready = $true
        break
      } catch { }
    }
    if (-not $ready) {
      $details = if (Test-Path -LiteralPath $workerErr) { Get-Content -Raw -LiteralPath $workerErr } else { "无 ASR Worker 日志" }
      throw "ASR Worker 未能在 60 秒内启动。`n$details"
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
