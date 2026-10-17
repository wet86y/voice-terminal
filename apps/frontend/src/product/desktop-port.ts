import { locateNativeInvoker } from '../runtime/desktop-preflight'
import type { FixedModelCredentialUpdate, FixedModelCredentials } from './types'
import type { FloatingSessionAction, FloatingWindowModel, FloatingWindowProjection } from './floating-window'

/** WorkspaceInfo（工作区信息）：由原生启动器返回的当前数据归属。 */
export interface WorkspaceInfo {
  /** root（当前工作区根目录）。 */
  root: string
  /** data_dir（用户数据目录）。 */
  data_dir: string
  /** version（应用版本）与 can_manage（是否开放工作区管理）。 */
  version: string
  can_manage: boolean
}

/** WorkspaceChoice（工作区选择结果）：选择器仅返回候选根目录，不会立即切换。 */
export interface WorkspaceChoice {
  root: string
}

/** ResourceStatus（独立资源状态）：与运行进程及内存采样分开。 */
export interface ResourceStatus {
  /** required（必需组件）：false（否）时缺失、损坏或待更新均不阻塞整体启动。 */
  required?: boolean
  /** files_installed（资源文件完整）、runtime_ready（运行依赖就绪）、can_start（可启动）。 */
  files_installed?: boolean
  runtime_ready?: boolean
  can_start?: boolean
  /** runtime_group（所需共享依赖组）。 */
  runtime_group?: string
  /** python（私有解释器路径）。 */
  python?: string
  /** version（版本号）：model（模型）资源表示随包目标模型版本；voice-server（语音服务）仍表示解释器版本。 */
  version?: string | null
  /** resource_id（固定资源标识）与 label（界面名称）。 */
  resource_id: string
  label: string
  /** installation_state（安装状态）：缺失、不完整或已安装。 */
  installation_state: 'missing' | 'incomplete' | 'installed'
  /** update_available（是否有必须处理的随包更新）；独立于 installation_state（安装状态），为真时前端阻止启动。旧宿主省略时按无更新处理。 */
  update_available?: boolean
  /** installed_version（已安装版本）；尚未安装或旧宿主未知时为空。 */
  installed_version?: string | null
  /** source_url（上游来源页面）与 error（最近校验或安装错误）。 */
  source_url?: string | null
  error?: string | null
}

/** ResourceJob（资源任务）：下载、导入及取消状态的桌面事件负载。 */
export interface ResourceJob {
  /** created_at（本次任务创建时间，毫秒）与 revision（状态修订号）：拒绝迟到的快照和旧重试事件。 */
  created_at?: number
  revision?: number
  /** job_id（任务标识）与 resource_id（当前处理资源）。 */
  job_id: string
  resource_id: string
  /** state（任务状态）与 phase（当前阶段说明）。 */
  state: string
  phase: string
  /** completed_bytes（已处理字节）及 total_bytes（总字节）；未知时为空。 */
  completed_bytes?: number | null
  total_bytes?: number | null
  /** error（失败原因）、can_cancel（当前能否取消）。 */
  error?: string | null
  can_cancel?: boolean
  /** installed_resources（批量任务成功安装的资源编号）。 */
  installed_resources?: string[]
  import_preview?: { resource_id: string; title: string; version: string; files_verified: boolean }[] | null // import_preview（整包识别和预校验结果）。
}

/** WorkspaceJob（工作区任务）：链接或迁移进度的桌面事件负载。 */
export interface WorkspaceJob {
  /** job_id（任务标识）。 */
  job_id: string
  /** state（任务状态）与 phase（复制、切换或清理阶段）。 */
  state: string
  phase: string
  /** completed_bytes（已复制字节）及 total_bytes（总字节）。 */
  completed_bytes?: number | null
  total_bytes?: number | null
  /** error（失败原因）；can_cancel=false 表示已进入不可取消切换阶段。 */
  error?: string | null
  can_cancel?: boolean
}

