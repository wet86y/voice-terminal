param([switch]$Dev, [switch]$Diagnostic, [switch]$UiPreview, [string]$Python = '', [string]$DataDir = '', [string]$WebView2 = '', [string]$InstallRoot = '')

function Invoke-DesktopBuild {
  param(
    [Parameter(Mandatory = $true)]$BuildScript,
    [switch]$Dev,
    [switch]$UiPreview
  )
  $BuildArguments = @{
    LaunchChannel = 'development'
    OptimizationLevel = 'debug'
    SkipFrontend = ($Dev -or $UiPreview)
  }
  & $BuildScript @BuildArguments
}

if ($MyInvocation.InvocationName -ne '.') {
  $ErrorActionPreference = 'Stop'
  # Windows PowerShell（系统脚本环境）默认编码随系统变化；演示启动日志固定 UTF-8（统一字符编码）。
  $OutputEncoding = [Text.UTF8Encoding]::new($false)
  try { [Console]::OutputEncoding = $OutputEncoding } catch { }
  . (Join-Path $PSScriptRoot 'local-context.ps1')
$DesktopRepo = Get-ProjectRoot
  $Executable = Join-Path $DesktopRepo '.local/build/desktop/development/debug/VoiceTerminalDesktop.exe'
  if ($UiPreview -and ($Dev -or $Diagnostic -or $Python -or $DataDir -or $InstallRoot)) {
    throw '桌面样例不接受 -Dev、-Diagnostic、-Python、-DataDir 或 -InstallRoot（产品启动参数）。'
  }
  Invoke-DesktopBuild -BuildScript (Join-Path $PSScriptRoot 'build-desktop.ps1') -Dev:$Dev -UiPreview:$UiPreview
  $Arguments = @('--root', $DesktopRepo)
  if ($WebView2) { $Arguments += @('--webview2', $WebView2) }
  if ($UiPreview) {
    $Arguments += '--ui-preview'
  } else {
    if ($InstallRoot) { $Arguments += @('--install', $InstallRoot) }
    if ($Python) { $Arguments += @('--python', $Python) }
    if ($DataDir) { $Arguments += @('--data-dir', $DataDir) }
    if ($Dev) { $Arguments += '--dev' }
    if ($Diagnostic) { $Arguments += '--diagnostic' }
  }
  # 直接调用避免 Start-Process（进程启动工具）的字符串参数二次拆分。
  & $Executable @Arguments
  exit $LASTEXITCODE
}
