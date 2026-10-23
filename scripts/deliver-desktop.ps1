param([string]$InstallRoot='') # InstallRoot（本机更新目标，默认固定工作区）。
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'desktop-pipeline.ps1')
Enter-DesktopPipeline
try {
  # 同一进程持有构建锁：准备一次产品包，安装器和本机消费同一包。
  & (Join-Path $PSScriptRoot 'distribute-desktop.ps1')
  if ($LASTEXITCODE) {throw '分发产物准备失败。'}
  & (Join-Path $PSScriptRoot 'publish-desktop.ps1') -SkipBuild -InstallRoot $InstallRoot
  if ($LASTEXITCODE) {throw '同包本机更新失败。'}
} finally {Exit-DesktopPipeline}
