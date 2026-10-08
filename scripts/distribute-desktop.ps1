param(
  [string]$SourceBase = '', # SourceBase（后续更新地址）：本体首次展开不需要该地址。
  [ValidateSet('debug', 'release')][string]$OptimizationLevel = 'release',
  [switch]$Qualification,
  [switch]$ReuseQualificationBase, # 兼容旧调用；不收集完整运行环境。
  [switch]$SkipDesktopBuild
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$DesktopRepo = Get-ProjectRoot
$Context = Get-LocalContext
Set-LocalEnvironment $Context $DesktopRepo
if (-not $SkipDesktopBuild) {
  & (Join-Path $PSScriptRoot 'prepare-7zip.ps1')
  if ($LASTEXITCODE) { throw '7-Zip（原生压缩工具）准备失败。' }
  if (-not (Test-Path -LiteralPath (Join-Path $DesktopRepo 'config/runtime/voice-runtime.lock.json'))) {
    & $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'lock-voice-runtime.py')
    if ($LASTEXITCODE) { throw '固定语音依赖锁定失败。' }
  }
  & npm run frontend:build
  if ($LASTEXITCODE) { throw '前端构建失败。' }
  & $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'assemble-embedded-app.py')
  if ($LASTEXITCODE) { throw '内嵌本体组装失败。' }
  $BuildArguments = @{ LaunchChannel = 'distribution'; OptimizationLevel = $OptimizationLevel; Qualification = [bool]$Qualification; DesktopOnly = $true; SkipFrontend = $true; EmbeddedApp = (Join-Path $DesktopRepo '.local/build/embedded-app/app.zip') }
  & (Join-Path $PSScriptRoot 'build-desktop.ps1') @BuildArguments
  if ($LASTEXITCODE) { throw '单 EXE（程序入口）构建失败。' }
}
$Channel = if ($Qualification) { 'distribution-qualification' } else { 'distribution' }
$OutputName = if ($Qualification) { 'qualification-single-exe' } else { 'distribution-single-exe' }
$TargetRoot = Join-Path $DesktopRepo ".local/build/desktop/$Channel/$OptimizationLevel"
$StampPath = Join-Path $DesktopRepo ".local/build/desktop/$Channel/build-stamp.json"
$Stamp = Get-Content -LiteralPath $StampPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Payload = Join-Path $DesktopRepo '.local/build/embedded-app/app.zip'
if ($Stamp.launch_channel -ne 'distribution' -or [bool]$Stamp.qualification -ne [bool]$Qualification -or $Stamp.embedded_app_sha256 -ne (Get-Sha256File -LiteralPath $Payload)) { throw '渠道或内嵌本体与构建戳不匹配。' }
$Output = Join-Path $DesktopRepo ".local/releases/$OutputName"
New-Item -ItemType Directory -Path $Output -Force | Out-Null
$OutputFull = [IO.Path]::GetFullPath($Output)
$AllowedParent = [IO.Path]::GetFullPath((Join-Path $DesktopRepo '.local/releases'))
if ([IO.Path]::GetDirectoryName($OutputFull) -ne $AllowedParent -or ((Get-Item -LiteralPath $OutputFull).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '分发输出目录越界或为链接。' }
Copy-Item -LiteralPath (Join-Path $TargetRoot 'VoiceTerminalDesktop.exe') -Destination (Join-Path $OutputFull 'VoiceTerminal.exe') -Force
Copy-Item -LiteralPath $StampPath -Destination (Join-Path $OutputFull 'build-stamp.json') -Force
$Delivery = [ordered]@{ schema = 2; version = '0.9.0'; launch_channel = 'distribution'; qualification = [bool]$Qualification; entry = 'VoiceTerminal.exe'; size = (Get-Item -LiteralPath (Join-Path $OutputFull 'VoiceTerminal.exe')).Length; sha256 = (Get-Sha256File -LiteralPath (Join-Path $OutputFull 'VoiceTerminal.exe')); embedded_app_sha256 = $Stamp.embedded_app_sha256; future_source_base = $SourceBase; fields = @{ schema = '单入口交付记录格式'; version = '程序版本'; launch_channel = '分发启动渠道'; qualification = '本地隔离验收产物'; entry = '公开交付入口，仅需分发此文件'; size = '入口字节数'; sha256 = '入口摘要'; embedded_app_sha256 = '内嵌本体摘要'; future_source_base = '预留后续更新发布地址，首次展开不使用' } }
[IO.File]::WriteAllText((Join-Path $OutputFull 'delivery.json'), ($Delivery | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
Write-Host "分发产物：$OutputFull/VoiceTerminal.exe；单 EXE（程序入口）内含本体，无 Python（后端环境）、模型或 DSH（代理宿主）。" -ForegroundColor Green
