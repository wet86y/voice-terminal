param(
  [switch]$Offline,
  [switch]$SkipInstall,
  [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LockPath = Join-Path $RepoRoot "config\dsh\runtime.lock.json"
$RuntimeRoot = Join-Path $RepoRoot ".runtime\dsh"
$SourceDir = Join-Path $RuntimeRoot "source"
$HomeDir = Join-Path $RuntimeRoot "home"
$PatchSource = Join-Path $RepoRoot "config\dsh\web-profile.patch.yml"
$PresetSource = Join-Path $RepoRoot "integrations\dsh\voice-terminal\preset\shadow-agent"
$CliPath = Join-Path $SourceDir "apps\cli\lib\bin.js"
$PackagePath = Join-Path $SourceDir "package.json"
$BuildStatePath = Join-Path $RuntimeRoot "build-state.json"
$SessionMappingPath = Join-Path $RuntimeRoot "session-mappings.json"
$PluginDir = Join-Path $RepoRoot "integrations\dsh\voice-terminal"
$PluginArtifactDir = Join-Path $RuntimeRoot "bundles\voice-terminal"
$PluginPackageName = "@voice-terminal/dsh-plugin"
$LegacyPackageNames = @("@voice-terminal/dsh-shadow-bridge", "@voice-terminal/dsh-shadow-agent-preset", "@voice-terminal/dsh-wake-tasks")
$PluginPackagePath = Join-Path $PluginDir "package.json"
$PluginBuildInputPaths = @(
  (Join-Path $PluginDir "src"),
  (Join-Path $PluginDir "preset"),
  $PluginPackagePath,
  (Join-Path $PluginDir "components.json"),
  (Join-Path $PluginDir "build.mjs"),
  (Join-Path $PluginDir "check.mjs"),
  (Join-Path $PluginDir "host.patch.yml"),
  (Join-Path $PluginDir "tsconfig.json"),
  (Join-Path $PluginDir "tsconfig.host.json"),
  (Join-Path $PluginDir "tsconfig.client.json"),
  (Join-Path $PSScriptRoot "build-dsh-client.mjs")
)
# 对完整 lib（编译目录）取摘要，包括共享协议与新增组件，避免漏检内部运行文件。
$PluginRuntimeArtifactPaths = @(
  (Join-Path $PluginArtifactDir "package.json"),
  (Join-Path $PluginArtifactDir "cordis.patch.yml"),
  (Join-Path $PluginArtifactDir "components.json"),
  (Join-Path $PluginArtifactDir "lib")
)
$CommonPromptPath = Join-Path $RepoRoot "config\shadow_agent\instructions.md"
$DshRulesPath = Join-Path $RepoRoot "config\dsh\shadow-agent.rules.md"
$ProfileDir = Join-Path $HomeDir "profiles\web"
$ProfilePackagePath = Join-Path $ProfileDir "package.json"
$DumpConfigPath = Join-Path $RuntimeRoot "web.dump-config.yml"
$env:DSH_HOME = $HomeDir

function Invoke-Checked {
  param([string]$Command, [string[]]$Arguments, [string]$WorkingDirectory = $RepoRoot)
  Push-Location $WorkingDirectory
  try {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令失败（exit $LASTEXITCODE）：$Command $($Arguments -join ' ')" }
  } finally { Pop-Location }
}

function Compare-PathOrdinal {
  param([string]$Left, [string]$Right)
  # Compare code point by code point. `Sort-Object` uses culture collation, which
  # orders `settings.ts` before `settings-contract.ts` ('.' before '-') and would
  # break the tree hash parity with the DSH runtime's Python sort key.
  $a = $Left.ToLowerInvariant()
  $b = $Right.ToLowerInvariant()
  $limit = [Math]::Min($a.Length, $b.Length)
  for ($index = 0; $index -lt $limit; $index++) {
    if ($a[$index] -ne $b[$index]) { return [int]$a[$index] - [int]$b[$index] }
  }
  return $a.Length - $b.Length
}

function Get-TreeHash {
  param([string[]]$Paths)
  $rows = foreach ($Path in $Paths) {
    $Files = if (Test-Path -LiteralPath $Path -PathType Leaf) {
      @(Get-Item -LiteralPath $Path)
    } else {
      @(Get-ChildItem -LiteralPath $Path -File -Recurse)
    }
    # The DSH runtime re-derives this hash in Python and refuses to start DSH when
    # the two disagree, so the order must match its lowercased code-point sort.
    $Sorted = [System.Collections.Generic.List[object]]::new()
    foreach ($File in $Files) { $Sorted.Add($File) }
    $Comparer = [System.Collections.Generic.Comparer[object]]::Create(
      [Comparison[object]]{ param($x, $y) Compare-PathOrdinal $x.FullName.Substring($RepoRoot.Length) $y.FullName.Substring($RepoRoot.Length) }
    )
    $Sorted.Sort($Comparer)
    $Sorted | ForEach-Object {
      "$(($_.FullName.Substring($RepoRoot.Length)).Replace('\','/')):$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)"
    }
  }
  $bytes = [Text.Encoding]::UTF8.GetBytes(($rows -join "`n"))
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
  finally { $sha.Dispose() }
}

function Write-Utf8NoBom {
  param([string]$Path, [string]$Text)
  [System.IO.File]::WriteAllText($Path, $Text, [System.Text.UTF8Encoding]::new($false))
}

function Assert-RuntimePath {
  param([string]$Path)
  $FullPath = [IO.Path]::GetFullPath($Path)
  $FullRoot = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
  if (-not $FullPath.StartsWith($FullRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "运行目录路径越界：$FullPath" }
}

function Copy-RuntimeTree {
  param([string]$From, [string]$To)
  Assert-RuntimePath $From
  Assert-RuntimePath $To
  # /SL、/SJ（保留链接）避免把依赖目录联接的外部目标收入数据快照。
  & robocopy $From $To /E /COPY:DAT /DCOPY:DAT /SL /SJ /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "DSH 运行目录备份或恢复失败：$From → $To（退出码 $LASTEXITCODE）" }
}

function Save-RuntimeBackup {
  param([string]$Path)
  Assert-RuntimePath $Path
  New-Item -ItemType Directory -Path $Path -ErrorAction Stop | Out-Null
  $Items = foreach ($Relative in @('home', 'bundles', 'build-state.json', 'session-mappings.json', 'web.dump-config.yml')) {
    $Original = Join-Path $RuntimeRoot $Relative
    $Exists = Test-Path -LiteralPath $Original
    if ($Exists) {
      $Copy = Join-Path $Path $Relative
      if (Test-Path -LiteralPath $Original -PathType Container) { Copy-RuntimeTree $Original $Copy }
      else { Copy-Item -LiteralPath $Original -Destination $Copy -ErrorAction Stop }
    }
    @{ path = $Relative; existed = $Exists }
  }
  Write-Utf8NoBom (Join-Path $Path 'backup.json') ((@{
    commit = $CurrentHead; items = @($Items)
    fields = @{ commit = '升级前源码提交号'; items = '受管备份项目'; path = '运行根目录内的相对路径'; existed = '升级前是否存在' }
  } | ConvertTo-Json -Depth 5) + "`n")
  Write-Host "升级前的 DSH 数据、功能包及构建记录已备份：$Path" -ForegroundColor Yellow
}

function Restore-RuntimeBackup {
  param([string]$Path)
  $FailedRoot = Join-Path $RuntimeRoot ("upgrade.failed-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
  Assert-RuntimePath $FailedRoot
  New-Item -ItemType Directory -Path $FailedRoot -ErrorAction Stop | Out-Null
  if ($SourceArchive -and (Test-Path -LiteralPath $SourceArchive)) {
    Assert-RuntimePath $SourceDir
    Assert-RuntimePath $SourceArchive
    if (Test-Path -LiteralPath $SourceDir) { Move-Item -LiteralPath $SourceDir -Destination (Join-Path $FailedRoot 'source') -ErrorAction Stop }
    Move-Item -LiteralPath $SourceArchive -Destination $SourceDir -ErrorAction Stop
  }
  $Backup = Get-Content -LiteralPath (Join-Path $Path 'backup.json') -Raw | ConvertFrom-Json
  foreach ($Item in $Backup.items) {
    $Target = Join-Path $RuntimeRoot $Item.path
    Assert-RuntimePath $Target
    if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination (Join-Path $FailedRoot $Item.path) -ErrorAction Stop }
    if ($Item.existed) {
      $Original = Join-Path $Path $Item.path
      if (Test-Path -LiteralPath $Original -PathType Container) { Copy-RuntimeTree $Original $Target }
      else { Copy-Item -LiteralPath $Original -Destination $Target -ErrorAction Stop }
    }
  }
  Write-Host "旧 DSH 运行组合已恢复；失败产物保留于 $FailedRoot" -ForegroundColor Yellow
}

function Update-ManagedProfilePatch {
  param([string]$ProfilePatchPath)
  if (-not (Test-Path -LiteralPath $ProfilePatchPath)) {
    Copy-Item -LiteralPath $PatchSource -Destination $ProfilePatchPath
  }
  $Text = Get-Content -LiteralPath $ProfilePatchPath -Raw
  # 保留旧 Loader（加载器）行的设置值，只更新安装包名称。
  foreach ($Pair in @(@('@voice-terminal/dsh-shadow-bridge', $PluginPackageName), @('@voice-terminal/dsh-wake-tasks', "$PluginPackageName/wake-tasks"))) {
    $Pattern = '(?m)^([ \t]+name:[ \t]+)([''"]?)' + [regex]::Escape($Pair[0]) + '\2([ \t]*(?:#[^\r\n]*)?)(\r?)$'
    $Replacement = '$1$2' + $Pair[1] + '$2$3$4'
    $Text = [regex]::Replace($Text, $Pattern, $Replacement)
  }
  # 原生 Schedule（会话调度）启用时保留用户补丁；关闭时只清理旧受管禁用行。
  if (-not (Test-NativeScheduleEnabled)) {
    foreach ($ScheduleId in @('schedule', 'ui-schedule')) {
      $BlockPattern = "(?ms)^- id: $ScheduleId[ \t]*\r?\n.*?(?=^- id:|\z)"
      $Text = [regex]::Replace($Text, $BlockPattern, [System.Text.RegularExpressions.MatchEvaluator]{
        param($Match)
        $Lines = @($Match.Value -split "`r?`n" | ForEach-Object { ($_ -split '#', 2)[0].TrimEnd() } | Where-Object { $_.Trim() })
        if ($Lines.Count -eq 2 -and $Lines[1] -match '^[ \t]+disabled:[ \t]+true$') { return '' }
        return $Match.Value
      })
    }
  }
  # DSH 0.1.7 使用 agent-preset-registry（Agent 预设注册器）；旧 agent-presets 注册行已废弃。
  $Text = [regex]::Replace($Text, '(?ms)^- id: agent-presets[ \t]*\r?\n.*?(?=^- id:|\z)', '')
  $Registry = [regex]::Match($Text, '(?ms)^- id: agent-preset-registry[ \t]*\r?\n.*?(?=^- id:|\z)')
  if (-not $Registry.Success) {
    $Text = "- id: agent-preset-registry`n  config:`n    default: shadow-agent`n`n$Text"
  } else {
    $Block = $Registry.Value
    $Default = [regex]::new('(?m)^([ \t]*)default:[^\r\n]*')
    if ($Default.IsMatch($Block)) {
      $Block = $Default.Replace($Block, '$1default: shadow-agent', 1)
    } else {
      $Config = [regex]::Match($Block, '(?m)^([ \t]*)config:[ \t]*$')
      if ($Config.Success) {
        $Block = $Block.Insert($Config.Index + $Config.Length, "`n$($Config.Groups[1].Value)  default: shadow-agent")
      } else {
        $Header = [regex]::Match($Block, '(?m)^- id: agent-preset-registry[ \t]*$')
        $Block = $Block.Insert($Header.Index + $Header.Length, "`n  config:`n    default: shadow-agent")
      }
    }
    $Text = $Text.Substring(0, $Registry.Index) + $Block + $Text.Substring($Registry.Index + $Registry.Length)
  }
  Write-Utf8NoBom -Path $ProfilePatchPath -Text ($Text.TrimStart([char[]]"`r`n").TrimEnd() + "`n")
}

function Test-NativeScheduleEnabled {
  if (Test-Path -LiteralPath $ProfilePackagePath -PathType Leaf) {
    $ExistingProfile = Get-Content -LiteralPath $ProfilePackagePath -Raw | ConvertFrom-Json
    return $ExistingProfile.dsh.profile.bundles -contains '@deepseek-ai/dsh-experimental-schedule-bundle'
  }
  return $false
}

if (-not (Test-Path -LiteralPath $LockPath)) { throw "DSH 锁文件缺失：$LockPath" }
foreach ($Required in @($PluginPackagePath, (Join-Path $PluginDir "src\bridge\plugin.ts"), (Join-Path $PluginDir "src\wake-tasks\plugin.ts"), (Join-Path $PresetSource "preset.yml"), (Join-Path $PresetSource "agent.cordis.yml"), $CommonPromptPath, $DshRulesPath)) {
  if (-not (Test-Path -LiteralPath $Required)) { throw "DSH 受管源文件缺失：$Required" }
}
$Lock = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
$IsWindowsHost = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
if (-not $IsWindowsHost -or $env:PROCESSOR_ARCHITECTURE -notin @("AMD64", "x86_64")) { throw "当前 DSH 基线仅正式支持 Windows x64。" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "需要 Git 才能取得并验证 DSH 源码。" }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "需要 Node.js $($Lock.minimum_node_version) 或更高兼容版本。" }
$NodeVersion = (& node --version).Trim().TrimStart('v')
$Node = [version]$NodeVersion
$NodeSupported = (($Node.Major -eq 22 -and $Node -ge [version]$Lock.minimum_node_version) -or $Node.Major -ge 24)
if (-not $NodeSupported) { throw "Node.js 版本不受支持：$NodeVersion；需要 ^22.19.0 或 >=24.0.0。" }

if (-not $ValidateOnly) {
  $VoiceServerPort = 8765
  foreach ($VoiceConfigPath in @(
    (Join-Path $RepoRoot "config\default.yaml"),
    (Join-Path $RepoRoot "config\local.yaml")
  )) {
    if (-not (Test-Path -LiteralPath $VoiceConfigPath -PathType Leaf)) { continue }
    $VoiceConfigText = Get-Content -LiteralPath $VoiceConfigPath -Raw
    $ServerBlock = [regex]::Match($VoiceConfigText, '(?ms)^server:[ \t]*\r?\n(?<block>(?:[ \t].*(?:\r?\n|$))*)')
    if ($ServerBlock.Success) {
      $PortMatch = [regex]::Match($ServerBlock.Groups['block'].Value, '(?m)^[ \t]+port:[ \t]*(\d+)\s*$')
      if ($PortMatch.Success) { $VoiceServerPort = [int]$PortMatch.Groups[1].Value }
    }
  }
  if (-not [string]::IsNullOrWhiteSpace($env:VOICE_SERVER_PORT)) {
    $EnvironmentVoicePort = 0
    $EnvironmentVoicePortValid = [int]::TryParse($env:VOICE_SERVER_PORT, [ref]$EnvironmentVoicePort)
    if (-not $EnvironmentVoicePortValid -or $EnvironmentVoicePort -lt 1 -or $EnvironmentVoicePort -gt 65535) {
      throw "VOICE_SERVER_PORT 必须为 1～65535 的端口号。"
    }
    $VoiceServerPort = $EnvironmentVoicePort
  }
  if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "无法确认 Voice Server 是否已停止；请在安装前停止服务并确保 Get-NetTCPConnection 可用。"
  }
  $VoiceListeners = @(Get-NetTCPConnection -State Listen -LocalPort $VoiceServerPort -ErrorAction SilentlyContinue)
  if ($VoiceListeners.Count -gt 0) {
    throw "检测到 Voice Server 端口 $VoiceServerPort 仍在监听；请先停止 Voice Server，再部署 Bridge 2.0。"
  }
}

