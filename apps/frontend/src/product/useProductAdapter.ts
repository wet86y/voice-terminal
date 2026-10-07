import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useVoiceTerminal } from '../composables/useVoiceTerminal'
import { useApplicationHost } from './application-host'
import type { ConversationEntry, ConversationEntryKind, ReasoningMode, ShadowProductSnapshot, TurnDelivery } from '../types'
import { productApi, ProductApiError } from './api'
import type {
  CustomInstruction,
  DshRuntimeStatus,
  ProductAssignments,
  ProductComputeStatus,
  ProductLocalModelId,
  ProductModel,
  ProductModelRegistry,
  ProductMutationResult,
  ProductRuntimeState,
  ProductSettingsSnapshot,
  ProductSnapshot,
  WakeTask,
} from './types'
import { isRecord } from '../record'
import { desktopPort } from './desktop-port'
import { DEFAULT_SETTINGS, DEFAULT_REGISTRY, DEFAULT_DSH, MAX_WAKE_KEYWORDS, defaultCurrent, defaultShadow, normalizeSettings, normalizeRegistry, normalizeRuntimeState, updateSettingsFromResponse } from './normalization'
export { MAX_WAKE_KEYWORDS } from './normalization'

export type ProductSettingsTab = 'wake' | 'persona' | 'silence' | 'speaker' | 'wakes' | 'models' | 'compute' | 'system'
export type ProductPage = 'home' | 'settings'
export type AssignmentSlot = 'voice' | 'shadow' | 'status'

const NOTICE_DISPLAY_MS = 5000
const NOTICE_FADE_MS = 420

export interface ProductUiModel extends ProductModel {
  provider: string
  modelId: string
  path: string
  status: string
  maxContextTokens?: number
  allowedReasoning?: string[]
}

export interface ProviderFormState {
  name: string
  baseUrl: string
  protocol: 'openai_compatible' | 'deepseek' | 'opencode_go' | 'responses' | 'minimax' | 'manual'
  apiKey: string
  customModelId: string
  maxContextTokens: number
  allowedReasoning: string[]
  voiceId: string
}

/** One rendered rail line, projected from the server-ordered conversation. */
export interface ConversationRow {
  key: string
  role: 'user' | 'assistant'
  kind: ConversationEntryKind
  text: string
  at: string
  status: string
  termination: string
  delivery: TurnDelivery
  speaking: boolean
}

/** Speech that is queued or playing but has not settled into the log yet. */
export interface PendingSpeechRow {
  key: string
  conversationId: string
  speechId: string
  turnId: string
  text: string
  state: 'waiting' | 'playing'
  speaking: boolean
}

/** One wake word, up to five accepted spellings; the server enforces the same cap. */

function messageFromError(error: unknown) {
  if (error instanceof ProductApiError) return error.message
  if (error instanceof Error) return error.message
  return '产品请求失败。'
}


