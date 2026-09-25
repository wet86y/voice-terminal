param(
  [string]$ModelDir = "D:\模型\voice-terminal\cosyvoice3-0.5b-2512"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot ".runtime\cosyvoice"
$ToolsDir = Join-Path $RuntimeRoot "tools"
$SourceDir = Join-Path $RuntimeRoot "source"
$EnvDir = Join-Path $RuntimeRoot "env"
$AssetDir = Join-Path $RuntimeRoot "asset"
$UpstreamCommit = "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
$UvVersion = "0.12.17"
$UvExe = Join-Path $ToolsDir "uv.exe"

if (-not (Test-Path -LiteralPath (Join-Path $ModelDir "cosyvoice3.yaml"))) {
  throw "CosyVoice 模型目录缺少 cosyvoice3.yaml：$ModelDir"
}
New-Item -ItemType Directory -Force -Path $RuntimeRoot, $ToolsDir, $AssetDir | Out-Null

if (-not (Test-Path -LiteralPath $UvExe)) {
  $ZipPath = Join-Path $RuntimeRoot "uv.zip"
  $UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
  Invoke-WebRequest -Uri $UvUrl -OutFile $ZipPath
  Expand-Archive -LiteralPath $ZipPath -DestinationPath $ToolsDir -Force
  Remove-Item -LiteralPath $ZipPath
}

$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot "python"
$env:UV_PYTHON_BIN_DIR = Join-Path $RuntimeRoot "python-bin"
$env:UV_CACHE_DIR = Join-Path $RuntimeRoot "cache"
& $UvExe python install 3.10
if ($LASTEXITCODE -ne 0) { throw "项目内 Python 3.10 安装失败。" }
if (-not (Test-Path -LiteralPath (Join-Path $EnvDir "Scripts\python.exe"))) {
  & $UvExe venv --python 3.10 --python-preference only-managed $EnvDir
  if ($LASTEXITCODE -ne 0) { throw "CosyVoice 独立环境创建失败。" }
}

if (-not (Test-Path -LiteralPath (Join-Path $SourceDir ".git"))) {
  git clone --recurse-submodules https://github.com/QwenAudio/CosyVoice.git $SourceDir
  if ($LASTEXITCODE -ne 0) { throw "CosyVoice 上游代码下载失败。" }
}
git -C $SourceDir checkout --detach $UpstreamCommit
if ($LASTEXITCODE -ne 0) { throw "CosyVoice 上游版本固定失败。" }
git -C $SourceDir submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { throw "CosyVoice 上游子模块下载失败。" }

$PromptWav = Join-Path $AssetDir "zero_shot_prompt.wav"
if (-not (Test-Path -LiteralPath $PromptWav)) {
  Invoke-WebRequest -Uri "https://raw.githubusercontent.com/QwenAudio/CosyVoice/$UpstreamCommit/asset/zero_shot_prompt.wav" -OutFile $PromptWav
}
if ((Get-Item -LiteralPath $PromptWav).Length -lt 1000) { throw "CosyVoice 参考 WAV 无效。" }

$PythonExe = Join-Path $EnvDir "Scripts\python.exe"
$BuildConstraints = Join-Path $ProjectRoot "apps\server\requirements-cosyvoice-build.txt"
& $UvExe pip install --python $PythonExe --index-strategy unsafe-best-match --constraint $BuildConstraints --build-constraints $BuildConstraints -r (Join-Path $SourceDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "CosyVoice 上游依赖安装失败；请查看终端输出。" }

$env:PYTHONPATH = "$SourceDir;$(Join-Path $SourceDir 'third_party\Matcha-TTS');$ProjectRoot"
& $PythonExe -c "from cosyvoice.cli.cosyvoice import AutoModel; import torch, torchaudio; print('CosyVoice 导入成功，PyTorch', torch.__version__)"
if ($LASTEXITCODE -ne 0) { throw "CosyVoice 导入检查失败。" }
Write-Host "CosyVoice 独立环境已就绪：$EnvDir" -ForegroundColor Green
