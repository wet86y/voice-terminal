param(
  [string]$Python = "python",
  [string]$ModelDir = "D:\模型\voice-terminal",
  [switch]$Offline,
  [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Requirements = Join-Path $RepoRoot "apps\server\requirements.txt"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$RepoRoot;$env:PYTHONPATH" } else { $RepoRoot }

function Invoke-Python {
  & $Python @args
  if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}

if (-not (Test-Path -LiteralPath $RepoRoot)) { throw "仓库目录不存在：$RepoRoot" }
New-Item -ItemType Directory -Force -Path $ModelDir, (Join-Path $ModelDir "cache\modelscope"), (Join-Path $ModelDir "cache\huggingface"), (Join-Path $ModelDir "cache\torch"), (Join-Path $ModelDir "manifests") | Out-Null
$env:PIP_CACHE_DIR = Join-Path $ModelDir "cache\pip"

$version = (& $Python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')").Trim()
if ($LASTEXITCODE -ne 0 -or $version -ne "3.12") { throw "需要 Python 3.12，当前为：$version。可使用 -Python D:\DevTools\Python312\python.exe" }

if (-not $Offline -and -not $SkipInstall) {
  Write-Host "Installing PyTorch CUDA wheels..." -ForegroundColor Cyan
  Invoke-Python -m pip install --upgrade pip
  Invoke-Python -m pip install torch==2.11.0+cu128 torchaudio==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
  Write-Host "Installing Voice Server dependencies..." -ForegroundColor Cyan
  Invoke-Python -m pip install -r $Requirements
}

$env:MODELSCOPE_CACHE = Join-Path $ModelDir "cache\modelscope"
$env:HF_HOME = Join-Path $ModelDir "cache\huggingface"
$env:TORCH_HOME = Join-Path $ModelDir "cache\torch"

if ($Offline) {
  Invoke-Python -m voice_terminal.model_setup --model-dir $ModelDir --offline
} else {
  Write-Host "Downloading and validating real KWS / Paraformer / FSMN-VAD models..." -ForegroundColor Cyan
  Push-Location $RepoRoot
  try {
    Invoke-Python -m voice_terminal.model_setup --model-dir $ModelDir
  } finally {
    Pop-Location
  }
}

Write-Host "Setup complete. Next: .\scripts\doctor.ps1 -Python `"$Python`"" -ForegroundColor Green
