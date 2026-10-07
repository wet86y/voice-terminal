param(
  [string]$Python = "",
  [switch]$Online,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'local-context.ps1')
$RepoRoot = Get-ProjectRoot
$Context = Get-LocalContext
Set-LocalEnvironment $Context $RepoRoot
if (-not $Python) { $Python = $Context.Python }
if ([IO.Path]::GetFullPath($Python) -ne $Context.Python) { throw '环境检查必须使用唯一便携配置所选解释器。' }
Push-Location $RepoRoot
try {
  $DshExit = 0
  $DshChecks = @()
  try {
    if ($Json) {
      & (Join-Path $PSScriptRoot "setup-dsh.ps1") -ValidateOnly *> $null
      $Lock = Get-Content -LiteralPath (Join-Path $RepoRoot "config\dsh\runtime.lock.json") -Raw | ConvertFrom-Json
      $DshChecks += [pscustomobject]@{
        name = "dsh:runtime"
        status = "PASS"
        message = "$($Lock.version) @ $($Lock.commit)"
      }
      $DshChecks += [pscustomobject]@{
        name = "dsh:isolation"
        status = "PASS"
        message = ".local/build/dsh/source; .local/data/dsh"
      }
      $Listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 3081 -State Listen -ErrorAction SilentlyContinue
      $DshChecks += [pscustomobject]@{
        name = "port:dsh"
        status = $(if ($Listener) { "WARN" } else { "PASS" })
        message = $(if ($Listener) { "127.0.0.1:3081 is listening (PID $($Listener.OwningProcess)); runtime ownership check required" } else { "127.0.0.1:3081 is free" })
      }
      $DshChecks += [pscustomobject]@{
        name = "dsh:path"
        status = "PASS"
        message = $(if ($RepoRoot -match '[^\x00-\x7F]') { "CLI validation succeeded from non-ASCII repository path" } else { "repository path is ASCII" })
      }
    } else {
      Write-Host "Checking project-isolated DSH..." -ForegroundColor Cyan
      & (Join-Path $PSScriptRoot "setup-dsh.ps1") -ValidateOnly
      $Listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 3081 -State Listen -ErrorAction SilentlyContinue
      if ($Listener) {
        Write-Host "Project DSH port 3081 is listening (PID $($Listener.OwningProcess)); verify it belongs to the current Voice Server." -ForegroundColor Yellow
      } else {
        Write-Host "Project DSH port 3081 is currently free." -ForegroundColor DarkGray
      }
      if ($RepoRoot -match '[^\x00-\x7F]') {
        Write-Host "DSH CLI validation succeeded from the non-ASCII repository path." -ForegroundColor Green
      }
    }
  } catch {
    $DshExit = 1
    if ($Json) {
      $DshChecks += [pscustomobject]@{ name = "dsh:runtime"; status = "FAIL"; message = $_.Exception.Message }
    } else {
      Write-Host "DSH check failed: $($_.Exception.Message)" -ForegroundColor Red
    }
  }
  $arguments = @('-B', '-X', 'utf8', (Join-Path $PSScriptRoot 'backend-entry.py'), 'voice_terminal.doctor')
  if ($Online) { $arguments += "--online" }
  if ($Json) { $arguments += "--json" }
  if ($Json) {
    $VoiceOutput = (& $Python @arguments | Out-String)
    $VoiceExit = $LASTEXITCODE
    $VoiceChecks = @($VoiceOutput | ConvertFrom-Json)
    @($VoiceChecks) + @($DshChecks) | ConvertTo-Json -Depth 5
  } else {
    & $Python @arguments
    $VoiceExit = $LASTEXITCODE
  }
  if ($VoiceExit -ne 0 -or $DshExit -ne 0) { exit 1 }
  exit 0
} finally {
  Pop-Location
}