/** DesktopEvent（桌面事件封装）：遵循 Tauri event（桌面事件）回调格式。 */
export interface DesktopEvent<T> {
  event: string
  id: number
  payload: T
}

export const RESOURCE_PROGRESS_EVENT = 'desktop://resource-progress'
export const WORKSPACE_PROGRESS_EVENT = 'desktop://workspace-progress'

/** DesktopPort（桌面交互接口）：正式产品入口通过桌面预检后才调用原生命令。 */
export interface DesktopPort {
  /** available（是否存在原生命令桥）。 */
  available: boolean
  /** focus（聚焦）：显示桌面主窗口。 */
  focus(): Promise<void>;
  /** openLogs（打开日志）：显示本机应用日志目录。 */
  openLogs(): Promise<void>;
  /** openConsole（打开控制台）：由 Host（应用宿主）校验并在默认浏览器打开 DSH（代理执行宿主）；url（短期令牌地址）。 */
  openConsole(url: string): Promise<void>
  /** exit（退出）：由桌面宿主关闭应用。 */
  exit(): Promise<void>;
  /** publishFloatingModel（发布悬浮窗视图）：仅向本实例的浮窗发送限量动效数据。 */
  publishFloatingModel?(model: FloatingWindowProjection): Promise<void>
  /** getFloatingModel（读取悬浮窗视图）：返回原生宿主保存的最新动效快照。 */
  getFloatingModel?(): Promise<FloatingWindowModel>
  /** returnToWorkbench（切回工作台）：仅悬浮窗可调用。 */
  returnToWorkbench?(): Promise<void>
  /** startFloatingDrag（拖动悬浮窗）：仅悬浮窗可调用。 */
  startFloatingDrag?(): Promise<void>
  /** controlFloatingSession（控制语音输入）：只允许静默或复用现有开始录制流程。 */
  controlFloatingSession?(action: FloatingSessionAction): Promise<void>
  /** listen（订阅桌面事件）：仅接收当前原生窗口获准的事件。 */
  listen?<T>(eventName: string, handler: (event: DesktopEvent<T>) => void): Promise<() => void>
}

/** DesktopWorkspacePort（桌面工作区扩展）：资源准备阶段直接由原生入口提供。 */
export interface DesktopWorkspacePort {
  /** resource_install_plan（下载安装内容）：下载前确认模型与共享运行依赖。 */
  confirmImport(jobId: string, accepted: boolean): Promise<void> // confirmImport（确认导入识别结果）。
  resourceInstallPlan(resourceId: string): Promise<ResourceInstallPlan>
  /** workspace_info（工作区信息）：可在 Voice（语音服务）就绪前读取。 */
  workspaceInfo(): Promise<WorkspaceInfo>
  /** workspace_choose（选择工作区）：打开原生文件夹选择器，取消时返回 null。 */
  chooseWorkspace(): Promise<WorkspaceChoice | null>
  /** workspace_link（链接工作区）：初始化或切换到选择的目标根目录。 */
  linkWorkspace(targetRoot: string): Promise<{ job_id: string }>
  /** workspace_migrate（迁移工作区）：迁移完整数据并在成功后清理源位置。 */
  migrateWorkspace(targetRoot: string): Promise<{ job_id: string }>
  /** resource_inventory（资源清单）：返回固定模型与 DSH（代理执行宿主）的独立安装状态。 */
  resourceInventory(): Promise<ResourceStatus[]>
  /** resourceJobSnapshots（轻量任务快照）：定时补查进度，不执行环境扫描。 */
  resourceJobSnapshots?(): Promise<ResourceJob[]>
  /** resource_download（下载资源）：启动固定版本资源下载任务。 */
  downloadResource(resourceId: string): Promise<{ job_id: string }>
  /** resource_import（导入资源）：带 id 选择单资源；省略 id 时选择批量压缩包。 */
  importResource(resourceId?: string): Promise<{ job_id: string }>
  /** resource_open_source（打开上游）：由原生宿主打开资源来源页面。 */
  openResourceSource(resourceId: string): Promise<void>
  /** resource_cancel（取消资源任务）：资源任务与迁移复制阶段共用。 */
  cancelResource(jobId: string): Promise<void>
  /** listen（订阅桌面事件）：组件卸载时调用返回的 unlisten（取消订阅）函数。 */
  listen<T>(eventName: string, handler: (event: DesktopEvent<T>) => void): Promise<() => void>
}
export interface ResourceInstallPlan {
  resource_id: string // resource_id（资源编号）。
  title: string // title（资源名称）。
  model_bytes: number // model_bytes（模型或组件最大下载字节数）。
  shared_resources?: Array<{ resource_id: string; title: string; download_bytes: number }> // shared_resources（共用组件）：编号、名称与最大下载字节数。
  environment?: EnvironmentPlan | null // environment（解释器与共享依赖安装内容）。
}

