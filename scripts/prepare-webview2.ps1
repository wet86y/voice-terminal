param([Parameter(Mandatory)][string]$RuntimeDir, [string]$Cab = '')
$ErrorActionPreference = 'Stop'
$FixedRuntime = [System.IO.Path]::GetFullPath($RuntimeDir)
if ($FixedRuntime.StartsWith('\\') -and -not $FixedRuntime.StartsWith('\\?\')) { throw '固定网页运行时不能从网络共享路径运行。' }
if ($FixedRuntime.StartsWith('\\?\UNC\',[System.StringComparison]::OrdinalIgnoreCase)) { throw '固定网页运行时不能从网络共享路径运行。' }
if ($Cab) {
  . (Join-Path $PSScriptRoot 'local-context.ps1')
  $Temporary = (Join-Path (Get-LocalContext).Root 'tmp') + [IO.Path]::DirectorySeparatorChar
  if (-not $FixedRuntime.StartsWith($Temporary, [StringComparison]::OrdinalIgnoreCase)) { throw '新渲染组件必须在本工程临时目录解包，再通过覆盖发布换入。' }
  if (Test-Path -LiteralPath $FixedRuntime) { throw '临时候选已存在，请先检查并回收。' }
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
if ($LASTEXITCODE) { throw '权限准备失败；请检查固定网页渲染目录的访问权限，不允许切换到另一套系统运行时。' }
Write-Host '固定网页运行时目录和读取执行权限已准备；仍需 Windows 10/11 实机启动验证。' -ForegroundColor Green
Write-Output $FixedRuntime
