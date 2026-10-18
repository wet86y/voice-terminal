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
  try {
    $env:RUSTUP_DIST_SERVER = $Policy.rust_dist; $env:RUSTUP_UPDATE_ROOT = $Policy.rust_update
    & $Executable @Arguments --config 'source.crates-io.replace-with="voice-mirror"' --config ('source.voice-mirror.registry="' + $Policy.rust_index + '"')
    if ($LASTEXITCODE -in @(130,3221225786,-1073741510)) { throw '原生构建已取消。' }
    if (-not $LASTEXITCODE) { return }
    Write-Host '原生依赖镜像或构建失败，使用原站重试并复用编译缓存。'
    $env:RUSTUP_DIST_SERVER = $SavedDist; $env:RUSTUP_UPDATE_ROOT = $SavedUpdate
    & $Executable @Arguments
    if ($LASTEXITCODE) { throw '原生构建失败，请检查构建日志。' }
  } finally { $env:RUSTUP_DIST_SERVER = $SavedDist; $env:RUSTUP_UPDATE_ROOT = $SavedUpdate }
}
