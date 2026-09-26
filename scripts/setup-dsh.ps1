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
$BridgeDir = Join-Path $RepoRoot "integrations\dsh\voice-terminal-shadow-bridge"
$BridgeArtifactDir = Join-Path $RuntimeRoot "bundles\voice-terminal-shadow-bridge"
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

function Get-TreeHash {
  param([string[]]$Paths)
  $rows = foreach ($Path in $Paths) {
    $Files = if (Test-Path -LiteralPath $Path -PathType Leaf) {
      @(Get-Item -LiteralPath $Path)
    } else {
      @(Get-ChildItem -LiteralPath $Path -File -Recurse)
    }
    $Files | Sort-Object FullName | ForEach-Object {
      "$(($_.FullName.Substring($RepoRoot.Length)).Replace('\','/')):$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)"
    }
  }
  $bytes = [Text.Encoding]::UTF8.GetBytes(($rows -join "`n"))
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
  finally { $sha.Dispose() }
}

if (-not (Test-Path -LiteralPath $LockPath)) { throw "DSH 锁文件缺失：$LockPath" }
foreach ($Required in @($BridgePackagePath, (Join-Path $BridgeDir "src\index.ts"), $CommonPromptPath, $DshRulesPath)) {
  if (-not (Test-Path -LiteralPath $Required)) { throw "DSH Bridge 受管源文件缺失：$Required" }
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

$SourceExisted = Test-Path -LiteralPath $SourceDir -PathType Container
$PriorRuntimeCommit = $null
if (Test-Path -LiteralPath $BuildStatePath) {
  try { $PriorRuntimeCommit = (Get-Content -LiteralPath $BuildStatePath -Raw | ConvertFrom-Json).commit } catch { }
}

if (-not (Test-Path -LiteralPath $SourceDir)) {
  if ($Offline -or $SkipInstall -or $ValidateOnly) { throw "离线/校验模式要求 DSH 源码已存在：$SourceDir" }
  New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
  Invoke-Checked -Command git -Arguments @("clone", "--depth", "1", "--branch", $Lock.tag, "--single-branch", $Lock.repository, $SourceDir)
} else {
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
  if ($CurrentHead -ne $Lock.commit) {
    if ($ValidateOnly) { throw "DSH source 版本漂移：当前 $CurrentHead，期望 $($Lock.commit)；请先运行 setup-dsh.ps1。" }

    $LocalTagCommit = (& git -C $SourceDir rev-list -n 1 $Lock.tag 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $LocalTagCommit -ne $Lock.commit) {
      if ($Offline) { throw "离线模式下现有 DSH source 没有目标 tag（版本标签）$($Lock.tag)。" }
      Invoke-Checked -Command git -Arguments @("-C", $SourceDir, "fetch", "--depth", "1", "origin", "refs/tags/$($Lock.tag):refs/tags/$($Lock.tag)")
      $LocalTagCommit = (& git -C $SourceDir rev-list -n 1 $Lock.tag).Trim()
      if ($LASTEXITCODE -ne 0) { throw "无法解析已获取的 DSH tag（版本标签）$($Lock.tag)。" }
    }
    if ($LocalTagCommit -ne $Lock.commit) { throw "DSH tag（版本标签）commit（提交号）不匹配：期望 $($Lock.commit)，实际 $LocalTagCommit。" }

    Invoke-Checked -Command git -Arguments @("-C", $SourceDir, "checkout", "--detach", $Lock.commit)
  }
}

# 清除旧版本留下但被 Git 忽略的生成产物，避免干扰新版构建；干净暂存 checkout（检出）会保留旧源码以便回退。
$LegacySettingsFileOutput = Join-Path $SourceDir "packages\settings\settings-file\lib\types\index.js"
if (-not $ValidateOnly -and $SourceExisted -and $PriorRuntimeCommit -ne $Lock.commit -and ($CurrentHead -ne $Lock.commit -or (Test-Path -LiteralPath $LegacySettingsFileOutput))) {
  $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $StagedSource = Join-Path $RuntimeRoot ("source.staging-{0}-{1}" -f $Lock.version, $Stamp)
  $ArchiveCommit = [string]$CurrentHead
  $SourceArchive = Join-Path $RuntimeRoot ("source.backup-{0}-{1}" -f $ArchiveCommit.Substring(0, [Math]::Min(12, $ArchiveCommit.Length)), $Stamp)
  foreach ($Candidate in @($StagedSource, $SourceArchive)) {
    $CandidateFull = [System.IO.Path]::GetFullPath($Candidate)
    $RuntimeFull = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $CandidateFull.StartsWith($RuntimeFull, [StringComparison]::OrdinalIgnoreCase)) { throw "DSH source staging path escaped runtime directory: $CandidateFull" }
    if (Test-Path -LiteralPath $CandidateFull) { throw "DSH source staging destination already exists: $CandidateFull" }
  }
  New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
  Invoke-Checked -Command git -Arguments @("clone", "--no-local", "--no-checkout", $SourceDir, $StagedSource)
  Invoke-Checked -Command git -Arguments @("-C", $StagedSource, "remote", "set-url", "origin", $Lock.repository)
  Invoke-Checked -Command git -Arguments @("-C", $StagedSource, "checkout", "--detach", $Lock.commit)
  $StagedHead = (& git -C $StagedSource rev-parse HEAD).Trim()
  $StagedTag = (& git -C $StagedSource describe --tags --exact-match HEAD).Trim()
  $StagedDirty = @(& git -C $StagedSource status --porcelain)
  if ($LASTEXITCODE -ne 0 -or $StagedHead -ne $Lock.commit -or $StagedTag -ne $Lock.tag -or $StagedDirty.Count -gt 0) {
    throw "DSH source staging checkout failed verification; original source remains at $SourceDir."
  }

  Move-Item -LiteralPath $SourceDir -Destination $SourceArchive
  try {
    Move-Item -LiteralPath $StagedSource -Destination $SourceDir
  } catch {
    if (-not (Test-Path -LiteralPath $SourceDir) -and (Test-Path -LiteralPath $SourceArchive)) {
      Move-Item -LiteralPath $SourceArchive -Destination $SourceDir
    }
    throw
  }
  Write-Host "旧 DSH source 已完整留档：$SourceArchive" -ForegroundColor Yellow
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
  # 每次构建前清理生成的 project-reference（项目引用）产物；目标版的 `clean`（清理）脚本会误拒自身桌面键盘测试的 outDir（输出目录）。
  Invoke-Checked -Command pnpm -Arguments @("exec", "tsc", "-b", "--clean") -WorkingDirectory $SourceDir
  Invoke-Checked -Command pnpm -Arguments @("run", "build") -WorkingDirectory $SourceDir
}

