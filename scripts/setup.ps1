param(
  [string]$Python = "python",
  [string]$ModelDir = "D:\模型\voice-terminal",
  [switch]$Offline,
  [switch]$SkipInstall,
  [switch]$SkipDsh,
  [switch]$Analysis,
  [switch]$Gpu,
  [switch]$NoDownloadProxy
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Requirements = Join-Path $RepoRoot "apps\server\requirements.txt"
if ($Analysis) { $Requirements = Join-Path $RepoRoot "apps\server\requirements-analysis.txt" }
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
  Invoke-Python -m pip install --upgrade pip
  if ($Gpu) {
    Write-Host "Installing PyTorch CUDA wheels..." -ForegroundColor Cyan
    Invoke-Python -m pip install torch==2.11.0+cu128 torchaudio==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
  } else {
    Write-Host "Installing default PyTorch CPU wheels..." -ForegroundColor Cyan
    Invoke-Python -m pip uninstall -y torch torchaudio
    Invoke-Python -m pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cpu
  }
  Write-Host "Installing Voice Server dependencies..." -ForegroundColor Cyan
  if (-not $Gpu) {
    # GPU wheels share the same Python import package names. Remove the optional
    # CUDA build first so reinstalling the default CPU profile is deterministic.
    Invoke-Python -m pip uninstall -y onnxruntime-gpu
  }
  Invoke-Python -m pip install -r $Requirements
  if ($Gpu) {
    Write-Host "Installing optional ONNX Runtime and Sherpa-ONNX CUDA backends..." -ForegroundColor Cyan
    Invoke-Python -m pip uninstall -y onnxruntime onnxruntime-gpu sherpa-onnx
    Invoke-Python -m pip install onnxruntime-gpu==1.24.3
    Invoke-Python -m pip install --find-links https://k2-fsa.github.io/sherpa/onnx/cuda.html "sherpa-onnx==1.13.8+cuda12.cudnn9"
  }
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

if ($Analysis) {
  $analysisArgs = @("-m", "voice_terminal.analysis.model_setup", "--model-dir", $ModelDir)
  if ($Offline) { $analysisArgs += "--offline" }
  if ($NoDownloadProxy) { $analysisArgs += "--no-proxy" }
  Invoke-Python @analysisArgs
}

if (-not $SkipDsh) {
  $dshArgs = @()
  if ($Offline) { $dshArgs += "-Offline" }
  if ($SkipInstall) { $dshArgs += "-SkipInstall" }
  & (Join-Path $PSScriptRoot "setup-dsh.ps1") @dshArgs
  if ($LASTEXITCODE -ne 0) { throw "DSH setup failed with exit code $LASTEXITCODE" }
}

Write-Host "Setup complete. Next: .\scripts\doctor.ps1 -Python `"$Python`"" -ForegroundColor Green
