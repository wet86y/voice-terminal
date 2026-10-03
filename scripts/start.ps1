param(
  [string]$Python = "",
  [string]$Config = "config/local.yaml",
  [ValidateSet("deepseek", "opencode_go")]
  [string]$LlmProvider = "",
  [switch]$NoWorker,
  [switch]$NoAnalysis,
  [switch]$NoWeb,
  [switch]$Dev,
  [switch]$NoTray,
  [switch]$NoBrowser
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
$Arguments = @("-m", "voice_terminal.application.launch", "--root", $RepoRoot, "--python", $RuntimePython, "--config", $Config)
if ($LlmProvider) { $Arguments += @("--llm-provider", $LlmProvider) }
if ($NoWorker) { $Arguments += "--no-worker" }
if ($NoAnalysis) { $Arguments += "--no-analysis" }
if ($NoWeb) { $Arguments += "--no-web" }
if ($Dev) { $Arguments += "--dev" }
if ($NoTray) { $Arguments += "--no-tray" }
if ($NoBrowser) { $Arguments += "--no-browser" }
Push-Location $RepoRoot
try {
  & $RuntimePython @Arguments
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
