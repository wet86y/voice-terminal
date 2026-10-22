import { computed, ref } from 'vue'
import { locateNativeInvoker } from '../runtime/desktop-preflight'
import { startupWasSkipped, rememberStartupSkip } from '../runtime/startup-choice'
import { desktopPort } from './desktop-port'
import type { RuntimeOperation } from '../runtime/runtime-operations'

/** DesktopRecovery（原生恢复快照）：独立于可能离线的网页宿主。 */
export interface DesktopRecovery {
  revision: number // revision（窗口内恢复状态版本）。
  suspended: boolean // suspended（业务宿主离线期间暂停网络连接）。
  operation: RuntimeOperation | null // operation（复用现有热切换状态卡）。
}

export interface ApplicationComponent {
  /** id（组件标识）。 */
  id: string
  /** name（中文名称）。 */
  name: string
  /** state（运行状态）。 */
  state: string
  /** phase（当前阶段）。 */
  phase: string
  /** error（诊断错误）。 */
  error: string
  /** pid（进程标识）。 */
  pid: number | null
  /** created_at（进程创建时间，秒）。 */
  created_at: number | null
  /** uptime_seconds（运行时长，秒）。 */
  uptime_seconds: number | null
  /** memory_rss_bytes（进程常驻工作集，字节，包含共享页）。 */
  memory_rss_bytes: number | null
  /** memory_private_commit_bytes（进程私有提交内存，字节）；旧宿主可能不提供。 */
  memory_private_commit_bytes?: number | null
  /** gpu_dedicated_bytes（进程专用显存字节）：计数器不可用时为空。 */
  gpu_dedicated_bytes?: number | null
  /** thread_count（进程当前线程数）；旧宿主可省略。 */
  thread_count?: number | null
  /** cpu_percent（中央处理器占用百分比）。 */
  cpu_percent: number | null
  /** sampled_at（指标采样时间，秒）。 */
  sampled_at: number | null
  /** shared_with（共享进程的组件标识）。 */
  shared_with: string | null
  /** retry_allowed（允许重试）。 */
  retry_allowed: boolean
  /** retrying（正在重试）。 */
  retrying: boolean
  /** external（外部进程，不负责回收）。 */
  external: boolean
}

export interface ApplicationCapability {
  /** id（能力标识）、label（显示名称）与 state（服务状态）。 */
  id: string
  label: string
  state: string
  /** blocking（阻塞能力）：未就绪时不能开始语音会话。 */
  blocking: boolean
  /** phase（阶段说明）、error（失败原因）与 retry（可单独恢复时的能力标识）。 */
  phase: string
  error: string
  retry: string | null
}

export interface ApplicationSnapshot {
  runtime_operations?: import('../runtime/runtime-operations').RuntimeOperation[] // 运行操作快照，重启期间由宿主持有。
  /** resource_processes（同次采样的去重进程）：component_id（所属组件），其余为进程资源指标。 */
  resource_processes?: Array<Pick<ApplicationComponent, 'pid' | 'created_at' | 'memory_rss_bytes' | 'memory_private_commit_bytes' | 'gpu_dedicated_bytes'> & { component_id: string }>
  /** instance_id（应用实例标识）。 */
  instance_id: string
  /** revision（状态版本）。 */
  revision: number
  /** endpoint_revision（语音端点版本）：地址不变但服务进程重启时仍触发重连。 */
  endpoint_revision?: number
  /** state（应用整体状态）。 */
  state: string
  /** core_phase（核心能力阶段）：ready（就绪）、starting（加载中）或 failed（失败）；旧宿主可能不提供。 */
  core_phase?: string
  /** unavailable_capabilities（分能力行）：界面据此显示“启动中/不可用”并单独恢复。 */
  unavailable_capabilities?: ApplicationCapability[]
  /** phase（当前启动或运行阶段）。 */
  phase: string
  /** started_at（应用启动时间，秒）。 */
  started_at: number
  /** startup_complete（全部组件结束启动尝试）。 */
  startup_complete: boolean
  /** startup_skipped（缺少本地模型时直接开放产品设置）。 */
  startup_skipped?: boolean
  /** core_available（核心语音能力可用）。 */
  core_available: boolean
  /** stopping（正在退出）。 */
  stopping: boolean
  /** voice_api_url（语音服务接口地址）。 */
  voice_api_url: string
  /** voice_api_ready（轻量业务接口已监听，模型仍可继续加载）。 */
  voice_api_ready?: boolean
  /** voice_ws_url（语音实时通信地址）。 */
  voice_ws_url: string
  /** capabilities（业务能力状态）。 */
  capabilities: Record<string, string>
  /** sampled_at（采样时间，秒）。 */
  sampled_at: number | null
  /** total_memory_rss_bytes（去重后的受管进程工作集之和，字节）。 */
  total_memory_rss_bytes: number | null
  /** total_memory_private_commit_bytes（去重后的受管进程私有提交内存之和，字节）。 */
  total_memory_private_commit_bytes?: number | null
  /** total_gpu_dedicated_bytes（按进程去重的应用专用显存字节）。 */
  total_gpu_dedicated_bytes?: number | null
  /** completed_components（完成启动尝试的组件数量）。 */
  completed_components: number
  /** total_components（启用组件数量）。 */
  total_components: number
  /** components（组件运行信息）。 */
  components: ApplicationComponent[]
}

