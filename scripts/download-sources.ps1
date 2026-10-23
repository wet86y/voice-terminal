# Download sources（下载来源）：构建脚本共用配置，不写入系统或用户全局设置。
function Get-DownloadPolicy {
  Get-Content -LiteralPath (Join-Path $PSScriptRoot '../config/download-sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}
function Get-DownloadCandidates {
  param([string]$Url)
  foreach ($Rule in (Get-DownloadPolicy).prefixes) {
    if ($Url.StartsWith($Rule.origin_prefix, [StringComparison]::Ordinal)) { $Rule.mirror_prefix + $Url.Substring($Rule.origin_prefix.Length) }
  }
  $Url
}
function Invoke-SourceGitClone {
  param([string]$Repository, [string]$Destination, [string[]]$Options = @())
  $Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../.local')) + [IO.Path]::DirectorySeparatorChar
  $Target = [IO.Path]::GetFullPath($Destination)
  if (-not $Target.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) { throw '源码候选必须位于工程本机目录。' }
  if (Test-Path -LiteralPath $Target) { throw '源码候选目标已存在，请先检查未完成的构建缓存。' }
  foreach ($Source in (Get-DownloadCandidates $Repository)) {
    $Stage = $Target + '.download-' + [Guid]::NewGuid().ToString('N')
    try {
      Write-Host ('源码下载来源：' + ([Uri]$Source).Host)
      & git -c http.connectTimeout=12 -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=20 clone @Options $Source $Stage | Out-Host
      if ($LASTEXITCODE -in @(130,3221225786,-1073741510)) { throw '源码下载已取消。' }
      if ($LASTEXITCODE) { Write-Host '源码来源失败，尝试备用来源。'; continue }
      & git -C $Stage remote set-url origin $Repository
      if ($LASTEXITCODE) { throw '无法保留源码的官方来源记录。' }
      Move-Item -LiteralPath $Stage -Destination $Target
      return
    } finally {
      # Stage（本次独有候选）：删除前确认完整绝对路径仍在已核实工程目录内。
      $ResolvedStage = [IO.Path]::GetFullPath($Stage)
      if (-not $ResolvedStage.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) { throw '拒绝清理工程外的源码候选。' }
      if (Test-Path -LiteralPath $ResolvedStage) { Remove-Item -LiteralPath $ResolvedStage -Recurse -Force }
    }
  }
  throw '所有源码下载来源均失败。'
}
function Invoke-SourceGitFetch {
  param([string]$Repository, [string]$Directory, [string]$Ref)
  foreach ($Source in (Get-DownloadCandidates $Repository)) {
    & git -C $Directory -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=20 fetch $Source $Ref | Out-Host
    if ($LASTEXITCODE -in @(130,3221225786,-1073741510)) { throw '源码获取已取消。' }
    if (-not $LASTEXITCODE) { return }
  }
  throw '所有源码获取来源均失败。'
}
function Invoke-SourceNpm {
  param([string]$Executable = 'npm', [string[]]$Arguments, [string]$Registry = '')
  $Policy = Get-DownloadPolicy
  if (-not $Registry) { $Registry = $env:npm_config_registry }
  if (-not $Registry) {
    $UserConfig = if ($env:NPM_CONFIG_USERCONFIG) { $env:NPM_CONFIG_USERCONFIG } else { Join-Path $env:USERPROFILE '.npmrc' }
    foreach ($ConfigPath in @((Join-Path (Get-Location).Path '.npmrc'),$UserConfig,$env:NPM_CONFIG_GLOBALCONFIG)) {
      if (-not $ConfigPath -or -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { continue }
      $MatchesFound = @(Get-Content -LiteralPath $ConfigPath -Encoding UTF8 | Select-String '^\s*registry\s*=\s*(.*?)\s*$')
      if ($MatchesFound.Count) { $Registry = $MatchesFound[-1].Matches[0].Groups[1].Value.Trim().Trim('"',"'"); break }
    }
  }
  if (-not $Registry) { $Registry = 'https://registry.npmjs.org' }
  $Sources = if ($Policy.npm_registries -contains $Registry.TrimEnd('/')) { $Policy.npm_registries } else { @($Registry) }
  $SavedRegistry = $env:npm_config_registry; $SavedDist = $env:npm_config_disturl
  try {
    foreach ($Source in $Sources) {
      $env:npm_config_registry = $Source
      if ($Policy.npm_registries -contains $Source) { $env:npm_config_disturl = if ($Source -eq $Policy.npm_registries[0]) { $Policy.node_dist } else { $Policy.node_dist_official } }
      Write-Host ('包下载来源：' + ([Uri]$Source).Host)
      & $Executable @Arguments --registry $Source
      if ($LASTEXITCODE -in @(130,3221225786,-1073741510)) { throw '包安装已取消。' }
      if (-not $LASTEXITCODE) { return }
      Write-Host '包来源失败，尝试备用注册表。'
    }
    throw '包安装失败，所有允许的注册表已尝试。'
  } finally { $env:npm_config_registry = $SavedRegistry; $env:npm_config_disturl = $SavedDist }
}
function Invoke-SourceCargo {
  param([string]$Executable, [string[]]$Arguments)
  $Policy = Get-DownloadPolicy
  $SavedDist = $env:RUSTUP_DIST_SERVER; $SavedUpdate = $env:RUSTUP_UPDATE_ROOT
  $Directory=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../.local/cache/cargo'))
  [IO.Directory]::CreateDirectory($Directory) | Out-Null
  $Config=Join-Path $Directory 'source.toml'
  # 配置文件保留 TOML（配置格式）的字符串引号，不依赖脚本宿主的原生参数转义。
  $Registry=ConvertTo-Json -InputObject ([string]$Policy.rust_index) -Compress
  [IO.File]::WriteAllText($Config,('[source.crates-io]'+"`n"+'replace-with = "voice-mirror"'+"`n"+'[source.voice-mirror]'+"`n"+'registry = '+$Registry+"`n"),[Text.UTF8Encoding]::new($false))
  $Log=Join-Path $Directory 'build-latest.log'
  $SavedPreference=$ErrorActionPreference
  try {
    $env:RUSTUP_DIST_SERVER = $Policy.rust_dist; $env:RUSTUP_UPDATE_ROOT = $Policy.rust_update
    $Lines=[Collections.Generic.List[string]]::new()
    $ErrorActionPreference='Continue' # 原生标准错误作为日志读取，实际成功由退出码决定。
    & $Executable @Arguments --config $Config 2>&1 | ForEach-Object {$Line=if ($_ -is [Management.Automation.ErrorRecord]) {$_.Exception.Message} else {[string]$_};$Lines.Add($Line);Write-Host $Line}
    $ErrorActionPreference=$SavedPreference
    [IO.File]::WriteAllLines($Log,$Lines,[Text.UTF8Encoding]::new($false))
    if ($LASTEXITCODE -in @(130,3221225786,-1073741510)) { throw '原生构建已取消。' }
    if (-not $LASTEXITCODE) { return }
    $Message=$Lines -join "`n"
    if ($Message -notmatch '(?i)failed to (download|fetch|update.*registry|query.*registry)|download of .+ failed|Could not resolve|Timeout was reached|failed to get .+ as a dependency') {throw '原生编译或配置失败；未切换软件源重复编译，详见 .local/cache/cargo/build-latest.log。'}
    Write-Host '原生依赖来源访问失败，使用原站重试；编译及配置错误不会触发来源重试。'
    $env:RUSTUP_DIST_SERVER = $SavedDist; $env:RUSTUP_UPDATE_ROOT = $SavedUpdate
    $ErrorActionPreference='Continue'
    & $Executable @Arguments 2>&1 | ForEach-Object {$Line=if ($_ -is [Management.Automation.ErrorRecord]) {$_.Exception.Message} else {[string]$_};$Lines.Add($Line);Write-Host $Line}
    $ErrorActionPreference=$SavedPreference
    [IO.File]::WriteAllLines($Log,$Lines,[Text.UTF8Encoding]::new($false))
    if ($LASTEXITCODE) { throw '原生构建失败，请检查构建日志。' }
  } finally { $ErrorActionPreference=$SavedPreference;$env:RUSTUP_DIST_SERVER = $SavedDist; $env:RUSTUP_UPDATE_ROOT = $SavedUpdate }
}
