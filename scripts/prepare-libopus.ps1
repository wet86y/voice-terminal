[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path.TrimEnd('\')
$repoPrefix = $repoRoot + '\'
$lockPath = Join-Path $repoRoot 'config/native-audio-sources.lock.json'
$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$opus = $lock.baselines.libopus
$cmakeLock = $lock.build_tools.cmake
$utf8NoBom = [Text.UTF8Encoding]::new($false)

function Get-ManagedPath([string]$RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath)) { throw "libopus 路径必须是仓库相对路径：$RelativePath" }
    $fullPath = [IO.Path]::GetFullPath((Join-Path $script:repoRoot $RelativePath))
    if (-not $fullPath.StartsWith($script:repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "libopus 路径越出仓库：$RelativePath"
    }
    return $fullPath
}

function Assert-NoReparseParents([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current -and $current.StartsWith($script:repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "libopus 受管路径不能经过重解析点：$current"
            }
        }
        if ($current -eq $script:repoRoot) { break }
        $current = Split-Path -Parent $current
    }
}

function Get-Sha256([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}

function Get-TreeSha256([string]$Directory) {
    $resolved = (Resolve-Path -LiteralPath $Directory).Path.TrimEnd('\')
    $prefix = $resolved + '\'
    $items = @(Get-ChildItem -LiteralPath $resolved -Force -Recurse)
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "固定 libopus 源码树包含链接或重解析点：$($item.FullName)"
        }
    }
    $filesByPath = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::Ordinal)
    foreach ($item in $items) {
        if ($item.PSIsContainer) { continue }
        $relativePath = $item.FullName.Substring($prefix.Length).Replace([string][char]92, [string][char]47)
        if ($filesByPath.ContainsKey($relativePath)) { throw "固定 libopus 源码树包含重复相对路径：$relativePath" }
        $filesByPath.Add($relativePath, $item)
    }
    [string[]]$orderedPaths = @($filesByPath.Keys)
    [Array]::Sort($orderedPaths, [StringComparer]::Ordinal)
    $memory = [IO.MemoryStream]::new()
    $writer = [IO.BinaryWriter]::new($memory, $script:utf8NoBom, $true)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $magic = [Text.Encoding]::ASCII.GetBytes('VoiceTerminalSourceTreeHashV1')
        $writer.Write([int]$magic.Length)
        $writer.Write($magic)
        $writer.Write([int]$orderedPaths.Length)
        foreach ($relativePath in $orderedPaths) {
            $item = $filesByPath[$relativePath]
            $pathBytes = $script:utf8NoBom.GetBytes($relativePath)
            $digest = Get-Sha256 $item.FullName
            $digestBytes = New-Object byte[] 32
            for ($index = 0; $index -lt $digestBytes.Length; $index++) {
                $digestBytes[$index] = [Convert]::ToByte($digest.Substring($index * 2, 2), 16)
            }
            $writer.Write([int]$pathBytes.Length)
            $writer.Write($pathBytes)
            $writer.Write([long]$item.Length)
            $writer.Write($digestBytes)
        }
        $writer.Flush()
        return [BitConverter]::ToString($algorithm.ComputeHash($memory.ToArray())).Replace('-', '').ToLowerInvariant()
    }
    finally { $writer.Dispose(); $memory.Dispose(); $algorithm.Dispose() }
}

function Assert-SafeTar([string]$Archive) {
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if (-not $tar) { throw '准备固定 libopus 源码需要 Windows 自带 tar.exe；未安装或更改系统工具。' }
    $entries = @(& $tar.Source -tzf $Archive 2>&1)
    if ($LASTEXITCODE -ne 0 -or $entries.Count -eq 0) { throw '无法列举固定 libopus 源码归档。' }
    foreach ($entry in $entries) {
        $name = $entry.ToString().Trim().Replace([string][char]92, '/')
        $drivePath = $name.Length -ge 2 -and $name[1] -eq ':'
        if ($name.StartsWith('/') -or $drivePath -or ($name.Split('/') -contains '..')) {
            throw "固定 libopus 源码归档包含越界路径：$name"
        }
    }
    $verbose = @(& $tar.Source -tvzf $Archive 2>&1)
    if ($LASTEXITCODE -ne 0) { throw '无法检查固定 libopus 源码归档条目类型。' }
    if ($verbose | Where-Object { $_.ToString() -match '^[lh]' }) { throw '固定 libopus 源码归档包含链接条目，拒绝展开。' }
    return $tar.Source
}

if ($lock.schema_version -ne 1 -or $opus.version -ne '1.6.1' -or
    $opus.sha256 -ne '6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1' -or
    $opus.source_tree_hash_format -ne 'sha256_binary_records_v1' -or
    $cmakeLock.generator -ne 'Ninja' -or $cmakeLock.msvc_runtime -ne 'static') {
    throw 'libopus 固定版本、发布摘要或构建配置不受支持。'
}

