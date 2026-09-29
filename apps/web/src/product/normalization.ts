import type { ShadowProductSnapshot } from '../types'
import type { CustomInstruction, DshRuntimeStatus, ProductAssignments, ProductLocalModelId, ProductModelRegistry, ProductMutationResult, ProductRuntimeState, ProductSettingsSnapshot } from './types'
import { isRecord } from '../record'

const SHADOW_REASONING_LEVELS = ['off', 'low', 'medium', 'high', 'max']

export const MAX_WAKE_KEYWORDS = 5

export const DEFAULT_SETTINGS: ProductSettingsSnapshot = {
  schema_version: 7,
  tts: { backend: 'minimax' },
  wake: { enabled: false, keyword: 'hei claw', keyword_label: '', keywords: ['hei claw'], sensitivity: 75 },
  silence: { seconds: 30, smart_turn_hard_silence_ms: 1400 },
  shadow: { experimental_context_gate_enabled: false },
  speaker: { mode: 'off', confirm_threshold: 0.5, continuity_threshold: 0.4 },
  compute: {
    devices: {
      paraformer: 'auto', sensevoice: 'cpu', voice_vad: 'cpu',
      kws: 'cpu', smart_turn: 'cpu', wespeaker: 'cpu',
    },
    fallback_devices: {
      paraformer: 'cpu', sensevoice: 'cpu', voice_vad: 'cpu',
      kws: 'cpu', smart_turn: 'cpu', wespeaker: 'cpu',
    },
  },
  custom_instructions: [],
  persona: { personality: '', user: '' },
  warnings: [],
}

export const DEFAULT_REGISTRY: ProductModelRegistry = {
  providers: [], models: [], tts_profiles: [], assignments: {},
}

export const DEFAULT_DSH: DshRuntimeStatus = {
  enabled: false, state: 'disabled', version: null, profile: 'web', ui_origin: null, message: '',
}

export function defaultCurrent() {
  return {
    partial_text: '', final_text: '', assistant_text: '', tts_text: '', tts_status: '',
  }
}

export function defaultShadow(): ShadowProductSnapshot {
  return {
    available: false, active: false, phase: 'unavailable', user_input_required: false,
    focus: '', summary: '', timeline: [], activities: [], context: {}, provider: {},
  }
}

export function normalizeReasoningEffort(value: unknown, fallback = 'off') {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : ''
  return normalized === 'none' ? 'off' : SHADOW_REASONING_LEVELS.includes(normalized) ? normalized : fallback
}

export function normalizeSettings(value: unknown): ProductSettingsSnapshot {
  if (!isRecord(value)) return structuredClone(DEFAULT_SETTINGS)
  const wake = isRecord(value.wake) ? value.wake : {}
  const silence = isRecord(value.silence) ? value.silence : {}
  const shadow = isRecord(value.shadow) ? value.shadow : {}
  const tts = isRecord(value.tts) ? value.tts : {}
  const speaker = isRecord(value.speaker) ? value.speaker : {}
  const compute = isRecord(value.compute) ? value.compute : {}
  const computeDevices = isRecord(compute.devices) ? compute.devices : {}
  const fallbackDevices = isRecord(compute.fallback_devices) ? compute.fallback_devices : {}
  const persona = isRecord(value.persona) ? value.persona : {}
  const customInstructions = Array.isArray(value.custom_instructions)
    ? value.custom_instructions.filter((item): item is CustomInstruction => isRecord(item)
      && typeof item.id === 'string' && typeof item.trigger === 'string'
      && (item.target === 'voice' || item.target === 'shadow')
      && typeof item.instruction === 'string' && typeof item.enabled === 'boolean')
    : []
  const text = (candidate: unknown) => typeof candidate === 'string' ? candidate : ''
  const wakeKeywords = Array.isArray(wake.keywords)
    ? wake.keywords.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
      .slice(0, MAX_WAKE_KEYWORDS)
    : []
  const primaryKeyword = text(wake.keyword) || DEFAULT_SETTINGS.wake.keyword
  return {
    schema_version: typeof value.schema_version === 'number' ? value.schema_version : DEFAULT_SETTINGS.schema_version,
    tts: { backend: 'minimax' },
    wake: {
      enabled: wake.enabled === true,
      keyword: primaryKeyword,
      keyword_label: text(wake.keyword_label),
      // An older backend only sends `keyword`; keep the list usable either way.
      keywords: wakeKeywords.length ? wakeKeywords : [primaryKeyword],
      sensitivity: typeof wake.sensitivity === 'number' ? wake.sensitivity : DEFAULT_SETTINGS.wake.sensitivity,
    },
    silence: {
      seconds: typeof silence.seconds === 'number' ? silence.seconds : 30,
      smart_turn_hard_silence_ms: typeof silence.smart_turn_hard_silence_ms === 'number'
        ? silence.smart_turn_hard_silence_ms : 1400,
    },
    shadow: {
      experimental_context_gate_enabled: shadow.experimental_context_gate_enabled === true,
    },
    custom_instructions: customInstructions.map(item => ({ ...item })),
    speaker: {
      mode: typeof speaker.mode === 'string' && ['off', 'observe', 'filter'].includes(speaker.mode)
        ? speaker.mode as ProductSettingsSnapshot['speaker']['mode'] : 'off',
      confirm_threshold: typeof speaker.confirm_threshold === 'number' ? speaker.confirm_threshold : 0.5,
      continuity_threshold: typeof speaker.continuity_threshold === 'number' ? speaker.continuity_threshold : 0.4,
    },
    compute: {
      devices: Object.fromEntries(
        Object.keys(DEFAULT_SETTINGS.compute.devices).map(key => {
          const candidate = computeDevices[key]
          const fallback = DEFAULT_SETTINGS.compute.devices[key as ProductLocalModelId]
          return [key, ['auto', 'cpu', 'cuda'].includes(String(candidate)) ? candidate : fallback]
        }),
      ) as ProductSettingsSnapshot['compute']['devices'],
      fallback_devices: Object.fromEntries(
        Object.keys(DEFAULT_SETTINGS.compute.fallback_devices || {}).map(key => [
          key,
          fallbackDevices[key] === 'cuda' ? 'cuda' : 'cpu',
        ]),
      ) as NonNullable<ProductSettingsSnapshot['compute']['fallback_devices']>,
    },
    persona: {
      personality: text(persona.personality),
      user: text(persona.user),
    },
    warnings: Array.isArray(value.warnings) ? value.warnings.filter(item => typeof item === 'string') : [],
  }
}

