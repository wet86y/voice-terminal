<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ReferenceAudioPlayer from './ReferenceAudioPlayer.vue'
import { useTtsVoiceState, voiceDraft, TTS_SAMPLE_TEXT, TTS_DESIGN_STEPS, TTS_DESIGN_RECIPE, type VoiceDraft } from './useTtsVoiceState'
import type { ProductLocalTtsModelId, ProductTtsVoice, ProductTtsOperation, TtsVoicePort } from './tts-voice-types'

const props = withDefaults(defineProps<{ model: ProductLocalTtsModelId; steps: number; unavailable?: boolean; demo?: boolean; port?: TtsVoicePort }>(), { unavailable: false, demo: false })
const productApi = props.port! // port（业务接口）：演示模式仅使用模拟数据。
const emit = defineEmits<{ changed: [] }>()
const state = useTtsVoiceState()
const activeReferenceKey = ref('') // activeReferenceKey（当前播放附件所属草稿）。
const transcribingKey = ref('') // transcribingKey（正在识别的草稿）。
const localAudioUrls = ref<Record<string, string>>({}) // localAudioUrls（演示附件的临时播放地址）。
function releaseLocalAudio(draft: VoiceDraft) {
  const assetId = draft.source?.asset_id
  if (assetId && localAudioUrls.value[assetId]) { URL.revokeObjectURL(localAudioUrls.value[assetId]); delete localAudioUrls.value[assetId] }
}
onBeforeUnmount(() => { for (const url of Object.values(localAudioUrls.value)) URL.revokeObjectURL(url) })
function referenceUrl(draft: VoiceDraft) {
  if (!draft.source) return undefined
  return props.demo ? localAudioUrls.value[draft.source.asset_id] : productApi.ttsAssetAudioUrl(props.model, draft.source.asset_id)
}
async function edit(draft: VoiceDraft) {
  draft.expanded = !draft.expanded
  if (draft.expanded) {
    await nextTick()
    const input = document.getElementById(`voice-name-${draft.key}`) as HTMLInputElement | null
    input?.focus(); input?.select()
  }
}
watch(() => [props.model, props.steps] as const, ([model, steps]) => { state.inferenceSteps[model] = steps }, { immediate: true })
const bucket = computed(() => state.models[props.model])
const drafts = computed(() => Object.values(bucket.value.drafts))
const globalBusy = computed(() => Object.values(state.models).some(model => Object.values(model.drafts).some(draft => draft.busy)))
function signature(draft: VoiceDraft, model = props.model) {
  return JSON.stringify([model, draft.description.trim(), draft.source?.asset_id, draft.transcript, draft.text, TTS_DESIGN_STEPS, TTS_DESIGN_RECIPE])
}
function invalidate(draft: VoiceDraft) {
  draft.generation++; draft.candidate = null; draft.signature = ''; draft.notice = ''
  if (draft.operationId && !props.demo) void productApi.cancelTtsOperation(draft.operationId).catch(() => {})
}
async function refresh(model = props.model) {
  try {
    if (props.demo && !state.models[model].voices.length) {
      state.models[model].voices = [{ id: 'demo-default', revision: 1, name: '默认音色', source: 'legacy', model, is_default: true, enabled: true, reference_asset_id: 'demo-reference', reference_text: TTS_SAMPLE_TEXT, duration_ms: 5680 }]
    } else if (!props.demo) state.models[model].voices = (await productApi.ttsVoices(model)).voices
    for (const voice of state.models[model].voices) {
      if (!state.models[model].drafts[voice.id]) {
        const draft = voiceDraft(voice)
        // 已保存样音保留原制作配方，允许改名或校对转写；改变生成输入仍会使候选失效。
        draft.signature = signature(draft, model)
        state.models[model].drafts[voice.id] = draft
      }
    }
  } catch (error) {
    const draft = Object.values(state.models[model].drafts)[0]
    if (draft) draft.error = error instanceof Error ? error.message : '读取音色库失败。'
  }
}
onMounted(() => refresh())
watch(() => props.model, model => { void refresh(model) })
function add() { const draft = voiceDraft(); draft.expanded = true; bucket.value.drafts[draft.key] = draft }
function stored(draft: VoiceDraft) { return bucket.value.voices.find(voice => voice.id === draft.id) }
/** voiceMode（音色创建方式）：design（纯设计）、mixed（混合设计）、clone（纯克隆）。 */
function voiceMode(draft: VoiceDraft): 'design' | 'mixed' | 'clone' {
  if (props.model === 'voxcpm2' && draft.description.trim()) return draft.source ? 'mixed' : 'design'
  return 'clone'
}
/** voiceName（保存名称）：空名称按当前创建方式补全，已有音色保留原名。 */
function voiceName(draft: VoiceDraft) {
  const defaults = { design: '设计音色', mixed: '混合音色', clone: '克隆音色' } // defaults（未命名音色的默认名称）。
  return draft.name.trim() || stored(draft)?.name || defaults[voiceMode(draft)]
}
async function monitor(draft: VoiceDraft, start: () => Promise<ProductTtsOperation>, generation: number) {
  let operation = await start()
  if (draft.generation !== generation) { await productApi.cancelTtsOperation(operation.operation_id); return operation }
  draft.operationId = operation.operation_id
  while (operation.state === 'running') {
    await new Promise(resolve => window.setTimeout(resolve, 400))
    operation = await productApi.getTtsOperation(operation.operation_id)
    if (draft.generation !== generation) return operation
  }
  if (operation.state === 'failed') { draft.error = operation.error || '音色任务失败。'; if (!operation.result?.voice) throw new Error(draft.error) }
  return operation
}
async function run(draft: VoiceDraft, action: (generation: number) => Promise<void>) {
  if (draft.busy || globalBusy.value) return
  const generation = draft.generation
  draft.busy = true; draft.error = ''; draft.notice = ''
  try { await action(generation) }
  catch (error) { if (draft.generation === generation) draft.error = error instanceof Error ? error.message : '音色操作失败。' }
  finally { draft.busy = false; draft.operationId = undefined }
}
async function transcribe(draft: VoiceDraft) {
  const model = props.model; const asset = draft.source; const initialText = draft.transcript
  if (!asset || globalBusy.value) return
  transcribingKey.value = draft.key
  try {
    await run(draft, async generation => {
      if (props.demo) { draft.transcript = '这是一段参考音频的演示转写，可以在这里校对。'; return }
      const result = await monitor(draft, () => productApi.startTtsTranscribe(model, asset.asset_id), generation)
      if (draft.generation === generation && draft.source?.asset_id === asset.asset_id && draft.transcript === initialText && result.result?.text) draft.transcript = result.result.text
      if (draft.generation === generation) {
        const notices = []
        if (result.result?.fallback_reason) notices.push('离线识别未完成，已改用流式模型转写。')
        if (result.result?.punctuation_status === 'DEGRADED') notices.push('标点处理失败，已保留识别正文，请校对。')
        draft.notice = notices.join(' ')
      }
    })
  } finally { transcribingKey.value = '' }
}
async function upload(draft: VoiceDraft, files: FileList | File[]) {
  if (files.length !== 1) { draft.error = '请一次选择一条参考音频。'; return }
  const model = props.model; const file = files[0]
  invalidate(draft); const generation = draft.generation; draft.busy = true; draft.error = ''
  try {
    const asset = props.demo ? { asset_id: crypto.randomUUID(), duration_ms: 5680 } : (await productApi.uploadTtsAsset(model, file)).asset
    if (draft.generation !== generation) return
    releaseLocalAudio(draft)
    if (props.demo) localAudioUrls.value[asset.asset_id] = URL.createObjectURL(file)
    draft.source = { ...asset, filename: file.name }; draft.transcript = ''; draft.name ||= file.name.replace(/\.[^.]+$/, '')
  } catch (error) { if (draft.generation === generation) draft.error = error instanceof Error ? error.message : '上传失败，原附件已保留。' }
  finally { draft.busy = false }
  if (draft.generation === generation && draft.source?.filename === file.name) await transcribe(draft)
}
function fileChange(draft: VoiceDraft, event: Event) { const input = event.target as HTMLInputElement; if (input.files?.length) void upload(draft, input.files); input.value = '' }
function drop(draft: VoiceDraft, event: DragEvent) { if (!globalBusy.value && event.dataTransfer?.files.length) void upload(draft, event.dataTransfer.files) }
function removeSource(draft: VoiceDraft) { releaseLocalAudio(draft); invalidate(draft); draft.source = null; draft.transcript = '' }
async function preview(draft: VoiceDraft) {
  const model = props.model; const prepared = signature(draft, model)
  await run(draft, async generation => {
    const voice = { name: voiceName(draft), design_description: voiceMode(draft) === 'clone' ? '' : draft.description, source_reference_asset_id: draft.source?.asset_id, source_reference_text: draft.transcript }
    if (props.demo) {
      draft.candidate = { ...voice, source: draft.description.trim() ? 'design' : 'import', reference_asset_id: 'demo-target', reference_text: draft.text, design_text: draft.text, inference_steps: TTS_DESIGN_STEPS, generation_recipe: TTS_DESIGN_RECIPE }
      draft.targetTranscript = draft.text
      draft.signature = prepared; draft.notice = '试音演示完成。'; return
    }
    const result = await monitor(draft, () => productApi.startTtsPreview(model, voice, draft.text), generation)
    if (draft.generation === generation && signature(draft, model) === prepared && result.result?.voice) {
      draft.candidate = result.result.voice; draft.targetTranscript = result.result.voice.reference_text || ''; draft.signature = prepared; draft.notice = '试音完成，请核对目标样音转写后保存。'
    }
  })
}
async function play(draft: VoiceDraft) {
  const model = props.model; const voice = stored(draft)
  if (!voice) return
  await run(draft, async generation => {
    if (props.demo) { draft.notice = '已试听演示音色。'; return }
    await monitor(draft, () => productApi.startTtsPlay(model, voice.reference_asset_id), generation)
  })
}
/** saveBlockReason（保存阻碍说明）：按钮与提示使用相同的内容校验，名称可自动补全。 */
function saveBlockReason(draft: VoiceDraft) {
  if (unchangedContent(draft)) return ''
  const mode = voiceMode(draft) // mode（当前音色创建方式）。
  if (mode !== 'clone') {
    if (draft.candidate && !draft.targetTranscript.trim()) return '请填写目标样音实际发音的准确转写。'
    if (draft.candidate && draft.signature === signature(draft)) return ''
    return `${mode === 'mixed' ? '混合设计' : '纯音色设计'}需要以当前参数完成一次试音，再保存生成的目标样音。`
  }
  if (!draft.source) return props.model === 'voxcpm2' ? '请填写音色设计描述，或添加参考音频进行克隆；也可以组合使用。' : '请添加参考音频进行音色克隆。'
  if (!draft.transcript.trim()) return '请填写参考音频的准确转写文本后保存克隆音色。'
  return ''
}
function saveable(draft: VoiceDraft) { return !saveBlockReason(draft) }
function unchangedContent(draft: VoiceDraft) {
  const voice = stored(draft)
  if (!voice) return false
  const original = voiceDraft(voice)
  return draft.description === original.description && draft.transcript === original.transcript && draft.text === original.text && draft.targetTranscript === original.targetTranscript && draft.source?.asset_id === original.source?.asset_id
}
async function save(draft: VoiceDraft) {
  if (!saveable(draft)) return
  const model = props.model; const name = voiceName(draft)
  await run(draft, async () => {
    const payload = unchangedContent(draft) ? { ...stored(draft)!, name } : voiceMode(draft) !== 'clone' ? { ...draft.candidate, name, reference_text: draft.targetTranscript.trim() } : {
      name, source: 'import', reference_asset_id: draft.source!.asset_id,
      reference_text: draft.transcript, source_reference_text: draft.transcript,
    }
    if (props.demo) {
      const original = state.models[model].voices.find(voice => voice.id === draft.id)
      const voice = { ...payload, id: draft.id || draft.key, revision: (draft.revision || 0) + 1, model, enabled: original?.enabled || false, is_default: original?.is_default || false } as ProductTtsVoice
      state.models[model].voices = [...state.models[model].voices.filter(item => item.id !== voice.id), voice]
      draft.id = voice.id; draft.revision = voice.revision
    } else {
      const response = draft.id ? await productApi.updateTtsVoice(model, draft.id, payload, draft.revision!) : await productApi.createTtsVoice(model, payload)
      draft.id = response.voice.id; draft.revision = response.voice.revision
      delete state.models[model].drafts[draft.key]; draft.key = response.voice.id; state.models[model].drafts[draft.key] = draft
      await refresh(model)
    }
    draft.name = name; draft.expanded = false
    draft.notice = '音色已保存；已启用音色从下一次回答生效。'; emit('changed')
  })
}
async function toggle(draft: VoiceDraft, enabled: boolean) {
  const model = props.model; const voice = stored(draft)
  if (!voice) return
  await run(draft, async () => {
    if (props.demo) for (const item of state.models[model].voices) item.enabled = enabled ? item.id === voice.id : item.is_default
    else { await productApi.selectTtsVoice(model, enabled ? { voice_id: voice.id, revision: voice.revision } : null); await refresh(model) }
    emit('changed')
  })
}
async function remove(draft: VoiceDraft) {
  const model = props.model
  await run(draft, async () => {
    if (draft.id && !props.demo) await productApi.deleteTtsVoice(model, draft.id)
    releaseLocalAudio(draft)
    delete state.models[model].drafts[draft.key]
    state.models[model].voices = state.models[model].voices.filter(item => item.id !== draft.id)
    if (!props.demo) await refresh(model)
    emit('changed')
  })
}
async function cancel(draft: VoiceDraft) { invalidate(draft); draft.notice = '任务已取消。' }
</script>

