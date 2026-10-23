# Release target（发行目标）：远端修改前核验标签及正式发行状态，不自动移动已有标签。
function Assert-GitHubReleaseTarget {
  param([string]$Tag,[string]$Head)
  $Query='query($owner:String!,$name:String!,$tag:String!,$qualified:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$qualified){target{__typename ... on Commit{oid} ... on Tag{target{... on Commit{oid}}}}} release(tagName:$tag){isDraft isPrerelease}}}'
  $Raw=(& gh api graphql --raw-field ('query='+$Query) --raw-field owner=wet86y --raw-field name=voice-terminal --raw-field ('tag='+$Tag) --raw-field ('qualified=refs/tags/'+$Tag)) -join "`n"
  if ($LASTEXITCODE) {throw '无法读取发行目标；尚未修改远端资产。'}
  $Result=$Raw | ConvertFrom-Json
  if ($Result.errors -or -not $Result.data.repository) {throw '发行目标查询失败；尚未修改远端资产。'}
  $Target=$Result.data.repository.ref.target
  if ($Target) {
    $Commit=if ($Target.__typename -eq 'Commit') {$Target.oid} else {$Target.target.oid}
    if (-not $Commit -or $Commit -ne $Head) {throw '已有发行标签没有指向本轮核对的源码提交；请按同版本重新发行规范处理，未覆盖公开资产。'}
  }
  $Release=$Result.data.repository.release
  if ($Release -and ($Release.isDraft -or $Release.isPrerelease -or -not $Target)) {throw '已有发行不是与本轮标签匹配的公开正式发行；未覆盖公开资产。'}
  return [bool]$Release
}
