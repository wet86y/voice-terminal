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
 . (Join-Path $PSScriptRoot 'local-context.ps1')
$RepoRoot = Get-ProjectRoot
$Context = Get-LocalContext
Set-LocalEnvironment $Context $RepoRoot
if ($Python -and [IO.Path]::GetFullPath($Python) -ne $Context.Python) { throw '解释器必须与便携配置一致。' }
$RuntimePython = $Context.Python
if (-not $DataDir) { $DataDir = $Context.Data }
$ResolvedDataDir = if ([IO.Path]::IsPathRooted($DataDir)) { [IO.Path]::GetFullPath($DataDir) } else { [IO.Path]::GetFullPath((Join-Path $RepoRoot $DataDir)) }
$Arguments = @(
  "-X", "utf8", "-X", "pycache_prefix=$($Context.Root)/cache/python",
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
