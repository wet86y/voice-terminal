# Desktop pipeline（桌面构建流水线）：进程内嵌套复用，同一仓库的其它进程立即拒绝并发覆盖。
. (Join-Path $PSScriptRoot 'local-context.ps1')
function Enter-DesktopPipeline {
  $Root=Get-ProjectRoot
  if ($global:VoiceTerminalDesktopPipeline) {
    if ($global:VoiceTerminalDesktopPipeline.Root -ne $Root) {throw '同一进程不能嵌套构建不同仓库。'}
    $global:VoiceTerminalDesktopPipeline.Depth++
    return
  }
  $Directory=Join-Path $Root '.local/build/desktop';[IO.Directory]::CreateDirectory($Directory) | Out-Null
  try {$Lock=[IO.File]::Open((Join-Path $Directory 'pipeline.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)}
  catch {throw '本仓库已有构建或发布流程正在运行，请等待该流程结束后重试；未修改构建产物。'}
  $global:VoiceTerminalDesktopPipeline=@{Root=$Root;Depth=1;Lock=$Lock;Started=[DateTime]::UtcNow;Timer=[Diagnostics.Stopwatch]::StartNew();Stages=[Collections.Generic.List[object]]::new()}
}
function Exit-DesktopPipeline {
  $Pipeline=$global:VoiceTerminalDesktopPipeline
  if (-not $Pipeline) {return}
  $Pipeline.Depth--
  if ($Pipeline.Depth -gt 0) {return}
  try {
    $Report=[ordered]@{schema=1;started_at=$Pipeline.Started.ToString('o');duration_ms=$Pipeline.Timer.ElapsedMilliseconds;stages=@($Pipeline.Stages.ToArray());fields=@{schema='流水线报告格式';started_at='开始时间（协调世界时）';duration_ms='总耗时毫秒';stages='阶段及结果'}}
    $Directory=Join-Path $Pipeline.Root '.local/cache/reports';[IO.Directory]::CreateDirectory($Directory) | Out-Null
    [IO.File]::WriteAllText((Join-Path $Directory 'desktop-pipeline-latest.json'),($Report | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
    Write-Host ('流水线总用时：{0:N1} 秒；阶段记录：.local/cache/reports/desktop-pipeline-latest.json' -f $Pipeline.Timer.Elapsed.TotalSeconds)
  } finally {$Pipeline.Lock.Dispose();$global:VoiceTerminalDesktopPipeline=$null}
}
function Invoke-DesktopStage {
  param([string]$Name,[scriptblock]$Action)
  $Timer=[Diagnostics.Stopwatch]::StartNew();$Succeeded=$false
  Write-Host ('开始：'+$Name) -ForegroundColor Cyan
  try {& $Action;$Succeeded=$true}
  finally {
    $global:VoiceTerminalDesktopPipeline.Stages.Add([ordered]@{name=$Name;duration_ms=$Timer.ElapsedMilliseconds;success=$Succeeded;fields=@{name='阶段名称';duration_ms='阶段耗时毫秒';success='阶段是否完成'}})
    Write-Host ('阶段结束：{0}；用时 {1:N1} 秒；完成：{2}' -f $Name,$Timer.Elapsed.TotalSeconds,$Succeeded)
  }
}
function Invoke-DesktopBuildState {
  param([ValidateSet('check','begin','record')][string]$Operation,[string]$Stage,[string]$Optimization='release',[string]$Components='desktop,bootstrap',[switch]$Qualification,[string]$Build='')
  $Context=Get-LocalContext
  $StateArgs=@('-I','-B','-X','utf8',(Join-Path $PSScriptRoot 'desktop-build-state.py'),$Operation,'--stage',$Stage,'--optimization',$Optimization,'--components',$Components)
  if ($Qualification) {$StateArgs+='--qualification'}
  if ($Build) {$StateArgs+=@('--build',$Build)}
  & $Context.Python @StateArgs | Out-Host
  if ($Operation -eq 'check') {
    if ($LASTEXITCODE -notin @(0,3)) {throw '构建复用检查失败。'}
    return ($LASTEXITCODE -eq 0)
  }
  if ($LASTEXITCODE) {throw '构建输入或产物登记失败。'}
}
function Assert-DesktopPackage {
  param([string]$Build='')
  if (-not (Invoke-DesktopBuildState -Operation check -Stage package -Build $Build)) {throw '已有程序包与当前输入不一致，请先运行 npm run desktop:prepare（准备正式程序包）；复用模式不会重打包。'}
}
function ConvertTo-DesktopNativeArgument {
  param([string]$Value)
  # Windows（操作系统）原生参数规则：引号前和末尾的反斜杠需要成对转义。
  $Escaped=[regex]::Replace($Value,'(\\*)"','$1$1\"')
  $Escaped=[regex]::Replace($Escaped,'(\\+)$','$1$1')
  return '"'+$Escaped+'"'
}
function Invoke-DesktopInstallProcess {
  param([string]$Executable,[string[]]$Arguments,[string]$LogName='desktop-install-latest.log') # LogName（独立维护步骤日志文件名）。
  $Directory=Join-Path (Get-ProjectRoot) '.local/cache/logs';[IO.Directory]::CreateDirectory($Directory) | Out-Null
  if ([IO.Path]::GetFileName($LogName) -ne $LogName) {throw '安装步骤日志必须使用固定文件名。'}
  $Log=[IO.StreamWriter]::new((Join-Path $Directory $LogName),$false,[Text.UTF8Encoding]::new($false))
  $Info=[Diagnostics.ProcessStartInfo]::new();$Info.FileName=$Executable;$Info.Arguments=(($Arguments | ForEach-Object {ConvertTo-DesktopNativeArgument $_}) -join ' ')
  $Info.UseShellExecute=$false;$Info.CreateNoWindow=$true;$Info.RedirectStandardOutput=$true;$Info.RedirectStandardError=$true
  $Info.StandardOutputEncoding=[Text.Encoding]::UTF8;$Info.StandardErrorEncoding=[Text.Encoding]::UTF8
  $Process=[Diagnostics.Process]::new();$Process.StartInfo=$Info
  $Elapsed=[Diagnostics.Stopwatch]::StartNew();$LastNotice=[DateTime]::UtcNow
  $Started=$false
  try {
    if (-not $Process.Start()) {throw '无法启动本地安装辅助程序。'}
    $Started=$true
    $Readers=@($Process.StandardOutput,$Process.StandardError);$Pending=@($Readers[0].ReadLineAsync(),$Readers[1].ReadLineAsync())
    while (-not $Process.HasExited -or $null -ne $Pending[0] -or $null -ne $Pending[1]) {
      for ($Index=0;$Index -lt 2;$Index++) {
        if ($null -ne $Pending[$Index] -and $Pending[$Index].IsCompleted) {
          $Line=$Pending[$Index].GetAwaiter().GetResult()
          if ($null -eq $Line) {$Pending[$Index]=$null}
          else {Write-Host $Line;$Log.WriteLine(('{0:o} {1}' -f [DateTime]::UtcNow,$Line));$Log.Flush();$LastNotice=[DateTime]::UtcNow;$Pending[$Index]=$Readers[$Index].ReadLineAsync()}
        }
      }
      if (([DateTime]::UtcNow-$LastNotice).TotalSeconds -ge 20) {Write-Host ('安装与兼容处理仍在运行，已用时 {0:N0} 秒；详细进度见 .local/cache/logs/{1}' -f $Elapsed.Elapsed.TotalSeconds,$LogName);$LastNotice=[DateTime]::UtcNow}
      Start-Sleep -Milliseconds 100
    }
    $Process.WaitForExit()
    if ($Process.ExitCode) {throw ('安装或强制兼容处理未完成，退出码：{0}；请核对阶段日志与事务记录。' -f $Process.ExitCode)}
  } finally {
    try {if ($Started -and -not $Process.HasExited) {$Process.Kill();$Process.WaitForExit()}}
    finally {$Log.Dispose();$Process.Dispose()}
  }
}