$archive = Get-ManagedPath $opus.cache_path
$sourceRoot = Get-ManagedPath $opus.source_root
Assert-NoReparseParents $archive
Assert-NoReparseParents (Join-Path $sourceRoot 'CMakeLists.txt')
$archiveParent = Split-Path -Parent $archive
if (-not (Test-Path -LiteralPath $archiveParent)) { New-Item -ItemType Directory -Path $archiveParent -Force | Out-Null }
if (Test-Path -LiteralPath $archive -PathType Leaf) {
    $existing = Get-Item -LiteralPath $archive -Force
    if ($existing.Length -ne [int64]$opus.size_bytes -or (Get-Sha256 $archive) -ne $opus.sha256) {
        throw "现有 libopus 源码归档与锁定字节数或摘要不符；保留原件：$archive"
    }
} elseif (Test-Path -LiteralPath $archive) {
    throw "固定 libopus 归档路径不是普通文件；保留原件：$archive"
} else {
    $partial = "$archive.download"
    Assert-NoReparseParents $partial
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { throw '下载固定 libopus 源码需要现有 curl.exe；脚本不会安装网络工具。' }
    Write-Host '下载官方 libopus 1.6.1 源码；下载可续传，完成后核对发布摘要。'
    & $curl.Source --fail --location --silent --show-error --max-time 900 --connect-timeout 20 --continue-at - --output $partial $opus.source_url
    if ($LASTEXITCODE -ne 0) { throw "固定 libopus 源码下载失败；保留续传文件：$partial" }
    $download = Get-Item -LiteralPath $partial -Force
    if ($download.Length -ne [int64]$opus.size_bytes -or (Get-Sha256 $partial) -ne $opus.sha256) {
        throw "下载的 libopus 源码字节数或 SHA-256 摘要不符；保留现场：$partial"
    }
    Move-Item -LiteralPath $partial -Destination $archive
}

if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'CMakeLists.txt') -PathType Leaf)) {
    if (Test-Path -LiteralPath $sourceRoot) { throw "固定 libopus 源码目录已存在但缺少 CMakeLists.txt；保留原目录：$sourceRoot" }
    $sourceParent = Split-Path -Parent $sourceRoot
    Assert-NoReparseParents $sourceParent
    if (Test-Path -LiteralPath $sourceParent) {
        if (@(Get-ChildItem -LiteralPath $sourceParent -Force).Count -ne 0) {
            throw "libopus 解压父目录非空而源码根目录缺失；拒绝覆盖：$sourceParent"
        }
    } else { New-Item -ItemType Directory -Path $sourceParent -Force | Out-Null }
    $tar = Assert-SafeTar $archive
    & $tar -xzf $archive -C $sourceParent
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $sourceRoot 'CMakeLists.txt') -PathType Leaf)) {
        throw "固定 libopus 源码解包失败；保留现场：$sourceParent"
    }
}
$sourceTreeSha256 = Get-TreeSha256 $sourceRoot
Write-Host "固定 libopus 源码树摘要：$sourceTreeSha256"
if ($sourceTreeSha256 -ne $opus.source_tree_sha256) {
    throw "解压的 libopus 源码树与固定内容摘要不符；保留原目录：$sourceRoot"
}

$cmake = Get-Command cmake.exe -ErrorAction SilentlyContinue
$ninja = Get-Command ninja.exe -ErrorAction SilentlyContinue
if (-not $cmake -or -not $ninja) { throw '构建固定 libopus 需要本机现有 CMake（C/C++ 构建系统）与 Ninja（构建执行器）。' }
$cmakeVersionOutput = (& $cmake.Source --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $cmakeVersionOutput -notmatch '(?m)^cmake version ([0-9]+\.[0-9]+\.[0-9]+)') {
    throw '无法读取本机 CMake 版本。'
}
$cmakeVersion = $Matches[1]
if ([version]$cmakeVersion -lt [version]$cmakeLock.minimum_version) { throw "CMake 版本低于固定 libopus 的最低版本：$cmakeVersion" }

