param(
  [string]$Python = "python",
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
