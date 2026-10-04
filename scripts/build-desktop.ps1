param([switch]$Debug, [switch]$SkipWeb)
$ErrorActionPreference = 'Stop'
$DesktopRepo = Split-Path -Parent $PSScriptRoot
$LocalCargo = Join-Path $DesktopRepo '.runtime/desktop-tools/cargo/bin/cargo.exe'
$CargoCommand = if (Test-Path -LiteralPath $LocalCargo) { $LocalCargo } else { (Get-Command cargo -ErrorAction Stop).Source }
$SavedCargoHome = $env:CARGO_HOME
$SavedRustupHome = $env:RUSTUP_HOME
$SavedRustFlags = $env:RUSTFLAGS
$SavedEncodedRustFlags = $env:CARGO_ENCODED_RUSTFLAGS
try {
  if ($CargoCommand -eq $LocalCargo) {
    $env:CARGO_HOME = Join-Path $DesktopRepo '.runtime/desktop-tools/cargo'
    $env:RUSTUP_HOME = Join-Path $DesktopRepo '.runtime/desktop-tools/rustup'
  }
  Push-Location $DesktopRepo
  try {
    if (-not $SkipWeb) { & npm run build; if ($LASTEXITCODE) { throw '前端构建失败。' } }
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
    Write-Host "桌面产物：$DesktopRepo/apps/desktop/target/$Profile" -ForegroundColor Green
  } finally { Pop-Location }
} finally {
  $env:CARGO_HOME = $SavedCargoHome; $env:RUSTUP_HOME = $SavedRustupHome
  $env:RUSTFLAGS = $SavedRustFlags; $env:CARGO_ENCODED_RUSTFLAGS = $SavedEncodedRustFlags
}
