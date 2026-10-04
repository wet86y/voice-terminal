param([Parameter(Mandatory)][string]$RuntimeDir, [string]$Cab = '')
$ErrorActionPreference = 'Stop'
$FixedRuntime = [System.IO.Path]::GetFullPath($RuntimeDir)
if ($FixedRuntime.StartsWith('\\') -and -not $FixedRuntime.StartsWith('\\?\')) { throw '固定网页运行时不能从网络共享路径运行。' }
if ($FixedRuntime.StartsWith('\\?\UNC\',[System.StringComparison]::OrdinalIgnoreCase)) { throw '固定网页运行时不能从网络共享路径运行。' }
if ($Cab) {
  if (Test-Path -LiteralPath $FixedRuntime) { throw '解包目标已存在，请使用新的运行环境标识。' }
  New-Item -ItemType Directory -Path $FixedRuntime | Out-Null
  & expand.exe '-F:*' ([System.IO.Path]::GetFullPath($Cab)) $FixedRuntime
  if ($LASTEXITCODE) { throw '固定网页运行时解包失败。' }
  if (-not (Test-Path -LiteralPath (Join-Path $FixedRuntime 'msedgewebview2.exe'))) {
    $EngineCandidates = @(Get-ChildItem -LiteralPath $FixedRuntime -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'msedgewebview2.exe') })
    if ($EngineCandidates.Count -eq 1) { $FixedRuntime = $EngineCandidates[0].FullName }
  }
}
if (-not (Test-Path -LiteralPath (Join-Path $FixedRuntime 'msedgewebview2.exe'))) { throw '请指向包含 msedgewebview2.exe 的实际运行目录。' }
# Windows 10（操作系统）的固定版本 120 及以后需要应用容器读取及执行权限。
& icacls.exe $FixedRuntime '/grant' '*S-1-15-2-1:(OI)(CI)(RX)' '*S-1-15-2-2:(OI)(CI)(RX)' '/T' '/Q'
if ($LASTEXITCODE) { throw '权限准备失败；可在 portable.json（便携配置）中将 webview2（固定运行时目录）设为 null，明确使用系统运行时。' }
Write-Host '固定网页运行时目录和读取执行权限已准备；仍需 Windows 10/11 实机启动验证。' -ForegroundColor Green
Write-Output $FixedRuntime