export const applicationStateLabel = (state: string) => ({
  waiting: '等待启动', starting: '启动中', ready: '运行正常', disabled: '已停用',
  failed: '需要检查', blocked: '依赖不可用', external: '外部进程',
  degraded: '降级运行', stopping: '退出中', stopped: '已退出',
}[state] || state)

export function acceptApplicationSnapshot(previous: ApplicationSnapshot | null, next: ApplicationSnapshot): boolean {
  return !previous || previous.instance_id !== next.instance_id || next.revision >= previous.revision
}

/** applicationApiReady（业务接口可用）：缺少语音能力或在线凭据时仍允许打开和保存设置。 */
export function applicationApiReady(value: ApplicationSnapshot | null): boolean {
  if (!value || value.stopping || !value.voice_api_url) return false
  const voice = value.components.find(component => component.id === 'voice')
  return Boolean(voice?.pid && (value.voice_api_ready === true || ['ready', 'failed', 'external'].includes(voice.state)))
}

/** applicationCorePhase（核心能力阶段）：以宿主发布的 core_phase 为准，旧宿主按 core_available 回落。 */
export function applicationCorePhase(value: ApplicationSnapshot | null): 'ready' | 'starting' | 'failed' {
  if (value?.core_phase === 'ready' || value?.core_phase === 'starting' || value?.core_phase === 'failed') {
    return value.core_phase
  }
  return value?.core_available ? 'ready' : 'failed'
}

/**
 * applicationOverlay（全屏启动画面）：只在尚未完成启动或正在退出时出现。
 * 运行期的组件故障改用页面横幅与“本地组件”入口，不能用启动失败遮住整个界面。
 */
export function applicationOverlay(value: ApplicationSnapshot | null, options: {
  sessionInvalid?: boolean; inspecting?: boolean; managed?: boolean
} = {}): boolean {
  if (options.sessionInvalid) return true
  if (options.managed === false) return false
  if (['stopping', 'stopped'].includes(value?.state ?? '')) return true
  return !value?.startup_complete && !options.inspecting
}

/** applicationRuntimeFailure（运行期故障）：启动完成后核心能力才失败，据此显示横幅。 */
export function applicationRuntimeFailure(value: ApplicationSnapshot | null): boolean {
  return Boolean(value?.startup_complete) && applicationCorePhase(value) === 'failed'
}

