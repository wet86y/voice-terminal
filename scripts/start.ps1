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
  [Parameter(HelpMessage="DataDir（独立用户数据目录）")]
  [string]$DataDir = ".runtime/desktop-data"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
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
  "-m", "voice_terminal.application.launch",
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
