# Context（安装上下文）：按工程标识定位，同一便携配置提供全部业务路径。
function Get-ProjectRoot {
  param([string]$Start = $PSScriptRoot)
  $Directory = [IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Start))
  while ($Directory) {
    if (Test-Path -LiteralPath (Join-Path $Directory.FullName 'pyproject.toml')) { return $Directory.FullName }
    $Directory = $Directory.Parent
  }
  throw '无法定位工程根目录。'
}
function Get-LocalContext {
  param([string]$Root = '')
  if (-not $Root) {
    $ProgramRoot = Get-ProjectRoot
    $Root = if (Test-Path -LiteralPath (Join-Path $ProgramRoot 'apps/desktop/Cargo.toml')) { Join-Path $ProgramRoot '.local' } else { Split-Path $ProgramRoot }
  }
  $Root = [IO.Path]::GetFullPath($Root)
  $Selection = Get-Content -LiteralPath (Join-Path $Root 'portable.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $Fixed = @{ app='app'; python='runtimes/python/python.exe'; models='models'; dsh='runtimes/dsh'; webview2='runtimes/webview2'; data_dir='data' }
  if ($Selection.schema -ne 2) { throw '需要 schema=2（第二版）便携配置；请先迁移旧布局。' }
  foreach ($Key in $Fixed.Keys) { if ($Selection.$Key -ne $Fixed[$Key]) { throw "固定路径字段不符：$Key（组件位置）" } }
  $Runtime = Join-Path $Root $Selection.dsh
  $Metadata = Get-Content -LiteralPath (Join-Path $Runtime 'runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $Node = [IO.Path]::GetFullPath((Join-Path $Runtime $Metadata.node))
  if (-not $Node.StartsWith($Runtime + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '宿主解释器路径越界。' }
  return @{ Root=$Root; Program=(Join-Path $Root $Selection.app); Python=(Join-Path $Root $Selection.python); Models=(Join-Path $Root $Selection.models); Data=(Join-Path $Root $Selection.data_dir); Dsh=$Runtime; Node=$Node; WebView2=(Join-Path $Root $Selection.webview2) }
}
function Set-LocalEnvironment {
  param($Context, [string]$Program = '')
  if (-not $Program) { $Program = $Context.Program }
  $env:VOICE_INSTALL_ROOT = $Context.Root
  $env:VOICE_PROGRAM_ROOT = $Program
  $env:VOICE_DATA_DIR = $Context.Data
  $env:VOICE_MODEL_DIR = $Context.Models
  $env:VOICE_DSH_RUNTIME = $Context.Dsh
  $env:VOICE_NODE_EXECUTABLE = $Context.Node
  $env:WEBVIEW2_BROWSER_EXECUTABLE_FOLDER = $Context.WebView2
  $env:PYTHONPYCACHEPREFIX = Join-Path $Context.Root 'cache/python'
  $env:PIP_CACHE_DIR = Join-Path $Context.Root 'cache/pip'
  $env:HF_HOME = Join-Path $Context.Root 'cache/huggingface'
  $env:MODELSCOPE_CACHE = Join-Path $Context.Root 'cache/modelscope'
  $env:TORCH_HOME = Join-Path $Context.Root 'cache/torch'
  $env:COREPACK_HOME = Join-Path $Context.Root 'cache/corepack'
  $env:npm_config_cache = Join-Path $Context.Root 'cache/npm'
}
