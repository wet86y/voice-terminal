import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useApplicationHost } from './application-host'
import { locateNativeInvoker } from '../runtime/desktop-preflight'
import { nativeDeviceApi, type RemoteManagementOperation } from './native-device-api'
import {
  desktopPort,
  APP_UPDATE_EVENT,
  RESOURCE_PROGRESS_EVENT,
  WORKSPACE_PROGRESS_EVENT,
  type DesktopPort,
  type DesktopWorkspacePort,
  type DesktopEnvironmentPort,
  type DesktopUpdatePort,
  type AppUpdateStatus,
  type ResourceJob,
  type ResourceStatus,
  type ResourceInstallPlan,
  type WorkspaceInfo,
  type WorkspaceJob,
} from './desktop-port'
import type { LocalDownloadItem } from '../interface/local-components'

const MAX_VISIBLE_JOBS = 8
const TERMINAL_JOB_STATES = new Set(['complete', 'completed', 'failed', 'cancelled', 'canceled'])
const ACTIVE_REFRESH_MS = 2000 // ACTIVE_REFRESH_MS（活动任务补查间隔）：事件之外每两秒确认状态。
const IDLE_REFRESH_MS = 10000 // IDLE_REFRESH_MS（空闲清单刷新间隔）。

/** ResourceDownloadBundlePlan（分类下载计划）：确认前并列展示该分类涉及的固定资源。 */
export interface ResourceDownloadBundlePlan {
  /** title（用途分类名称）与 plans（逐资源安装计划）。 */
  title: string
  plans: ResourceInstallPlan[]
  /** entries（确认清单）：含已就绪、正在处理及无需独立下载的条目。 */
  entries: LocalDownloadItem[]
}