// product/main（产品入口）只会在 Host 配置通过预检后加载，因此产品永远由宿主管理。
const managed = ref(true)
const snapshot = ref<ApplicationSnapshot | null>(null)
const error = ref('')
const inspecting = ref(startupWasSkipped())
const connected = ref(false)
const now = ref(Date.now())
const retryingAll = ref(false)
const sessionInvalid = ref(false)
const recovery = ref<DesktopRecovery | null>(null)
let entered = false // entered（当前页面已经完成首次启动，后续只呈现热切换卡）。
let hostGeneration = 0 // hostGeneration（连接代次）：丢弃停机前尚未返回的状态与鉴权失败。
let lifecycle = 0 // lifecycle（页面订阅生命周期）：阻止卸载前的异步监听接入下一次挂载。
let recoveryTimer: ReturnType<typeof setInterval> | undefined
let unlistenRecovery: (() => void) | undefined
const SESSION_INVALID_MESSAGE = '会话已失效，请退出后重新启动。'
let started = false
let events: EventSource | null = null
let fallback: ReturnType<typeof setInterval> | undefined
let clock: ReturnType<typeof setInterval> | undefined
let reconnect: ReturnType<typeof setTimeout> | undefined
let refreshRequest: Promise<void> | null = null

if (typeof window !== 'undefined' && window.__VOICE_APPLICATION__) {
  window.__VOICE_APPLICATION__.business_ready = false
}

function closeEvents() {
  events?.close()
  events = null
  connected.value = false
}

function clearHostTimers() {
  if (fallback !== undefined) clearInterval(fallback)
  if (clock !== undefined) clearInterval(clock)
  if (reconnect !== undefined) clearTimeout(reconnect)
  fallback = undefined
  clock = undefined
  reconnect = undefined
}

function clearHostConfiguration() {
  const configuration = typeof window === 'undefined' ? undefined : window.__VOICE_APPLICATION__
  if (!configuration) return
  configuration.origin = ''
  configuration.voice_api_url = ''
  configuration.voice_ws_url = ''
  delete configuration.endpoint_revision
  configuration.business_ready = false
}

function invalidateSession() {
  if (sessionInvalid.value) return
  sessionInvalid.value = true
  started = false
  clearHostTimers()
  closeEvents()
  snapshot.value = null
  clearHostConfiguration()
  error.value = SESSION_INVALID_MESSAGE
}

function isSessionInvalidStatus(status: number) {
  return status === 401 || status === 403
}

function apply(value: ApplicationSnapshot) {
  if (sessionInvalid.value || recovery.value?.suspended) return
  if (!acceptApplicationSnapshot(snapshot.value, value)) return
  if (window.__VOICE_APPLICATION__) {
    window.__VOICE_APPLICATION__.voice_api_url = value.voice_api_url
    window.__VOICE_APPLICATION__.voice_ws_url = value.voice_ws_url
    if (value.endpoint_revision === undefined) delete window.__VOICE_APPLICATION__.endpoint_revision
    else window.__VOICE_APPLICATION__.endpoint_revision = value.endpoint_revision
    window.__VOICE_APPLICATION__.business_ready = applicationApiReady(value)
  }
  // Publish Host-provided addresses and permission before the synchronous
  // businessReady watcher can start product requests or the voice socket.
  if (value.startup_skipped === true) {
    inspecting.value = true
    rememberStartupSkip()
  }
  snapshot.value = value
  if (value.startup_complete) entered = true
  if (value.state === 'stopped' && clock !== undefined) {
    now.value = Date.now()
    clearInterval(clock)
    clock = undefined
  }
  error.value = ''
}

function refresh(): Promise<void> {
  if (sessionInvalid.value || recovery.value?.suspended) return Promise.resolve()
  if (refreshRequest) return refreshRequest
  const generation = hostGeneration
  const request = (async () => {
    try {
      const response = await fetch('/api/v1/application/snapshot', { cache: 'no-store' })
      if (generation !== hostGeneration) return
      if (isSessionInvalidStatus(response.status)) {
        invalidateSession()
        return
      }
      if (!response.ok) throw new Error(`应用状态连接不可用（HTTP ${response.status}），正在重试。`)
      const value = await response.json() as ApplicationSnapshot
      if (generation === hostGeneration) apply(value)
    } catch (reason) {
      if (generation === hostGeneration && !sessionInvalid.value) error.value = reason instanceof Error ? reason.message : '应用状态连接中断'
    }
  })()
  const tracked = request.finally(() => {
    if (refreshRequest === tracked) refreshRequest = null
  })
  refreshRequest = tracked
  return tracked
}

