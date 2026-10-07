param([string]$Root = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$PackageRoot = [IO.Path]::GetFullPath($Root)
$Selection = Get-Content -LiteralPath (Join-Path $PackageRoot 'portable.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Selection.schema -ne 2) { throw '仅支持第二版便携配置。' }
$Interpreter = Join-Path $PackageRoot $Selection.python
$ProgramDirectory = Join-Path $PackageRoot $Selection.app
& $Interpreter '-B' '-X' 'utf8' (Join-Path $ProgramDirectory 'scripts/backend-entry.py') 'voice_terminal.distribution' 'verify-install' '--root' $PackageRoot
if ($LASTEXITCODE) { throw '发行目录校验失败。' }
