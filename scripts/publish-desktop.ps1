param([string]$InstallRoot = '', [string]$Build = '', [string]$PythonRuntime = '', [string]$ModelsRoot = '', [string]$DshRuntime = '', [string]$WebView2Runtime = '')
$ErrorActionPreference = 'Stop'
 . (Join-Path $PSScriptRoot 'local-context.ps1')
$DesktopRepo = Get-ProjectRoot
if (-not $InstallRoot) { $InstallRoot = Join-Path $DesktopRepo '.local' }
if ([IO.Path]::GetFullPath($InstallRoot) -ne (Join-Path $DesktopRepo '.local')) { throw '发布使用工程内唯一 .local（本机安装目录）。' }
$env:VOICE_INSTALL_ROOT = $InstallRoot
$UpdatePython = Join-Path $InstallRoot 'tmp/update-tools/python.exe'
if (Test-Path -LiteralPath $UpdatePython) {
  & $UpdatePython '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'local-admin.py') 'recover'
  if ($LASTEXITCODE) { throw '未完成事务恢复失败；保留临时更新工具。' }
}
$Context = Get-LocalContext $InstallRoot
$InstallRoot = $Context.Root
$BuildPython = $UpdatePython
if (-not $Build) { $Build = 'desktop-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') }
& $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'prepare-update-tools.py')
if ($LASTEXITCODE) { throw '临时更新工具准备失败。' }
& $UpdatePython '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'local-admin.py') 'recover'
if ($LASTEXITCODE) { throw '上一次更新事务恢复失败。' }
# 先构建正式产物和演示使用的调试宿主，再停机校验、更新原正式目录。
& (Join-Path $PSScriptRoot 'prepare-7zip.ps1')
if ($LASTEXITCODE) { throw '完整 7-Zip（压缩工具）准备失败。' }
$Node = $Context.Node
if (-not $Node) { $Node = (Get-Command node -ErrorAction Stop).Source }
& $Node (Join-Path $DesktopRepo 'integrations/dsh/voice-terminal/build.mjs')
if ($LASTEXITCODE) { throw 'DSH（代理执行宿主）统一插件构建失败。' }
& (Join-Path $PSScriptRoot 'build-desktop.ps1') -LaunchChannel development -OptimizationLevel release
if ($LASTEXITCODE) { throw '开发渠道正式优化构建失败。' }
& (Join-Path $PSScriptRoot 'build-desktop.ps1') -LaunchChannel development -OptimizationLevel debug -SkipFrontend
if ($LASTEXITCODE) { throw '开发渠道调试构建失败。' }
Push-Location $DesktopRepo
try {
  $AssembleArgs = @('-B', '-X', 'utf8', 'scripts/assemble-desktop.py', '--output', $InstallRoot, '--build', $Build, '--refresh-program')
  if ($PythonRuntime) { $AssembleArgs += @('--python-runtime', $PythonRuntime) } # PythonRuntime（新程序运行环境组件）。
  if ($ModelsRoot) { $AssembleArgs += @('--models', $ModelsRoot) } # ModelsRoot（含显卡与中央处理器模型的完整资源目录）。
  if ($DshRuntime) { $AssembleArgs += @('--dsh-runtime', $DshRuntime) } # DshRuntime（新代理宿主组件）。
  if ($WebView2Runtime) { $AssembleArgs += @('--webview2', $WebView2Runtime) } # WebView2Runtime（新网页渲染组件）。
  & $BuildPython @AssembleArgs
  if ($LASTEXITCODE) { throw '正式程序更新失败；请查看组装诊断。' }
  & (Join-Path $InstallRoot 'runtimes/python/python.exe') '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'local-admin.py') 'reclaim-update-tools'
  if ($LASTEXITCODE) { throw '更新成功，但临时工具回收失败。' }
  Write-Host "正式程序：$InstallRoot/VoiceTerminal.exe" -ForegroundColor Green
  Write-Host "前端演示：$InstallRoot/VoiceTerminalUIDemo.exe" -ForegroundColor Green
  Write-Host '程序已覆盖更新；模型与运行依赖复用，未生成整包备份。' -ForegroundColor Green
} finally { Pop-Location }
