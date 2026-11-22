/** ReleaseBlock（发行说明段落）：只解析标题、列表及正文，内容始终按文本显示。 */
export interface ReleaseBlock {
  kind: 'heading' | 'list' | 'paragraph' // kind（段落类型）。
  lines: string[] // lines（段落文本）。
}

/** parseReleaseNotes（整理同一份发行说明）：兼容更新清单的纯文本和版本文档。 */
export function parseReleaseNotes(notes: string): ReleaseBlock[] {
  const blocks: ReleaseBlock[] = []
  for (const raw of notes.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || /^#\s/.test(line)) continue
    const heading = line.match(/^#{2,6}\s+(.+)$/)
    const title = heading?.[1] ?? (/^(新增与改进|修复|升级注意)[：:]?$/.test(line) ? line.replace(/[：:]$/, '') : '')
    const bullet = line.match(/^(?:[-*•]\s+)(.+)$/)
    const kind = title ? 'heading' : bullet ? 'list' : 'paragraph'
    const content = title || bullet?.[1] || line
    const previous = blocks.at(-1)
    if (previous?.kind === kind && kind !== 'heading') previous.lines.push(content)
    else blocks.push({ kind, lines: [content] })
  }
  return blocks
}

/** ReleaseEntry（版本记录）：直接使用仓库内与发行共用的版本文档。 */
export interface ReleaseEntry {
  version: string // version（产品版本）。
  notes: string // notes（完整更新内容）。
  summary: string // summary（折叠时的内容摘要）。
}

const documents = import.meta.glob<string>('../../../../docs/releases/v*.md', { query: '?raw', import: 'default', eager: true })

/** releaseHistory（版本倒序记录）：随程序提供往期日志，离线也能查看。 */
export function releaseHistory(targetVersion?: string | null, targetNotes?: string | null): ReleaseEntry[] {
  const notesByVersion = new Map<string, string>()
  for (const [path, notes] of Object.entries(documents)) {
    const version = path.match(/\/v(\d+\.\d+\.\d+)\.md$/)?.[1]
    if (version) notesByVersion.set(version, notes)
  }
  if (targetVersion && targetNotes?.trim()) notesByVersion.set(targetVersion.replace(/^v/, ''), targetNotes)
  return [...notesByVersion].sort(([a], [b]) => b.localeCompare(a, 'en', { numeric: true }))
    .map(([version, notes]) => ({ version, notes,
      summary: parseReleaseNotes(notes).find(block => block.kind !== 'heading')?.lines[0] ?? '查看此版本更新内容',
    }))
}
