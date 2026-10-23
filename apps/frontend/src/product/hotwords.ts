/** HotwordSettings（热词配置）：用户维护目标词，特殊读音可补充别名。 */
export interface HotwordEntry {
  word: string // word（纠正后名称）。
  aliases: string[] // aliases（读音或常见误识别）。
  blacklist: string[] // blacklist（邻近排除词）。
}
/** HotwordJsonEntry（JSON 编辑输入）：别名和排除词省略时按空列表处理。 */
export interface HotwordJsonEntry {
  word: string // word（目标词，必填）。
  aliases?: string[] // aliases（别名，可选；省略时为空列表）。
  blacklist?: string[] // blacklist（邻近排除词，可选；省略时为空列表）。
}
export interface HotwordSettings {
  enabled: boolean // enabled（热词总开关）。
  stream_enabled: boolean // stream_enabled（主流式后置热词开关）。
  threshold: number // threshold（音素相似度门槛）。
  entries: HotwordEntry[] // entries（热词列表）。
}
export interface HotwordPreview {
  text: string // text（纠正文本）。
  raw_text: string // raw_text（试测原文）。
  matches: Array<{ original: string; replacement: string; score: number }> // matches（原词、替换词和相似度）。
  status: string // status（纠正、无变化、关闭、忙碌或超时）。
  elapsed_ms: number // elapsed_ms（处理耗时，毫秒）。
}

/** parseHotwordJsonEntries（解析 JSON 词表）：限定结构并补齐可选数组，语义校验仍由后端设置校验完成。 */
export function parseHotwordJsonEntries(text: string): HotwordEntry[] {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`JSON 格式错误：${detail}`)
  }
  if (!Array.isArray(value)) throw new Error('JSON 顶层必须是词条数组。')

  return value.map((raw, index) => {
    const number = index + 1
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
      throw new Error(`第 ${number} 条必须是词条对象。`)
    }
    const entry = raw as Record<string, unknown>
    const unknown = Object.keys(entry).filter(key => !['word', 'aliases', 'blacklist'].includes(key))
    if (unknown.length) throw new Error(`第 ${number} 条包含不支持的字段：${unknown.join('、')}。`)
    if (!Object.prototype.hasOwnProperty.call(entry, 'word')) throw new Error(`第 ${number} 条缺少 word（目标词）。`)
    if (typeof entry.word !== 'string') throw new Error(`第 ${number} 条的 word（目标词）必须是字符串。`)

    for (const [field, label] of [['aliases', 'aliases（别名）'], ['blacklist', 'blacklist（排除词）']] as const) {
      if (Object.prototype.hasOwnProperty.call(entry, field) &&
        (!Array.isArray(entry[field]) || entry[field].some(value => typeof value !== 'string'))) {
        throw new Error(`第 ${number} 条的 ${label} 必须是字符串数组。`)
      }
    }
    return {
      word: entry.word,
      aliases: Object.prototype.hasOwnProperty.call(entry, 'aliases') ? entry.aliases as string[] : [],
      blacklist: Object.prototype.hasOwnProperty.call(entry, 'blacklist') ? entry.blacklist as string[] : [],
    }
  })
}

export function exportHotwordText(entries: HotwordEntry[]) {
  const cell = (value: string) => `"${value.replaceAll('"', '""')}"`
  return '热词,别名,排除词\r\n' + entries.map(entry =>
    [entry.word, entry.aliases.join('；'), entry.blacklist.join('；')].map(cell).join(',')).join('\r\n')
}
