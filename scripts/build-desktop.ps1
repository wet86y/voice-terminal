param([switch]$Debug, [switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$DesktopRepo = Get-ProjectRoot
$ForbiddenBuildFlags = @('--ui-preview', '--dev', '--diagnostic', '--dev-server', '--frontend-dev')
foreach ($Argument in $args) {
  if ($ForbiddenBuildFlags -contains $Argument) {
    throw "构建脚本不接受运行参数：$Argument"
  }
}
$LocalCargo = Join-Path $DesktopRepo '.local/tools/rust/cargo/bin/cargo.exe'
$CargoCommand = if (Test-Path -LiteralPath $LocalCargo) { $LocalCargo } else { (Get-Command cargo -ErrorAction Stop).Source }
$SavedTargetDir = $env:CARGO_TARGET_DIR
$env:CARGO_TARGET_DIR = Join-Path $DesktopRepo '.local/build/desktop'
$SavedCargoHome = $env:CARGO_HOME
$SavedRustupHome = $env:RUSTUP_HOME
$SavedRustFlags = $env:RUSTFLAGS
$SavedEncodedRustFlags = $env:CARGO_ENCODED_RUSTFLAGS
try {
  if ($CargoCommand -eq $LocalCargo) {
    $env:CARGO_HOME = Join-Path $DesktopRepo '.local/tools/rust/cargo'
    $env:RUSTUP_HOME = Join-Path $DesktopRepo '.local/tools/rust/rustup'
  }
  Push-Location $DesktopRepo
  try {
    $Context = Get-LocalContext
    Set-LocalEnvironment $Context $DesktopRepo
    $Toolchain = (& $CargoCommand -V) + ' / ' + (& (Join-Path (Split-Path $CargoCommand) 'rustc.exe') -V)
    & $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'prepare-build-cache.py') 'desktop' '--toolchain' $Toolchain
    if ($LASTEXITCODE) { throw '原生构建缓存检查失败。' }
    if (-not $SkipFrontend) { & npm run frontend:build; if ($LASTEXITCODE) { throw '前端构建失败。' } }
    $CargoArguments = @('build', '--manifest-path', 'apps/desktop/Cargo.toml', '--package', 'voice-terminal-desktop', '--features', 'voice-terminal-desktop/custom-protocol', '--locked')
    if (-not $Debug) { $CargoArguments += '--release' }
    & $CargoCommand @CargoArguments
    if ($LASTEXITCODE) { throw '桌面程序构建失败。' }
    # 根入口需要在没有系统 C++（原生运行库）的机器上启动，单独静态链接其运行库。
    if ($SavedEncodedRustFlags) {
      $env:CARGO_ENCODED_RUSTFLAGS = $SavedEncodedRustFlags + [char]31 + '-C' + [char]31 + 'target-feature=+crt-static'
    } else {
      $env:RUSTFLAGS = ($SavedRustFlags + ' -C target-feature=+crt-static').Trim()
    }
    $BootstrapArguments = @('build', '--manifest-path', 'apps/desktop/Cargo.toml', '--package', 'voice-terminal-bootstrap', '--locked')
    if (-not $Debug) { $BootstrapArguments += '--release' }
    & $CargoCommand @BootstrapArguments
    if ($LASTEXITCODE) { throw '独立根入口构建失败。' }
    $Profile = if ($Debug) { 'debug' } else { 'release' }
    Write-Host "桌面产物：$DesktopRepo/.local/build/desktop/$Profile" -ForegroundColor Green
  } finally { Pop-Location }
} finally {
  $env:CARGO_TARGET_DIR = $SavedTargetDir
  $env:CARGO_HOME = $SavedCargoHome; $env:RUSTUP_HOME = $SavedRustupHome
  $env:RUSTFLAGS = $SavedRustFlags; $env:CARGO_ENCODED_RUSTFLAGS = $SavedEncodedRustFlags
}
