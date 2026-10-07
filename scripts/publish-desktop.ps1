param([string]$InstallRoot = '', [string]$Build = '', [string]$PythonRuntime = '', [string]$ModelsRoot = '')
$ErrorActionPreference = 'Stop'
$DesktopRepo = Split-Path -Parent $PSScriptRoot
if (-not $InstallRoot) { $InstallRoot = Join-Path $DesktopRepo '.runtime/delivery/VoiceTerminal' }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot 'portable.json'))) {
  throw '正式安装目录缺少 portable.json（便携配置）。请先准备完整正式程序；此命令只更新已有正式程序，不创建空白数据候选。'
}
if (-not $Build) { $Build = 'desktop-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') }
$Portable = Get-Content -LiteralPath (Join-Path $InstallRoot 'portable.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Data = if ([IO.Path]::IsPathRooted($Portable.data_dir)) { $Portable.data_dir } else { Join-Path $InstallRoot $Portable.data_dir }
$ActivePath = Join-Path $Data 'state/active.json'
$Selected = if (Test-Path -LiteralPath $ActivePath) { Get-Content -LiteralPath $ActivePath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $Portable }
$BuildPython = if ($Selected.external_python) { $Selected.external_python } else { Join-Path $InstallRoot $Selected.python }
if (-not (Test-Path -LiteralPath $BuildPython)) { throw '正式程序的 Python（后端运行时）不可用。' }
$Demo = Join-Path $DesktopRepo '.runtime/delivery/VoiceTerminalUIDemo.exe'
$DebugDesktop = Join-Path $DesktopRepo 'apps/desktop/target/debug/VoiceTerminalDesktop.exe'
$Running = Get-CimInstance Win32_Process | Where-Object {
  $_.ExecutablePath -and ($_.ExecutablePath.StartsWith($InstallRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
    $_.ExecutablePath -eq $Demo -or $_.ExecutablePath -eq $DebugDesktop)
}
if ($Running) { throw '请先从正式程序中执行“退出应用”，并关闭前端演示窗口，再更新正式程序。' }
# 先构建正式产物和演示使用的调试宿主，再停机校验、更新原正式目录。
& node (Join-Path $DesktopRepo 'integrations/dsh/voice-terminal-shadow-bridge/build.mjs')
if ($LASTEXITCODE) { throw 'DSH（代理执行宿主）协作桥接插件构建失败。' }
& node (Join-Path $DesktopRepo 'integrations/dsh/voice-terminal-wake-tasks/build.mjs')
if ($LASTEXITCODE) { throw 'DSH（代理执行宿主）任务插件构建失败。' }
& (Join-Path $PSScriptRoot 'build-desktop.ps1')
& (Join-Path $PSScriptRoot 'build-desktop.ps1') -Debug -SkipFrontend
Push-Location $DesktopRepo
try {
  $AssembleArgs = @('-B', '-X', 'utf8', 'scripts/assemble-desktop.py', '--output', $InstallRoot, '--build', $Build, '--refresh-program')
  if ($PythonRuntime) { $AssembleArgs += @('--python-runtime', $PythonRuntime) } # PythonRuntime（新程序运行环境组件）。
  if ($ModelsRoot) { $AssembleArgs += @('--models', $ModelsRoot) } # ModelsRoot（含显卡与中央处理器模型的完整资源目录）。
  & $BuildPython @AssembleArgs
  if ($LASTEXITCODE) { throw '正式程序更新失败；请查看组装诊断。' }
  Copy-Item -LiteralPath (Join-Path $DesktopRepo 'apps/desktop/target/release/VoiceTerminalUIDemo.exe') -Destination $Demo -Force
  Write-Host "正式程序：$InstallRoot/VoiceTerminal.exe" -ForegroundColor Green
  Write-Host "前端演示：$Demo" -ForegroundColor Green
  Write-Host '正式程序继续使用完整原有数据；演示修改通过本命令更新到正式程序。' -ForegroundColor Green
} finally { Pop-Location }
