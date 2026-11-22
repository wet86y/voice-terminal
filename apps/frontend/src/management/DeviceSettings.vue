<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { matchesRemoteManagementEnvelope, nativeDeviceApi, parseRemoteSshSystemFeatureSnapshot, type DeviceConfig, type DeviceConfigStatus, type DiscoveredDevice, type ManagedResourceJob, type OpenSshServerStatus, type RemoteSshSystemFeatureSnapshot } from '../product/native-device-api'
import { ensureRemoteClientRuntime } from '../product/remote-client-runtime'
import { deviceSettingsOperation, recordDeviceSettingsError, runDeviceSettingsOperation, setDeviceSettingsPhase } from '../product/device-settings-operation'
import { productApi, type ProductDeviceSnapshot } from '../product/api'
import { deviceState } from '../product/device-state'
import { connectionDisplayNames, deviceDisplayName } from '../product/device-display-name'
import ManagedResourceProgress from '../interface/ManagedResourceProgress.vue'
import SettingsPageHeader from '../interface/SettingsPageHeader.vue'
import UiDialog from '../interface/UiDialog.vue'
import { lanVersionSync, showLanVersionSync } from '../product/lan-version-sync'
import type { ManagedResourceJob as ProgressJob } from '../interface/local-components'

const emit = defineEmits<{ notice: [message: string] }>() // notice（短暂操作提示）：交给应用现有弹窗统一显示及自动淡出。

/** DeviceConfirmation（设备操作确认）：项目弹窗只承接业务确认，管理员提权仍由 Windows 处理。 */
interface DeviceConfirmation {
  title: string // title（确认标题）。
  message: string // message（操作影响及下载来源）。
  details?: string // details（只读检查摘要）。
  confirmLabel: string // confirmLabel（确认按钮的可访问名称）。
}
const confirmation = ref<DeviceConfirmation | null>(null)
let confirmationResult: ((accepted: boolean) => void) | null = null // confirmationResult（当前弹窗的单次确认回执）。
const activityPhase = ref('') // activityPhase（前台搜索、配对和连接操作的当前阶段）。

function requestConfirmation(value: DeviceConfirmation): Promise<boolean> {
  if (disposed || confirmation.value || pairingCode.value || renameOpen.value) return Promise.resolve(false)
  confirmation.value = value
  return new Promise(resolve => { confirmationResult = resolve })
}

function settleConfirmation(accepted: boolean) {
  const resolve = confirmationResult
  confirmationResult = null
  confirmation.value = null
  resolve?.(accepted)
}

function closeDeviceDialog() {
  if (confirmation.value) settleConfirmation(false)
  else void closePairingWindow()
}

const status = ref<DeviceConfigStatus | null>(null)
const mode = ref<DeviceConfig['mode']>('local')
const modeDraft = ref<DeviceConfig['mode']>('local') // modeDraft（待保存的运行模式，不影响当前连接）。
const serverNameDraft = ref('') // serverNameDraft（待保存的中文服务端名称）。
const renameOpen = ref(false) // renameOpen（服务端重命名弹窗状态）。
const renameError = ref('') // renameError（重命名校验或保存错误）。
const broadcastEnabled = ref(false)
const clientOnlineTtsLocal = ref(true)
const audioCompression = ref(false)
const deviceId = ref('')
const defaultServerName = computed(() => deviceDisplayName('', status.value?.device_config?.device_id || deviceId.value))
const serverDisplayName = computed(() => status.value?.device_config?.server_name || defaultServerName.value)
const serverNameChanged = computed(() => (serverNameDraft.value.trim() || defaultServerName.value) !== serverDisplayName.value)
const pairingCode = ref('')
const pairingCodeInput = ref('')
const pairingExpiresAt = ref(0)
const discoveredDevices = ref<DiscoveredDevice[]>([])
const selectedHostId = ref('')
const searchingDevices = ref(false)
const connection = ref<'connected' | 'disconnected' | 'unavailable'>('disconnected')
const pairedAvailability = ref<'unknown' | 'available' | 'starting' | 'incompatible' | 'missing'>('unknown') // pairedAvailability（已配对服务端的局域网发现状态）。
const pairedHostDiscovery = ref<Pick<DiscoveredDevice, 'host_id' | 'display_name'> | null>(null) // pairedHostDiscovery（按稳定服务端编号绑定的最新公告名称，仅用于显示）。
let pairedDiscoveryGeneration = 0 // pairedDiscoveryGeneration（配对显示范围代次，拒绝切换目标或模式后迟到的发现结果）。
let pairedDiscoveryRequest: Promise<void> | null = null // pairedDiscoveryRequest（自动发现的单次请求，避免与手动搜索重叠）。
let connectionPolling = false // connectionPolling（连接状态轻量补查互斥，不触发系统功能检查）。
const taskChannel = ref<'running' | 'stopped' | 'error'>('stopped')
const taskChannelError = ref('')
const taskDesktopControl = ref<{ available: boolean; error: string } | null>(null)
const mediaChannel = ref<'running' | 'standby' | 'stopped' | 'error'>('stopped')
const mediaChannelError = ref('')
const mediaCaptureActive = ref<boolean | null>(null)
const remoteCoreState = ref<'ready' | 'starting' | 'unavailable' | 'unknown'>('unknown')
const inboundDevices = ref<ProductDeviceSnapshot | null>(null) // 入站设备、连接代次和当前语音设备。
const inboundPresence = ref<Awaited<ReturnType<typeof nativeDeviceApi.inboundStatus>> | null>(null) // inboundPresence（本机可信登记和当前局域网发现）。
const inboundPresenceCheckedAt = ref(0) // inboundPresenceCheckedAt（最近成功状态查询时间，毫秒）。
const inboundPresenceClock = ref(Date.now()) // inboundPresenceClock（复用设备轮询推进的状态时钟）。
let inboundPresenceReading = false // inboundPresenceReading（轻量发现请求互斥）。
let inboundRefresh = false
let devicePoll: ReturnType<typeof setInterval> | undefined
const ownDevice = computed(() => deviceState.value?.self_device_id === status.value?.device_config?.device_id
  ? deviceState.value?.devices.find(row => row.device_id === deviceState.value?.self_device_id) : undefined)
watch(deviceState, value => {
  if (mode.value === 'local' && value?.self_device_id === 'local') inboundDevices.value = value
})
const inboundClients = computed(() => {
  const connections = inboundDevices.value?.devices.filter(device => device.device_id !== 'local') ?? []
  const presence = inboundPresence.value
  const registered = presence && presence.host_id === status.value?.device_config?.device_id ? presence.devices : null
  const presenceFresh = !!presence?.discovery_available && inboundPresenceClock.value - inboundPresenceCheckedAt.value < 30000
  const devices = registered ? registered.map(device => {
    const current = connections.find(row => row.device_id === device.device_id)
    return { device_id: device.device_id, name: device.display_name || current?.name || device.device_id,
      recordToken: device.record_token, removalPending: device.removal_pending === true,
      connected: device.connected || current?.connected === true, online: presenceFresh ? device.online : null }
  }) : connections.map(device => ({ ...device, recordToken: '', removalPending: false, online: null }))
  const names = connectionDisplayNames(devices.map(device => ({ id: device.device_id, name: device.name })))
  return devices.map(device => ({ ...device, displayName: names.get(device.device_id)! }))
    .sort((a, b) => a.displayName.localeCompare(b.displayName, 'zh-CN', { numeric: true }))
})
const discoveredDeviceNames = computed(() => connectionDisplayNames(discoveredDevices.value
  .map(device => ({ id: device.host_id, name: device.display_name }))))
const discoveredDeviceRows = computed(() => discoveredDevices.value.map(device => ({
  ...device, displayName: discoveredDeviceNames.value.get(device.host_id)!,
})).sort((a, b) => a.displayName.localeCompare(b.displayName, 'zh-CN', { numeric: true })))
const pairedHostName = computed(() => {
  const host = status.value?.device_config?.paired_host
  if (!host) return ''
  const name = pairedHostDiscovery.value?.host_id === host.host_id ? pairedHostDiscovery.value.display_name : host.display_name
  return deviceDisplayName(name, host.host_id)
})
const openSshStatus = ref<OpenSshServerStatus | null>(null)
const maxVisibleSshRuleDetails = 5 // maxVisibleSshRuleDetails（展开时最多显示的入站规则摘要条数）。
const maxSshRuleDisplayChars = 180 // maxSshRuleDisplayChars（单条规则摘要最大显示字符数）。
function compactSshRuleDetail(rule: string) {
  const characters = Array.from(rule.replace(/\s+/g, ' ').trim())
  return characters.length > maxSshRuleDisplayChars
    ? `${characters.slice(0, maxSshRuleDisplayChars - 1).join('')}…`
    : characters.join('')
}
const visibleSshRuleDetails = computed(() => openSshStatus.value?.existing_ssh_inbound_rules
  .slice(0, maxVisibleSshRuleDetails).map(compactSshRuleDetail) ?? [])
const hiddenSshRuleDetailCount = computed(() => Math.max(0,
  (openSshStatus.value?.existing_ssh_inbound_rules.length ?? 0) - visibleSshRuleDetails.value.length))
const openSshStatusError = ref('')
const openSshJob = ref<ManagedResourceJob | null>(null)
const openSshBusy = ref(false)
const openSshStatusLoading = ref(false)
const openSshLongWaitNotice = ref(false) // openSshLongWaitNotice（本机 SSH 准备超过五分钟后的等待提示，仍继续跟踪正式任务）。
const openingPairing = ref(false) // openingPairing（配对码生成阶段，与广播启停分别显示）。
const runtimeStatusTimer = ref<ReturnType<typeof setTimeout> | null>(null) // runtimeStatusTimer（轻量设备状态补查，不查询 Windows 功能）。
let runtimeStatusReading = false // runtimeStatusReading（防止设备状态补查重叠）。
let disposed = false // disposed（页面销毁后不再启动补查）。
let sshStatusGeneration = 0 // sshStatusGeneration（拒绝模式切换或新检查前发出的旧系统快照）。
const openSshJobTimer = ref<ReturnType<typeof setInterval> | null>(null)
const remoteOpenSshSnapshot = ref<RemoteSshSystemFeatureSnapshot | null>(null) // remoteOpenSshSnapshot（配对服务端只读 SSH 快照）。
const remoteOpenSshHostId = ref<string | null>(null) // remoteOpenSshHostId（当前快照绑定的服务端编号）。
const remoteOpenSshError = ref('') // remoteOpenSshError（服务端 SSH 检查或管理操作失败原因）。
const remoteOpenSshBusy = ref(false) // remoteOpenSshBusy（服务端 SSH 远程操作进行中）。
const remoteOpenSshPollTimer = ref<ReturnType<typeof setTimeout> | null>(null) // remoteOpenSshPollTimer（服务端受管任务状态补查）。
let remoteOpenSshGeneration = 0 // remoteOpenSshGeneration（配对目标或服务端状态操作代次）。
const pairingExpiryTimer = ref<ReturnType<typeof setTimeout> | null>(null)
const pairingStatusTimer = ref<ReturnType<typeof setInterval> | null>(null)
let pairingWindowGeneration = 0 // pairingWindowGeneration（配对窗口代次，忽略关闭或重建后迟到的剪贴板回执）。
const loading = ref(false)
const busy = ref(false)
const error = ref('')
function notify(message: string) { if (!disposed) emit('notice', message) }
const localCoreAvailable = computed(() => status.value?.local_core_available !== false)
const openSshJobPending = computed(() => !!openSshJob.value && ['queued', 'running', 'cancelling'].includes(openSshJob.value.state))
const remoteOpenSshJobPending = computed(() => !!remoteOpenSshSnapshot.value?.job
  && ['queued', 'running', 'cancelling'].includes(remoteOpenSshSnapshot.value.job.state))
