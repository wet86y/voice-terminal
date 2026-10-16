import { inject, provide, reactive, type InjectionKey } from 'vue'
import type { ProductLocalTtsModelId, ProductTtsAsset, ProductTtsVoice } from './tts-voice-types'

export const TTS_SAMPLE_TEXT = '你好，很高兴认识你。现在开始测试这个音色，我会用自然的语气清楚地说话。'
export const TTS_DESIGN_STEPS = 30 // TTS_DESIGN_STEPS（样音制作步数）：独立于正式播报设置。
export const TTS_DESIGN_RECIPE = 'voxcpm2-full-decode-v2' // TTS_DESIGN_RECIPE（样音生成配方版本）：混合制作使用隔离音色参考。
/** VoiceDraft（音色草稿）：模型、卡片、异步代次与素材独立保存。 */
export interface VoiceDraft {
  key: string; id?: string; revision?: number; name: string // 草稿键、音色标识、旧版本、名称。
  description: string; transcript: string; text: string // 设计描述、源校对文本、试音正文。
  targetTranscript: string // targetTranscript（目标样音实际转写）：可独立校对，不改变制作正文。
  source: (ProductTtsAsset & { filename: string }) | null // 参考音频附件。
  candidate: Partial<ProductTtsVoice> | null; signature: string // 生成候选及输入签名。
  expanded: boolean; busy: boolean; generation: number; operationId?: string // 展开、任务状态、代次、操作标识。
  error: string; notice: string // 当前卡片的错误和反馈。
}
export interface TtsVoiceState {
  inferenceSteps: Record<ProductLocalTtsModelId, number> // 各模型当前推理步数。
  models: Record<ProductLocalTtsModelId, { voices: ProductTtsVoice[]; drafts: Record<string, VoiceDraft> }> // 两套音色与草稿。
}
const key: InjectionKey<TtsVoiceState> = Symbol('tts-voices')
export function createTtsVoiceState(): TtsVoiceState {
  return reactive({ inferenceSteps: { voxcpm1: 10, voxcpm2: 20 }, models: { voxcpm1: { voices: [], drafts: {} }, voxcpm2: { voices: [], drafts: {} } } })
}
export function provideTtsVoiceState() { const state = createTtsVoiceState(); provide(key, state); return state }
export function useTtsVoiceState() { return inject(key, undefined) ?? createTtsVoiceState() }
export function voiceDraft(voice?: ProductTtsVoice): VoiceDraft {
  const source = voice?.source_reference_asset_id || (voice?.source !== 'design' ? voice?.reference_asset_id : undefined)
  return {
    key: voice?.id || crypto.randomUUID(), id: voice?.id, revision: voice?.revision,
    name: voice?.name || '', description: voice?.design_description || '',
    transcript: voice?.source_reference_text || (voice?.source !== 'design' ? voice?.reference_text || '' : ''),
    text: voice?.design_text || TTS_SAMPLE_TEXT,
    targetTranscript: voice?.source === 'design' ? voice.reference_text : '',
    source: source ? { asset_id: source, duration_ms: voice?.source_reference_duration_ms || voice?.duration_ms || 0, filename: '参考音频.wav' } : null,
    candidate: voice?.source === 'design' ? { ...voice } : null, signature: '',
    expanded: false, busy: false, generation: 0, error: '', notice: '',
  }
}
