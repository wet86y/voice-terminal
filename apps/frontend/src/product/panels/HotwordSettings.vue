<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
import { useProductContext } from '../product-context'
import { exportHotwordText } from '../hotwords'

const { hotwordForm, hotwordLoading, hotwordSaving, hotwordTesting, hotwordError,
  hotwordTestText, hotwordPreview, hotwordLoaded, hotwordDirty, hotwordBusy,
  loadHotwords, saveHotwords, importHotwords, testHotwords, addHotword,
  setHotwordList, hotwordListValue, hotwordsChanged, hotwordImportPreview, applyHotwordImport } = useProductContext()
const fileInput = ref<HTMLInputElement | null>(null) // fileInput（导入文件选择器）。
const search = ref('') // search（目标词与别名筛选）。
const page = ref(0) // page（当前页，每页 50 条）。
const filtered = computed(() => hotwordForm.entries.map((entry, index) => ({ entry, index }))
  .filter(({ entry }) => !search.value.trim() || [entry.word, ...entry.aliases].some(value => value.toLowerCase().includes(search.value.trim().toLowerCase()))))
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / 50)))
const visible = computed(() => filtered.value.slice(page.value * 50, (page.value + 1) * 50))
watch(search, () => { page.value = 0 })
watch(pageCount, count => { page.value = Math.min(page.value, count - 1) })
onMounted(loadHotwords)
function inputValue(event: Event) { return (event.target as HTMLInputElement).value }
async function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    if (file.size > 1024 * 1024) throw new Error('请选择不超过 1MiB（二进制兆字节）的文本文件。')
    const text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
    await importHotwords(text)
  } catch (error) {
    hotwordError.value = error instanceof TypeError ? '文件编码无法读取，请另存为 UTF-8（通用文本编码）后导入。' : error instanceof Error ? error.message : String(error)
  } finally { input.value = '' }
}
function exportFile(template = false) {
  const entries = template ? [{ word: '示例热词', aliases: ['别名一', '别名二'], blacklist: ['排除词'] }] : hotwordForm.entries
  const url = URL.createObjectURL(new Blob(['\ufeff' + exportHotwordText(entries)], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url; link.download = template ? '热词导入模板.csv' : '热词词表.csv'; document.body.appendChild(link)
  link.click(); link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function removeValue(entry: typeof hotwordForm.entries[number], field: 'aliases' | 'blacklist', index: number) {
  setHotwordList(entry, field, entry[field].filter((_, position) => position !== index).join('；'))
}
function pasteValues(event: ClipboardEvent, entry: typeof hotwordForm.entries[number], field: 'aliases' | 'blacklist') {
  const pasted = event.clipboardData?.getData('text')
  if (!pasted || !/[\r\n]/.test(pasted)) return
  event.preventDefault()
  const input = event.target as HTMLInputElement
  setHotwordList(entry, field, input.value.slice(0, input.selectionStart ?? 0) + pasted.replace(/[\r\n]+/g, '；') + input.value.slice(input.selectionEnd ?? input.value.length))
}
function add() {
  search.value = ''; addHotword(); page.value = Math.floor((hotwordForm.entries.length - 1) / 50)
}
</script>

<template>
  <section class="hotword-settings-section" aria-label="热词替换设置">
  <SettingsPageHeader section="speaker" title="热词替换" description="专有名称、别名与识别文本纠正" compact>
    <template #actions>
      <button class="outline-button" :disabled="hotwordBusy || !hotwordLoaded" @click="fileInput?.click()">导入词表</button>
      <button class="outline-button" @click="exportFile(true)">下载模板</button>
      <button class="outline-button" :disabled="hotwordBusy || !hotwordForm.entries.length" @click="exportFile(false)">导出词表</button>
    </template>
  </SettingsPageHeader>
  <input ref="fileInput" class="hotword-file-input" type="file" accept=".csv,.txt,text/csv,text/plain" aria-label="导入热词条目文件" @change="chooseFile" />
  <section v-if="hotwordImportPreview" class="hotword-result" aria-label="热词导入预览">
    <strong>合并后共 {{ hotwordImportPreview.length }} 条，尚未修改当前草稿</strong>
    <p v-for="entry in hotwordImportPreview.slice(0, 10)" :key="entry.word">{{ entry.word }} · 别名：{{ entry.aliases.join('；') || '无' }} · 排除词：{{ entry.blacklist.join('；') || '无' }}</p>
    <small v-if="hotwordImportPreview.length > 10">仅预览前 10 条，确认后可搜索和分页检查全部条目。</small>
    <div><button class="outline-button" @click="applyHotwordImport">确认合并到草稿</button><button class="outline-button" @click="hotwordImportPreview = null">取消导入</button></div>
  </section>
  <p v-if="hotwordError" class="instruction-error" role="alert">{{ hotwordError }}</p>
  <div v-if="hotwordLoading" class="pool-empty" role="status">正在读取热词…</div>
  <button v-if="!hotwordLoaded && !hotwordLoading" class="outline-button" @click="loadHotwords">重新读取</button>
  <fieldset :disabled="hotwordBusy || !hotwordLoaded" class="hotword-fields">
    <label class="switch-row">
      <div><strong>启用热词</strong><span>总开关：控制主流式纠正和双路离线模型的原生热词增强。音色参考转写始终不使用热词。</span></div>
      <input v-model="hotwordForm.enabled" type="checkbox" role="switch" @change="hotwordsChanged" /><i></i>
    </label>
    <label class="switch-row">
      <div><strong>流式热词修订</strong><span>只控制主流式文本的后置纠正；关闭后，已开启双路的离线模型仍可使用目标热词。</span></div>
      <input v-model="hotwordForm.stream_enabled" :disabled="!hotwordForm.enabled" type="checkbox" role="switch" @change="hotwordsChanged" /><i></i>
    </label>
    <div class="hotword-toolbar">
      <label class="form-field hotword-search"><span>搜索热词</span><input v-model="search" placeholder="输入名称或别名" /></label>
      <button class="outline-button" :disabled="hotwordForm.entries.length >= 5000" @click="add">添加热词</button>
      <span class="hotword-count">共 {{ hotwordForm.entries.length }} / 5000 条</span>
    </div>
    <p class="hotword-help">多个别名或排除词可用英文分号、中文分号、顿号或换行分隔，斜杠与词内空格保留。别名和邻近排除词仅用于流式修订，离线模型只使用目标热词。</p>
    <div v-if="!filtered.length" class="pool-empty">{{ hotwordForm.entries.length ? '没有匹配的热词。' : '还没有热词。添加名称，或导入每行一个词的文本文件。' }}</div>
    <div v-for="{ entry, index } in visible" :key="index" class="hotword-row">
      <label class="form-field"><span>目标词 {{ index + 1 }}</span><input v-model="entry.word" maxlength="96" :aria-label="`目标词 ${index + 1}`" placeholder="希望输出的正确名称" @input="hotwordsChanged" /></label>
      <div v-for="field in (['aliases', 'blacklist'] as const)" :key="field" class="form-field">
        <span>{{ field === 'aliases' ? '别名（可选）' : '排除词（可选）' }}</span>
        <div class="hotword-tags"><button v-for="(value, position) in entry[field]" :key="value" type="button" :aria-label="`移除${field === 'aliases' ? '别名' : '排除词'} ${value}`" @click="removeValue(entry, field, position)">{{ value }} ×</button></div>
        <input :value="hotwordListValue(entry, field)" :aria-label="`${field === 'aliases' ? '读音或别名' : '邻近排除词'} ${index + 1}`" placeholder="多个值用分号分隔" @input="setHotwordList(entry, field, inputValue($event))" @paste="pasteValues($event, entry, field)" />
      </div>
      <button class="ghost-action hotword-remove" :aria-label="`删除热词 ${index + 1}`" @click="hotwordForm.entries.splice(index, 1); hotwordsChanged()">删除</button>
    </div>
    <nav v-if="pageCount > 1" class="hotword-pagination" aria-label="热词分页"><button class="outline-button" :disabled="page === 0" @click="page--">上一页</button><span>{{ page + 1 }} / {{ pageCount }}</span><button class="outline-button" :disabled="page + 1 >= pageCount" @click="page++">下一页</button></nav>
    <details class="hotword-advanced"><summary>高级设置</summary><label class="form-field"><span>相似度门槛</span><input v-model.number="hotwordForm.threshold" type="number" min="0.5" max="1" step="0.01" @input="hotwordsChanged" /></label><p class="hotword-help">默认 0.85。提高门槛可减少近音匹配；这不是纠正准确率。</p></details>
    <footer class="hotword-save"><span role="status">{{ hotwordDirty ? '有未保存的修改' : '保存后从下一轮发言生效，无需重启' }}</span><button class="save-button" @click="saveHotwords">{{ hotwordSaving ? '正在构建并保存…' : '保存热词设置' }}</button></footer>
    <section class="preview-section hotword-preview" aria-label="测试热词纠正">
      <h3>试一下流式热词纠正</h3><p class="hotword-help">仅试测流式后置纠正，不代表离线模型增强效果；不会保存或修改会话。</p>
      <label class="form-field"><span>识别文本</span><textarea v-model="hotwordTestText" maxlength="500" placeholder="例如：请让克劳得检查一下。" @input="hotwordsChanged" /></label>
      <button class="outline-button" :disabled="!hotwordTestText.trim()" @click="testHotwords">{{ hotwordTesting ? '正在试测…' : '测试纠正' }}</button>
      <div v-if="hotwordPreview" class="hotword-result" role="status">
        <strong>{{ ['timeout', 'busy', 'error'].includes(hotwordPreview.status) ? '本次未完成纠正，保留原文' : hotwordPreview.status === 'disabled' ? '热词替换已关闭' : hotwordPreview.matches.length ? '纠正结果' : '没有触发替换' }}</strong>
        <p>{{ hotwordPreview.text }}</p><small>处理耗时 {{ hotwordPreview.elapsed_ms }} 毫秒</small>
        <ul v-if="hotwordPreview.matches.length"><li v-for="(match, index) in hotwordPreview.matches" :key="index">{{ match.original }} → {{ match.replacement }}</li></ul>
      </div>
    </section>
  </fieldset>
  </section>
</template>

<style scoped>
.hotword-settings-section{display:flex;flex-direction:column;gap:20px;min-width:0}.hotword-file-input{display:none}.hotword-fields{border:0;padding:0;margin:0;min-width:0}.hotword-toolbar{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin-top:20px}.hotword-search{flex:1;min-width:180px}.hotword-count,.hotword-help,.hotword-save span{font-size:var(--font-caption);color:var(--muted)}.hotword-help{line-height:1.7}.hotword-row{display:grid;grid-template-columns:repeat(3,minmax(140px,1fr)) auto;gap:10px 16px;padding:16px 0;border-bottom:1px solid var(--line)}.hotword-remove{align-self:end;min-height:36px}.hotword-exclusions{grid-column:1 / -1}.hotword-exclusions summary,.hotword-advanced summary{cursor:pointer;color:var(--muted);font-size:var(--font-caption);padding:6px 0}.hotword-exclusions .form-field,.hotword-advanced .form-field{margin-top:10px}.hotword-advanced{margin-top:18px}.hotword-advanced input{max-width:140px}.hotword-save,.hotword-pagination{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:18px 0}.hotword-pagination{justify-content:center}.hotword-preview textarea{min-height:85px;resize:vertical}.hotword-result{margin-top:16px;padding:16px;border:1px solid var(--line);border-radius:12px;overflow-wrap:anywhere}.hotword-result p{white-space:pre-wrap}.hotword-result small{color:var(--muted)}@media(max-width:640px){.hotword-row{grid-template-columns:minmax(0,1fr) auto}.hotword-row .form-field{grid-column:1}.hotword-remove{grid-column:2;grid-row:1 / 4}.hotword-save .save-button{width:100%}}
.hotword-tags{display:flex;flex-wrap:wrap;gap:4px}.hotword-tags:empty{display:none}.hotword-tags button{font-size:var(--font-caption);padding:2px 7px;border:1px solid var(--line);border-radius:12px;background:transparent;color:var(--muted);cursor:pointer}.hotword-row>.form-field{min-width:0}
</style>