export function useProductAdapter(options: { webSocketUrl?: string } = {}) {
  const application = useApplicationHost()
  const voice = useVoiceTerminal(options)
  const page = ref<ProductPage>('home')
  const settingsTab = ref<ProductSettingsTab>('wake')
  const snapshot = ref<ProductSnapshot | null>(null)
  const settings = reactive<ProductSettingsSnapshot>(structuredClone(DEFAULT_SETTINGS))
  const computeStatus = ref<ProductComputeStatus | null>(null)
  const computeSavedDevices = ref(structuredClone(DEFAULT_SETTINGS.compute.devices)) // computeSavedDevices（已持久化设备偏好）。
  const registry = ref<ProductModelRegistry>(structuredClone(DEFAULT_REGISTRY))
  let registryRevision = 0 // registryRevision（注册表响应代次）：旧快照不能覆盖已经完成的扫描或保存。
  const initialComputeDevices = structuredClone(DEFAULT_SETTINGS.compute.devices)
  const dsh = ref<DshRuntimeStatus>(structuredClone(DEFAULT_DSH))
  const wakeTasks = ref<WakeTask[]>([])
  let adapterDisposed = false // adapterDisposed（适配器已释放）：迟到的注册响应必须清理服务端会话。
  let enrollmentGeneration = 0 // enrollmentGeneration（注册代次）：取消后不接纳旧请求的响应。
  onBeforeUnmount(() => { adapterDisposed = true; void cancelSpeakerEnrollment({ notify: false }) })
  const editingWakeTaskId = ref('')
  const addingWakeTask = ref(false) // addingWakeTask（手动创建表单是否展开）。
  const wakeTaskCreateDraft = reactive({
    ruleKind: 'at' as 'at' | 'daily' | 'weekly', // ruleKind（创建排期规则）。
    when: '', // when（本地单次日期时间）。
    time: '', // time（每天或每周的时间）。
    weekdays: '', // weekdays（周一至周日的 1–7 集合）。
    timeZone: '', // timeZone（系统当前时区）。
    text: '', // text（任务说明），没有冻结参考。
  })
  const wakeTaskDraft = reactive({
    /** target（目标）: 唤醒哪一侧 Agent. */
    target: 'voice_llm' as WakeTask['target'],
    /** when（时间）: 项目本地时间. */
    when: '',
    /** repeat（间隔）: 固定循环间隔，空值表示一次性. */
    repeat: '',
    ruleKind: 'at' as NonNullable<WakeTask['rule_kind']>,
    ruleText: '',
    timeZone: 'Asia/Hong_Kong',
    weekdays: '',
    /** text（任务说明）: 触发时提供给 Agent 的方向. */
    text: '',
  })
  const loading = ref(false)
  const saving = ref(false)
  const lastError = ref('')
  const notice = ref('')
  const noticeVisible = ref(false)
  const personaDocument = ref<keyof ProductSettingsSnapshot['persona']>('personality')
  const customInstructions = ref<CustomInstruction[]>([])
  const silenceSeconds = ref(30)
  const quietHoursForm = reactive({ enabled: false, start: '23:00', end: '07:00' })
  const modelScanState = ref<'idle' | 'scanning' | 'complete'>('idle')
  const providerStep = ref<'idle' | 'discovering' | 'select-model' | 'custom-id' | 'verified'>('idle')
  const speakerForm = reactive({ mode: 'off', confirm_threshold: 0.5, continuity_threshold: 0.4, owner_only_barge_in: false })
  const speakerStatus = ref<Record<string, unknown>>({})
  const speakerProfile = ref<Record<string, unknown>>({})
  const speakerTestResult = ref<Record<string, unknown> | null>(null)
  const ambientContext = ref<Record<string, unknown>>({ records: [] })
  const speakerEnrollment = ref<{
    id: string
    segments: number[]
    /** One distinct sentence per segment, shown so the user does not repeat one line. */
    prompts: string[]
    required: number
    guidance: string
  } | null>(null)
  const discoveredModels = ref<string[]>([])
  const restartState = ref<'idle' | 'restarting' | 'complete' | 'error'>('idle')
  const applyImpact = ref<string[]>([])

  const wakeForm = reactive({ enabled: false, keywords: ['hei claw'] as string[], sensitivity: 75 })
  const personaDocuments = reactive<Record<keyof ProductSettingsSnapshot['persona'], string>>({
    personality: '', user: '',
  })
  const providerForm = reactive<ProviderFormState>({
    name: '', baseUrl: 'https://api.example.com/v1', protocol: 'openai_compatible',
    apiKey: '', customModelId: '', maxContextTokens: 131072,
    allowedReasoning: ['off', 'low'], voiceId: 'female-shaonv',
  })
  // The template can toggle policy chips directly.  Keep the request
  // contract valid even when the user removes the last visible chip.
  watch(
    () => providerForm.allowedReasoning.slice(),
    values => {
      if (!values.length) providerForm.allowedReasoning = ['off']
    },
    { flush: 'sync' },
  )
  const modelAssignments = reactive<Record<AssignmentSlot, string>>({
    voice: '', shadow: '', status: '',
  })

  watch([page, settingsTab], ([currentPage, tab]) => {
    if (computeStatusTimer !== undefined) window.clearInterval(computeStatusTimer)
    computeStatusTimer = undefined
    if (speakerStatusTimer !== undefined) window.clearInterval(speakerStatusTimer)
    speakerStatusTimer = undefined
    if (currentPage === 'settings' && tab === 'compute') {
      void refreshComputeStatus()
      computeStatusTimer = window.setInterval(() => void refreshComputeStatus(), 5000)
    }
    if (currentPage === 'settings' && tab === 'speaker') {
      void refreshSpeakerStatus()
      speakerStatusTimer = window.setInterval(() => void refreshSpeakerStatus(), 3000)
    }
  })

  let snapshotRequest: Promise<void> | null = null
  let dshRevision = 0 // dshRevision（宿主状态修订号）：阻止迟到的启动快照覆盖新状态。
  let reconnectTimer: number | undefined
  let dshStatusTimer: number | undefined
  let computeStatusTimer: number | undefined
  let speakerStatusTimer: number | undefined
  let noticeDisplayTimer: ReturnType<typeof setTimeout> | undefined
  let noticeFadeTimer: ReturnType<typeof setTimeout> | undefined
  let mutationQueue: Promise<unknown> = Promise.resolve()

  function enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const next = mutationQueue.then(operation, operation)
    mutationQueue = next.then(() => undefined, () => undefined)
    return next
  }

  function clearNoticeTimers() {
    if (noticeDisplayTimer !== undefined) clearTimeout(noticeDisplayTimer)
    if (noticeFadeTimer !== undefined) clearTimeout(noticeFadeTimer)
    noticeDisplayTimer = undefined
    noticeFadeTimer = undefined
  }

  function setNotice(value: string) {
    clearNoticeTimers()
    lastError.value = ''
    if (!value) {
      notice.value = ''
      noticeVisible.value = false
      return
    }
    notice.value = value
    noticeVisible.value = true
    noticeDisplayTimer = setTimeout(() => {
      noticeDisplayTimer = undefined
      noticeVisible.value = false
      noticeFadeTimer = setTimeout(() => {
        notice.value = ''
        noticeFadeTimer = undefined
      }, NOTICE_FADE_MS)
    }, NOTICE_DISPLAY_MS)
  }

  function setError(error: unknown) {
    clearNoticeTimers()
    lastError.value = messageFromError(error)
    notice.value = ''
    noticeVisible.value = false
  }

  function applySnapshot(value: ProductSnapshot, requestedRegistryRevision: number, requestedDshRevision: number) {
    // 重连快照更新服务端基线，正在编辑的表单仍由根层持有。
    const known = snapshot.value !== null
    const differs = (draft: unknown, saved: unknown) => JSON.stringify(draft) !== JSON.stringify(saved)
    const wakeDirty = differs(wakeForm, { enabled: settings.wake.enabled, keywords: settings.wake.keywords, sensitivity: settings.wake.sensitivity })
    const silenceDirty = silenceSeconds.value !== settings.silence.seconds
    const quietDirty = differs(quietHoursForm, settings.quiet_hours)
    const savedSpeaker = Object.fromEntries(Object.keys(speakerForm).map(key => [key, settings.speaker[key as keyof typeof settings.speaker]]))
    const speakerDirty = differs(speakerForm, savedSpeaker)
    const personaDirty = differs(personaDocuments, settings.persona)
    const computeDirty = differs(settings.compute.devices, known ? computeSavedDevices.value : initialComputeDevices)
    const assignmentDirty = differs(modelAssignments, {
      voice: registry.value.assignments.voice_llm_model_id || '', shadow: registry.value.assignments.shadow_llm_model_id || '', status: registry.value.assignments.status_llm_model_id || '',
    })
    snapshot.value = value
    if (isRecord(value.dsh) && requestedDshRevision === dshRevision) {
      const state = ['disabled', 'starting', 'ready', 'unavailable', 'exited'].includes(String(value.dsh.state))
        ? value.dsh.state as DshRuntimeStatus['state'] : 'unavailable'
      applyDshStatus({
        enabled: value.dsh.enabled === true,
        state,
        version: typeof value.dsh.version === 'string' ? value.dsh.version : null,
        profile: 'web',
        ui_origin: typeof value.dsh.ui_origin === 'string' ? value.dsh.ui_origin : null,
        message: typeof value.dsh.message === 'string' ? value.dsh.message : '',
      })
      scheduleDshStatusPoll()
    }
    const nextSettings = normalizeSettings(value.settings)
    computeSavedDevices.value = { ...nextSettings.compute.devices }
    Object.assign(settings.wake, nextSettings.wake)
    Object.assign(settings.silence, nextSettings.silence)
    settings.quiet_hours = nextSettings.quiet_hours
    if (!quietDirty) Object.assign(quietHoursForm, nextSettings.quiet_hours)
    Object.assign(settings.shadow, nextSettings.shadow)
    Object.assign(settings.speaker, nextSettings.speaker)
    if (!computeDirty) Object.assign(settings.compute.devices, nextSettings.compute.devices)
    Object.assign(settings.compute.fallback_devices || {}, nextSettings.compute.fallback_devices || {})
    Object.assign(settings.tts, nextSettings.tts)
    Object.assign(settings.persona, nextSettings.persona)
    const hasPendingInstructionEdits = JSON.stringify(customInstructions.value) !== JSON.stringify(settings.custom_instructions)
    settings.custom_instructions = nextSettings.custom_instructions.map(item => ({ ...item }))
    if (!hasPendingInstructionEdits) {
      customInstructions.value = nextSettings.custom_instructions.map(item => ({ ...item }))
    }
    settings.schema_version = nextSettings.schema_version
    settings.warnings = nextSettings.warnings
    if (!wakeDirty) Object.assign(wakeForm, {
      enabled: nextSettings.wake.enabled,
      keywords: [...nextSettings.wake.keywords],
      sensitivity: nextSettings.wake.sensitivity,
    })
    if (!silenceDirty) silenceSeconds.value = nextSettings.silence.seconds
    if (!speakerDirty) Object.assign(speakerForm, nextSettings.speaker)
    if (!personaDirty) Object.assign(personaDocuments, nextSettings.persona)
    if (registryRevision === requestedRegistryRevision) {
      registry.value = normalizeRegistry(value.models)
      if (!assignmentDirty) syncAssignments(registry.value.assignments)
    }
  }

  function applyRegistry(value: unknown) {
    const pending = JSON.stringify(modelAssignments) !== JSON.stringify({ voice: registry.value.assignments.voice_llm_model_id || '', shadow: registry.value.assignments.shadow_llm_model_id || '', status: registry.value.assignments.status_llm_model_id || '' })
    registry.value = normalizeRegistry(value)
    registryRevision += 1
    if (!pending) syncAssignments(registry.value.assignments)
  }

  function syncAssignments(assignments: ProductAssignments) {
    const values: Record<AssignmentSlot, unknown> = {
      voice: assignments.voice_llm_model_id,
      shadow: assignments.shadow_llm_model_id,
      status: assignments.status_llm_model_id,
    }
    for (const slot of Object.keys(values) as AssignmentSlot[]) {
      modelAssignments[slot] = typeof values[slot] === 'string' ? values[slot] : ''
    }
  }

  async function loadSnapshot() {
    if (!application.businessReady.value) return
    if (snapshotRequest) return snapshotRequest
    loading.value = true
    const requestedRegistryRevision = registryRevision
    const requestedDshRevision = dshRevision
    snapshotRequest = productApi.snapshot().then(async value => {
      if (adapterDisposed) return
      applySnapshot(value, requestedRegistryRevision, requestedDshRevision)
      await Promise.all([refreshSpeaker(), refreshWakeTasks(), refreshComputeStatus()])
      lastError.value = ''
    }).catch(setError).finally(() => {
      loading.value = false
      snapshotRequest = null
    })
    return snapshotRequest
  }

  async function refreshComputeStatus(refreshCapabilities = false) {
    if (!application.businessReady.value) return
    try {
      computeStatus.value = await productApi.computeStatus(refreshCapabilities)
    } catch (error) {
      setError(error)
    }
  }

  async function saveComputeSettings() {
    await enqueue(async () => {
      saving.value = true
      try {
        const submitted = { ...settings.compute.devices }
        const result = await productApi.saveCompute({ devices: submitted })
        const newerDraft = JSON.stringify(settings.compute.devices) !== JSON.stringify(submitted) ? { ...settings.compute.devices } : null
        updateSettingsFromResponse(settings, result)
        computeSavedDevices.value = { ...settings.compute.devices }
        if (newerDraft) Object.assign(settings.compute.devices, newerDraft)
        setNotice(result.message || (result.restart_required
          ? '本地模型运行设备设置已保存，重新启动应用后生效。'
          : '本地模型运行设备设置已保存。'))
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function refreshWakeTasks() {
    if (!application.businessReady.value) return
    try {
      const result = await productApi.wakeTasks()
      wakeTasks.value = Array.isArray(result.tasks) ? result.tasks : []
    } catch (error) { setError(error) }
  }

  function editWakeTask(task: WakeTask) {
    if (task.status !== 'pending') return
    editingWakeTaskId.value = task.task_id
    addingWakeTask.value = false
    wakeTaskDraft.target = task.target
    if (task.when) {
      const date = new Date(task.when)
      const pad = (value: number) => String(value).padStart(2, '0')
      wakeTaskDraft.when = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    } else wakeTaskDraft.when = ''
    wakeTaskDraft.repeat = task.repeat || ''
    wakeTaskDraft.text = task.text
    wakeTaskDraft.ruleKind = task.rule_kind || (task.repeat ? 'every' : 'at')
    const rule = task.rule || {}
    wakeTaskDraft.ruleText = String(rule.expression || rule.time || wakeTaskDraft.when.slice(11, 16) || '09:00')
    wakeTaskDraft.timeZone = String(rule.timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone)
    wakeTaskDraft.weekdays = Array.isArray(rule.weekdays) ? rule.weekdays.join(',') : ''
  }

  async function saveWakeTask() {
    if (!editingWakeTaskId.value) return
    saving.value = true
    try {
      const calendar = ['daily', 'weekly', 'cron'].includes(wakeTaskDraft.ruleKind)
      const change = wakeTaskDraft.ruleKind === 'daily'
        ? { daily: { time: scheduleTime(wakeTaskDraft.ruleText), time_zone: wakeTaskDraft.timeZone } }
        : wakeTaskDraft.ruleKind === 'weekly'
          ? { weekly: { time: scheduleTime(wakeTaskDraft.ruleText), time_zone: wakeTaskDraft.timeZone,
            weekdays: wakeTaskDraft.weekdays.split(',').map(value => Number(value.trim())) } }
          : { cron: { expression: wakeTaskDraft.ruleText, time_zone: wakeTaskDraft.timeZone } }
      await productApi.updateWakeTask(editingWakeTaskId.value, {
        target: wakeTaskDraft.target, text: wakeTaskDraft.text,
        ...(calendar ? { change } : { when: wakeTaskDraft.when, repeat: wakeTaskDraft.ruleKind === 'every' ? wakeTaskDraft.repeat : '' }),
      })
      editingWakeTaskId.value = ''
      await refreshWakeTasks()
      setNotice('唤醒任务已更新。')
    } catch (error) { setError(error) }
    finally { saving.value = false }
  }

  // scheduleTime（调度时间）：官方接口要求秒数，界面分钟值补零，已有秒数保留。
  function scheduleTime(value: string) { return /^\d{2}:\d{2}$/.test(value) ? `${value}:00` : value }

  function addWakeTask() {
    if (saving.value || addingWakeTask.value) return
    const date = new Date(Date.now() + 3600000)
    const pad = (value: number) => String(value).padStart(2, '0')
    const when = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
    Object.assign(wakeTaskCreateDraft, { ruleKind: 'at', when, time: when.slice(11), weekdays: '', timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone, text: '' })
    editingWakeTaskId.value = ''
    addingWakeTask.value = true
  }

  async function createWakeTask() {
    if (saving.value || !addingWakeTask.value) return
    const draft = { ...wakeTaskCreateDraft }
    const text = draft.text.trim()
    const weekdays = draft.weekdays.split(',').filter(Boolean).map(Number)
    if (!text || text.length > 500 || (draft.ruleKind === 'weekly' && (!weekdays.length || weekdays.some(day => !Number.isInteger(day) || day < 1 || day > 7))) || (draft.ruleKind !== 'at' && !/^\d{2}:\d{2}(?::\d{2})?$/.test(draft.time))) {
      setError(new Error('请填写有效的任务说明、触发时间与星期。'))
      return
    }
    const date = new Date(draft.when)
    if (draft.ruleKind === 'at' && (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now())) {
      setError(new Error('单次触发时间必须晚于当前时间。'))
      return
    }
    saving.value = true
    try {
      await productApi.createWakeTask({ text,
        ...(draft.ruleKind === 'at' ? { at: date.toISOString() }
          : draft.ruleKind === 'daily' ? { daily: { time: scheduleTime(draft.time), time_zone: draft.timeZone } }
          : { weekly: { time: scheduleTime(draft.time), time_zone: draft.timeZone, weekdays } }),
      })
      addingWakeTask.value = false
      await refreshWakeTasks()
      setNotice('任务已保存到 DSH（代理执行宿主），到期进入当时的后台主线。')
    } catch (error) { setError(error) }
    finally { saving.value = false }
  }

  async function removeWakeTask(task: WakeTask) {
    if (saving.value) return
    saving.value = true
    try {
      if (task.status === 'pending') await productApi.cancelWakeTask(task.task_id)
      else {
        const result = await productApi.deleteWakeTask(task.task_id)
        if (result.deleted !== true) throw new Error('归档删除未成功，请刷新后重试。')
      }
      wakeTasks.value = wakeTasks.value.filter(item => item.task_id !== task.task_id)
      if (editingWakeTaskId.value === task.task_id) editingWakeTaskId.value = ''
      await refreshWakeTasks()
      setNotice(task.status === 'pending' ? '唤醒任务已取消，记录保存在归档中。' : '归档任务已删除。')
    } catch (error) { setError(error) }
    finally { saving.value = false }
  }

  watch(settingsTab, value => {
    if (value === 'wakes') void refreshWakeTasks()
  })

  async function openDshConsole() {
    const desktop = desktopPort()
    if (!desktop.available) {
      setError(new Error('DSH 控制台只能由 Voice Terminal 桌面应用打开。'))
      return
    }
    try {
      const result = await productApi.launchDsh()
      const launchUrl = new URL(result.url)
      if (!dsh.value.ui_origin || launchUrl.origin !== dsh.value.ui_origin || launchUrl.pathname !== '/' || !launchUrl.searchParams.has('token')) {
        throw new Error('Voice Server 返回了无效的 DSH 地址。')
      }
      await desktop.openConsole(result.url)
    } catch (error) {
      setError(error)
      void productApi.dshStatus().then(value => { if (!adapterDisposed) applyDshStatus(value) }).catch(() => undefined)
    }
  }

  function applyDshStatus(value: DshRuntimeStatus) {
    dshRevision += 1
    dsh.value = value
  }

  function scheduleDshStatusPoll() {
    if (dshStatusTimer !== undefined) window.clearInterval(dshStatusTimer)
    dshStatusTimer = undefined
    if (dsh.value.state !== 'starting') return
    dshStatusTimer = window.setInterval(() => {
      void productApi.dshStatus().then(value => {
        if (adapterDisposed) return
        const wasStarting = dsh.value.state === 'starting'
        applyDshStatus(value)
        if (value.state !== 'starting') {
          if (dshStatusTimer !== undefined) window.clearInterval(dshStatusTimer)
          dshStatusTimer = undefined
          if (wasStarting && (value.state === 'unavailable' || value.state === 'exited')) {
            setError(new Error(value.message || 'DSH 控制台不可用；Voice Terminal 仍可正常使用。'))
          }
        }
      }).catch(() => undefined)
    }, 1000)
  }

  async function refreshSpeaker() {
    if (!application.businessReady.value) return
    try {
      const [status, profile, ambient] = await Promise.all([
        productApi.speakerStatus(), productApi.speakerProfile(), productApi.ambientContext(),
      ])
      speakerStatus.value = status
      speakerProfile.value = profile
      ambientContext.value = ambient
    } catch (error) {
      // The sidecar is optional; do not make the main product snapshot fail.
      speakerStatus.value = { service: 'UNAVAILABLE', last_error: messageFromError(error) }
    }
  }

  async function refreshSpeakerStatus() {
    if (!application.businessReady.value) return
    try {
      speakerStatus.value = await productApi.speakerStatus()
    } catch (error) {
      speakerStatus.value = { service: 'UNAVAILABLE', last_error: messageFromError(error) }
    }
  }

  async function saveSpeaker() {
    await enqueue(async () => {
      saving.value = true
      try {
        const submitted = { ...speakerForm }
        const result = await productApi.saveSpeaker(submitted)
        const changed = JSON.stringify(speakerForm) !== JSON.stringify(submitted)
        updateSettingsFromResponse(settings, result)
        if (!changed) Object.assign(speakerForm, settings.speaker)
        await refreshSpeaker()
        setNotice(result.message || '声纹设置已保存。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function startSpeakerEnrollment() {
    if (!application.businessReady.value) {
      setError(new Error('语音服务尚未就绪，不能开始声纹录音。'))
      return
    }
    const generation = ++enrollmentGeneration
    try {
      const value = await productApi.startSpeakerEnrollment()
      const id = value.enrollment_id
      if (typeof id !== 'string') throw new Error('后端未返回录音会话。')
      if (adapterDisposed || generation !== enrollmentGeneration) {
        await productApi.cancelSpeakerEnrollment(id).catch(() => {})
        return
      }
      const prompts = Array.isArray(value.prompts)
        ? (value.prompts as Array<Record<string, unknown>>)
            .map(item => String(item?.text || '')).filter(text => text.length > 0)
        : []
      const required = typeof value.required_segments === 'number' && value.required_segments > 0
        ? value.required_segments
        : 3
      speakerEnrollment.value = {
        id,
        segments: [],
        prompts,
        required,
        guidance: String(value.guidance || ''),
      }
      setNotice(`录音会话已开始，请依次朗读 ${required} 段不同的句子（每段 5–10 秒）。`)
    } catch (error) { if (!adapterDisposed && generation === enrollmentGeneration) setError(error) }
  }

  async function cancelSpeakerEnrollment(options: unknown = {}) {
    enrollmentGeneration += 1
    const enrollment = speakerEnrollment.value
    speakerEnrollment.value = null
    if (enrollment) {
      try {
        await productApi.cancelSpeakerEnrollment(enrollment.id)
      } catch {
        // The local UI is already reset.  If the server is offline, its
        // short-lived enrollment session still expires and is never written
        // to disk; do not turn a best-effort cleanup into a blocking error.
      }
    }
    const notify = !isRecord(options) || options.notify !== false
    if (notify) {
      setNotice('已取消本次录音会话；未提交的原始录音不会保留。')
    }
  }

  /**
   * Record one enrollment segment on the server, from the same device and
   * processing chain the live session uses.  Browser capture stays available
   * as a fallback via `addSpeakerEnrollmentSegment`.
   */
  async function captureSpeakerEnrollmentSegment(index: number, label: string, seconds: number, signal?: AbortSignal, captureId?: string) {
    const enrollment = speakerEnrollment.value
    if (!enrollment) return null
    try {
      const result = await productApi.captureSpeakerEnrollmentSegment(enrollment.id, {
        index, label, seconds, ...(captureId ? { capture_id: captureId } : {}),
      }, signal)
      if (adapterDisposed || signal?.aborted || speakerEnrollment.value?.id !== enrollment.id) return null
      if (!enrollment.segments.includes(index)) enrollment.segments.push(index)
      const quality = isRecord(result.quality) ? result.quality : {}
      const speech = Number(quality.speech_ms || 0) / 1000
      setNotice(`第 ${index + 1} 段已录制：有效人声 ${speech.toFixed(1)} 秒。`)
      return true
    } catch (error) {
      if (adapterDisposed || signal?.aborted) return null
      const detail = isRecord(error) ? error : {}
      // No server-side capture device: let the caller fall back to browser
      // recording rather than blocking enrollment entirely.
      if (String(detail.code || '').includes('SPEAKER_CAPTURE_UNAVAILABLE')) return false
      setError(error)
      return null
    }
  }

  async function captureSpeakerTest(seconds: number, signal?: AbortSignal, captureId?: string) {
    try {
      const result = await productApi.captureSpeakerTest({ seconds, ...(captureId ? { capture_id: captureId } : {}) }, signal)
      if (adapterDisposed || signal?.aborted) return null
      speakerTestResult.value = result
      setNotice('现场声纹测试已完成。')
      return true
    } catch (error) {
      if (adapterDisposed || signal?.aborted) return null
      const detail = isRecord(error) ? error : {}
      if (String(detail.code || '').includes('SPEAKER_CAPTURE_UNAVAILABLE')) return false
      setError(error)
      return null
    }
  }

  async function addSpeakerEnrollmentSegment(index: number, label: string, pcmBase64: string) {
    const enrollment = speakerEnrollment.value
    if (!enrollment) return
    try {
      await productApi.addSpeakerEnrollmentSegment(enrollment.id, {
        index, label, pcm_base64: pcmBase64,
      })
      if (adapterDisposed || speakerEnrollment.value?.id !== enrollment.id) return
      if (!enrollment.segments.includes(index)) enrollment.segments.push(index)
      setNotice(`第 ${index + 1} 段录音已接收。`)
    } catch (error) { setError(error) }
  }

  async function commitSpeakerEnrollment() {
    if (saving.value) return
    const enrollment = speakerEnrollment.value
    const required = enrollment?.required && enrollment.required > 0 ? enrollment.required : 3
    if (!enrollment || enrollment.segments.length !== required) {
      setError(new Error(`请先完成${required}段录音。`))
      return
    }
    saving.value = true
    try {
      const result = await productApi.commitSpeakerEnrollment(enrollment.id)
      if (adapterDisposed || speakerEnrollment.value?.id !== enrollment.id) return
      speakerEnrollment.value = null
      await refreshSpeaker()
      const detail = isRecord(result.enrollment) ? result.enrollment : {}
      const weakest = Number(detail.min_similarity)
      const suffix = Number.isFinite(weakest) ? `三段最低相似度 ${weakest.toFixed(2)}。` : ''
      setNotice(`声纹资料已更新；原始录音已删除。${suffix}`)
    } catch (error) { if (!adapterDisposed && speakerEnrollment.value?.id === enrollment.id) setError(error) }
    finally { saving.value = false }
  }

  async function deleteSpeakerTemplate(id: string) {
    try {
      const result = await productApi.deleteSpeakerTemplate(id)
      if (typeof result.downgraded_to === 'string') {
        // Profile loss disables filtering while preserving the independent
        // owner-only toggle so the UI can show that it is selected but inert.
        speakerForm.mode = result.downgraded_to
        settings.speaker.mode = result.downgraded_to as typeof settings.speaker.mode
      }
      await refreshSpeaker()
      setNotice(typeof result.downgraded_to === 'string'
        ? '主人声纹资料已删除；依赖声纹的能力当前不可用，主人打断保持选中但不会执行。'
        : '声纹模板已删除。')
    } catch (error) { setError(error) }
  }

  async function setSpeakerTemplateCentroid(id: string, include: boolean) {
    try {
      await productApi.setSpeakerTemplateCentroid(id, include)
      await refreshSpeaker()
      setNotice(include ? '模板已纳入主声纹中心。' : '模板已从主声纹中心移除。')
    } catch (error) { setError(error) }
  }

  async function testSpeakerAudio(pcmBase64: string) {
    const generation = enrollmentGeneration
    try {
      const result = await productApi.testSpeaker({ pcm_base64: pcmBase64 })
      if (adapterDisposed || generation !== enrollmentGeneration) return
      speakerTestResult.value = result
      setNotice('现场声纹测试已完成。')
    } catch (error) { setError(error) }
  }

  async function refreshAmbientContext() {
    try { ambientContext.value = await productApi.ambientContext() } catch (error) { setError(error) }
  }

  async function clearAmbientContext() {
    try { ambientContext.value = await productApi.clearAmbientContext(); setNotice('环境记录已清空。') } catch (error) { setError(error) }
  }

  async function refreshRegistry() {
    try {
      applyRegistry(await productApi.registry())
      lastError.value = ''
    } catch (error) {
      setError(error)
    }
  }

  const connected = computed(() => voice.connection.value === 'connected')
  const coreUnavailable = computed(() => application.managed.value && application.businessReady.value && !application.snapshot.value?.core_available)
  // 输入与输出独立运行；实际播放可早于输入结束判定完成。打断及错误仍优先呈现。
  const runtimeState = computed<ProductRuntimeState>(() => coreUnavailable.value && connected.value ? 'error'
    : connected.value && voice.listening.value && !['ERROR', 'INTERRUPTING'].includes(voice.state.value)
      && voice.activeSpeech.value?.state === 'playing' ? 'speaking'
      : normalizeRuntimeState(voice.state.value, connected.value))
  const runtimeSource = computed(() => snapshot.value?.runtime)
  const quietStatus = computed(() => connected.value ? voice.quietHours : runtimeSource.value?.quiet_hours)
  const current = computed(() => connected.value
    ? voice.current
    : snapshot.value?.conversation.current || defaultCurrent())
  const conversation = computed(() => connected.value ? voice.recentTurns.value : snapshot.value?.conversation.recent || [])

  // The rail renders the server-ordered conversation log directly.  Ordering is
  // the server's ``seq``, so a system-owned line (the KWS wake acknowledgement)
  // keeps the place it really happened in and a reload restores the same view
  // instead of rebuilding the chronology from three unrelated projections.  The
  // offline bootstrap exposes the same projection from the product API.
  const railEntries = computed<ConversationEntry[]>(() => connected.value
    ? voice.conversationEntries.value
    : snapshot.value?.conversation.entries || [])

  const conversationRows = computed<ConversationRow[]>(() => {
    const playingKeys = new Set(voice.playbackQueue.value
      .filter(item => item.state === 'playing')
      .map(item => `${item.conversation_id}\u0000${item.speech_id}`))
    // empty_input（空输入）：只过滤产品气泡，诊断记录与 seq（服务端顺序号）保留。
    return railEntries.value
      .filter(entry => !(entry.kind === 'user_turn' && entry.status === 'empty_input' && !entry.text.trim()))
      .map(entry => ({
        key: `${entry.kind === 'user_turn' ? 'turn' : 'speech'}:${entry.conversation_id}:${entry.turn_id || entry.speech_id}:${entry.seq}`,
        role: entry.kind === 'user_turn' ? 'user' as const : 'assistant' as const,
        kind: entry.kind,
        text: entry.text,
        at: entry.at,
        status: entry.status,
        termination: entry.termination,
        delivery: entry.delivery,
        speaking: entry.speech_id ? playingKeys.has(`${entry.conversation_id}\u0000${entry.speech_id}`) : false,
      }))
  })

  // Speech that is queued or playing but has not settled into the log yet.  Its
  // identity is the same ``conversation_id + speech_id``, so a transaction moves
  // from this tail into the ordered log exactly once.
  const pendingSpeechRows = computed<PendingSpeechRow[]>(() => {
    const settled = new Set(railEntries.value
      .map(entry => `${entry.conversation_id}\u0000${entry.speech_id}`))
    return voice.playbackQueue.value
      .filter(item => typeof item.text === 'string' && item.text.trim().length > 0)
      .filter(item => !settled.has(`${item.conversation_id}\u0000${item.speech_id}`))
      .map(item => ({
        key: `pending:${item.conversation_id}:${item.speech_id}`,
        conversationId: item.conversation_id,
        speechId: item.speech_id,
        turnId: item.turn_id,
        text: item.text,
        state: item.state,
        speaking: item.state === 'playing',
      }))
  })

  const currentTurnId = computed(() => voice.currentTurnId.value)
  const currentTurnLogged = computed(() => {
    const turnId = currentTurnId.value
    return Boolean(turnId) && railEntries.value.some(entry => entry.turn_id === turnId)
  })
  const currentTurnSpeechLogged = computed(() => {
    const turnId = currentTurnId.value
    return Boolean(turnId) && railEntries.value
      .some(entry => entry.turn_id === turnId && entry.kind !== 'user_turn')
  })
  const currentTurnSpeechPending = computed(() => {
    const turnId = currentTurnId.value
    return Boolean(turnId) && pendingSpeechRows.value.some(row => row.turnId === turnId)
  })
  const shadow = computed<ShadowProductSnapshot>(() => connected.value ? voice.shadowProduct.value : snapshot.value?.shadow || defaultShadow())
  const uiModels = computed<ProductUiModel[]>(() => {
    const providers = new Map(registry.value.providers.map(provider => [provider.id, provider]))
    return registry.value.models.map(item => {
      const provider = providers.get(item.provider_id)
      return {
        ...item,
        provider: provider?.name || item.origin || '未知供应商',
        modelId: item.model_id,
        path: item.local_path || '',
        status: item.verification === 'verified' ? 'verified' : item.verification === 'inherited' ? 'inherited' : item.health || item.verification || 'needs_check',
        maxContextTokens: item.max_context_tokens,
        allowedReasoning: item.allowed_reasoning,
      }
    })
  })
  const availableLlmModels = computed(() => uiModels.value.filter(item =>
    item.capability === 'llm' && !item.hidden && ['verified', 'inherited'].includes(item.verification),
  ))
  const availableTtsModels = computed(() => uiModels.value.filter(item =>
    item.capability === 'tts' && !item.hidden && ['verified', 'inherited'].includes(item.verification),
  ))
  const visibleLocalPool = computed(() => uiModels.value.filter(item => item.capability !== 'llm' && item.capability !== 'tts' && !item.hidden))
  const hiddenPool = computed(() => uiModels.value.filter(item => item.hidden))
  const managedApiModelPool = computed(() => uiModels.value.filter(item => item.origin === 'product_managed' && (item.capability === 'llm' || item.capability === 'tts') && !item.hidden))
  const selectedModels = computed(() => ({
    voice: uiModels.value.find(item => item.id === modelAssignments.voice),
    shadow: uiModels.value.find(item => item.id === modelAssignments.shadow),
    status: uiModels.value.find(item => item.id === modelAssignments.status),
  }))
  const activeModels = computed(() => {
    const committed = registry.value.assignments
    const runtimeLlm = uiModels.value.filter(item => item.origin === 'runtime_config' && item.capability === 'llm')
    const committedOrRuntime = (key: 'voice_llm_model_id' | 'shadow_llm_model_id' | 'status_llm_model_id', fallbackIndex: number) =>
      uiModels.value.find(item => item.id === committed[key]) || runtimeLlm[fallbackIndex]
    return {
      voice: committedOrRuntime('voice_llm_model_id', 0),
      shadow: committedOrRuntime('shadow_llm_model_id', 1),
      status: committedOrRuntime('status_llm_model_id', 2),
      tts: registry.value.tts_profiles.find(item => item.id === committed.tts_profile_id)
        || registry.value.tts_profiles.find(item => item.origin === 'runtime_config'),
    }
  })
  const activeTtsModel = computed<ProductUiModel | undefined>(() => {
    const profile = activeModels.value.tts
    return profile ? uiModels.value.find(item => item.id === profile.model_id) : undefined
  })

  const latencyMs = computed(() => {
    if (connected.value) {
      // 首音延迟由浏览器本地计时；收到首个真实播放事件后停止计时。
      return voice.perceptualLatencyMs.value
    }
    const values = runtimeSource.value?.latency?.values
    if (!values) return null
    const candidates = [values.speech_end_to_playback_ms, values.agent_request_to_speak_ms, values.turn_total_ms]
    return candidates.find(value => typeof value === 'number' && Number.isFinite(value)) ?? null
  })
  const latencyRunning = computed(() => connected.value && voice.perceptualLatencyRunning.value)

  function runtimeAction() {
    if (!application.businessReady.value) return false
    if (!connected.value) {
      voice.connect()
      void loadSnapshot()
      return false
    }
    if (coreUnavailable.value) {
      setError(new Error('语音能力尚未就绪，请先完成模型配置或查看本地组件。'))
      return false
    }
    return voice.toggleListening()
  }

  function resetSession() {
    if (!connected.value) return false
    return voice.resetSession()
  }

  function setReasoningMode(mode: ReasoningMode) {
    if (!connected.value) return false
    return voice.setReasoningMode(mode)
  }

  function toggleProviderReasoning(value: string) {
    const current = providerForm.allowedReasoning
    if (current.includes(value)) {
      if (current.length === 1) return
      providerForm.allowedReasoning = current.filter(item => item !== value)
      return
    }
    providerForm.allowedReasoning = [...current, value]
  }

  function addWakeKeyword() {
    if (wakeForm.keywords.length >= MAX_WAKE_KEYWORDS) return
    wakeForm.keywords.push('')
  }

  function removeWakeKeyword(index: number) {
    // One phrase is mandatory: the wake word must always exist.
    if (wakeForm.keywords.length <= 1) return
    wakeForm.keywords.splice(index, 1)
  }

  async function saveWake() {
    await enqueue(async () => {
      saving.value = true
      try {
        const keywords = wakeForm.keywords.map(item => item.trim()).filter(Boolean)
        if (!keywords.length) {
          setError('请至少填写 1 个唤醒词。')
          return
        }
        const result = await productApi.saveWake({
          enabled: wakeForm.enabled,
          keywords,
          sensitivity: wakeForm.sensitivity,
        })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || 'KWS 设置已保存，将在下一次会话生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function saveSilence() {
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.saveSilence({ seconds: silenceSeconds.value })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || '静默设置已保存，将在下一次 Episode 生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function saveQuietHours() {
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.saveQuietHours({ ...quietHoursForm })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || '被动静默时间已即时生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function saveShadowGate() {
    const desired = settings.shadow.experimental_context_gate_enabled
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.saveShadow({ experimental_context_gate_enabled: desired })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || 'Shadow 上下文门禁设置已立即生效。')
      } catch (error) {
        settings.shadow.experimental_context_gate_enabled = !desired
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function savePersona() {
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.savePersona({ ...personaDocuments })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || 'Persona 已保存，将在下一次 Episode 生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function saveCustomInstructions() {
    await enqueue(async () => {
      saving.value = true
      try {
        const submitted = customInstructions.value.map(item => ({ ...item }))
        const result = await productApi.saveCustomInstructions({ custom_instructions: submitted })
        const changed = JSON.stringify(customInstructions.value) !== JSON.stringify(submitted)
        updateSettingsFromResponse(settings, result)
        if (!changed) customInstructions.value = settings.custom_instructions.map(item => ({ ...item }))
        setNotice(result.message || '自定义指令已保存，将在下一次会话或重置后生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function scanModels() {
    await enqueue(async () => {
      modelScanState.value = 'scanning'
      try {
        applyRegistry(await productApi.scanModels())
        modelScanState.value = 'complete'
        setNotice('已完成真实扫描：现有配置和受管模型目录已同步。')
      } catch (error) {
        modelScanState.value = 'idle'
        setError(error)
      }
    })
  }

  function providerPayload(modelId?: string) {
    const isTts = providerForm.protocol === 'minimax'
    const payload: Record<string, unknown> = {
      name: providerForm.name,
      base_url: providerForm.baseUrl,
      protocol: providerForm.protocol,
      capability: isTts ? 'tts' : 'llm',
      api_key: providerForm.apiKey,
      ...(modelId ? { model_id: modelId } : {}),
    }
    if (!isTts) {
      payload.model_policy = {
        max_context_tokens: providerForm.maxContextTokens,
        allowed_reasoning: [...providerForm.allowedReasoning],
      }
    } else {
      payload.voice_id = providerForm.voiceId
      payload.output_format = 'pcm'
      payload.transport = 'websocket_bidi'
      payload.scheduling = 'direct'
      payload.tts_profile = {
        voice_id: providerForm.voiceId,
        output_format: 'pcm',
        transport: 'websocket_bidi',
        scheduling: 'direct',
      }
    }
    return payload
  }

  function requireProviderKey() {
    if (providerForm.apiKey.trim()) return true
    setError(new Error('请输入 API Key；它只会保留到当前一次真实请求结束。'))
    return false
  }

  async function discoverProvider() {
    if (!requireProviderKey()) return
    providerStep.value = 'discovering'
    discoveredModels.value = []
    try {
      const result = await productApi.validateProvider(providerPayload())
      const discovered = Array.isArray(result.discovered_models)
        ? result.discovered_models.filter(item => typeof item === 'string') as string[]
        : []
      discoveredModels.value = discovered
      if (discovered.length) {
        providerStep.value = 'select-model'
        setNotice('已发现模型列表；请再次输入 API Key，再选择一个模型执行最小真实能力测试。')
      } else if (result.requires_custom_model_id) {
        providerStep.value = 'custom-id'
        setNotice('该服务未提供模型列表；请再次输入 API Key，填写模型 ID 后执行最小能力测试。')
      } else if (result.ok === true && Array.isArray(result.models) && result.models.length) {
        providerForm.customModelId = String(result.models[0])
        providerStep.value = 'verified'
      } else {
        providerStep.value = 'idle'
        setError(new Error(typeof result.message === 'string' ? result.message : '供应商发现失败。'))
      }
    } catch (error) {
      providerStep.value = 'idle'
      setError(error)
    } finally {
      // Discovery is one complete credential use.  The next validation step
      // deliberately asks for the key again instead of retaining it in the
      // adapter between requests.
      providerForm.apiKey = ''
    }
  }

  async function verifyCustomModel(modelId = providerForm.customModelId) {
    const selected = modelId.trim()
    if (!selected) {
      providerForm.apiKey = ''
      setError(new Error('请输入模型 ID 后再执行最小能力测试。'))
      return
    }
    if (!requireProviderKey()) return
    providerStep.value = 'discovering'
    try {
      const result = await productApi.validateProvider(providerPayload(selected))
      if (result.ok === true && Array.isArray(result.models) && result.models.includes(selected)) {
        providerForm.customModelId = selected
        providerStep.value = 'verified'
        setNotice('已通过最小真实能力测试，可以加入模型池。')
      } else {
        providerStep.value = 'custom-id'
        setError(new Error(typeof result.message === 'string' ? result.message : '模型最小能力测试未通过。'))
      }
    } catch (error) {
      providerStep.value = 'custom-id'
      setError(error)
    } finally {
      providerForm.apiKey = ''
    }
  }

  async function addVerifiedProvider() {
    const modelId = providerForm.customModelId.trim()
    if (providerStep.value !== 'verified' || !modelId) {
      setError(new Error('请先完成模型发现和最小真实能力测试。'))
      return
    }
    if (!requireProviderKey()) return
    await enqueue(async () => {
      try {
        const result = await productApi.addProvider(providerPayload(modelId))
        applyRegistry(result)
        providerForm.apiKey = ''
        providerForm.customModelId = ''
        providerStep.value = 'idle'
        setNotice('验证成功，模型已加入产品模型池。')
      } catch (error) {
        setError(error)
      } finally {
        // The key is never retained after either outcome.
        providerForm.apiKey = ''
      }
    })
  }

  async function updateModel(model: ProductUiModel, changes: Record<string, unknown>) {
    return await enqueue(async () => {
      try {
        applyRegistry(await productApi.updateModel(model.id, changes))
        setNotice(`${model.provider} · ${model.name} 的模型策略已保存。`)
        return true
      } catch (error) {
        setError(error)
        return false
      }
    })
  }

  async function deleteModel(model: ProductUiModel) {
    return await enqueue(async () => {
      try {
        applyRegistry(await productApi.deleteModel(model.id))
        setNotice(`${model.provider} · ${model.name} 已从模型池删除。`)
        return true
      } catch (error) {
        setError(error)
        return false
      }
    })
  }

  async function hideModel(model: ProductUiModel) {
    await enqueue(async () => {
      try {
        applyRegistry(await productApi.hideModel(model.id))
        setNotice(`${model.provider} · ${model.name} 已隐藏。`)
      } catch (error) {
        setError(error)
      }
    })
  }

  async function restoreModel(model: ProductUiModel) {
    await enqueue(async () => {
      try {
        applyRegistry(await productApi.restoreModel(model.id))
        setNotice(`${model.provider} · ${model.name} 已恢复。`)
      } catch (error) {
        setError(error)
      }
    })
  }

  function assignmentPayload() {
    return {
      voice_llm_model_id: modelAssignments.voice || null,
      shadow_llm_model_id: modelAssignments.shadow || null,
      status_llm_model_id: modelAssignments.status || null,
    }
  }

  // pendingApply（待确认分配）：确认时提交用户已看过影响说明的同一组模型。
  let pendingApply: ReturnType<typeof assignmentPayload> | null = null
  async function requestModelApply() {
    const proposed = assignmentPayload()
    pendingApply = null
    try {
      const result = await enqueue(() => productApi.applyAssignments(proposed, false))
      pendingApply = proposed
      applyImpact.value = Array.isArray(result.impact) ? result.impact.filter(item => typeof item === 'string') : []
      return result
    } catch (error) {
      setError(error)
      return null
    }
  }

  async function confirmModelApply() {
    const proposed = pendingApply
    if (!proposed) return null
    restartState.value = 'restarting'
    try {
      const result = await enqueue(() => productApi.applyAssignments(proposed, true))
      if (result.applied === true) {
        const submittedStillCurrent = JSON.stringify(assignmentPayload()) === JSON.stringify(proposed)
        const newerDraft = { ...modelAssignments }
        pendingApply = null
        restartState.value = 'complete'
        if (isRecord(result.assignments)) applyRegistry({ ...registry.value, assignments: result.assignments })
        if (!submittedStillCurrent) Object.assign(modelAssignments, newerDraft)
        setNotice(typeof result.message === 'string' ? result.message : '模型服务已重启，新的配置已生效。')
        await loadSnapshot()
      } else {
        restartState.value = 'error'
        setError(new Error(typeof result.message === 'string' ? result.message : '模型服务重启失败，已保留上一份有效配置。'))
      }
      return result
    } catch (error) {
      restartState.value = 'error'
      setError(error)
      return null
    }
  }

  function retry() {
    if (!application.businessReady.value) return
    if (!connected.value) voice.connect()
    void loadSnapshot()
  }

  function connect() {
    if (!application.businessReady.value) return
    voice.connect()
    void loadSnapshot()
  }

  watch(voice.connection, value => {
    if (value === 'connected') {
      void loadSnapshot()
    }
    if (value === 'offline' && application.businessReady.value) {
      setError(new Error(voice.connectionError.value || 'Voice Server 连接已关闭。'))
    }
  })

  onMounted(() => {
    connect()
    reconnectTimer = window.setInterval(() => {
      if (application.businessReady.value && voice.connection.value === 'offline') voice.connect()
    }, 3000)
  })

  watch(application.businessReady, (ready, wasReady) => {
    if (ready) connect()
    if (!ready) {
      if (wasReady) voice.disconnect()
      lastError.value = ''
    }
  }, { flush: 'sync' })

  watch(() => application.snapshot.value?.components?.find(row => row.id === 'dsh')?.state, state => {
    // 宿主晚于语音接口启动：组件就绪／重试时同步状态，不让初始 unavailable（暂不可用）停留到重载。
    if (!state || !application.businessReady.value) return
    void productApi.dshStatus().then(value => {
      if (adapterDisposed) return
      applyDshStatus(value)
      scheduleDshStatusPoll()
    }).catch(() => undefined)
  })

  onBeforeUnmount(() => {
    if (reconnectTimer !== undefined) window.clearInterval(reconnectTimer)
    if (dshStatusTimer !== undefined) window.clearInterval(dshStatusTimer)
    if (computeStatusTimer !== undefined) window.clearInterval(computeStatusTimer)
    if (speakerStatusTimer !== undefined) window.clearInterval(speakerStatusTimer)
    clearNoticeTimers()
    voice.dispose()
  })

  return {
    voice,
    page,
    settingsTab,
    snapshot,
    settings,
    computeStatus,
    computeSavedDevices,
    registry,
    dsh,
    wakeTasks,
    addingWakeTask,
    wakeTaskCreateDraft,
    addWakeTask,
    createWakeTask,
    editingWakeTaskId,
    wakeTaskDraft,
    loading,
    saving,
    connected,
    runtimeState,
    coreUnavailable,
    current,
    conversation,
    conversationRows,
    pendingSpeechRows,
    currentTurnId,
    currentTurnLogged,
    currentTurnSpeechLogged,
    currentTurnSpeechPending,
    shadow,
    latencyMs,
    latencyRunning,
    wakeForm,
    maxWakeKeywords: MAX_WAKE_KEYWORDS,
    addWakeKeyword,
    removeWakeKeyword,
    silenceSeconds,
    personaDocument,
    personaDocuments,
    customInstructions,
    speakerForm,
    speakerStatus,
    speakerProfile,
    speakerTestResult,
    ambientContext,
    speakerEnrollment,
    providerForm,
    providerStep,
    discoveredModels,
    modelAssignments,
    uiModels,
    availableLlmModels,
    availableTtsModels,
    visibleLocalPool,
    hiddenPool,
    managedApiModelPool,
    selectedModels,
    activeModels,
    activeTtsModel,
    modelScanState,
    restartState,
    applyImpact,
    lastError,
    notice,
    noticeVisible,
    loadSnapshot,
    openDshConsole,
    refreshWakeTasks,
    editWakeTask,
    saveWakeTask,
    removeWakeTask,
    refreshRegistry,
    runtimeAction,
    resetSession,
    setReasoningMode,
    toggleProviderReasoning,
    saveWake,
    saveSilence,
    quietHoursForm,
    quietStatus,
    saveQuietHours,
    saveShadowGate,
    savePersona,
    saveCustomInstructions,
    saveSpeaker,
    refreshComputeStatus,
    saveComputeSettings,
    refreshSpeaker,
    startSpeakerEnrollment,
    cancelSpeakerEnrollment,
    addSpeakerEnrollmentSegment,
    commitSpeakerEnrollment,
    deleteSpeakerTemplate,
    setSpeakerTemplateCentroid,
    testSpeakerAudio,
    captureSpeakerEnrollmentSegment,
    captureSpeakerTest,
    stopSpeakerCapture: productApi.stopSpeakerCapture,
    refreshAmbientContext,
    clearAmbientContext,
    scanModels,
    discoverProvider,
    verifyCustomModel,
    addVerifiedProvider,
    updateModel,
    deleteModel,
    hideModel,
    restoreModel,
    requestModelApply,
    confirmModelApply,
    retry,
    setNotice,
    setError,
  }
}
