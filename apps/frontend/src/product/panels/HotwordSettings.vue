<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
import UiDialog from '../../interface/UiDialog.vue'
import { useProductContext } from '../product-context'

const { hotwordForm, hotwordLoading, hotwordSaving, hotwordError,
  hotwordLoaded, hotwordDirty, hotwordBusy,
  loadHotwords, saveHotwords, addHotword,
  setHotwordList, hotwordListValue, hotwordsChanged,
  hotwordJsonLoading, hotwordJsonSaving, hotwordJsonReady, hotwordJsonBusy,
  hotwordJsonText, hotwordJsonDraftConflict, hotwordJsonError, loadHotwordJson, saveHotwordJson } = useProductContext()
const jsonDialogOpen = ref(false) // jsonDialogOpen（批量编辑弹窗状态）。
const search = ref('') // search（目标词与别名筛选）。
const page = ref(0) // page（当前页，从零开始计数）。
const pageSize = 20 // pageSize（每页热词数量）。
const listScroll = ref<HTMLElement | null>(null) // listScroll（列表滚动容器）。
const filtered = computed(() => hotwordForm.entries.map((entry, index) => ({ entry, index }))
  .filter(({ entry }) => !search.value.trim() || [entry.word, ...entry.aliases].some(value => value.toLowerCase().includes(search.value.trim().toLowerCase()))))
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const visible = computed(() => filtered.value.slice(page.value * pageSize, (page.value + 1) * pageSize))
type PaginationItem = { type: 'page'; value: number } | { type: 'ellipsis'; key: string } // PaginationItem（分页项；value 是页码，key 是省略区间标识）。
const paginationItems = computed<PaginationItem[]>(() => {
  const count = pageCount.value
  const current = page.value + 1
  if (count <= 7) return Array.from({ length: count }, (_, index) => ({ type: 'page', value: index + 1 }))

  // 首尾各保留一页，当前页附近展开；靠近两端时连续展示更多页码。
  let pages: number[]
  if (current <= 4) pages = [1, 2, 3, 4, 5, count]
  else if (current >= count - 3) pages = [1, count - 4, count - 3, count - 2, count - 1, count]
  else pages = [1, current - 1, current, current + 1, count]

  const items: PaginationItem[] = []
  let previous = 0
  for (const value of pages) {
    if (value - previous > 1) items.push({ type: 'ellipsis', key: `gap-${previous}-${value}` })
    items.push({ type: 'page', value })
    previous = value
  }
  return items
})
function resetListScroll() { void nextTick(() => { if (listScroll.value) listScroll.value.scrollTop = 0 }) }
watch(search, () => { page.value = 0; resetListScroll() })
watch(page, resetListScroll)
watch(pageCount, count => { page.value = Math.min(page.value, count - 1) })
onMounted(loadHotwords)
function inputValue(event: Event) { return (event.target as HTMLInputElement).value }
async function openJsonEditor() {
  jsonDialogOpen.value = true
  await loadHotwordJson()
}
function closeJsonEditor() {
  if (hotwordJsonSaving.value) return
  jsonDialogOpen.value = false
}
async function saveJsonEditor() {
  if (await saveHotwordJson()) {
    jsonDialogOpen.value = false
    search.value = ''
    page.value = 0
    resetListScroll()
  }
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
  search.value = ''; addHotword(); page.value = Math.floor((hotwordForm.entries.length - 1) / pageSize)
}
</script>

