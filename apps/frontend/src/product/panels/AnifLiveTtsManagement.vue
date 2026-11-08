<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AnifLiveModelImport from './AnifLiveModelImport.vue'
import { productApi, type AnifLiveImportJob } from '../api'
import type { ProductLocalTtsModelId, ProductLocalTtsStatus } from '../types'

type AnifLiveImportCompletion = {
  model_id: string // model_id（导入完成后登记的模型标识）。
  revision?: string | null // revision（导入模型的修订编号）。
  task_id: string // task_id（模型导入任务编号）。
  scan_id?: string // scan_id（模型来源扫描编号）。
  reference_candidates?: AnifLiveImportJob['reference_candidates'] // reference_candidates（待补充的参考素材）。
}

const props = defineProps<{
  models: ProductLocalTtsStatus['models'] // models（本机 AnifLive 模型状态）。
  openImport?: boolean // openImport（跨页跳入时展开导入区）。
  disabled?: boolean // disabled（模型选择或组件任务进行时锁定操作）。
}>()

const emit = defineEmits<{
  importCompleted: [value: AnifLiveImportCompletion] // importCompleted（模型转换和登记完成）。
  refresh: [] // refresh（名称修改后重新读取模型登记）。
  deleteModel: [model: string] // deleteModel（打开既有受管删除确认弹窗）。
}>()

const registeredModels = computed(() => props.models.filter(model => model.registered ?? model.available)) // registeredModels（已转换并登记的模型）。
const importOpen = ref(false) // importOpen（模型来源导入窗口是否打开）。
const editingNames = ref(new Set<string>()) // editingNames（正在编辑名称的模型）。
watch(() => props.openImport, value => { if (value) importOpen.value = true }, { immediate: true })
const nameDrafts = ref<Record<string, string>>({}) // nameDrafts（每个模型的显示名称草稿）。
const savedNames = ref<Record<string, string>>({}) // savedNames（最近一次读取的已保存显示名称）。
const nameErrors = ref<Record<string, string>>({}) // nameErrors（逐模型显示名称保存错误）。
const savingNames = ref(new Set<string>()) // savingNames（正在保存显示名称的模型标识）。
const notice = ref('') // notice（最近一次名称保存结果）。

watch(() => props.models.map(model => [model.id, model.name, model.available] as const), () => {
  const drafts = { ...nameDrafts.value }
  const saved: Record<string, string> = {}
  for (const model of registeredModels.value) {
    const previous = savedNames.value[model.id]
    if (!(model.id in drafts) || drafts[model.id] === previous) drafts[model.id] = model.name
    saved[model.id] = model.name
  }
  nameDrafts.value = drafts
  savedNames.value = saved
}, { immediate: true })

function handleModelImportCompleted(value: AnifLiveImportCompletion) {
  importOpen.value = false
  emit('importCompleted', value)
}

function nameSaveDisabled(model: ProductLocalTtsStatus['models'][number]) {
  const name = (nameDrafts.value[model.id] ?? '').trim()
  return savingNames.value.has(model.id) || !name || name.length > 80 || name === model.name
}

async function saveModelName(model: ProductLocalTtsStatus['models'][number]) {
  const name = (nameDrafts.value[model.id] ?? model.name).trim()
  if (!name || name.length > 80 || name === model.name || savingNames.value.has(model.id)) return
  nameErrors.value = { ...nameErrors.value, [model.id]: '' }
  notice.value = ''
  savingNames.value = new Set(savingNames.value).add(model.id)
  try {
    const result = await productApi.renameAnifLiveModel(model.id, name)
    nameDrafts.value = { ...nameDrafts.value, [model.id]: result.name }
    savedNames.value = { ...savedNames.value, [model.id]: result.name }
    notice.value = `${result.name}：名称已保存。`
    editingNames.value.delete(model.id)
    emit('refresh')
  } catch (failure) {
    nameErrors.value = { ...nameErrors.value, [model.id]: failure instanceof Error ? failure.message : '保存模型名称失败。' }
  } finally {
    const next = new Set(savingNames.value)
    next.delete(model.id)
    savingNames.value = next
  }
}
</script>

