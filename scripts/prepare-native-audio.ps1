[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path.TrimEnd('\')
$repoPrefix = $repoRoot + '\'
$lockPath = Join-Path $repoRoot 'config/native-audio-sources.lock.json'
$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$utf8NoBom = [Text.UTF8Encoding]::new($false)

function Get-ManagedPath([string]$RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath)) {
        throw "原生音频准备路径必须是仓库相对路径：$RelativePath"
    }
    $fullPath = [IO.Path]::GetFullPath((Join-Path $script:repoRoot $RelativePath))
    if (-not $fullPath.StartsWith($script:repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "原生音频准备路径越出仓库：$RelativePath"
    }
    return $fullPath
}

function Assert-ManagedParents([string]$Path) {
    $current = Split-Path -Parent $Path
    while ($current -and $current.StartsWith($script:repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "原生音频准备目录不能穿过重解析点：$current"
            }
        }
        if ($current -eq $script:repoRoot) { break }
        $current = Split-Path -Parent $current
    }
}

function Get-Sha256([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Get-DirectoryStampSha256([string]$Directory) {
    $resolved = (Resolve-Path -LiteralPath $Directory).Path.TrimEnd('\')
    $prefix = $resolved + '\'
    $items = @(Get-ChildItem -LiteralPath $resolved -Force -Recurse)
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "固定音频源码目录含链接或重解析点：$($item.FullName)"
        }
    }
    $records = @(
        $items | Where-Object { -not $_.PSIsContainer } | Sort-Object FullName | ForEach-Object {
            [ordered]@{
                path = $_.FullName.Substring($prefix.Length).Replace([string][char]92, [string][char]47)
                size_bytes = $_.Length
                last_write_utc_ticks = $_.LastWriteTimeUtc.Ticks
            }
        }
    )
    $canonical = ConvertTo-Json -InputObject $records -Compress -Depth 4
    $digest = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($canonical)
        return [BitConverter]::ToString($digest.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally { $digest.Dispose() }
}

function Ensure-Archive($Entry, [string]$Label) {
    $archivePath = Get-ManagedPath $Entry.cache_path
    Assert-ManagedParents $archivePath
    $expectedSize = [int64]$Entry.size_bytes
    $expectedHash = $Entry.sha256.ToLowerInvariant()
    if (Test-Path -LiteralPath $archivePath) {
        $existing = Get-Item -LiteralPath $archivePath -Force
        $existingHash = Get-Sha256 $archivePath
        if (-not $existing.PSIsContainer -and $existing.Length -eq $expectedSize -and $existingHash -eq $expectedHash) {
            return $archivePath
        }
        throw "已存在的固定 $Label 源码归档与锁摘要不符；保留原件，请先人工核对：$archivePath"
    }

    $parent = Split-Path -Parent $archivePath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Assert-ManagedParents $archivePath
    $partial = "$archivePath.download-$PID"
    if (Test-Path -LiteralPath $partial) {
        throw "发现未登记的原生音频下载暂存；保留并停止：$partial"
    }
    Write-Host "下载固定 $Label 源码归档到 .local（本机资源目录）。"
    Invoke-WebRequest -Uri $Entry.source_url -OutFile $partial -UseBasicParsing
    $download = Get-Item -LiteralPath $partial -Force
    if ($download.Length -ne $expectedSize) {
        throw "下载的固定 $Label 源码归档字节数与锁定值不符；保留下载现场：$partial"
    }
    $downloadHash = Get-Sha256 $partial
    if ($downloadHash -ne $expectedHash) {
        throw "下载的固定 $Label 源码归档 SHA-256 摘要不符；保留下载现场：$partial"
    }
    Move-Item -LiteralPath $partial -Destination $archivePath
    return $archivePath
}

function Assert-SafeTar([string]$Archive, [string]$Label) {
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if (-not $tar) { throw '准备固定原生音频源码需要 Windows 自带 tar.exe；未安装或更改系统工具。' }
    $entries = @(& $tar.Source -tzf $Archive 2>&1)
    if ($LASTEXITCODE -ne 0 -or $entries.Count -eq 0) {
        throw "无法读取固定 $Label tar.gz 源码归档。"
    }
    foreach ($entry in $entries) {
        $name = $entry.ToString().Trim().Replace([string][char]92, [string][char]47)
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $pathParts = $name.Split('/')
        $drivePath = $name.Length -ge 2 -and $name[1] -eq ':'
        if ($name.StartsWith('/') -or $drivePath -or ($pathParts -contains '..')) {
            throw "固定 $Label 归档含越界路径，拒绝解压：$name"
        }
    }
    $verbose = @(& $tar.Source -tvzf $Archive 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "无法检查固定 $Label 归档条目类型。" }
    if ($verbose | Where-Object { $_.ToString() -match '^[lh]' }) {
        throw "固定 $Label 归档含链接条目，拒绝解压。"
    }
    return $tar.Source
}

function Ensure-ExtractedSource($Entry, [string]$Archive, [string]$Label, [string]$RequiredRelativePath) {
    $sourceRoot = Get-ManagedPath $Entry.source_root
    Assert-ManagedParents (Join-Path $sourceRoot $RequiredRelativePath)
    if (Test-Path -LiteralPath (Join-Path $sourceRoot $RequiredRelativePath) -PathType Leaf) {
        return $sourceRoot
    }
    if (Test-Path -LiteralPath $sourceRoot) {
        throw "固定 $Label 源码目录已存在但缺少预期文件；保留原目录，请先人工核对：$sourceRoot"
    }
    $sourceParent = Split-Path -Parent $sourceRoot
    if (Test-Path -LiteralPath $sourceParent) {
        $existing = @(Get-ChildItem -LiteralPath $sourceParent -Force)
        if ($existing.Count -gt 0) {
            throw "固定 $Label 解压父目录非空但源码根目录缺失；拒绝覆盖：$sourceParent"
        }
    } else {
        New-Item -ItemType Directory -Path $sourceParent -Force | Out-Null
    }
    Assert-ManagedParents (Join-Path $sourceParent 'source-check')
    $tar = Assert-SafeTar $Archive $Label
    & $tar -xzf $Archive -C $sourceParent
    if ($LASTEXITCODE -ne 0) { throw "解压固定 $Label 源码归档失败。" }
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $RequiredRelativePath) -PathType Leaf)) {
        throw "固定 $Label 源码归档解压后缺少预期文件：$RequiredRelativePath"
    }
    return $sourceRoot
}

