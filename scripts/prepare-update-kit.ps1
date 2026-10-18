$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
. (Join-Path $PSScriptRoot 'download-sources.ps1')
$KitRepo = Get-ProjectRoot
$KitRoot = Join-Path $KitRepo '.local/build/update-kit'
$KitLock = Get-Content -LiteralPath (Join-Path $KitRepo 'config/update-kit.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $KitRoot)) {
  Invoke-SourceGitClone -Repository $KitLock.repository -Destination $KitRoot
  & git -C $KitRoot checkout --detach $KitLock.commit
  if ($LASTEXITCODE) { throw '下载组件固定版本检出失败。' }
}
$KitHead = & git -C $KitRoot rev-parse HEAD
if ($LASTEXITCODE -or $KitHead.Trim() -ne $KitLock.commit) { throw '下载组件提交与锁定版本不符，请先核对开发缓存。' }
Write-Host 'DesktopUpdateKit（桌面下载组件）固定源码已准备。'
