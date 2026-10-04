import { computed, ref } from 'vue'

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

export interface ApplicationSnapshot {
  /** instance_id（应用实例标识）。 */
  instance_id: string
  /** revision（状态版本）。 */
  revision: number
  /** state（应用整体状态）。 */
  state: string
  /** phase（当前启动或运行阶段）。 */
  phase: string
  /** started_at（应用启动时间，秒）。 */
  started_at: number
  /** startup_complete（全部组件结束启动尝试）。 */
  startup_complete: boolean
  /** core_available（核心语音能力可用）。 */
  core_available: boolean
  /** stopping（正在退出）。 */
  stopping: boolean
  /** voice_api_url（语音服务接口地址）。 */
  voice_api_url: string
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
  return Boolean(voice?.pid && ['ready', 'failed', 'external'].includes(voice.state))
}

const replay = import.meta.env.DEV && typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('replay') === 'voice-shadow'
const managed = ref(typeof window !== 'undefined' && Boolean(window.__VOICE_APPLICATION__?.managed) && !replay)
const snapshot = ref<ApplicationSnapshot | null>(null)
const error = ref('')
const inspecting = ref(false)
const connected = ref(false)
const now = ref(Date.now())
const retryingAll = ref(false)
let started = false
let events: EventSource | null = null
let fallback: ReturnType<typeof setInterval> | undefined
let clock: ReturnType<typeof setInterval> | undefined
let reconnect: ReturnType<typeof setTimeout> | undefined

function apply(value: ApplicationSnapshot) {
  if (!acceptApplicationSnapshot(snapshot.value, value)) return
  snapshot.value = value
  if (value.state === 'stopped' && clock) {
    now.value = Date.now()
    clearInterval(clock)
  }
  if (window.__VOICE_APPLICATION__) {
    window.__VOICE_APPLICATION__.voice_api_url = value.voice_api_url
    window.__VOICE_APPLICATION__.voice_ws_url = value.voice_ws_url
  }
  error.value = ''
}

async function refresh() {
  try {
    const response = await fetch('/api/v1/application/snapshot', { cache: 'no-store' })
    if (!response.ok) throw new Error('应用状态连接不可用，请从托盘重新打开页面。')
    apply(await response.json() as ApplicationSnapshot)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '应用状态连接中断'
  }
}

function subscribe() {
  if (!started || snapshot.value?.state === 'stopped') return
  events?.close()
  events = new EventSource('/api/v1/application/events')
  events.onopen = () => { connected.value = true }
  events.onmessage = event => {
    try {
      apply(JSON.parse(event.data) as ApplicationSnapshot)
      if (snapshot.value?.state === 'stopped') { connected.value = false; events?.close() }
    } catch { error.value = '应用状态消息无效，正在重新读取' }
  }
  events.onerror = () => {
    connected.value = false
    events?.close()
    if (snapshot.value?.state !== 'stopped') reconnect = setTimeout(subscribe, 2000)
  }
}

async function post(path: string) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  if (!response.ok) {
    const value = await response.json().catch(() => ({})) as { error?: string }
    throw new Error(value.error || '应用操作失败')
  }
  await refresh()
}

async function retry(id: string) {
  try { await post(`/api/v1/application/components/${id}/retry`) }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '组件恢复失败' }
}

async function retryFailed() {
  if (retryingAll.value) return
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
  try { await post('/api/v1/application/exit') }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '退出请求失败' }
}

async function start() {
  if (started || !managed.value) return
  started = true
  clock = setInterval(() => { now.value = Date.now() }, 1000)
  await refresh()
  subscribe()
  fallback = setInterval(() => { if (!connected.value && snapshot.value?.state !== 'stopped') void refresh() }, 2000)
}

function stop() {
  started = false
  events?.close()
  if (fallback) clearInterval(fallback)
  if (clock) clearInterval(clock)
  if (reconnect) clearTimeout(reconnect)
}

export function useApplicationHost() {
  const businessReady = computed(() => !managed.value || applicationApiReady(snapshot.value))
  const overlay = computed(() => managed.value && (
    !snapshot.value || ['stopping', 'stopped'].includes(snapshot.value.state)
    || ((!snapshot.value.startup_complete || snapshot.value.state === 'failed') && !inspecting.value)))
  return { managed, snapshot, error, inspecting, connected, now, retryingAll,
    businessReady, overlay, start, stop, retry, retryFailed, exit,
    inspect: () => { inspecting.value = true }, refresh }
}