function subscribe() {
  if (!started || sessionInvalid.value || recovery.value?.suspended || snapshot.value?.state === 'stopped') return
  closeEvents()
  let stream: EventSource
  try {
    stream = new EventSource('/api/v1/application/events')
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法订阅应用状态消息。'
    reconnect = setTimeout(() => { reconnect = undefined; subscribe() }, 2000)
    return
  }
  events = stream
  stream.onopen = () => { if (events === stream && !sessionInvalid.value) connected.value = true }
  stream.onmessage = event => {
    if (sessionInvalid.value || events !== stream) return
    try {
      apply(JSON.parse(event.data) as ApplicationSnapshot)
      if (snapshot.value?.state === 'stopped') closeEvents()
    } catch { error.value = '应用状态消息无效，正在重新读取' }
  }
  stream.onerror = () => {
    if (events !== stream || sessionInvalid.value) return
    connected.value = false
    stream.close()
    events = null
    // Refresh first so an expired session becomes terminal before another stream is opened.
    void refresh().finally(() => {
      if (!started || sessionInvalid.value || snapshot.value?.state === 'stopped') return
      reconnect = setTimeout(() => { reconnect = undefined; subscribe() }, 2000)
    })
  }
}

async function post(path: string) {
  if (sessionInvalid.value) throw new Error(SESSION_INVALID_MESSAGE)
  if (recovery.value?.suspended) throw new Error('组件正在更新，请等待业务服务恢复。')
  const generation = hostGeneration
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  if (generation !== hostGeneration) return
  if (isSessionInvalidStatus(response.status)) {
    invalidateSession()
    throw new Error(SESSION_INVALID_MESSAGE)
  }
  if (!response.ok) {
    const value = await response.json().catch(() => ({})) as { error?: string }
    throw new Error(value.error || '应用操作失败')
  }
  // 已发送的旧状态请求可以完成，操作后的确认必须重新读取，避免把重试误判为仍然失败。
  if (refreshRequest) await refreshRequest
  await refresh()
}

async function retry(id: string) {
  if (sessionInvalid.value) return
  try { await post(`/api/v1/application/components/${id}/retry`) }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '组件恢复失败' }
}

async function retryCapability(id: string) {
  if (sessionInvalid.value) return
  try { await post(`/api/v1/application/capabilities/${id}/retry`) }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '组件恢复失败' }
}

async function retryFailed() {
  if (sessionInvalid.value || retryingAll.value) return
  retryingAll.value = true
  const attempted = new Set<string>()
  try {
    for (let count = 0; count < 8; count++) {
      const rows = snapshot.value?.components || []
      const priority = ['web', 'setup', 'asr', 'analysis', 'voice', 'speaker', 'dsh']
      const id = priority.find(key => rows.some(c => c.id === key && c.retry_allowed) && !attempted.has(key))
      if (!id) break
      attempted.add(id)
      await retry(id)
      while (started && snapshot.value?.components.some(c => c.id === id && c.retrying)) {
        await new Promise(resolve => setTimeout(resolve, 1000))
        await refresh()
      }
    }
  } finally { retryingAll.value = false }
}

async function exit() {
  if (sessionInvalid.value) {
    try {
      const native = locateNativeInvoker()
      if (!native) throw new Error('退出应用需要桌面原生运行时。')
      await native.invoke<void>('exit_application')
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : '退出请求失败'
      error.value = detail === SESSION_INVALID_MESSAGE ? SESSION_INVALID_MESSAGE : `${SESSION_INVALID_MESSAGE}\n${detail}`
    }
    return
  }
  try { await post('/api/v1/application/exit') }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '退出请求失败' }
}

async function start() {
  if (started || !managed.value || sessionInvalid.value) return
  started = true
  const version = ++lifecycle
  const port = desktopPort()
  if (port.available) {
    void port.listen?.<DesktopRecovery>('desktop://runtime-recovery', event => {
      if (started && version === lifecycle) applyRecovery(event.payload)
    }).then(unlisten => { if (!started || version !== lifecycle) unlisten(); else unlistenRecovery = unlisten }).catch(() => undefined)
    await refreshRecovery(version)
    if (!started || version !== lifecycle) return
    recoveryTimer = setInterval(() => { void refreshRecovery() }, 2000)
  }
  clock = setInterval(() => { now.value = Date.now() }, 1000)
  await refresh()
  if (!started || version !== lifecycle || sessionInvalid.value) return
  subscribe()
  fallback = setInterval(() => {
    if (started && !sessionInvalid.value && !connected.value && snapshot.value?.state !== 'stopped') void refresh()
  }, 2000)
}

