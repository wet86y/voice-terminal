param([string]$InstallRoot='', [string]$Build='', [switch]$SkipBuild, [switch]$BuildDemo) # BuildDemo（显式更新本机演示宿主）。
$ErrorActionPreference='Stop'
if ($SkipBuild -and $BuildDemo) {throw '复用已有构建与重建演示不能同时指定。'}
. (Join-Path $PSScriptRoot 'desktop-pipeline.ps1')
Enter-DesktopPipeline
try {
$DesktopRepo=Get-ProjectRoot
if (-not $InstallRoot) {$InstallRoot=Join-Path $DesktopRepo '.local'}
$InstallRoot=[IO.Path]::GetFullPath($InstallRoot)
$Context=Get-LocalContext
if (-not $SkipBuild) {
  & (Join-Path $PSScriptRoot 'prepare-desktop-product.ps1') -Build $Build -BuildDemo:$BuildDemo
  if ($LASTEXITCODE) {throw '正式程序包准备失败。'}
}
Assert-DesktopPackage -Build $Build
$Package=Join-Path $DesktopRepo '.local/build/program-package/program.zip'
$PackageHash=Get-Sha256File -LiteralPath $Package
# 本体替换前通过现有实例的正式退出流程停机，模型与用户内容不参与替换。
& $Context.Python '-B' '-X' 'utf8' '-c' 'import sys; sys.path.insert(0,sys.argv[2]); from pathlib import Path; from voice_terminal.distribution import require_stopped; root=Path(sys.argv[1]); require_stopped(root, root)' $InstallRoot $DesktopRepo
if ($LASTEXITCODE) {throw '现有正式程序未能安全退出。'}
$Helper=Join-Path $DesktopRepo '.local/build/desktop/product/release/VoiceTerminal.exe'
$InstallArguments=@('--install-package',$Package,'--target-root',$InstallRoot)
Invoke-DesktopStage '安装已有程序包与强制兼容处理' {Invoke-DesktopInstallProcess -Executable $Helper -Arguments $InstallArguments}
if ((Get-Sha256File -LiteralPath $Package) -ne $PackageHash) {throw '安装期间程序包发生变化，拒绝将本轮标记为同包交付。'}
if ($BuildDemo -and $InstallRoot -eq (Join-Path $DesktopRepo '.local')) {
  Copy-Item -LiteralPath (Join-Path $DesktopRepo '.local/build/desktop/product/release/VoiceTerminalUIDemo.exe') -Destination (Join-Path $InstallRoot 'VoiceTerminalUIDemo.exe') -Force
}
Write-Host "正式入口：$InstallRoot/VoiceTerminal.exe" -ForegroundColor Green
Write-Host '本地发布与安装器共用同一程序包及兼容性组件；有效资源已复用，配置、故障资料及必需组件已完成强制处理。' -ForegroundColor Green
} finally {Exit-DesktopPipeline}
