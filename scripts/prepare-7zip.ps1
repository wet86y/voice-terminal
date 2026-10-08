param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$SevenRoot = Get-ProjectRoot
$SevenLock = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $SevenRoot 'config/7zip.lock.json') | ConvertFrom-Json
$SevenSource = Join-Path $SevenRoot '.local/build/7zip'
$SevenOutput = Join-Path $SevenRoot '.local/build/tools/7zip'
if (-not (Test-Path -LiteralPath (Join-Path $SevenSource '.git'))) {
  & git clone --depth 1 --branch $SevenLock.version $SevenLock.repository $SevenSource
  if ($LASTEXITCODE) { throw '完整 7-Zip（压缩工具）源码获取失败。' }
}
$SevenCommit = (& git -C $SevenSource rev-parse HEAD).Trim()
if ($SevenCommit -ne $SevenLock.commit) { throw '压缩工具源码与锁定提交不一致。' }
if ((Test-Path -LiteralPath (Join-Path $SevenOutput 'build.json'))) {
  $Previous = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $SevenOutput 'build.json') | ConvertFrom-Json
  if ($Previous.commit -eq $SevenCommit -and (Test-Path -LiteralPath (Join-Path $SevenOutput '7z.exe')) -and (Test-Path -LiteralPath (Join-Path $SevenOutput '7z.dll'))) { return }
}
$SevenVsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$SevenVsRoot = (& $SevenVsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
if (-not $SevenVsRoot) { throw '完整压缩工具构建需要 MSVC（微软原生编译工具）。' }
$SevenVcVars = Join-Path $SevenVsRoot 'VC/Auxiliary/Build/vcvars64.bat'
New-Item -ItemType Directory -Force -Path $SevenOutput | Out-Null
foreach ($SevenBundle in @('UI/Console','Bundles/Format7zF')) {
  $SevenWork = Join-Path $SevenSource ('CPP/7zip/' + $SevenBundle)
  $SevenLog = Join-Path $SevenOutput (($SevenBundle -replace '/','-') + '.log')
  $SevenBatch = Join-Path $SevenOutput 'compile.cmd'
  $SevenCommands = @('@echo off','chcp 65001 >nul',('call "' + $SevenVcVars + '" >nul'),'if errorlevel 1 exit /b 1',('nmake /nologo PLATFORM=x64 >"' + $SevenLog + '" 2>&1'),'exit /b %errorlevel%')
  [IO.File]::WriteAllLines($SevenBatch, $SevenCommands, [Text.UTF8Encoding]::new($false))
  Push-Location $SevenWork
  try {
    # 仅调用编译工具；不运行 7-Zip 或 DSH（代理宿主）安装器。
    & $SevenBatch
    if ($LASTEXITCODE) { Get-Content -Tail 35 -LiteralPath $SevenLog; throw '完整压缩工具构建失败。' }
  } finally { Pop-Location }
}
Copy-Item -LiteralPath (Join-Path $SevenSource 'CPP/7zip/UI/Console/x64/7z.exe') -Destination $SevenOutput -Force
Copy-Item -LiteralPath (Join-Path $SevenSource 'CPP/7zip/Bundles/Format7zF/x64/7z.dll') -Destination $SevenOutput -Force
foreach ($SevenNotice in @('License.txt','readme.txt')) {
  Copy-Item -LiteralPath (Join-Path $SevenSource ('DOC/' + $SevenNotice)) -Destination $SevenOutput -Force
}
@{schema=1; version=$SevenLock.version; commit=$SevenCommit; files=@(
  @{path='7z.exe'; sha256=(Get-Sha256File -LiteralPath (Join-Path $SevenOutput '7z.exe'))},
  @{path='7z.dll'; sha256=(Get-Sha256File -LiteralPath (Join-Path $SevenOutput '7z.dll'))}
); fields=@{version='压缩工具版本'; commit='官方固定提交'; files='构建文件摘要'}} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $SevenOutput 'build.json')
Write-Host "完整压缩工具已准备：$SevenOutput"