/** useWorkspaceResources（工作区与资源控制器）：原生操作不依赖 Voice（语音服务）接口。 */
export function useWorkspaceResources() {
  const application = useApplicationHost()
  const port = desktopPort() as DesktopPort & DesktopWorkspacePort & DesktopEnvironmentPort & DesktopUpdatePort
  const remoteMode = ref(typeof window !== 'undefined' && window.__VOICE_APPLICATION__?.remote_mode === true)
  let targetResolved = remoteMode.value || !locateNativeInvoker()
  let targetResolution: Promise<boolean> | null = null
  const remoteResourceNotice = '远程服务端组件状态暂不可用。本机资源操作已停用，以免修改客户端组件。'
  const remoteConnected = computed(() => application.snapshot.value?.remote_mode === true
    && application.snapshot.value.management_connected === true)
  const connectionNotice = computed(() => !remoteMode.value || remoteConnected.value ? ''
    : application.snapshot.value?.remote_connection_state === 'unpaired' ? '请先在设备与角色中配对服务端，再查看服务端组件。'
      : application.snapshot.value?.remote_connection_state === 'paused' ? '自动重连已暂停，请在设备与角色中恢复连接。'
        : '正在等待服务端连接，连接后自动读取服务端组件和任务进度。')
  const workspace = ref<WorkspaceInfo | null>(null)
  const resources = ref<ResourceStatus[]>([])
  const resourceJobs = ref<ResourceJob[]>([])
  const workspaceJobs = ref<WorkspaceJob[]>([])
  const loading = ref(false)
  const refreshing = ref(false)
  const inventoryRefreshing = ref(false)
  const inventoryUpdatedAt = ref<number | null>(null) // inventoryUpdatedAt（最近成功刷新时间，毫秒）。
  const resourceError = ref('')
  const workspaceError = ref('')
  const eventError = ref('')
  const appUpdate = ref<AppUpdateStatus>({
    state: 'idle', current_version: '', fields: {
      state: '更新阶段', current_version: '当前程序版本', target_version: '目标程序版本',
      release_notes: '发行说明', phase: '当前处理阶段', completed_bytes: '已下载字节数', total_bytes: '安装器总字节数',
      error: '更新错误', can_cancel: '当前是否允许取消',
    },
  })
  const integrity = ref<AppUpdateStatus>({ state: 'idle', operation: 'integrity', installer_ready: false, current_version: '', fields: {} }) // integrity（当前构建维护状态）。
  const startingResources = ref(new Set<string>())
  const workspaceOperationStarting = ref(false)
  const pendingImport = computed(() => resourceJobs.value.find(job => job.import_preview?.length && !TERMINAL_JOB_STATES.has(job.state)) ?? null)
  const pendingResetPlan = ref<{ resource_id: string; paths: string[]; voice_paths?: string[]; token: string; message: string; plan_request_id?: string } | null>(null) // pendingResetPlan（服务端一次性删除范围确认）。
  const pendingDownloadPlan = ref<ResourceInstallPlan | null>(null)
  const pendingDownloadBundlePlan = ref<ResourceDownloadBundlePlan | null>(null) // pendingDownloadBundlePlan（分类下载确认草稿）。
  const preparingDownloadBundle = ref(false) // preparingDownloadBundle（正在并行读取分类资源计划）。
  let mounted = false
  let unlistenResource: (() => void) | undefined
  let unlistenWorkspace: (() => void) | undefined
  let unlistenAppUpdate: (() => void) | undefined
  let unlistenRequests: Promise<void>[] = []
  let refreshRequest: Promise<void> | null = null
  let inventoryRefreshRequest: Promise<void> | null = null
  let inventoryRevision = 0 // inventoryRevision（刷新请求代次）：完成事件要求再读一次最新清单。
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  let pollDueAt = 0 // pollDueAt（下次补查时刻）：持续进度事件不能把两秒计时不断推迟。
  let pollRunning = false // pollRunning（补查正在读取）：慢磁盘期间不叠加下一轮或使旧结果不断失效。
  let snapshotRequest: Promise<void> | null = null
  let eventRevision = 0 // eventRevision（页面事件代次）：兼容没有任务修订号的测试接口。
  let targetRevision = 0 // targetRevision（远程目标与连通性代次）：交接后丢弃旧清单和任务结果。
  const receivedEvents = new Map<string, number>()

  async function resolveRemoteMode(): Promise<boolean> {
    if (targetResolved) return remoteMode.value
    if (!locateNativeInvoker()) {
      targetResolved = true
      return remoteMode.value
    }
    if (!targetResolution) {
      targetResolution = nativeDeviceApi.deviceConfigStatus().then(status => {
        remoteMode.value = status.runtime.mode === 'remote'
        targetResolved = true
        return remoteMode.value
      }).catch(error => {
        // 无法确认操作目标时，拒绝本机资源写入。
        remoteMode.value = true
        targetResolved = true
        resourceError.value = error instanceof Error ? error.message : '无法确认资源管理目标。'
        return true
      }).finally(() => { targetResolution = null })
    }
    return targetResolution
  }

  function localResourceTargetAllowed(): boolean {
    if (!port.available) return false
    if (!targetResolved) {
      void resolveRemoteMode()
      resourceError.value = '正在确认资源管理目标，请稍后重试。'
      return false
    }
    if (remoteMode.value) {
      resourceError.value = remoteResourceNotice
      workspaceError.value = remoteResourceNotice
      return false
    }
    return true
  }

  function resourceTargetAllowed(): boolean {
    if (!port.available) return false
    if (!targetResolved) {
      void resolveRemoteMode()
      resourceError.value = '正在确认资源管理目标，请稍后重试。'
      return false
    }
    return !remoteMode.value || remoteConnected.value
  }

  async function remoteResourceOperation<T>(operation: RemoteManagementOperation, argumentsValue: Record<string, unknown> = {}): Promise<T> {
    const result = await nativeDeviceApi.remoteManagementRequest(operation, argumentsValue)
    if (result.status < 200 || result.status >= 300) {
      const body = result.body as Record<string, unknown>
      const message = typeof body.message === 'string' ? body.message
        : typeof body.error === 'string' ? body.error : `服务端资源操作失败（${result.status}）。`
      throw new Error(message)
    }
    return result.body as unknown as T
  }

  // canManage（工作区管理权限）统一由正式程序提供；源码调试使用相同权限。
  const canManage = computed(() => port.available && !remoteMode.value && workspace.value?.can_manage)
  const resourcesCanManage = computed(() => port.available && (!remoteMode.value || remoteConnected.value))
  const activeWorkspaceJob = computed(() => [...workspaceJobs.value].reverse().find(job => !TERMINAL_JOB_STATES.has(job.state)))
  const latestWorkspaceJob = computed(() => workspaceJobs.value.at(-1) ?? null)

  function upsertResourceJob(incoming: ResourceJob, refreshAfterUpdate = true) {
    // history（历次尝试归档）只供追溯；重试后的当前任务由原编号提供。
    if (incoming.job_id.startsWith('history-')) return
    const index = resourceJobs.value.findIndex(job => job.job_id === incoming.job_id)
    const previous = resourceJobs.value[index]
    if (previous && incoming.created_at !== undefined && previous.created_at !== undefined) {
      if (incoming.created_at < previous.created_at) return
      if (incoming.created_at === previous.created_at && incoming.revision !== undefined && previous.revision !== undefined
        && incoming.revision <= previous.revision) return
    }
    // 最近事件移到末尾，同一资源重新下载时不会继续展示旧任务。
    const next = [
      ...resourceJobs.value.filter(job => job.job_id !== incoming.job_id),
      index < 0 || (incoming.created_at !== undefined && incoming.created_at !== previous?.created_at)
        ? incoming : { ...previous, ...incoming },
    ]
    resourceJobs.value = next.filter(job => !TERMINAL_JOB_STATES.has(job.state))
      .concat(next.filter(job => TERMINAL_JOB_STATES.has(job.state)).slice(-MAX_VISIBLE_JOBS))
    const completed = TERMINAL_JOB_STATES.has(incoming.state)
    const installedChanged = incoming.installed_resources?.some(id => !previous?.installed_resources?.includes(id))
    if (refreshAfterUpdate && (incoming.installed_resources?.length || completed)
      && (installedChanged || !previous || previous.state !== incoming.state || previous.created_at !== incoming.created_at)) {
      void refreshInventory()
    }
  }

  function refreshJobSnapshots() {
    if (!port.available || snapshotRequest) return snapshotRequest ?? Promise.resolve()
    const startedAt = eventRevision
    const target = targetRevision
    const request = (async () => {
      if (!resourceTargetAllowed()) return
      try {
        const jobs = remoteMode.value
          ? await remoteResourceOperation<ResourceJob[]>('resource.jobs')
          : typeof port.resourceJobSnapshots === 'function'
            ? await port.resourceJobSnapshots()
            : typeof port.environmentStatus === 'function' ? (await port.environmentStatus()).jobs ?? [] : []
        if (!mounted || target !== targetRevision) return
        // 清除旧页面曾恢复的归档，避免其失败提示覆盖已经完成的当前任务。
        resourceJobs.value = resourceJobs.value.filter(job => !job.job_id.startsWith('history-'))
        eventError.value = ''
        for (const job of jobs) {
          if (job.job_id.startsWith('history-')) continue
          const known = resourceJobs.value.some(current => current.job_id === job.job_id)
          // 只恢复活动任务；已知任务则接收完成/失败快照，补救丢失的终态事件。
          if (!known && TERMINAL_JOB_STATES.has(job.state) && !(job.state === 'failed' && job.recovery?.next_action !== 'none' && job.recovery?.next_action)) continue
          if ((receivedEvents.get(job.job_id) ?? 0) > startedAt && (job.created_at === undefined || job.revision === undefined)) continue
          upsertResourceJob(job)
        }
      } catch (error) {
        if (mounted && target === targetRevision) eventError.value = error instanceof Error ? error.message : '任务进度补查失败，将自动重试。'
      }
    })()
    const tracked = request.finally(() => { if (snapshotRequest === tracked) snapshotRequest = null })
    snapshotRequest = tracked
    return tracked
  }

  function schedulePoll(delay?: number) {
    if (!mounted || !port.available || pollRunning) return
    const active = startingResources.value.size > 0 || resourceJobs.value.some(job => !TERMINAL_JOB_STATES.has(job.state))
    const wait = delay ?? (active ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS)
    const due = Date.now() + wait
    if (pollTimer !== undefined && pollDueAt <= due) return
    if (pollTimer !== undefined) clearTimeout(pollTimer)
    pollDueAt = due
    pollTimer = setTimeout(() => {
      pollTimer = undefined
      pollDueAt = 0
      pollRunning = true
      // 不叠加定时请求；快照完成后刷新安装状态，再开始下一次计时。
      void refreshJobSnapshots().finally(() => { pollRunning = false; schedulePoll() })
    }, wait)
  }
  function refreshOnFocus() { schedulePoll(0) }

  function upsertWorkspaceJob(incoming: WorkspaceJob) {
    const index = workspaceJobs.value.findIndex(job => job.job_id === incoming.job_id)
    const next = index < 0
      ? [...workspaceJobs.value, incoming]
      : workspaceJobs.value.map((job, at) => at === index ? { ...job, ...incoming } : job)
    workspaceJobs.value = next.slice(-MAX_VISIBLE_JOBS)
    if (TERMINAL_JOB_STATES.has(incoming.state)) void refreshWorkspace()
  }

  async function refreshWorkspace() {
    if (!port.available) return
    if (!localResourceTargetAllowed()) return
    try {
      workspace.value = await port.workspaceInfo()
      if (!appUpdate.value.current_version) appUpdate.value.current_version = workspace.value.version
      workspaceError.value = ''
    } catch (error) {
      workspaceError.value = error instanceof Error ? error.message : '无法读取工作区信息。'
    }
  }

  function refreshInventory() {
    if (!port.available) return Promise.resolve()
    inventoryRevision += 1
    if (inventoryRefreshRequest) return inventoryRefreshRequest
    inventoryRefreshing.value = true
    const request = (async () => {
      if (!resourceTargetAllowed()) return
      let checkedRevision: number
      do {
        if (!resourceTargetAllowed()) return
        checkedRevision = inventoryRevision
        const target = targetRevision
        try {
          const inventory = remoteMode.value
            ? await remoteResourceOperation<ResourceStatus[]>('resource.inventory')
            : await port.resourceInventory()
          if (!mounted) return
          if (target !== targetRevision) continue
          // 请求途中组件完成启用时，丢弃旧结果并在同一等待链中重新读取。
          if (checkedRevision === inventoryRevision) {
            // runtime_components（运行依赖子卡片）保留父子结构，并进入统一资源查找与任务刷新链路。
            const children = inventory.flatMap(resource => resource.runtime_components ?? [])
            resources.value = [...inventory, ...children.filter(child => !inventory.some(resource => resource.resource_id === child.resource_id))]
            inventoryUpdatedAt.value = Date.now()
            resourceError.value = ''
          }
        } catch (error) {
          if (mounted && checkedRevision === inventoryRevision && target === targetRevision) resourceError.value = error instanceof Error ? error.message : '无法读取资源清单。'
        }
      } while (mounted && checkedRevision !== inventoryRevision)
    })()
    const tracked = request.finally(() => {
      if (inventoryRefreshRequest === tracked) inventoryRefreshRequest = null
      inventoryRefreshing.value = false
    })
    inventoryRefreshRequest = tracked
    return tracked
  }

  function refresh() {
    if (!port.available) return Promise.resolve()
    if (refreshRequest) return refreshRequest
    refreshing.value = true
    const request = Promise.all([refreshWorkspace(), refreshInventory()]).then(() => undefined)
    const tracked = request.finally(() => {
      if (refreshRequest === tracked) refreshRequest = null
      refreshing.value = false
    })
    refreshRequest = tracked
    return tracked
  }

  async function subscribe() {
    if (!port.available) return
    if (!targetResolved) return
    let resourceUnlisten: (() => void) | undefined
    let workspaceUnlisten: (() => void) | undefined
    let updateUnlisten: (() => void) | undefined
    try {
      resourceUnlisten = await port.listen<ResourceJob>(
        RESOURCE_PROGRESS_EVENT,
        event => {
          if (!mounted) return
          if (remoteMode.value && !event.payload.job_id.startsWith('remote-import-')) return
          receivedEvents.set(event.payload.job_id, ++eventRevision)
          upsertResourceJob(event.payload)
          schedulePoll()
        },
      )
      workspaceUnlisten = await port.listen<WorkspaceJob>(
        WORKSPACE_PROGRESS_EVENT,
        event => { if (!remoteMode.value) upsertWorkspaceJob(event.payload) },
      )
      updateUnlisten = await port.listen<AppUpdateStatus>(
        APP_UPDATE_EVENT,
        event => { if (mounted) { if (event.payload.operation === 'integrity') integrity.value = event.payload; else appUpdate.value = event.payload } },
      )
      if (!mounted) {
        resourceUnlisten()
        workspaceUnlisten()
        updateUnlisten()
        return
      }
      unlistenResource = resourceUnlisten
      unlistenWorkspace = workspaceUnlisten
      unlistenAppUpdate = updateUnlisten
    } catch (error) {
      resourceUnlisten?.()
      workspaceUnlisten?.()
      updateUnlisten?.()
      eventError.value = error instanceof Error ? error.message : '无法订阅桌面任务进度。'
    }
  }

  onMounted(() => {
    mounted = true
    if (!port.available) return
    loading.value = true
    window.addEventListener('focus', refreshOnFocus)
    void (async () => {
      const remote = await resolveRemoteMode()
      if (remote) {
        workspaceError.value = remoteResourceNotice
        unlistenRequests = [subscribe(), refreshJobSnapshots(), refreshInventory()]
        await Promise.all(unlistenRequests)
        schedulePoll()
        return
      }
      unlistenRequests = [subscribe(), refreshJobSnapshots(), refresh().then(() => undefined),
        Promise.resolve().then(() => port.integrityStatus()).then(value => { if (mounted) integrity.value = value }).catch(error => { if (mounted) integrity.value.error = String(error) })]
      await Promise.all(unlistenRequests)
      schedulePoll()
    })().finally(() => { loading.value = false })
  })

  onBeforeUnmount(() => {
    mounted = false
    if (pollTimer !== undefined) clearTimeout(pollTimer)
    pollTimer = undefined
    pollDueAt = 0
    window.removeEventListener('focus', refreshOnFocus)
    unlistenResource?.()
    unlistenWorkspace?.()
    unlistenAppUpdate?.()
    unlistenResource = undefined
    unlistenWorkspace = undefined
    unlistenAppUpdate = undefined
  })

  watch([() => application.snapshot.value?.host_id, remoteConnected], ([hostId, ready], previous) => {
    if (!mounted || !remoteMode.value) return
    targetRevision++
    if (hostId !== previous?.[0]) {
      resources.value = []; resourceJobs.value = []; inventoryUpdatedAt.value = null
      pendingResetPlan.value = null; pendingDownloadPlan.value = null; pendingDownloadBundlePlan.value = null
    }
    resourceError.value = ''; eventError.value = ''
    if (ready) { void refreshInventory(); void refreshJobSnapshots() }
  })

  async function openProjectRepository() {
    try { await port.openProjectRepository() }
    catch (error) { workspaceError.value = error instanceof Error ? error.message : '无法打开项目仓库。' }
  }

  async function checkAppUpdate() {
    if (!port.available || workspaceOperationStarting.value || activeWorkspaceJob.value) return
    appUpdate.value = { ...appUpdate.value, state: 'checking', error: null }
    try { appUpdate.value = await port.checkAppUpdate() }
    catch (error) {
      appUpdate.value = { ...appUpdate.value, state: 'failed', error: error instanceof Error ? error.message : '检查更新失败。' }
    }
  }

  async function downloadAppUpdate() {
    if (!port.available || workspaceOperationStarting.value || activeWorkspaceJob.value) return
    try { appUpdate.value = await port.downloadAppUpdate() }
    catch (error) {
      appUpdate.value = { ...appUpdate.value, state: 'failed', error: error instanceof Error ? error.message : '无法开始下载更新。' }
    }
  }

  async function downloadIntegrity() {
    if (!port.available || remoteMode.value || workspaceOperationStarting.value || activeWorkspaceJob.value) return
    try { integrity.value = await port.downloadIntegrity() }
    catch (error) { integrity.value = { ...integrity.value, state: 'failed', error: String(error) } }
  }
  async function checkIntegrity() {
    if (!port.available || remoteMode.value || workspaceOperationStarting.value || activeWorkspaceJob.value) return
    try { integrity.value = await port.checkIntegrity() }
    catch (error) { integrity.value = { ...integrity.value, state: 'failed', error: String(error), installer_ready: false } }
  }
  async function requestIntegrity() {
    // 组件故障入口复用现有全面校验；不传入组件编号，不从错误事件自动启动维护。
    if (remoteMode.value) return
    if (integrity.value.installer_ready) await checkIntegrity()
    else await downloadIntegrity()
  }
  async function cancelAppUpdate() {
    const target = integrity.value.can_cancel ? integrity : appUpdate
    if (!target.value.can_cancel) return
    try { await port.cancelAppUpdate() }
    catch (error) { target.value = { ...target.value, error: error instanceof Error ? error.message : '取消维护下载失败。' } }
  }

  async function chooseWorkspace() {
    if (!localResourceTargetAllowed()) return null
    if (!canManage.value || workspaceOperationStarting.value || activeWorkspaceJob.value) return null
    workspaceError.value = ''
    try {
      return await port.chooseWorkspace()
    } catch (error) {
      workspaceError.value = error instanceof Error ? error.message : '无法打开工作区选择器。'
      return null
    }
  }

  async function linkWorkspace(targetRoot: string) {
    if (!localResourceTargetAllowed()) return
    if (!canManage.value || workspaceOperationStarting.value || activeWorkspaceJob.value) return
    workspaceOperationStarting.value = true
    workspaceError.value = ''
    try {
      const result = await port.linkWorkspace(targetRoot)
      if (!workspaceJobs.value.some(job => job.job_id === result.job_id)) {
        upsertWorkspaceJob({ job_id: result.job_id, state: 'queued', phase: '正在准备切换', can_cancel: false })
      }
    } catch (error) {
      workspaceError.value = error instanceof Error ? error.message : '链接工作区失败。'
    } finally {
      workspaceOperationStarting.value = false
    }
  }

  async function migrateWorkspace(targetRoot: string) {
    if (!localResourceTargetAllowed()) return
    if (!canManage.value || workspaceOperationStarting.value || activeWorkspaceJob.value) return
    workspaceOperationStarting.value = true
    workspaceError.value = ''
    try {
      const result = await port.migrateWorkspace(targetRoot)
      if (!workspaceJobs.value.some(job => job.job_id === result.job_id)) {
        upsertWorkspaceJob({ job_id: result.job_id, state: 'queued', phase: '正在准备迁移', can_cancel: true })
      }
    } catch (error) {
      workspaceError.value = error instanceof Error ? error.message : '迁移工作区失败。'
    } finally {
      workspaceOperationStarting.value = false
    }
  }

  async function downloadResource(resourceId: string) {
    if (!resourceTargetAllowed()) return
    if (!resourcesCanManage.value || startingResources.value.has(resourceId)
      || resourceJobs.value.some(job => job.resource_id === resourceId && !TERMINAL_JOB_STATES.has(job.state))
      || pendingDownloadPlan.value?.resource_id === resourceId) return
    startingResources.value = new Set(startingResources.value).add(resourceId)
    resourceError.value = ''
    try {
      pendingDownloadPlan.value = remoteMode.value
        ? await remoteResourceOperation<ResourceInstallPlan>('resource.install.plan', { resource_id: resourceId })
        : await port.resourceInstallPlan(resourceId)
    }
    catch (error) { resourceError.value = String(error) }
    finally {
      const next = new Set(startingResources.value)
      next.delete(resourceId)
      startingResources.value = next
    }
  }
  async function confirmDownload() {
    const plan = pendingDownloadPlan.value
    if (!plan) return
    pendingDownloadPlan.value = null
    await startResourceJob(plan.resource_id, 'download')
  }

  async function prepareDownloadBundle(entries: LocalDownloadItem[], title: string) {
    if (!resourceTargetAllowed()) return
    if (!resourcesCanManage.value || pendingDownloadPlan.value || pendingDownloadBundlePlan.value || preparingDownloadBundle.value) return
    preparingDownloadBundle.value = true
    const ids = [...new Set(entries.flatMap(item => item.id ? [item.id] : []))].filter(id => {
      const resource = resources.value.find(item => item.resource_id === id)
      const alreadyInstalled = resource?.installation_state === 'installed' && resource.update_available !== true
      const repairSharedRuntime = entries.some(item => item.id === id && item.forcePlan)
      const alreadyRunning = startingResources.value.has(id)
        || resourceJobs.value.some(job => job.resource_id === id && !TERMINAL_JOB_STATES.has(job.state))
      return (!alreadyInstalled || repairSharedRuntime) && !alreadyRunning
    })
    const loadingKeys = ids.map(id => `plan:${id}`)
    startingResources.value = new Set([...startingResources.value, ...loadingKeys])
    resourceError.value = ''
    try {
      const plans = await Promise.all(ids.map(id => remoteMode.value
        ? remoteResourceOperation<ResourceInstallPlan>('resource.install.plan', { resource_id: id })
        : port.resourceInstallPlan(id)))
      pendingDownloadBundlePlan.value = { title, plans, entries }
    } catch (error) {
      resourceError.value = error instanceof Error ? error.message : '无法读取分类下载计划。'
    } finally {
      const next = new Set(startingResources.value)
      loadingKeys.forEach(id => next.delete(id))
      startingResources.value = next
      preparingDownloadBundle.value = false
    }
  }

  async function confirmDownloadBundle(resourceIds?: string[]) {
    const bundle = pendingDownloadBundlePlan.value
    if (!bundle) return
    pendingDownloadBundlePlan.value = null
    // 各资源任务并行启动；原生资源管理器负责共享依赖、环境启用和重复文件协调。
    const selected = resourceIds ? new Set(resourceIds) : new Set(bundle.plans.map(plan => plan.resource_id))
    await Promise.all(bundle.plans
      .filter(plan => selected.has(plan.resource_id))
      .map(plan => startResourceJob(plan.resource_id, 'download')))
  }

  async function confirmReset() {
    if (!resourceTargetAllowed()) return
    const plan = pendingResetPlan.value
    if (!plan) return
    pendingResetPlan.value = null
    try {
      const result = remoteMode.value
        ? await remoteResourceOperation<{ job_id: string }>('resource.reset', {
          plan_request_id: plan.plan_request_id,
          resource_id: plan.resource_id,
          token: plan.token,
          confirmed: true,
        })
        : await port.resetResource(plan.resource_id, plan.token)
      upsertResourceJob({ job_id: result.job_id, resource_id: plan.resource_id, state: 'queued', phase: '准备重置组件', can_cancel: false })
      void refreshJobSnapshots()
      schedulePoll(ACTIVE_REFRESH_MS)
    } catch (error) { resourceError.value = String(error) }
  }
  async function requestReset(resourceId: string) {
    if (!resourceTargetAllowed()) return
    try {
      pendingResetPlan.value = remoteMode.value
        ? await remoteResourceOperation<{ resource_id: string; paths: string[]; voice_paths?: string[]; token: string; message: string; plan_request_id?: string }>(
          'resource.reset.plan', { resource_id: resourceId },
        )
        : await port.resourceResetPlan(resourceId)
    }
    catch (error) { resourceError.value = String(error) }
  }
  async function importResource(resourceId?: string) {
    if (!resourceTargetAllowed()) return
    const rows = resources.value.flatMap(row => [row, ...(row.runtime_components ?? [])])
    if (resourceId && rows.find(row => row.resource_id === resourceId)?.reset_available) {
      await requestReset(resourceId)
      return
    }
    if (remoteMode.value && resourceId?.startsWith('tts-aniflive-') && resourceId !== 'tts-aniflive-runtime') {
      resourceError.value = '远程设备的 AnifLive 模型仅支持服务端从在线网址导入；客户端模型目录上传暂不支持。'
      return
    }
    if (remoteMode.value) {
      await startResourceJob(resourceId ?? 'batch', 'import', resourceId)
      return
    }
    const directImports = new Set(['voice-server', 'voice-runtime-cpu', 'voice-runtime-gpu'])
    if (resourceId && directImports.has(resourceId)) {
      await startResourceJob(resourceId, 'import', resourceId)
      return
    }
    if (port.chooseImportSources && port.startImportSources) {
      try {
        const selection = await port.chooseImportSources(resourceId)
        if (selection.cancelled || !selection.selection_id) return
        await startResourceJob(resourceId ?? 'batch', 'import', resourceId, selection.selection_id)
      } catch (error) {
        resourceError.value = error instanceof Error ? error.message : '资源来源选择失败。'
      }
      return
    }
    await startResourceJob(resourceId ?? 'batch', 'import', resourceId)
  }

  async function startResourceJob(resourceKey: string, operation: 'download' | 'import' | 'required', resourceId?: string, selectionId?: string) {
    if (!resourceTargetAllowed()) return
    if (!resourcesCanManage.value || startingResources.value.has(resourceKey)
      || resourceJobs.value.some(job => job.resource_id === resourceKey && !TERMINAL_JOB_STATES.has(job.state))) return
    startingResources.value = new Set(startingResources.value).add(resourceKey)
    resourceError.value = ''
    try {
      const result = remoteMode.value
        ? operation === 'required'
          ? await remoteResourceOperation<{ job_id: string }>('resource.install.required')
          : operation === 'download'
            ? await remoteResourceOperation<{ job_id: string }>('resource.install.start', { resource_id: resourceKey })
            : await nativeDeviceApi.startRemoteResourceImport(resourceId)
        : operation === 'required'
          ? await port.downloadRequiredResources?.() ?? (() => { throw new Error('当前宿主不支持一键下载，请更新程序。') })()
          : operation === 'download'
            ? await port.downloadResource(resourceKey)
            : selectionId && port.startImportSources
              ? await port.startImportSources(selectionId)
              : await port.importResource(resourceId)
      if (('cancelled' in result && result.cancelled) || !result.job_id) return
      if (!resourceJobs.value.some(job => job.job_id === result.job_id)) {
        upsertResourceJob({
          job_id: result.job_id,
          resource_id: resourceKey,
          state: 'queued',
          phase: operation === 'required' ? '正在准备必要组件' : operation === 'download' ? '正在准备下载' : resourceId ? '正在准备导入' : '正在准备批量导入',
          can_cancel: true,
        })
      }
      // 同一编号重试也补查新一轮快照，避免终态事件早于命令响应时遗留排队状态。
      void refreshJobSnapshots()
      schedulePoll(ACTIVE_REFRESH_MS)
    } catch (error) {
      resourceError.value = error instanceof Error ? error.message : '资源任务启动失败。'
    } finally {
      const next = new Set(startingResources.value)
      next.delete(resourceKey)
      startingResources.value = next
    }
  }

  async function openResourceSource(resourceId: string) {
    if (await resolveRemoteMode()) {
      resourceError.value = '远程模式暂不支持从客户端打开服务端组件来源链接。'
      return
    }
    if (!localResourceTargetAllowed()) return
    try {
      await port.openResourceSource(resourceId)
    } catch (error) {
      resourceError.value = error instanceof Error ? error.message : '无法打开上游来源页面。'
    }
  }

  async function cancelJob(jobId: string) {
    if (!resourceTargetAllowed()) return
    const resourceJob = resourceJobs.value.find(job => job.job_id === jobId)
    const workspaceJob = workspaceJobs.value.find(job => job.job_id === jobId)
    if ((!resourceJob && !workspaceJob) || resourceJob?.can_cancel === false || workspaceJob?.can_cancel === false) return
    try {
      if (remoteMode.value) {
        if (jobId.startsWith('remote-import-')) await nativeDeviceApi.cancelRemoteResourceImport(jobId)
        else await remoteResourceOperation('resource.job.cancel', { job_id: jobId })
      } else await port.cancelResource(jobId)
    } catch (error) {
      const message = error instanceof Error ? error.message : '取消任务失败。'
      if (resourceJob) upsertResourceJob({ ...resourceJob, error: message })
      if (workspaceJob) upsertWorkspaceJob({ ...workspaceJob, error: message })
    }
  }

  async function confirmImport(accepted: boolean) {
    if (!resourceTargetAllowed()) return
    if (!pendingImport.value) return
    try {
      if (remoteMode.value) await remoteResourceOperation('resource.import.confirm', {
        job_id: pendingImport.value.job_id,
        accepted,
      })
      else await port.confirmImport(pendingImport.value.job_id, accepted)
    }
    catch (failure) { resourceError.value = String(failure) }
  }
  return {
    remoteMode,
    connectionNotice,
    pendingImport,
    confirmImport,
    portAvailable: computed(() => port.available),
    workspace,
    resources,
    resourceJobs,
    workspaceJobs,
    activeWorkspaceJob,
    latestWorkspaceJob,
    canManage,
    resourcesCanManage,
    loading,
    refreshing,
    inventoryRefreshing,
    inventoryUpdatedAt,
    resourceError,
    workspaceError,
    eventError,
    appUpdate,
    integrity,
    downloadIntegrity,
    checkIntegrity,
    requestIntegrity,
    openProjectRepository,
    checkAppUpdate,
    downloadAppUpdate,
    cancelAppUpdate,
    startingResources,
    workspaceOperationStarting,
    refresh,
    refreshInventory,
    chooseWorkspace,
    linkWorkspace,
    migrateWorkspace,
    downloadResource,
    downloadRequiredResources: () => startResourceJob('required-batch', 'required'),
    pendingResetPlan,
    requestReset,
    confirmReset,
    pendingDownloadPlan,
    confirmDownload,
    pendingDownloadBundlePlan,
    preparingDownloadBundle,
    prepareDownloadBundle,
    confirmDownloadBundle,
    importResource,
    openResourceSource,
    cancelJob,
  }
}
