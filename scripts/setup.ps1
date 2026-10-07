param(
  [string]$Python = "python",
  [string]$ModelDir = "D:\模型\voice-terminal",
  [switch]$Offline,
  [switch]$SkipInstall,
  [switch]$SkipDsh,
  [switch]$Analysis,
  [switch]$Gpu,
  [switch]$NoDownloadProxy,
  [string]$EnvironmentDir = "",
  [switch]$OnnxVad,
  [switch]$TorchGpu
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Requirements = Join-Path $RepoRoot "apps\server\requirements.txt"
if ($Analysis) { $Requirements = Join-Path $RepoRoot "apps\server\requirements-analysis.txt" }
if ($OnnxVad) { $Requirements = Join-Path $RepoRoot "apps\server\requirements-vad-onnx.txt" }
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

if (-not $SkipInstall) {
  if (-not $EnvironmentDir) { $EnvironmentDir = Join-Path $RepoRoot ".runtime\environments\runtime-candidate" }
  $EnvironmentDir = [IO.Path]::GetFullPath($EnvironmentDir)
  $ManagedPython = Join-Path $EnvironmentDir "Scripts\python.exe"
  if (-not (Test-Path -LiteralPath $ManagedPython)) {
    if ($Offline) { throw "离线安装需要已有业务环境：$EnvironmentDir" }
    & $Python -m venv $EnvironmentDir
    if ($LASTEXITCODE -ne 0) { throw "创建独立业务环境失败" }
  }
  $Python = $ManagedPython
  $ReadyMarker = Join-Path $EnvironmentDir "voice-runtime-ready.json"
  if (Test-Path -LiteralPath $ReadyMarker) { Remove-Item -LiteralPath $ReadyMarker }
}

if (-not $Offline -and -not $SkipInstall) {
  Invoke-Python -m pip install --upgrade pip
  if ($Gpu -or $TorchGpu) {
    Write-Host "Installing PyTorch CUDA wheels..." -ForegroundColor Cyan
    Invoke-Python -m pip install -r (Join-Path $RepoRoot "apps\server\requirements-torch-cu128.txt")
  } else {
    Write-Host "Installing default PyTorch CPU wheels..." -ForegroundColor Cyan
    Invoke-Python -m pip uninstall -y torch torchaudio
    Invoke-Python -m pip install -r (Join-Path $RepoRoot "apps\server\requirements-torch-cpu.txt")
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
    Invoke-Python -m pip install onnxruntime-gpu==1.24.4
    Invoke-Python -m pip install --find-links https://k2-fsa.github.io/sherpa/onnx/cuda.html "sherpa-onnx==1.13.8+cuda12.cudnn9"
  }
}

if (-not $SkipInstall) {
  # dependency_errors（依赖闭包检查）识别同模块的显卡替代包，同时拒绝重叠安装。
  Invoke-Python -c "from voice_terminal.dependencies import dependency_errors; errors=dependency_errors(); print('\n'.join(errors) if errors else '依赖闭包校验通过'); raise SystemExit(bool(errors))"
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

if ($Analysis -or $OnnxVad) {
  $analysisArgs = @("-m", "voice_terminal.analysis.model_setup", "--model-dir", $ModelDir)
  if ($Offline) { $analysisArgs += "--offline" }
  if ($NoDownloadProxy) { $analysisArgs += "--no-proxy" }
  if ($Gpu) { $analysisArgs += "--smart-turn-gpu" }
  Invoke-Python @analysisArgs
}

if ($OnnxVad) {
  $VadModelDir = Join-Path $ModelDir "funasr\fsmn-vad"
  if (-not (Test-Path -LiteralPath (Join-Path $VadModelDir "onnx\manifest.json"))) {
    Invoke-Python (Join-Path $RepoRoot "scripts\export_vad_onnx.py") --model-dir $VadModelDir
  }
  Invoke-Python -c "import sys; from pathlib import Path; from voice_terminal.vad.onnx_backend import OnnxStreamingModel; OnnxStreamingModel(Path(sys.argv[1])); print('轻量检测模型校验通过')" $VadModelDir
}

if (-not $SkipInstall) {
  $dependencyArgs = @("-m", "voice_terminal.dependencies")
  if ($OnnxVad) { $dependencyArgs += "--candidate" }
  Invoke-Python @dependencyArgs
  # 安装期间不自动切换正在运行的宿主；新启动只选已完成版本校验的业务环境。
  Set-Content -LiteralPath (Join-Path $EnvironmentDir "voice-runtime-ready.json") -Encoding utf8 -Value '{"python":"3.12","validated":true}'
}

if (-not $SkipDsh) {
  $dshArgs = @()
  if ($Offline) { $dshArgs += "-Offline" }
  if ($SkipInstall) { $dshArgs += "-SkipInstall" }
  & (Join-Path $PSScriptRoot "setup-dsh.ps1") @dshArgs
  if ($LASTEXITCODE -ne 0) { throw "DSH setup failed with exit code $LASTEXITCODE" }
}

Write-Host "Setup complete. Next: .\scripts\doctor.ps1 -Python `"$Python`"" -ForegroundColor Green
