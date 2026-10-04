param([string]$Root = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$PackageRoot = [IO.Path]::GetFullPath($Root)
$Portable = Get-Content -LiteralPath (Join-Path $PackageRoot 'portable.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$DataDirectory = if ([IO.Path]::IsPathRooted($Portable.data_dir)) { $Portable.data_dir } else { Join-Path $PackageRoot $Portable.data_dir }
$ActiveFile = Join-Path $DataDirectory 'state/active.json'
$Selection = if (Test-Path -LiteralPath $ActiveFile) { Get-Content -LiteralPath $ActiveFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $Portable }
$Interpreter = if ($Selection.external_python) { $Selection.external_python } else { Join-Path $PackageRoot $Selection.python }
$ProgramDirectory = Join-Path $PackageRoot $Selection.app
& $Interpreter '-B' '-X' 'utf8' (Join-Path $ProgramDirectory 'scripts/backend-entry.py') 'voice_terminal.distribution' 'verify-install' '--root' $PackageRoot
if ($LASTEXITCODE) { throw '发行目录校验失败。' }
