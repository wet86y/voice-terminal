param(
  [string]$Python = "python",
  [string]$Config = "config/local.yaml",
  [switch]$NoWorker
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Push-Location $RepoRoot
$worker = $null
try {
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
  if ($worker -and -not $worker.HasExited) {
    Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue
  }
  Pop-Location
}
