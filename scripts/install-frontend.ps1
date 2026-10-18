param([switch]$Offline)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'download-sources.ps1')
Push-Location (Join-Path $PSScriptRoot '..')
try {
  if ($Offline) {
    & npm ci --offline --no-audit --no-fund
    if ($LASTEXITCODE) { throw '离线缓存缺少前端依赖，未尝试网络下载。' }
  } else { Invoke-SourceNpm -Arguments @('ci','--no-audit','--no-fund') }
} finally { Pop-Location }