<template>
  <section class="hotword-settings-section" aria-label="热词替换设置">
  <SettingsPageHeader section="speaker" title="热词替换" description="专有名称、别名与识别文本纠正" compact>
  </SettingsPageHeader>
  <p v-if="hotwordError" class="instruction-error" role="alert">{{ hotwordError }}</p>
  <div v-if="hotwordLoading" class="pool-empty" role="status">正在读取热词…</div>
  <button v-if="!hotwordLoaded && !hotwordLoading" class="outline-button" @click="loadHotwords">重新读取</button>
  <fieldset :disabled="hotwordBusy || !hotwordLoaded" class="hotword-fields">
    <div class="hotword-switches">
      <label class="switch-row">
        <div><strong>启用热词</strong><span>总开关：控制主流式纠正和双路离线模型的原生热词增强。音色参考转写始终不使用热词。</span></div>
        <input v-model="hotwordForm.enabled" type="checkbox" role="switch" @change="hotwordsChanged" /><i aria-hidden="true"></i>
      </label>
      <label class="switch-row hotword-stream-option">
        <div><strong>流式热词修订</strong><span>只控制主流式文本的后置纠正；关闭后，已开启双路的离线模型仍可使用目标热词。</span></div>
        <input v-model="hotwordForm.stream_enabled" :disabled="!hotwordForm.enabled" type="checkbox" role="switch" @change="hotwordsChanged" /><i aria-hidden="true"></i>
      </label>
    </div>
    <section class="hotword-list-window" aria-labelledby="hotword-list-title">
      <header class="hotword-list-heading">
        <h3 id="hotword-list-title">热词表</h3>
        <span class="hotword-count"><template v-if="search.trim()">匹配 {{ filtered.length }} 条 · </template>共 {{ hotwordForm.entries.length }} / 5000 条</span>
      </header>
      <div class="hotword-toolbar">
        <label class="form-field hotword-search"><span>搜索热词</span><input v-model="search" placeholder="输入名称或别名" /></label>
        <div class="hotword-actions">
          <button class="outline-button" :disabled="hotwordForm.entries.length >= 5000" @click="add">添加热词</button>
          <button class="outline-button" :disabled="hotwordBusy || !hotwordLoaded" @click="openJsonEditor">导入词表</button>
        </div>
      </div>
      <div ref="listScroll" class="hotword-list-scroll" tabindex="0" aria-label="当前页热词列表，滚动查看更多词条">
        <div v-if="!filtered.length" class="hotword-empty" role="status">
          <strong>{{ hotwordForm.entries.length ? '没有匹配的热词' : '还没有热词' }}</strong>
          <p>{{ hotwordForm.entries.length ? '试试其他名称或别名。' : '点击“添加热词”逐条填写，或通过“导入词表”批量添加。' }}</p>
        </div>
        <div v-for="{ entry, index } in visible" :key="index" class="hotword-row">
          <label class="form-field"><span>目标词 {{ index + 1 }}</span><input v-model="entry.word" maxlength="96" :aria-label="`目标词 ${index + 1}`" placeholder="希望输出的正确名称" @input="hotwordsChanged" /></label>
          <div v-for="field in (['aliases', 'blacklist'] as const)" :key="field" class="form-field">
            <span>{{ field === 'aliases' ? '别名（可选）' : '排除词（可选）' }}</span>
            <div class="hotword-tags"><button v-for="(value, position) in entry[field]" :key="value" type="button" :aria-label="`移除${field === 'aliases' ? '别名' : '排除词'} ${value}`" @click="removeValue(entry, field, position)">{{ value }} ×</button></div>
            <input :value="hotwordListValue(entry, field)" :aria-label="`${field === 'aliases' ? '读音或别名' : '邻近排除词'} ${index + 1}`" placeholder="多个值用分号分隔" @input="setHotwordList(entry, field, inputValue($event))" @paste="pasteValues($event, entry, field)" />
          </div>
          <button class="ghost-action hotword-remove" :aria-label="`删除热词 ${index + 1}`" @click="hotwordForm.entries.splice(index, 1); hotwordsChanged()">删除</button>
        </div>
      </div>
      <footer v-if="pageCount > 1" class="hotword-list-footer">
        <nav class="hotword-pagination" aria-label="热词分页">
          <button class="outline-button hotword-page-edge" :disabled="page === 0" @click="page = Math.max(0, page - 1)">上一页</button>
          <template v-for="item in paginationItems" :key="item.type === 'page' ? `page-${item.value}` : item.key">
            <button v-if="item.type === 'page'" class="hotword-page-number" :class="{ 'is-current': item.value === page + 1 }" :aria-current="item.value === page + 1 ? 'page' : undefined" @click="page = item.value - 1">{{ item.value }}</button>
            <span v-else class="hotword-page-ellipsis" aria-hidden="true">…</span>
          </template>
          <button class="outline-button hotword-page-edge" :disabled="page + 1 >= pageCount" @click="page = Math.min(pageCount - 1, page + 1)">下一页</button>
        </nav>
      </footer>
      <details class="hotword-list-guide">
        <summary>填写说明</summary>
        <p class="hotword-help">目标词填写希望输出的正确名称，别名和排除词为可选项。多个值可用英文分号、中文分号、顿号或换行分隔，斜杠与词内空格保留。</p>
        <p class="hotword-help">别名和邻近排除词仅用于流式修订，离线模型只使用目标热词。</p>
      </details>
    </section>
    <details class="hotword-advanced"><summary>高级设置</summary><label class="form-field"><span>相似度门槛</span><input v-model.number="hotwordForm.threshold" type="number" min="0.5" max="1" step="0.01" @input="hotwordsChanged" /></label><p class="hotword-help">默认 0.85。提高门槛可减少近音匹配；这不是纠正准确率。</p></details>
    <footer class="hotword-save"><span role="status">{{ hotwordDirty ? '有未保存的修改' : '保存后从下一轮发言生效，无需重启' }}</span><button class="save-button" @click="saveHotwords">{{ hotwordSaving ? '正在构建并保存…' : '保存热词设置' }}</button></footer>
  </fieldset>
  <UiDialog :open="jsonDialogOpen" kicker="热词词表" title="批量编辑热词 JSON" :busy="hotwordJsonSaving" wide @close="closeJsonEditor">
    <div class="hotword-json-editor">
      <p class="hotword-json-intro">这里显示当前已保存的完整词表。保存会立即写入设置，并从下一轮发言生效。</p>
      <p v-if="hotwordJsonDraftConflict" class="hotword-json-warning" role="status">当前页面有未保存的词条修改；保存此 JSON 会按已保存词表替换这些词条草稿。</p>
      <p v-if="hotwordJsonError" class="instruction-error" role="alert">{{ hotwordJsonError }}</p>
      <div v-if="hotwordJsonLoading" class="pool-empty" role="status">正在读取已保存词表…</div>
      <label v-else-if="hotwordJsonReady" class="form-field hotword-json-field">
        <span>完整词表 JSON</span>
        <textarea v-model="hotwordJsonText" aria-label="完整词表 JSON" spellcheck="false" autocomplete="off" autocapitalize="off" :disabled="hotwordJsonSaving"></textarea>
      </label>
      <div class="hotword-json-guide">
        <div>
          <strong>字段说明</strong>
          <p><code>word</code>（目标词，必填）；<code>aliases</code>（别名，可省略）；<code>blacklist</code>（邻近排除词，可省略）。省略两个可选字段时会按空数组保存。</p>
        </div>
        <div>
          <strong>JSON 示例</strong>
          <pre><code>[{"word":"示例热词","aliases":["别名或读音"],"blacklist":[]}]</code></pre>
        </div>
      </div>
    </div>
    <template #actions>
      <button v-if="!hotwordJsonReady && !hotwordJsonLoading" class="outline-button" :disabled="hotwordJsonBusy" @click="loadHotwordJson">重新读取</button>
      <button class="outline-button" :disabled="hotwordJsonSaving" @click="closeJsonEditor">取消</button>
      <button class="save-button" :disabled="!hotwordJsonReady || hotwordJsonBusy" @click="saveJsonEditor">{{ hotwordJsonSaving ? '正在校验并保存…' : '保存词表' }}</button>
    </template>
  </UiDialog>
  </section>
