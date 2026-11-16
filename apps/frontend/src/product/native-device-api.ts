import { locateNativeInvoker } from '../runtime/desktop-preflight'

/** DeviceAudioEndpoint（默认音频端点）：本机系统混音格式与设备周期。 */
export interface DeviceAudioEndpoint {
  flow: 'capture' | 'render' // flow（端点方向）：采集或播放。
  endpoint_id: string // endpoint_id（Windows 默认音频端点编号）。
  sample_rate: number // sample_rate（系统混音采样率，每秒采样数）。
  channels: number // channels（系统混音声道数）。
  bits_per_sample: number // bits_per_sample（每采样位数）。
  format_tag: number // format_tag（WAVEFORMATEX 格式编号）。
  block_align: number // block_align（每音频帧字节数）。
  average_bytes_per_second: number // average_bytes_per_second（每秒平均字节数）。
  default_period_100ns: number // default_period_100ns（默认设备周期，100 纳秒单位）。
  minimum_period_100ns: number // minimum_period_100ns（最小设备周期，100 纳秒单位）。
}

/** CapturedPcmFrame（本机采集帧）：固定 20 ms、小端 16 位单声道 PCM。 */
export interface CapturedPcmFrame {
  sequence: number // sequence（连续采集帧编号）。
  start_sample: number // start_sample（16 kHz 设备流首个采样位置）。
  device_time_us: number // device_time_us（设备单调采集时刻，微秒）。
  discontinuity_before: boolean // discontinuity_before（本帧前是否检测到采样缺口）。
  pcm_le_bytes: number[] // pcm_le_bytes（小端 16 位单声道 PCM 字节数组）。
  audio_processing: NativeAudioProcessingFrame // audio_processing（当前帧 AEC、降噪、近端语音及打断保护状态）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** NativeAudioProcessingState（原生前处理状态）：不得把缺少反向参考报告为受保护。 */
export interface NativeAudioProcessingState {
  state: 'ready' | 'degraded' | 'unavailable' | 'stopped' // state（AEC、降噪与近端 VAD 状态）。
  error?: string | null // error（前处理故障说明；健康时为空）。
  reverse_generation?: number // reverse_generation（播放参考格式代次）。
  reverse_sample_rate?: number // reverse_sample_rate（播放参考源采样率）。
  playback_delay_ms?: number | null // playback_delay_ms（限制在 500 毫秒内的延迟估计）。
  protected_interruption_available?: boolean // protected_interruption_available（是否有新鲜且时延有效的反向参考）。
  fields?: Record<string, string> // fields（状态字段中文说明）。
}

/** NativeAudioProcessingFrame（采集帧前处理结论）：状态与 PCM 同帧返回。 */
export interface NativeAudioProcessingFrame extends NativeAudioProcessingState {
  near_end_voice?: boolean | null // near_end_voice（近端 VAD（语音活动检测）结果；不可用时为空）。
}

/** NativePlaybackSnapshot（本机播放快照）：区别入队、端点提交与设备时钟估计。 */
export interface NativePlaybackSnapshot {
  enqueued_ms?: number // enqueued_ms（已受理播放时长，毫秒）。
  played_ms?: number // played_ms（已提交给 WASAPI 端点的内容时长，毫秒）。
  buffered_ms?: number // buffered_ms（端点与本地排队时长，毫秒）。
  output_estimated_ms?: number // output_estimated_ms（端点设备时钟输出估计，毫秒）。
  started?: boolean // started（设备时钟是否已开始推进）。
  device_time_us?: number // device_time_us（设备时钟对应 QPC 时刻，微秒）。
  playing: boolean // playing（系统默认播放端点是否已打开）。
  fields?: Record<string, string> // fields（字段中文说明）。
}

/** DeviceConfig（设备角色配置）：独立于产品业务设置第 20 版。 */
export interface DeviceConfig {
  schema_version: 2 // schema_version（设备配置格式版本）。
  device_id: string // device_id（稳定本机设备编号）。
  desired_role: 'standalone' | 'shared_host' | 'remote_client' // desired_role（期望运行角色）。
  audio_mode: 'pcm' | 'opus_experimental' // audio_mode（PCM 或实验 Opus 音频档）。
  host: DeviceHostTrust | null // host（已核对主机的公开身份和本机凭据引用）。
}

/** DeviceHostTrust（设备主机信任）：不包含口令或私钥。 */
export interface DeviceHostTrust {
  address: string // address（SSH 主机名称或 IP 地址）。
  port: number // port（SSH 端口）。
  username: string // username（受管 SSH 用户名）。
  host_key_sha256: string // host_key_sha256（用户明确确认的主机指纹）。
  credential_ref: string // credential_ref（Windows 凭据库引用，不是秘密内容）。
  transport_credential_ref?: string // transport_credential_ref（独立 SSH 登录密钥引用；省略时沿用产品设备身份）。
}

/** DeviceConfigStatus（设备配置状态）：损坏原件只报告状态，不自动替换。 */
export interface DeviceConfigStatus {
  local_core_available?: boolean // local_core_available（本机核心解释器入口是否存在；不代表模型与核心已就绪）。
  runtime: { config_state: 'missing' | 'valid' | 'invalid'; device_id: string | null; desired_role: DeviceConfig['desired_role'] | null; audio_mode: DeviceConfig['audio_mode'] | null; error: string | null }
  device_config: DeviceConfig | null
  device_runtime?: {
    actual_role: 'standalone' | 'shared_host' | 'remote_client' | 'none' // actual_role（已就绪的实际角色；none 表示目标模式未就绪）。
    core_ready: boolean // core_ready（权威语音核心是否已就绪）。
    device_gateway: {
      enabled: boolean // enabled（本次运行是否请求共享设备网关）。
      ready: boolean // ready（共享设备网关是否已监听）。
      error: string | null // error（网关未就绪的非敏感原因）。
    } // device_gateway（本次运行的共享网关状态）。
    remote_client_connected: boolean // remote_client_connected（已认证的固定 SSH 主机连接状态）。
    fields: Record<string, string> // fields（字段中文说明）。
  }
  fields: Record<string, string>
}

/** OpenSshServerStatus（OpenSSH Server 状态）：只读字段；不会代表 SSH 已从其他设备可达。 */
export interface OpenSshServerStatus {
  feature_id: string // feature_id（固定 Windows 系统功能编号）。
  capability_state: 'Installed' | 'NotPresent' | 'InstallPending' | 'UninstallPending' | 'Staged' | 'Unknown' // capability_state（Windows 可选功能状态；Unknown 表示只读查询未提权）。
  service_present: boolean // service_present（sshd 系统服务是否存在）。
  service_state: string | null // service_state（sshd 当前服务状态）。
  startup_type: string | null // startup_type（sshd 当前启动类型，只读）。
  ready: boolean // ready（功能已安装且 sshd 正在运行）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** ManagedResourceJob（受管任务快照）：功能管理操作与普通组件任务使用同一只读进度格式。 */
export interface ManagedResourceJob {
  job_id: string // job_id（受管任务编号）。
  resource_id: string // resource_id（组件或系统功能编号）。
  state: string // state（任务状态）。
  phase: string // phase（当前任务阶段）。
  error: string | null // error（失败说明或空值）。
  can_cancel: boolean // can_cancel（任务是否允许取消）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** RemoteDeviceSnapshot（共享主机设备快照）：只含状态，不包含配对或凭据信息。 */
export interface RemoteDeviceSnapshot {
  protocol_version: 1 // protocol_version（设备网关协议版本）。
  ready: boolean // ready（核心是否完成启动）。
  active_device_id: string | null // active_device_id（当前获准语音交互的设备编号）。
  interaction_generation: number // interaction_generation（当前语音占用代次；切换时递增）。
  devices: Array<{
    device_id: string // device_id（稳定设备编号）。
    name: string // name（设备显示名称）。
    connected: boolean // connected（主机登记的连接状态）。
    connection_generation: number // connection_generation（连接代次）。
    executor_generation: number // executor_generation（设备执行器代次）。
    capabilities: string[] // capabilities（设备能力编号）。
    error: string | null // error（设备错误摘要）。
    interaction_active: boolean // interaction_active（当前语音交互占用状态）。
  }> // devices（主机登记的设备列表）。
  capability_labels: Record<string, string> // capability_labels（能力编号的中文说明）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** NativeSshIdentity（本机 SSH 身份）：私钥始终留在 Windows 凭据库。 */
export interface NativeSshIdentity {
  credential_ref: string // credential_ref（本机凭据引用）。
  public_key: string // public_key（给主机登记的 OpenSSH 公钥）。
  fingerprint: string // fingerprint（本设备公钥 SHA-256 指纹）。
}

/** NativeSshTransportIdentity（SSH 登录身份）：只返回公开公钥资料。 */
export interface NativeSshTransportIdentity {
  credential_ref: string // credential_ref（SSH 登录密钥在 Windows 凭据库中的引用）。
  public_key: string // public_key（需由主机操作系统 SSH 账户单独准入的公钥）。
  fingerprint: string // fingerprint（登录公钥 SHA-256 指纹）。
}

/** nativeDeviceApi（原生设备接口）：只显式调用，不在模块加载时启动进程或打开端点。 */
export const nativeDeviceApi = {
  deviceConfigStatus: () => call<DeviceConfigStatus>('device_config_status'),
  saveDeviceConfig: (config: DeviceConfig, preserveInvalid = false) => call<DeviceConfigStatus>(
    'device_config_save', { config, preserveInvalid },
  ),
  /** openSshServerStatus（读取 OpenSSH Server 状态）：仅共享主机管理页显式读取。 */
  openSshServerStatus: () => call<OpenSshServerStatus>('resource_system_feature_status'),
  /** enableOpenSshServer（启动受管系统功能任务）：调用前由界面要求确认 Windows 安装副作用。 */
  enableOpenSshServer: () => call<{ job_id: string; fields: Record<string, string> }>('resource_system_feature_enable'),
  /** resourceJobs（读取任务快照）：只读取已登记的受管任务。 */
  resourceJobs: () => call<ManagedResourceJob[]>('resource_jobs'),
  probeSshHost: (address: string, port: number) => call<{ fingerprint: string; trusted: false; fields: Record<string, string> }>(
    'device_ssh_probe', { address, port },
  ),
  createSshIdentity: (deviceId: string) => call<NativeSshIdentity>('device_ssh_identity_create', { deviceId }),
  sshIdentityStatus: (deviceId: string) => call<NativeSshIdentity | null>('device_ssh_identity_status', { deviceId }),
  deleteSshIdentity: (deviceId: string) => call<void>('device_ssh_identity_delete', { deviceId }),
  importSshTransportIdentity: (path: string) => call<NativeSshTransportIdentity>(
    'device_ssh_transport_identity_import', { path },
  ),
  sshTransportIdentityStatus: (credentialRef: string) => call<NativeSshTransportIdentity | null>(
    'device_ssh_transport_identity_status', { credentialRef },
  ),
  deleteSshTransportIdentity: (credentialRef: string) => call<void>(
    'device_ssh_transport_identity_delete', { credentialRef },
  ),
  connectSshHost: (timeoutMs?: number) => call<{ state: 'ready'; device_id: string; address: string; port: number; host_key_sha256: string; gateway_port: number; fields: Record<string, string> }>(
    'device_ssh_connect', { timeoutMs },
  ),
  requestSshGateway: (path: '/v1/status' | '/v1/devices', body: Record<string, unknown>, timeoutMs?: number) => call<{ status: number; body: Record<string, unknown> }>(
    'device_ssh_request', { path, body, timeoutMs },
  ),
  /** submitSshPairingRequest（提交 SSH 配对申请）：本机私钥仅由原生层读取并签名。 */
  submitSshPairingRequest: (pairingCode: string, timeoutMs = 20_000) => call<{
    status: number // status（SSH 网关返回的 HTTP 状态码）。
    body: Record<string, unknown> // body（配对请求编号、设备指纹及主机回执）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_ssh_pairing_request', { pairingCode, timeoutMs }),
  remoteDeviceSnapshot: async () => {
    const response = await call<{ status: number; body: RemoteDeviceSnapshot }>(
      'device_ssh_request', { path: '/v1/devices', body: { protocol_version: 1 }, timeoutMs: 10_000 },
    )
    if (response.status < 200 || response.status >= 300 || response.body?.protocol_version !== 1
      || typeof response.body.ready !== 'boolean'
      || !(response.body.active_device_id === null || typeof response.body.active_device_id === 'string')
      || !Number.isInteger(response.body.interaction_generation)
      || !Array.isArray(response.body.devices)) {
      throw new Error(`共享主机设备快照无效（HTTP ${response.status}）。`)
    }
    return response.body
  },
  remoteTaskChannel: (action: 'start' | 'status' | 'stop') => call<{
    state: 'running' | 'stopped' | 'error' // state（远端桌面任务通道状态）。
    stopped?: boolean // stopped（本次是否停止了轮询任务）。
    device_id?: string // device_id（已登记的远端设备编号）。
    connection_generation?: number // connection_generation（主机授予的连接代次）。
    executor_generation?: number // executor_generation（主机授予的执行器代次）。
    error?: string | null // error（最近一次通道错误）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_ssh_task_channel', { action }),
  remoteMediaChannel: (action: 'start' | 'status' | 'stop') => call<{
    state: 'running' | 'stopped' | 'error' // state（远端语音媒体通道状态）。
    stopped?: boolean // stopped（本次是否关闭原媒体流）。
    device_id?: string | null // device_id（当前语音交互设备编号）。
    stream_id?: string | null // stream_id（连续音频流编号）。
    interaction_generation?: number // interaction_generation（主机授予的语音占用代次）。
    capture_active?: boolean // capture_active（核心是否要求本机采集）。
    error?: string | null // error（最近一次媒体错误）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_ssh_media_channel', { action }),
  disconnectSshHost: () => call<{
    state: 'disconnected'; disconnected: boolean; media_channel_stopped?: boolean; task_channel_stopped?: boolean; fields: Record<string, string>
  }>('device_ssh_disconnect'),
  sshHostStatus: () => call<{ state: 'connected' | 'disconnected' | 'unavailable'; connected: boolean; fields: Record<string, string> }>('device_ssh_status'),
  startWorker: () => call<{ state: 'ready'; fields: Record<string, string> }>('device_worker_start'),
  statusWorker: () => call<{ state: 'ready' | 'stopped'; fields: Record<string, string> }>('device_worker_status'),
  desktopHelperHealth: () => call<{
    state: 'ready' | 'unavailable' | 'unsupported' | 'stopped' // state（桌面辅助进程状态）。
    ok?: boolean // ok（辅助健康请求是否成功）。
    data?: Record<string, unknown> // data（辅助进程健康回执或不可用原因）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_worker_desktop_helper_health'),
  audioDevices: () => call<{ state: 'ready' | 'stopped'; devices: DeviceAudioEndpoint[]; fields: Record<string, string> }>('device_worker_audio_devices'),
  captureStart: () => call<{
    state: 'capturing' // state（麦克风采集流状态）。
    capturing: true // capturing（默认麦克风已显式打开）。
    audio_processing: NativeAudioProcessingState // audio_processing（原生 AEC、降噪和 VAD 能力状态）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_worker_capture_start'),
  captureNext: (waitMs = 20) => call<{ frame: CapturedPcmFrame | null; fields: Record<string, string> }>(
    'device_worker_capture_next', { waitMs },
  ),
  captureStop: () => call<{ state: 'stopped'; fields: Record<string, string> }>('device_worker_capture_stop'),
  playbackStart: () => call<{
    state: 'playing' // state（扬声器播放流状态）。
    playing: true // playing（默认输出端点已显式打开）。
    audio_processing: NativeAudioProcessingState // audio_processing（AEC 反向参考状态）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_worker_playback_start'),
  playbackEnqueue: (sampleRate: 16_000 | 24_000 | 32_000 | 48_000, pcmLeBytes: Uint8Array) => call<{
    enqueued_ms: number // enqueued_ms（已受理播放时长，毫秒）。
    audio_processing: NativeAudioProcessingState // audio_processing（AEC 反向参考与处理健康状态）。
    playback_snapshot_error: string | null // playback_snapshot_error（读取播放缓冲延迟失败说明）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_worker_playback_enqueue', { sampleRate, pcmLeBytes: Array.from(pcmLeBytes) }),
  playbackSnapshot: () => call<NativePlaybackSnapshot>('device_worker_playback_snapshot'),
  playbackStop: () => call<{ playing: false; fields: Record<string, string> }>('device_worker_playback_stop'),
}

function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const native = locateNativeInvoker()
  if (!native) return Promise.reject(new Error('此操作需要桌面窗口。'))
  return native.invoke<T>(command, args).catch((error: unknown) => {
    throw error instanceof Error ? error : new Error(typeof error === 'string' ? error : '原生设备命令执行失败。')
  })
}