const remoteOpenSshJobIsHistorical = computed(() => !!remoteOpenSshSnapshot.value?.job
  && remoteOpenSshSnapshot.value.status.ready && !remoteOpenSshJobPending.value) // remoteOpenSshJobIsHistorical（当前已就绪时，结束的准备任务只作为历史记录）。
const remoteOpenSshJobCreatedAt = computed(() => {
  const createdAt = remoteOpenSshSnapshot.value?.job?.created_at
  return createdAt === undefined ? null : new Date(createdAt)
}) // remoteOpenSshJobCreatedAt（服务端任务创建时间；旧服务端未提供时不推测日期）。
const isRemote = computed(() => mode.value === 'remote')
watch([mode, () => status.value?.device_config?.paired_host?.host_id], () => {
  pairedDiscoveryGeneration++
  pairedHostDiscovery.value = null
  pairedAvailability.value = 'unknown'
}, { flush: 'sync' })
// canDisconnect（允许断开）：底层 SSH（安全远程连接）已认证时，管理握手失败也必须保留恢复入口。
const canDisconnect = computed(() => isRemote.value && !!status.value?.device_config?.paired_host
  && (connection.value === 'connected' || status.value?.device_runtime?.remote_client_connected === true))
const remoteOpenSshCanEnable = computed(() => {
  const hostId = status.value?.device_config?.paired_host?.host_id
  return isRemote.value && !!hostId && remoteOpenSshHostId.value === hostId
    && connection.value === 'connected' && !remoteOpenSshBusy.value
    && !remoteOpenSshJobPending.value && !confirmation.value && !busy.value
    && remoteOpenSshSnapshot.value?.status.ready === false
})
const isBroadcasting = computed(() => mode.value === 'local' && status.value?.device_config?.broadcast_enabled === true)
const deviceOperationPending = computed(() => deviceSettingsOperation.active || status.value?.device_runtime?.operation?.active === true)
watch(deviceOperationPending, pending => {
  if (pending) notify('正在配置设备连接，切换页面不会取消任务。')
}, { immediate: true })
const deviceOperationError = computed(() => error.value || deviceSettingsOperation.error || status.value?.device_runtime?.operation?.error || '')
const controlsLocked = computed(() => loading.value || busy.value || openSshBusy.value || openSshJobPending.value
  || deviceOperationPending.value || !!confirmation.value || !!pairingCode.value || renameOpen.value || remoteOpenSshBusy.value || remoteOpenSshJobPending.value)
const broadcastRuntimeError = computed(() => {
  const runtime = status.value?.device_runtime
  if (!isBroadcasting.value || (runtime?.broadcast_actual && runtime.device_gateway.ready)) return ''
  return runtime?.device_gateway.error || runtime?.error || ''
})
const broadcastPhaseLabel = computed(() => {
  if (status.value?.device_runtime?.operation?.active || deviceSettingsOperation.active) return '配置中'
  if (openingPairing.value) return '正在生成配对码…'
  if (busy.value) return '配置中'
  if (!isBroadcasting.value) return '广播已关闭'
  if (broadcastRuntimeError.value) return '广播异常'
  if (!status.value?.device_runtime?.device_gateway.ready) return '网关准备中'
  return status.value.device_runtime.broadcast_actual ? '广播正常' : '广播发布中'
})
const visibleOpenSshJob = computed(() => openSshJobPending.value || openSshLongWaitNotice.value || openSshStatusError.value ? openSshJob.value : null)
const canSave = computed(() => !loading.value && !busy.value && !openSshBusy.value && !openSshJobPending.value
  && !confirmation.value && !pairingCode.value && !remoteOpenSshBusy.value && !remoteOpenSshJobPending.value
  && !status.value?.device_runtime?.operation?.active
  && !!status.value)

/** sshStatusLabel（SSH 状态短语）：完整系统检查只在用户主动操作或准备任务结束时执行。 */
function sshStatusLabel(snapshot: OpenSshServerStatus | null, failure = '', pending = false) {
  if (pending) return '检查中'
  if (failure) return '检查异常'
  if (!snapshot) return '未检查'
  if (!snapshot.official_binary || !snapshot.service_path_official) return '程序异常'
  if (!snapshot.service_present || snapshot.service_state !== 'Running') return '服务异常'
  if (!snapshot.configuration_valid) return '配置异常'
  if (!snapshot.ready) return '监听异常'
  if (snapshot.account_is_entra) return '账户异常'
  if ((isBroadcasting.value || isRemote.value) && (!snapshot.project_access_valid || !snapshot.pairing_access_valid
    || snapshot.discovery_access_valid === false)) return '网络规则异常'
  return '正常'
}
const localSshLabel = computed(() => {
  if (openSshJobPending.value) return '准备中'
  if (!openSshStatus.value && !openSshStatusError.value && !openSshBusy.value
    && status.value?.device_runtime?.broadcast_actual && status.value.device_runtime.device_gateway.ready) return '正常'
  return sshStatusLabel(openSshStatus.value, openSshStatusError.value, openSshBusy.value || openSshStatusLoading.value)
})
const remoteSshLabel = computed(() => remoteOpenSshJobPending.value ? '准备中'
  : sshStatusLabel(remoteOpenSshSnapshot.value?.status ?? null, remoteOpenSshError.value, remoteOpenSshBusy.value))
const pairingNeedsRepair = computed(() => ['host_key_changed', 'authentication_failed', 'server_removed_pairing']
  .includes(status.value?.device_config?.reconnect_suspended_reason || ''))
const pairedConnectionLabel = computed(() => pairingNeedsRepair.value ? '需重新配对' : connection.value === 'connected' ? '已连接'
  : connection.value === 'unavailable' ? '连接不可用'
  : ({ unknown: '已断开', available: '可连接', starting: '服务端准备中', incompatible: '版本不兼容', missing: '未发现' })[pairedAvailability.value])

/** deviceProgress（设备连接统一进度）：复用受管任务样式，未知总量时不推测百分比。 */
const deviceProgress = computed<ProgressJob | null>(() => {
  const localJob = openSshJobPending.value ? openSshJob.value : null
  const remoteJob = remoteOpenSshJobPending.value ? remoteOpenSshSnapshot.value?.job : null
  const job = localJob || remoteJob
  const runtimeOperation = status.value?.device_runtime?.operation
  let phase = job ? `${remoteJob && !localJob ? '服务端 SSH' : '本机 SSH'}任务状态：${openSshJobStateLabel(job.state)}；${job.phase}` : ''
  if (!phase && confirmation.value) phase = `等待确认：${confirmation.value.title}`
  if (!phase && activityPhase.value) phase = activityPhase.value
  if (!phase && runtimeOperation?.active) phase = runtimeOperation.message
  if (!phase && deviceSettingsOperation.active) phase = deviceSettingsOperation.phase
  if (!phase && openSshBusy.value) phase = '正在检查和准备 SSH（安全外壳协议）服务…'
  if (!phase && remoteOpenSshBusy.value) phase = '正在读取配对服务端的 SSH（安全外壳协议）状态…'
  if (!phase && searchingDevices.value) phase = '正在搜索局域网服务端…'
  if (!phase && busy.value) phase = '正在应用设备连接设置…'
  if (!phase && loading.value) phase = '正在读取设备连接状态…'
  if (!phase && openSshStatusLoading.value) phase = '正在读取本机 SSH（安全外壳协议）状态…'
  if (!phase && isBroadcasting.value && !broadcastRuntimeError.value
    && (!status.value?.device_runtime?.broadcast_actual || !status.value.device_runtime.device_gateway.ready)) phase = broadcastPhaseLabel.value
  return phase ? {
    id: job?.job_id || 'device-connection', resourceId: job?.resource_id || 'device-connection',
    label: '设备连接', state: job?.state || 'running', phase, progressKind: 'waiting', canCancel: false,
  } : null
})

