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

/** useWorkspaceResources（工作区与资源控制器）：原生操作不依赖 Voice（语音服务）接口。 */
export function useWorkspaceResources() {
  const port = desktopPort() as DesktopPort & DesktopWorkspacePort & DesktopEnvironmentPort
  const workspace = ref<WorkspaceInfo | null>(null)
  const resources = ref<ResourceStatus[]>([])
  const resourceJobs = ref<ResourceJob[]>([])
  const workspaceJobs = ref<WorkspaceJob[]>([])
  const loading = ref(false)
  const refreshing = ref(false)
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
  let restoredNativeJobs = false // restoredNativeJobs（已恢复任务）：每个页面只读取一次原生任务快照。

  // canManage（工作区管理权限）统一由正式程序提供；源码调试使用相同权限。
  const canManage = computed(() => port.available && workspace.value?.can_manage)
  const resourcesCanManage = computed(() => port.available)
  const activeWorkspaceJob = computed(() => [...workspaceJobs.value].reverse().find(job => !TERMINAL_JOB_STATES.has(job.state)))
  const latestWorkspaceJob = computed(() => workspaceJobs.value.at(-1) ?? null)

  function upsertResourceJob(incoming: ResourceJob, refreshAfterUpdate = true) {
    const index = resourceJobs.value.findIndex(job => job.job_id === incoming.job_id)
    // 最近事件移到末尾，同一资源重新下载时不会继续展示旧任务。
    const next = [
      ...resourceJobs.value.filter(job => job.job_id !== incoming.job_id),
      index < 0 ? incoming : { ...resourceJobs.value[index], ...incoming },
    ]
    resourceJobs.value = next.slice(-MAX_VISIBLE_JOBS)
    if (refreshAfterUpdate && (incoming.installed_resources?.length || TERMINAL_JOB_STATES.has(incoming.state))) {
      void refreshInventory()
    }
  }

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

  async function refreshInventory() {
    if (!port.available) return
    try {
      const [inventory, environment] = await Promise.all([
        port.resourceInventory(),
        !restoredNativeJobs && typeof port.environmentStatus === 'function' ? port.environmentStatus().catch(() => null) : null,
      ])
      resources.value = inventory
      if (environment) restoredNativeJobs = true
      // 维护时切换到管理页仍恢复活动任务，已经结束的历史任务不覆盖现有安装状态。
      for (const job of environment?.jobs ?? []) {
        if (!TERMINAL_JOB_STATES.has(job.state) && !resourceJobs.value.some(current => current.job_id === job.job_id)) {
          upsertResourceJob(job, false)
        }
      }
      resourceError.value = ''
    } catch (error) {
      resourceError.value = error instanceof Error ? error.message : '无法读取资源清单。'
    }
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
        event => upsertResourceJob(event.payload),
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
    unlistenRequests = [subscribe(), refresh().then(() => undefined)]
    void Promise.all(unlistenRequests).finally(() => { loading.value = false })
  })

  onBeforeUnmount(() => {
    mounted = false
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
    resourceError.value = ''
    try { pendingDownloadPlan.value = await port.resourceInstallPlan(resourceId) }
    catch (error) { resourceError.value = String(error) }
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
    resourceError,
    workspaceError,
    eventError,
    startingResources,
    workspaceOperationStarting,
    refresh,
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