<template>
  <section class="voice-library" aria-label="本地音色库">
    <header class="library-heading"><div><h3>音色库</h3><p>为当前模型保存独立音色，启用后用于下一次回答。</p></div><button type="button" class="outline-button" @click="add">＋ 添加音色</button></header>
    <p v-if="!drafts.length" class="instruction-hint">尚无音色。可以添加参考音频，或使用二代模型设计音色。</p>
    <article v-for="draft in drafts" :key="draft.key" class="voice-card" :class="{ enabled: stored(draft)?.enabled }">
      <header class="voice-heading"><div class="voice-name"><input v-if="draft.expanded" :id="`voice-name-${draft.key}`" v-model="draft.name" class="voice-name-input" aria-label="音色名称" maxlength="80" placeholder="音色名称（可选）" :disabled="draft.busy"><strong v-else>{{ stored(draft)?.name || draft.name || '新音色' }}</strong><span v-if="stored(draft)?.is_default" class="voice-badge">默认</span><small v-if="stored(draft)?.enabled">正在使用</small></div>
        <div class="voice-actions"><button type="button" :disabled="!draft.id || unavailable || globalBusy" @click="play(draft)">试听</button><button type="button" :aria-expanded="draft.expanded" :aria-controls="`voice-editor-${draft.key}`" @click="edit(draft)">{{ draft.expanded ? '收起' : '编辑' }}</button>
          <label class="voice-toggle"><input type="checkbox" role="switch" :aria-label="`启用 ${draft.name || '音色'}`" :checked="stored(draft)?.enabled" :disabled="!draft.id || globalBusy || (stored(draft)?.is_default && stored(draft)?.enabled)" @change="toggle(draft, ($event.target as HTMLInputElement).checked)"><span aria-hidden="true"></span></label>
        </div></header>
      <div v-if="draft.expanded" :id="`voice-editor-${draft.key}`" class="voice-editor">
        <div class="voice-inputs" :class="{ single: model === 'voxcpm1' }">
          <label v-if="model === 'voxcpm2'" class="form-field voice-design"><span>音色设计 <small>可选</small></span><textarea v-model="draft.description" class="compact-textarea" rows="1" maxlength="500" placeholder="例如：语速稍慢，语气平稳自然。" @input="invalidate(draft)"></textarea><small v-if="draft.source">已添加参考音频：描述只控制语速、语气等风格；音色由参考音频决定。不要再写性别、年龄、音质等音色特征，那会覆盖参考音频的音色。</small><small v-else>描述声音特征（性别、年龄、音色、语速），仅用描述即可从零设计音色。</small></label>
          <label class="form-field"><span>音频转写 <small>{{ model === 'voxcpm1' ? '准确转写必填' : '可校对' }}</small></span><textarea v-model="draft.transcript" class="compact-textarea" rows="1" placeholder="添加音频后自动识别，也可手动输入并校对。" @input="invalidate(draft)"></textarea></label>
        </div>
        <div class="voice-reference">
          <div class="reference-input" @dragover.prevent @drop.prevent="drop(draft, $event)">
            <ReferenceAudioPlayer v-if="draft.source" :key="`${model}:${draft.source.asset_id}`" :src="referenceUrl(draft)" :filename="draft.source.filename" :duration-ms="draft.source.duration_ms" :busy="globalBusy" :transcribing="transcribingKey === draft.key" :active="activeReferenceKey === draft.key" @play="activeReferenceKey = draft.key" @transcribe="transcribe(draft)" @remove="removeSource(draft)" @error="draft.error = $event" />
            <label v-else class="attachment-upload" :class="{ disabled: globalBusy }">
              <input type="file" aria-label="添加参考音频" accept=".wav,.mp3,.flac,.ogg" :disabled="globalBusy" @change="fileChange(draft, $event)">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m-4 4 4-4 4 4M5 15v5h14v-5" /></svg>
              <span class="upload-caption"><strong>添加参考音频</strong><small>点击选择或拖入文件</small></span>
            </label>
          </div>
          <small class="reference-hint">支持 5–15 秒音频（官方建议至少 5 秒，5–12 秒最佳），最大 20 MiB（二进制兆字节）。</small>
        </div>
        <div class="voice-footer"><label class="form-field"><span>试音正文</span><textarea v-model="draft.text" class="compact-textarea" rows="1" maxlength="1000" @input="invalidate(draft)"></textarea></label><div class="voice-footer-actions"><button type="button" :disabled="unavailable || globalBusy || (!draft.description.trim() && !draft.source)" @click="preview(draft)">试音</button><button type="button" class="save-button" :disabled="globalBusy || !saveable(draft)" @click="save(draft)">保存</button><button type="button" class="danger-action" :disabled="globalBusy || stored(draft)?.is_default" @click="remove(draft)">{{ draft.id ? '删除' : '取消添加' }}</button></div></div>
        <label v-if="voiceMode(draft) !== 'clone' && draft.candidate" class="form-field"><span>目标样音转写</span><textarea v-model="draft.targetTranscript" aria-label="目标样音转写" class="compact-textarea" rows="1" maxlength="2000" :disabled="globalBusy"></textarea><small>请按样音实际发音校对；保存后以这段样音和转写克隆播报。</small></label>
        <p v-if="model === 'voxcpm2' && voiceMode(draft) !== 'clone'" class="instruction-hint">设计与混合样音固定使用 30 步完整解码；正式播报沿用合成质量设置中的步数。</p>
        <p v-if="saveBlockReason(draft)" class="instruction-hint">{{ saveBlockReason(draft) }}</p>
        <p v-if="!draft.name.trim()" class="instruction-hint">未填写名称时，将保存为“{{ voiceName(draft) }}”。</p>
      </div>
      <div v-if="draft.busy" class="voice-task" role="status"><span>正在处理音色…</span><button type="button" @click="cancel(draft)">取消任务</button></div>
      <p v-if="draft.error" class="instruction-error" role="alert">{{ draft.error }}</p><p v-if="draft.notice" class="instruction-hint" role="status">{{ draft.notice }}</p>
    </article>
  </section>
