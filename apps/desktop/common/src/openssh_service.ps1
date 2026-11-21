# OpenSSH（微软安全外壳服务）只读定位：供状态检查与提权后的安装准入共用。
function Test-ProjectOpenSshPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return $false }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $allowed = @(Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe')
        foreach ($root in @($env:ProgramW6432, $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ } | Select-Object -Unique) {
            foreach ($folder in @('OpenSSH', 'OpenSSH-Win64', 'OpenSSH-Win32', 'OpenSSH-ARM64')) {
                $allowed += Join-Path $root "$folder\sshd.exe"
            }
        }
        if (-not @($allowed | Where-Object { [string]::Equals([IO.Path]::GetFullPath($_), $full, [StringComparison]::OrdinalIgnoreCase) }).Count) { return $false }
        # ReparsePoint（重解析点）：不允许官方目录通过联接转到可写的非官方位置。
        $cursor = [IO.FileInfo]$full
        while ($null -ne $cursor) {
            if ($cursor.Exists -and ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $false }
            $cursor = if ($cursor -is [IO.FileInfo]) { $cursor.Directory } else { $cursor.Parent }
        }
        return $true
    } catch { return $false }
}

function Get-ProjectOpenSshLayout {
    param($NativeService)
    $path = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
    $arguments = ''
    $commandValid = $true
    if ($null -ne $NativeService) {
        $command = [Environment]::ExpandEnvironmentVariables([string]$NativeService.PathName)
        $match = [regex]::Match($command, '^\s*(?:"(?<exe>[^"]+)"|(?<exe>[^"\r\n]+?\.exe))(?<args>(?:\s+[^\r\n]*)?)\s*$', 'IgnoreCase')
        if ($match.Success) {
            $path = $match.Groups['exe'].Value
            $arguments = $match.Groups['args'].Value.Trim()
        } else { $path = $null; $commandValid = $false }
    }
    $config = Join-Path $env:ProgramData 'ssh\sshd_config'
    $probeArguments = @('-T')
    $simpleConfig = $true
    $portOverride = $null
    # 仅解释 sshd（服务程序）的已知参数；未知参数不猜测，也不改写系统服务。
    $tokenPattern = '"[^"]*"|[^\s"]+'
    $tokens = @([regex]::Matches($arguments, $tokenPattern) | ForEach-Object { $_.Value.Trim('"') })
    if ([regex]::Replace($arguments, $tokenPattern, '').Trim()) { $commandValid = $false }
    for ($index = 0; $index -lt $tokens.Count; $index++) {
        $option = $tokens[$index]
        if ($option -cin @('-D', '-e', '-d', '-dd', '-ddd', '-4', '-6')) { continue }
        if ($option -cnotin @('-f', '-p', '-o', '-h', '-c', '-E') -or $index + 1 -ge $tokens.Count) { $commandValid = $false; break }
        $index++
        $value = $tokens[$index]
        if ($option -ceq '-f') {
            if (-not [IO.Path]::IsPathRooted($value)) { $commandValid = $false; break }
            $config = [IO.Path]::GetFullPath($value)
        }
        if ($option -ceq '-p') {
            $port = 0
            if (-not [int]::TryParse($value, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { $commandValid = $false; break }
            $portOverride = $port
        }
        if ($option -cin @('-o', '-h', '-c')) { $simpleConfig = $false }
        if ($option -cne '-E') { $probeArguments += @($option, $value) }
    }
    $sourcePath = $null; $version = $null; $signatureStatus = 'NotFound'; $subject = $null
    $officialPath = Test-ProjectOpenSshPath $path
    if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) {
        $binary = Get-Item -LiteralPath $path -ErrorAction Stop
        $sourcePath = [string]$binary.FullName
        $version = [string]$binary.VersionInfo.FileVersion
        if ($officialPath) {
            $signature = Get-AuthenticodeSignature -FilePath $path -ErrorAction SilentlyContinue
            if ($null -ne $signature) {
                $signatureStatus = [string]$signature.Status
                if ($null -ne $signature.SignerCertificate) { $subject = [string]$signature.SignerCertificate.Subject }
            }
        }
    }
    return @{
        SourcePath = $sourcePath # SourcePath（实际服务程序路径，缺少服务时为系统默认程序）。
        SourceVersion = $version # SourceVersion（程序文件版本）。
        SignatureStatus = $signatureStatus # SignatureStatus（数字签名验证结果）。
        SignatureSubject = $subject # SignatureSubject（签名主体）。
        OfficialBinary = [bool]($officialPath -and $version -and $signatureStatus -eq 'Valid' -and $subject -match '(?i)Microsoft (Corporation|Windows)')
        ServicePathOfficial = [bool]($null -ne $NativeService -and $officialPath) # ServicePathOfficial（服务登记路径属于官方位置）。
        ArgumentsValid = $commandValid # ArgumentsValid（服务命令参数可完整解释）。
        ConfigPath = $config # ConfigPath（实际配置文件路径）。
        ProbeArguments = $probeArguments # ProbeArguments（只读有效配置检查参数）。
        SimpleConfig = $simpleConfig # SimpleConfig（可由未变化的主配置安全推断运行端口）。
        PortOverride = $portOverride # PortOverride（服务命令行覆盖的端口）。
    }
}

function Invoke-ProjectOpenSshProbe {
    param($Layout)
    $preference = $ErrorActionPreference
    $code = -1; $lines = @()
    try {
        # Windows PowerShell 5.1（系统脚本环境）将原生命令错误流视为错误对象。
        $ErrorActionPreference = 'Continue'
        $probeArguments = $Layout.ProbeArguments
        $lines = @(& $Layout.SourcePath @probeArguments 2>$null)
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $preference }
    return @{ ExitCode=$code; Lines=$lines } # ExitCode（退出码）、Lines（有效配置行）。
}

function Resolve-ProjectOpenSshAuthorizedKeys {
    param($Layout, [bool]$IsAdmin, [string]$ProfilePath)
    # 只自动维护微软默认两种公钥文件；未知条件或自定义存储保留原配置，明确交给人工处理。
    if (-not $Layout.ArgumentsValid -or -not $Layout.SimpleConfig -or -not $ProfilePath) { return $null }
    try {
        $item = Get-Item -LiteralPath $Layout.ConfigPath -ErrorAction Stop
        if ($item.Length -gt 1048576 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $null }
        $contents = [string](Get-Content -LiteralPath $Layout.ConfigPath -Raw -ErrorAction Stop)
        $userPath = [IO.Path]::GetFullPath((Join-Path $ProfilePath '.ssh\authorized_keys'))
        $adminPath = [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'))
        $selected = $userPath
        $matchesAccount = $true
        $selectedInScope = $false
        $conditionalSeen = $false
        foreach ($row in ($contents -split "`r?`n")) {
            $line = ([string]$row -replace '#.*$', '').Trim()
            if (-not $line) { continue }
            if ($line -match '^Include\s+') { return $null }
            if ($line -match '^Match\s+') {
                if ($conditionalSeen) { return $null }
                $conditionalSeen = $true
                if ($line -match '^Match\s+Group\s+"?administrators"?\s*$') { $matchesAccount = $IsAdmin }
                elseif ($line -match '^Match\s+all\s*$') { $matchesAccount = $true }
                else { return $null }
                $selectedInScope = $false
                continue
            }
            if (-not $matchesAccount) { continue }
            if ($line -match '^PubkeyAuthentication\s+no\s*$' -or $line -match '^ForceCommand\s+(?!none\s*$)' -or $line -match '^AuthorizedKeysCommand\s+(?!none\s*$)') { return $null }
            if ($line -match '^AuthorizedKeysFile\s+(.+)$' -and -not $selectedInScope) {
                $value = $Matches[1].Trim().Trim('"').Replace('/', '\')
                if ($value -eq '.ssh\authorized_keys' -or $value -eq '%h\.ssh\authorized_keys' -or $value -eq $userPath) { $selected = $userPath }
                elseif ($IsAdmin -and ($value -eq '__PROGRAMDATA__\ssh\administrators_authorized_keys' -or $value -eq $adminPath)) { $selected = $adminPath }
                else { return $null }
                $selectedInScope = $true
            }
        }
        return $selected
    } catch { return $null }
}
