/** HotwordSettings（热词配置）：用户维护目标词，特殊读音可补充别名。 */
export interface HotwordEntry {
  word: string // word（纠正后名称）。
  aliases: string[] // aliases（读音或常见误识别）。
  blacklist: string[] // blacklist（邻近排除词）。
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
export function exportHotwordText(entries: HotwordEntry[]) {
  const cell = (value: string) => `"${value.replaceAll('"', '""')}"`
  return '热词,别名,排除词\r\n' + entries.map(entry =>
    [entry.word, entry.aliases.join('；'), entry.blacklist.join('；')].map(cell).join(',')).join('\r\n')
}