if ($lock.schema_version -ne 1) { throw '不支持的原生音频源码锁格式。' }
$aec = $lock.baselines.aec_audio_processing
$meson = $lock.build_tools.meson
$buildConfiguration = $lock.build_configuration
if ($buildConfiguration.default_library -ne 'static' -or $buildConfiguration.wrap_mode -ne 'nodownload' -or
    $buildConfiguration.msvc_runtime -notin @('mt', 'mtd') -or $buildConfiguration.abseil_msvc_runtime -notin @('mt', 'mtd')) {
    throw '原生音频源码锁中的静态库、离线包装或 MSVC 运行库设置不受支持。'
}
$aecArchive = Ensure-Archive $aec 'AEC'
$mesonArchive = Ensure-Archive $meson 'Meson'
$aecRoot = Ensure-ExtractedSource $aec $aecArchive 'AEC' 'webrtc-audio-processing/meson.build'
$mesonRoot = Ensure-ExtractedSource $meson $mesonArchive 'Meson' 'meson.py'
$aecTreeStamp = Get-DirectoryStampSha256 (Join-Path $aecRoot 'webrtc-audio-processing')
$mesonTreeStamp = Get-DirectoryStampSha256 $mesonRoot

$python = $null
if ($env:VOICE_AUDIO_PYTHON) {
    $python = (Get-Command $env:VOICE_AUDIO_PYTHON -ErrorAction Stop).Source
} else {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonCommand) { $python = $pythonCommand.Source }
}
if (-not $python) {
    throw '构建固定 WebRTC AEC 需要现有 Python（构建解释器）3.7 或更高版本；请设置 VOICE_AUDIO_PYTHON 指向本机解释器。脚本不会安装解释器。'
}
$pythonVersion = (& $python --version 2>&1 | Out-String).Trim()
$versionParts = $pythonVersion.Split(' ')
$numericVersion = if ($versionParts.Count -gt 1) { $versionParts[1].Split('.') } else { @() }
$pythonMajor = if ($numericVersion.Count -gt 0) { [int]$numericVersion[0] } else { 0 }
$pythonMinor = if ($numericVersion.Count -gt 1) { [int]$numericVersion[1] } else { 0 }
if ($LASTEXITCODE -ne 0 -or $pythonMajor -lt 3 -or ($pythonMajor -eq 3 -and $pythonMinor -lt 7)) {
    throw "固定 Meson 1.4.2 需要可运行的 Python 3.7 或更高版本；当前为 $pythonVersion"
}
$mesonScript = Join-Path $mesonRoot 'meson.py'
$mesonVersion = (& $python $mesonScript --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $mesonVersion -ne $meson.version) {
    throw "Meson（C/C++ 构建系统）版本与源码锁不一致：$mesonVersion"
}