$SourceExisted = Test-Path -LiteralPath $SourceDir -PathType Container
$CurrentHead = $null
$SourceArchive = $null
$UpgradeBackupPath = $null
if ($SourceExisted) {
  $Dirty = @(& git -C $SourceDir status --porcelain)
  if ($LASTEXITCODE -ne 0) { throw "现有 DSH source 不是可验证的 Git checkout：$SourceDir" }
  if ($Dirty.Count -gt 0) { throw "DSH source 含本地修改，拒绝覆盖：$SourceDir" }

  $Origin = (& git -C $SourceDir remote get-url origin).Trim()
  if ($LASTEXITCODE -ne 0) { throw "无法读取现有 DSH source 的 origin：$SourceDir" }
  $ExpectedOrigin = ([string]$Lock.repository).Trim().TrimEnd('/') -replace '\.git$', ''
  $Origin = $Origin -replace '^git@github\.com:', 'https://github.com/'
  $Origin = $Origin.Trim().TrimEnd('/') -replace '\.git$', ''
  if ($Origin -ne $ExpectedOrigin) { throw "现有 DSH source 仓库不匹配：期望 $ExpectedOrigin，实际 $Origin。" }

  $CurrentHead = (& git -C $SourceDir rev-parse HEAD).Trim()
  if ($LASTEXITCODE -ne 0) { throw "无法读取现有 DSH source 的 HEAD：$SourceDir" }
  if ($CurrentHead -ne $Lock.commit -and ($ValidateOnly -or $SkipInstall)) { throw "DSH source（源码）版本漂移：当前 $CurrentHead，期望 $($Lock.commit)；请运行完整安装。" }
} elseif ($Offline -or $SkipInstall -or $ValidateOnly) {
  throw "离线/校验模式要求 DSH 源码已存在：$SourceDir"
}

