$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'start-desktop.ps1')

$BuildStub = {
  # LaunchChannel（启动渠道）、OptimizationLevel（优化级别）、SkipFrontend（跳过静态界面构建）。
  param([string]$LaunchChannel, [string]$OptimizationLevel, [switch]$SkipFrontend)
  [pscustomobject]@{
    LaunchChannel = $LaunchChannel
    OptimizationLevel = $OptimizationLevel
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
  if ($Bound.LaunchChannel -ne 'development' -or $Bound.OptimizationLevel -ne 'debug' -or
      -not $Bound.SkipFrontendBound -or $Bound.SkipFrontend -ne $Case.SkipFrontend) {
    throw "构建开关绑定失败：$($Case.Name)。"
  }
  Write-Output "构建开关绑定通过：$($Case.Name)。"
}
