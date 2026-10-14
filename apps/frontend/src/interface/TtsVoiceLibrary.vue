<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useTtsVoiceState, voiceDraft, TTS_SAMPLE_TEXT, type VoiceDraft } from './useTtsVoiceState'
import type { ProductLocalTtsModelId, ProductTtsVoice, ProductTtsOperation, TtsVoicePort } from './tts-voice-types'

const props = withDefaults(defineProps<{ model: ProductLocalTtsModelId; steps: number; unavailable?: boolean; demo?: boolean; port?: TtsVoicePort }>(), { unavailable: false, demo: false })
const productApi = props.port! // port（业务接口）：演示模式仅使用模拟数据。
const emit = defineEmits<{ changed: [] }>()
const state = useTtsVoiceState()
watch(() => [props.model, props.steps] as const, ([model, steps]) => { state.inferenceSteps[model] = steps }, { immediate: true })
const bucket = computed(() => state.models[props.model])
const drafts = computed(() => Object.values(bucket.value.drafts))
const globalBusy = computed(() => Object.values(state.models).some(model => Object.values(model.drafts).some(draft => draft.busy)))
function signature(draft: VoiceDraft, model = props.model) {
  return JSON.stringify([model, draft.description.trim(), draft.source?.asset_id, draft.transcript, draft.text, state.inferenceSteps[model]])
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
        draft.signature = voice.source !== 'design' || voice.inference_steps === state.inferenceSteps[model] ? signature(draft, model) : ''
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
  if (!asset) return
  await run(draft, async generation => {
    if (props.demo) { draft.transcript = '这是一段参考音频的演示转写，可以在这里校对。'; return }
    const result = await monitor(draft, () => productApi.startTtsTranscribe(model, asset.asset_id), generation)
    if (draft.generation === generation && draft.source?.asset_id === asset.asset_id && draft.transcript === initialText && result.result?.text) draft.transcript = result.result.text
  })
}
async function upload(draft: VoiceDraft, files: FileList | File[]) {
  if (files.length !== 1) { draft.error = '请一次选择一条参考音频。'; return }
  const model = props.model; const file = files[0]
  invalidate(draft); const generation = draft.generation; draft.busy = true; draft.error = ''
  try {
    const asset = props.demo ? { asset_id: crypto.randomUUID(), duration_ms: 5680 } : (await productApi.uploadTtsAsset(model, file)).asset
    if (draft.generation !== generation) return
    draft.source = { ...asset, filename: file.name }; draft.transcript = ''; draft.name ||= file.name.replace(/\.[^.]+$/, '')
  } catch (error) { if (draft.generation === generation) draft.error = error instanceof Error ? error.message : '上传失败，原附件已保留。' }
  finally { draft.busy = false }
  if (draft.generation === generation && draft.source?.filename === file.name) await transcribe(draft)
}
function fileChange(draft: VoiceDraft, event: Event) { const input = event.target as HTMLInputElement; if (input.files?.length) void upload(draft, input.files); input.value = '' }
function drop(draft: VoiceDraft, event: DragEvent) { if (!draft.busy && event.dataTransfer?.files.length) void upload(draft, event.dataTransfer.files) }
function removeSource(draft: VoiceDraft) { invalidate(draft); draft.source = null; draft.transcript = '' }
async function preview(draft: VoiceDraft) {
  const model = props.model; const prepared = signature(draft, model)
  await run(draft, async generation => {
    const voice = { name: draft.name || '新音色', design_description: draft.description, source_reference_asset_id: draft.source?.asset_id, source_reference_text: draft.transcript }
    if (props.demo) {
      draft.candidate = { ...voice, source: draft.description.trim() ? 'design' : 'import', reference_asset_id: 'demo-target', reference_text: draft.text, design_text: draft.text, inference_steps: props.steps }
      draft.signature = prepared; draft.notice = '试音演示完成。'; return
    }
    const result = await monitor(draft, () => productApi.startTtsPreview(model, voice, draft.text), generation)
    if (draft.generation === generation && signature(draft, model) === prepared && result.result?.voice) {
      draft.candidate = result.result.voice; draft.signature = prepared; draft.notice = '试音完成，可以保存音色。'
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
function saveable(draft: VoiceDraft) {
  return !!draft.name.trim() && (props.model === 'voxcpm2' && draft.description.trim()
    ? !!draft.candidate && draft.signature === signature(draft)
    : !!draft.source && (props.model === 'voxcpm2' || !!draft.transcript.trim()))
}
async function save(draft: VoiceDraft) {
  if (!saveable(draft)) return
  const model = props.model
  await run(draft, async () => {
    const payload = draft.description.trim() ? { ...draft.candidate, name: draft.name.trim() } : {
      name: draft.name.trim(), source: 'import', reference_asset_id: draft.source!.asset_id,
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
      <header class="voice-heading"><div class="voice-name"><strong>{{ stored(draft)?.name || draft.name || '新音色' }}</strong><span v-if="stored(draft)?.is_default" class="voice-badge">默认</span><small v-if="stored(draft)?.enabled">正在使用</small></div>
        <div class="voice-actions"><button type="button" :disabled="!draft.id || unavailable || globalBusy" @click="play(draft)">试听</button><button type="button" :aria-expanded="draft.expanded" @click="draft.expanded = !draft.expanded">{{ draft.expanded ? '收起' : '编辑' }}</button>
          <label class="voice-toggle"><input type="checkbox" role="switch" :aria-label="`启用 ${draft.name || '音色'}`" :checked="stored(draft)?.enabled" :disabled="!draft.id || globalBusy || (stored(draft)?.is_default && stored(draft)?.enabled)" @change="toggle(draft, ($event.target as HTMLInputElement).checked)"><span aria-hidden="true"></span></label>
        </div></header>
      <div v-if="draft.expanded" class="voice-editor">
        <label class="form-field voice-title"><span>音色名称</span><input v-model="draft.name" maxlength="80" placeholder="给这个音色起个名字" :disabled="draft.busy"></label>
        <div class="voice-inputs" :class="{ single: model === 'voxcpm1' }">
          <label v-if="model === 'voxcpm2'" class="form-field"><span>音色设计 <small>可选</small></span><textarea v-model="draft.description" rows="6" maxlength="500" placeholder="例如：温和自然的女声，清晰、平稳，略带微笑。可以单独设计，也可以结合右侧参考音频。" @input="invalidate(draft)"></textarea></label>
          <div class="form-field"><span>克隆参考与文本校对 <small>{{ model === 'voxcpm1' ? '准确转写必填' : '可选' }}</small></span>
            <div class="reference-input" @dragover.prevent @drop.prevent="drop(draft, $event)">
              <textarea v-model="draft.transcript" rows="5" placeholder="拖入一条参考音频。系统使用当前精修模型识别，你可以在这里校对。" @input="invalidate(draft)"></textarea>
              <div v-if="draft.source" class="audio-attachment"><span class="audio-icon" aria-hidden="true">♫</span><div><strong>{{ draft.source.filename }}</strong><small>{{ (draft.source.duration_ms / 1000).toFixed(2) }} 秒</small></div><button type="button" aria-label="移除参考音频" :disabled="draft.busy" @click="removeSource(draft)">×</button></div>
              <label class="attachment-upload"><input type="file" accept=".wav,.mp3,.flac,.ogg" :disabled="draft.busy" @change="fileChange(draft, $event)"><span>{{ draft.source ? '替换音频' : '＋ 选择音频' }}</span></label>
            </div><small>支持 3–15 秒的音频，最大 20 MiB（二进制兆字节）。</small>
            <button v-if="draft.source" type="button" class="transcribe-action" :disabled="globalBusy" @click="transcribe(draft)">重新识别校对文本</button>
          </div>
        </div>
        <div class="voice-footer"><label class="form-field"><span>试音正文</span><textarea v-model="draft.text" rows="2" maxlength="1000" @input="invalidate(draft)"></textarea></label><div class="voice-footer-actions"><button type="button" :disabled="unavailable || globalBusy || (!draft.description.trim() && !draft.source)" @click="preview(draft)">试音</button><button type="button" class="save-button" :disabled="globalBusy || !saveable(draft)" @click="save(draft)">保存</button><button type="button" class="danger-action" :disabled="globalBusy || stored(draft)?.is_default" @click="remove(draft)">{{ draft.id ? '删除' : '取消添加' }}</button></div></div>
        <p v-if="draft.description.trim() && !saveable(draft)" class="instruction-hint">设计或混合音色需要以当前参数完成一次试音，再保存生成的目标样音。</p>
      </div>
      <div v-if="draft.busy" class="voice-task" role="status"><span>正在处理音色…</span><button type="button" @click="cancel(draft)">取消任务</button></div>
      <p v-if="draft.error" class="instruction-error" role="alert">{{ draft.error }}</p><p v-if="draft.notice" class="instruction-hint" role="status">{{ draft.notice }}</p>
    </article>
  </section>
</template>

<style scoped>
.voice-library { display: grid; gap: 14px; }
.library-heading,.voice-heading,.voice-actions,.voice-footer-actions,.voice-name,.voice-task { display: flex; align-items: center; gap: 10px; }
.library-heading,.voice-heading { justify-content: space-between; }
.library-heading p { color: var(--muted); margin-top: 5px; }
.voice-card { background: var(--panel); border: 1px solid var(--line-strong); border-radius: 18px; padding: 18px 20px; display: grid; gap: 16px; }
.voice-card.enabled { border-color: color-mix(in srgb, var(--mint) 42%, transparent); }
.voice-name { min-width: 0; flex-wrap: wrap; }.voice-name strong { overflow-wrap: anywhere; }.voice-name small { color: var(--mint); }
.voice-badge { border-radius: 7px; background: var(--panel-soft); color: var(--muted); padding: 3px 7px; font-size: .7rem; }
.voice-actions { flex-shrink: 0; }.voice-actions button { padding: 6px 11px; }
.voice-toggle { display: inline-flex; position: relative; padding: 8px; cursor: pointer; }.voice-toggle input { position: absolute; width: 36px; height: 20px; opacity: 0; }
.voice-toggle span { width: 36px; height: 20px; border-radius: 20px; background: var(--line-strong); }.voice-toggle span::after { content: ''; display: block; width: 14px; height: 14px; margin: 3px; border-radius: 50%; background: var(--muted); transition: transform 160ms; }
.voice-toggle input:checked + span { background: var(--mint); }.voice-toggle input:checked + span::after { transform: translateX(16px); background: var(--bg); }.voice-toggle input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 3px; }.voice-toggle input:disabled + span { opacity: .5; }
.voice-editor { display: grid; gap: 17px; border-top: 1px solid var(--line); padding-top: 18px; }.voice-title { max-width: 420px; }
.voice-inputs { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 18px; }.voice-inputs.single { grid-template-columns: 1fr; }.voice-inputs textarea { resize: vertical; min-height: 170px; }
.reference-input { border: 1px solid var(--line-strong); background: #10141d; border-radius: 10px; overflow: hidden; display: grid; gap: 8px; padding-bottom: 10px; }.reference-input:focus-within { border-color: var(--accent); }.reference-input textarea { border: 0; background: transparent; min-height: 115px; }
.audio-attachment { display: flex; align-items: center; gap: 10px; margin: 0 10px; padding: 10px; background: var(--panel-soft); border: 1px solid var(--line); border-radius: 9px; }.audio-attachment div { flex: 1; min-width: 0; }.audio-attachment strong { display: block; overflow-wrap: anywhere; font-size: .75rem; }.audio-attachment button { padding: 2px 7px; }.audio-icon { color: var(--accent); font-size: 1.3rem; }
.attachment-upload { margin-left: 12px; color: var(--accent); cursor: pointer; font-size: .75rem; }.attachment-upload input { display: none; }.transcribe-action { justify-self: start; font-size: .7rem; padding: 5px 9px; }
.voice-footer { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: end; gap: 16px; }.voice-footer textarea { resize: vertical; }.voice-footer-actions { padding-bottom: 2px; }.voice-task { color: var(--muted); justify-content: space-between; }
@media (max-width: 800px) { .voice-inputs,.voice-footer { grid-template-columns: 1fr; }.voice-heading { align-items: flex-start; }.voice-footer-actions { justify-content: flex-end; }.voice-card { padding: 16px; } }
@media (max-width: 480px) { .voice-heading { flex-wrap: wrap; }.voice-actions { margin-left: auto; } }
</style>
