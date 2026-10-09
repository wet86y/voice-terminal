param([string]$Root=$PSScriptRoot)
$ErrorActionPreference='Stop'
$PackageRoot=[IO.Path]::GetFullPath($Root)
& (Join-Path $PackageRoot 'VoiceTerminal.exe') '--verify-package'
if ($LASTEXITCODE) {throw '正式本体校验失败。'}
Write-Host '正式入口、宿主、业务与界面文件校验通过；无需安装后端环境。' -ForegroundColor Green
