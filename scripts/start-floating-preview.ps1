param([switch]$BuildOnly)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'desktop-pipeline.ps1')
. (Join-Path $PSScriptRoot 'download-sources.ps1')
$FloatingRepo=Get-ProjectRoot
$FloatingTarget=Join-Path $FloatingRepo '.local/build/desktop/product'
$FloatingExecutable=Join-Path $FloatingTarget 'debug/VoiceTerminalDesktop.exe'
$FloatingCargo=Join-Path $FloatingRepo '.local/tools/rust/cargo/bin/cargo.exe'
if (-not (Test-Path -LiteralPath $FloatingCargo -PathType Leaf)) {throw '缺少项目现有的 Rust（原生编译）工具链。'}
$SavedFloatingEnv=@{}
foreach ($Name in @('CARGO_HOME','RUSTUP_HOME','CARGO_TARGET_DIR','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS')) {
  $SavedFloatingEnv[$Name]=[Environment]::GetEnvironmentVariable($Name,'Process')
}
Enter-DesktopPipeline
try {
  $env:CARGO_HOME=Join-Path $FloatingRepo '.local/tools/rust/cargo'
  $env:RUSTUP_HOME=Join-Path $FloatingRepo '.local/tools/rust/rustup'
  $env:CARGO_TARGET_DIR=$FloatingTarget
  $env:RUSTFLAGS=($SavedFloatingEnv['RUSTFLAGS']+' -C target-feature=+crt-static').Trim()
  $env:CARGO_ENCODED_RUSTFLAGS=$null
  Push-Location $FloatingRepo
  try {
    Invoke-SourceCargo -Executable $FloatingCargo -Arguments @('build','--offline','--locked','--manifest-path','apps/desktop/Cargo.toml','-p','voice-terminal-desktop','--bin','VoiceTerminalDesktop','--features','custom-protocol')
  } finally {Pop-Location}
} finally {
  foreach ($Name in $SavedFloatingEnv.Keys) {
    [Environment]::SetEnvironmentVariable($Name,$SavedFloatingEnv[$Name],'Process')
  }
  Exit-DesktopPipeline
}
if (-not $BuildOnly) {
  & $FloatingExecutable '--ui-preview' '--floating-preview' '--root' $FloatingRepo
  exit $LASTEXITCODE
}
