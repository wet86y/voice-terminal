import { locateNativeInvoker } from '../runtime/desktop-preflight'
import type { ProductLocalTtsModelId, ProductTtsAsset, RemoteTtsAssetImportCancelled } from '../interface/tts-voice-types'

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

/** DeviceConfig（设备角色配置）：第 3 版同时兼容旧角色投影。 */
export interface DeviceConfig {
  schema_version: 3 // schema_version（设备配置格式版本）。
  device_id: string // device_id（稳定本机设备编号）。
  mode: 'local' | 'remote' // mode（本地运行或远程配对）。
  broadcast_enabled: boolean // broadcast_enabled（本机是否广播并接受配对）。
  client_online_tts_local: boolean // client_online_tts_local（在线语音是否由客户端本地合成播放）。
  audio_compression: boolean // audio_compression（远程音频是否压缩）。
  reconnect_suspended_reason: string | null // reconnect_suspended_reason（自动重连暂停原因）。
  desired_role: 'standalone' | 'shared_host' | 'remote_client' // desired_role（期望运行角色）。
  audio_mode: 'pcm' | 'opus_experimental' // audio_mode（PCM 或实验 Opus 音频档）。
  paired_host: DeviceHostTrust | null // paired_host（已配对服务端身份、地址及凭据引用）。
  host?: DeviceHostTrust | null // host（仅读取旧界面测试和旧响应；正式保存使用 paired_host）。
}

