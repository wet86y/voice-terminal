param(
  [switch]$Debug,[switch]$SkipFrontend,
  [ValidateSet('product','installer')][string]$BuildTarget='product', # BuildTarget（入口职责构建目标）。
  [ValidateSet('debug','release')][string]$OptimizationLevel='release',
  [switch]$Qualification,[switch]$DesktopOnly,[switch]$BootstrapOnly,
  [string]$ProgramPackage='' # ProgramPackage（安装器嵌入的统一正式程序包）。
)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
. (Join-Path $PSScriptRoot 'download-sources.ps1')
$DesktopRepo=Get-ProjectRoot
if ($args.Count) { throw '构建脚本不接受产品运行参数。' }
if ($Debug) { $OptimizationLevel='debug' }
if ($DesktopOnly -and $BootstrapOnly) { throw '不能同时指定仅构建宿主和仅构建根入口。' }
if ($Qualification -and $BuildTarget -ne 'installer') { throw '自动验收仅用于安装器；正式程序始终使用同一构建。' }
if ($BuildTarget -eq 'installer' -and -not (Test-Path -LiteralPath $ProgramPackage -PathType Leaf)) { throw '安装器必须嵌入已组装的正式程序包。' }
$SavedTarget=$env:CARGO_TARGET_DIR;$SavedCargo=$env:CARGO_HOME;$SavedRustup=$env:RUSTUP_HOME;$SavedFlags=$env:RUSTFLAGS;$SavedEncoded=$env:CARGO_ENCODED_RUSTFLAGS;$SavedPackage=$env:VOICE_PROGRAM_PACKAGE;$SavedEmbedded=$env:VOICE_EMBEDDED_APP
try {
  $CargoCommand=Join-Path $DesktopRepo '.local/tools/rust/cargo/bin/cargo.exe'
  $env:CARGO_HOME=Join-Path $DesktopRepo '.local/tools/rust/cargo';$env:RUSTUP_HOME=Join-Path $DesktopRepo '.local/tools/rust/rustup'
  $CacheTarget=if ($Qualification) {'installer-qualification'} else {$BuildTarget}
  $OutputRoot=Join-Path $DesktopRepo ".local/build/desktop/$CacheTarget"
  $env:CARGO_TARGET_DIR=Join-Path $DesktopRepo ".local/build/desktop/product" # 两个职责复用原生依赖编译缓存。
  $env:RUSTFLAGS=($SavedFlags+' -C target-feature=+crt-static').Trim();$env:CARGO_ENCODED_RUSTFLAGS=$null
  $env:VOICE_PROGRAM_PACKAGE=if ($ProgramPackage) {[IO.Path]::GetFullPath($ProgramPackage)} else {$null}
  $env:VOICE_EMBEDDED_APP=$null # 正式程序不嵌入自身；旧分发路径不再参与构建。
  Push-Location $DesktopRepo
  try {
    & (Join-Path $PSScriptRoot 'prepare-update-kit.ps1');if ($LASTEXITCODE) {throw '下载组件准备失败。'}
    if ($BuildTarget -eq 'product' -and -not $SkipFrontend -and -not $BootstrapOnly) { & npm run frontend:build;if ($LASTEXITCODE) {throw '界面构建失败。'} }
    [string[]]$ProfileArgs=@();if ($OptimizationLevel -eq 'release') {$ProfileArgs=@('--release')}
    $Built=@()
    if ($BuildTarget -eq 'installer') {
      $Features='voice-terminal-installer/custom-protocol';if ($Qualification) {$Features+=',voice-terminal-installer/qualification'}
      Invoke-SourceCargo -Executable $CargoCommand -Arguments (@('build','--manifest-path','apps/desktop/Cargo.toml','-p','voice-terminal-installer','--features',$Features,'--locked') + $ProfileArgs)
      if ($LASTEXITCODE) {throw '独立安装器构建失败。'};$Built=@('installer')
    } else {
      if (-not $BootstrapOnly) {
        Invoke-SourceCargo -Executable $CargoCommand -Arguments (@('build','--manifest-path','apps/desktop/Cargo.toml','-p','voice-terminal-desktop','--features','voice-terminal-desktop/custom-protocol','--locked') + $ProfileArgs)
        if ($LASTEXITCODE) {throw '统一正式宿主构建失败。'};$Built+='desktop'
      }
      if (-not $DesktopOnly) {
        Invoke-SourceCargo -Executable $CargoCommand -Arguments (@('build','--manifest-path','apps/desktop/Cargo.toml','-p','voice-terminal-bootstrap','--features','voice-terminal-bootstrap/custom-protocol','--locked') + $ProfileArgs)
        if ($LASTEXITCODE) {throw '统一正式入口构建失败。'};$Built+='bootstrap'
      }
      if ($OptimizationLevel -eq 'debug' -and -not $BootstrapOnly) {
        $DemoCache=Join-Path $DesktopRepo '.local/build/desktop/debug';New-Item -ItemType Directory -Path $DemoCache -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $env:CARGO_TARGET_DIR 'debug/VoiceTerminalDesktop.exe') -Destination (Join-Path $DemoCache 'VoiceTerminalDesktop.exe') -Force
      }
    }
    $Stamp=[ordered]@{schema=2;version=(Get-Content package.json -Raw -Encoding UTF8 | ConvertFrom-Json).version;role=$BuildTarget;optimization_level=$OptimizationLevel;qualification=[bool]$Qualification;built_components=$Built;program_package_sha256=$(if ($ProgramPackage) {Get-Sha256File -LiteralPath $ProgramPackage} else {$null});fields=@{schema='统一构建戳格式';version='产品版本';role='product（正式程序）或 installer（安装器）';optimization_level='debug（调试）或 release（优化）';qualification='是否开放隔离自动验收';built_components='已构建入口职责';program_package_sha256='安装器内嵌统一包的 SHA-256（摘要）'}}
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
    if ($BuildTarget -eq 'installer') {
      $InstallerOutput=Join-Path $OutputRoot $OptimizationLevel;New-Item -ItemType Directory -Path $InstallerOutput -Force | Out-Null
      Copy-Item -LiteralPath (Join-Path $env:CARGO_TARGET_DIR "$OptimizationLevel/VoiceTerminalSetup.exe") -Destination (Join-Path $InstallerOutput 'VoiceTerminalSetup.exe') -Force
    }
    [IO.File]::WriteAllText((Join-Path $OutputRoot 'build-stamp.json'),($Stamp | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
    Write-Host "构建完成：$DesktopRepo/.local/build/desktop/$CacheTarget/$OptimizationLevel" -ForegroundColor Green
  } finally {Pop-Location}
} finally {$env:CARGO_TARGET_DIR=$SavedTarget;$env:CARGO_HOME=$SavedCargo;$env:RUSTUP_HOME=$SavedRustup;$env:RUSTFLAGS=$SavedFlags;$env:CARGO_ENCODED_RUSTFLAGS=$SavedEncoded;$env:VOICE_PROGRAM_PACKAGE=$SavedPackage;$env:VOICE_EMBEDDED_APP=$SavedEmbedded}
