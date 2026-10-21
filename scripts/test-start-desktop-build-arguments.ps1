$ErrorActionPreference='Stop'
$TaskRepo=Split-Path $PSScriptRoot -Parent
$TaskStart=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'start-desktop.ps1') -Raw -Encoding UTF8
if ($TaskStart -notmatch "\.local'" -or $TaskStart -notmatch 'VoiceTerminal.exe' -or $TaskStart -notmatch '--source-root' -or $TaskStart -notmatch '--dev') {throw '正式启动及显式调试参数未收敛。'}
if ($TaskStart -match 'LaunchChannel|--install|--data-dir|--python|--webview2') {throw '正式入口仍存在开发路径覆盖。'}
$TaskBuild=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'build-desktop.ps1') -Raw -Encoding UTF8
if ($TaskBuild -notmatch "ValidateSet\('product','installer'\)" -or $TaskBuild -match 'LaunchChannel') {throw '构建职责尚未分离。'}
if ($TaskBuild -match 'workspace-assets\.py' -or $TaskStart -match 'workspace-assets\.py') {throw '普通启动或构建不得自动运行容量扫描。'}
Write-Host '正式入口、显式调试、构建职责参数检查通过。'
$TaskPublish=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'publish-desktop.ps1') -Raw -Encoding UTF8
if ($TaskPublish -notmatch 'if \(\$BuildDemo\)\s*\{\s*&[^\r\n]+-OptimizationLevel debug' -or $TaskPublish -notmatch '-OptimizationLevel release') {throw '正式覆盖发布必须默认只构建正式版，演示构建需显式选择。'}
Write-Host '常规覆盖发布与按需演示构建参数检查通过。'