</template>

<style scoped>
.hotword-settings-section{display:flex;flex-direction:column;gap:20px;min-width:0}
.hotword-fields{display:flex;flex-direction:column;gap:24px;border:0;padding:0;margin:0;min-width:0}
/* hotword-switches（热词开关组）：纵向留出间距，缩进流式子选项。 */
.hotword-switches{display:grid;gap:12px;min-width:0}
.hotword-switches .switch-row>div{min-width:0}
.hotword-switches .switch-row>i{flex-shrink:0}
.hotword-stream-option{margin-left:20px}
.hotword-list-heading{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 16px;min-width:0}
.hotword-list-heading h3{font-size:var(--font-section);font-weight:600}
.hotword-toolbar{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;min-width:0}
.hotword-search{flex:1;min-width:min(180px,100%)}
.hotword-actions{display:flex;align-items:center;gap:8px;flex:0 0 auto}
.hotword-count,.hotword-help,.hotword-save span{font-size:var(--font-caption);color:var(--muted)}
.hotword-help{line-height:1.7}
.hotword-list-window{display:grid;gap:16px;min-width:0;padding:20px;overflow:hidden;border:1px solid var(--line);border-radius:12px;background:#ffffff02;container:hotword-list / inline-size}
.hotword-list-scroll{max-height:min(70vh,690px);min-width:0;overflow-y:auto;overscroll-behavior-y:auto;border-top:1px solid var(--line)}
.hotword-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;min-height:140px;padding:24px 16px;text-align:center}
.hotword-empty strong{font-size:var(--font-body);font-weight:500;color:var(--text)}
.hotword-empty p{max-width:360px;color:var(--muted);font-size:var(--font-caption);line-height:1.7}
.hotword-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr)) auto;align-items:end;gap:8px 12px;padding:14px 0;border-bottom:1px solid var(--line)}
.hotword-row:last-child{border-bottom:0}
.hotword-row .form-field{min-width:0}
.hotword-row .form-field>span{margin-bottom:5px}
.hotword-row .form-field input{height:34px;min-height:34px;padding:6px 9px;font-size:var(--font-body)}
.hotword-remove{align-self:end;min-height:32px;padding:5px 8px}
.hotword-list-footer{display:flex;align-items:center;justify-content:center;min-width:0;padding-top:12px;border-top:1px solid var(--line)}
.hotword-pagination{display:flex;align-items:center;gap:4px;min-width:0;max-width:100%;overflow-x:auto;white-space:nowrap}
.hotword-pagination .hotword-page-edge{min-height:30px;padding:4px 7px;font-size:var(--font-caption);white-space:nowrap}
.hotword-page-number{display:inline-flex;align-items:center;justify-content:center;flex:0 0 30px;min-width:30px;height:30px;padding:0 4px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--muted);font:inherit;font-size:var(--font-caption);cursor:pointer}
.hotword-page-number:hover{border-color:var(--line-strong);color:var(--text)}
.hotword-page-number.is-current{border-color:var(--accent);background:#ffffff0a;color:var(--accent);font-weight:600}
.hotword-page-ellipsis{display:inline-flex;align-items:center;justify-content:center;flex:0 0 14px;color:var(--muted);font-size:var(--font-caption)}
.hotword-count{white-space:nowrap}
.hotword-list-guide summary{cursor:pointer;color:var(--muted);font-size:var(--font-caption)}
.hotword-list-guide .hotword-help{margin-top:8px}
.hotword-advanced input{max-width:140px}
.hotword-save{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:16px 0 0;border-top:1px solid var(--line)}
.hotword-tags{display:flex;flex-wrap:wrap;gap:3px;max-height:34px;overflow-y:auto;align-content:start}
.hotword-tags:empty{display:none}
.hotword-tags button{font-size:var(--font-caption);padding:1px 7px;border:1px solid var(--line);border-radius:12px;background:transparent;color:var(--muted);cursor:pointer;line-height:1.45}
.hotword-json-editor{display:grid;gap:14px;min-width:0}
.hotword-json-intro{font-size:var(--font-caption);line-height:1.7;color:var(--muted)}
.hotword-json-warning{padding:10px 12px;border:1px solid #efa0b140;border-radius:9px;background:#efa0b108;color:var(--danger);font-size:var(--font-caption);line-height:1.65}
.hotword-json-field textarea{display:block;width:100%;height:min(54vh,520px);min-height:260px;resize:vertical;padding:12px 14px;border:1px solid var(--line-strong);border-radius:10px;background:#10141d;color:var(--text);font:12px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;tab-size:2;white-space:pre;overflow:auto}
.hotword-json-field textarea:focus{border-color:var(--accent)}
.hotword-json-guide{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;padding:12px;border:1px solid var(--line);border-radius:10px;background:#ffffff02}
.hotword-json-guide>div{min-width:0}
.hotword-json-guide strong{display:block;margin-bottom:6px;font-size:var(--font-caption);color:var(--text)}
.hotword-json-guide p,.hotword-json-guide pre{margin:0;color:var(--muted);font-size:var(--font-caption);line-height:1.65;overflow-wrap:anywhere}
.hotword-json-guide code{color:var(--accent);font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.hotword-json-guide pre{overflow-x:auto}
.hotword-exclusions{grid-column:1 / -1}
.hotword-exclusions summary,.hotword-advanced summary{cursor:pointer;color:var(--muted);font-size:var(--font-caption);padding:6px 0}
.hotword-exclusions .form-field,.hotword-advanced .form-field{margin-top:10px}
/* hotword-list（热词表容器）：按设置面板实际宽度切换为纵向词条。 */
@container hotword-list (max-width:600px){
  .hotword-row{grid-template-columns:minmax(0,1fr) auto}
  .hotword-row .form-field{grid-column:1}
  .hotword-remove{grid-column:2;grid-row:1 / 4}
}
@container hotword-list (max-width:420px){
  .hotword-toolbar{flex-direction:column;align-items:stretch}
  .hotword-search{width:100%}
  .hotword-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));width:100%}
  .hotword-pagination{gap:3px}
  .hotword-pagination .hotword-page-edge{padding-inline:5px}
  .hotword-page-number{flex-basis:28px;min-width:28px}
  .hotword-page-ellipsis{flex-basis:10px}
}
@media(max-width:640px){
  .hotword-list-window{padding:16px}
  .hotword-stream-option{margin-left:12px}
  .hotword-save{align-items:stretch}
  .hotword-save .save-button{width:100%}
  .hotword-json-guide{grid-template-columns:minmax(0,1fr)}
  .hotword-json-field textarea{height:42vh;min-height:220px}
  .hotword-list-scroll{max-height:68vh}
}
</style>
