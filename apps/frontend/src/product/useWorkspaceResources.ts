import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  desktopPort,
  RESOURCE_PROGRESS_EVENT,
  WORKSPACE_PROGRESS_EVENT,
  type DesktopPort,
  type DesktopWorkspacePort,
  type DesktopEnvironmentPort,
  type ResourceJob,
  type ResourceStatus,
  type ResourceInstallPlan,
  type WorkspaceInfo,
  type WorkspaceJob,
} from './desktop-port'

const MAX_VISIBLE_JOBS = 8
const TERMINAL_JOB_STATES = new Set(['complete', 'completed', 'failed', 'cancelled', 'canceled'])
const ACTIVE_REFRESH_MS = 2000 // ACTIVE_REFRESH_MS（活动任务补查间隔）：事件之外每两秒确认状态。
const IDLE_REFRESH_MS = 10000 // IDLE_REFRESH_MS（空闲清单刷新间隔）。

/** useWorkspaceResources（工作区与资源控制器）：原生操作不依赖 Voice（语音服务）接口。 */
export function useWorkspaceResources() {
  const port = desktopPort() as DesktopPort & DesktopWorkspacePort & DesktopEnvironmentPort
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
  const startingResources = ref(new Set<string>())
  const workspaceOperationStarting = ref(false)
  const pendingImport = computed(() => resourceJobs.value.find(job => job.import_preview?.length && !TERMINAL_JOB_STATES.has(job.state)) ?? null)
  const pendingDownloadPlan = ref<ResourceInstallPlan | null>(null)
  let mounted = false
  let unlistenResource: (() => void) | undefined
  let unlistenWorkspace: (() => void) | undefined
  let unlistenRequests: Promise<void>[] = []
  let refreshRequest: Promise<void> | null = null
  let inventoryRefreshRequest: Promise<void> | null = null
  let inventoryRevision = 0 // inventoryRevision（刷新请求代次）：完成事件要求再读一次最新清单。
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  let pollDueAt = 0 // pollDueAt（下次补查时刻）：持续进度事件不能把两秒计时不断推迟。
  let pollRunning = false // pollRunning（补查正在读取）：慢磁盘期间不叠加下一轮或使旧结果不断失效。
  let snapshotRequest: Promise<void> | null = null
  let eventRevision = 0 // eventRevision（页面事件代次）：兼容没有任务修订号的测试接口。
  const receivedEvents = new Map<string, number>()

  // canManage（工作区管理权限）统一由正式程序提供；源码调试使用相同权限。
  const canManage = computed(() => port.available && workspace.value?.can_manage)
  const resourcesCanManage = computed(() => port.available)
  const activeWorkspaceJob = computed(() => [...workspaceJobs.value].reverse().find(job => !TERMINAL_JOB_STATES.has(job.state)))
  const latestWorkspaceJob = computed(() => workspaceJobs.value.at(-1) ?? null)

  function upsertResourceJob(incoming: ResourceJob, refreshAfterUpdate = true) {
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
    const request = (async () => {
      try {
        const jobs = typeof port.resourceJobSnapshots === 'function'
          ? await port.resourceJobSnapshots()
          : typeof port.environmentStatus === 'function' ? (await port.environmentStatus()).jobs ?? [] : []
        if (!mounted) return
        for (const job of jobs) {
          const known = resourceJobs.value.some(current => current.job_id === job.job_id)
          // 只恢复活动任务；已知任务则接收完成/失败快照，补救丢失的终态事件。
          if (!known && TERMINAL_JOB_STATES.has(job.state)) continue
          if ((receivedEvents.get(job.job_id) ?? 0) > startedAt && (job.created_at === undefined || job.revision === undefined)) continue
          upsertResourceJob(job)
        }
      } catch (error) {
        if (mounted) eventError.value = error instanceof Error ? error.message : '任务进度补查失败，将自动重试。'
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
      void refreshJobSnapshots().then(() => refreshInventory()).finally(() => { pollRunning = false; schedulePoll() })
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
    try {
      workspace.value = await port.workspaceInfo()
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
      let checkedRevision: number
      do {
        checkedRevision = inventoryRevision
        try {
          const inventory = await port.resourceInventory()
          if (!mounted) return
          // 请求途中组件完成启用时，丢弃旧结果并在同一等待链中重新读取。
          if (checkedRevision === inventoryRevision) {
            resources.value = inventory
            inventoryUpdatedAt.value = Date.now()
            resourceError.value = ''
          }
        } catch (error) {
          if (mounted) resourceError.value = error instanceof Error ? error.message : '无法读取资源清单。'
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
    let resourceUnlisten: (() => void) | undefined
    let workspaceUnlisten: (() => void) | undefined
    try {
      resourceUnlisten = await port.listen<ResourceJob>(
        RESOURCE_PROGRESS_EVENT,
        event => {
          if (!mounted) return
          receivedEvents.set(event.payload.job_id, ++eventRevision)
          upsertResourceJob(event.payload)
          schedulePoll()
        },
      )
      workspaceUnlisten = await port.listen<WorkspaceJob>(
        WORKSPACE_PROGRESS_EVENT,
        event => upsertWorkspaceJob(event.payload),
      )
      if (!mounted) {
        resourceUnlisten()
        workspaceUnlisten()
        return
      }
      unlistenResource = resourceUnlisten
      unlistenWorkspace = workspaceUnlisten
    } catch (error) {
      resourceUnlisten?.()
      workspaceUnlisten?.()
      eventError.value = error instanceof Error ? error.message : '无法订阅桌面任务进度。'
    }
  }

  onMounted(() => {
    mounted = true
    if (!port.available) return
    loading.value = true
    window.addEventListener('focus', refreshOnFocus)
    unlistenRequests = [subscribe(), refreshJobSnapshots(), refresh().then(() => undefined)]
    void Promise.all(unlistenRequests).finally(() => { loading.value = false; schedulePoll() })
  })

  onBeforeUnmount(() => {
    mounted = false
    if (pollTimer !== undefined) clearTimeout(pollTimer)
    pollTimer = undefined
    pollDueAt = 0
    window.removeEventListener('focus', refreshOnFocus)
    unlistenResource?.()
    unlistenWorkspace?.()
    unlistenResource = undefined
    unlistenWorkspace = undefined
  })

  async function chooseWorkspace() {
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
    if (!resourcesCanManage.value || startingResources.value.has(resourceId)
      || resourceJobs.value.some(job => job.resource_id === resourceId && !TERMINAL_JOB_STATES.has(job.state))
      || pendingDownloadPlan.value?.resource_id === resourceId) return
    startingResources.value = new Set(startingResources.value).add(resourceId)
    resourceError.value = ''
    try { pendingDownloadPlan.value = await port.resourceInstallPlan(resourceId) }
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

  async function importResource(resourceId?: string) {
    await startResourceJob(resourceId ?? 'batch', 'import', resourceId)
  }

  async function startResourceJob(resourceKey: string, operation: 'download' | 'import', resourceId?: string) {
    if (!resourcesCanManage.value || startingResources.value.has(resourceKey)
      || resourceJobs.value.some(job => job.resource_id === resourceKey && !TERMINAL_JOB_STATES.has(job.state))) return
    startingResources.value = new Set(startingResources.value).add(resourceKey)
    resourceError.value = ''
    try {
      const result = operation === 'download'
        ? await port.downloadResource(resourceKey)
        : await port.importResource(resourceId)
      if (!resourceJobs.value.some(job => job.job_id === result.job_id)) {
        upsertResourceJob({
          job_id: result.job_id,
          resource_id: resourceKey,
          state: 'queued',
          phase: operation === 'download' ? '正在准备下载' : resourceId ? '正在准备导入' : '正在准备批量导入',
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
    try {
      await port.openResourceSource(resourceId)
    } catch (error) {
      resourceError.value = error instanceof Error ? error.message : '无法打开上游来源页面。'
    }
  }

  async function cancelJob(jobId: string) {
    const resourceJob = resourceJobs.value.find(job => job.job_id === jobId)
    const workspaceJob = workspaceJobs.value.find(job => job.job_id === jobId)
    if ((!resourceJob && !workspaceJob) || resourceJob?.can_cancel === false || workspaceJob?.can_cancel === false) return
    try {
      await port.cancelResource(jobId)
    } catch (error) {
      const message = error instanceof Error ? error.message : '取消任务失败。'
      if (resourceJob) upsertResourceJob({ ...resourceJob, error: message })
      if (workspaceJob) upsertWorkspaceJob({ ...workspaceJob, error: message })
    }
  }

  async function confirmImport(accepted: boolean) {
    if (!pendingImport.value) return
    try { await port.confirmImport(pendingImport.value.job_id, accepted) }
    catch (failure) { resourceError.value = String(failure) }
  }
  return {
    pendingImport,
    confirmImport,
    portAvailable: port.available,
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
    startingResources,
    workspaceOperationStarting,
    refresh,
    refreshInventory,
    chooseWorkspace,
    linkWorkspace,
    migrateWorkspace,
    downloadResource,
    pendingDownloadPlan,
    confirmDownload,
    importResource,
    openResourceSource,
    cancelJob,
  }
}
