param([switch]$Dev, [switch]$Diagnostic, [switch]$UiPreview, [string]$Python = '', [string]$DataDir = '', [string]$WebView2 = '', [string]$InstallRoot = '')

function Invoke-DesktopBuild {
  param(
    [Parameter(Mandatory = $true)]$BuildScript,
    [switch]$Dev,
    [switch]$UiPreview
  )
  $BuildArguments = @{
    Debug = $true
    SkipFrontend = ($Dev -or $UiPreview)
  }
  & $BuildScript @BuildArguments
}

if ($MyInvocation.InvocationName -ne '.') {
  $ErrorActionPreference = 'Stop'
  $DesktopRepo = Split-Path -Parent $PSScriptRoot
  $Executable = Join-Path $DesktopRepo 'apps/desktop/target/debug/VoiceTerminalDesktop.exe'
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
