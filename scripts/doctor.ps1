param(
  [string]$Python = "python",
  [switch]$Online,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
  $arguments = @("-m", "voice_terminal.doctor")
  if ($Online) { $arguments += "--online" }
  if ($Json) { $arguments += "--json" }
  & $Python @arguments
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
