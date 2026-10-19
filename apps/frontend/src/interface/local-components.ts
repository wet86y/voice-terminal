/** LocalComponentState（本地组件状态）：用于状态颜色与操作可用性。 */
export type LocalComponentState = 'ready' | 'loading' | 'failed' | 'disabled'

/** LocalComponentCategory（组件用途分类）：界面按稳定分类编号分组。 */
export type LocalComponentCategory = 'speech' | 'tts' | 'inference' | 'extensions' | 'voice-server'

/** FixedProviderId（固定在线服务编号）：密钥只通过后端或原生凭据接口保存。 */
export type FixedProviderId = 'deepseek' | 'minimax'

/** LocalProviderSettings（在线服务设置模型）：由主应用或启动管理器提供保存与用量回调。 */
export interface LocalProviderSettings {
  /** provider（服务商编号）：对应 DeepSeek（推理服务）或 MiniMax（语音合成服务）。 */
  provider: FixedProviderId
  /** configured（本机是否已有已保存密钥）。 */
  configured: boolean
  /** disabled（读取或保存时是否暂时禁用操作）。 */
  disabled?: boolean
  /** usageHint（用量查询说明）：在当前宿主不能查询时解释原因。 */
  usageHint?: string
  /** save（保存单个服务商密钥）：返回是否已配置及用户可见说明。 */
  save: (apiKey: string) => Promise<{ configured: boolean; message?: string }>
  /** usage（查询账户余额或订阅余量）：没有后端查询能力时省略。 */
  usage?: () => Promise<Record<string, unknown>>
}

/** LocalComponentRow（组件条目）：展示层只接收指标与选项，不直接调用业务。 */
export interface LocalComponentRow {
  /** id（稳定标识）与 name（组件名称）。 */
  id: string
  name: string
  /** category（用途分类）：缺省时由组件编号推导。 */
  category?: LocalComponentCategory
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
  /** resourceBytes（组件计量原始字节）：供用途分类汇总，避免先格式化再相加。 */
  resourceBytes?: {
    /** resident（常驻内存字节数）。 */
    resident?: number
    /** privateCommit（提交内存字节数）。 */
    privateCommit?: number
    /** vram（显存字节数）。 */
    vram?: number
  }
  /** description（详情说明）、error（异常原因）与 note（指标说明）。 */
  description: string
  error?: string
  note?: string
  /** settingsSection（关联设置分区）：组件需要专门编辑页时提供跳转目标。 */
  settingsSection?: string
  /** resource（固定资源状态）：安装操作独立于组件健康状态。 */
  resource?: ManagedResourceRow
  /** resourceChoices（可选模型）与 sharedResource（共用运行程序）：摘要显示状态，详情显示安装信息。 */
  resourceChoices?: ManagedResourceRow[]
  sharedResource?: ManagedResourceRow
  /** runtimeDependencies（语音服务运行依赖）：中央处理器必需，原生语音与显卡可选。 */
  runtimeDependencies?: ManagedResourceRow[]
  /** runtimeDependencyMetrics（依赖进程指标）：按依赖编号提供独占进程采样，避免把共享进程重复记到模型卡。 */
  runtimeDependencyMetrics?: Record<string, {
    /** resident（常驻内存）：该运行依赖独占进程的物理内存采样，单位为字节。 */
    resident?: number
    /** privateCommit（私有提交内存）：该运行依赖独占进程的提交内存采样，单位为字节。 */
    privateCommit?: number
    /** vram（显存）：该运行依赖独占进程的专用显存采样，单位为字节。 */
    vram?: number
  }>
  /** provider（在线服务配置）：用于在该组件详情中安全配置密钥与查询用量。 */
  provider?: LocalProviderSettings
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
  /** updateAvailable（存在必须处理的随包更新）、installedVersion（已安装版本）及 targetVersion（目标模型或组件版本）。 */
  updateAvailable?: boolean
  /** required（是否必需）：false（可选）不阻塞整体启动。 */
  required?: boolean
  installedVersion?: string | null
  targetVersion?: string | null
  /** error（最近下载、导入或完整性检查错误）。 */
  error?: string
  /** retryable（最近一次资源任务失败，可重新下载或导入）。 */
  retryable?: boolean
  /** busy（该资源正被任务处理）。 */
  busy?: boolean
}

/** LocalDownloadItem（分类下载项）：展示该分类包含的本机资源与安装状态。 */
export interface LocalDownloadItem {
  /** id（固定资源编号）：纯配置或内置组件没有独立资源编号。 */
  id?: string
  /** label（中文名称）。 */
  label: string
  /** state（安装状态）和 updateAvailable（是否需要更新）。 */
  state?: ManagedResourceRow['state']
  status?: string
  updateAvailable?: boolean
  busy?: boolean
  /** optional（用户可选项）：用于 VoxCPM1／VoxCPM2 等可选本地模型。 */
  optional?: boolean
  /** forcePlan（依赖待修复时强制读取安装计划）：本地语音模型文件已在，但共享运行程序尚未就绪。 */
  forcePlan?: boolean
  /** note（为何跳过或是否另行配置）。 */
  note?: string
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
  progressKind?: 'download' | 'verify' | 'install' | 'waiting' // progressKind（当前进度类型）。
  attempt?: number // attempt（自动恢复轮次）。
  parentJobId?: string | null // parentJobId（所属一键任务）。
  /** completedBytes（已处理字节）与 totalBytes（总字节）。 */
  completedBytes?: number | null
  totalBytes?: number | null
  /** download*（下载进度）；verification*（本地摘要校验进度）。 */
  downloadCompletedBytes?: number | null
  downloadTotalBytes?: number | null
  verificationCompletedBytes?: number | null
  verificationTotalBytes?: number | null
  /** error（失败原因）与 canCancel（当前是否允许取消）。 */
  error?: string
  canCancel: boolean
}

/** LocalComponentsModel（本地组件页面数据）：统一呈现总量、硬件与组件。 */
export interface LocalComponentsModel {
  /** ttsSelection（启动前播放目标）：只提供已就绪的本地选项，value（当前选择）可以尚未就绪。 */
  ttsSelection?: { value: 'minimax' | 'voxcpm1' | 'voxcpm2'; options: Array<{ value: string; label: string }>; saving: boolean; error?: string }
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
  /** downloadResources（固定资源安装状态）：供分类下载卡片显示内容及状态。 */
  downloadResources?: ManagedResourceRow[]
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
