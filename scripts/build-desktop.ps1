param(
  [switch]$Debug,
  [switch]$SkipFrontend,
  [ValidateSet('development', 'distribution')][string]$LaunchChannel = 'development',
  [string]$OptimizationLevel = '',
  [switch]$Qualification,
  [switch]$DesktopOnly,
  [switch]$BootstrapOnly,
  [string]$DistributionCatalog = '',
  [string]$EmbeddedApp = '' # EmbeddedApp（内嵌本体）：分发渠道使用。
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$DesktopRepo = Get-ProjectRoot
$BuildVersion = (Get-Content -LiteralPath (Join-Path $DesktopRepo 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
$ForbiddenBuildFlags = @('--ui-preview', '--dev', '--diagnostic', '--dev-server', '--frontend-dev')
foreach ($Argument in $args) {
  if ($ForbiddenBuildFlags -contains $Argument) {
    throw "构建脚本不接受运行参数：$Argument"
  }
}
if ($Debug) {
  if ($OptimizationLevel) { throw '请在 -Debug 与 -OptimizationLevel 之间只选择一个优化级别。' }
  $OptimizationLevel = 'debug'
}
if (-not $OptimizationLevel) { $OptimizationLevel = 'release' }
if ($OptimizationLevel -notin @('debug', 'release')) { throw '优化级别只能是 debug（调试）或 release（优化）。' }
if ($Qualification -and $LaunchChannel -ne 'distribution') { throw 'Qualification（本地验收）构建必须使用 distribution（分发）渠道。' }
if ($DesktopOnly -and $BootstrapOnly) { throw '-DesktopOnly 与 -BootstrapOnly 不能同时使用。' }
if ($DistributionCatalog -and $LaunchChannel -ne 'distribution') { throw '自定义分发清单只能用于 distribution（分发）渠道。' }
if ($DistributionCatalog -and -not (Test-Path -LiteralPath $DistributionCatalog -PathType Leaf)) {
  throw "找不到分发清单：$DistributionCatalog"
}

$LocalCargo = Join-Path $DesktopRepo '.local/tools/rust/cargo/bin/cargo.exe'
$CargoCommand = if (Test-Path -LiteralPath $LocalCargo) { $LocalCargo } else { (Get-Command cargo -ErrorAction Stop).Source }
$CacheChannel = if ($Qualification) { 'distribution-qualification' } else { $LaunchChannel }
$SavedTargetDir = $env:CARGO_TARGET_DIR
$env:CARGO_TARGET_DIR = Join-Path $DesktopRepo ".local/build/desktop/$CacheChannel"
$SavedCargoHome = $env:CARGO_HOME
$SavedRustupHome = $env:RUSTUP_HOME
$SavedRustFlags = $env:RUSTFLAGS
$SavedEncodedRustFlags = $env:CARGO_ENCODED_RUSTFLAGS
$SavedDistributionCatalog = $env:VOICE_DISTRIBUTION_CATALOG
$SavedEmbeddedApp = $env:VOICE_EMBEDDED_APP
try {
  if ($CargoCommand -eq $LocalCargo) {
    $env:CARGO_HOME = Join-Path $DesktopRepo '.local/tools/rust/cargo'
    $env:RUSTUP_HOME = Join-Path $DesktopRepo '.local/tools/rust/rustup'
  }
  $CatalogPath = if ($DistributionCatalog) { [IO.Path]::GetFullPath($DistributionCatalog) } else { Join-Path $DesktopRepo 'config/distribution.json' }
  if (-not (Test-Path -LiteralPath $CatalogPath -PathType Leaf)) { throw "找不到待嵌入的分发清单：$CatalogPath" }
  $env:VOICE_DISTRIBUTION_CATALOG = $CatalogPath
  if ($EmbeddedApp) {
    if ($LaunchChannel -ne 'distribution' -or -not (Test-Path -LiteralPath $EmbeddedApp -PathType Leaf)) { throw '内嵌本体须属于分发渠道且文件存在。' }
    $env:VOICE_EMBEDDED_APP = [IO.Path]::GetFullPath($EmbeddedApp)
  } else { $env:VOICE_EMBEDDED_APP = $null }
  Push-Location $DesktopRepo
  try {
    $Context = Get-LocalContext
    Set-LocalEnvironment $Context $DesktopRepo
    $Toolchain = (& $CargoCommand -V) + ' / ' + (& (Join-Path (Split-Path $CargoCommand) 'rustc.exe') -V)
    & $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'prepare-build-cache.py') 'desktop' '--toolchain' $Toolchain
    if ($LASTEXITCODE) { throw '原生构建缓存检查失败。' }
    # DesktopUpdateKit（桌面下载组件）由开发与分发渠道共用，固定到锁定提交。
    & (Join-Path $PSScriptRoot 'prepare-update-kit.ps1')
    if ($LASTEXITCODE) { throw '桌面下载组件准备失败。' }
    if (-not $SkipFrontend -and -not $BootstrapOnly) { & npm run frontend:build; if ($LASTEXITCODE) { throw '前端构建失败。' } }

    $Profile = $OptimizationLevel
    $BuildProfileArgs = if ($OptimizationLevel -eq 'release') { @('--release') } else { @() }
    $ChannelFeature = if ($Qualification) { 'qualification' } elseif ($LaunchChannel -eq 'distribution') { 'distribution' } else { '' }
    $DesktopFeatures = @('voice-terminal-desktop/custom-protocol')
    $BootstrapFeatures = @()
    if ($ChannelFeature) {
      $DesktopFeatures += "voice-terminal-desktop/$ChannelFeature"
      $BootstrapFeatures += "voice-terminal-bootstrap/$ChannelFeature"
    }

    if (-not $BootstrapOnly) {
      # 单 EXE（入口）桌面宿主静态链接 C++（原生）运行库。
      if ($SavedEncodedRustFlags) { $env:CARGO_ENCODED_RUSTFLAGS = $SavedEncodedRustFlags + [char]31 + '-C' + [char]31 + 'target-feature=+crt-static' }
      else { $env:RUSTFLAGS = ($SavedRustFlags + ' -C target-feature=+crt-static').Trim() }
      $DesktopArguments = @('build', '--manifest-path', 'apps/desktop/Cargo.toml', '--package', 'voice-terminal-desktop', '--features', ($DesktopFeatures -join ','), '--locked') + $BuildProfileArgs
      & $CargoCommand @DesktopArguments
      if ($LASTEXITCODE) { throw '桌面程序构建失败。' }
      # VoiceTerminalUIDemo（界面演示入口）的旧路径保留为兼容副本；正式缓存仍按渠道隔离。
      if ($LaunchChannel -eq 'development' -and $OptimizationLevel -eq 'debug') {
        $LegacyDebug = Join-Path $DesktopRepo '.local/build/desktop/debug'
        New-Item -ItemType Directory -Path $LegacyDebug -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $env:CARGO_TARGET_DIR "$Profile/VoiceTerminalDesktop.exe") -Destination (Join-Path $LegacyDebug 'VoiceTerminalDesktop.exe') -Force
      }
    }

    if (-not $DesktopOnly) {
      # 根入口需要在没有系统 C++（原生运行库）的机器上启动，单独静态链接其运行库。
      if ($SavedEncodedRustFlags) {
        $env:CARGO_ENCODED_RUSTFLAGS = $SavedEncodedRustFlags + [char]31 + '-C' + [char]31 + 'target-feature=+crt-static'
      } else {
        $env:RUSTFLAGS = ($SavedRustFlags + ' -C target-feature=+crt-static').Trim()
      }
      $BootstrapArguments = @('build', '--manifest-path', 'apps/desktop/Cargo.toml', '--package', 'voice-terminal-bootstrap', '--locked')
      if ($BootstrapFeatures.Count) { $BootstrapArguments += @('--features', ($BootstrapFeatures -join ',')) }
      $BootstrapArguments += $BuildProfileArgs
      & $CargoCommand @BootstrapArguments
      if ($LASTEXITCODE) { throw '独立根入口构建失败。' }
    }

    $StampPath = Join-Path $env:CARGO_TARGET_DIR 'build-stamp.json'
    $BuiltComponents = @()
    if (-not $BootstrapOnly) { $BuiltComponents += 'desktop' }
    if (-not $DesktopOnly) { $BuiltComponents += 'bootstrap' }
    if (Test-Path -LiteralPath $StampPath) {
      try {
        $PreviousStamp = Get-Content -LiteralPath $StampPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($PreviousStamp.version -eq $BuildVersion -and $PreviousStamp.launch_channel -eq $LaunchChannel -and
            $PreviousStamp.optimization_level -eq $OptimizationLevel -and
            [bool]$PreviousStamp.qualification -eq [bool]$Qualification) {
          $BuiltComponents += @($PreviousStamp.built_components)
        }
      } catch { }
    }
    $BuiltComponents = @($BuiltComponents | Sort-Object -Unique)
    $CatalogHash = (Get-Sha256File -LiteralPath $CatalogPath)
    $Stamp = [ordered]@{
      schema = 1
      version = $BuildVersion # version（已构建源码版本）。
      launch_channel = $LaunchChannel # launch_channel（启动渠道，与优化级别分离）。
      optimization_level = $OptimizationLevel # optimization_level（优化级别）。
      qualification = [bool]$Qualification # qualification（本地验收构建标记）。
      built_components = $BuiltComponents # built_components（本轮缓存已构建目标）。
      embedded_app_sha256 = $(if ($EmbeddedApp) { (Get-Sha256File -LiteralPath $EmbeddedApp) } else { $null }) # embedded_app_sha256（内嵌本体摘要）。
      catalog_sha256 = $CatalogHash # catalog_sha256（嵌入分发清单摘要）。
      cargo_target_dir = ".local/build/desktop/$CacheChannel" # cargo_target_dir（渠道固定编译缓存目录）。
      fields = @{
        schema = '构建戳格式'
        launch_channel = 'development（开发）或 distribution（分发），与优化级别独立'
        optimization_level = 'debug（调试）或 release（优化）'
        qualification = '是否允许本地 Qualification（验收）参数'
        built_components = '已构建的桌面主程序与根启动入口'
        catalog_sha256 = '嵌入分发清单的 SHA-256（摘要）'
        cargo_target_dir = '本渠道固定编译缓存目录'
      }
    }
    $StampJson = ConvertTo-Json -InputObject $Stamp -Depth 6
    [IO.File]::WriteAllText($StampPath, $StampJson + "`n", [Text.UTF8Encoding]::new($false))
    Write-Host "桌面渠道：$LaunchChannel；优化级别：$OptimizationLevel；Qualification：$([bool]$Qualification)" -ForegroundColor Green
    Write-Host "桌面产物：$DesktopRepo/.local/build/desktop/$CacheChannel/$Profile" -ForegroundColor Green
  } finally { Pop-Location }
} finally {
  $env:CARGO_TARGET_DIR = $SavedTargetDir
  $env:CARGO_HOME = $SavedCargoHome; $env:RUSTUP_HOME = $SavedRustupHome
  $env:RUSTFLAGS = $SavedRustFlags; $env:CARGO_ENCODED_RUSTFLAGS = $SavedEncodedRustFlags
  $env:VOICE_DISTRIBUTION_CATALOG = $SavedDistributionCatalog
  $env:VOICE_EMBEDDED_APP = $SavedEmbeddedApp
}
