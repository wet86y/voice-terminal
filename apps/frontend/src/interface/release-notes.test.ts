import { describe, expect, it } from 'vitest'
import { parseReleaseNotes, releaseHistory } from './release-notes'

describe('发行说明共用排版', () => {
  it('版本文档与更新清单采用同一组标题和条目', () => {
    const body = '新增与改进\n• 增加日志入口。\n\n修复\n• 修复试听等待。\n升级注意\n• 无需额外操作。'
    const document = '# Voice Terminal（语音终端）v1.1.0\n\n' + body.replace(/^(新增与改进|修复|升级注意)$/gm, '## $1').replace(/• /g, '- ')
    expect(parseReleaseNotes(document)).toEqual(parseReleaseNotes(body))
  })
  it('保留旧版本的段落和标题，并将新发现的发行版本排到最前', () => {
    const history = releaseHistory('1.10.0', '新增与改进\n- <script>不会执行的发行文本</script>')
    expect(history[0]?.version).toBe('1.10.0')
    expect(history[0]?.summary).toContain('<script>')
    expect(history.some(row => row.version === '0.9.0')).toBe(true)
    expect(parseReleaseNotes('旧版简介\n\n## 主要内容\n- 内容')[0]).toEqual({ kind: 'paragraph', lines: ['旧版简介'] })
  })
})
