param([switch]$Offline, [switch]$SkipInstall, [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$RepoRoot = Get-ProjectRoot
$Context = Get-LocalContext
Set-LocalEnvironment $Context $RepoRoot
$Source = Join-Path $RepoRoot '.local/build/dsh/source'
$Lock = Get-Content -LiteralPath (Join-Path $RepoRoot 'config/dsh/runtime.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
# Source（受控源码缓存）：单个 Git（版本控制）检出，依赖版本由锁文件固定。
if (-not (Test-Path -LiteralPath (Join-Path $Source '.git'))) {
  if ($Offline -or $SkipInstall -or $ValidateOnly) { throw '缺少受控宿主构建缓存。' }
  & git clone --single-branch --branch $Lock.tag $Lock.repository $Source
  if ($LASTEXITCODE) { throw '宿主源码缓存建立失败。' }
}
$Head = (& git -C $Source rev-parse HEAD).Trim()
if ($Head -ne $Lock.commit) {
  if ($ValidateOnly -or $SkipInstall) { throw '宿主构建缓存与当前锁定版本不符。' }
  $Dirty = & git -C $Source status --porcelain
  if ($Dirty) { throw '宿主源码缓存有本机修改，不能自动覆盖。' }
  if (-not $Offline) { & git -C $Source fetch origin $Lock.tag; if ($LASTEXITCODE) { throw '宿主源码获取失败。' } }
  & git -C $Source checkout --detach $Lock.commit
  if ($LASTEXITCODE) { throw '宿主锁定版本检出失败。' }
}
if (-not $ValidateOnly) {
  if (-not $SkipInstall) {
    & $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'prepare-build-cache.py') 'dsh' '--toolchain' ($Lock.package_manager + '/' + (& $Context.Node --version))
    if ($LASTEXITCODE) { throw '代理构建缓存检查失败。' }
    Push-Location $Source
    try {
      $Pnpm = $Lock.package_manager -replace '^pnpm@', ''
      $PnpmHome = Join-Path $Context.Root 'tools/pnpm'
      $PnpmEntry = Join-Path $PnpmHome 'node_modules/pnpm/bin/pnpm.cjs'
      $PnpmVersion = if (Test-Path -LiteralPath $PnpmEntry) { & $Context.Node $PnpmEntry --version } else { '' }
      if ($PnpmVersion -ne $Pnpm) {
        if ($Offline) { throw '离线构建缺少当前锁定版本的项目内包管理器。' }
        & npm install --prefix $PnpmHome --no-save --package-lock=false "pnpm@$Pnpm"
        if ($LASTEXITCODE) { throw '项目内包管理器准备失败。' }
      }
      $InstallArgs = @('install', '--frozen-lockfile', '--store-dir', (Join-Path $Context.Root 'cache/pnpm'))
      if ($Offline) { $InstallArgs += '--offline' }
      & $Context.Node $PnpmEntry @InstallArgs
      if ($LASTEXITCODE) { throw '宿主开发依赖安装失败。' }
      & $Context.Node $PnpmEntry run build
      if ($LASTEXITCODE) { throw '宿主源码构建失败。' }
    } finally { Pop-Location }
  }
  & $Context.Node (Join-Path $RepoRoot 'integrations/dsh/voice-terminal/build.mjs')
  if ($LASTEXITCODE) { throw '统一插件构建失败。' }
}
# 插件安装只同步现用生产宿主的受管配置，保留现有用户补丁，无第二套持久状态。
& $Context.Python '-B' '-X' 'utf8' (Join-Path $PSScriptRoot 'backend-entry.py') 'voice_terminal.dsh.install' $(if ($ValidateOnly) { '--validate-only' })
if ($LASTEXITCODE) { throw '生产代理宿主配置或版本验证失败。' }
Write-Host '代理宿主使用唯一生产运行目录与数据目录；未制作环境或源码备份。' -ForegroundColor Green
