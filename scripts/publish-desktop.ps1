param([string]$InstallRoot='', [string]$Build='', [switch]$SkipBuild)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$DesktopRepo=Get-ProjectRoot
if (-not $InstallRoot) {$InstallRoot=Join-Path $DesktopRepo '.local'}
$InstallRoot=[IO.Path]::GetFullPath($InstallRoot)
$Context=Get-LocalContext
if (-not $SkipBuild) {
  & (Join-Path $PSScriptRoot 'prepare-7zip.ps1');if ($LASTEXITCODE) {throw '压缩工具准备失败。'}
  & $Context.Node (Join-Path $DesktopRepo 'integrations/dsh/voice-terminal/build.mjs');if ($LASTEXITCODE) {throw '代理插件构建失败。'}
  # 本地演示单独构建，使用隔离缓存；不进入正式程序包或用户工作区。
  & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BuildTarget product -OptimizationLevel debug -DesktopOnly -SkipFrontend
  if ($LASTEXITCODE) {throw '本地演示宿主构建失败。'}
  & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BuildTarget product -OptimizationLevel release
  if ($LASTEXITCODE) {throw '正式程序构建失败。'}
}
$PackageArgs=@('-B','-X','utf8',(Join-Path $PSScriptRoot 'assemble-program-package.py'))
if ($Build) {$PackageArgs+=@('--build',$Build)}
& $Context.Python @PackageArgs;if ($LASTEXITCODE) {throw '统一正式程序包组装失败。'}
# 本体替换前通过现有实例的正式退出流程停机，模型与用户内容不参与替换。
& $Context.Python '-B' '-X' 'utf8' '-c' 'import sys; sys.path.insert(0,sys.argv[2]); from pathlib import Path; from voice_terminal.distribution import require_stopped; root=Path(sys.argv[1]); require_stopped(root, root)' $InstallRoot $DesktopRepo
if ($LASTEXITCODE) {throw '现有正式程序未能安全退出。'}
$Helper=Join-Path $DesktopRepo '.local/build/desktop/product/release/VoiceTerminal.exe'
& $Helper '--install-package' (Join-Path $DesktopRepo '.local/build/program-package/program.zip') '--target-root' $InstallRoot
if ($LASTEXITCODE) {throw '正式本体事务替换失败；原程序已恢复或保留恢复日志。'}
if ($InstallRoot -eq (Join-Path $DesktopRepo '.local')) {
  Copy-Item -LiteralPath (Join-Path $DesktopRepo '.local/build/desktop/product/release/VoiceTerminalUIDemo.exe') -Destination (Join-Path $InstallRoot 'VoiceTerminalUIDemo.exe') -Force
}
Write-Host "正式入口：$InstallRoot/VoiceTerminal.exe" -ForegroundColor Green
Write-Host '本地发布与安装器共用同一程序包；数据、模型、运行环境及登记已保留。' -ForegroundColor Green
