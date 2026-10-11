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
  $Fixed = @{ app='app'; python='runtimes/python/python.exe'; models='models'; dsh='runtimes/dsh'; data_dir='data' }
  if ($Selection.schema -ne 2) { throw '需要 schema=2（第二版）便携配置；请先迁移旧布局。' }
  foreach ($Key in $Fixed.Keys) { if ($Selection.$Key -ne $Fixed[$Key]) { throw "固定路径字段不符：$Key（组件位置）" } }
  $Runtime = Join-Path $Root $Selection.dsh
  $Node = ''
  $MetadataPath = Join-Path $Runtime 'runtime.json'
  if (Test-Path -LiteralPath $MetadataPath -PathType Leaf) {
    $Metadata = Get-Content -LiteralPath $MetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    # node（Node.js 解释器字段）仅属于源码闭包；官方 Electron（桌面框架）运行包只有 executable（应用入口）。
    if ($Metadata.node -is [string] -and $Metadata.node) {
      $NodeCandidate = [IO.Path]::GetFullPath((Join-Path $Runtime $Metadata.node))
      if (-not $NodeCandidate.StartsWith($Runtime + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '宿主解释器路径越界。' }
      if (Test-Path -LiteralPath $NodeCandidate -PathType Leaf) { $Node = $NodeCandidate }
    }
  }
  # npm（包管理器）宿主使用系统 Node.js（运行环境）；维护者构建也可在无宿主时运行。
  if (-not $Node -or $Metadata.source_kind -eq 'npm_package') {
    $NodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($NodeCommand) { $Node = $NodeCommand.Source }
  }
  return @{ Root=$Root; Program=(Join-Path $Root $Selection.app); Python=(Join-Path $Root $Selection.python); Models=(Join-Path $Root $Selection.models); Data=(Join-Path $Root $Selection.data_dir); Dsh=$Runtime; Node=$Node; WebView2=$null }
}
function Set-LocalEnvironment {
  param($Context, [string]$Program = '')
  if (-not $Program) { $Program = $Context.Program }
  $env:VOICE_INSTALL_ROOT = $Context.Root
  $env:VOICE_PROGRAM_ROOT = $Program
  $env:VOICE_LAUNCH_CHANNEL = 'product' # product（统一正式程序）；旧渠道标签不控制管理权限。
  $env:VOICE_SOURCE_DEBUG = if ([IO.Path]::GetFullPath($Program) -ne [IO.Path]::GetFullPath($Context.Program)) {'1'} else {$null} # VOICE_SOURCE_DEBUG（显式业务源码调试）。
  $env:VOICE_DATA_DIR = $Context.Data
  $env:VOICE_MODEL_DIR = $Context.Models
  $env:VOICE_DSH_RUNTIME = $Context.Dsh
  $env:VOICE_NODE_EXECUTABLE = $Context.Node
  $env:WEBVIEW2_BROWSER_EXECUTABLE_FOLDER = $null
  $env:PYTHONPYCACHEPREFIX = Join-Path $Context.Root 'cache/python'
  $env:PIP_CACHE_DIR = Join-Path $Context.Root 'cache/pip'
  $env:HF_HOME = Join-Path $Context.Root 'cache/huggingface'
  $env:MODELSCOPE_CACHE = Join-Path $Context.Root 'cache/modelscope'
  $env:TORCH_HOME = Join-Path $Context.Root 'cache/torch'
  $env:COREPACK_HOME = Join-Path $Context.Root 'cache/corepack'
  $env:npm_config_cache = Join-Path $Context.Root 'cache/npm'
}

# Get-Sha256File（文件摘要）：使用系统 .NET（程序运行库），不依赖命令模块的自动加载。
function Get-Sha256File {
  param([string]$LiteralPath)
  $TaskStream = [IO.File]::OpenRead([IO.Path]::GetFullPath($LiteralPath))
  $TaskHasher = [Security.Cryptography.SHA256]::Create()
  try { return [BitConverter]::ToString($TaskHasher.ComputeHash($TaskStream)).Replace('-', '').ToLowerInvariant() }
  finally { $TaskStream.Dispose(); $TaskHasher.Dispose() }
}
