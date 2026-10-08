/** LocalComponentState（本地组件状态）：用于状态颜色与操作可用性。 */
export type LocalComponentState = 'ready' | 'loading' | 'failed' | 'disabled'

/** LocalComponentRow（组件条目）：展示层只接收指标与选项，不直接调用业务。 */
export interface LocalComponentRow {
  /** id（稳定标识）与 name（组件名称）。 */
  id: string
  name: string
  /** state（状态类别）、status（状态文案）与 available（详情是否可用）。 */
  state: LocalComponentState
  status: string
  available: boolean
  /** retryable（是否允许重试）与 busy（是否正在恢复）。 */
  retryable?: boolean
  busy?: boolean
  /** runtime（推理或执行环境）、residentMemory（常驻内存）与 privateMemory（提交内存）。 */
  runtime: string
  residentMemory?: string
  privateMemory?: string
  /** vram（进程专用显存占用）：没有有效测量时省略。 */
  vram?: string
  /** description（详情说明）、error（异常原因）与 note（指标说明）。 */
  description: string
  error?: string
  note?: string
  /** resource（固定资源状态）：安装操作独立于组件健康状态。 */
  resource?: ManagedResourceRow
  /** location（当前运行位置）：没有可调整设备选项时显示。 */
  location?: string
  /** facts（补充指标）：label（名称）与 value（数值）。 */
  facts: Array<{ label: string; value: string }>
  /** device（运行位置选项）：value（草稿）、current（实际位置）、pending（尚未应用）。 */
  device?: {
    value: string
    current: string
    pending?: boolean
    draft?: boolean // draft（未保存设备草稿），不代表待启动生效配置。
    locked?: boolean // locked（环境检查或组件未就绪，暂停修改）。
    /** options（设备选项）：value（标识）、label（文案）、disabled（不可选）。 */
    options: Array<{ value: string; label: string; disabled?: boolean }>
    /** hint（设备限制说明）。 */
    hint?: string
  }
}

/** ManagedResourceRow（受管独立资源）：文件安装状态与进程健康分开呈现。 */
export interface ManagedResourceRow {
  /** id（固定资源编号）、label（显示名称）与状态中文文案。 */
  id: string
  label: string
  state: 'missing' | 'incomplete' | 'installed'
  status: string
  /** error（最近下载、导入或完整性检查错误）。 */
  error?: string
  /** retryable（最近一次资源任务失败，可重新下载或导入）。 */
  retryable?: boolean
  /** busy（该资源正被任务处理）。 */
  busy?: boolean
}

/** ManagedResourceJob（资源任务进度）：前端从桌面事件投影出的展示信息。 */
export interface ManagedResourceJob {
  /** id（任务标识）、resourceId（当前资源或批量导入）及 label（任务名称）。 */
  id: string
  resourceId: string
  label: string
  /** state（任务状态）与 phase（当前阶段）。 */
  state: string
  phase: string
  /** completedBytes（已处理字节）与 totalBytes（总字节）。 */
  completedBytes?: number | null
  totalBytes?: number | null
  /** error（失败原因）与 canCancel（当前是否允许取消）。 */
  error?: string
  canCancel: boolean
}

/** LocalComponentsModel（本地组件页面数据）：统一呈现总量、硬件与组件。 */
export interface LocalComponentsModel {
  /** status（整体状态）、state（状态类别）与 sampledAt（采样时间）。 */
  status: string
  state: LocalComponentState
  sampledAt: string
  /** resources（资源摘要）：id（标识）、label（名称）、used（已用）与 total（全部）。 */
  resources: Array<{ id: string; label: string; used?: string; total?: string }>
  /** managedResourceJobs（资源下载、导入任务进度）。 */
  managedResourceJobs?: ManagedResourceJob[]
  /** resourceActionsAvailable（桌面入口是否允许资源下载与导入）。 */
  resourceActionsAvailable?: boolean
  /** resourceLoading（是否正在读取独立资源清单）。 */
  resourceLoading?: boolean
  /** resourceError（资源任务或原生清单错误）。 */
  resourceError?: string
  /** resourceEventError（桌面进度事件订阅错误）。 */
  resourceEventError?: string
  /** resourceTasksBusy（是否已有资源任务运行，防止重复导入覆盖）。 */
  resourceTasksBusy?: boolean
  /** resourceBatchRetry（最近一次批量导入失败）。 */
  resourceBatchRetry?: boolean
  /** rows（组件列表）、error（页面异常）与 refreshing（正在刷新）。 */
  rows: LocalComponentRow[]
  error?: string
  refreshing?: boolean
}

/** formatComponentMemory（内存格式）：界面仅使用 MB／GB，保留现有字节换算口径。 */
export function formatComponentMemory(value: number | null | undefined): string | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return undefined
  return value >= 1024 ** 3
    ? `${Number((value / 1024 ** 3).toFixed(2))} GB`
    : `${Number((value / 1024 ** 2).toFixed(1))} MB`
}