/** DeviceHostTrust（设备主机信任）：不包含口令或私钥。 */
export interface DeviceHostTrust {
  host_id: string // host_id（从 mDNS（局域网服务发现）取得的稳定服务端编号）。
  display_name: string // display_name（服务端显示名称）。
  addresses: string[] // addresses（配对时发现并验证的局域网地址记录）。
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
  runtime: { config_state: 'missing' | 'valid' | 'invalid'; device_id: string | null; mode?: DeviceConfig['mode'] | null; broadcast_enabled?: boolean; desired_role: DeviceConfig['desired_role'] | null; audio_mode: DeviceConfig['audio_mode'] | null; error: string | null }
  device_config: DeviceConfig | null
  device_runtime?: {
    operation?: {
      active: boolean // active（宿主仍在执行设备设置，页面切换不会取消）。
      revision: number // revision（设备设置任务序号）。
      state_revision?: number // state_revision（任务进度快照递增号，用于拒绝迟到回执）。
      phase: string // phase（宿主执行阶段）。
      message: string // message（阶段中文说明）。
      target_mode: 'local' | 'remote' // target_mode（本次任务目标运行模式）。
      target_broadcast: boolean // target_broadcast（本次任务目标广播开关）。
      error: string | null // error（上次任务失败的非敏感说明）。
    } | null // operation（跨页面接续的原生设备任务）。
    mode?: DeviceConfig['mode'] | null // mode（实际选择的本地或远程模式）。
    broadcast_enabled?: boolean // broadcast_enabled（已保存的广播意图）。
    expected_mode?: DeviceConfig['mode'] | null // expected_mode（已保存的目标运行模式）。
    actual_mode?: DeviceConfig['mode'] | null // actual_mode（当前真实运行模式；未就绪时为空）。
    switch_phase?: 'ready' | 'waiting_for_remote' | 'starting_local' | 'stopped' | 'failed' // switch_phase（运行模式切换阶段）。
    error?: string | null // error（模式切换或广播发布的最近故障）。
    broadcast_requested?: boolean // broadcast_requested（已保存的局域网广播意图）。
    broadcast_actual?: boolean // broadcast_actual（广播网关实际就绪状态）。
    revision?: number // revision（运行状态修订号）。
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
  discovery_access_enabled?: boolean // discovery_access_enabled（本程序多播发现规则是否启用）。
  discovery_access_valid?: boolean // discovery_access_valid（UDP 5353 发现端口的程序和子网复核是否通过）。
  feature_id: string // feature_id（固定 Windows 系统功能编号）。
  capability_state: 'Installed' | 'NotPresent' | 'InstallPending' | 'UninstallPending' | 'Staged' | 'Unknown' // capability_state（Windows 可选功能状态；Unknown 表示只读查询未提权）。
  service_present: boolean // service_present（sshd 系统服务是否存在）。
  service_state: string | null // service_state（sshd 当前服务状态）。
  startup_type: string | null // startup_type（sshd 当前启动类型，只读）。
  source_path: string | null // source_path（OpenSSH Server 程序文件路径）。
  source_version: string | null // source_version（OpenSSH Server 文件版本）。
  signature_status: string | null // signature_status（程序签名验证状态）。
  signature_subject: string | null // signature_subject（程序签名主体）。
  official_binary: boolean // official_binary（系统路径和微软签名是否通过检查）。
  service_path_official: boolean // service_path_official（服务程序路径是否为微软系统文件）。
  effective_port: number | null // effective_port（sshd 有效监听端口配置）。
  listening_ports: number[] // listening_ports（sshd 进程实际监听端口）。
  configuration_valid: boolean // configuration_valid（sshd 有效配置校验状态）。
  authorized_keys_path: string | null // authorized_keys_path（当前账户授权公钥文件路径）。
  authorized_keys_exists: boolean // authorized_keys_exists（当前账户授权公钥文件是否存在）。
  account_name: string | null // account_name（当前 Windows 登录账户名）。
    account_ssh_username: string | null // account_ssh_username（由账户 SID 解析的 SSH 登录名；不使用邮箱或用户目录名）。
  account_sid: string | null // account_sid（当前 Windows 登录账户 SID）。
  account_is_admin: boolean // account_is_admin（当前账户是否属于本机管理员组）。
  account_is_entra: boolean // account_is_entra（当前身份是否为不受 Windows OpenSSH 支持的 Entra 账户）。
  project_access_enabled: boolean // project_access_enabled（本程序受限防火墙规则是否启用）。
  project_access_valid: boolean // project_access_valid（程序、SSH 端口和精确局域网范围均通过复核）。
  project_network_scope: string[] // project_network_scope（本程序规则允许的局域网子网）。
  pairing_access_enabled: boolean // pairing_access_enabled（配对服务专用规则是否启用）。
  pairing_access_valid: boolean // pairing_access_valid（程序、固定端口和精确局域网范围均通过复核）。
  pairing_network_scope: string[] // pairing_network_scope（配对规则允许的局域网范围）。
  existing_ssh_inbound_rules: string[] // existing_ssh_inbound_rules（其他启用且可能匹配 SSH 的入站规则；只读报告，不修改）。
  local_lan_addresses: string[] // local_lan_addresses（活动物理网卡的局域网地址）。
  local_lan_scopes: string[] // local_lan_scopes（活动物理网卡的精确 CIDR 子网范围）。
  ready: boolean // ready（微软程序来源、配置、服务及实际端口均通过基础检查）。
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

/** RemoteSshSystemFeatureJob（服务端 OpenSSH 任务）：只暴露固定任务状态，不返回原生路径或账户材料。 */
export interface RemoteSshSystemFeatureJob {
  job_id: 'system-feature-openssh-server' // job_id（固定 OpenSSH Server 系统功能任务编号）。
  resource_id: 'openssh-server' // resource_id（固定 OpenSSH Server 系统功能编号）。
  state: 'queued' | 'running' | 'cancelling' | 'complete' | 'failed' | 'cancelled' // state（受管任务状态）。
  phase: string // phase（服务端系统功能任务阶段，含管理员确认等待阶段）。
  error: string | null // error（服务端任务失败原因）。
  can_cancel: boolean // can_cancel（当前任务是否允许取消）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** RemoteSshSystemFeatureSnapshot（服务端 SSH 功能快照）：ready（是否已就绪）只由服务端只读检查结果决定。 */
export interface RemoteSshSystemFeatureSnapshot {
  status: OpenSshServerStatus // status（服务端 Windows OpenSSH Server 完整检查快照）。
  job: RemoteSshSystemFeatureJob | null // job（固定安装或启动任务；空值表示无活动或历史任务）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** RemoteVoiceForwarderResult（远程语音回环转发状态）：仅向 WebSocket 连接器提供受限本机地址。 */
export type RemoteVoiceForwarderResult =
  | { state: 'running'; url: string; fields: Record<string, string> } // url（本机 127.0.0.1 回环转发的 WebSocket 地址）。
  | { state: 'stopped'; fields: Record<string, string> } // stopped（本机远程语音转发已停止）。

/** parseRemoteSshSystemFeatureSnapshot（校验服务端 SSH 快照）：不接受缺失或类型错误的状态字段。 */
export function parseRemoteSshSystemFeatureSnapshot(value: unknown): RemoteSshSystemFeatureSnapshot {
  if (!isRecord(value) || !isOpenSshServerStatus(value.status)
    || !(value.job === null || isRemoteSshSystemFeatureJob(value.job)) || !isStringMap(value.fields)) {
    throw new Error('服务端 SSH 系统功能回执字段不完整或类型无效。')
  }
  return value as unknown as RemoteSshSystemFeatureSnapshot
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string')
}

function isStringMap(value: unknown): value is Record<string, string> {
  return isRecord(value) && Object.values(value).every(item => typeof item === 'string')
}

function isLoopbackVoiceForwarderUrl(value: unknown): value is string {
  if (typeof value !== 'string') return false
  try {
    const url = new URL(value)
    const parts = url.pathname.split('/').filter(Boolean)
    const port = Number(url.port)
    return url.protocol === 'ws:' && url.hostname === '127.0.0.1'
      && Number.isInteger(port) && port >= 1 && port <= 65535
      && !url.username && !url.password && !url.search && !url.hash
      && parts.length === 2 && /^[A-Za-z0-9_-]{1,128}$/.test(decodeURIComponent(parts[0]!))
      && parts[1] === 'ws'
  } catch { return false }
}

function isOpenSshServerStatus(value: unknown): value is OpenSshServerStatus {
  if (!isRecord(value)) return false
  const capabilityStates = ['Installed', 'NotPresent', 'InstallPending', 'UninstallPending', 'Staged', 'Unknown']
  const validPort = (port: unknown) => Number.isInteger(port) && Number(port) >= 1 && Number(port) <= 65535
  return value.feature_id === 'openssh-server'
    && capabilityStates.includes(String(value.capability_state))
    && typeof value.service_present === 'boolean' && isNullableString(value.service_state)
    && isNullableString(value.startup_type) && isNullableString(value.source_path)
    && isNullableString(value.source_version) && isNullableString(value.signature_status)
    && isNullableString(value.signature_subject) && typeof value.official_binary === 'boolean'
    && typeof value.service_path_official === 'boolean'
    && (value.effective_port === null || validPort(value.effective_port))
    && Array.isArray(value.listening_ports) && value.listening_ports.every(validPort)
    && typeof value.configuration_valid === 'boolean'
    && isNullableString(value.authorized_keys_path) && typeof value.authorized_keys_exists === 'boolean'
    && isNullableString(value.account_name) && isNullableString(value.account_ssh_username)
    && isNullableString(value.account_sid) && typeof value.account_is_admin === 'boolean'
    && typeof value.account_is_entra === 'boolean'
    && typeof value.project_access_enabled === 'boolean' && typeof value.project_access_valid === 'boolean'
    && isStringArray(value.project_network_scope)
    && typeof value.pairing_access_enabled === 'boolean' && typeof value.pairing_access_valid === 'boolean'
    && (value.discovery_access_enabled === undefined || typeof value.discovery_access_enabled === 'boolean')
    && (value.discovery_access_valid === undefined || typeof value.discovery_access_valid === 'boolean')
    && isStringArray(value.pairing_network_scope) && isStringArray(value.existing_ssh_inbound_rules)
    && isStringArray(value.local_lan_addresses) && isStringArray(value.local_lan_scopes)
    && typeof value.ready === 'boolean' && isStringMap(value.fields)
}

function isRemoteSshSystemFeatureJob(value: unknown): value is RemoteSshSystemFeatureJob {
  return isRecord(value) && value.job_id === 'system-feature-openssh-server'
    && value.resource_id === 'openssh-server'
    && ['queued', 'running', 'cancelling', 'complete', 'failed', 'cancelled'].includes(String(value.state))
    && typeof value.phase === 'string' && isNullableString(value.error)
    && typeof value.can_cancel === 'boolean' && isStringMap(value.fields)
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

/** DiscoveredDevice（发现的服务端）：原生层负责解析地址，页面只显示设备身份和状态。 */
export interface DiscoveredDevice {
  host_id: string // host_id（服务端稳定设备编号）。
  display_name: string // display_name（服务端显示名称）。
  hostname: string // hostname（mDNS 服务端主机名）。
  addresses: string[] // addresses（解析到的局域网地址；不要求用户编辑）。
  pairing_port: number // pairing_port（OPAQUE 六位码配对端口）。
  ssh_port: number // ssh_port（微软 OpenSSH 服务端口）。
  protocol_version: number // protocol_version（设备发现协议版本）。
  compatible: boolean // compatible（发现协议是否兼容）。
  state: 'ready' | 'starting' | 'unavailable' // state（服务端实际准备状态）。
  capabilities: string[] // capabilities（服务端能力编号）。
  fields: Record<string, string> // fields（字段的中文说明）。
}

/** DevicePairingWindowResult（本机配对窗口回执）：配对码只在开启时返回一次。 */
export interface DevicePairingWindowResult {
  pairing_code: string // pairing_code（六位一次性配对码；页面收到后立即复制）。
  expires_in_seconds: number // expires_in_seconds（配对窗口有效秒数）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** DevicePairingResult（远程配对回执）：不包含私钥、配对码或用户可编辑连接参数。 */
export interface DevicePairingResult {
  state: 'paired' // state（已登记六位码配对并建立 SSH 桥接）。
  device_id: string // device_id（本机稳定设备编号）。
  host_id: string // host_id（已固定服务端设备编号）。
  display_name: string // display_name（服务端显示名称）。
  ssh_port: number // ssh_port（微软 OpenSSH 服务端口）。
  address: string // address（本次使用的局域网服务地址）。
  connected: boolean // connected（SSH 桥接当前是否已认证）。
  connection_error: string | null // connection_error（配对完成后 SSH 连接失败的非敏感原因）。
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
  deviceConfigStatus: async () => {
    const status = await call<DeviceConfigStatus>('device_config_status')
    const hostId = status.runtime.mode === 'remote' && status.device_config?.mode === 'remote'
      ? status.device_config.paired_host?.host_id ?? null : null
    if (!hostId || activeRemoteManagementSession?.host_id !== hostId) {
      activeRemoteManagementSession = null
      activeRemoteTaskSession = null
    }
    return status
  },
  saveDeviceConfig: async (config: DeviceConfig, preserveInvalid = false) => {
    const status = await call<DeviceConfigStatus>('device_config_save', { config, preserveInvalid })
    const hostId = config.mode === 'remote' ? config.paired_host?.host_id ?? null : null
    if (!hostId || activeRemoteManagementSession?.host_id !== hostId) {
      activeRemoteManagementSession = null
      activeRemoteTaskSession = null
    }
    return status
  },
  /** searchDevices（搜索服务端）：固定为局域网 mDNS 搜索，不接受手工地址。 */
  searchDevices: (timeoutMs = 3000) => call<{
    protocol_version: number // protocol_version（发现协议版本）。
    devices: DiscoveredDevice[] // devices（局域网内可配对服务端）。
    fields: Record<string, string> // fields（字段的中文说明）。
  }>('device_discovery_search', { timeoutMs }),
  /** openPairingWindow（开启本机配对窗口）：六位码仅在本次回执中返回。 */
  openPairingWindow: () => call<DevicePairingWindowResult>('device_pairing_window_open'),
  /** closePairingWindow（关闭本机配对窗口）：撤销尚未使用的六位码和监听器。 */
  closePairingWindow: () => call<void>('device_pairing_window_close'),
  /** pairingWindowStatus（读取配对窗口）：只返回开关和剩余时长，不返回配对码。 */
  pairingWindowStatus: () => call<{ open: boolean; expires_in_ms: number; fields: Record<string, string> }>(
    'device_pairing_window_status',
  ),
  /** pairWithDiscoveredDevice（配对已发现服务端）：没有手工地址、账户或指纹参数。 */
  pairWithDiscoveredDevice: (hostId: string, pairingCode: string) => call<DevicePairingResult>(
    'device_pairing_connect', { hostId, pairingCode },
  ),
  /** openSshServerStatus（读取 OpenSSH Server 状态）：仅共享主机管理页显式读取。 */
  openSshServerStatus: (refresh = false) => call<OpenSshServerStatus>('resource_system_feature_status', { refresh }),
  /** enableOpenSshServer（启动本机 SSH 系统功能任务）：把用户的明确确认值传给原生端复核。 */
  enableOpenSshServer: (confirmed: boolean) => call<{ job_id: string; fields: Record<string, string> }>(
    'resource_system_feature_enable', { confirmed },
  ),
  /** setOpenSshProjectAccess（设置项目局域网规则）：仅修改 Voice Terminal 专用规则。 */
  setOpenSshProjectAccess: (enabled: boolean) => call<OpenSshServerStatus>(
    'resource_system_feature_access', { enabled },
  ),
  /** resourceJobs（读取任务快照）：只读取已登记的受管任务。 */
  resourceJobs: () => call<ManagedResourceJob[]>('resource_jobs'),
  /** remoteTtsAssetImport（远程 TTS 资产导入）：客户端原生选择器通过固定 FILE 通道上传音频。 */
  remoteTtsAssetImport,
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
  connectSshHost: async (timeoutMs?: number) => {
    resetRemoteManagementSession()
    return await call<{ state: 'ready'; device_id: string; address: string; port: number; host_key_sha256: string; gateway_port: number; fields: Record<string, string> }>(
      'device_ssh_connect', { timeoutMs },
    )
  },
  requestSshGateway: (path: '/v1/status' | '/v1/devices', body: Record<string, unknown>, timeoutMs?: number) => call<{ status: number; body: Record<string, unknown> }>(
    'device_ssh_request', { path, body, timeoutMs },
  ),
  /** remoteManagementRequest（远程业务操作）：固定主机与操作，并校验回执属于当前管理代次。 */
  remoteManagementRequest: (operation: RemoteManagementOperation, argumentsValue: Record<string, unknown>, requestId?: string) =>
    requestRemoteManagement(operation, argumentsValue, requestId),
  /** startRemoteResourceImport（客户端选择并上传资源）：本机绝对路径只由原生文件选择器和 FILE 通道处理。 */
  startRemoteResourceImport: (resourceId?: string) => call<{
    cancelled?: boolean // cancelled（用户关闭客户端来源选择器）。
    job_id?: string // job_id（客户端受管 FILE 上传编号）。
    resource_id?: string // resource_id（服务端目标组件编号）。
    state?: string // state（客户端上传任务状态）。
    phase?: string // phase（服务端确认的上传阶段）。
    can_cancel?: boolean // can_cancel（上传期间是否可取消）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_ssh_import_start', { resource_id: resourceId ?? null }),
  /** cancelRemoteResourceImport（取消客户端远程上传）：只取消当前原生传输。 */
  cancelRemoteResourceImport: (jobId: string) => call<void>(
    'device_ssh_import_cancel', { job_id: jobId },
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
  /** remoteVoiceForwarder（远程语音回环转发）：仅连接客户端本机随机端口，不把服务端地址交给网页。 */
  remoteVoiceForwarder: async (action: 'start' | 'status' | 'stop'): Promise<RemoteVoiceForwarderResult> => {
    const result: unknown = await call('device_ssh_voice_forwarder', { action })
    if (!isRecord(result) || !isStringMap(result.fields)) {
      throw new Error('远程语音转发回执字段无效。')
    }
    if (action === 'stop') {
      if (result.state !== 'stopped') throw new Error('远程语音转发停止回执状态无效。')
      return { state: 'stopped', fields: result.fields }
    }
    if (result.state !== 'running' || !isLoopbackVoiceForwarderUrl(result.url)) {
      throw new Error('远程语音转发回执必须提供本机回环 WebSocket 地址。')
    }
    return { state: 'running', url: result.url, fields: result.fields }
  },
  remoteTaskChannel: async (action: 'start' | 'status' | 'stop') => {
    const management = activeRemoteManagementSession
    const result = await call<{
    state: 'running' | 'stopped' | 'error' // state（远端桌面任务通道状态）。
    stopped?: boolean // stopped（本次是否停止了轮询任务）。
    device_id?: string // device_id（已登记的远端设备编号）。
    connection_generation?: number // connection_generation（主机授予的连接代次）。
    executor_generation?: number // executor_generation（主机授予的执行器代次）。
    error?: string | null // error（最近一次通道错误）。
    desktop_control?: boolean | null // desktop_control（当前通道是否已登记桌面控制能力）。
    desktop_control_error?: string | null // desktop_control_error（桌面能力登记失败原因）。
      fields: Record<string, string> // fields（字段中文说明）。
    }>('device_ssh_task_channel', { action })
    const currentManagement = activeRemoteManagementSession
    const managementStillCurrent = !!management && currentManagement?.host_id === management.host_id
      && currentManagement.session_generation === management.session_generation
    if (action === 'stop' || result.state !== 'running' || !management
      || !managementStillCurrent
      || !Number.isSafeInteger(result.connection_generation) || (result.connection_generation ?? 0) <= 0) {
      activeRemoteTaskSession = null
    } else {
      activeRemoteTaskSession = {
        host_id: management.host_id,
        management_session_generation: management.session_generation,
        connection_generation: result.connection_generation!,
        ...(Number.isSafeInteger(result.executor_generation) && (result.executor_generation ?? 0) > 0
          ? { executor_generation: result.executor_generation } : {}),
      }
    }
    return result
  },
  remoteMediaChannel: (action: 'start' | 'status' | 'stop') => call<{
    state: 'running' | 'standby' | 'stopped' | 'error' // state（语音媒体连接状态，standby 表示等待主机选择）。
    stopped?: boolean // stopped（本次是否关闭原媒体流）。
    device_id?: string | null // device_id（当前语音交互设备编号）。
    stream_id?: string | null // stream_id（连续音频流编号）。
    connection_generation?: number // connection_generation（服务端授予的设备连接代次）。
    executor_generation?: number // executor_generation（服务端授予的客户端执行器代次）。
    interaction_generation?: number // interaction_generation（主机授予的语音占用代次）。
    capture_active?: boolean // capture_active（核心是否要求本机采集）。
    error?: string | null // error（最近一次媒体错误）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('device_ssh_media_channel', { action }),
  disconnectSshHost: async () => {
    resetRemoteManagementSession()
    return await call<{
      state: 'disconnected'; disconnected: boolean; media_channel_stopped?: boolean; task_channel_stopped?: boolean; fields: Record<string, string>
    }>('device_ssh_disconnect')
  },
  sshHostStatus: async () => {
    const status = await call<{ state: 'connected' | 'disconnected' | 'unavailable'; connected: boolean; fields: Record<string, string> }>('device_ssh_status')
    if (!status.connected) {
      activeRemoteManagementSession = null
      activeRemoteTaskSession = null
    }
    return status
  },
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

/** RemoteManagementOperation（服务端业务操作）：每个编号由桌面、Rust 和 Python 三处固定白名单共同约束。 */
export type RemoteManagementOperation =
  | 'resource.inventory' | 'resource.jobs' | 'resource.job.read' | 'resource.job.cancel'
  | 'resource.install.plan' | 'resource.install.start' | 'resource.install.required' | 'resource.job.start'
  | 'resource.import.confirm' | 'resource.reset.plan' | 'resource.reset'
  | 'system.ssh.status' | 'system.ssh.enable' // system.ssh.*（服务端只读检查及用户确认后的 SSH 功能操作）。
  | 'management.status' | 'product.snapshot' | 'product.devices' | 'product.compute_status' | 'product.compute_status.refresh'
  | 'product.dsh_status' | 'product.wake_tasks' | 'wake_task.create' // wake_task.create（在服务端创建定时任务）。
  | 'wake_task.update' | 'wake_task.delete' | 'wake_task.cancel'
  | 'settings.hotwords.read' | 'settings.hotwords.save' | 'settings.wake.save' | 'settings.silence.save'
  | 'settings.quiet_hours.save' | 'settings.shadow.save' | 'settings.approvals.save' | 'settings.persona.save'
  | 'settings.custom_instructions.save' | 'settings.speaker.save' | 'settings.compute.save'
  | 'settings.audio_enhancements.save' | 'settings.screen_capture.save' | 'settings.tts.save' | 'settings.inference.save'
  | 'models.registry.read' | 'models.scan' | 'models.validate' | 'models.providers.add'
  | 'models.assignments.apply' | 'models.credentials.read' | 'models.credentials.save'
  | 'provider.usage' | 'model.update' | 'model.delete' | 'model.hide' | 'model.restore'
  | 'hotwords.import' | 'hotwords.preview' | 'tts.minimax.voice_name'
  | 'tts.operation.preview' | 'tts.operation.minimax_preview' | 'tts.operation.play' | 'tts.operation.transcribe'
  | 'tts.operation.read' | 'tts.operation.cancel' | 'tts.voices.read' | 'tts.voices.create'
  | 'tts.voices.update' | 'tts.voices.delete' | 'tts.voices.select' | 'tts.voices.restore_default'
  | 'tts.voices.default_reference' | 'tts.assets.clip' | 'tts.aniflive.references.import'
  | 'tts.aniflive.import.pending' | 'tts.aniflive.import.read' | 'tts.aniflive.import.reference'
  | 'tts.aniflive.import.scan' | 'tts.aniflive.import.start' | 'tts.aniflive.model.rename'
  | 'tts.voices.capabilities' | 'tts.status' | 'speaker.status' | 'speaker.profile'
  | 'dsh.approval.decide' // dsh.approval.decide（服务端 DSH 权限答复）。
  | 'speaker.enrollment.start' | 'speaker.enrollment.segment' | 'speaker.enrollment.capture'
  | 'speaker.enrollment.capture_stop' | 'speaker.enrollment.commit' | 'speaker.enrollment.delete'
  | 'speaker.test.capture' | 'speaker.test.capture_stop' | 'speaker.test.run'
  | 'speaker.template.delete' | 'speaker.template.update' | 'product.devices.select'

/** RemoteManagementResponse（远程操作结果）：包含主机原业务状态及目标代次回执。 */
export interface RemoteManagementResponse {
  request_id?: string // request_id（主机确认的原操作编号）。
  target_host_id?: string // target_host_id（已配对服务端设备编号）。
  session_kind?: 'management' | 'task' // session_kind（本回执绑定的管理会话或核心任务通道）。
  session_generation?: number // session_generation（由 session_kind（会话类型）区分的当前通道代次）。
  connection_generation?: number // connection_generation（task 会话使用的核心设备连接代次）。
  executor_generation?: number // executor_generation（客户端本地执行器代次）。
  operation?: RemoteManagementOperation // operation（固定服务端业务操作）。
  status: number // status（服务端原业务 HTTP 状态码，或远程网关错误码）。
  body: Record<string, unknown> // body（服务端现有业务逻辑回执或错误）。
  fields: Record<string, string> // fields（字段中文说明）。
}

/** matchesRemoteManagementEnvelope（核对远程管理回执代次）：拒绝目标、操作或代次不匹配的迟到状态。 */
export function matchesRemoteManagementEnvelope(
  response: RemoteManagementResponse,
  hostId: string | null | undefined,
  operation: RemoteManagementOperation,
  sessionGeneration?: number,
  connectionGeneration?: number,
  requestId?: string,
): boolean {
  const expectedKind = operation === 'management.status' || operation.startsWith('resource.')
    || operation.startsWith('system.ssh.') ? 'management' : 'task'
  const actualKind = response.session_kind ?? expectedKind
  const responseGeneration = response.session_generation
  return Boolean(hostId
    && response.target_host_id === hostId
    && response.operation === operation
    && actualKind === expectedKind
    && typeof response.request_id === 'string'
    && /^[0-9a-f]{32}$/.test(response.request_id)
    && (requestId === undefined || response.request_id === requestId)
    && Number.isSafeInteger(responseGeneration)
    && (responseGeneration ?? 0) > 0
    && (expectedKind === 'management'
      ? sessionGeneration === undefined || responseGeneration === sessionGeneration
      : Number.isSafeInteger(response.connection_generation)
        && response.connection_generation === responseGeneration
        && (connectionGeneration === undefined || response.connection_generation === connectionGeneration)))
}

interface ActiveRemoteManagementSession {
  host_id: string // host_id（当前管理会话固定的配对服务端编号）。
  session_generation: number // session_generation（当前服务端签发的管理会话代次）。
}
interface ActiveRemoteTaskSession {
  host_id: string // host_id（当前任务通道固定的配对服务端编号）。
  management_session_generation: number // management_session_generation（任务通道所属的管理会话代次）。
  connection_generation: number // connection_generation（任务通道当前核心连接代次）。
  executor_generation?: number // executor_generation（客户端执行器代次）。
}
let activeRemoteManagementSession: ActiveRemoteManagementSession | null = null
let activeRemoteTaskSession: ActiveRemoteTaskSession | null = null
let remoteManagementStatusInFlight: Promise<RemoteManagementResponse> | null = null
let remoteSessionRevision = 0 // remoteSessionRevision（显式连接/断开代次）：阻止旧状态复活已断开的会话。

function resetRemoteManagementSession() {
  remoteSessionRevision++
  activeRemoteManagementSession = null
  activeRemoteTaskSession = null
  remoteManagementStatusInFlight = null
}

function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const native = locateNativeInvoker()
  if (!native) return Promise.reject(new Error('此操作需要桌面窗口。'))
  return native.invoke<T>(command, args).catch((error: unknown) => {
    throw error instanceof Error ? error : new Error(typeof error === 'string' ? error : '原生设备命令执行失败。')
  })
}

/** remoteTtsAssetImport（远程 TTS 资产导入）：source_and_asset（原件与参考资产）共用一次选择和 FILE 上传。 */
function remoteTtsAssetImport(modelId: ProductLocalTtsModelId, purpose: 'source_and_asset'): Promise<RemoteTtsAssetImportCancelled | {
  asset: ProductTtsAsset // asset（供转写或合成使用的参考音频资产）。
  source: ProductTtsAsset // source（保留可裁剪的原始录音资产）。
}>
function remoteTtsAssetImport(modelId: ProductLocalTtsModelId, purpose: 'asset' | 'source'): Promise<RemoteTtsAssetImportCancelled | {
  asset: ProductTtsAsset // asset（服务端接收并登记的参考音频资产）。
}>
function remoteTtsAssetImport(modelId: ProductLocalTtsModelId, purpose: 'asset' | 'source' | 'source_and_asset') {
  return call<RemoteTtsAssetImportCancelled | { asset: ProductTtsAsset; source?: ProductTtsAsset }>(
    'device_ssh_tts_asset_import', { model_id: modelId, purpose },
  )
}

function readRemoteManagementStatus(): Promise<RemoteManagementResponse> {
  if (remoteManagementStatusInFlight) return remoteManagementStatusInFlight
  const request = (async () => {
    const status = await nativeDeviceApi.deviceConfigStatus()
    const hostId = status.runtime.mode === 'remote' && status.device_config?.mode === 'remote'
      ? status.device_config.paired_host?.host_id ?? null : null
    if (!hostId) {
      activeRemoteManagementSession = null
      throw new Error('远程管理状态要求本机处于已配对远程模式。')
    }
    const revision = remoteSessionRevision
    const requestId = newRemoteManagementRequestId()
    const response = await call<RemoteManagementResponse>('device_ssh_management_request', {
      operation: 'management.status', arguments: {}, request_id: requestId,
    })
    if (revision !== remoteSessionRevision) throw new Error('服务端连接已切换，已丢弃旧管理状态。')
    if (response.status === 200 && matchesRemoteManagementEnvelope(response, hostId, 'management.status', undefined, undefined, requestId)) {
      if (activeRemoteManagementSession?.host_id !== hostId
        || activeRemoteManagementSession.session_generation !== response.session_generation) {
        activeRemoteTaskSession = null
      }
      activeRemoteManagementSession = { host_id: hostId, session_generation: response.session_generation! }
    } else {
      activeRemoteManagementSession = null
      activeRemoteTaskSession = null
    }
    return response
  })()
  const tracked = request.finally(() => {
    if (remoteManagementStatusInFlight === tracked) remoteManagementStatusInFlight = null
  })
  remoteManagementStatusInFlight = tracked
  return tracked
}

async function requestRemoteManagement(
  operation: RemoteManagementOperation,
  argumentsValue: Record<string, unknown>,
  requestId?: string,
): Promise<RemoteManagementResponse> {
  if (operation === 'management.status') return readRemoteManagementStatus()
  if (!activeRemoteManagementSession) {
    const status = await readRemoteManagementStatus()
    if (status.status !== 200 || !activeRemoteManagementSession) {
      const detail = typeof status.body?.detail === 'string' ? status.body.detail : ''
      throw new Error(detail || '服务端管理会话尚未就绪，已取消本次业务请求。')
    }
  }
  const expected = { ...activeRemoteManagementSession! }
  const expectedKind = operation.startsWith('resource.') || operation.startsWith('system.ssh.') ? 'management' : 'task'
  const expectedTask = activeRemoteTaskSession
  const expectedConnectionGeneration = expectedKind === 'task' && expectedTask?.host_id === expected.host_id
    && expectedTask.management_session_generation === expected.session_generation
    ? expectedTask.connection_generation : undefined
  const requestIdValue = requestId ?? newRemoteManagementRequestId()
  const response = await call<RemoteManagementResponse>('device_ssh_management_request', {
    operation, arguments: argumentsValue, request_id: requestIdValue,
  })
  const current = activeRemoteManagementSession
  if (!current || current.host_id !== expected.host_id || current.session_generation !== expected.session_generation
    || !matchesRemoteManagementEnvelope(response, expected.host_id, operation,
      expectedKind === 'management' ? expected.session_generation : undefined, expectedConnectionGeneration, requestIdValue)
    || expectedKind === 'task' && expectedTask
      && (expectedTask.executor_generation !== undefined && response.executor_generation !== expectedTask.executor_generation
        || activeRemoteTaskSession?.connection_generation !== expectedTask.connection_generation)) {
    if (activeRemoteManagementSession?.host_id === expected.host_id
      && activeRemoteManagementSession.session_generation === expected.session_generation) {
      if (expectedKind === 'management') {
        activeRemoteManagementSession = null
        activeRemoteTaskSession = null
      } else {
        activeRemoteTaskSession = null
      }
    }
    throw new Error('服务端连接代次已切换，已丢弃迟到的业务回执；请刷新服务端状态后重试。')
  }
  if (expectedKind === 'task' && !activeRemoteTaskSession && Number.isSafeInteger(response.connection_generation)
    && (response.connection_generation ?? 0) > 0) {
    activeRemoteTaskSession = {
      host_id: expected.host_id,
      management_session_generation: expected.session_generation,
      connection_generation: response.connection_generation!,
      ...(Number.isSafeInteger(response.executor_generation) && (response.executor_generation ?? 0) > 0
        ? { executor_generation: response.executor_generation } : {}),
    }
  }
  return response
}

function newRemoteManagementRequestId(): string {
  if (typeof crypto === 'undefined' || typeof crypto.getRandomValues !== 'function') {
    throw new Error('无法安全生成远程管理请求编号。')
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}