/** ModelCapability（模型运行能力）：实际显卡推理通过后才开放选项。 */
export interface ModelCapability {
  gpu_available: boolean // gpu_available（显卡可用）。
  cpu_available?: boolean // cpu_available（中央处理器可用）。
  reason?: string | null // reason（不可用原因）。
  actual_inference?: boolean // actual_inference（已执行实际模型推理）。
}
/** VoiceEnvironmentStatus（语音环境状态）：文件、依赖、服务分别呈现。 */
export interface VoiceEnvironmentStatus {
  interpreter_ready: boolean // interpreter_ready（解释器存在）。
  service_ready: boolean // service_ready（基础依赖就绪）。
  python: string // python（私有解释器位置）。
  version?: string | null // version（解释器版本）。
  groups: Record<string, { ready: boolean; reason?: string }> // groups（共享依赖组）。
  jobs?: ResourceJob[] // jobs（持久资源任务）。
  gpu?: { path: string; models: Record<string, ModelCapability> } | null // gpu（登记显卡环境）。
}
/** EnvironmentPlan（安装内容）：锁定来源，安装前展示。 */
export interface EnvironmentPlan {
  group: string // group（依赖组）。
  already_ready: boolean // already_ready（已有环境完整匹配）。
  total_bytes: number // total_bytes（最大下载字节数，复用后减少）。
  wheels: Array<{ name: string; version: string; size: number }> // wheels（固定预编译依赖）。
  system_candidates: Array<{ path: string; accepted: boolean }> // system_candidates（本机解释器筛选结果）。
}
/** DesktopEnvironmentPort（原生环境接口）：无 Python（后端）时也能使用。 */
export interface DesktopEnvironmentPort {
  environmentStatus(): Promise<VoiceEnvironmentStatus>
  environmentPlan(group: string): Promise<EnvironmentPlan>
  configureEnvironment(group: string): Promise<{ job_id: string }>
  importEnvironment(): Promise<{ job_id: string }>
  registerGpu(): Promise<void>
  probeGpu(): Promise<{ job_id: string }>
  environmentGuide(): Promise<void>
  startVoiceService(): Promise<void>
  showManagement(): Promise<void>
}

/** DesktopCredentialsPort（原生接口密钥设置）：不需要 Python（后端运行环境）。 */
export interface DesktopCredentialsPort {
  /** fixedCredentials（读取配置状态）：只返回是否已配置，不返回密钥正文。 */
  fixedCredentials(): Promise<FixedModelCredentials>
  /** saveFixedCredentials（保存固定服务密钥）：空白保留已有值，系统凭据库保存正文。 */
  saveFixedCredentials(request: FixedModelCredentialUpdate): Promise<FixedModelCredentials>
}