export function normalizeRegistry(value: unknown): ProductModelRegistry {
  if (!isRecord(value)) return structuredClone(DEFAULT_REGISTRY)
  const models = Array.isArray(value.models) ? value.models.filter(isRecord).map(item => ({
    ...item,
    allowed_reasoning: Array.isArray(item.allowed_reasoning)
      ? [...new Set(item.allowed_reasoning.map(level => normalizeReasoningEffort(level)).filter(level => level))]
      : item.allowed_reasoning,
  })) : []
  return {
    credential_vault: isRecord(value.credential_vault)
      ? { available: value.credential_vault.available === true, diagnostic: typeof value.credential_vault.diagnostic === 'string' ? value.credential_vault.diagnostic : null }
      : undefined,
    providers: Array.isArray(value.providers) ? value.providers.filter(isRecord) as ProductModelRegistry['providers'] : [],
    models: models as ProductModelRegistry['models'],
    tts_profiles: Array.isArray(value.tts_profiles) ? value.tts_profiles.filter(isRecord) as ProductModelRegistry['tts_profiles'] : [],
    assignments: isRecord(value.assignments) ? value.assignments as ProductAssignments : {},
  }
}

export function normalizeRuntimeState(value: string | undefined, connected: boolean): ProductRuntimeState {
  if (!connected) return 'offline'
  if (value === 'ERROR') return 'error'
  if (value === 'LISTENING' || value === 'FINALIZING' || value === 'INTERRUPTING') return 'listening'
  if (value === 'THINKING') return 'thinking'
  if (value === 'SPEAKING') return 'speaking'
  return 'standby'
}
export function updateSettingsFromResponse(settings: ProductSettingsSnapshot, result: ProductMutationResult) {
  const candidate: unknown = result.settings
  if (!isRecord(candidate)) return
  if (isRecord(candidate.wake)) Object.assign(settings.wake, candidate.wake)
  if (isRecord(candidate.silence)) Object.assign(settings.silence, candidate.silence)
  if (isRecord(candidate.shadow)) Object.assign(settings.shadow, candidate.shadow)
  if (isRecord(candidate.speaker)) Object.assign(settings.speaker, candidate.speaker)
  if (isRecord(candidate.compute)) {
    if (isRecord(candidate.compute.devices)) Object.assign(settings.compute.devices, candidate.compute.devices)
    if (isRecord(candidate.compute.fallback_devices)) Object.assign(settings.compute.fallback_devices || {}, candidate.compute.fallback_devices)
  }
  if (isRecord(candidate.tts)) Object.assign(settings.tts, candidate.tts)
  if (isRecord(candidate.persona)) Object.assign(settings.persona, candidate.persona)
  if (Array.isArray(candidate.custom_instructions)) {
    settings.custom_instructions = normalizeSettings({ custom_instructions: candidate.custom_instructions }).custom_instructions
  }
  if (Array.isArray(candidate.warnings)) settings.warnings = candidate.warnings.filter((item): item is string => typeof item === 'string')
}
