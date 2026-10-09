param([switch]$Dev,[switch]$Diagnostic,[switch]$UiPreview,[string]$InstallRoot='')
if ($MyInvocation.InvocationName -eq '.') { return }
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$DesktopRepo=Get-ProjectRoot
if ($UiPreview) {
  if ($Dev -or $Diagnostic -or $InstallRoot) {throw '演示使用隔离缓存，不接受业务运行参数。'}
  & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BuildTarget product -OptimizationLevel debug -SkipFrontend
  if ($LASTEXITCODE) {throw '演示宿主构建失败。'}
  Copy-Item -LiteralPath (Join-Path $DesktopRepo '.local/build/desktop/product/debug/VoiceTerminalUIDemo.exe') -Destination (Join-Path $DesktopRepo '.local/VoiceTerminalUIDemo.exe') -Force
  & (Join-Path $DesktopRepo '.local/build/desktop/product/debug/VoiceTerminalDesktop.exe') '--root' $DesktopRepo '--ui-preview'
  exit $LASTEXITCODE
}
if (-not $InstallRoot) {$InstallRoot=Join-Path $DesktopRepo '.local'}
$Executable=Join-Path $InstallRoot 'VoiceTerminal.exe'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {throw '缺少正式入口，请先执行 npm run desktop:publish（本地发布）。'}
$Arguments=@()
if ($Dev) {$Arguments+=@('--dev','--source-root',$DesktopRepo)}
if ($Diagnostic) {$Arguments+='--diagnostic'}
# PowerShell（命令环境）的直接调用会等待后代进程；只等待根入口完成交接。
$LaunchOptions=@{FilePath=$Executable;WorkingDirectory=$InstallRoot;WindowStyle='Normal';PassThru=$true}
if ($Arguments.Count) {$LaunchOptions.ArgumentList=($Arguments | ForEach-Object {'"'+$_+'"'}) -join ' '}
$EntryProcess=Start-Process @LaunchOptions
$EntryProcess.WaitForExit()
exit $EntryProcess.ExitCode
