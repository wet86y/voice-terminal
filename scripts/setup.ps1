param(
  [string]$Python = 'python', [string]$ModelDir = '', [switch]$Offline, [switch]$SkipInstall,
  [switch]$SkipDsh, [switch]$Analysis, [switch]$Gpu, [switch]$NoDownloadProxy,
  [string]$EnvironmentDir = '', [switch]$OnnxVad, [switch]$TorchGpu, [string]$VcRedist = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$RepoRoot = Get-ProjectRoot
$Context = Get-LocalContext
Set-LocalEnvironment $Context $RepoRoot
if ($ModelDir -and [IO.Path]::GetFullPath($ModelDir) -ne $Context.Models) { throw '模型位置统一由便携配置管理。' }
if ($EnvironmentDir -and [IO.Path]::GetFullPath($EnvironmentDir) -ne (Split-Path $Context.Python)) { throw '不再创建独立业务虚拟环境；新组件在临时区构建并通过发布事务换入。' }
if (-not $SkipInstall) {
  if (-not $VcRedist) { throw '重新构建运行组件需要 -VcRedist（已授权的微软原生运行库目录）；已有环境校验使用 -SkipInstall。' }
  $Candidate = Join-Path $RepoRoot '.local/tmp/python-candidate'
  $Profile = if ($Gpu -or $TorchGpu) { 'cu128' } else { 'cpu' }
  $BuildArgs = @('-B', '-X', 'utf8', (Join-Path $PSScriptRoot 'build-runtime.py'), '--profile', $Profile, '--output', $Candidate, '--vc-redist', $VcRedist)
  if ($Offline) { $BuildArgs += '--offline' }
  & $Python @BuildArgs
  if ($LASTEXITCODE) { throw '新运行组件构建失败；正式环境保持原状。' }
  & (Join-Path $PSScriptRoot 'publish-desktop.ps1') -PythonRuntime $Candidate
  & $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'local-admin.py') 'reclaim-candidates'
} else {
  & $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'backend-entry.py') 'voice_terminal.dependencies'
  if ($LASTEXITCODE) { throw '当前运行组件依赖校验失败。' }
}
if (-not $SkipDsh) { & (Join-Path $PSScriptRoot 'setup-dsh.ps1') -SkipInstall -Offline:$Offline }
Write-Host '当前唯一业务环境已校验；模型组件变更通过覆盖发布参数提交。' -ForegroundColor Green