if (-not $ValidateOnly) {
  $Stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
  $UpgradeBackupPath = Join-Path $RuntimeRoot ("upgrade.backup-{0}-{1}" -f $Lock.version, $Stamp)
  Save-RuntimeBackup $UpgradeBackupPath
}

try {
if (-not $ValidateOnly -and -not $SkipInstall) {
  # 干净暂存源码保留旧版本依赖与生成产物，构建失败可完整恢复。
  $StagedSource = Join-Path $RuntimeRoot ("source.staging-{0}-{1}" -f $Lock.version, $Stamp)
  Assert-RuntimePath $StagedSource
  if ($Offline) {
    Invoke-Checked -Command git -Arguments @("clone", "--no-local", "--no-checkout", $SourceDir, $StagedSource)
    Invoke-Checked -Command git -Arguments @("-C", $StagedSource, "remote", "set-url", "origin", $Lock.repository)
    Invoke-Checked -Command git -Arguments @("-C", $StagedSource, "checkout", "--detach", $Lock.commit)
  } else {
    Invoke-Checked -Command git -Arguments @("clone", "--depth", "1", "--branch", $Lock.tag, "--single-branch", $Lock.repository, $StagedSource)
  }
  $StagedHead = (& git -C $StagedSource rev-parse HEAD).Trim()
  $StagedTag = (& git -C $StagedSource describe --tags --exact-match HEAD).Trim()
  $StagedDirty = @(& git -C $StagedSource status --porcelain)
  if ($LASTEXITCODE -ne 0 -or $StagedHead -ne $Lock.commit -or $StagedTag -ne $Lock.tag -or $StagedDirty.Count -gt 0) {
    throw "DSH source staging checkout failed verification; original source remains at $SourceDir."
  }

  if ($SourceExisted) {
    $SourceArchive = Join-Path $RuntimeRoot ("source.backup-{0}-{1}" -f $CurrentHead.Substring(0, 12), $Stamp)
    Assert-RuntimePath $SourceDir
    Assert-RuntimePath $SourceArchive
    Move-Item -LiteralPath $SourceDir -Destination $SourceArchive -ErrorAction Stop
    Write-Host "旧 DSH source（源码）及其构建产物已留档：$SourceArchive" -ForegroundColor Yellow
  }
  Move-Item -LiteralPath $StagedSource -Destination $SourceDir -ErrorAction Stop
}

if (-not $ValidateOnly) { New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null }

$Head = (& git -C $SourceDir rev-parse HEAD).Trim()
$Tag = (& git -C $SourceDir describe --tags --exact-match HEAD).Trim()
$Package = Get-Content -LiteralPath $PackagePath -Raw | ConvertFrom-Json
if ($Head -ne $Lock.commit) { throw "DSH commit 漂移：期望 $($Lock.commit)，实际 $Head。" }
if ($Tag -ne $Lock.tag) { throw "DSH tag 漂移：期望 $($Lock.tag)，实际 $Tag。" }
if ($Package.version -ne $Lock.version) { throw "DSH package 版本漂移：期望 $($Lock.version)，实际 $($Package.version)。" }

$PnpmVersion = $Lock.package_manager -replace '^pnpm@', ''
if (-not $SkipInstall -and -not $ValidateOnly) {
  Invoke-Checked -Command corepack -Arguments @("prepare", "pnpm@$PnpmVersion", "--activate")
  # Windows optional payload（Windows 可选二进制包）体积较大，下载可能耗时数分钟；串行获取并延长网络超时。
  $InstallArgs = @("install", "--frozen-lockfile", "--network-concurrency=1", "--fetch-timeout=900000")
  if ($Offline) { $InstallArgs += "--offline" }
  # Corepack（Node.js 包管理器代理）先固定 pnpm（包管理器）版本；DSH 的 packageManager（包管理器版本字段）约束嵌套命令。
  Invoke-Checked -Command pnpm -Arguments $InstallArgs -WorkingDirectory $SourceDir
  Invoke-Checked -Command pnpm -Arguments @("run", "verify-third-party-notices") -WorkingDirectory $SourceDir
  Invoke-Checked -Command pnpm -Arguments @("run", "verify-dsh-package-licenses") -WorkingDirectory $SourceDir
  # 每次构建前清理生成的 project-reference（项目引用）产物；目标版的 `clean`（清理）脚本会误拒自身桌面键盘测试的 outDir（输出目录）。
  Invoke-Checked -Command pnpm -Arguments @("exec", "tsc", "-b", "--clean") -WorkingDirectory $SourceDir
  Invoke-Checked -Command pnpm -Arguments @("run", "build") -WorkingDirectory $SourceDir
  Invoke-Checked -Command pnpm -Arguments @("run", "verify-runtime-closure") -WorkingDirectory $SourceDir
}

if (-not (Test-Path -LiteralPath $CliPath)) { throw "DSH CLI 构建产物缺失：$CliPath" }
$ActualPnpm = (& pnpm --dir $SourceDir --version).Trim()
if ($LASTEXITCODE -ne 0 -or $ActualPnpm -ne $PnpmVersion) { throw "pnpm 版本不匹配：期望 $PnpmVersion，实际 $ActualPnpm。" }
$CliVersion = (& node $CliPath --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $CliVersion -notmatch [regex]::Escape($Lock.version)) { throw "DSH CLI 版本验证失败：$CliVersion" }

$PluginPackage = Get-Content -LiteralPath $PluginPackagePath -Raw | ConvertFrom-Json
$PluginVersion = [string]$PluginPackage.version
$BridgeProtocolVersion = "2.0"
$PromptHash = Get-TreeHash @($CommonPromptPath, $DshRulesPath)

if (-not $ValidateOnly) {
  Invoke-Checked -Command node -Arguments @((Join-Path $PluginDir "build.mjs"), $PluginArtifactDir)
}

if ($ValidateOnly) {
  $ProfilePatchPath = Join-Path $HomeDir "profiles\web\cordis.patch.yml"
  if (-not (Test-Path -LiteralPath $ProfilePatchPath)) { throw "DSH_HOME 缺少 Web profile patch（Web 配置补丁）：$ProfilePatchPath" }
  $ProfilePatchText = Get-Content -LiteralPath $ProfilePatchPath -Raw
  if ($ProfilePatchText -notmatch '(?m)^- id: agent-preset-registry\s*$' -or $ProfilePatchText -notmatch '(?m)^\s+default: shadow-agent\s*$') {
    throw "DSH_HOME Web profile patch（Web 配置补丁）缺少正式 Shadow preset（Shadow 预设）默认项。"
  }
  if ($ProfilePatchText -notmatch '(?m)^\s*- id: session-log-deepseek\s*$' -or $ProfilePatchText -notmatch '(?m)^\s+disabled: true\s*$') {
    throw "DSH_HOME Web profile patch（Web 配置补丁）缺少预期的 session-log-deepseek 设置。"
  }
  if (-not (Test-Path -LiteralPath $BuildStatePath)) { throw "DSH build-state.json 缺失。" }
  if (-not (Test-Path -LiteralPath $ProfilePackagePath)) { throw "DSH Web profile（网页配置）清单缺失。" }
  $ProfilePackage = Get-Content -LiteralPath $ProfilePackagePath -Raw | ConvertFrom-Json
  if ($ProfilePackage.dependencies.PSObject.Properties.Name -notcontains $PluginPackageName -or
      $ProfilePackage.dsh.profile.bundles -notcontains $PluginPackageName) { throw "DSH 统一功能包未安装到项目网页配置。" }
  foreach ($LegacyName in $LegacyPackageNames) {
    if ($ProfilePackage.dependencies.PSObject.Properties.Name -contains $LegacyName -or
        $ProfilePackage.dsh.profile.bundles -contains $LegacyName) { throw "旧 DSH 功能包仍注册，可能重复加载：$LegacyName" }
  }
  foreach ($RuntimeArtifactPath in $PluginRuntimeArtifactPaths) {
    if (-not (Test-Path -LiteralPath $RuntimeArtifactPath)) { throw "DSH 插件运行产物缺失：$RuntimeArtifactPath" }
  }
  $ArtifactPackage = Get-Content -LiteralPath (Join-Path $PluginArtifactDir "package.json") -Raw | ConvertFrom-Json
  if ($ArtifactPackage.main -ne 'lib/bridge/plugin.js' -or $ArtifactPackage.exports.'.' -ne './lib/bridge/plugin.js') { throw "DSH 桥接组件入口错误。" }
  if ($ArtifactPackage.exports.'./wake-tasks' -ne './lib/wake-tasks/plugin.js') { throw "DSH 唤醒任务组件入口错误。" }
  if ($ArtifactPackage.exports.'./client' -ne './lib/client.js' -or $ArtifactPackage.dsh.client.platform -ne 'web') { throw "DSH 浏览器组件声明错误。" }
  foreach ($Entry in @('lib\bridge\plugin.js', 'lib\wake-tasks\plugin.js', 'lib\client.js')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PluginArtifactDir $Entry))) { throw "DSH 组件产物缺失：$Entry" }
  }
  $State = Get-Content -LiteralPath $BuildStatePath -Raw | ConvertFrom-Json
  if ($State.commit -ne $Lock.commit -or $State.version -ne $Lock.version -or $State.tag -ne $Lock.tag) { throw "DSH 构建记录与版本锁不匹配。" }
  if ($State.schema_version -ne 6) { throw "DSH build-state（构建记录）格式已过期；请重新运行 setup-dsh.ps1。" }
  if ($State.plugin_bundle_version -ne $PluginVersion) { throw "DSH 统一插件版本漂移。" }
  if ($State.bridge_protocol_version -ne $BridgeProtocolVersion) { throw "DSH 桥接协议版本漂移。" }
  if ($State.plugin_runtime_artifacts_sha256 -ne (Get-TreeHash @($PluginRuntimeArtifactPaths))) { throw "DSH 插件运行产物 hash（摘要）漂移。" }
  if ($State.plugin_build_inputs_sha256 -ne (Get-TreeHash @($PluginBuildInputPaths))) { throw "DSH 插件构建输入 hash（摘要）漂移。" }
  if ($State.shadow_prompt_sha256 -ne $PromptHash) { throw "Shadow 公共提示词或 DSH 附加规则已漂移。" }
}