$project = Join-Path $aecRoot 'webrtc-audio-processing'
$build = Get-ManagedPath '.local/build/audio/aec-native-static-meson-build'
$install = Get-ManagedPath '.local/build/audio/aec-native-static-install'
$library = Join-Path $install 'lib/libwebrtc-audio-processing-2.a'
$builtLibrary = Join-Path $build 'webrtc/modules/audio_processing/libwebrtc-audio-processing-2.a'
$receipt = Join-Path $install 'native-audio-build-receipt.json'
Assert-ManagedParents (Join-Path $build 'meson-private/coredata.dat')
Assert-ManagedParents $library
if ((Test-Path -LiteralPath $build) -and -not (Test-Path -LiteralPath (Join-Path $build 'meson-private/coredata.dat'))) {
    $existingBuildFiles = @(Get-ChildItem -LiteralPath $build -Force)
    if ($existingBuildFiles.Count -gt 0) {
        throw "固定 AEC 构建目录非空但不是 Meson 构建；拒绝覆盖：$build"
    }
}

$mesonArgs = @($mesonScript)
if (Test-Path -LiteralPath (Join-Path $build 'meson-private/coredata.dat')) {
    $mesonArgs += @('configure', $build)
} else {
    $mesonArgs += @('setup', $build, $project)
}
$mesonArgs += @(
    "--prefix=$install",
    "--buildtype=$($buildConfiguration.buildtype)",
    "-Ddefault_library=$($buildConfiguration.default_library)",
    "-Dwrap_mode=$($buildConfiguration.wrap_mode)",
    "-Db_vscrt=$($buildConfiguration.msvc_runtime)",
    "-Dabseil-cpp:b_vscrt=$($buildConfiguration.abseil_msvc_runtime)"
)
Write-Host '配置固定 WebRTC Audio Processing（WebRTC 音频处理）静态构建。'
& $python @mesonArgs
if ($LASTEXITCODE -ne 0) { throw 'Meson 配置固定 AEC 静态构建失败。' }

