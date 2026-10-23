param([string]$Build='',[switch]$BuildDemo)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'desktop-pipeline.ps1')
Enter-DesktopPipeline
try {
  $DesktopRepo=Get-ProjectRoot;$Context=Get-LocalContext
  & (Join-Path $PSScriptRoot 'prepare-7zip.ps1');if ($LASTEXITCODE) {throw '压缩工具准备失败。'}
  if (-not (Invoke-DesktopBuildState -Operation check -Stage plugin)) {
    Invoke-DesktopStage '代理插件类型检查与构建' {
      Invoke-DesktopBuildState -Operation begin -Stage plugin
      & $Context.Node (Join-Path $DesktopRepo 'integrations/dsh/voice-terminal/build.mjs');if ($LASTEXITCODE) {throw '代理插件构建失败。'}
      Invoke-DesktopBuildState -Operation record -Stage plugin
    }
  }
  if ($BuildDemo) {
    & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BuildTarget product -OptimizationLevel debug -DesktopOnly -SkipFrontend
    if ($LASTEXITCODE) {throw '演示调试宿主构建失败。'}
    & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BuildTarget product -OptimizationLevel release -DemoLauncherOnly
    if ($LASTEXITCODE) {throw '演示启动器构建失败。'}
  }
  & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BuildTarget product -OptimizationLevel release
  if ($LASTEXITCODE) {throw '正式程序构建失败。'}
  if (-not (Invoke-DesktopBuildState -Operation check -Stage package -Build $Build)) {
    Invoke-DesktopStage '组装统一正式程序包' {
      Invoke-DesktopBuildState -Operation begin -Stage package -Build $Build
      $PackageArgs=@('-I','-B','-X','utf8',(Join-Path $PSScriptRoot 'assemble-program-package.py'))
      if ($Build) {$PackageArgs+=@('--build',$Build)}
      & $Context.Python @PackageArgs;if ($LASTEXITCODE) {throw '统一正式程序包组装失败。'}
      Invoke-DesktopBuildState -Operation record -Stage package -Build $Build
    }
  }
} finally {Exit-DesktopPipeline}
