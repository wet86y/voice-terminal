param([switch]$SkipBuild)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'local-context.ps1')
$Repo=Get-ProjectRoot
Push-Location $Repo
try {
  if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw '未找到 GitHub CLI（GitHub 命令行工具）gh。' }
  & gh auth status --hostname github.com *> $null
  if ($LASTEXITCODE) { throw 'GitHub CLI 尚未登录 github.com。' }

  $Branch=(& git branch --show-current).Trim()
  if ($LASTEXITCODE -or $Branch -ne 'main') { throw '发行版只能从已审阅的 main 分支发布。' }
  $Dirty=@(& git status --porcelain)
  if ($LASTEXITCODE -or $Dirty.Count) { throw '请先提交或清理源码修改，再运行发行脚本。' }
  $RemoteUrl=(& git remote get-url origin 2>$null)
  if ($LASTEXITCODE -or -not $RemoteUrl -or $RemoteUrl -notmatch 'github\.com[:/]wet86y/voice-terminal(?:\.git)?$') { throw 'origin（默认远程仓库）未指向 wet86y/voice-terminal。' }
  & git fetch origin main --quiet
  if ($LASTEXITCODE) { throw '无法读取 origin/main，请先推送源码并检查网络。' }
  $Head=(& git rev-parse HEAD).Trim();$RemoteHead=(& git rev-parse origin/main).Trim()
  if ($LASTEXITCODE -or $Head -ne $RemoteHead) { throw '本地 main 尚未与 GitHub origin/main 同步。' }

  $Repository=(& gh repo view wet86y/voice-terminal --json nameWithOwner,visibility)
  if ($LASTEXITCODE) { throw '找不到公开仓库 wet86y/voice-terminal。' }
  $RepositoryInfo=$Repository | ConvertFrom-Json
  if ($RepositoryInfo.nameWithOwner -ne 'wet86y/voice-terminal' -or $RepositoryInfo.visibility -ne 'PUBLIC') { throw 'GitHub 发行目标必须是公开仓库 wet86y/voice-terminal。' }

  if (-not $SkipBuild) {
    & npm run desktop:dist
    if ($LASTEXITCODE) { throw '本机 Windows 分发构建失败，未发布 GitHub Release（GitHub 发行版）。' }
  }

  $Manifest=Get-Content -LiteralPath (Join-Path $Repo 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $Version=[string]$Manifest.version
  if ($Version -notmatch '^\d+\.\d+\.\d+$' -or $Manifest.license -ne 'Apache-2.0') { throw '根项目版本格式无效，或许可证声明不是 Apache-2.0。' }
  $Tag="v$Version"
  $Output=Join-Path $Repo '.local/releases/installer'
  $Installer=Join-Path $Output 'VoiceTerminalSetup.exe'
  $DeliveryPath=Join-Path $Output 'delivery.json'
  $InstallerStampPath=Join-Path $Repo '.local/build/desktop/installer/build-stamp.json'
  $ProductStampPath=Join-Path $Repo '.local/build/desktop/product/build-stamp.json'
  $PackagePath=Join-Path $Repo '.local/build/program-package/program.zip'
  $PackageInfoPath=Join-Path $Repo '.local/build/program-package/package-info.json'
  foreach ($Path in @($Installer,$DeliveryPath,$InstallerStampPath,$ProductStampPath,$PackagePath,$PackageInfoPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "缺少发行构建文件：$Path" }
  }
  $Delivery=Get-Content -LiteralPath $DeliveryPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $InstallerStamp=Get-Content -LiteralPath $InstallerStampPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $ProductStamp=Get-Content -LiteralPath $ProductStampPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $PackageInfo=Get-Content -LiteralPath $PackageInfoPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $InstallerHash=Get-Sha256File -LiteralPath $Installer
  $PackageHash=Get-Sha256File -LiteralPath $PackagePath
  $InstallerSize=(Get-Item -LiteralPath $Installer).Length

  if ($ProductStamp.role -ne 'product' -or $ProductStamp.version -ne $Version -or $ProductStamp.optimization_level -ne 'release' -or $ProductStamp.qualification -ne $false) { throw '正式程序构建戳与发行版本不匹配。' }
  if ($InstallerStamp.role -ne 'installer' -or $InstallerStamp.version -ne $Version -or $InstallerStamp.optimization_level -ne 'release' -or $InstallerStamp.qualification -ne $false) { throw '分发安装器构建戳与发行版本不匹配。' }
  if ($PackageInfo.version -ne $Version -or $InstallerStamp.program_package_sha256 -ne $PackageInfo.sha256 -or $PackageHash -ne $PackageInfo.sha256) { throw '分发器内嵌程序包、构建戳与当前程序包摘要不一致。' }
  if ($Delivery.version -ne $Version -or $Delivery.build -ne $PackageInfo.build -or $Delivery.role -ne 'installer' -or $Delivery.entry -ne 'VoiceTerminalSetup.exe') { throw 'delivery.json（交付记录）与当前程序包构建不一致。' }
  if ($Delivery.size -ne $InstallerSize -or $Delivery.sha256 -ne $InstallerHash -or $Delivery.program_package_sha256 -ne $PackageInfo.sha256) { throw '安装器大小或摘要与交付记录不一致。' }

  $NotesPath=Join-Path $Repo "docs/releases/$Tag.md"
  if (-not (Test-Path -LiteralPath $NotesPath -PathType Leaf)) { throw "缺少发行说明：$NotesPath" }
  $Notes=Get-Content -LiteralPath $NotesPath -Raw -Encoding UTF8
  $Update=[ordered]@{
    version=$Version;tag=$Tag;asset='VoiceTerminalSetup.exe';size=$InstallerSize;sha256=$InstallerHash;releaseNotes=$Notes.Trim()
    fields=[ordered]@{version='产品版本';tag='GitHub 发行标签';asset='分发安装器名称';size='安装器字节数';sha256='安装器 SHA-256（文件摘要）';releaseNotes='中文发行说明'}
  }
  $UpdatePath=Join-Path $Output 'update.json'
  [IO.File]::WriteAllText($UpdatePath,($Update | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
  $DeliveryHash=Get-Sha256File -LiteralPath $DeliveryPath
  $UpdateHash=Get-Sha256File -LiteralPath $UpdatePath
  $ChecksumPath=Join-Path $Output 'SHA256SUMS.txt'
  $Checksums=@("$InstallerHash  VoiceTerminalSetup.exe","$DeliveryHash  delivery.json","$UpdateHash  update.json") -join "`n"
  [IO.File]::WriteAllText($ChecksumPath,$Checksums+"`n",[Text.UTF8Encoding]::new($false))

  $ReleaseView=(& gh release view $Tag --json tagName 2>$null)
  $ReleaseExists=($LASTEXITCODE -eq 0)
  $Assets=@($Installer,$DeliveryPath,$UpdatePath,$ChecksumPath)
  if ($ReleaseExists) {
    & gh release upload $Tag @Assets --clobber
  } else {
    & gh release create $Tag @Assets --title "Voice Terminal $Tag" --notes-file $NotesPath --target main
  }
  if ($LASTEXITCODE) { throw "GitHub Release $Tag 发布失败。" }
  $Published=(& gh release view $Tag --json tagName,assets)
  if ($LASTEXITCODE) { throw "无法读取已发布的 GitHub Release $Tag。" }
  $PublishedInfo=$Published | ConvertFrom-Json
  $Names=@($PublishedInfo.assets | ForEach-Object name)
  foreach ($Name in @('VoiceTerminalSetup.exe','delivery.json','update.json','SHA256SUMS.txt')) {
    if ($Name -notin $Names) { throw "发行版资产核验失败，缺少 $Name。" }
  }
  $RemoteInstaller=$PublishedInfo.assets | Where-Object name -eq 'VoiceTerminalSetup.exe' | Select-Object -First 1
  if ($RemoteInstaller.size -ne $InstallerSize) { throw 'GitHub 上的安装器资产大小与本机构建不一致。' }
  Write-Host "GitHub Release 已发布：https://github.com/wet86y/voice-terminal/releases/tag/$Tag" -ForegroundColor Green
} finally { Pop-Location }