if (-not (Test-Path -LiteralPath $CliPath)) { throw "DSH CLI 构建产物缺失：$CliPath" }
$ActualPnpm = (& pnpm --dir $SourceDir --version).Trim()
if ($LASTEXITCODE -ne 0 -or $ActualPnpm -ne $PnpmVersion) { throw "pnpm 版本不匹配：期望 $PnpmVersion，实际 $ActualPnpm。" }
$CliVersion = (& node $CliPath --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $CliVersion -notmatch [regex]::Escape($Lock.version)) { throw "DSH CLI 版本验证失败：$CliVersion" }

$BridgePackage = Get-Content -LiteralPath $BridgePackagePath -Raw | ConvertFrom-Json
$BridgeVersion = [string]$BridgePackage.version
$BridgeProtocolVersion = "1.0"
$PromptHash = Get-TreeHash @($CommonPromptPath, $DshRulesPath)
$PresetText = Get-Content -LiteralPath (Join-Path $PresetSource "agent.cordis.yml") -Raw
foreach ($RequiredPresetRow in @("tool-pwsh", "tool-fs", "tool-jobs", "tool-skill", "compaction-basic", "tool-subagent", "tool-web")) {
  if ($PresetText -notmatch [regex]::Escape("id: $RequiredPresetRow")) { throw "正式 Shadow preset 缺少默认工具/能力：$RequiredPresetRow" }
}
if ($PresetText -match "tools\.restrict|exact safe tool surface") { throw "正式 Shadow preset 不得缩减默认工具可见性。" }

if (-not $ValidateOnly) {
  Invoke-Checked -Command node -Arguments @((Join-Path $BridgeDir "build.mjs"), $BridgeArtifactDir)
  if (-not (Test-Path -LiteralPath $BridgeEntryPath)) { throw "DSH Bridge 核心构建产物缺失：$BridgeEntryPath" }
  if (-not (Test-Path -LiteralPath $BridgePluginEntryPath)) { throw "DSH Bridge 插件入口缺失：$BridgePluginEntryPath" }
  Invoke-Checked -Command node -Arguments @((Join-Path $PSScriptRoot "build-dsh-client.mjs"), $BridgeDir, $BridgeArtifactDir, $SourceDir)
  if (-not (Test-Path -LiteralPath $BridgeClientEntryPath)) { throw "DSH Bridge 浏览器插件产物缺失：$BridgeClientEntryPath" }
}