/** openSshJobStateLabel（SSH 任务状态中文说明）：本机与服务端使用相同的任务状态名称。 */
function openSshJobStateLabel(state: string) {
  return ({ queued: '等待开始', running: '进行中', cancelling: '正在取消', complete: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[state] ?? state
}

function newDeviceId() {
  return `device-${crypto.randomUUID()}`
}

function applyTaskChannel(result: Awaited<ReturnType<typeof nativeDeviceApi.remoteTaskChannel>>) {
  taskChannel.value = result.state
  taskChannelError.value = result.error || ''
  taskDesktopControl.value = typeof result.desktop_control === 'boolean'
    ? { available: result.desktop_control, error: result.desktop_control_error || '' }
    : null
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const next = await nativeDeviceApi.deviceConfigStatus()
    const keepModeDraft = modeDraft.value !== mode.value
    const keepServerNameDraft = renameOpen.value
    status.value = next
    const saved = next.device_config
    const nextRemoteHostId = next.runtime.mode === 'remote' && saved?.mode === 'remote'
      ? saved.paired_host?.host_id ?? null : null
    if (!nextRemoteHostId || remoteOpenSshHostId.value !== nextRemoteHostId) {
      stopRemoteOpenSshStatusWatch()
      remoteOpenSshSnapshot.value = null
      remoteOpenSshHostId.value = null
      remoteOpenSshError.value = ''
    }
    if (saved) {
      mode.value = saved.mode
      broadcastEnabled.value = saved.broadcast_enabled
      clientOnlineTtsLocal.value = saved.client_online_tts_local
      audioCompression.value = saved.audio_compression
      deviceId.value = saved.device_id
      if (!keepServerNameDraft) serverNameDraft.value = saved.server_name || ''
    } else if (next.runtime.config_state === 'missing') {
      if (!deviceId.value) deviceId.value = newDeviceId()
      mode.value = next.local_core_available === false ? 'remote' : 'local'
      broadcastEnabled.value = false
      clientOnlineTtsLocal.value = true
      audioCompression.value = false
    }
    if (!keepModeDraft) modeDraft.value = mode.value
    if (mode.value === 'local') {
      clearRemoteOpenSshSnapshot()
      // 进入页面和普通刷新只接续任务，不主动查询 Windows（操作系统）功能。
      void refreshOpenSshJob(false)
      connection.value = 'disconnected'
      taskChannel.value = 'stopped'
      mediaChannel.value = 'stopped'
      mediaCaptureActive.value = null
      void refreshDeviceChannels()
      void refreshInboundPresence()
    } else {
      inboundDevices.value = null
      inboundPresence.value = null
      const ssh = await nativeDeviceApi.sshHostStatus()
      if (!ssh.connected) connection.value = ssh.state
      if (ssh.connected) {
        let managementReady = false
        try {
          const management = await nativeDeviceApi.remoteManagementRequest('management.status', {})
          const hostId = status.value?.device_config?.paired_host?.host_id
          managementReady = management.status === 200
            && matchesRemoteManagementEnvelope(management, hostId, 'management.status')
          connection.value = managementReady ? 'connected' : 'disconnected'
          if (managementReady) {
            const coreState = management.body?.core_state
            remoteCoreState.value = coreState === 'ready' || coreState === 'starting' ? coreState : 'unavailable'
          } else {
            remoteCoreState.value = management.status === 200 ? 'unknown' : 'unavailable'
            const detail = typeof management.body?.detail === 'string' ? management.body.detail : ''
            taskChannelError.value = detail || (management.status === 200
              ? '服务端管理回执的目标主机或连接代次无效，已丢弃该状态。' : '')
          }
        } catch (failure) {
          remoteCoreState.value = 'unknown'
          taskChannelError.value = failure instanceof Error ? failure.message : '无法读取服务端管理状态。'
        }
        if (managementReady) {
          const [tasks, media] = await Promise.all([
            nativeDeviceApi.remoteTaskChannel('status'),
            nativeDeviceApi.remoteMediaChannel('status'),
          ])
          applyTaskChannel(tasks)
          mediaChannel.value = media.state
          mediaChannelError.value = media.error || ''
          mediaCaptureActive.value = media.capture_active ?? null
        } else if (connection.value !== 'connected') {
          clearRemoteOpenSshSnapshot()
        }
      } else {
        clearRemoteOpenSshSnapshot()
        remoteCoreState.value = 'unknown'
        taskChannel.value = 'stopped'
        taskChannelError.value = ''
        mediaChannel.value = 'stopped'
        mediaChannelError.value = ''
        mediaCaptureActive.value = null
      }
      void refreshPairedAvailability()
    }
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    loading.value = false
  }
}

/** applyPairedDiscovery（同步配对显示）：发现只更新同编号设备的名称与可用性，不写入配对信任资料。 */
function applyPairedDiscovery(devices: DiscoveredDevice[]) {
  const hostId = status.value?.device_config?.paired_host?.host_id
  if (disposed || !isRemote.value || !hostId) return
  const found = devices.find(device => device.host_id === hostId)
  pairedAvailability.value = !found || found.state === 'unavailable' ? 'missing'
    : !found.compatible ? 'incompatible' : found.state === 'ready' ? 'available' : 'starting'
  if (!found) return
  pairedHostDiscovery.value = { host_id: hostId, display_name: found.display_name }
  // 配对区域已列出的同一服务端同步名称；不新增搜索条目或改变用户选择。
  discoveredDevices.value = discoveredDevices.value.map(device => device.host_id === hostId
    ? { ...device, display_name: found.display_name } : device)
}

/** refreshPairedAvailability（自动发现已配对服务端）：连接中也补查名称，不建立连接或改写配对资料。 */
async function refreshPairedAvailability() {
  const hostId = status.value?.device_config?.paired_host?.host_id
  if (disposed || !isRemote.value || !hostId || searchingDevices.value || pairedDiscoveryRequest) return
  const generation = pairedDiscoveryGeneration
  pairedDiscoveryRequest = (async () => {
    try {
      const result = await nativeDeviceApi.searchDevices(1000)
      if (disposed || !isRemote.value || generation !== pairedDiscoveryGeneration || status.value?.device_config?.paired_host?.host_id !== hostId) return
      applyPairedDiscovery(result.devices)
    } catch { /* 多播暂不可用不能证明服务端离线；保留最近发现状态，仍允许按已保存资料连接。 */ }
  })().finally(() => { pairedDiscoveryRequest = null })
  await pairedDiscoveryRequest
}

/** pollRemoteConnection（后台连接状态）：检测断开和恢复，不显示前台进度，不发起自动连接。 */
async function pollRemoteConnection() {
  const hostId = status.value?.device_config?.paired_host?.host_id
  if (connectionPolling || disposed || !isRemote.value || !hostId) return
  connectionPolling = true
  const current = () => !disposed && !busy.value && !deviceOperationPending.value
    && isRemote.value && status.value?.device_config?.paired_host?.host_id === hostId
  try {
    const ssh = await nativeDeviceApi.sshHostStatus()
    if (!current()) return
    if (!ssh.connected) {
      connection.value = ssh.state
      remoteCoreState.value = 'unknown'; taskChannel.value = 'stopped'; mediaChannel.value = 'stopped'
      mediaCaptureActive.value = null
      clearRemoteOpenSshSnapshot()
    } else if (connection.value !== 'connected') {
      const management = await nativeDeviceApi.remoteManagementRequest('management.status', {})
      if (!current() || management.status !== 200 || !matchesRemoteManagementEnvelope(management, hostId, 'management.status')) return
      connection.value = 'connected'
      remoteCoreState.value = management.body?.core_state === 'ready' ? 'ready'
        : management.body?.core_state === 'starting' ? 'starting' : 'unavailable'
      const [tasks, media] = await Promise.all([nativeDeviceApi.remoteTaskChannel('status'), nativeDeviceApi.remoteMediaChannel('status')])
      if (!current()) return
      applyTaskChannel(tasks); mediaChannel.value = media.state; mediaChannelError.value = media.error || ''
      mediaCaptureActive.value = media.capture_active ?? null
    }
  } catch { /* 暂时无法读取时保留最近状态，下一轮补查。 */ }
  finally {
    if (current()) await refreshPairedAvailability()
    connectionPolling = false
  }
}

async function refreshDeviceChannels() {
  if (inboundRefresh) return
  inboundRefresh = true
  const scope = status.value?.device_config?.device_id
  try {
    if (mode.value === 'local' && isBroadcasting.value) {
      if (deviceState.value?.self_device_id === 'local') {
        inboundDevices.value = deviceState.value
        return
      }
      const devices = await productApi.deviceSnapshot()
      if (mode.value === 'local' && isBroadcasting.value && status.value?.device_config?.device_id === scope
          && deviceState.value?.self_device_id !== 'local') inboundDevices.value = devices
    }
  } catch { /* 读状态失败时保留最后一次状态，服务端实时通道恢复后会推送最新快照。 */
  } finally {
    inboundRefresh = false
  }
}

/** refreshInboundPresence（入站可达状态）：仅更新可信条目，不主动连接或修改授权。 */
async function refreshInboundPresence() {
  const hostId = status.value?.device_config?.device_id
  if (disposed || inboundPresenceReading || !hostId || mode.value !== 'local') return
  inboundPresenceReading = true
  try {
    const result = await nativeDeviceApi.inboundStatus()
    if (!disposed && mode.value === 'local' && status.value?.device_config?.device_id === hostId && result.host_id === hostId) {
      inboundPresence.value = result
      inboundPresenceCheckedAt.value = Date.now()
    }
  } catch { /* 暂时无法发现时保留上次状态，不把读取失败误判为离线。 */ }
  finally { inboundPresenceReading = false }
}

function stopRuntimeStatusWatch() {
  if (runtimeStatusTimer.value !== null) clearTimeout(runtimeStatusTimer.value)
  runtimeStatusTimer.value = null
}

function scheduleRuntimeStatusWatch() {
  stopRuntimeStatusWatch()
  if (disposed || runtimeStatusReading) return
  runtimeStatusTimer.value = setTimeout(async () => {
    runtimeStatusTimer.value = null
    runtimeStatusReading = true
    try {
      const next = await nativeDeviceApi.deviceConfigStatus()
      if (disposed) return
      if ((next.device_runtime?.revision ?? 0) < (status.value?.device_runtime?.revision ?? 0)) return
      if ((next.device_runtime?.operation?.state_revision ?? 0) < (status.value?.device_runtime?.operation?.state_revision ?? 0)) return
      if (!busy.value && !loading.value && !deviceSettingsOperation.active) {
        const keepServerNameDraft = renameOpen.value
        status.value = { ...next }
        if (next.device_config) {
          if (modeDraft.value === mode.value) modeDraft.value = next.device_config.mode
          if (!keepServerNameDraft) serverNameDraft.value = next.device_config.server_name || ''
          mode.value = next.device_config.mode
          broadcastEnabled.value = next.device_config.broadcast_enabled
          clientOnlineTtsLocal.value = next.device_config.client_online_tts_local
          audioCompression.value = next.device_config.audio_compression
        }
      } else if (status.value) {
        status.value = { ...status.value, device_runtime: next.device_runtime }
      }
    } catch { /* 短暂断开时保留最后状态，下一轮补查；不触发系统功能查询。 */ }
    finally {
      runtimeStatusReading = false
      scheduleRuntimeStatusWatch()
    }
  }, 1500)
}

watch([mode, busy, isBroadcasting], scheduleRuntimeStatusWatch)

async function refreshOpenSshStatus() {
  if (disposed) return
  const generation = ++sshStatusGeneration
  if (mode.value !== 'local') {
    openSshStatus.value = null
    openSshStatusError.value = ''
    return
  }
  openSshStatusLoading.value = true
  try {
    const next = await nativeDeviceApi.openSshServerStatus(true)
    if (disposed || mode.value !== 'local' || generation !== sshStatusGeneration) return
    openSshStatus.value = next
    openSshStatusError.value = ''
  } catch (failure) {
    if (disposed || mode.value !== 'local' || generation !== sshStatusGeneration) return
    openSshStatus.value = null
    openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    if (generation === sshStatusGeneration) openSshStatusLoading.value = false
  }
}

function stopOpenSshJobWatch() {
  if (openSshJobTimer.value !== null) clearInterval(openSshJobTimer.value)
  openSshJobTimer.value = null
}

function stopPairingStatusWatch() {
  if (pairingStatusTimer.value !== null) clearInterval(pairingStatusTimer.value)
  pairingStatusTimer.value = null
}

async function refreshPairingWindowStatus() {
  try {
    const current = await nativeDeviceApi.pairingWindowStatus()
    if (current.open) return
    const hadVisibleCode = !!pairingCode.value
    pairingWindowGeneration++
    pairingCode.value = ''
    pairingExpiresAt.value = 0
    if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
    pairingExpiryTimer.value = null
    stopPairingStatusWatch()
    if (hadVisibleCode) notify('配对窗口已使用、关闭或到期，配对码已清除。')
  } catch { /* 状态读取失败时保留当前码，直到有效期计时器到期。 */ }
}

async function refreshOpenSshJob(refreshTerminalStatus = true) {
  if (disposed || mode.value !== 'local') return
  try {
    const wasPending = openSshJobPending.value
    openSshJob.value = (await nativeDeviceApi.resourceJobs())
      .find(item => item.job_id === 'system-feature-openssh-server') || null
    if (disposed || mode.value !== 'local') return
    if (openSshJobPending.value) {
      if (openSshJobTimer.value === null) openSshJobTimer.value = setInterval(() => { void refreshOpenSshJob() }, 1500)
      return
    }
    stopOpenSshJobWatch()
    const job = openSshJob.value
    if (refreshTerminalStatus && (wasPending || openSshLongWaitNotice.value)
      && (job?.state === 'complete' || job?.state === 'failed' || job?.state === 'cancelled')) await refreshOpenSshStatus()
    if (openSshLongWaitNotice.value) {
      openSshLongWaitNotice.value = false
      if (!job) {
        notify('')
        openSshStatusError.value = '无法读取 Windows SSH 准备任务状态；请再次检查 SSH 服务后重试。'
      } else if (job.state === 'complete' && openSshStatus.value?.ready) {
        openSshStatusError.value = ''
        notify('SSH（安全外壳协议）服务正常。')
      } else if (job.state === 'failed') {
        notify('')
        openSshStatusError.value = job.error || 'Windows SSH 服务准备任务失败；请检查任务状态后重试。'
      } else if (job.state === 'cancelled') {
        notify('')
        openSshStatusError.value = job.phase || 'Windows SSH 服务准备已取消；可以再次检查并重试。'
      } else if (job.state === 'complete') {
        notify('')
        openSshStatusError.value ||= '系统任务已完成，但 OpenSSH Server 状态尚未确认就绪；请检查状态后重试。'
      }
    }
  } catch (failure) {
    openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
  }
}

function stopRemoteOpenSshStatusWatch() {
  if (remoteOpenSshPollTimer.value !== null) clearTimeout(remoteOpenSshPollTimer.value)
  remoteOpenSshPollTimer.value = null
}

function clearRemoteOpenSshSnapshot() {
  remoteOpenSshGeneration += 1
  stopRemoteOpenSshStatusWatch()
  remoteOpenSshSnapshot.value = null
  remoteOpenSshHostId.value = null
  remoteOpenSshError.value = ''
}

function scheduleRemoteOpenSshStatusWatch() {
  if (!isRemote.value || !remoteOpenSshJobPending.value || remoteOpenSshBusy.value
    || connection.value !== 'connected' || remoteOpenSshPollTimer.value !== null) return
  remoteOpenSshPollTimer.value = setTimeout(() => {
    remoteOpenSshPollTimer.value = null
    void checkRemoteOpenSshStatus()
  }, 1500)
}

function remoteSshResponseError(response: Awaited<ReturnType<typeof nativeDeviceApi.remoteManagementRequest>>): string {
  const body = response.body
  if (typeof body.detail === 'string') return body.detail
  if (typeof body.error === 'string') return body.error
  if (typeof body.message === 'string') return body.message
  return `服务端 SSH 操作失败（HTTP ${response.status}）。`
}

async function checkRemoteOpenSshStatus() {
  if (!isRemote.value || remoteOpenSshBusy.value) return
  const hostId = status.value?.device_config?.paired_host?.host_id
  if (!hostId) {
    remoteOpenSshError.value = '请先配对服务端，再检查服务端 SSH 状态。'
    return
  }
  if (connection.value !== 'connected') {
    remoteOpenSshError.value = '服务端管理连接尚未就绪，暂时无法读取 SSH 状态。'
    return
  }
  remoteOpenSshBusy.value = true
  remoteOpenSshError.value = ''
  const generation = ++remoteOpenSshGeneration
  try {
    const response = await nativeDeviceApi.remoteManagementRequest('system.ssh.status', {})
    if (response.status !== 200) throw new Error(remoteSshResponseError(response))
    if (!matchesRemoteManagementEnvelope(response, hostId, 'system.ssh.status')) {
      throw new Error('服务端 SSH 状态回执的主机、操作或连接代次无效。')
    }
    const snapshot = parseRemoteSshSystemFeatureSnapshot(response.body)
    if (generation !== remoteOpenSshGeneration || !isRemote.value
      || status.value?.device_config?.paired_host?.host_id !== hostId || connection.value !== 'connected') return
    remoteOpenSshSnapshot.value = snapshot
    remoteOpenSshHostId.value = hostId
    if (snapshot.job?.state === 'failed' && !snapshot.status.ready) {
      remoteOpenSshError.value = snapshot.job.error || '服务端 OpenSSH Server 准备任务失败。'
    }
    scheduleRemoteOpenSshStatusWatch()
  } catch (failure) {
    if (generation === remoteOpenSshGeneration) {
      stopRemoteOpenSshStatusWatch()
      remoteOpenSshSnapshot.value = null
      remoteOpenSshHostId.value = null
      remoteOpenSshError.value = failure instanceof Error ? failure.message : '无法读取服务端 SSH 状态。'
    }
  } finally {
    remoteOpenSshBusy.value = false
    if (generation === remoteOpenSshGeneration) scheduleRemoteOpenSshStatusWatch()
  }
}

async function enableRemoteOpenSshServer() {
  const hostId = status.value?.device_config?.paired_host?.host_id
  const current = remoteOpenSshSnapshot.value
  if (!isRemote.value || !hostId || !current || remoteOpenSshHostId.value !== hostId
    || current.status.ready || remoteOpenSshBusy.value || connection.value !== 'connected') return
  const confirmedGeneration = remoteOpenSshGeneration
  if (!await requestConfirmation({
    title: '准备服务端 SSH 服务', confirmLabel: '确认准备服务端 SSH',
    message: `配对服务端“${pairedHostName.value || '已配对服务端'}”的 OpenSSH Server（安全外壳服务端）尚未就绪。继续后会在该服务端安装或启动微软 OpenSSH Server；缺失时由服务端通过 Windows Update（系统更新）下载，并可能在服务端显示管理员授权窗口。`,
  })) return
  // 确认等待期间可能断线或更换配对目标；旧确认不得用于另一台服务端。
  if (disposed || confirmedGeneration !== remoteOpenSshGeneration || !isRemote.value
    || status.value?.device_config?.paired_host?.host_id !== hostId || connection.value !== 'connected') return

  remoteOpenSshBusy.value = true
  remoteOpenSshError.value = ''
  const generation = ++remoteOpenSshGeneration
  try {
    const response = await nativeDeviceApi.remoteManagementRequest('system.ssh.enable', { confirmed: true })
    if (response.status !== 200 && response.status !== 202) throw new Error(remoteSshResponseError(response))
    if (!matchesRemoteManagementEnvelope(response, hostId, 'system.ssh.enable')) {
      throw new Error('服务端 SSH 操作回执的主机、操作或连接代次无效。')
    }
    const snapshot = parseRemoteSshSystemFeatureSnapshot(response.body)
    if (response.status === 200 && !snapshot.status.ready) {
      throw new Error('服务端报告操作已结束，但 OpenSSH Server 检查仍未就绪。')
    }
    if (response.status === 202 && !snapshot.status.ready && !snapshot.job) {
      throw new Error('服务端已接受 SSH 准备请求，但没有返回受管任务状态。')
    }
    if (generation !== remoteOpenSshGeneration || !isRemote.value
      || status.value?.device_config?.paired_host?.host_id !== hostId || connection.value !== 'connected') return
    remoteOpenSshSnapshot.value = snapshot
    remoteOpenSshHostId.value = hostId
    if (snapshot.job?.state === 'failed' && !snapshot.status.ready) {
      remoteOpenSshError.value = snapshot.job.error || '服务端 OpenSSH Server 准备任务失败。'
    }
    scheduleRemoteOpenSshStatusWatch()
  } catch (failure) {
    if (generation === remoteOpenSshGeneration) {
      remoteOpenSshError.value = failure instanceof Error ? failure.message : '服务端 SSH 准备请求失败。'
      stopRemoteOpenSshStatusWatch()
    }
  } finally {
    remoteOpenSshBusy.value = false
    if (generation === remoteOpenSshGeneration) scheduleRemoteOpenSshStatusWatch()
  }
}

const delay = (milliseconds: number) => new Promise(resolve => setTimeout(resolve, milliseconds))

async function ensureOpenSshServerReady(): Promise<OpenSshServerStatus> {
  let current = await nativeDeviceApi.openSshServerStatus(true)
  openSshStatus.value = current
  if (current.ready) return current
  const details = [
    `Windows 功能：${current.capability_state}`,
    `sshd 服务：${current.service_present ? current.service_state || '状态未知' : '不存在'}`,
    `微软程序：${current.source_path || '未找到'}（${current.source_version || '无版本信息'}；签名 ${current.signature_status || '未知'}）`,
    `有效端口：${current.effective_port ?? '未验证'}；实际监听：${current.listening_ports?.join(', ') || '无'}`,
  ].join('\n')
  if (!await requestConfirmation({
    title: '准备本机 SSH 服务', confirmLabel: '确认下载或启动', details,
    message: '程序会复用并启动已有的微软 OpenSSH（安全外壳服务）；缺失时，通过 Windows Update（系统更新）下载微软推荐的 Windows 可选功能版，程序安装到 %SystemRoot%\\System32\\OpenSSH，默认配置位于 %ProgramData%\\ssh。已有配置和主机密钥继续保留；禁用的服务会改为手动启动。系统可能显示管理员授权窗口。',
  })) {
    throw new Error('你取消了 SSH 服务准备；本机配置未改动。')
  }
  if (disposed) throw new Error('设备设置页面已关闭，尚未提交 SSH 服务准备任务。')
  if (openSshJobPending.value || openSshBusy.value) throw new Error('Windows SSH 功能任务正在进行，请完成后再检查。')
  openSshBusy.value = true
  try {
    const started = await nativeDeviceApi.enableOpenSshServer(true)
    const startedAt = Date.now() // startedAt（正式系统功能任务开始等待的时间）。
    const deadline = startedAt + 31 * 60_000 // deadline（覆盖原生三十分钟执行时限及一分钟回执余量）。
    let completed = false
    while (Date.now() < deadline) {
      const job = (await nativeDeviceApi.resourceJobs()).find(item => item.job_id === started.job_id) || null
      openSshJob.value = job
      if (job?.state === 'failed' || job?.state === 'cancelled') throw new Error(job.error || 'Windows SSH 服务准备任务未完成。')
      if (job?.state === 'complete') { completed = true; break }
      if (!openSshLongWaitNotice.value && Date.now() - startedAt >= 5 * 60_000) {
        openSshLongWaitNotice.value = true
        openSshStatusError.value = 'Windows SSH 准备任务仍在进行；程序会继续跟踪，完成后接着应用本次设备设置。'
      }
      await delay(1200)
    }
    if (!completed) {
      if (openSshJobPending.value) {
        openSshLongWaitNotice.value = true
        notify('')
        throw new Error('Windows SSH 准备任务仍在进行；页面会继续自动跟踪结果，请等待任务完成后再重试。')
      }
      throw new Error('未能确认 Windows SSH 准备任务结果；请刷新状态后重试。')
    }
    current = await nativeDeviceApi.openSshServerStatus()
    openSshStatus.value = current
    if (!current.ready) throw new Error('系统任务已结束，但 OpenSSH 有效配置尚未确认；请检查服务配置、主机密钥权限和实际监听端口。')
    openSshLongWaitNotice.value = false
    openSshStatusError.value = ''
    return current
  } finally {
    openSshBusy.value = false
    await refreshOpenSshJob()
  }
}

async function checkOpenSshServer() {
  if (mode.value !== 'local' || openSshBusy.value || openSshJobPending.value) return
  openSshBusy.value = true
  openSshLongWaitNotice.value = false
  openSshStatusError.value = ''
  notify('')
  try {
    const current = await nativeDeviceApi.openSshServerStatus(true)
    openSshStatus.value = current
    if (current.ready) {
      notify('SSH（安全外壳协议）服务正常。')
      return
    }
    openSshBusy.value = false
    await ensureOpenSshServerReady()
    notify('SSH（安全外壳协议）服务正常。')
  } catch (failure) {
    openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    openSshBusy.value = false
  }
}

async function openPairingWindow() {
  if (controlsLocked.value || !isBroadcasting.value || !status.value?.device_runtime?.broadcast_actual) return
  const generation = ++pairingWindowGeneration
  openingPairing.value = true
  activityPhase.value = '正在获取本机 SSH（安全外壳协议）公钥并生成配对码…'
  busy.value = true; error.value = ''; notify('正在获取本机 SSH 公钥并生成配对码…')
  try {
    const result = await nativeDeviceApi.openPairingWindow()
    if (disposed || generation !== pairingWindowGeneration) return
    pairingCode.value = result.pairing_code
    pairingExpiresAt.value = Date.now() + result.expires_in_seconds * 1000
    if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
    stopPairingStatusWatch()
    pairingStatusTimer.value = setInterval(() => { void refreshPairingWindowStatus() }, 1000)
    pairingExpiryTimer.value = setTimeout(() => {
      pairingWindowGeneration++
      pairingCode.value = ''
      pairingExpiresAt.value = 0
      stopPairingStatusWatch()
      notify('配对窗口已过期；如需再次配对，请重新获取配对码。')
    }, result.expires_in_seconds * 1000)
    notify('配对窗口已开启，可输入六位码配对；正在尝试自动复制。')
    void copyPairingCode(true)
  } catch (failure) { notify(''); error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false; openingPairing.value = false; activityPhase.value = '' }
}

async function copyPairingCode(automatic = false) {
  if (!pairingCode.value) return
  const code = pairingCode.value // code（本次复制的六位码，不进入进度或错误摘要）。
  const generation = pairingWindowGeneration
  try {
    await navigator.clipboard.writeText(code)
    if (!disposed && generation === pairingWindowGeneration && pairingCode.value === code) {
      notify(automatic ? '配对窗口已开启，六位配对码已复制。' : '六位配对码已复制到剪贴板。')
    }
  } catch {
    if (!disposed && generation === pairingWindowGeneration && pairingCode.value === code) {
      if (automatic) notify('配对窗口已开启。剪贴板不可用，请手动抄录六位配对码。')
      else error.value = '剪贴板不可用，请手动抄录本页显示的配对码。'
    }
  }
}

async function closePairingWindow() {
  if (busy.value) return
  activityPhase.value = '正在关闭配对码和本机配对监听器…'
  busy.value = true; error.value = ''; notify('')
  try {
    await nativeDeviceApi.closePairingWindow()
    pairingWindowGeneration++
    pairingCode.value = ''
    pairingExpiresAt.value = 0
    stopPairingStatusWatch()
    if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
    pairingExpiryTimer.value = null
    notify('六位码和本机配对监听器已关闭。')
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false; activityPhase.value = '' }
}

async function saveConfig(preserveInvalid = false, serverName?: string): Promise<boolean> {
  if (busy.value || !canSave.value) return false
  if (status.value?.runtime.config_state === 'invalid' && !preserveInvalid) return false
  const savedConfig = status.value?.device_config
  const nextAudioMode: DeviceConfig['audio_mode'] = audioCompression.value ? 'opus_experimental' : 'pcm'
  if (mediaChannel.value === 'running' && savedConfig && nextAudioMode !== savedConfig.audio_mode) {
    error.value = '更改音频传输模式前，请先停止当前远端语音媒体通道。'
    return false
  }
  if (preserveInvalid && !await requestConfirmation({
    title: '归档原件并修复设备配置', confirmLabel: '确认归档并修复',
    message: '程序会先将当前无法识别的设备配置原件归档并保留，再保存这份新配置。',
  })) return false
  if (disposed) return false
  busy.value = true; error.value = ''; notify('')
  setDeviceSettingsPhase('正在应用设备设置；如出现 Windows 管理员授权，请完成授权。')
  try {
    const config: DeviceConfig = {
      schema_version: 3,
      device_id: deviceId.value.trim() || newDeviceId(),
      server_name: serverName ?? savedConfig?.server_name ?? '',
      mode: mode.value,
      broadcast_enabled: mode.value === 'local' && broadcastEnabled.value,
      client_online_tts_local: clientOnlineTtsLocal.value,
      audio_compression: audioCompression.value,
      reconnect_suspended_reason: mode.value === savedConfig?.mode ? savedConfig.reconnect_suspended_reason : null,
      desired_role: mode.value === 'remote' ? 'remote_client' : broadcastEnabled.value ? 'shared_host' : 'standalone',
      audio_mode: nextAudioMode,
      paired_host: savedConfig?.paired_host ?? savedConfig?.host ?? null,
    }
    if (config.mode === 'local' && config.broadcast_enabled) {
      setDeviceSettingsPhase('正在检查 SSH（安全外壳协议）服务；缺失时等待下载确认')
      await ensureOpenSshServerReady()
      setDeviceSettingsPhase('正在应用设备设置；正在准备局域网访问，如出现 Windows 管理员授权，请完成授权。')
    }
    await nativeDeviceApi.saveDeviceConfig(config, preserveInvalid)
    notify(preserveInvalid ? '新配置已保存；无法识别的原件已归档并保留。' : '设备配置已保存。')
    await refresh()
    return true
  } catch (failure) {
    const message = failure instanceof Error ? failure.message : String(failure)
    // 原生层可能已保存配置后才在公告发布阶段失败；重读权威配置，避免开关误回退。
    await refresh()
    error.value = message
    recordDeviceSettingsError(message)
    return false
  } finally { busy.value = false }
}

async function onRemotePreferenceChanged() {
  if (busy.value || loading.value || !isRemote.value) return
  const saved = status.value?.device_config
  if (!saved || saved.mode !== 'remote') return

  const nextAudioMode: DeviceConfig['audio_mode'] = audioCompression.value ? 'opus_experimental' : 'pcm'
  const ttsChanged = clientOnlineTtsLocal.value !== saved.client_online_tts_local
  const compressionChanged = audioCompression.value !== saved.audio_compression || nextAudioMode !== saved.audio_mode
  if (!ttsChanged && !compressionChanged) return

  const wasTaskRunning = connection.value === 'connected' && taskChannel.value === 'running'
  const wasMediaRunning = connection.value === 'connected' && mediaChannel.value === 'running'
  let configSaved = false
  busy.value = true
  activityPhase.value = '正在调整远程连接通道并保存设置…'
  error.value = ''
  notify('')
  try {
    // 媒体必须先停；在线语音能力是在任务会话握手时协商的，因此其开关变化要重建任务会话。
    if (wasMediaRunning && (ttsChanged || compressionChanged)) {
      await nativeDeviceApi.remoteMediaChannel('stop')
      mediaChannel.value = 'stopped'
      mediaCaptureActive.value = null
    }
    if (wasTaskRunning && ttsChanged) {
      await nativeDeviceApi.remoteTaskChannel('stop')
      taskChannel.value = 'stopped'
    }

    const next: DeviceConfig = {
      ...saved,
      client_online_tts_local: clientOnlineTtsLocal.value,
      audio_compression: audioCompression.value,
      audio_mode: nextAudioMode,
    }
    await nativeDeviceApi.saveDeviceConfig(next)
    configSaved = true
    activityPhase.value = '正在按新设置恢复任务与语音通道…'

    if (connection.value === 'connected' && wasTaskRunning && ttsChanged) {
      const tasks = await nativeDeviceApi.remoteTaskChannel('start')
      applyTaskChannel(tasks)
      if (tasks.state !== 'running') throw new Error(tasks.error || '设置已保存，但远程任务通道重建失败。')
      if (wasMediaRunning) {
        const media = await nativeDeviceApi.remoteMediaChannel('start')
        mediaChannel.value = media.state
        mediaChannelError.value = media.error || ''
        mediaCaptureActive.value = media.capture_active ?? null
        if (media.state !== 'running') throw new Error(media.error || '设置已保存，但远程语音通道重建失败。')
      }
    } else if (connection.value === 'connected' && wasMediaRunning && compressionChanged) {
      const media = await nativeDeviceApi.remoteMediaChannel('start')
      mediaChannel.value = media.state
      mediaChannelError.value = media.error || ''
      mediaCaptureActive.value = media.capture_active ?? null
      if (media.state !== 'running') throw new Error(media.error || '设置已保存，但远程语音通道重建失败。')
    }

    status.value = await nativeDeviceApi.deviceConfigStatus()
    notify('本设备连接设置已保存。')
  } catch (failure) {
    if (!configSaved) {
      clientOnlineTtsLocal.value = saved.client_online_tts_local
      audioCompression.value = saved.audio_compression
      if (wasTaskRunning && ttsChanged && connection.value === 'connected') {
        try {
          const tasks = await nativeDeviceApi.remoteTaskChannel('start')
          applyTaskChannel(tasks)
          if (tasks.state === 'running' && wasMediaRunning) {
            const media = await nativeDeviceApi.remoteMediaChannel('start')
            mediaChannel.value = media.state
            mediaChannelError.value = media.error || ''
            mediaCaptureActive.value = media.capture_active ?? null
          }
        } catch { /* 原始保存错误优先展示；通道状态会由刷新按钮重新读取。 */ }
      } else if (wasMediaRunning && compressionChanged && connection.value === 'connected') {
        try {
          const media = await nativeDeviceApi.remoteMediaChannel('start')
          mediaChannel.value = media.state
          mediaChannelError.value = media.error || ''
          mediaCaptureActive.value = media.capture_active ?? null
        } catch { /* 原始保存错误优先展示；通道状态会由刷新按钮重新读取。 */ }
      }
    }
    error.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    busy.value = false
    activityPhase.value = ''
    if (!configSaved) await refresh()
  }
}

async function onModeChanged() {
  await runDeviceSettingsOperation(changeMode)
}

async function changeMode() {
  const savedMode = status.value?.device_config?.mode
  if (!status.value || savedMode === modeDraft.value) return
  mode.value = modeDraft.value
  if (mode.value === 'local' && savedMode !== 'local') broadcastEnabled.value = false
  if (!await saveConfig()) mode.value = savedMode || (localCoreAvailable.value ? 'local' : 'remote')
  modeDraft.value = mode.value
}

function openRenameDialog() {
  if (controlsLocked.value || !status.value) return
  serverNameDraft.value = serverDisplayName.value
  renameError.value = ''
  renameOpen.value = true
}

function closeRenameDialog() {
  if (busy.value) return
  renameOpen.value = false
  renameError.value = ''
}

async function saveServerName() {
  if (!renameOpen.value || loading.value || busy.value || deviceOperationPending.value || !status.value || !serverNameChanged.value) return
  const name = serverNameDraft.value.trim()
  if (Array.from(name).length > 32 || /[\u0000-\u001f\u007f-\u009f]/u.test(name)) {
    renameError.value = `${isRemote.value ? '客户端' : '服务端'}名称最多 32 个字符，不能包含控制字符。`
    return
  }
  await runDeviceSettingsOperation(async () => {
    if (!status.value?.device_config) {
      if (await saveConfig(false, name)) renameOpen.value = false
      return
    }
    busy.value = true
    renameError.value = ''; notify('')
    try {
      await nativeDeviceApi.saveDeviceConfig({ ...status.value!.device_config!, server_name: name })
      serverNameDraft.value = name
      await refresh()
      renameOpen.value = false
      notify(`${isRemote.value ? '客户端' : '服务端'}名称已保存。`)
    } catch (failure) {
      await refresh()
      renameError.value = failure instanceof Error ? failure.message : String(failure)
    } finally { busy.value = false }
  })
}

async function onBroadcastChanged() {
  await runDeviceSettingsOperation(changeBroadcast)
}

async function changeBroadcast() {
  const savedValue = status.value?.device_config?.broadcast_enabled ?? false
  if (broadcastEnabled.value && !savedValue && (openSshBusy.value || openSshJobPending.value)) {
    broadcastEnabled.value = savedValue
    openSshStatusError.value = 'Windows SSH 功能任务正在进行，请完成后再开启广播。'
    return
  }
  // SSH（安全外壳协议）预检查和下载确认通过后，再提交广播保存与局域网访问准备。
  if (!await saveConfig()) broadcastEnabled.value = status.value?.device_config?.broadcast_enabled ?? savedValue
}

async function searchForDevices() {
  if (busy.value || searchingDevices.value || !isRemote.value) return
  searchingDevices.value = true; error.value = ''; notify('')
  try {
    await pairedDiscoveryRequest
    if (status.value?.device_config?.mode !== 'remote') {
      if (!await saveConfig()) throw new Error(error.value || '无法保存远程配对模式。')
    }
    const generation = pairedDiscoveryGeneration
    const result = await nativeDeviceApi.searchDevices(3000)
    if (disposed || !isRemote.value || generation !== pairedDiscoveryGeneration) return
    discoveredDevices.value = result.devices
    applyPairedDiscovery(result.devices)
    selectedHostId.value = result.devices.find(device => device.compatible && device.state !== 'unavailable')?.host_id || ''
    notify(result.devices.length === 0
      ? '没有发现可配对服务端。请确认服务端已开启本地广播。'
      : `搜索到 ${result.devices.length} 台服务端设备。`)
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { searchingDevices.value = false }
}

async function activateRemoteChannels() {
  const state = await ensureRemoteClientRuntime()
  if (state.state === 'waiting' && state.reason === 'version_mismatch') {
    notify('双端程序版本不同，等待确认版本同步。')
    return
  }
  if (state.state === 'waiting') throw new Error('远程模式尚未完成配对，连接通道没有启动。')
  if (state.state === 'management_only') {
    taskChannel.value = 'stopped'
    mediaChannel.value = 'stopped'
    taskChannelError.value = ''
    mediaChannelError.value = ''
    mediaCaptureActive.value = null
    notify(state.core_state === 'starting'
      ? '服务端管理已连接，语音核心正在准备；组件管理仍可使用。'
      : '服务端管理已连接，语音核心暂不可用；组件管理仍可使用。')
    return
  }
  applyTaskChannel(state.task)
  mediaChannel.value = state.media.state
  mediaChannelError.value = state.media.error || ''
  mediaCaptureActive.value = state.media.capture_active ?? null
}

async function pairWithSelectedDevice() {
  if (busy.value || !selectedHostId.value || !/^[0-9]{6}$/.test(pairingCodeInput.value)) return
  busy.value = true; error.value = ''; notify('')
  activityPhase.value = '正在验证配对、登记受限公钥并建立 SSH（安全外壳协议）连接；如需管理员授权，请在服务端确认。'
  notify('正在验证配对并登记受限公钥；服务端可能需要 Windows 管理员确认，请在服务端完成授权。')
  try {
    const result = await nativeDeviceApi.pairWithDiscoveredDevice(selectedHostId.value, pairingCodeInput.value)
    pairingCodeInput.value = ''
    notify('')
    if (!result.connected) {
      status.value = await nativeDeviceApi.deviceConfigStatus()
      connection.value = 'disconnected'
      error.value = `配对和服务端身份保存成功，但 SSH 桥接连接失败：${result.connection_error || '原因未提供'}。点击“连接”重试，无需重新配对。`
      return
    }
    connection.value = 'connected'
    activityPhase.value = '配对连接已建立，正在准备任务与语音通道…'
    notify(`已连接到 ${result.display_name}。正在准备任务与语音通道。`)
    await activateRemoteChannels()
    await refresh()
  } catch (failure) {
    notify('')
    error.value = failure instanceof Error ? failure.message : String(failure)
  }
  finally { busy.value = false; activityPhase.value = '' }
}

async function connectHost(automatic = false) {
  if (busy.value || !isRemote.value || !status.value?.device_config?.paired_host) return
  if (pairingNeedsRepair.value) {
    if (!automatic) {
      await searchForDevices()
      const hostId = status.value?.device_config?.paired_host?.host_id
      if (hostId && discoveredDevices.value.some(device => device.host_id === hostId)) selectedHostId.value = hostId
      notify('请在远程配对区域输入有效配对码，验证后替换旧信任。')
    }
    return
  }
  busy.value = true
  activityPhase.value = '正在连接配对服务端并准备管理、任务与语音通道…'
  if (!automatic) { error.value = ''; notify('') }
  try {
    const saved = status.value.device_config
    if (saved.reconnect_suspended_reason) {
      const resumed = { ...saved, reconnect_suspended_reason: null }
      await nativeDeviceApi.saveDeviceConfig(resumed)
      status.value = await nativeDeviceApi.deviceConfigStatus()
    }
    const runtime = await ensureRemoteClientRuntime()
    if (runtime.state === 'waiting' && runtime.reason === 'version_mismatch') {
      notify('双端程序版本不同，请确认同步后继续连接。')
      showLanVersionSync()
      return
    }
    if (runtime.state === 'waiting') throw new Error('当前设备尚未完成远程配对，不能连接服务端。')
    connection.value = 'connected'
    if (runtime.state === 'ready') {
      applyTaskChannel(runtime.task)
      mediaChannel.value = runtime.media.state
      mediaChannelError.value = runtime.media.error || ''
      mediaCaptureActive.value = runtime.media.capture_active ?? null
    } else {
      taskChannel.value = 'stopped'
      taskChannelError.value = ''
      mediaChannel.value = 'stopped'
      mediaChannelError.value = ''
      mediaCaptureActive.value = null
    }
    if (!automatic && runtime.state === 'ready') notify(`已连接到 ${pairedHostName.value || '已配对服务端'}。`)
    if (!automatic && runtime.state === 'management_only') {
      notify(runtime.core_state === 'starting'
        ? '服务端管理已连接，语音核心正在准备；组件管理仍可使用。'
        : '服务端管理已连接，语音核心暂不可用；组件管理仍可使用。')
    }
  } catch (failure) {
    connection.value = 'disconnected'
    if (!automatic) error.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    busy.value = false
    activityPhase.value = ''
    const connectionError = error.value // connectionError（刷新连接状态前保留本次连接失败原因）。
    await refresh()
    if (connectionError) error.value = connectionError
  }
}

async function disconnectHost() {
  if (busy.value) return
  busy.value = true; error.value = ''; notify('')
  activityPhase.value = '正在暂停自动重连并关闭服务端通道…'
  try {
    // 先保存暂停意图，后台唯一监督器不能在通道关闭间隙自动重连。
    const saved = status.value?.device_config
    if (saved) await nativeDeviceApi.saveDeviceConfig({ ...saved, reconnect_suspended_reason: 'user_disconnected' })
    if (mediaChannel.value === 'running') await nativeDeviceApi.remoteMediaChannel('stop')
    if (taskChannel.value === 'running') await nativeDeviceApi.remoteTaskChannel('stop')
    await nativeDeviceApi.disconnectSshHost()
    connection.value = 'disconnected'
    taskChannel.value = 'stopped'
    mediaChannel.value = 'stopped'
    mediaCaptureActive.value = null
    notify('已断开，配对资料已保留。')
    await refresh()
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false; activityPhase.value = '' }
}

async function removeHost() {
  const host = status.value?.device_config?.paired_host
  if (!host || controlsLocked.value || canDisconnect.value) return
  if (!await requestConfirmation({
    title: '移除服务端', confirmLabel: '确认移除',
    message: `移除“${pairedHostName.value}”及其在本机保存的密钥、地址和配对资料。再次连接需要重新输入配对码。`,
  })) return
  if (disposed || canDisconnect.value || status.value?.device_config?.paired_host?.host_id !== host.host_id) return
  await runDeviceSettingsOperation(async () => {
    busy.value = true; error.value = ''; notify('')
    activityPhase.value = '正在移除服务端配对资料…'
    try {
      await nativeDeviceApi.removePairedHost(host.host_id, true)
      clearRemoteOpenSshSnapshot()
      pairedAvailability.value = 'unknown'
      discoveredDevices.value = []; selectedHostId.value = ''; pairingCodeInput.value = ''
      await refresh()
      notify('服务端已移除；再次连接需要重新配对。')
    } catch (failure) {
      await refresh()
      error.value = failure instanceof Error ? failure.message : String(failure)
    } finally { busy.value = false; activityPhase.value = '' }
  })
}

async function controlInboundClient(deviceId: string, action: 'disconnect' | 'remove') {
  const device = inboundClients.value.find(item => item.device_id === deviceId)
  const hostId = status.value?.device_config?.device_id
  if (!device?.recordToken || !hostId || controlsLocked.value || isRemote.value) return
  if (action === 'remove') {
    if (device.connected || !await requestConfirmation({ title: '移除客户端', confirmLabel: '确认移除',
      message: `移除“${device.displayName}”在服务端保存的公钥和配对授权；再次连接需要重新配对。` })) return
  }
  const current = inboundClients.value.find(item => item.device_id === deviceId)
  if (disposed || current?.recordToken !== device.recordToken || (action === 'remove' && current.connected)) return
  await runDeviceSettingsOperation(async () => {
    busy.value = true; error.value = ''; notify('')
    activityPhase.value = action === 'remove' ? '正在移除客户端配对资料…' : '正在通知客户端暂停重连并断开…'
    try {
      await nativeDeviceApi.controlInboundDevice(hostId, deviceId, device.recordToken, action)
      await refresh()
      notify(action === 'remove' ? '客户端已移除；再次连接需要重新配对。' : '已断开客户端，配对资料已保留；由客户端手动连接恢复。')
    } catch (failure) {
      await refresh()
      error.value = failure instanceof Error ? failure.message : String(failure)
    } finally { busy.value = false; activityPhase.value = '' }
  })
}

onMounted(async () => {
  devicePoll = setInterval(() => {
    inboundPresenceClock.value = Date.now()
    if (busy.value || loading.value) return
    if (!deviceState.value) void refreshDeviceChannels()
    if (mode.value === 'local') void refreshInboundPresence()
    void pollRemoteConnection()
  }, 10000)
  await refresh()
  scheduleRuntimeStatusWatch()
})

onUnmounted(() => {
  if (devicePoll !== undefined) clearInterval(devicePoll)
  disposed = true
  pairingWindowGeneration++
  settleConfirmation(false)
  sshStatusGeneration++
  stopRuntimeStatusWatch()
  stopOpenSshJobWatch()
  stopRemoteOpenSshStatusWatch()
  if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
  stopPairingStatusWatch()
})
</script>

<template>
  <section class="device-settings" aria-labelledby="device-settings-title">
    <SettingsPageHeader section="devices" title-id="device-settings-title" class="device-heading">
      <template #actions><span class="role-state" :data-state="status?.runtime.config_state || 'loading'">{{ status?.runtime.config_state === 'valid' ? '配置有效' : status?.runtime.config_state === 'invalid' ? '配置需处理' : localCoreAvailable ? '本地运行' : '远程待配对' }}</span></template>
    </SettingsPageHeader>

    <ManagedResourceProgress v-if="deviceProgress" :job="deviceProgress" label="设备连接" />
    <p v-if="deviceOperationError" class="device-message error" role="alert">{{ deviceOperationError }}</p>
    <p v-if="status?.runtime.error" class="device-message warning">{{ status.runtime.error }} 原文件已保留，未自动覆盖。</p>
    <p v-if="!localCoreAvailable && mode === 'local'" class="small-text">本机组件尚未完备，可在组件设置中下载或导入；准备好后再开启广播。</p>

    <fieldset class="device-mode-card" :disabled="controlsLocked">
      <div class="device-save-row">
        <div class="form-field"><span>运行模式</span>
          <div class="mode-options" role="group" aria-label="运行模式">
            <button type="button" :class="{ selected: modeDraft === 'local' }" :aria-pressed="modeDraft === 'local'" @click="modeDraft = 'local'">本地运行</button>
            <button type="button" :class="{ selected: modeDraft === 'remote' }" :aria-pressed="modeDraft === 'remote'" @click="modeDraft = 'remote'">远程配对</button>
          </div>
        </div>
        <button type="button" class="save-button" :disabled="!canSave || (modeDraft === mode && !!status?.device_config)" @click="onModeChanged">保存运行模式</button>
      </div>
      <p class="small-text">当前生效：{{ mode === 'local' ? '本地运行' : '远程配对' }}<template v-if="modeDraft !== mode"> · 保存后切换</template></p>
    </fieldset>
    <section v-if="mode === 'local'" class="device-card">
      <header class="device-card-heading"><h3>局域网连接</h3>
        <span class="connection-state broadcast-status" role="status" :data-state="broadcastRuntimeError ? 'error' : status?.device_runtime?.broadcast_actual ? 'connected' : 'disconnected'">{{ broadcastPhaseLabel }}</span>
      </header>
      <div class="device-setting-row server-name-row">
        <p class="setting-content server-name-display"><span>服务端名称：</span><strong :title="serverDisplayName">{{ serverDisplayName }}</strong></p>
        <button type="button" class="outline-button" :disabled="controlsLocked || !status" @click="openRenameDialog">重命名</button>
      </div>
      <p v-if="broadcastRuntimeError && !busy" class="device-message error" role="alert">{{ broadcastRuntimeError }}</p>
      <div class="device-setting-row broadcast-actions">
        <label class="switch-row device-toggle broadcast-toggle"><div><strong>广播并接受配对</strong><small>在局域网中显示本机；关闭后保留已有配对。</small></div>
          <input v-model="broadcastEnabled" type="checkbox" role="switch" aria-label="广播并接受配对" :disabled="controlsLocked" @change="onBroadcastChanged" /><i aria-hidden="true"></i>
        </label>
        <button class="outline-button" :disabled="controlsLocked || !isBroadcasting || !status?.device_runtime?.broadcast_actual" @click="openPairingWindow">获取配对码</button>
      </div>
      <div class="device-setting-row ssh-status-row">
        <div class="setting-content"><span>SSH（安全外壳协议）</span>
        <span class="connection-state" role="status" :data-state="localSshLabel === '正常' ? 'connected' : localSshLabel.includes('异常') ? 'error' : 'disconnected'">{{ localSshLabel }}</span>
        </div>
        <button type="button" class="outline-button" :disabled="controlsLocked || openSshStatusLoading" @click="checkOpenSshServer">检查 SSH 服务</button>
      </div>
      <details v-if="openSshStatus?.existing_ssh_inbound_rules.length" class="device-message warning ssh-rule-details">
        <summary>其他入站规则：{{ openSshStatus.existing_ssh_inbound_rules.length }} 条</summary>
        <ul>
          <li v-for="rule in visibleSshRuleDetails" :key="rule">{{ rule }}</li>
        </ul>
        <p v-if="hiddenSshRuleDetailCount">还有 {{ hiddenSshRuleDetailCount }} 条规则未展开显示。</p>
      </details>
      <p v-if="openSshStatusError" :class="['device-message', openSshLongWaitNotice ? 'warning' : 'error']" :role="openSshLongWaitNotice ? 'status' : 'alert'">{{ openSshStatusError }}</p>
      <p v-if="visibleOpenSshJob && !openSshJobPending" class="small-text" role="status">任务状态：{{ openSshJobStateLabel(visibleOpenSshJob.state) }}；{{ visibleOpenSshJob.phase }}<span v-if="visibleOpenSshJob.error">：{{ visibleOpenSshJob.error }}</span></p>
    </section>
    <section v-else class="device-card client-name-card" aria-label="客户端名称">
      <div class="device-setting-row server-name-row">
        <p class="setting-content server-name-display"><span>客户端名称：</span><strong :title="serverDisplayName">{{ serverDisplayName }}</strong></p>
        <button type="button" class="outline-button" :disabled="controlsLocked || !status" @click="openRenameDialog">重命名</button>
      </div>
    </section>

    <section v-if="isRemote" class="device-card remote-pairing-card">
      <header class="device-card-heading"><h3>远程配对</h3>
        <button type="button" class="outline-button" :disabled="controlsLocked || searchingDevices" @click="searchForDevices">{{ searchingDevices ? '正在搜索…' : '搜索设备' }}</button>
      </header>
      <p v-if="discoveredDevices.length === 0" class="small-text">搜索到的服务端会显示在这里；无需填写地址、账户或密钥。</p>
      <label v-if="discoveredDevices.length > 0">选择服务端
        <select v-model="selectedHostId" aria-label="选择服务端" :disabled="controlsLocked || searchingDevices">
          <option v-for="device in discoveredDeviceRows" :key="device.host_id" :value="device.host_id" :disabled="!device.compatible || device.state === 'unavailable'">
            {{ device.displayName }} · {{ device.state === 'ready' ? '可连接' : device.state === 'starting' ? '正在准备' : '不可用' }}{{ device.compatible ? '' : ' · 需更新程序' }}
          </option>
        </select>
      </label>
      <div v-if="selectedHostId" class="device-save-row pairing-submit-row">
        <label>六位配对码
          <input v-model="pairingCodeInput" aria-label="六位配对码" :disabled="controlsLocked" autocomplete="one-time-code" inputmode="numeric" maxlength="6" pattern="[0-9]{6}" placeholder="输入服务端显示的配对码" @input="pairingCodeInput = pairingCodeInput.replace(/[^0-9]/g, '').slice(0, 6)" />
        </label>
        <button type="button" class="save-button" :disabled="controlsLocked || !/^[0-9]{6}$/.test(pairingCodeInput)" @click="pairWithSelectedDevice">连接</button>
      </div>
      <p v-if="discoveredDevices.some(device => !device.compatible)" class="device-message warning">列表中有服务端使用不兼容的发现协议，请更新其中一端。</p>

      <div class="remote-ssh-feature-card" role="group" aria-label="服务端 SSH 服务设置">
      <div class="device-setting-row ssh-status-row">
        <div class="setting-content"><span>服务端 SSH（安全外壳协议）</span><span class="connection-state" role="status" :data-state="remoteSshLabel === '正常' ? 'ready' : 'not-ready'">{{ remoteSshLabel }}</span></div>
        <button class="outline-button" :disabled="busy || loading || remoteOpenSshBusy || connection !== 'connected' || !status?.device_config?.paired_host" @click="checkRemoteOpenSshStatus">
          检查 SSH 服务
        </button>
      </div>
      <div v-if="remoteOpenSshCanEnable" class="button-row">
        <button class="outline-button" :disabled="busy || loading || remoteOpenSshBusy" @click="enableRemoteOpenSshServer">
          {{ remoteOpenSshSnapshot?.job?.state === 'failed' ? '重试服务端 SSH 准备' : '请求服务端安装或启动' }}
        </button>
      </div>
      <template v-if="remoteOpenSshSnapshot && remoteOpenSshHostId === status?.device_config?.paired_host?.host_id">
        <details v-if="remoteOpenSshSnapshot.job && remoteOpenSshJobIsHistorical" class="remote-device-row remote-ssh-job-history">
          <summary>历史准备任务：{{ openSshJobStateLabel(remoteOpenSshSnapshot.job.state) }}（当前服务已就绪）</summary>
          <p class="small-text">此记录属于此前的准备任务；本次检查已确认服务就绪。</p>
          <p v-if="remoteOpenSshJobCreatedAt" class="small-text">
            任务创建时间：<time :datetime="remoteOpenSshJobCreatedAt.toISOString()">{{ remoteOpenSshJobCreatedAt.toLocaleString('zh-CN', { hour12: false }) }}</time>
          </p>
          <p class="small-text">当时阶段：{{ remoteOpenSshSnapshot.job.phase }}</p>
          <p v-if="remoteOpenSshSnapshot.job.error" class="small-text">历史错误：{{ remoteOpenSshSnapshot.job.error }}</p>
        </details>
        <article v-else-if="remoteOpenSshSnapshot.job" class="remote-device-row" aria-live="polite">
          <div class="remote-device-title">
            <strong>服务端任务：{{ openSshJobStateLabel(remoteOpenSshSnapshot.job.state) }}</strong>
            <code>{{ remoteOpenSshSnapshot.job.job_id }}</code>
          </div>
          <p>{{ remoteOpenSshSnapshot.job.phase }}</p>
          <p v-if="remoteOpenSshJobPending && /管理员授权|用户账户控制|UAC/i.test(remoteOpenSshSnapshot.job.phase)" class="device-message warning" role="status">
            服务端正在等待 Windows UAC（用户账户控制）授权；确认完成前仍以服务端只读快照判断就绪状态。
          </p>
          <p v-if="remoteOpenSshSnapshot.job.error" class="device-message error">{{ remoteOpenSshSnapshot.job.error }}</p>
        </article>
      </template>
      <p v-if="remoteOpenSshError" class="device-message error" role="alert">{{ remoteOpenSshError }}</p>
      </div>
      <div class="remote-pairing-preferences">
        <label class="switch-row device-toggle remote-preference-toggle"><div><strong>在线语音在本机合成并播放</strong><small>配对后由本机语音服务合成，音频不回传服务端。</small></div>
          <input v-model="clientOnlineTtsLocal" type="checkbox" role="switch" aria-label="在线语音在本机合成并播放" :disabled="controlsLocked" @change="onRemotePreferenceChanged" /><i aria-hidden="true"></i>
        </label>
        <label class="switch-row device-toggle remote-preference-toggle"><div><strong>压缩远程音频</strong><small>通过 Opus（音频编码）减少局域网传输数据。</small></div>
          <input v-model="audioCompression" type="checkbox" role="switch" aria-label="压缩远程音频" :disabled="controlsLocked" @change="onRemotePreferenceChanged" /><i aria-hidden="true"></i>
        </label>
      </div>
      <p v-if="audioCompression" class="small-text">Opus 双向压缩仍需完成真实音质及识别对照。</p>
    </section>

    <section v-if="mode === 'local'" class="device-card inbound-clients-card" aria-label="入站客户端">
      <header class="device-card-heading"><h3>入站客户端</h3>
        <button type="button" class="quiet-button" :disabled="loading || busy" @click="refresh">刷新</button>
      </header>
      <p v-if="inboundDevices === null && inboundPresence === null">{{ isBroadcasting ? '正在等待服务端设备状态。' : '广播已关闭。' }}</p>
      <p v-else-if="!inboundClients.length">暂无已登记客户端。</p>
      <article v-for="device in inboundClients" :key="device.device_id" class="server-connection-row remote-device-row">
        <div class="server-connection-info remote-device-title">
          <strong>{{ device.displayName }}</strong>
          <span class="connection-state" :data-state="device.removalPending ? 'error' : device.connected ? 'connected' : device.online ? 'available' : 'disconnected'">{{ device.removalPending ? '待清理' : device.connected ? '已连接' : device.online === null ? '待检查' : device.online ? '在线' : '离线' }}</span>
        </div>
        <div class="server-connection-actions">
          <button v-if="device.connected && !device.removalPending" type="button" class="quiet-button" :aria-label="`断开 ${device.displayName}`" :disabled="controlsLocked || !device.recordToken" @click="controlInboundClient(device.device_id, 'disconnect')">断开</button>
          <button v-else type="button" class="quiet-button" :aria-label="`移除 ${device.displayName}`" :disabled="controlsLocked || !device.recordToken || device.connected" @click="controlInboundClient(device.device_id, 'remove')">{{ device.removalPending ? '重试移除' : '移除' }}</button>
        </div>
      </article>
    </section>

    <section v-if="isRemote" class="device-card remote-channel-status">
      <header class="device-card-heading"><h3>服务端连接状态</h3>
        <button type="button" class="quiet-button" :disabled="loading || busy" @click="refresh">刷新</button>
      </header>
      <p v-if="lanVersionSync.check && lanVersionSync.check.comparison !== 'same'" role="status">
        程序版本不同：本机 {{ lanVersionSync.check.local_version }}，服务端 {{ lanVersionSync.check.remote_version }}。
        <button type="button" class="outline-button" @click="showLanVersionSync">同步程序版本</button>
      </p>
      <article v-if="status?.device_config?.paired_host" class="server-connection-row">
        <div class="server-connection-info">
          <div class="remote-device-title"><strong>{{ pairedHostName }}</strong>
            <span class="connection-state" :data-state="connection === 'connected' || pairedAvailability === 'available' ? 'connected' : connection">{{ pairedConnectionLabel }}</span>
          </div>
          <div v-if="connection === 'connected'" class="channel-status-list">
            <span :data-state="remoteCoreState">{{ remoteCoreState === 'ready' ? '语音就绪' : remoteCoreState === 'starting' ? '语音准备中' : remoteCoreState === 'unavailable' ? '语音异常' : '语音待检查' }}</span>
            <span :data-state="taskChannel">{{ taskChannel === 'running' ? '任务正常' : taskChannel === 'error' ? '任务异常' : '任务准备中' }}</span>
            <span v-if="taskChannel === 'running' && taskDesktopControl" :data-state="taskDesktopControl.available ? 'ready' : 'error'">{{ taskDesktopControl.available ? '桌面正常' : '桌面不可用' }}</span>
            <span :data-state="mediaChannel">{{ ownDevice?.media_ready ? '通道正常' : ownDevice?.connected || mediaChannel === 'standby' ? '待语音授权' : mediaChannel === 'error' ? '通道异常' : '通道准备中' }}</span>
            <span v-if="ownDevice?.media_ready">{{ ownDevice.recording_phase === 'recording' ? '录音中' : ownDevice.recording_phase === 'starting' ? '音频准备中' : ownDevice.recording_phase === 'interrupted' ? '麦克风恢复中' : '未录音' }}</span>
          </div>
          <details v-if="taskChannelError || mediaChannelError || taskDesktopControl?.error" class="connection-error-details">
            <summary>查看异常详情</summary>
            <p v-if="taskChannelError || mediaChannelError" class="small-text">{{ taskChannelError || mediaChannelError }}</p>
            <p v-if="taskDesktopControl?.error" class="small-text">{{ taskDesktopControl.error }}</p>
          </details>
        </div>
        <div class="server-connection-actions">
          <button v-if="canDisconnect" type="button" class="quiet-button" :disabled="controlsLocked" @click="disconnectHost">断开</button>
          <button v-else type="button" class="outline-button" :disabled="controlsLocked || !status || status.runtime.config_state === 'invalid'" @click="connectHost(false)">{{ pairingNeedsRepair ? '重新配对' : '连接' }}</button>
          <button v-if="!canDisconnect" type="button" class="quiet-button remove-host-button" :disabled="controlsLocked" @click="removeHost">移除</button>
        </div>
      </article>
      <p v-else class="small-text">等待配对</p>
    </section>

    <footer v-if="status?.runtime.config_state === 'invalid'" class="device-footer">
        <button v-if="status?.runtime.config_state === 'invalid'" class="outline-button" :disabled="!canSave || busy" @click="saveConfig(true)">归档原件并修复</button>
    </footer>

    <UiDialog :open="renameOpen" :title="isRemote ? '重命名客户端' : '重命名服务端'" kicker="设备显示名称" :busy="busy" @close="closeRenameDialog">
      <form id="rename-server-form" class="rename-server-form" @submit.prevent="saveServerName">
        <label for="device-server-name">{{ isRemote ? '客户端名称' : '服务端名称' }}
          <input id="device-server-name" v-model="serverNameDraft" maxlength="64" autocomplete="off" :placeholder="defaultServerName" :disabled="busy" aria-describedby="server-name-hint" />
        </label>
        <p id="server-name-hint" class="small-text">支持中文，最多 32 个字符。留空恢复默认名称；同名设备在连接列表中自动编号。</p>
        <p v-if="renameError" class="device-message error" role="alert">{{ renameError }}</p>
      </form>
      <template #actions>
        <button type="button" class="outline-button" :disabled="busy" @click="closeRenameDialog">取消</button>
        <button type="submit" form="rename-server-form" class="save-button" :disabled="busy || !serverNameChanged" @click.prevent="saveServerName">保存名称</button>
      </template>
    </UiDialog>

    <UiDialog :open="!!confirmation || !!pairingCode" :title="confirmation?.title || '本机配对码'"
      :kicker="confirmation ? '设备操作确认' : '局域网配对'" :busy="!confirmation && busy" @close="closeDeviceDialog">
      <template v-if="confirmation">
        <p class="device-confirmation-message">{{ confirmation.message }}</p>
        <pre v-if="confirmation.details" class="device-confirmation-details">{{ confirmation.details }}</pre>
      </template>
      <div v-else-if="pairingCode" class="pairing-code-box" role="status">
        <span>本机六位配对码 · 有效至 {{ new Date(pairingExpiresAt).toLocaleTimeString() }}</span>
        <output aria-label="本机六位配对码">{{ pairingCode }}</output>
        <small>在另一台设备输入此码以完成配对。配对成功、关闭窗口或两分钟到期后立即失效。</small>
      </div>
      <p v-if="!confirmation && error" class="device-message error" role="alert">{{ error }}</p>
      <template #actions>
        <template v-if="confirmation">
          <button class="quiet-button" @click="settleConfirmation(false)">取消</button>
          <button class="primary-button" @click="settleConfirmation(true)">{{ confirmation.confirmLabel }}</button>
        </template>
        <template v-else>
          <button class="quiet-button" :disabled="busy" @click="copyPairingCode()">复制配对码</button>
          <button class="primary-button" :disabled="busy" @click="closePairingWindow">关闭配对窗口</button>
        </template>
      </template>
    </UiDialog>
  </section>
</template>

<style scoped>
.device-settings { --device-action-width: 112px; width: 100%; min-width: 0; color: var(--text); font-size: var(--font-body); line-height: 1.6; }
.device-heading { margin-bottom: 20px; }
.device-settings fieldset, .device-card { min-width: 0; margin: 14px 0; padding: 20px; border: 1px solid var(--line); border-radius: 14px; background: #ffffff02; }
.device-card h3 { margin: 0; font-size: var(--font-section); }
.device-card p { color: var(--muted); }
.device-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.device-settings button { min-height: var(--control-height); padding: 8px 16px; border-radius: 10px; font-size: var(--font-body); font-weight: 600; line-height: 1.5; white-space: nowrap; cursor: pointer; }
.device-settings button:disabled { opacity: .45; cursor: not-allowed; }
.primary-button, .save-button { border: 1px solid var(--accent); background: var(--accent); color: #15192a; }
.outline-button, .quiet-button { border: 1px solid var(--line-strong); background: transparent; color: var(--muted); }
.device-settings label { display: flex; flex-direction: column; gap: 8px; margin: 12px 0; color: var(--muted); font-size: var(--font-body); }
.device-settings input, .device-settings select { min-width: 0; width: 100%; min-height: var(--control-height); padding: 8px 12px; border: 1px solid var(--line-strong); border-radius: 10px; background: var(--bg); color: var(--text); font-size: var(--font-body); }
.device-save-row { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: end; gap: 12px; }
.device-mode-card .form-field > span { color: var(--muted); }
.device-mode-card .mode-options { margin-top: 8px; }
.device-mode-card .mode-options button { min-height: var(--compact-control-height); padding: 6px 12px; border: 0; background: transparent; font-weight: 400; }
.device-mode-card .mode-options button.selected { background: #b2baff16; color: var(--accent); }
.device-mode-card > p { margin: 10px 0 0; color: var(--muted); }
.device-setting-row { display: grid; grid-template-columns: minmax(0,1fr) var(--device-action-width); align-items: center; gap: 16px; }
.device-setting-row + .device-setting-row { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line); }
.device-setting-row > button { width: 100%; }
.setting-content { display: flex; min-width: 0; align-items: center; gap: 10px; margin: 0; }
.server-name-display > span { flex: none; }
.server-name-display strong { min-width: 0; overflow: hidden; color: var(--text); font-size: var(--font-body); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.device-settings label.device-toggle { display: flex; flex-direction: row; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 16px; color: var(--text); }
.device-toggle > div { display: grid; min-width: 0; gap: 4px; padding-right: 8px; }
.device-toggle strong { font-size: var(--font-body); font-weight: 600; }
.device-toggle small { color: var(--muted); font-size: var(--font-caption); line-height: 1.6; }
.device-settings label.broadcast-toggle { margin: 0; padding: 0; border: 0; border-radius: 0; background: transparent; }
.device-settings .switch-row input[type=checkbox] { position: absolute; top: 50%; right: 16px; width: 36px; height: 24px; min-height: 0; margin: 0; padding: 0; opacity: 0; transform: translateY(-50%); cursor: pointer; }
.device-settings .broadcast-toggle input[type=checkbox] { right: 0; }
.device-settings .switch-row input:disabled { cursor: not-allowed; }
.device-settings .switch-row input:disabled + i { opacity: .45; }
.role-state, .connection-state { display: inline-flex; flex: none; align-items: center; padding: 5px 9px; border: 1px solid var(--line-strong); border-radius: 999px; color: var(--muted); font-size: var(--font-caption); line-height: 1.4; white-space: nowrap; }
.role-state[data-state=valid], .connection-state[data-state=connected], .connection-state[data-state=ready], .connection-state[data-state=available] { border-color: #286c5a; color: var(--mint); }
.role-state[data-state=invalid], .connection-state[data-state=error] { border-color: #825333; color: #efce8b; }
.remote-ssh-feature-card, .remote-pairing-preferences { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--line); }
.pairing-submit-row { grid-template-columns: minmax(0,1fr) var(--device-action-width); }
.device-settings .pairing-submit-row label { margin: 0; }
.pairing-submit-row > button { width: 100%; }
.device-settings label.remote-preference-toggle { margin: 0; padding: 12px 0; border: 0; border-radius: 0; background: transparent; }
.device-settings .remote-preference-toggle input[type=checkbox] { right: 0; }
.button-row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.small-text { font-size: var(--font-caption); }
.device-message { margin: 10px 0; padding: 10px 12px; border-radius: 10px; background: #1c2b3e; color: #c7d7f0; }
.device-message.error { background: #422727; color: #ffc5b8; }
.device-message.warning { background: #392f1d; color: #efce8b; }
.remote-device-row { padding: 14px 0; border-bottom: 1px solid var(--line); }
.remote-device-title { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.remote-device-title strong { font-size: var(--font-body); overflow-wrap: anywhere; }
.remote-device-title .connection-state { margin-left: auto; }
.remote-device-title code { color: var(--muted); font-size: var(--font-caption); overflow-wrap: anywhere; }
.inbound-clients-card .remote-device-row + .remote-device-row { margin-top: 10px; }
.server-connection-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px; border: 1px solid var(--line); border-radius: 12px; background: #ffffff03; }
.server-connection-info { flex: 1; min-width: 0; }
.server-connection-info .connection-state { margin-left: 0; }
.server-connection-actions { display: flex; flex: none; align-items: center; gap: 8px; }
.channel-status-list { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 10px; color: var(--muted); font-size: var(--font-caption); }
.channel-status-list [data-state=error], .channel-status-list [data-state=unavailable] { color: #efce8b; }
.connection-error-details, .ssh-rule-details { overflow-wrap: anywhere; color: var(--muted); font-size: var(--font-caption); }
.connection-error-details { margin-top: 10px; }
.connection-error-details summary, .ssh-rule-details summary, .remote-ssh-job-history summary { cursor: pointer; }
.ssh-rule-details ul { max-height: 120px; margin: 8px 0 0; padding-left: 20px; overflow: auto; }
.ssh-rule-details p { margin: 8px 0 0; }
.device-footer { display: flex; justify-content: flex-end; padding-top: 12px; }
.device-confirmation-message { white-space: pre-wrap; overflow-wrap: anywhere; }
.device-confirmation-details { padding: 12px; border: 1px solid var(--line); border-radius: 10px; color: var(--muted); font: var(--font-caption) ui-monospace,Consolas,monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.pairing-code-box { display: grid; gap: 8px; padding: 14px; border: 1px solid #785f35; border-radius: 10px; background: #211e18; color: #e1c78e; }
.pairing-code-box output { color: #fff; font: 700 28px ui-monospace,Consolas,monospace; letter-spacing: .18em; }
.pairing-code-box small { color: #cdbb97; }
.rename-server-form label { margin-top: 0; }
.rename-server-form .small-text { margin: 8px 0 0; color: var(--muted); }
@media(max-width:700px) { .device-card, .device-settings fieldset { padding: 16px; } .device-heading { gap: 12px; } .device-setting-row { gap: 12px; } .server-connection-row { align-items: flex-start; flex-wrap: wrap; } .server-connection-actions { margin-left: auto; } }
@media(max-width:460px) { .device-save-row:not(.pairing-submit-row) { grid-template-columns: minmax(0,1fr); } .device-save-row > button { justify-self: end; } .setting-content { flex-wrap: wrap; gap: 6px; } .server-name-display > span { width: 100%; } }
</style>
