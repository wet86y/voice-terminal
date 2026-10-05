param(
  [Parameter(HelpMessage="Python（业务程序运行环境）路径")]
  [string]$Python = "",
  [Parameter(HelpMessage="Config（本机应用配置）路径")]
  [string]$Config = "config/local.yaml",
  [ValidateSet("deepseek", "opencode_go")]
  [Parameter(HelpMessage="LlmProvider（大语言模型服务商）")]
  [string]$LlmProvider = "",
  [Parameter(HelpMessage="NoWorker（不启动语音识别工作进程）")]
  [switch]$NoWorker,
  [Parameter(HelpMessage="NoAnalysis（不启动分析工作进程）")]
  [switch]$NoAnalysis,
  [Parameter(HelpMessage="DataDir（业务数据目录，默认沿用正式程序数据）")]
  [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$InstallRoot = Join-Path $RepoRoot '.runtime/delivery/VoiceTerminal'
if (-not $DataDir) {
  $PortablePath = Join-Path $InstallRoot 'portable.json'
  if (-not (Test-Path -LiteralPath $PortablePath)) { throw '缺少正式程序数据；请先准备正式程序，或使用 -DataDir（业务数据目录）明确指定。' }
  $Portable = Get-Content -LiteralPath $PortablePath -Raw -Encoding utf8 | ConvertFrom-Json
  $DataDir = if ([IO.Path]::IsPathRooted($Portable.data_dir)) { $Portable.data_dir } else { Join-Path $InstallRoot $Portable.data_dir }
  $ActivePath = Join-Path $DataDir 'state/active.json'
  $Selected = if (Test-Path -LiteralPath $ActivePath) { Get-Content -LiteralPath $ActivePath -Raw -Encoding utf8 | ConvertFrom-Json } else { $Portable }
  if (-not $Python) { $Python = if ($Selected.external_python) { $Selected.external_python } else { Join-Path $InstallRoot $Selected.python } }
  $env:VOICE_INSTALL_ROOT = $InstallRoot
  $env:VOICE_MODEL_DIR = if ($Selected.external_models) { $Selected.external_models } else { Join-Path $InstallRoot $Selected.models }
  if ($Selected.dsh) { $env:VOICE_DSH_RUNTIME = Join-Path $InstallRoot $Selected.dsh }
  if ($Selected.node) { $env:VOICE_NODE_EXECUTABLE = Join-Path $InstallRoot $Selected.node }
}
$RuntimeProfile = Join-Path $RepoRoot ".runtime\application\runtime.json"
if (-not $Python -and (Test-Path -LiteralPath $RuntimeProfile)) {
  $Profile = Get-Content -LiteralPath $RuntimeProfile -Raw -Encoding utf8 | ConvertFrom-Json
  if (-not ($Profile.python -is [string]) -or -not $Profile.python.Trim()) { throw "已激活的业务环境记录缺少路径" }
  $Python = if ([IO.Path]::IsPathRooted($Profile.python)) { $Profile.python } else { Join-Path $RepoRoot $Profile.python }
  $Python = [IO.Path]::GetFullPath($Python)
  $ReadyMarker = Join-Path (Split-Path -Parent (Split-Path -Parent $Python)) "voice-runtime-ready.json"
  if (-not (Test-Path -LiteralPath $Python) -or -not (Test-Path -LiteralPath $ReadyMarker)) { throw "已激活的业务环境不可用；使用 -Python（明确指定程序运行环境）回退" }
  $Ready = Get-Content -LiteralPath $ReadyMarker -Raw -Encoding utf8 | ConvertFrom-Json
  if ($Ready.validated -isnot [bool] -or -not $Ready.validated) { throw "已激活的业务环境尚未完成依赖校验" }
}
if (-not $Python) { $Python = "python" }
$RuntimePython = (Get-Command $Python -ErrorAction Stop).Source
$ResolvedDataDir = if ([IO.Path]::IsPathRooted($DataDir)) { [IO.Path]::GetFullPath($DataDir) } else { [IO.Path]::GetFullPath((Join-Path $RepoRoot $DataDir)) }
$Arguments = @(
  "-X", "utf8", "-X", "pycache_prefix=$ResolvedDataDir/cache/python",
  (Join-Path $RepoRoot 'scripts/backend-entry.py'), "voice_terminal.application.launch",
  "--root", $RepoRoot,
  "--python", $RuntimePython,
  "--config", $Config,
  "--data-dir", $ResolvedDataDir,
  "--headless"
)
if ($LlmProvider) { $Arguments += @("--llm-provider", $LlmProvider) }
if ($NoWorker) { $Arguments += "--no-worker" }
if ($NoAnalysis) { $Arguments += "--no-analysis" }
Push-Location $RepoRoot
try {
  & $RuntimePython @Arguments
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
