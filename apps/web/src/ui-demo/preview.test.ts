import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { demoConversation, demoSettings, previewRows } from './demo-data'

// Preview boundary（展示边界）：从入口检查整个本地依赖图，防止以后误接入真实业务。
function localSources(entry: string, visited = new Map<string, string>()) {
  if (visited.has(entry)) return visited
  const source = readFileSync(entry, 'utf8')
  visited.set(entry, source)
  const imports = source.matchAll(/(?:from\s*|import\s*|@import\s*)['"](\.[^'"]+)['"]/g)
  for (const match of imports) {
    const path = resolve(dirname(entry), match[1])
    localSources(/\.(?:ts|vue|css)$/.test(path) ? path : `${path}.ts`, visited)
  }
  return visited
}

describe('视觉展示边界', () => {
  it('入口依赖保持在展示目录，不包含业务连接、设备权限或真实配置持久化', () => {
    const entry = fileURLToPath(new URL('./main.ts', import.meta.url))
    const sources = localSources(entry)
    expect(sources.size).toBeGreaterThan(3)
    for (const [path, source] of sources) {
      expect(path).not.toMatch(/[\\/](product|composables)[\\/]/)
      expect(source).not.toMatch(/\bfetch\s*\(|new\s+(?:WebSocket|EventSource)\b|\bgetUserMedia\s*\(|\b(?:localStorage|sessionStorage)\b/)
    }
  })

  it('场景生成不改变样例，长列表标识唯一，空场景没有会话条目', () => {
    const before = structuredClone(demoConversation)
    expect(previewRows(demoConversation, 'empty')).toEqual([])
    const longRows = previewRows(demoConversation, 'long')
    expect(longRows.length).toBeGreaterThan(demoConversation.length)
    expect(new Set(longRows.map(item => item.id)).size).toBe(longRows.length)
    longRows[0].text = '展示编辑'
    expect(demoConversation).toEqual(before)
  })

  it('展示表单初值不包含密钥，编辑副本不改变重置初值', () => {
    const draft = structuredClone(demoSettings)
    expect(draft.providerKey).toBe('')
    draft.keywords.push('临时短语')
    expect(demoSettings.keywords).toEqual(['嘿，小爪', '小爪同学'])
  })
})