</template>

<style scoped>
.voice-library { display: grid; gap: 14px; container: voice-library / inline-size; }
.library-heading,.voice-heading,.voice-actions,.voice-footer-actions,.voice-name,.voice-task { display: flex; align-items: center; gap: 10px; }
.library-heading,.voice-heading { justify-content: space-between; }
.library-heading > div { min-width: 0; }.library-heading > button { flex-shrink: 0; white-space: nowrap; }
.library-heading p { color: var(--muted); margin-top: 5px; }
.voice-card { background: var(--panel); border: 1px solid var(--line-strong); border-radius: 18px; padding: 18px 20px; display: grid; gap: 16px; }
.voice-card.enabled { border-color: color-mix(in srgb, var(--mint) 42%, transparent); }
.voice-name { min-width: 0; flex: 1; flex-wrap: wrap; }.voice-name strong { overflow-wrap: anywhere; }.voice-name small { color: var(--mint); }
.voice-name .voice-name-input { width: 180px; max-width: 100%; min-height: 32px; height: 32px; padding: 5px 9px; font-weight: 570; }
.voice-badge { border-radius: 7px; background: var(--panel-soft); color: var(--muted); padding: 3px 7px; font-size: .7rem; }
.voice-actions { flex-shrink: 0; }.voice-actions button { padding: 6px 11px; }
.voice-toggle { display: inline-flex; position: relative; padding: 8px; cursor: pointer; }.voice-toggle input { position: absolute; width: 36px; height: 20px; opacity: 0; }
.voice-toggle span { width: 36px; height: 20px; border-radius: 20px; background: var(--line-strong); }.voice-toggle span::after { content: ''; display: block; width: 14px; height: 14px; margin: 3px; border-radius: 50%; background: var(--muted); transition: transform 160ms; }
.voice-toggle input:checked + span { background: var(--mint); }.voice-toggle input:checked + span::after { transform: translateX(16px); background: var(--bg); }.voice-toggle input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 3px; }.voice-toggle input:disabled + span { opacity: .5; }
.voice-editor { display: grid; gap: 18px; border-top: 1px solid var(--line); padding-top: 18px; }
.voice-inputs { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 18px; align-items: start; }.voice-inputs.single { grid-template-columns: 1fr; }
.voice-inputs .form-field { min-width: 0; }
.voice-reference { display: grid; gap: 8px; min-width: 0; }.reference-input { min-width: 0; }
.attachment-upload { position: relative; display: flex; align-items: center; justify-content: center; gap: 12px; min-height: 72px; padding: 12px; border: 1px dashed var(--line-strong); background: #10141d; border-radius: 10px; cursor: pointer; }
.attachment-upload:hover:not(.disabled),.attachment-upload:focus-within { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 5%, #10141d); }
.attachment-upload:focus-within { outline: 2px solid var(--accent); outline-offset: 3px; }
.attachment-upload.disabled { opacity: .45; cursor: not-allowed; }.attachment-upload input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: inherit; }
.attachment-upload svg { width: 22px; height: 22px; fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.upload-caption { display: grid; gap: 3px; }.attachment-upload strong { font-size: .75rem; }.attachment-upload small,.reference-hint { font-size: .625rem; line-height: 1.5; }
.voice-footer { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: end; gap: 12px; }
.voice-footer-actions { gap: 8px; }.voice-footer-actions button { height: 42px; padding: 8px 12px; white-space: nowrap; }
.voice-task { color: var(--muted); justify-content: space-between; }
@container voice-library (max-width: 600px) { .voice-inputs { grid-template-columns: 1fr; }.voice-card { padding: 16px; }.voice-name .voice-name-input { width: 140px; } }
@container voice-library (max-width: 480px) { .voice-heading { flex-wrap: wrap; }.voice-actions { margin-left: auto; }.voice-footer { grid-template-columns: 1fr; }.voice-footer-actions { justify-content: flex-end; } }
</style>