if ($ValidateOnly) {
  $ManagedCopies = @(
    @((Join-Path $PresetSource "agent.cordis.yml"), (Join-Path $HomeDir ".agent-presets\shadow-agent\agent.cordis.yml")),
    @((Join-Path $PresetSource "preset.yml"), (Join-Path $HomeDir ".agent-presets\shadow-agent\preset.yml"))
  )
  foreach ($Pair in $ManagedCopies) {
    if (-not (Test-Path -LiteralPath $Pair[1])) { throw "DSH_HOME 缺少受管配置：$($Pair[1])" }
    $ExpectedHash = (Get-FileHash -LiteralPath $Pair[0] -Algorithm SHA256).Hash
    $ActualHash = (Get-FileHash -LiteralPath $Pair[1] -Algorithm SHA256).Hash
    if ($ExpectedHash -ne $ActualHash) { throw "DSH_HOME 受管配置已漂移：$($Pair[1])" }
  }
  $ProfilePatchPath = Join-Path $HomeDir "profiles\web\cordis.patch.yml"
  if (-not (Test-Path -LiteralPath $ProfilePatchPath)) { throw "DSH_HOME 缺少 Web profile patch（Web 配置补丁）：$ProfilePatchPath" }
  $ProfilePatchText = Get-Content -LiteralPath $ProfilePatchPath -Raw
  if ($ProfilePatchText -notmatch '(?m)^\s*- id: agent-presets\s*$' -or $ProfilePatchText -notmatch '(?m)^\s+default: shadow-agent\s*$') {
    throw "DSH_HOME Web profile patch（Web 配置补丁）缺少正式 Shadow preset（Shadow 预设）默认项。"
  }
  if ($ProfilePatchText -notmatch '(?m)^\s*- id: session-log-deepseek\s*$' -or $ProfilePatchText -notmatch '(?m)^\s+disabled: true\s*$') {
    throw "DSH_HOME Web profile patch（Web 配置补丁）缺少预期的 session-log-deepseek 设置。"
  }
  if (-not (Test-Path -LiteralPath $BuildStatePath)) { throw "DSH build-state.json 缺失。" }
  if (-not (Test-Path -LiteralPath $BridgeEntryPath)) { throw "DSH Bridge 构建产物缺失。" }
  if (-not (Test-Path -LiteralPath $BridgePluginEntryPath)) { throw "DSH Bridge 插件入口缺失。" }
  if (-not (Test-Path -LiteralPath $BridgeClientEntryPath)) { throw "DSH Bridge 浏览器插件产物缺失。" }
  if (-not (Test-Path -LiteralPath $ProfilePackagePath)) { throw "DSH web profile manifest 缺失。" }
  $ProfilePackage = Get-Content -LiteralPath $ProfilePackagePath -Raw | ConvertFrom-Json
  if (-not $ProfilePackage.dependencies.'@voice-terminal/dsh-shadow-bridge') { throw "DSH Bridge 未安装到 web profile。" }
  if ($ProfilePackage.dsh.profile.bundles -notcontains '@voice-terminal/dsh-shadow-bridge') { throw "DSH Bridge bundle 未注册到 web profile。" }
  $ArtifactPackagePath = Join-Path $BridgeArtifactDir "package.json"
  if (-not (Test-Path -LiteralPath $ArtifactPackagePath)) { throw "DSH Bridge artifact package.json 缺失。" }
  foreach ($RuntimeArtifactPath in $BridgeRuntimeArtifactPaths) {
    if (-not (Test-Path -LiteralPath $RuntimeArtifactPath -PathType Leaf)) { throw "DSH Bridge 运行产物缺失：$RuntimeArtifactPath" }
  }
  $ArtifactPackage = Get-Content -LiteralPath $ArtifactPackagePath -Raw | ConvertFrom-Json
  if ($ArtifactPackage.main -ne 'lib/plugin.js' -or $ArtifactPackage.exports.'.' -ne './lib/plugin.js') { throw "DSH Bridge 插件包入口未指向带配置模式的 plugin.js。" }
  if ($ArtifactPackage.exports.'./client' -ne './lib/client.js') { throw "DSH Bridge 浏览器入口未导出。" }
  if ($ArtifactPackage.dsh.client.platform -ne 'web') { throw "DSH Bridge 未声明 web client bundle。" }
  $State = Get-Content -LiteralPath $BuildStatePath -Raw | ConvertFrom-Json
  if ($State.schema_version -ne 3) { throw "DSH build-state 格式版本已过期；请重新运行 setup-dsh.ps1。" }
  $CurrentRuntimeArtifactHash = Get-TreeHash @($BridgeRuntimeArtifactPaths)
  $CurrentBuildInputsHash = Get-TreeHash @($BridgeBuildInputPaths)
  if ($State.bridge_protocol_version -ne $BridgeProtocolVersion) { throw "DSH Bridge 协议版本漂移。" }
  if ($State.bridge_runtime_artifacts_sha256 -ne $CurrentRuntimeArtifactHash) { throw "DSH Bridge 运行产物 hash 漂移。" }
  if ($State.bridge_build_inputs_sha256 -ne $CurrentBuildInputsHash) { throw "DSH Bridge 构建输入 hash 漂移。" }
  if ($State.shadow_prompt_sha256 -ne $PromptHash) { throw "Shadow 公共提示词或 DSH 附加规则已漂移。" }
}

