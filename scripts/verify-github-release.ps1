param([string]$Tag='') # Tag（待核验的公开发行标签）。
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'desktop-pipeline.ps1')
$Repo=Get-ProjectRoot
if (-not $Tag) {$Tag='v'+(Get-Content -LiteralPath (Join-Path $Repo 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version}
if ($Tag -notmatch '^v\d+\.\d+\.\d+$') {throw '发行标签格式无效。'}
$Repository='wet86y/voice-terminal'
$Output=Join-Path $Repo '.local/releases/installer'
$Directory=Join-Path $Repo ('.local/tmp/release-public-'+[Guid]::NewGuid().ToString('N'))
$Succeeded=$false
Enter-DesktopPipeline
try {
  [IO.Directory]::CreateDirectory($Directory) | Out-Null
  $Published=(& gh release view $Tag --repo $Repository --json tagName,body,isDraft,isPrerelease,assets) -join "`n"
  if ($LASTEXITCODE) {throw '无法读取公开发行记录。'}
  $Release=$Published | ConvertFrom-Json
  if ($Release.tagName -ne $Tag -or $Release.isDraft -or $Release.isPrerelease) {throw '公开发行的版本或正式状态不符。'}
  $Expected=Get-Content -LiteralPath (Join-Path $Repo "docs/releases/$Tag.md") -Raw -Encoding UTF8
  if (($Release.body -replace "`r`n","`n").TrimEnd() -cne ($Expected -replace "`r`n","`n").TrimEnd()) {throw '公开发行正文与对应版本说明不同。'}
  & gh release download $Tag --repo $Repository --dir $Directory
  if ($LASTEXITCODE) {throw '公开资产回下载失败。'}
  $Rows=foreach ($Name in @('VoiceTerminalSetup.exe','delivery.json','update.json','SHA256SUMS.txt')) {
    $Local=Join-Path $Output $Name;$Remote=Join-Path $Directory $Name
    $Hash=Get-Sha256File -LiteralPath $Remote
    if ((Get-Item -LiteralPath $Local).Length -ne (Get-Item -LiteralPath $Remote).Length -or $Hash -ne (Get-Sha256File -LiteralPath $Local)) {throw "公开资产大小或摘要不符：$Name（发行文件）。"}
    [ordered]@{name=$Name;size=(Get-Item -LiteralPath $Remote).Length;sha256=$Hash;fields=@{name='资产名称';size='字节数';sha256='完整内容摘要'}}
  }
  $Latest=(& gh api "repos/$Repository/releases/latest" --jq .tag_name) -join ''
  if ($LASTEXITCODE -or $Latest -ne $Tag) {throw '公开最新发行入口尚未指向目标版本。'}
  $Ref=((& gh api "repos/$Repository/git/ref/tags/$Tag") -join "`n") | ConvertFrom-Json
  if ($LASTEXITCODE) {throw '无法核验发行标签。'}
  $Object=$Ref.object
  for ($Level=0;$Object.type -eq 'tag' -and $Level -lt 5;$Level++) {
    $Object=(((& gh api "repos/$Repository/git/tags/$($Object.sha)") -join "`n") | ConvertFrom-Json).object
    if ($LASTEXITCODE) {throw '无法解析附注发行标签。'}
  }
  $Head=(& git -C $Repo rev-parse HEAD) -join ''
  if ($LASTEXITCODE -or $Object.type -ne 'commit' -or $Object.sha -ne $Head) {throw '发行标签未绑定本轮已核对的源码提交；不得自动移动标签。'}
  $Report=[ordered]@{schema=1;tag=$Tag;commit=$Head;verified_at=[DateTime]::UtcNow.ToString('o');assets=@($Rows);fields=@{schema='公开核验记录格式';tag='发行标签';commit='标签绑定的源码提交';verified_at='核验时间（协调世界时）';assets='逐项回下载结果'}}
  $Reports=Join-Path $Repo '.local/cache/reports';[IO.Directory]::CreateDirectory($Reports) | Out-Null
  [IO.File]::WriteAllText((Join-Path $Reports 'release-public-latest.json'),($Report | ConvertTo-Json -Depth 7),[Text.UTF8Encoding]::new($false))
  $Succeeded=$true
  Write-Host '公开发行正文、标签、最新入口及四项资产回下载摘要核验通过。' -ForegroundColor Green
} finally {
  try {
  if ($Succeeded) {
    # 仅回收本次新建的已知四个文件；有未知内容时保留目录，不递归删除。
    $Tmp=[IO.Path]::GetFullPath((Join-Path $Repo '.local/tmp'))+[IO.Path]::DirectorySeparatorChar
    $Resolved=[IO.Path]::GetFullPath($Directory)
    if (-not $Resolved.StartsWith($Tmp,[StringComparison]::OrdinalIgnoreCase) -or ((Get-Item -LiteralPath $Resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw '公开核验暂存目录身份异常，拒绝回收。'}
    foreach ($Name in @('VoiceTerminalSetup.exe','delivery.json','update.json','SHA256SUMS.txt')) {Remove-Item -LiteralPath (Join-Path $Resolved $Name) -Force}
    if (-not (Get-ChildItem -LiteralPath $Resolved -Force)) {Remove-Item -LiteralPath $Resolved -Force}
  } else {Write-Host "公开核验尚未完成，诊断文件保留在：$Directory"}
  } finally {Exit-DesktopPipeline}
}