function applyRecovery(value: DesktopRecovery) {
  if (!value || typeof value.revision !== 'number' || recovery.value && value.revision <= recovery.value.revision) return
  const previous = recovery.value
  const replaced = value.operation && (previous?.operation?.id !== value.operation.id
    || ['stopping', 'stopped'].includes(snapshot.value?.state ?? ''))
  if (!value.operation || previous?.suspended === value.suspended && !replaced) { recovery.value = value; return }
  hostGeneration++
  refreshRequest = null
  closeEvents()
  if (reconnect !== undefined) clearTimeout(reconnect)
  reconnect = undefined
  if (!value.suspended) snapshot.value = null
  recovery.value = value
  const configuration = window.__VOICE_APPLICATION__
  if (configuration) {
    configuration.voice_api_url = ''; configuration.voice_ws_url = ''; configuration.business_ready = false
    delete configuration.endpoint_revision
  }
  if (value.suspended) {
    // __VOICE_DESKTOP_RECOVERY__（计划内开发服务重启）：Vite（界面开发工具）仅重连，不刷新整个页面。
    ;(window as Window & { __VOICE_DESKTOP_RECOVERY__?: boolean }).__VOICE_DESKTOP_RECOVERY__ = true
  }
  if (!value.suspended && started) {
    // 新宿主可能仍在加载模型；保留页面，先重新接入其轻量状态接口。
    if (clock === undefined) clock = setInterval(() => { now.value = Date.now() }, 1000)
    void refresh().then(subscribe)
  }
}

async function refreshRecovery(version = lifecycle) {
  try {
    const value = await locateNativeInvoker()?.invoke<{ runtime_recovery?: DesktopRecovery }>('desktop_status')
    if (started && version === lifecycle && value?.runtime_recovery) applyRecovery(value.runtime_recovery)
  } catch { /* 原生事件继续工作；下一次补查会恢复状态。 */ }
}

function stop() {
  started = false
  lifecycle++
  closeEvents()
  clearHostTimers()
  if (recoveryTimer !== undefined) clearInterval(recoveryTimer)
  recoveryTimer = undefined
  unlistenRecovery?.()
  unlistenRecovery = undefined
  hostGeneration++
  recovery.value = null
  entered = false
}

export function useApplicationHost() {
  const businessReady = computed(() => !recovery.value?.suspended && applicationApiReady(snapshot.value))
  // corePhase（核心能力阶段）：旧宿主没有该字段时按 core_available 回落，保持原有语义。
  const corePhase = computed(() => applicationCorePhase(snapshot.value))
  // overlay（全屏启动画面）：只在尚未完成启动或正在退出时出现。
  // 运行期的组件故障改用页面横幅与“本地组件”入口，不再用启动失败遮住整个界面。
  const overlay = computed(() => entered && !sessionInvalid.value ? false : applicationOverlay(snapshot.value, {
    sessionInvalid: sessionInvalid.value, inspecting: inspecting.value, managed: managed.value }))
  // runtimeFailure（运行期故障）：启动完成后核心能力才失败，据此显示横幅。
  const runtimeFailure = computed(() => applicationRuntimeFailure(snapshot.value))
  const runtimeOperations = computed(() => recovery.value?.operation ? [recovery.value.operation] : [])
  return { managed, snapshot, error, inspecting, connected, now, retryingAll, sessionInvalid, recovery, runtimeOperations,
    businessReady, overlay, corePhase, runtimeFailure, start, stop, retry, retryCapability, retryFailed, exit,
    recover: () => locateNativeInvoker()?.invoke<void>('desktop_start_service'), // recover（原生恢复）：网页宿主离线时仍可重试。
    inspect: () => { inspecting.value = true; rememberStartupSkip() }, refresh }
}
