$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'start-desktop.ps1')

$BuildStub = {
  # Debug（调试构建）、SkipFrontend（跳过静态界面构建）；Bound 字段表示参数是否显式传入。
  param([switch]$Debug, [switch]$SkipFrontend)
  [pscustomobject]@{
    DebugBound = $PSBoundParameters.ContainsKey('Debug')
    Debug = [bool]$Debug
    SkipFrontendBound = $PSBoundParameters.ContainsKey('SkipFrontend')
    SkipFrontend = [bool]$SkipFrontend
  }
}

$Cases = @(
  @{ Name = 'static（静态桌面）'; Dev = $false; UiPreview = $false; SkipFrontend = $false },
  @{ Name = 'dev（真实桌面开发）'; Dev = $true; UiPreview = $false; SkipFrontend = $true },
  @{ Name = 'ui-preview（桌面样例）'; Dev = $false; UiPreview = $true; SkipFrontend = $true }
)
foreach ($Case in $Cases) {
  $Bound = Invoke-DesktopBuild -BuildScript $BuildStub -Dev:$Case.Dev -UiPreview:$Case.UiPreview
  if (-not $Bound.DebugBound -or -not $Bound.Debug -or
      -not $Bound.SkipFrontendBound -or $Bound.SkipFrontend -ne $Case.SkipFrontend) {
    throw "构建开关绑定失败：$($Case.Name)。"
  }
  Write-Output "构建开关绑定通过：$($Case.Name)。"
}