if (-not $ValidateOnly) {
  if (Test-Path -LiteralPath $SessionMappingPath -PathType Leaf) {
    $MappingSchemaVersion = $null
    try { $MappingSchemaVersion = (Get-Content -LiteralPath $SessionMappingPath -Raw | ConvertFrom-Json).schema_version } catch { }
    if ($MappingSchemaVersion -ne 2) {
      $MappingArchivePath = Join-Path $RuntimeRoot ("session-mappings.v1.archive-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
      Move-Item -LiteralPath $SessionMappingPath -Destination $MappingArchivePath
      Write-Host "旧版 Session mapping（会话映射）已归档到 $MappingArchivePath" -ForegroundColor Yellow
    }
  }
  if (-not (Test-Path -LiteralPath $SessionMappingPath -PathType Leaf)) {
    Write-Utf8NoBom -Path $SessionMappingPath -Text (([ordered]@{ schema_version = 2; sessions = @{} } | ConvertTo-Json -Depth 4) + "`n")
  }
  New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
  $ProfilePatchPath = Join-Path $ProfileDir "cordis.patch.yml"
  Invoke-Checked -Command node -Arguments @(
    (Join-Path $PSScriptRoot "check-dsh-wake-migration.mjs"),
    (Join-Path $RepoRoot "config\wake-tasks.json")
  )
  Update-ManagedProfilePatch -ProfilePatchPath $ProfilePatchPath
  # 先移除旧安装记录，再添加统一功能包；外层事务备份保证失败时可恢复。
  if (Test-Path -LiteralPath $ProfilePackagePath) {
    $Before = Get-Content -LiteralPath $ProfilePackagePath -Raw | ConvertFrom-Json
    foreach ($LegacyName in $LegacyPackageNames) {
      if ($Before.dependencies.PSObject.Properties.Name -contains $LegacyName -or $Before.dsh.profile.bundles -contains $LegacyName) {
        $RemoveArgs = @($CliPath, "plugin", "--profile", "web", "remove", $LegacyName)
        # pnpm remove（移除依赖）不接受 --offline 参数；通过配置参数维持离线约束。
        if ($Offline) { $RemoveArgs += "--config.offline=true" }
        Invoke-Checked -Command node -Arguments $RemoveArgs
      }
    }
  }
  $PluginArgs = @($CliPath, "plugin", "--profile", "web", "add", $PluginArtifactDir, "--save-exact")
  if ($Offline) { $PluginArgs += "--offline" }
  Invoke-Checked -Command node -Arguments $PluginArgs
  $DumpConfig = (& node $CliPath --profile web --dump-config 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) { throw "DSH dump-config 失败。" }
  foreach ($RequiredRow in @("voice-terminal-shadow-bridge", "voice-terminal-wake-tasks", "preset-shadow-agent")) {
    if ($DumpConfig -notmatch [regex]::Escape($RequiredRow)) { throw "DSH dump-config（展开配置）缺少组件：$RequiredRow" }
  }
  if ($DumpConfig -notmatch [regex]::Escape($PluginPackageName)) { throw "DSH 展开配置缺少统一功能包。" }
  $DumpConfig | Set-Content -LiteralPath $DumpConfigPath -Encoding utf8
  $ManagedHash = Get-TreeHash @((Split-Path -Parent $PatchSource), $PresetSource)
  $RuntimeArtifactHash = Get-TreeHash @($PluginRuntimeArtifactPaths)
  $PluginInputsHash = Get-TreeHash @($PluginBuildInputPaths)
  [ordered]@{
    schema_version = 6
    repository = $Lock.repository
    tag = $Tag
    commit = $Head
    version = $Package.version
    node_version = $NodeVersion
    pnpm_version = $ActualPnpm
    managed_config_sha256 = $ManagedHash
    plugin_bundle_version = $PluginVersion
    bridge_protocol_version = $BridgeProtocolVersion
    plugin_runtime_artifacts_sha256 = $RuntimeArtifactHash
    plugin_build_inputs_sha256 = $PluginInputsHash
    shadow_prompt_sha256 = $PromptHash
    source_dir = $SourceDir
    home_dir = $HomeDir
    generated_at = [DateTimeOffset]::UtcNow.ToString("O")
  } | ConvertTo-Json | Set-Content -LiteralPath $BuildStatePath -Encoding utf8
}

Write-Host "DSH $($Lock.version) 校验完成；source=$SourceDir；home=$HomeDir" -ForegroundColor Green
} catch {
  $InstallFailure = $_
  if ($UpgradeBackupPath) {
    try { Restore-RuntimeBackup $UpgradeBackupPath }
    catch { throw "DSH 安装失败，自动恢复也失败；请保留备份 $UpgradeBackupPath。安装错误：$InstallFailure；恢复错误：$_" }
  }
  throw $InstallFailure
}
