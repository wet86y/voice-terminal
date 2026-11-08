<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ReferenceAudioPlayer from './ReferenceAudioPlayer.vue'
import { useTtsVoiceState, voiceDraft, TTS_SAMPLE_TEXT, TTS_DESIGN_STEPS, TTS_DESIGN_RECIPE, type VoiceDraft } from './useTtsVoiceState'
import type { ProductLocalTtsModelId, ProductTtsVoice, ProductTtsOperation, ProductTtsReference, TtsVoicePort } from './tts-voice-types'

const props = withDefaults(defineProps<{
  model: ProductLocalTtsModelId
  steps?: number
  unavailable?: boolean // unavailable（目标切换或事务进行期间暂不可操作）。
  demo?: boolean // demo（仅演示界面）。
  port?: TtsVoicePort // port（音色库业务接口）。
  ensureSupport?: (model: ProductLocalTtsModelId) => Promise<void> // ensureSupport（试听前确保对应本地模型就绪）。
}>(), { unavailable: false, demo: false })
const productApi = props.port! // port（业务接口）：演示模式仅使用模拟数据。
const emit = defineEmits<{ changed: []; modelImport: [] }>()
const state = useTtsVoiceState()
function ensureModelBucket(model: ProductLocalTtsModelId) {
  state.models[model] ??= { voices: [], drafts: {} }
}
ensureModelBucket(props.model)
watch(() => props.model, model => ensureModelBucket(model), { flush: 'sync' })
const libraryError = ref('') // libraryError（默认参考恢复或全库操作错误）。
let disposed = false
const activeReferenceKey = ref('') // activeReferenceKey（当前播放附件所属草稿）。
const transcribingKey = ref('') // transcribingKey（正在识别的草稿）。
const localAudioUrls = ref<Record<string, string>>({}) // localAudioUrls（演示附件的临时播放地址）。
const clipStarts = ref<Record<string, number>>({}) // 参考片段起点，单位秒。
const clipEnds = ref<Record<string, number>>({}) // 参考片段终点，单位秒。
const expandedExpressions = ref<Record<string, boolean>>({}) // expandedExpressions（逐条情绪参考的展开状态）。
const expandedExpressionLists = ref<Record<string, boolean>>({}) // expandedExpressionLists（每张音色卡的情绪参考队列展开状态）。
function releaseLocalAudio(draft: VoiceDraft) {
  const assetId = draft.source?.asset_id
  if (assetId && localAudioUrls.value[assetId]) { URL.revokeObjectURL(localAudioUrls.value[assetId]); delete localAudioUrls.value[assetId] }
}
onBeforeUnmount(() => { disposed = true; for (const url of Object.values(localAudioUrls.value)) URL.revokeObjectURL(url) })
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
watch(() => [props.model, props.steps] as const, ([model, steps]) => { state.inferenceSteps[model] = steps ?? 0 }, { immediate: true })
const bucket = computed(() => state.models[props.model])
const drafts = computed(() => Object.values(bucket.value.drafts))
const globalBusy = computed(() => Object.values(state.models).some(model => Object.values(model.drafts).some(draft => draft.busy)))
function signature(draft: VoiceDraft, model = props.model) {
  return JSON.stringify([model, draft.description.trim(), draft.source?.asset_id, draft.transcript, draft.referenceLanguage, draft.text, TTS_DESIGN_STEPS, TTS_DESIGN_RECIPE])
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
      const old = state.models[model].drafts[voice.id]
      if (!old || (!old.expanded && !old.busy && old.revision !== voice.revision)) {
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
function addExpression(draft: VoiceDraft) {
  expandedExpressionLists.value[draft.key] = true
  draft.references.push({ id: crypto.randomUUID().replace(/-/g, ''), name: '新情绪', tag: '', intensity: .5,
    asset_id: null, source_asset_id: null, reference_text: '', reference_language: draft.referenceLanguage, status: 'pending', error: '请添加参考录音。' })
}
function expressionChanged(item: ProductTtsReference) {
  item.status = item.asset_id && item.reference_text.trim() && item.reference_language ? 'ready' : 'pending'
  item.error = item.status === 'ready' ? '' : item.error || '请补齐音频、转写和语言。'
}
/** inheritReferenceLanguage（让所有情绪参考沿用主卡片语言，并刷新就绪状态）。 */
function inheritReferenceLanguage(draft: VoiceDraft) {
  for (const item of draft.references) {
    item.reference_language = draft.referenceLanguage
    expressionChanged(item)
  }
  invalidate(draft)
}
async function uploadExpression(draft: VoiceDraft, item: ProductTtsReference, event: Event) {
  const input = event.target as HTMLInputElement; const file = input.files?.[0]; input.value = ''
  if (!file) return
  await run(draft, async () => {
    const source = props.demo ? null : (await productApi.uploadTtsSource(props.model, file)).asset
    item.source_asset_id = source?.asset_id ?? null; item.asset_id = null; item.reference_text = ''
    let asset
    try {
      asset = props.demo ? { asset_id: crypto.randomUUID().replace(/-/g, ''), duration_ms: 5680 } : (await productApi.uploadTtsAsset(props.model, file)).asset
    } catch (error) {
      item.error = error instanceof Error ? error.message : '原录音已保存，请选择 3–10 秒片段。'
      expressionChanged(item)
      return
    }
    if (props.demo) localAudioUrls.value[asset.asset_id] = URL.createObjectURL(file)
    item.asset_id = asset.asset_id; item.reference_text = ''; item.error = ''
    expressionChanged(item)
  })
  if (item.asset_id) await transcribeExpression(draft, item)
}
async function transcribeExpression(draft: VoiceDraft, item: ProductTtsReference) {
  const asset = item.asset_id; if (!asset) return
  transcribingKey.value = item.id
  try {
    await run(draft, async generation => {
      if (props.demo) { item.reference_text = '这是一段参考音频的演示转写。'; expressionChanged(item); return }
      const result = await monitor(draft, () => productApi.startTtsTranscribe(props.model, asset), generation)
      if (draft.generation === generation && item.asset_id === asset && result.result?.text) {
        item.reference_text = result.result.text; item.verification_source = 'automatic_transcription'; expressionChanged(item)
      }
    })
  } finally { transcribingKey.value = '' }
}
function expressionUrl(item: ProductTtsReference) {
  const asset = item.asset_id || item.source_asset_id
  return asset ? (props.demo ? localAudioUrls.value[asset] : productApi.ttsAssetAudioUrl(props.model, asset)) : undefined
}
/** expressionKey（区分不同音色卡片中情绪参考的界面状态键）。 */
function expressionKey(draft: VoiceDraft, item: ProductTtsReference) { return `${draft.key}:${item.id}` }
/** expressionExpanded（读取情绪参考是否展开）。 */
function expressionExpanded(draft: VoiceDraft, item: ProductTtsReference) { return expandedExpressions.value[expressionKey(draft, item)] === true }
/** toggleExpression（切换情绪参考展开状态）。 */
function toggleExpression(draft: VoiceDraft, item: ProductTtsReference) {
  const key = expressionKey(draft, item)
  expandedExpressions.value[key] = !expandedExpressions.value[key]
}
/** expressionListExpanded（读取当前音色的情绪参考队列状态，首次打开音色卡时默认展开）。 */
function expressionListExpanded(draft: VoiceDraft) { return expandedExpressionLists.value[draft.key] !== false }
/** toggleExpressionList（单独展开或收起情绪参考队列）。 */
function toggleExpressionList(draft: VoiceDraft) {
  expandedExpressionLists.value[draft.key] = !expressionListExpanded(draft)
}
/** expressionListId（生成情绪参考队列的无障碍关联编号）。 */
function expressionListId(draft: VoiceDraft) { return `expression-list-${draft.key}` }
/** expressionAudioKey（区分摘要行和展开详情中的试听焦点）。 */
function expressionAudioKey(draft: VoiceDraft, item: ProductTtsReference, area: 'summary' | 'detail') {
  return `${expressionKey(draft, item)}:${area}`
}
/** expressionEditorId（生成情绪参考详情区的无障碍编号）。 */
function expressionEditorId(draft: VoiceDraft, item: ProductTtsReference) {
  return `expression-editor-${expressionKey(draft, item)}`
}
async function clipExpression(draft: VoiceDraft, item: ProductTtsReference) {
  const source = item.source_asset_id; if (!source) return
  await run(draft, async () => {
    if (props.demo) throw new Error('演示界面不能裁剪音频。')
    const start = Math.round(Number(clipStarts.value[item.id] ?? 0) * 1000)
    const end = Math.round(Number(clipEnds.value[item.id] ?? 5) * 1000)
    const result = await productApi.clipTtsAsset(props.model, source, start, end)
    item.asset_id = result.asset.asset_id; item.reference_text = ''; item.error = '请确认裁剪片段的准确转写。'
    expressionChanged(item)
  })
  if (item.asset_id) await transcribeExpression(draft, item)
}
function stored(draft: VoiceDraft) { return bucket.value.voices.find(voice => voice.id === draft.id) }
function isDefaultReference(draft: VoiceDraft, item: ProductTtsReference) {
  const voice = stored(draft)
  return voice?.is_default && voice.default_reference_registered !== false && voice.default_reference_id === item.id
}
function defaultSelectable(draft: VoiceDraft, item: ProductTtsReference) {
  const saved = stored(draft)?.references?.find(row => row.id === item.id)
  return item.status === 'ready' && saved?.status === 'ready' && saved.asset_id === item.asset_id &&
    saved.reference_text === item.reference_text && saved.reference_language === item.reference_language
}
async function setDefaultReference(draft: VoiceDraft, item: ProductTtsReference | null) {
  const voice = stored(draft); if (!voice) return
  const model = props.model
  await run(draft, async () => {
    if (props.demo) {
      voice.default_reference_registered = item !== null
      if (item?.asset_id) Object.assign(voice, { default_reference_id: item.id, reference_asset_id: item.asset_id,
        reference_text: item.reference_text, reference_language: item.reference_language })
    } else {
      if (!productApi.setDefaultTtsReference) throw new Error('默认参考设置接口尚未连接。')
      await productApi.setDefaultTtsReference(model, voice.id, item?.id ?? null, voice.revision)
    }
    delete state.models[model].drafts[draft.key]
    await refresh(model)
    const updated = state.models[model].drafts[voice.id]
    if (updated) { updated.expanded = true; updated.notice = item ? '已登记为模型默认合成参考，并从情绪候选中排除。' : '已取消默认参考；录音记录保留。' }
    emit('changed')
  })
}
/** voiceMode（音色创建方式）：design（纯设计）、mixed（混合设计）、clone（纯克隆）。 */
function voiceMode(draft: VoiceDraft): 'design' | 'mixed' | 'clone' {
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
async function preview(draft: VoiceDraft, expression?: ProductTtsReference) {
  const model = props.model; const prepared = signature(draft, model)
  await run(draft, async generation => {
    const voice = { name: voiceName(draft), source: 'import', reference_asset_id: expression?.asset_id || draft.source?.asset_id,
      reference_text: expression?.reference_text || draft.transcript, reference_language: expression?.reference_language || draft.referenceLanguage,
      references: draft.references }
    if (props.demo) {
      draft.candidate = { ...voice, source: 'import', reference_asset_id: 'demo-target', reference_text: draft.text, design_text: draft.text, inference_steps: TTS_DESIGN_STEPS, generation_recipe: TTS_DESIGN_RECIPE }
      draft.targetTranscript = draft.text
      draft.signature = prepared; draft.notice = '试音演示完成。'; return
    }
    await props.ensureSupport?.(model)
    if (draft.generation !== generation) return
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
  const keys = new Set<string>()
  for (const item of draft.references) {
    if (!item.name.trim() || ['default', 'base_internal'].includes(item.tag) || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(item.tag)) return '请填写情绪名称及英文小写标记；default 和 base_internal 为系统保留标记。'
    if (!Number.isFinite(Number(item.intensity)) || Number(item.intensity) < 0 || Number(item.intensity) > 1) return '情绪强度必须在 0 到 1 之间。'
    const key = `${item.tag}:${item.reference_language}:${item.intensity}`
    if (keys.has(key)) return '同一情绪标记、语言和强度不能重复。'
    keys.add(key)
  }
  if (unchangedContent(draft)) return ''
  const mode = voiceMode(draft) // mode（当前音色创建方式）。
  if (mode !== 'clone') {
    if (draft.candidate && !draft.targetTranscript.trim()) return '请填写目标样音实际发音的准确转写。'
    if (draft.candidate && draft.signature === signature(draft)) return ''
    return `${mode === 'mixed' ? '混合设计' : '纯音色设计'}需要以当前参数完成一次试音，再保存生成的目标样音。`
  }
  if (!draft.source) return '请添加参考音频进行音色克隆。'
  if (!draft.transcript.trim()) return '请填写参考音频的准确转写文本后保存克隆音色。'
  return ''
}
function saveable(draft: VoiceDraft) { return !saveBlockReason(draft) }
function unchangedContent(draft: VoiceDraft) {
  const voice = stored(draft)
  if (!voice) return false
  const original = voiceDraft(voice)
  return draft.referenceLanguage === original.referenceLanguage && draft.description === original.description && draft.transcript === original.transcript && draft.text === original.text && draft.targetTranscript === original.targetTranscript && draft.source?.asset_id === original.source?.asset_id
}
async function save(draft: VoiceDraft) {
  if (!saveable(draft)) return
  const model = props.model; const name = voiceName(draft)
  await run(draft, async () => {
    const originalVoice = stored(draft)
    const defaultReferenceId = originalVoice?.default_reference_id
    const references = draft.references.map((item): ProductTtsReference => {
      const referenceText = item.id === defaultReferenceId ? draft.transcript : item.reference_text
      const ready = !!item.asset_id && !!referenceText.trim() && !!draft.referenceLanguage
      const transcriptChanged = item.id === defaultReferenceId && referenceText !== item.reference_text
      return {
        ...item,
        reference_text: referenceText,
        reference_language: draft.referenceLanguage,
        status: ready ? 'ready' : 'pending',
        error: ready ? '' : item.error,
        ...(transcriptChanged ? { verification_source: 'manual' } : {}),
      }
    })
    const hasManagedDefaultReference = originalVoice?.default_reference_registered === true || !!defaultReferenceId
    const payload = hasManagedDefaultReference ? {
      ...originalVoice!, name, reference_asset_id: draft.source?.asset_id || originalVoice!.reference_asset_id,
      reference_language: draft.referenceLanguage, reference_text: draft.transcript, references,
    } : unchangedContent(draft) ? { ...originalVoice!, name, references } : voiceMode(draft) !== 'clone' ? { ...draft.candidate, name, reference_text: draft.targetTranscript.trim(), references } : {
      name, source: 'import', reference_asset_id: draft.source!.asset_id,
      reference_language: draft.referenceLanguage, reference_text: draft.transcript, source_reference_text: draft.transcript, references,
    }
    if (props.demo) {
      const original = state.models[model].voices.find(voice => voice.id === draft.id)
      const voice = { ...payload, id: draft.id || draft.key, revision: (draft.revision || 0) + 1, model, enabled: original?.enabled || false, is_default: original?.is_default || false } as ProductTtsVoice
      state.models[model].voices = [...state.models[model].voices.filter(item => item.id !== voice.id), voice]
      draft.id = voice.id; draft.revision = voice.revision; draft.references = references
    } else {
      const response = draft.id ? await productApi.updateTtsVoice(model, draft.id, payload, draft.revision!) : await productApi.createTtsVoice(model, payload)
      draft.id = response.voice.id; draft.revision = response.voice.revision; draft.references = references
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
    <header class="library-heading"><div><h3>音色库</h3><p>音色按基础模型独立管理，启用后用于下一次回答。</p></div><div class="library-actions"><button type="button" class="outline-button" :disabled="demo" @click="emit('modelImport')">模型导入</button><button type="button" class="outline-button" @click="add">＋ 添加音色</button></div></header>
    <p v-if="libraryError" class="instruction-error" role="alert">{{ libraryError }}</p>
    <p v-if="!drafts.length" class="instruction-hint">暂无已登记参考音频。导入模型或添加录音后，可选择默认合成参考。</p>
    <article v-for="draft in drafts" :key="draft.key" class="voice-card" :class="{ enabled: stored(draft)?.enabled }">
      <header class="voice-heading"><div class="voice-name"><input v-if="draft.expanded" :id="`voice-name-${draft.key}`" v-model="draft.name" class="voice-name-input" aria-label="音色名称" maxlength="80" placeholder="音色名称（可选）" :disabled="draft.busy"><strong v-else>{{ stored(draft)?.name || draft.name || '新音色' }}</strong><span v-if="stored(draft)?.is_default && stored(draft)?.default_reference_registered !== false" class="voice-badge">默认</span><small v-if="stored(draft)?.enabled">正在使用</small><small>已就绪 {{ (stored(draft)?.default_reference_registered === undefined && draft.source && draft.transcript.trim() ? 1 : 0) + draft.references.filter(item => item.status === 'ready').length }} · 待完善 {{ draft.references.filter(item => item.status !== 'ready').length + (stored(draft)?.default_reference_registered !== undefined || draft.source && draft.transcript.trim() ? 0 : 1) }}</small></div>
        <div class="voice-actions"><button type="button" :disabled="!draft.id || globalBusy || stored(draft)?.default_reference_registered === false" @click="play(draft)">播放参考</button><button type="button" :aria-expanded="draft.expanded" :aria-controls="`voice-editor-${draft.key}`" @click="edit(draft)">{{ draft.expanded ? '收起' : '编辑' }}</button>
          <label class="voice-toggle"><input type="checkbox" role="switch" :aria-label="`启用 ${draft.name || '音色'}`" :checked="stored(draft)?.enabled" :disabled="!draft.id || globalBusy || stored(draft)?.default_reference_registered === false || (stored(draft)?.is_default && stored(draft)?.enabled)" @change="toggle(draft, ($event.target as HTMLInputElement).checked)"><span aria-hidden="true"></span></label>
        </div></header>
        <p v-if="stored(draft)?.default_reference_registered === false" class="instruction-hint" role="status">模型默认合成参考未登记。请在下方参考音频中选择一段已就绪录音并设为默认。</p>
      <div v-if="draft.expanded" :id="`voice-editor-${draft.key}`" class="voice-editor">
        <div class="base-reference">
          <strong>基础参考</strong>
          <template v-if="stored(draft)?.default_reference_registered !== false">
            <div class="voice-inputs" :class="{ single: model === 'aniflive' }">
              <label class="form-field"><span>参考录音语言</span><select v-model="draft.referenceLanguage" :disabled="globalBusy" @change="inheritReferenceLanguage(draft)"><option value="zh">中文</option><option value="yue">粤语</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option></select></label>
              <label class="form-field"><span>音频转写 <small>准确转写必填</small></span><textarea v-model="draft.transcript" class="compact-textarea" rows="1" placeholder="添加音频后自动识别，也可手动输入并校对。" @input="invalidate(draft)"></textarea></label>
            </div>
            <div class="voice-reference">
              <div v-if="draft.source" class="reference-input">
                <ReferenceAudioPlayer :key="`${model}:${draft.source.asset_id}`" :src="referenceUrl(draft)" :filename="draft.source.filename" :duration-ms="draft.source.duration_ms" :busy="globalBusy" :transcribing="transcribingKey === draft.key" :active="activeReferenceKey === draft.key" :removable="false" :transcribable="false" @play="activeReferenceKey = draft.key" @error="draft.error = $event" />
              </div>
              <div v-else-if="stored(draft)?.default_reference_registered !== true" class="reference-input" @dragover.prevent @drop.prevent="drop(draft, $event)">
                <label class="attachment-upload" :class="{ disabled: globalBusy }">
                  <input type="file" aria-label="添加参考音频" accept=".wav,.mp3,.flac,.ogg" :disabled="globalBusy" @change="fileChange(draft, $event)">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m-4 4 4-4 4 4M5 15v5h14v-5" /></svg>
                  <span class="upload-caption"><strong>添加参考音频</strong><small>点击选择或拖入文件</small></span>
                </label>
              </div>
              <p v-else class="instruction-hint" role="status">模型默认参考音频暂不可用，请在下方选择另一条已就绪参考音频。</p>
              <small class="reference-hint">支持 3–10 秒清晰单声道参考录音，保留原采样率，最大 20 MiB（二进制兆字节）。</small>
            </div>
          </template>
          <p v-else class="instruction-hint" role="status">当前没有模型默认参考音频。请在下方选择一段已就绪参考音频，设为默认后会显示在这里。</p>
        </div>
        <section class="expression-list-window" aria-label="情绪参考">
          <header class="expression-list-heading">
            <strong>情绪参考</strong>
            <div class="expression-list-actions">
              <button type="button" :disabled="globalBusy" @click="addExpression(draft)">＋ 添加情绪参考</button>
              <button type="button" :aria-expanded="expressionListExpanded(draft)" :aria-controls="expressionListId(draft)" @click="toggleExpressionList(draft)">{{ expressionListExpanded(draft) ? '收起' : '展开' }}</button>
            </div>
          </header>
          <div v-show="expressionListExpanded(draft)" :id="expressionListId(draft)" class="expression-list-scroll" tabindex="0" aria-label="情绪参考队列，滚动查看更多条目">
          <article v-for="(item, index) in draft.references" :key="item.id" class="expression-card">
            <div class="expression-summary">
              <button type="button" class="expression-expand" :aria-expanded="expressionExpanded(draft, item)" :aria-controls="expressionEditorId(draft, item)" @click="toggleExpression(draft, item)">
                <svg class="expression-chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m7 4 6 6-6 6" /></svg>
                <span>{{ item.name || '参考音频' }}</span>
              </button>
              <span class="expression-readiness" :class="{ ready: item.status === 'ready' }" role="status"><i aria-hidden="true"></i>{{ item.status === 'ready' ? '已就绪' : '待完善' }}</span>
              <ReferenceAudioPlayer :compact="true" :src="expressionUrl(item)" :filename="item.name + '.wav'" :duration-ms="0" :busy="globalBusy" :transcribing="transcribingKey === item.id" :active="activeReferenceKey === expressionAudioKey(draft, item, 'summary')" @play="activeReferenceKey = expressionAudioKey(draft, item, 'summary')" @error="draft.error = $event" />
              <label class="voice-toggle expression-default-toggle">
                <input type="checkbox" role="switch" :aria-checked="isDefaultReference(draft, item)" :aria-label="isDefaultReference(draft, item) ? `取消${item.name || '参考音频'}的默认参考` : `将${item.name || '参考音频'}设为默认参考`" :checked="isDefaultReference(draft, item)" :disabled="globalBusy || (!isDefaultReference(draft, item) && !defaultSelectable(draft, item))" @change="setDefaultReference(draft, ($event.target as HTMLInputElement).checked ? item : null)"><span aria-hidden="true"></span>
              </label>
            </div>
            <div v-if="expressionExpanded(draft, item)" :id="expressionEditorId(draft, item)" class="expression-editor">
              <p v-if="isDefaultReference(draft, item)" class="instruction-hint">用于模型默认合成，不参与情绪自动选择。</p>
              <p v-else-if="item.status === 'ready' && !defaultSelectable(draft, item)" class="instruction-hint">保存参考资料后可设为默认。</p>
              <div class="expression-field-row">
                <label class="form-field"><span>情绪名称</span><input v-model="item.name" maxlength="80"></label>
                <label class="form-field"><span>情绪标记</span><input v-model="item.tag" maxlength="64" placeholder="例如 soft"></label>
              </div>
              <div class="expression-intensity-text-row">
                <label class="form-field"><span>参考强度 · {{ item.intensity }}</span><input v-model.number="item.intensity" type="range" min="0" max="1" step="0.1"></label>
                <label class="form-field"><span>参考录音转写</span><textarea v-model="item.reference_text" class="compact-textarea" rows="1" @input="item.verification_source = 'manual'; expressionChanged(item)"></textarea></label>
              </div>
              <ReferenceAudioPlayer v-if="item.asset_id || item.source_asset_id" :src="expressionUrl(item)" :filename="item.name + '.wav'" :duration-ms="0" :busy="globalBusy" :transcribing="transcribingKey === item.id" :active="activeReferenceKey === expressionAudioKey(draft, item, 'detail')" @play="activeReferenceKey = expressionAudioKey(draft, item, 'detail')" @transcribe="transcribeExpression(draft, item)" @remove="item.asset_id = null; item.source_asset_id = null; expressionChanged(item)" @error="draft.error = $event" />
              <label v-if="!item.asset_id && !item.source_asset_id" class="form-field"><span>添加参考录音（3–10 秒）</span><input type="file" accept=".wav,.mp3,.flac,.ogg" :disabled="globalBusy" @change="uploadExpression(draft, item, $event)"></label>
              <div v-if="item.source_asset_id" class="expression-clip"><label>起点（秒）<input v-model.number="clipStarts[item.id]" type="number" min="0" step="0.1" placeholder="0"></label><label>终点（秒）<input v-model.number="clipEnds[item.id]" type="number" min="0" step="0.1" placeholder="5"></label><button type="button" :disabled="globalBusy" @click="clipExpression(draft, item)">裁剪参考</button></div>
              <p v-if="item.error" class="instruction-hint">{{ item.error }}</p>
              <button type="button" class="danger-action" :disabled="globalBusy || isDefaultReference(draft, item)" @click="draft.references.splice(index, 1)">删除参考音频</button>
            </div>
          </article>
          </div>
        </section>
        <div class="voice-footer"><label class="form-field"><span>试音正文</span><textarea v-model="draft.text" class="compact-textarea" rows="1" maxlength="1000" @input="invalidate(draft)"></textarea></label><div class="voice-footer-actions"><button type="button" :disabled="unavailable || globalBusy || (!draft.description.trim() && !draft.source)" @click="preview(draft)">合成试音</button><button type="button" class="save-button" :disabled="globalBusy || !saveable(draft)" @click="save(draft)">保存</button><button type="button" class="danger-action" :disabled="globalBusy || stored(draft)?.is_default" @click="remove(draft)">{{ draft.id ? '删除' : '取消添加' }}</button></div></div>
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
.library-actions { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.library-heading > div { min-width: 0; }.library-heading > button { flex-shrink: 0; white-space: nowrap; }
.library-actions > button { flex-shrink: 0; white-space: nowrap; }
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
.base-reference { display: grid; gap: 12px; }
.expression-editor { display: grid; gap: 12px; }
.expression-list-window { display: grid; gap: 12px; min-width: 0; min-height: 0; padding: 14px; overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: #ffffff02; }
.expression-list-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; min-width: 0; }
.expression-list-heading > strong { font-size: var(--font-body); font-weight: 600; }
.expression-list-actions { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.expression-list-actions > button { white-space: nowrap; }
.expression-list-scroll { display: flex; flex-direction: column; gap: 12px; max-height: 46.67vh; min-width: 0; min-height: 0; overflow-y: auto; overscroll-behavior-y: auto; scrollbar-gutter: stable; padding: 12px 4px 0 0; border-top: 1px solid var(--line); }
.expression-card { flex: 0 0 auto; min-width: 0; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.expression-summary { display: flex; align-items: center; gap: 12px; min-height: 48px; padding: 7px 12px; }
.expression-expand { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; min-height: 30px; padding: 3px 2px; border: 0; background: transparent; color: inherit; text-align: left; }
.expression-expand:hover { color: var(--accent); }
.expression-expand:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.expression-expand span { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.expression-chevron { width: 12px; height: 12px; flex: 0 0 12px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; transition: transform 140ms; }
.expression-expand[aria-expanded="true"] .expression-chevron { transform: rotate(90deg); }
.expression-readiness { display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0; color: var(--muted); font-size: .7rem; white-space: nowrap; }
.expression-readiness i { width: 6px; height: 6px; border-radius: 50%; background: var(--line-strong); }
.expression-readiness.ready { color: var(--mint); }.expression-readiness.ready i { background: var(--mint); }
.expression-default-toggle { flex-shrink: 0; padding: 4px 2px; }
.expression-editor { padding: 12px; border-top: 1px solid var(--line); }
.expression-field-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.expression-field-row .form-field { min-width: 0; }
.expression-intensity-text-row { display: grid; grid-template-columns: minmax(160px, 1fr) minmax(0, 2fr); align-items: end; gap: 12px; }
.expression-intensity-text-row .form-field { min-width: 0; }
.expression-clip { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; }
.expression-clip label { display: grid; gap: 4px; }
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
@container voice-library (max-width: 480px) { .voice-heading { flex-wrap: wrap; }.voice-actions { margin-left: auto; }.voice-footer { grid-template-columns: 1fr; }.voice-footer-actions { justify-content: flex-end; }.expression-list-heading { align-items: stretch; }.expression-list-actions { width: 100%; }.expression-list-actions > button { flex: 1; }.expression-summary { gap: 8px; padding-inline: 9px; }.expression-field-row,.expression-intensity-text-row { grid-template-columns: 1fr; } }
</style>