if (-not $ValidateOnly) {
  if ((Test-Path -LiteralPath $HomeDir -PathType Container) -and @(Get-ChildItem -Force -LiteralPath $HomeDir).Count -gt 0) {
    $PreviousState = $null
    if (Test-Path -LiteralPath $BuildStatePath) {
      try { $PreviousState = Get-Content -LiteralPath $BuildStatePath -Raw | ConvertFrom-Json } catch { }
    }
    if (-not $PreviousState -or $PreviousState.commit -ne $Lock.commit) {
      $BackupPath = Join-Path $RuntimeRoot ("home.backup-{0}-{1}" -f $Lock.version, (Get-Date -Format "yyyyMMdd-HHmmss"))
      New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
      & robocopy $HomeDir $BackupPath /E /COPY:DAT /DCOPY:DAT /SL /SJ /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
      if ($LASTEXITCODE -ge 8) { throw "DSH_HOME 备份失败（robocopy exit $LASTEXITCODE）：$BackupPath" }
      Write-Host "DSH_HOME 已备份到 $BackupPath" -ForegroundColor Yellow
    }
  }
  $PresetTarget = Join-Path $HomeDir ".agent-presets\shadow-agent"
  New-Item -ItemType Directory -Force -Path $ProfileDir, $PresetTarget | Out-Null
  $ProfilePatchPath = Join-Path $ProfileDir "cordis.patch.yml"
  if (-not (Test-Path -LiteralPath $ProfilePatchPath)) {
    Copy-Item -LiteralPath $PatchSource -Destination $ProfilePatchPath
  } else {
    Write-Host "保留现有 Web profile patch（Web 配置补丁）及其中的 DSH_HOME 设置：$ProfilePatchPath" -ForegroundColor Yellow
  }
  Copy-Item -LiteralPath (Join-Path $PresetSource "agent.cordis.yml") -Destination $PresetTarget -Force
  Copy-Item -LiteralPath (Join-Path $PresetSource "preset.yml") -Destination $PresetTarget -Force
  $PluginArgs = @($CliPath, "plugin", "--profile", "web", "add", $BridgeArtifactDir, "--save-exact")
  if ($Offline) { $PluginArgs += "--offline" }
  Invoke-Checked -Command node -Arguments $PluginArgs
  $DumpConfig = (& node $CliPath --profile web --dump-config 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) { throw "DSH dump-config 失败。" }
  if ($DumpConfig -notmatch [regex]::Escape('@voice-terminal/dsh-shadow-bridge')) { throw "DSH dump-config 未包含 Bridge Bundle。" }
  $DumpConfig | Set-Content -LiteralPath $DumpConfigPath -Encoding utf8
  $ManagedHash = Get-TreeHash @((Split-Path -Parent $PatchSource), $PresetSource)
  foreach ($RuntimeArtifactPath in $BridgeRuntimeArtifactPaths) {
    if (-not (Test-Path -LiteralPath $RuntimeArtifactPath -PathType Leaf)) { throw "DSH Bridge 运行产物缺失：$RuntimeArtifactPath" }
  }
  $RuntimeArtifactHash = Get-TreeHash @($BridgeRuntimeArtifactPaths)
  $BridgeBuildInputsHash = Get-TreeHash @($BridgeBuildInputPaths)
  [ordered]@{
    schema_version = 3
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
    shadow_prompt_sha256 = $PromptHash
    source_dir = $SourceDir
    home_dir = $HomeDir
    generated_at = [DateTimeOffset]::UtcNow.ToString("O")
  } | ConvertTo-Json | Set-Content -LiteralPath $BuildStatePath -Encoding utf8
}

Write-Host "DSH $($Lock.version) 校验完成；source=$SourceDir；home=$HomeDir" -ForegroundColor Green
