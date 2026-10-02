param([string]$Python = "python", [switch]$SkipWebBuild)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BuildEnvironment = Join-Path $RepoRoot ".runtime\launcher-build"
$BuildPython = Join-Path $BuildEnvironment "Scripts\python.exe"
Push-Location $RepoRoot
try {
  if (-not (Test-Path -LiteralPath $BuildPython)) {
    & $Python -m venv $BuildEnvironment
    if ($LASTEXITCODE -ne 0) { throw "无法建立独立启动程序打包环境" }
  }
  & $BuildPython -m pip install -r (Join-Path $PSScriptRoot "requirements-launcher.txt")
  if ($LASTEXITCODE -ne 0) { throw "启动程序打包依赖安装失败" }
  if (-not $SkipWebBuild) {
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw "网页构建失败" }
  }
  & $BuildPython -m PyInstaller --noconfirm --clean --onedir --windowed --name VoiceTerminal --paths $RepoRoot --distpath (Join-Path $RepoRoot ".runtime\launcher") --workpath (Join-Path $RepoRoot ".runtime\launcher-build\work") --specpath (Join-Path $RepoRoot ".runtime\launcher-build") (Join-Path $PSScriptRoot "launcher-entry.py")
  if ($LASTEXITCODE -ne 0) { throw "独立启动程序打包失败" }
  Write-Host "可双击启动：$RepoRoot\.runtime\launcher\VoiceTerminal\VoiceTerminal.exe"
} finally {
  Pop-Location
}
