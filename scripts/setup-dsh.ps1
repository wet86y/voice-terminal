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
$PresetSource = Join-Path $RepoRoot "config\dsh\agent-presets\shadow-agent"
$CliPath = Join-Path $SourceDir "apps\cli\lib\bin.js"
$PackagePath = Join-Path $SourceDir "package.json"
$BuildStatePath = Join-Path $RuntimeRoot "build-state.json"
$SessionMappingPath = Join-Path $RuntimeRoot "session-mappings.json"
$BridgeDir = Join-Path $RepoRoot "integrations\dsh\voice-terminal-shadow-bridge"
$BridgeArtifactDir = Join-Path $RuntimeRoot "bundles\voice-terminal-shadow-bridge"
$WakeTasksDir = Join-Path $RepoRoot "integrations\dsh\voice-terminal-wake-tasks"
$WakeTasksArtifactDir = Join-Path $RuntimeRoot "bundles\voice-terminal-wake-tasks"
$WakeTasksPackageName = "@voice-terminal/dsh-wake-tasks"
$WakeTasksPackagePath = Join-Path $WakeTasksArtifactDir "package.json"
$WakeTasksBuildInputPaths = @(
  (Join-Path $WakeTasksDir "src"),
  (Join-Path $WakeTasksDir "package.json"),
  (Join-Path $WakeTasksDir "build.mjs"),
  (Join-Path $WakeTasksDir "cordis.patch.yml")
)
$WakeTasksRuntimeArtifactPaths = @(
  $WakeTasksPackagePath,
  (Join-Path $WakeTasksArtifactDir "cordis.patch.yml"),
  (Join-Path $WakeTasksArtifactDir "lib\event-id.js"),
  (Join-Path $WakeTasksArtifactDir "lib\index.js"),
  (Join-Path $WakeTasksArtifactDir "lib\task-service.js"),
  (Join-Path $WakeTasksArtifactDir "lib\plugin.js")
)
$ShadowPresetArtifactDir = Join-Path $RuntimeRoot "bundles\voice-terminal-shadow-preset"
$ShadowPresetPackageName = "@voice-terminal/dsh-shadow-agent-preset"
$ShadowPresetPackagePath = Join-Path $ShadowPresetArtifactDir "package.json"
$ShadowPresetPatchPath = Join-Path $ShadowPresetArtifactDir "cordis.patch.yml"
$ShadowPresetBuildInputPaths = @($PresetSource)
$ShadowPresetRuntimeArtifactPaths = @($ShadowPresetPackagePath, $ShadowPresetPatchPath)
$BridgePackagePath = Join-Path $BridgeDir "package.json"
$BridgeEntryPath = Join-Path $BridgeArtifactDir "lib\index.js"
$BridgePluginEntryPath = Join-Path $BridgeArtifactDir "lib\plugin.js"
$BridgeClientEntryPath = Join-Path $BridgeArtifactDir "lib\client.js"
$BridgeBuildInputPaths = @(
  (Join-Path $BridgeDir "src"),
  $BridgePackagePath,
  (Join-Path $BridgeDir "build.mjs"),
  (Join-Path $BridgeDir "cordis.patch.yml"),
  (Join-Path $PSScriptRoot "build-dsh-client.mjs")
)
$BridgeRuntimeArtifactPaths = @(
  (Join-Path $BridgeArtifactDir "package.json"),
  (Join-Path $BridgeArtifactDir "cordis.patch.yml"),
  (Join-Path $BridgeArtifactDir "lib\index.js"),
  (Join-Path $BridgeArtifactDir "lib\plugin.js"),
  (Join-Path $BridgeArtifactDir "lib\settings.js"),
  (Join-Path $BridgeArtifactDir "lib\settings-contract.js"),
  (Join-Path $BridgeArtifactDir "lib\generated-prompt.js"),
  (Join-Path $BridgeArtifactDir "lib\client.js")
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

function Get-ShadowPresetBundlePatch {
  $MetadataPath = Join-Path $PresetSource "preset.yml"
  $CompositionPath = Join-Path $PresetSource "agent.cordis.yml"
  $Metadata = Get-Content -LiteralPath $MetadataPath -Raw
  $NameMatch = [regex]::Match($Metadata, '(?m)^name:\s*(.+?)\s*$')
  $DescriptionMatch = [regex]::Match($Metadata, '(?m)^description:\s*(.+?)\s*$')
  if (-not $NameMatch.Success -or -not $DescriptionMatch.Success) { throw "Shadow preset 元数据（预设说明）缺少 name/description（名称/说明字段）。" }
  $NameYaml = $NameMatch.Groups[1].Value | ConvertTo-Json -Compress
  $DescriptionYaml = $DescriptionMatch.Groups[1].Value | ConvertTo-Json -Compress
  $Composition = (Get-Content -LiteralPath $CompositionPath -Raw).TrimEnd([char[]]"`r`n")
  $IndentedComposition = (($Composition -split "`r?`n") | ForEach-Object { "          $_" }) -join "`n"
  return @"
- insert:
    - id: preset-shadow-agent
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: shadow-agent
        name: $NameYaml
        description: $DescriptionYaml
        order: 5
        plugins:
$IndentedComposition
"@
}

function Update-ManagedProfilePatch {
  param([string]$ProfilePatchPath)
  if (-not (Test-Path -LiteralPath $ProfilePatchPath)) {
    Copy-Item -LiteralPath $PatchSource -Destination $ProfilePatchPath
  }
  $Text = Get-Content -LiteralPath $ProfilePatchPath -Raw
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
foreach ($Required in @($BridgePackagePath, (Join-Path $BridgeDir "src\index.ts"), (Join-Path $WakeTasksDir "src\index.ts"), (Join-Path $WakeTasksDir "src\plugin.ts"), (Join-Path $PresetSource "preset.yml"), (Join-Path $PresetSource "agent.cordis.yml"), $CommonPromptPath, $DshRulesPath)) {
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

$BridgePackage = Get-Content -LiteralPath $BridgePackagePath -Raw | ConvertFrom-Json
$BridgeVersion = [string]$BridgePackage.version
$BridgeProtocolVersion = "2.0"
$PromptHash = Get-TreeHash @($CommonPromptPath, $DshRulesPath)
$PresetText = Get-Content -LiteralPath (Join-Path $PresetSource "agent.cordis.yml") -Raw
$ShadowPresetPackageText = ([ordered]@{
  name = $ShadowPresetPackageName
  version = "0.1.1"
  private = $true
  type = "module"
  dsh = @{ bundle = @{ patch = "./cordis.patch.yml" } }
} | ConvertTo-Json -Depth 8) + "`n"
$ShadowPresetPatchText = Get-ShadowPresetBundlePatch
foreach ($RequiredPresetRow in @("tool-pwsh", "tool-fs", "tool-jobs", "tool-skill", "compaction-basic", "tool-subagent", "tool-web", "workflow-ptc")) {
  if ($PresetText -notmatch [regex]::Escape("id: $RequiredPresetRow")) { throw "正式 Shadow preset 缺少默认工具/能力：$RequiredPresetRow" }
}
if ($PresetText -match "tools\.restrict|exact safe tool surface") { throw "正式 Shadow preset 不得缩减默认工具可见性。" }

if (-not $ValidateOnly) {
  Invoke-Checked -Command node -Arguments @((Join-Path $BridgeDir "build.mjs"), $BridgeArtifactDir)
  Invoke-Checked -Command node -Arguments @((Join-Path $WakeTasksDir "build.mjs"), $WakeTasksArtifactDir)
  if (-not (Test-Path -LiteralPath $BridgeEntryPath)) { throw "DSH Bridge 核心构建产物缺失：$BridgeEntryPath" }
  if (-not (Test-Path -LiteralPath $BridgePluginEntryPath)) { throw "DSH Bridge 插件入口缺失：$BridgePluginEntryPath" }
  if (-not (Test-Path -LiteralPath (Join-Path $WakeTasksArtifactDir "lib\plugin.js"))) { throw "唤醒任务插件入口缺失。" }
  Invoke-Checked -Command node -Arguments @((Join-Path $PSScriptRoot "build-dsh-client.mjs"), $BridgeDir, $BridgeArtifactDir, $SourceDir)
  if (-not (Test-Path -LiteralPath $BridgeClientEntryPath)) { throw "DSH Bridge 浏览器插件产物缺失：$BridgeClientEntryPath" }
}

if ($ValidateOnly) {
  foreach ($RuntimeArtifactPath in $ShadowPresetRuntimeArtifactPaths) {
    if (-not (Test-Path -LiteralPath $RuntimeArtifactPath -PathType Leaf)) { throw "Shadow preset Bundle（Shadow 预设功能包）产物缺失：$RuntimeArtifactPath" }
  }
  if ((Get-Content -LiteralPath $ShadowPresetPackagePath -Encoding UTF8 -Raw) -ne $ShadowPresetPackageText) { throw "Shadow preset Bundle（Shadow 预设功能包）清单已漂移。" }
  if ((Get-Content -LiteralPath $ShadowPresetPatchPath -Encoding UTF8 -Raw) -ne $ShadowPresetPatchText) { throw "Shadow preset Bundle（Shadow 预设功能包）配置补丁已漂移。" }
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
  if (-not (Test-Path -LiteralPath $BridgeEntryPath)) { throw "DSH Bridge 构建产物缺失。" }
  if (-not (Test-Path -LiteralPath $BridgePluginEntryPath)) { throw "DSH Bridge 插件入口缺失。" }
  if (-not (Test-Path -LiteralPath $BridgeClientEntryPath)) { throw "DSH Bridge 浏览器插件产物缺失。" }
  foreach ($RuntimeArtifactPath in $WakeTasksRuntimeArtifactPaths) {
    if (-not (Test-Path -LiteralPath $RuntimeArtifactPath -PathType Leaf)) { throw "唤醒任务插件运行产物缺失：$RuntimeArtifactPath" }
  }
  if (-not (Test-Path -LiteralPath $ProfilePackagePath)) { throw "DSH web profile manifest 缺失。" }
  $ProfilePackage = Get-Content -LiteralPath $ProfilePackagePath -Raw | ConvertFrom-Json
  if (-not $ProfilePackage.dependencies.'@voice-terminal/dsh-shadow-bridge') { throw "DSH Bridge 未安装到 web profile。" }
  if ($ProfilePackage.dsh.profile.bundles -notcontains '@voice-terminal/dsh-shadow-bridge') { throw "DSH Bridge bundle 未注册到 web profile。" }
  if ($ProfilePackage.dependencies.PSObject.Properties.Name -notcontains $ShadowPresetPackageName) { throw "Shadow preset Bundle（Shadow 预设功能包）未安装到 web profile。" }
  if ($ProfilePackage.dsh.profile.bundles -notcontains $ShadowPresetPackageName) { throw "Shadow preset Bundle（Shadow 预设功能包）未注册到 web profile。" }
  if ($ProfilePackage.dependencies.PSObject.Properties.Name -notcontains $WakeTasksPackageName) { throw "唤醒任务插件未安装到 web profile。" }
  if ($ProfilePackage.dsh.profile.bundles -notcontains $WakeTasksPackageName) { throw "唤醒任务插件未注册到 web profile。" }
  $ArtifactPackagePath = Join-Path $BridgeArtifactDir "package.json"
  if (-not (Test-Path -LiteralPath $ArtifactPackagePath)) { throw "DSH Bridge artifact package.json 缺失。" }
  foreach ($RuntimeArtifactPath in $BridgeRuntimeArtifactPaths) {
    if (-not (Test-Path -LiteralPath $RuntimeArtifactPath -PathType Leaf)) { throw "DSH Bridge 运行产物缺失：$RuntimeArtifactPath" }
  }
  $ArtifactPackage = Get-Content -LiteralPath $ArtifactPackagePath -Raw | ConvertFrom-Json
  if ($ArtifactPackage.main -ne 'lib/plugin.js' -or $ArtifactPackage.exports.'.' -ne './lib/plugin.js') { throw "DSH Bridge 插件包入口未指向带配置模式的 plugin.js。" }
  if ($ArtifactPackage.exports.'./client' -ne './lib/client.js') { throw "DSH Bridge 浏览器入口未导出。" }
  if ($ArtifactPackage.dsh.client.platform -ne 'web') { throw "DSH Bridge 未声明 web client bundle。" }
  $WakeTasksArtifactPackage = Get-Content -LiteralPath $WakeTasksPackagePath -Raw | ConvertFrom-Json
  if ($WakeTasksArtifactPackage.main -ne 'lib/plugin.js' -or $WakeTasksArtifactPackage.exports.'.' -ne './lib/plugin.js') { throw "唤醒任务插件入口未指向 plugin.js。" }
  $State = Get-Content -LiteralPath $BuildStatePath -Raw | ConvertFrom-Json
  if ($State.commit -ne $Lock.commit -or $State.version -ne $Lock.version -or $State.tag -ne $Lock.tag) { throw "DSH 构建记录与版本锁不匹配。" }
  if ($State.schema_version -ne 5) { throw "DSH build-state 格式版本已过期；请重新运行 setup-dsh.ps1。" }
  $CurrentRuntimeArtifactHash = Get-TreeHash @($BridgeRuntimeArtifactPaths)
  $CurrentBuildInputsHash = Get-TreeHash @($BridgeBuildInputPaths)
  $CurrentShadowPresetRuntimeHash = Get-TreeHash @($ShadowPresetRuntimeArtifactPaths)
  $CurrentShadowPresetBuildInputsHash = Get-TreeHash @($ShadowPresetBuildInputPaths)
  $CurrentWakeTasksRuntimeHash = Get-TreeHash @($WakeTasksRuntimeArtifactPaths)
  $CurrentWakeTasksBuildInputsHash = Get-TreeHash @($WakeTasksBuildInputPaths)
  if ($State.bridge_protocol_version -ne $BridgeProtocolVersion) { throw "DSH Bridge 协议版本漂移。" }
  if ($State.bridge_runtime_artifacts_sha256 -ne $CurrentRuntimeArtifactHash) { throw "DSH Bridge 运行产物 hash 漂移。" }
  if ($State.bridge_build_inputs_sha256 -ne $CurrentBuildInputsHash) { throw "DSH Bridge 构建输入 hash 漂移。" }
  if ($State.shadow_preset_runtime_artifacts_sha256 -ne $CurrentShadowPresetRuntimeHash) { throw "Shadow preset Bundle（Shadow 预设功能包）运行产物 hash 漂移。" }
  if ($State.shadow_preset_build_inputs_sha256 -ne $CurrentShadowPresetBuildInputsHash) { throw "Shadow preset Bundle（Shadow 预设功能包）源文件 hash 漂移。" }
  if ($State.wake_tasks_runtime_artifacts_sha256 -ne $CurrentWakeTasksRuntimeHash) { throw "唤醒任务插件运行产物 hash 漂移。" }
  if ($State.wake_tasks_build_inputs_sha256 -ne $CurrentWakeTasksBuildInputsHash) { throw "唤醒任务插件源文件 hash 漂移。" }
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
  New-Item -ItemType Directory -Force -Path $ProfileDir, $ShadowPresetArtifactDir, $WakeTasksArtifactDir | Out-Null
  $ProfilePatchPath = Join-Path $ProfileDir "cordis.patch.yml"
  Invoke-Checked -Command node -Arguments @(
    (Join-Path $PSScriptRoot "check-dsh-wake-migration.mjs"),
    (Join-Path $RepoRoot "config\wake-tasks.json")
  )
  Update-ManagedProfilePatch -ProfilePatchPath $ProfilePatchPath
  Write-Utf8NoBom -Path $ShadowPresetPackagePath -Text $ShadowPresetPackageText
  Write-Utf8NoBom -Path $ShadowPresetPatchPath -Text $ShadowPresetPatchText
  $PluginArgs = @($CliPath, "plugin", "--profile", "web", "add", $BridgeArtifactDir, "--save-exact")
  if ($Offline) { $PluginArgs += "--offline" }
  Invoke-Checked -Command node -Arguments $PluginArgs
  $PresetPluginArgs = @($CliPath, "plugin", "--profile", "web", "add", $ShadowPresetArtifactDir, "--save-exact")
  if ($Offline) { $PresetPluginArgs += "--offline" }
  Invoke-Checked -Command node -Arguments $PresetPluginArgs
  $WakeTasksPluginArgs = @($CliPath, "plugin", "--profile", "web", "add", $WakeTasksArtifactDir, "--save-exact")
  if ($Offline) { $WakeTasksPluginArgs += "--offline" }
  Invoke-Checked -Command node -Arguments $WakeTasksPluginArgs
  $DumpConfig = (& node $CliPath --profile web --dump-config 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) { throw "DSH dump-config 失败。" }
  if ($DumpConfig -notmatch [regex]::Escape('@voice-terminal/dsh-shadow-bridge')) { throw "DSH dump-config 未包含 Bridge Bundle。" }
  if ($DumpConfig -notmatch [regex]::Escape($ShadowPresetPackageName) -or $DumpConfig -notmatch 'preset-shadow-agent') { throw "DSH dump-config 未包含 Shadow preset Bundle（Shadow 预设功能包）声明。" }
  if ($DumpConfig -notmatch [regex]::Escape($WakeTasksPackageName)) { throw "DSH dump-config 未包含唤醒任务插件声明。" }
  $DumpConfig | Set-Content -LiteralPath $DumpConfigPath -Encoding utf8
  $ManagedHash = Get-TreeHash @((Split-Path -Parent $PatchSource), $PresetSource)
  foreach ($RuntimeArtifactPath in $BridgeRuntimeArtifactPaths) {
    if (-not (Test-Path -LiteralPath $RuntimeArtifactPath -PathType Leaf)) { throw "DSH Bridge 运行产物缺失：$RuntimeArtifactPath" }
  }
  $RuntimeArtifactHash = Get-TreeHash @($BridgeRuntimeArtifactPaths)
  $BridgeBuildInputsHash = Get-TreeHash @($BridgeBuildInputPaths)
  $ShadowPresetRuntimeArtifactHash = Get-TreeHash @($ShadowPresetRuntimeArtifactPaths)
  $ShadowPresetBuildInputsHash = Get-TreeHash @($ShadowPresetBuildInputPaths)
  $WakeTasksRuntimeArtifactHash = Get-TreeHash @($WakeTasksRuntimeArtifactPaths)
  $WakeTasksBuildInputsHash = Get-TreeHash @($WakeTasksBuildInputPaths)
  [ordered]@{
    schema_version = 5
    repository = $Lock.repository
    tag = $Tag
    commit = $Head
    version = $Package.version
    node_version = $NodeVersion
    pnpm_version = $ActualPnpm
    managed_config_sha256 = $ManagedHash
    bridge_bundle_version = $BridgeVersion
    bridge_protocol_version = $BridgeProtocolVersion
    bridge_runtime_artifacts_sha256 = $RuntimeArtifactHash
    bridge_build_inputs_sha256 = $BridgeBuildInputsHash
    shadow_preset_runtime_artifacts_sha256 = $ShadowPresetRuntimeArtifactHash
    shadow_preset_build_inputs_sha256 = $ShadowPresetBuildInputsHash
    wake_tasks_runtime_artifacts_sha256 = $WakeTasksRuntimeArtifactHash
    wake_tasks_build_inputs_sha256 = $WakeTasksBuildInputsHash
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
