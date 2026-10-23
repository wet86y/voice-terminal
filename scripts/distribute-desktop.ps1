param([switch]$Qualification,[switch]$SkipProductBuild,[switch]$SkipInstallerBuild)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'desktop-pipeline.ps1')
Enter-DesktopPipeline
try {
$DesktopRepo=Get-ProjectRoot;$Context=Get-LocalContext
if (-not $SkipProductBuild) {
  & (Join-Path $PSScriptRoot 'prepare-desktop-product.ps1')
  if ($LASTEXITCODE) {throw '正式程序包准备失败。'}
}
Assert-DesktopPackage
$Package=Join-Path $DesktopRepo '.local/build/program-package/program.zip'
if (-not (Test-Path -LiteralPath $Package -PathType Leaf)) {throw '缺少统一正式程序包，请先发布或构建。'}
& (Join-Path $PSScriptRoot 'build-desktop.ps1') -BuildTarget installer -ProgramPackage $Package -Qualification:$Qualification -RequireExisting:$SkipInstallerBuild
if ($LASTEXITCODE) {throw '独立分发安装器构建失败。'}
$Cache=if ($Qualification) {'installer-qualification'} else {'installer'}
$StampPath=Join-Path $DesktopRepo ".local/build/desktop/$Cache/build-stamp.json"
$Stamp=Get-Content -LiteralPath $StampPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Stamp.role -ne 'installer' -or $Stamp.program_package_sha256 -ne (Get-Sha256File -LiteralPath $Package)) {throw '安装器内嵌包与当前统一程序包不一致。'}
$Output=Join-Path $DesktopRepo ".local/releases/$Cache";New-Item -ItemType Directory -Path $Output -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $DesktopRepo ".local/build/desktop/$Cache/release/VoiceTerminalSetup.exe") -Destination (Join-Path $Output 'VoiceTerminalSetup.exe') -Force
Copy-Item -LiteralPath $StampPath -Destination (Join-Path $Output 'build-stamp.json') -Force
$Info=Get-Content (Join-Path $DesktopRepo '.local/build/program-package/package-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Delivery=[ordered]@{schema=3;version=$Info.version;build=$Info.build;role='installer';entry='VoiceTerminalSetup.exe';size=(Get-Item -LiteralPath (Join-Path $Output 'VoiceTerminalSetup.exe')).Length;sha256=(Get-Sha256File -LiteralPath (Join-Path $Output 'VoiceTerminalSetup.exe'));program_package_sha256=$Info.sha256;fields=@{schema='独立安装器交付格式';version='产品版本';build='正式程序构建编号';role='installer（分发安装器）';entry='交付入口';size='安装器字节数';sha256='安装器 SHA-256（摘要）';program_package_sha256='与本地发布共用的正式程序包摘要'}}
[IO.File]::WriteAllText((Join-Path $Output 'delivery.json'),($Delivery | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
Write-Host "分发安装器：$Output/VoiceTerminalSetup.exe" -ForegroundColor Green
} finally {Exit-DesktopPipeline}
