import { computed, reactive, ref } from 'vue'
import { productApi } from './api'
import type { HotwordEntry, HotwordPreview, HotwordSettings } from './hotwords'

/** useHotwordSettings（热词编辑状态）：页面切换保留未保存草稿。 */
export function useHotwordSettings(notify: (message: string) => void) {
  const hotwordForm = reactive<HotwordSettings>({ enabled: true, stream_enabled: true, threshold: 0.85, entries: [] })
  const hotwordImportPreview = ref<HotwordEntry[] | null>(null) // hotwordImportPreview（合并预览，确认前不改草稿）。
  const hotwordLoading = ref(false) // hotwordLoading（词表读取中）。
  const hotwordSaving = ref(false) // hotwordSaving（索引构建并保存中）。
  const hotwordTesting = ref(false) // hotwordTesting（试测执行中）。
  const hotwordImporting = ref(false) // hotwordImporting（文件解析中）。
  const hotwordError = ref('') // hotwordError（可读操作错误）。
  const hotwordTestText = ref('') // hotwordTestText（测试文本）。
  const hotwordPreview = ref<HotwordPreview | null>(null)
  const hotwordBaseline = ref('') // hotwordBaseline（最后保存的配置）。
  const hotwordLoaded = ref(false) // hotwordLoaded（首次读取完成）。
  const listDrafts = reactive(new WeakMap<HotwordEntry, { aliases: string; blacklist: string }>()) // listDrafts（保留未失焦的输入及尾部分隔符）。
  const hotwordDirty = computed(() => hotwordLoaded.value && JSON.stringify(hotwordForm) !== hotwordBaseline.value)
  const hotwordBusy = computed(() => hotwordLoading.value || hotwordSaving.value || hotwordTesting.value || hotwordImporting.value)
  let editRevision = 0 // editRevision（防止迟到响应覆盖本地编辑）。
  function message(error: unknown) { return error instanceof Error ? error.message : String(error) }
  function changed() { editRevision++; hotwordPreview.value = null; hotwordImportPreview.value = null }
  async function loadHotwords() {
    if (hotwordLoaded.value || hotwordLoading.value) return
    hotwordLoading.value = true
    hotwordError.value = ''
    const revision = editRevision
    try {
      const result = await productApi.hotwords()
      hotwordBaseline.value = JSON.stringify(result.settings)
      if (revision === editRevision) Object.assign(hotwordForm, result.settings)
      hotwordLoaded.value = true
    } catch (error) { hotwordError.value = message(error) }
    finally { hotwordLoading.value = false }
  }
  async function saveHotwords() {
    if (hotwordBusy.value || !hotwordLoaded.value) return
    hotwordSaving.value = true
    hotwordError.value = ''
    const revision = editRevision
    const submitted = structuredClone(JSON.parse(JSON.stringify(hotwordForm))) as HotwordSettings
    try {
      const result = await productApi.saveHotwords(submitted)
      hotwordBaseline.value = JSON.stringify(result.settings)
      if (revision === editRevision) Object.assign(hotwordForm, result.settings)
      notify(result.message)
    } catch (error) { hotwordError.value = message(error) }
    finally { hotwordSaving.value = false }
  }
  async function importHotwords(text: string) {
    if (hotwordBusy.value || !hotwordLoaded.value) return
    hotwordImporting.value = true
    hotwordImportPreview.value = null
    hotwordError.value = ''
    try {
      const result = await productApi.importHotwords(text)
      const merged = hotwordForm.entries.map(entry => ({ ...entry, aliases: [...entry.aliases], blacklist: [...entry.blacklist] }))
      for (const entry of result.entries) {
        const previous = merged.find(row => row.word.trim() === entry.word)
        if (previous) {
          previous.aliases = [...new Set([...previous.aliases, ...entry.aliases])]
          previous.blacklist = [...new Set([...previous.blacklist, ...entry.blacklist])]
        } else merged.push(entry)
      }
      if (merged.length > 5000) throw new Error('合并后的热词超过 5000 条，请先减少词条。')
      for (const [index, entry] of merged.entries()) {
        if (entry.aliases.length > 8 || entry.blacklist.length > 8) throw new Error(`合并后第 ${index + 1} 条的别名或排除词超过 8 个。`)
      }
      hotwordImportPreview.value = merged
      notify(`已解析 ${result.entries.length} 条热词，请检查合并预览。`)
    } catch (error) { hotwordError.value = message(error) }
    finally { hotwordImporting.value = false }
  }
  function applyHotwordImport() {
    if (!hotwordImportPreview.value) return
    hotwordForm.entries = hotwordImportPreview.value
    hotwordImportPreview.value = null
    changed()
    notify('已合并到草稿，保存后从下一轮发言生效。')
  }
  async function testHotwords() {
    if (hotwordBusy.value || !hotwordLoaded.value) return
    hotwordTesting.value = true
    hotwordError.value = ''
    hotwordPreview.value = null
    const revision = editRevision
    try {
      const result = await productApi.previewHotwords(hotwordForm, hotwordTestText.value)
      if (revision === editRevision) hotwordPreview.value = result
    } catch (error) { hotwordError.value = message(error) }
    finally { hotwordTesting.value = false }
  }
  function addHotword() {
    hotwordForm.entries.push({ word: '', aliases: [], blacklist: [] })
    changed()
  }
  function setHotwordList(entry: HotwordEntry, field: 'aliases' | 'blacklist', value: string) {
    if (!listDrafts.has(entry)) listDrafts.set(entry, { aliases: entry.aliases.join('；'), blacklist: entry.blacklist.join('；') })
    listDrafts.get(entry)![field] = value
    entry[field] = [...new Set(value.split(/[;；、\r\n]+/).map(part => part.trim()).filter(Boolean))]
    changed()
  }
  function hotwordListValue(entry: HotwordEntry, field: 'aliases' | 'blacklist') {
    return listDrafts.get(entry)?.[field] ?? entry[field].join('；')
  }
  return { hotwordForm, hotwordLoading, hotwordSaving, hotwordTesting, hotwordError, hotwordTestText,
    hotwordPreview, hotwordLoaded, hotwordDirty, hotwordBusy, loadHotwords, saveHotwords, importHotwords,
    hotwordImportPreview, applyHotwordImport,
    testHotwords, addHotword, setHotwordList, hotwordListValue, hotwordsChanged: changed }
}