$modes = @(
    [ordered]@{ name = 'static'; shared = 'OFF'; build = '.local/build/audio/opus-static-cmake-build'; install = '.local/build/audio/opus-static-install'; library = 'lib/opus.lib'; runtime = $null },
    [ordered]@{ name = 'shared'; shared = 'ON'; build = '.local/build/audio/opus-shared-cmake-build'; install = '.local/build/audio/opus-shared-install'; library = 'lib/opus.lib'; runtime = 'bin/opus.dll' }
)
$jobs = [Math]::Max(1, [Math]::Min(8, [Environment]::ProcessorCount))
$artifacts = [ordered]@{}
foreach ($mode in $modes) {
    $build = Get-ManagedPath $mode.build
    $install = Get-ManagedPath $mode.install
    $library = Join-Path $install $mode.library
    $cache = Join-Path $build 'CMakeCache.txt'
    Assert-NoReparseParents $cache
    if ((Test-Path -LiteralPath $build) -and -not (Test-Path -LiteralPath $cache -PathType Leaf) -and
        @(Get-ChildItem -LiteralPath $build -Force).Count -ne 0) {
        throw "固定 libopus $($mode.name) 构建目录非空但缺少 CMakeCache.txt；保留原目录：$build"
    }
    if (Test-Path -LiteralPath $cache -PathType Leaf) {
        $cacheLines = @(Get-Content -LiteralPath $cache -Encoding UTF8)
        $sourceForCache = $sourceRoot.Replace('\', '/')
        $expectedSource = "CMAKE_HOME_DIRECTORY:INTERNAL=$sourceForCache"
        if ($cacheLines -notcontains $expectedSource -or
            $cacheLines -notcontains 'CMAKE_GENERATOR:INTERNAL=Ninja') {
            throw "固定 libopus $($mode.name) 构建缓存来源或生成器不符；保留原目录：$build"
        }
    }
    $arguments = @(
        '-S', $sourceRoot, '-B', $build, '-G', $cmakeLock.generator,
        "-DCMAKE_MAKE_PROGRAM=$($ninja.Source)", "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_INSTALL_PREFIX=$install", "-DBUILD_SHARED_LIBS=$($mode.shared)",
        "-DOPUS_BUILD_SHARED_LIBRARY=$($mode.shared)", '-DOPUS_STATIC_RUNTIME=ON',
        '-DOPUS_BUILD_TESTING=OFF', '-DOPUS_BUILD_PROGRAMS=OFF', '-DOPUS_CUSTOM_MODES=OFF',
        '-DOPUS_DRED=OFF', '-DOPUS_OSCE=OFF'
    )
    Write-Host "配置固定 libopus 1.6.1 $($mode.name) 构建。"
    & $cmake.Source @arguments
    if ($LASTEXITCODE -ne 0) { throw "配置固定 libopus $($mode.name) 构建失败；保留缓存：$build" }
    & $cmake.Source --build $build --config Release --parallel $jobs --target opus
    if ($LASTEXITCODE -ne 0) { throw "编译固定 libopus $($mode.name) 库失败；保留构建现场：$build" }
    & $cmake.Source --install $build --config Release
    if ($LASTEXITCODE -ne 0) { throw "安装固定 libopus $($mode.name) 库到 .local 失败：$install" }
    if (-not (Test-Path -LiteralPath $library -PathType Leaf)) { throw "固定 libopus $($mode.name) 库安装后缺失：$library" }
    $artifact = [ordered]@{ library = $mode.library; size_bytes = (Get-Item -LiteralPath $library).Length; sha256 = Get-Sha256 $library }
    if ($mode.runtime) {
        $runtime = Join-Path $install $mode.runtime
        if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) { throw "固定 libopus 运行时 DLL（动态链接库）缺失：$runtime" }
        $artifact.runtime = [ordered]@{ path = $mode.runtime; size_bytes = (Get-Item -LiteralPath $runtime).Length; sha256 = Get-Sha256 $runtime }
    }
    $artifacts[$mode.name] = $artifact
}

$receipt = Get-ManagedPath '.local/build/audio/opus-build-receipt.json'
$record = [ordered]@{
    schema_version = 1
    component = 'Xiph.Org libopus'
    version = $opus.version
    official_source_url = $opus.source_url
    archive_size_bytes = [int64]$opus.size_bytes
    archive_sha256 = Get-Sha256 $archive
    source_tree_sha256 = $sourceTreeSha256
    cmake_version = $cmakeVersion
    generator = $cmakeLock.generator
    msvc_runtime = $cmakeLock.msvc_runtime
    build_configuration = 'release; opus shared and static libraries; tests/programs/custom modes/DRED/OSCE disabled'
    artifacts = $artifacts
    fields = [ordered]@{
        schema_version = 'libopus 本机构建回执格式版本'
        component = '固定原生编解码组件名称'
        version = '官方 libopus 上游版本'
        official_source_url = '官方源码归档地址'
        archive_size_bytes = '官方源码归档字节数'
        archive_sha256 = '官方源码归档 SHA-256 摘要'
        source_tree_sha256 = '逐文件名称、大小及摘要生成的解压源码树摘要'
        cmake_version = '本机执行构建时的 CMake 版本'
        generator = 'CMake 构建生成器'
        msvc_runtime = '静态库和动态库使用的 MSVC C/C++ 运行库'
        build_configuration = '固定 libopus 构建选项'
        artifacts = '静态库、共享库及其内容摘要'
    }
}
Assert-NoReparseParents $receipt
New-Item -ItemType Directory -Path (Split-Path -Parent $receipt) -Force | Out-Null
[IO.File]::WriteAllText($receipt, ($record | ConvertTo-Json -Depth 7), $utf8NoBom)
Write-Host "官方 libopus 1.6.1 静态与共享库已就绪；源码摘要：$sourceTreeSha256"
