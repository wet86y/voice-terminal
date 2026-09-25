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
$BridgeClientEntryPath = Join-Path $BridgeArtifactDir "lib\client.js"
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
    Get-ChildItem -LiteralPath $Path -File -Recurse | Sort-Object FullName | ForEach-Object {
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

if (-not (Test-Path -LiteralPath $SourceDir)) {
  if ($Offline -or $SkipInstall -or $ValidateOnly) { throw "离线/校验模式要求 DSH 源码已存在：$SourceDir" }
  New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
  Invoke-Checked -Command git -Arguments @("clone", "--depth", "1", "--branch", $Lock.tag, "--single-branch", $Lock.repository, $SourceDir)
} else {
  $Dirty = (& git -C $SourceDir status --porcelain)
  if ($LASTEXITCODE -ne 0) { throw "现有 DSH source 不是可验证的 Git checkout：$SourceDir" }
  if ($Dirty) { throw "DSH source 含本地修改，拒绝覆盖。请备份后删除 .runtime\dsh\source，再重新运行 setup。" }
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
  # Keep a few large Windows optional binaries from sharing the entire link
  # and tripping pnpm's per-request timeout on slower developer networks.
  $InstallArgs = @("install", "--frozen-lockfile", "--network-concurrency=4", "--fetch-timeout=300000")
  if ($Offline) { $InstallArgs += "--offline" }
  # Invoke the Corepack shim directly. Running `corepack pnpm` leaves a global
  # pnpm earlier on PATH for DSH's nested build scripts on some Windows hosts.
  Invoke-Checked -Command pnpm -Arguments $InstallArgs -WorkingDirectory $SourceDir
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
  if (-not (Test-Path -LiteralPath $BridgeEntryPath)) { throw "DSH Bridge 构建产物缺失：$BridgeEntryPath" }
  Invoke-Checked -Command node -Arguments @((Join-Path $PSScriptRoot "build-dsh-client.mjs"), $BridgeDir, $BridgeArtifactDir, $SourceDir)
  if (-not (Test-Path -LiteralPath $BridgeClientEntryPath)) { throw "DSH Bridge 浏览器插件产物缺失：$BridgeClientEntryPath" }
}

if ($ValidateOnly) {
  $ManagedCopies = @(
    @($PatchSource, (Join-Path $HomeDir "profiles\web\cordis.patch.yml")),
    @((Join-Path $PresetSource "agent.cordis.yml"), (Join-Path $HomeDir ".agent-presets\shadow-agent\agent.cordis.yml")),
    @((Join-Path $PresetSource "preset.yml"), (Join-Path $HomeDir ".agent-presets\shadow-agent\preset.yml"))
  )
  foreach ($Pair in $ManagedCopies) {
    if (-not (Test-Path -LiteralPath $Pair[1])) { throw "DSH_HOME 缺少受管配置：$($Pair[1])" }
    $ExpectedHash = (Get-FileHash -LiteralPath $Pair[0] -Algorithm SHA256).Hash
    $ActualHash = (Get-FileHash -LiteralPath $Pair[1] -Algorithm SHA256).Hash
    if ($ExpectedHash -ne $ActualHash) { throw "DSH_HOME 受管配置已漂移：$($Pair[1])" }
  }
  if (-not (Test-Path -LiteralPath $BuildStatePath)) { throw "DSH build-state.json 缺失。" }
  if (-not (Test-Path -LiteralPath $BridgeEntryPath)) { throw "DSH Bridge 构建产物缺失。" }
  if (-not (Test-Path -LiteralPath $BridgeClientEntryPath)) { throw "DSH Bridge 浏览器插件产物缺失。" }
  if (-not (Test-Path -LiteralPath $ProfilePackagePath)) { throw "DSH web profile manifest 缺失。" }
  $ProfilePackage = Get-Content -LiteralPath $ProfilePackagePath -Raw | ConvertFrom-Json
  if (-not $ProfilePackage.dependencies.'@voice-terminal/dsh-shadow-bridge') { throw "DSH Bridge 未安装到 web profile。" }
  if ($ProfilePackage.dsh.profile.bundles -notcontains '@voice-terminal/dsh-shadow-bridge') { throw "DSH Bridge bundle 未注册到 web profile。" }
  $ArtifactPackagePath = Join-Path $BridgeArtifactDir "package.json"
  if (-not (Test-Path -LiteralPath $ArtifactPackagePath)) { throw "DSH Bridge artifact package.json 缺失。" }
  $ArtifactPackage = Get-Content -LiteralPath $ArtifactPackagePath -Raw | ConvertFrom-Json
  if ($ArtifactPackage.exports.'./client' -ne './lib/client.js') { throw "DSH Bridge 浏览器入口未导出。" }
  if ($ArtifactPackage.dsh.client.platform -ne 'web') { throw "DSH Bridge 未声明 web client bundle。" }
  $State = Get-Content -LiteralPath $BuildStatePath -Raw | ConvertFrom-Json
  $CurrentBundleHash = Get-TreeHash @($BridgeArtifactDir)
  $CurrentSourceHash = Get-TreeHash @($BridgeDir)
  if ($State.bridge_protocol_version -ne $BridgeProtocolVersion) { throw "DSH Bridge 协议版本漂移。" }
  if ($State.bridge_bundle_sha256 -ne $CurrentBundleHash) { throw "DSH Bridge Bundle 内容已漂移。" }
  if ($State.bridge_source_sha256 -ne $CurrentSourceHash) { throw "DSH Bridge 源码或构建规则已漂移。" }
  if ($State.shadow_prompt_sha256 -ne $PromptHash) { throw "Shadow 公共提示词或 DSH 附加规则已漂移。" }
}

if (-not $ValidateOnly) {
  $PresetTarget = Join-Path $HomeDir ".agent-presets\shadow-agent"
  New-Item -ItemType Directory -Force -Path $ProfileDir, $PresetTarget | Out-Null
  Copy-Item -LiteralPath $PatchSource -Destination (Join-Path $ProfileDir "cordis.patch.yml") -Force
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
  $BundleHash = Get-TreeHash @($BridgeArtifactDir)
  $BridgeSourceHash = Get-TreeHash @($BridgeDir)
  [ordered]@{
    schema_version = 2
    repository = $Lock.repository
    tag = $Tag
    commit = $Head
    version = $Package.version
    node_version = $NodeVersion
    pnpm_version = $ActualPnpm
    managed_config_sha256 = $ManagedHash
    bridge_bundle_version = $BridgeVersion
    bridge_protocol_version = $BridgeProtocolVersion
    bridge_bundle_sha256 = $BundleHash
    bridge_source_sha256 = $BridgeSourceHash
    shadow_prompt_sha256 = $PromptHash
    source_dir = $SourceDir
    home_dir = $HomeDir
    generated_at = [DateTimeOffset]::UtcNow.ToString("O")
  } | ConvertTo-Json | Set-Content -LiteralPath $BuildStatePath -Encoding utf8
}

Write-Host "DSH $($Lock.version) 校验完成；source=$SourceDir；home=$HomeDir" -ForegroundColor Green
