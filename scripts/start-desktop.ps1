param([switch]$Dev, [switch]$Diagnostic, [string]$Python = '', [string]$DataDir = '', [string]$WebView2 = '')
$ErrorActionPreference = 'Stop'
$DesktopRepo = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $DesktopRepo 'apps/desktop/target/debug/VoiceTerminalDesktop.exe'
if (-not (Test-Path -LiteralPath $Executable)) { & (Join-Path $PSScriptRoot 'build-desktop.ps1') -Debug }
$Arguments = @('--root', $DesktopRepo)
if ($Python) { $Arguments += @('--python', $Python) }
if ($DataDir) { $Arguments += @('--data-dir', $DataDir) }
if ($WebView2) { $Arguments += @('--webview2', $WebView2) }
if ($Dev) { $Arguments += '--dev' }
if ($Diagnostic) { $Arguments += '--diagnostic' }
# 直接调用避免 Start-Process（进程启动工具）的字符串参数二次拆分。
& $Executable @Arguments