export function desktopPort(): DesktopPort {
  const native = locateNativeInvoker()
  const call = <T = void>(command: string, args?: Record<string, unknown>): Promise<T> =>
    native
      ? native.invoke<T>(command, args).catch((error: unknown) => {
        // 原生接口拒绝时通常返回字符串；保留其已脱敏的中文原因供界面显示。
        throw error instanceof Error ? error : new Error(typeof error === 'string' ? error : '原生命令执行失败。')
      })
      : Promise.reject(new Error('此操作需要桌面窗口。'))
  const port: DesktopPort & DesktopWorkspacePort & DesktopEnvironmentPort & DesktopCredentialsPort = {
    available: Boolean(native),
    focus: () => call('focus_main'),
    openLogs: () => call('open_logs'),
    openConsole: url => call('open_console', { url }),
    exit: () => call('exit_application'),
    publishFloatingModel: model => call('publish_floating_model', { model }),
    getFloatingModel: () => call<FloatingWindowModel>('get_floating_model'),
    returnToWorkbench: () => call('return_to_workbench'),
    startFloatingDrag: () => call('start_floating_drag'),
    controlFloatingSession: action => call('control_floating_session', { action }),
    workspaceInfo: () => call<WorkspaceInfo>('workspace_info'),
    chooseWorkspace: () => call<WorkspaceChoice | null>('workspace_choose'),
    linkWorkspace: targetRoot => call<{ job_id: string }>('workspace_link', { target_root: targetRoot }),
    migrateWorkspace: targetRoot => call<{ job_id: string }>('workspace_migrate', { target_root: targetRoot }),
    resourceInventory: () => call<ResourceStatus[]>('resource_inventory'),
    resourceJobSnapshots: () => call<ResourceJob[]>('resource_jobs'),
    confirmImport: (jobId, accepted) => call('resource_import_confirm', { job_id: jobId, accepted }),
    resourceInstallPlan: resourceId => call('resource_install_plan', { resource_id: resourceId }),
    downloadResource: resourceId => call<{ job_id: string }>('resource_download', { resource_id: resourceId }),
    importResource: resourceId => call<{ job_id: string }>('resource_import', resourceId === undefined ? undefined : { resource_id: resourceId }),
    openResourceSource: resourceId => call('resource_open_source', { resource_id: resourceId }),
    cancelResource: jobId => call('resource_cancel', { job_id: jobId }),
    environmentStatus: () => call('voice_environment_status'),
    environmentPlan: group => call('voice_environment_plan', { group }),
    configureEnvironment: group => call('voice_environment_configure', { group }),
    importEnvironment: () => call('voice_environment_import'),
    registerGpu: () => call('gpu_environment_register'),
    probeGpu: () => call('gpu_environment_probe'),
    environmentGuide: () => call('environment_guide'),
    startVoiceService: () => call('desktop_start_service'),
    showManagement: () => call('desktop_manage'),
    fixedCredentials: () => call('model_credentials_status'),
    saveFixedCredentials: request => call('model_credentials_save', { request }),
    listen: async <T>(eventName: string, handler: (event: DesktopEvent<T>) => void) => {
      const eventWindow = typeof window === 'undefined' ? undefined : window as NativeEventWindow
      const eventRuntime = eventWindow?.__TAURI_INTERNALS__
      const eventPlugin = eventWindow?.__TAURI_EVENT_PLUGIN_INTERNALS__
      if (!eventRuntime?.transformCallback || !eventPlugin) {
        throw new Error('桌面事件接口不可用。')
      }
      const callbackId = eventRuntime.transformCallback((event: DesktopEvent<T>) => handler(event))
      const eventId = await eventRuntime.invoke<number>('plugin:event|listen', {
        event: eventName,
        target: { kind: 'Any' },
        handler: callbackId,
      })
      let active = true
      return () => {
        if (!active) return
        active = false
        eventPlugin.unregisterListener(eventName, eventId)
        void eventRuntime.invoke<void>('plugin:event|unlisten', { event: eventName, eventId })
          .catch(() => eventRuntime.unregisterCallback?.(callbackId))
      }
    },
  }
  return port
}

type NativeEventWindow = Window & {
  __TAURI_INTERNALS__?: ReturnType<typeof locateNativeInvoker>
  __TAURI_EVENT_PLUGIN_INTERNALS__?: {
    unregisterListener(event: string, eventId: number): void
  }
}