$jobs = [Math]::Max(1, [Math]::Min(8, [Environment]::ProcessorCount))
& $python $mesonScript compile -C $build --jobs $jobs 'webrtc-audio-processing-2'
if ($LASTEXITCODE -ne 0) { throw '编译固定 AEC 静态库失败。' }
if (-not (Test-Path -LiteralPath $builtLibrary -PathType Leaf)) {
    throw "AEC 静态库构建完成但 Meson 输出缺失：$builtLibrary"
}
$builtHash = Get-Sha256 $builtLibrary
$previousReceipt = $null
if (Test-Path -LiteralPath $receipt -PathType Leaf) {
    try { $previousReceipt = Get-Content -LiteralPath $receipt -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $previousReceipt = $null }
}
$installedHeader = Join-Path $install 'include/webrtc-audio-processing-2/api/audio/audio_processing.h'
$needsInstall = $true
if ($previousReceipt -and (Test-Path -LiteralPath $library -PathType Leaf) -and (Test-Path -LiteralPath $installedHeader -PathType Leaf)) {
    $installedHash = Get-Sha256 $library
    $sameInputs = $previousReceipt.aec_archive_sha256 -eq $aec.sha256 -and
        $previousReceipt.meson_archive_sha256 -eq $meson.sha256 -and
        $previousReceipt.aec_source_tree_stamp_sha256 -eq $aecTreeStamp -and
        $previousReceipt.meson_source_tree_stamp_sha256 -eq $mesonTreeStamp -and
        $previousReceipt.buildtype -eq $buildConfiguration.buildtype -and
        $previousReceipt.default_library -eq $buildConfiguration.default_library -and
        $previousReceipt.wrap_mode -eq $buildConfiguration.wrap_mode -and
        $previousReceipt.msvc_crt -eq $buildConfiguration.msvc_runtime -and
        $previousReceipt.abseil_msvc_crt -eq $buildConfiguration.abseil_msvc_runtime -and
        $previousReceipt.python_version -eq $pythonVersion -and
        $previousReceipt.meson_version -eq $mesonVersion
    $needsInstall = -not ($sameInputs -and $installedHash -eq $builtHash)
}
if ($needsInstall) {
    & $python $mesonScript install -C $build
    if ($LASTEXITCODE -ne 0) { throw '安装固定 AEC 静态库到本机受管构建目录失败。' }
} else {
    Write-Host '固定 AEC 静态库和头文件与构建回执一致；复用本机受管安装。'
}
if (-not (Test-Path -LiteralPath $library -PathType Leaf) -or -not (Test-Path -LiteralPath $installedHeader -PathType Leaf)) {
    throw "AEC 静态库或公开头文件安装后缺失：$install"
}

$record = [ordered]@{
    schema_version = 1
    build = 'webrtc-audio-processing-static'
    aec_package_version = $aec.package_version
    aec_archive_sha256 = Get-Sha256 $aecArchive
    aec_source_tree_stamp_sha256 = $aecTreeStamp
    meson_version = $mesonVersion
    meson_archive_sha256 = Get-Sha256 $mesonArchive
    meson_source_tree_stamp_sha256 = $mesonTreeStamp
    buildtype = $buildConfiguration.buildtype
    default_library = $buildConfiguration.default_library
    wrap_mode = $buildConfiguration.wrap_mode
    msvc_crt = $buildConfiguration.msvc_runtime
    abseil_msvc_crt = $buildConfiguration.abseil_msvc_runtime
    python_version = $pythonVersion
    static_library = '.local/build/audio/aec-native-static-install/lib/libwebrtc-audio-processing-2.a'
    static_library_size_bytes = (Get-Item -LiteralPath $library).Length
    static_library_sha256 = Get-Sha256 $library
    fields = [ordered]@{
        schema_version = '本机原生音频构建回执格式版本'
        build = '固定 WebRTC AEC 静态构建类型'
        aec_package_version = '与现用组件匹配的上游 AEC 源码版本'
        aec_archive_sha256 = '上游 AEC 源码归档 SHA-256 摘要'
        aec_source_tree_stamp_sha256 = '解压后固定 AEC 源码树的文件名、大小及修改时间摘要'
        meson_version = '固定 Meson 构建系统版本'
        meson_archive_sha256 = 'Meson 源码归档 SHA-256 摘要'
        meson_source_tree_stamp_sha256 = '解压后固定 Meson 源码树的文件名、大小及修改时间摘要'
        buildtype = 'Meson 静态库构建类型'
        default_library = '是否构建静态库'
        wrap_mode = '是否允许构建时隐式下载依赖'
        msvc_crt = '与正式桌面宿主一致的静态 MSVC C/C++ 运行库模式'
        abseil_msvc_crt = 'Abseil C++ 子项目的静态 MSVC C/C++ 运行库模式'
        python_version = '本机用于执行 Meson 的构建解释器版本'
        static_library = '静态库在本机受管构建目录中的位置'
        static_library_size_bytes = '静态库字节数'
        static_library_sha256 = '静态库 SHA-256 摘要'
    }
}
New-Item -ItemType Directory -Path (Split-Path -Parent $receipt) -Force | Out-Null
[IO.File]::WriteAllText($receipt, ($record | ConvertTo-Json -Depth 6), $utf8NoBom)
Write-Host "固定 AEC 静态库已就绪；SHA-256：$($record.static_library_sha256)"