<template>
  <section class="aniflive-management" aria-label="AnifLive-TTS 模型管理">

    <section class="aniflive-registered-models" aria-label="已转换并登记的 AnifLive 模型">
      <div class="aniflive-model-list-heading">
        <h5>已转换并登记的模型</h5>
        <span>{{ registeredModels.length }} 款</span>
      </div>
      <p v-if="!registeredModels.length" class="instruction-hint">暂无已转换并登记的模型，可使用下方“导入”添加。</p>
      <article v-for="model in registeredModels" :key="model.id" class="aniflive-registered-model" :data-model-id="model.id">
        <div class="aniflive-registered-model-copy">
          <strong>{{ model.name }}</strong>
          <div class="aniflive-model-actions"><span class="aniflive-registration-state" :class="{ unavailable: !model.available }"><i aria-hidden="true"></i>{{ model.available ? '已就绪' : '已登记，当前不可用' }}</span><button class="outline-button" type="button" :disabled="savingNames.has(model.id)" @click="editingNames.has(model.id) ? editingNames.delete(model.id) : editingNames.add(model.id)">{{ editingNames.has(model.id) ? '取消重命名' : '重命名' }}</button><button class="outline-button danger-button" type="button" :disabled="disabled || model.loaded || savingNames.has(model.id)" @click="emit('deleteModel', model.id)">删除</button></div>
          <small>模型编号（稳定标识）：{{ model.id }}</small>
          <small v-if="model.loaded">当前运行模型</small>
        </div>
        <label v-if="editingNames.has(model.id)" class="form-field aniflive-name-field">
          <span>显示名称</span>
          <input v-model="nameDrafts[model.id]" maxlength="80" :disabled="savingNames.has(model.id)" :aria-label="`${model.name}的显示名称`" @input="notice = ''; nameErrors[model.id] = ''">
        </label>
        <button v-if="editingNames.has(model.id)" class="outline-button" type="button" :disabled="nameSaveDisabled(model)" @click="saveModelName(model)">
          {{ savingNames.has(model.id) ? '保存中…' : '保存名称' }}
        </button>
        <p v-if="nameErrors[model.id]" class="instruction-error" role="alert">{{ nameErrors[model.id] }}</p>
      </article>
      <p v-if="notice" class="instruction-hint" role="status">{{ notice }}</p>
    </section>

    <section id="aniflive-model-import" class="aniflive-download-card">
      <div class="aniflive-model-list-heading"><h5>模型下载与导入</h5><button class="outline-button" type="button" :aria-expanded="importOpen" @click="importOpen = !importOpen">{{ importOpen ? '关闭导入' : '导入' }}</button></div>
      <AnifLiveModelImport :open="importOpen" embedded @update:open="importOpen = $event" @completed="handleModelImportCompleted" />
    </section>
  </section>
</template>

<style scoped>
.aniflive-management { display: grid; gap: 16px; }
.aniflive-download-card { display: grid; gap: 12px; padding: 14px; border: 1px solid var(--line); border-radius: 12px; }
.aniflive-model-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.aniflive-target-card { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: end; gap: 10px; padding: 14px; border: 1px solid var(--line); border-radius: 12px; background: #ffffff02; }
.aniflive-registered-models { display: grid; gap: 10px; }
.aniflive-model-list-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.aniflive-model-list-heading h5 { margin: 0; font-size: var(--font-body); }
.aniflive-model-list-heading span, .aniflive-registered-model-copy small { color: var(--muted); font-size: var(--font-caption); }
.aniflive-registered-model { display: grid; grid-template-columns: minmax(150px, .8fr) minmax(180px, 1fr) auto; align-items: end; gap: 12px; padding: 12px; border: 1px solid var(--line); border-radius: 12px; }
.aniflive-registered-model-copy { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 5px 12px; min-width: 0; grid-column: 1 / -1; }
.aniflive-registered-model-copy small { grid-column: 1 / -1; }
.aniflive-registration-state { display: flex; align-items: center; gap: 6px; color: var(--accent); font-size: var(--font-caption); }
.aniflive-registration-state.unavailable { color: var(--muted); }
.aniflive-registration-state i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.aniflive-name-field { min-width: 0; }
.instruction-error { margin: 0; }
.aniflive-registered-model > .instruction-error { grid-column: 1 / -1; }
@media(max-width:650px) {
  .aniflive-target-card { grid-template-columns: minmax(0, 1fr) auto; }
  .aniflive-target-card .form-field { grid-column: 1 / -1; }
  .aniflive-registered-model { grid-template-columns: minmax(0, 1fr) auto; }
  .aniflive-registered-model-copy { grid-column: 1 / -1; }
  .aniflive-name-field { min-width: 0; }
}
</style>
