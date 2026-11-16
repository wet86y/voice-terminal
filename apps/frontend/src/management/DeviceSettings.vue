<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { nativeDeviceApi, type DeviceConfig, type DeviceConfigStatus, type ManagedResourceJob, type NativeSshIdentity, type NativeSshTransportIdentity, type OpenSshServerStatus, type RemoteDeviceSnapshot } from '../product/native-device-api'
import { productApi, type DevicePairingOpenResult, type DevicePairingStatus } from '../product/api'

const status = ref<DeviceConfigStatus | null>(null)
const role = ref<DeviceConfig['desired_role']>('standalone')
const audioMode = ref<DeviceConfig['audio_mode']>('pcm')
const deviceId = ref('')
const address = ref('')
const port = ref(22)
const username = ref('')
const hostKey = ref('')
const credentialRef = ref('')
const transportKeyPath = ref('')
const transportCredentialRef = ref('')
const transportIdentity = ref<NativeSshTransportIdentity | null>(null)
const pairingCodeInput = ref('')
const pairingSubmission = ref<{ request_id: string; device_id: string; fingerprint: string } | null>(null)
const candidateFingerprint = ref('')
const identity = ref<NativeSshIdentity | null>(null)
const remoteDevices = ref<RemoteDeviceSnapshot | null>(null)
const remoteCheckedAt = ref('')
const pairing = ref<DevicePairingStatus | null>(null)
const pairingCode = ref('')
const pairingTtlSeconds = ref(120)
const helperHealth = ref<'unknown' | 'ready' | 'unavailable' | 'unsupported' | 'stopped'>('unknown')
const connection = ref<'connected' | 'disconnected' | 'unavailable'>('disconnected')
const taskChannel = ref<'running' | 'stopped' | 'error'>('stopped')
const taskChannelError = ref('')
const mediaChannel = ref<'running' | 'stopped' | 'error'>('stopped')
const mediaChannelError = ref('')
const mediaCaptureActive = ref<boolean | null>(null)
const localDeviceSnapshot = ref<Awaited<ReturnType<typeof productApi.deviceSnapshot>> | null>(null)
const openSshStatus = ref<OpenSshServerStatus | null>(null)
const openSshStatusError = ref('')
const openSshJob = ref<ManagedResourceJob | null>(null)
const openSshBusy = ref(false)
const openSshStatusLoading = ref(false)
const openSshJobTimer = ref<ReturnType<typeof setInterval> | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const localCoreAvailable = computed(() => status.value?.local_core_available !== false)
const canSave = computed(() => !loading.value && !busy.value && !openSshBusy.value && !openSshJobPending.value
  && !!status.value && (role.value === 'remote_client' || localCoreAvailable.value))
const isRemote = computed(() => role.value === 'remote_client')
const isSavedSharedHost = computed(() => role.value === 'shared_host'
  && status.value?.device_config?.desired_role === 'shared_host')
// canManagePairing（配对管理就绪）：配置有效不代表本次权威核心与共享网关已经启动。
const canManagePairing = computed(() => isSavedSharedHost.value && localCoreAvailable.value
  && status.value?.device_runtime?.actual_role === 'shared_host'
  && status.value.device_runtime.core_ready && status.value.device_runtime.device_gateway.ready)
const openSshJobPending = computed(() => !!openSshJob.value
  && ['queued', 'running', 'cancelling'].includes(openSshJob.value.state))
const openSshCapabilityPending = computed(() => !!openSshStatus.value
  && ['InstallPending', 'UninstallPending'].includes(openSshStatus.value.capability_state))

function newDeviceId() {
  return `device-${crypto.randomUUID()}`
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const next = await nativeDeviceApi.deviceConfigStatus()
    status.value = next
    const saved = next.device_config
    if (saved) {
      role.value = saved.desired_role
      audioMode.value = saved.audio_mode
      deviceId.value = saved.device_id
      if (saved.host) {
        address.value = saved.host.address
        port.value = saved.host.port
        username.value = saved.host.username
        hostKey.value = saved.host.host_key_sha256
        credentialRef.value = saved.host.credential_ref
        transportCredentialRef.value = saved.host.transport_credential_ref || ''
      } else {
        address.value = ''; port.value = 22; username.value = ''; hostKey.value = ''; credentialRef.value = ''
        transportCredentialRef.value = ''
      }
    } else if (next.runtime.config_state === 'missing') {
      transportCredentialRef.value = ''
      if (!deviceId.value) {
        deviceId.value = newDeviceId()
        role.value = localCoreAvailable.value ? 'standalone' : 'remote_client'
        audioMode.value = 'pcm'
      }
    } else if (next.runtime.config_state === 'invalid' && !deviceId.value) {
      deviceId.value = newDeviceId()
    }
    const identityId = saved?.device_id || deviceId.value || next.runtime.device_id
    if (identityId) {
      identity.value = await nativeDeviceApi.sshIdentityStatus(identityId)
      if (identity.value) credentialRef.value = identity.value.credential_ref
    }
    transportIdentity.value = transportCredentialRef.value
      ? await nativeDeviceApi.sshTransportIdentityStatus(transportCredentialRef.value)
      : null
    const ssh = await nativeDeviceApi.sshHostStatus()
    connection.value = ssh.state
    if (isRemote.value && ssh.connected) {
      try {
        const channel = await nativeDeviceApi.remoteTaskChannel('status')
        taskChannel.value = channel.state
        taskChannelError.value = channel.error || ''
      } catch {
        taskChannel.value = 'stopped'
        taskChannelError.value = ''
      }
      try {
        const media = await nativeDeviceApi.remoteMediaChannel('status')
        mediaChannel.value = media.state
        mediaChannelError.value = media.error || ''
        mediaCaptureActive.value = media.capture_active ?? null
      } catch {
        mediaChannel.value = 'stopped'
        mediaChannelError.value = ''
        mediaCaptureActive.value = null
      }
    } else {
      taskChannel.value = 'stopped'
      taskChannelError.value = ''
      mediaChannel.value = 'stopped'
      mediaChannelError.value = ''
      mediaCaptureActive.value = null
    }
    if (canManagePairing.value) {
      try { localDeviceSnapshot.value = await productApi.deviceSnapshot() }
      catch { localDeviceSnapshot.value = null }
    } else {
      localDeviceSnapshot.value = null
    }
    if (isSavedSharedHost.value) {
      await Promise.all([refreshPairing(), refreshOpenSshStatus(), refreshOpenSshJob()])
    } else {
      pairing.value = null; pairingCode.value = ''
      openSshStatus.value = null; openSshStatusError.value = ''; openSshJob.value = null
      stopOpenSshJobWatch()
    }
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    loading.value = false
  }
}

async function refreshPairing() {
  if (!canManagePairing.value) {
    pairing.value = null
    pairingCode.value = ''
    return
  }
  pairing.value = await productApi.devicePairingStatus()
  pairingCode.value = ''
}

async function refreshOpenSshStatus() {
  if (!isSavedSharedHost.value || !localCoreAvailable.value) {
    openSshStatus.value = null
    openSshStatusError.value = ''
    return
  }
  openSshStatusLoading.value = true
  try {
    openSshStatus.value = await nativeDeviceApi.openSshServerStatus()
    openSshStatusError.value = ''
  } catch (failure) {
    openSshStatus.value = null
    openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    openSshStatusLoading.value = false
  }
}

function stopOpenSshJobWatch() {
  if (openSshJobTimer.value !== null) clearInterval(openSshJobTimer.value)
  openSshJobTimer.value = null
}

async function refreshOpenSshJob() {
  if (!isSavedSharedHost.value || !localCoreAvailable.value) return
  try {
    const job = (await nativeDeviceApi.resourceJobs())
      .find(item => item.job_id === 'system-feature-openssh-server') || null
    openSshJob.value = job
    if (job && ['queued', 'running', 'cancelling'].includes(job.state)) {
      if (openSshJobTimer.value === null) {
        openSshJobTimer.value = setInterval(() => { void refreshOpenSshJob() }, 1500)
      }
      return
    }
    stopOpenSshJobWatch()
    if (job?.state === 'complete' || job?.state === 'failed') await refreshOpenSshStatus()
  } catch (failure) {
    openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
  }
}

async function enableOpenSshServer() {
  if (!isSavedSharedHost.value || !localCoreAvailable.value || !openSshStatus.value || openSshStatus.value.ready
    || openSshCapabilityPending.value || openSshJobPending.value || openSshBusy.value) return
  const accepted = window.confirm(
    '安装 OpenSSH Server 时，Windows 会自动创建并启用 OpenSSH-Server-In-TCP 入站防火墙规则（TCP 端口 22）。这会允许网络上的 SSH 入站连接。程序不会另行修改防火墙、sshd 启动类型或账户公钥；停止共享也不会自动关闭系统 SSH 服务。是否继续并接受随后出现的 Windows 管理员授权提示？',
  )
  if (!accepted) return
  openSshBusy.value = true
  openSshStatusError.value = ''
  try {
    await nativeDeviceApi.enableOpenSshServer()
    await refreshOpenSshJob()
    if (openSshJobTimer.value === null && openSshJob.value?.state !== 'complete') {
      openSshStatusError.value = openSshJob.value?.error || '任务已登记；请刷新状态确认系统结果。'
    }
  } catch (failure) {
    openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    openSshBusy.value = false
  }
}

async function openPairingWindow() {
  if (busy.value || !canManagePairing.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result: DevicePairingOpenResult = await productApi.openDevicePairing(pairingTtlSeconds.value)
    pairing.value = result
    pairingCode.value = result.pairing_code
    notice.value = '配对窗口已开启。配对码只在本次界面显示；批准设备后可继续配对，也可手动关闭窗口。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function closePairingWindow() {
  if (busy.value || !canManagePairing.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    pairing.value = await productApi.closeDevicePairing()
    pairingCode.value = ''
    notice.value = '配对窗口已关闭，待批准请求已清除。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function approvePairing(requestId: string, deviceId: string, fingerprint: string) {
  if (busy.value || !canManagePairing.value || !window.confirm(`批准设备 ${deviceId}？\n公钥指纹：${fingerprint}`)) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    pairing.value = await productApi.approveDevicePairing(requestId)
    pairingCode.value = ''
    notice.value = `已明确批准设备 ${deviceId}。`
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function rejectPairing(requestId: string, deviceId: string) {
  if (busy.value || !canManagePairing.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    pairing.value = await productApi.rejectDevicePairing(requestId)
    notice.value = `已拒绝设备 ${deviceId} 的配对请求。`
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function revokeDevice(deviceId: string, fingerprint: string) {
  if (busy.value || !canManagePairing.value || !window.confirm(`撤销设备 ${deviceId} 的信任？\n公钥指纹：${fingerprint}`)) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    pairing.value = await productApi.revokeDeviceTrust(deviceId)
    notice.value = `已撤销设备 ${deviceId} 的信任。`
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function createIdentity() {
  if (!deviceId.value.trim() || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    identity.value = await nativeDeviceApi.createSshIdentity(deviceId.value.trim())
    credentialRef.value = identity.value.credential_ref
    pairingSubmission.value = null
    notice.value = 'SSH 私钥已保存在本机 Windows 凭据库；连接主机后可签署配对申请。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function importTransportIdentity() {
  const path = transportKeyPath.value.trim()
  if (!path || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    transportIdentity.value = await nativeDeviceApi.importSshTransportIdentity(path)
    transportCredentialRef.value = transportIdentity.value.credential_ref
    notice.value = 'SSH 登录私钥已由原生层导入 Windows 凭据库。请将下方公钥单独加入主机 SSH 用户的 authorized_keys，再保存配置；产品配对批准不会修改主机登录账户。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function deleteTransportIdentity() {
  const reference = transportCredentialRef.value
  const saved = status.value?.device_config
  if (!reference || !saved?.host || busy.value || status.value?.runtime.config_state !== 'valid'
      || !window.confirm('删除此 SSH 登录私钥，并从设备配置中移除其引用？')) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    if (connection.value === 'connected') {
      await nativeDeviceApi.disconnectSshHost()
      connection.value = 'disconnected'
      taskChannel.value = 'stopped'
      taskChannelError.value = ''
    }
    const updated: DeviceConfig = {
      ...saved,
      host: { ...saved.host, transport_credential_ref: undefined },
    }
    status.value = await nativeDeviceApi.saveDeviceConfig(updated)
    await nativeDeviceApi.deleteSshTransportIdentity(reference)
    transportCredentialRef.value = ''
    transportIdentity.value = null
    transportKeyPath.value = ''
    notice.value = 'SSH 登录密钥已从设备配置解除并从本机 Windows 凭据库删除。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function submitPairingRequest() {
  if (busy.value || connection.value !== 'connected' || !identity.value
      || !/^[0-9]{8}$/.test(pairingCodeInput.value)) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await nativeDeviceApi.submitSshPairingRequest(pairingCodeInput.value)
    const body = response.body
    if (response.status !== 202 || typeof body.request_id !== 'string'
        || typeof body.device_id !== 'string' || typeof body.fingerprint !== 'string') {
      const detail = typeof body.detail === 'string' ? body.detail : '主机未接受配对申请。'
      throw new Error(detail)
    }
    pairingSubmission.value = {
      request_id: body.request_id,
      device_id: body.device_id,
      fingerprint: body.fingerprint,
    }
    pairingCodeInput.value = ''
    notice.value = '签名申请已送达主机，须由主机本机明确批准后才会登记信任。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function deleteIdentity() {
  if (!deviceId.value.trim() || busy.value || !window.confirm('删除此设备在本机保存的 SSH 私钥？')) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await nativeDeviceApi.disconnectSshHost()
    await nativeDeviceApi.deleteSshIdentity(deviceId.value.trim())
    identity.value = null; credentialRef.value = ''
    notice.value = '本机 SSH 私钥凭据已删除。'
    connection.value = 'disconnected'
    taskChannel.value = 'stopped'
    taskChannelError.value = ''
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function probeHost() {
  if (!address.value.trim() || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''; candidateFingerprint.value = ''
  try {
    const result = await nativeDeviceApi.probeSshHost(address.value.trim(), port.value)
    candidateFingerprint.value = result.fingerprint
    notice.value = '这是尚未信任的候选指纹。请在共享主机本机核对显示值，再明确确认。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

function acceptFingerprint() {
  if (!candidateFingerprint.value || !window.confirm('你已在共享主机本机核对过完整 SHA-256 指纹？')) return
  hostKey.value = candidateFingerprint.value
  notice.value = '指纹已填入草稿；保存配置后才会用于 SSH 主机验证。'
}

async function saveConfig(preserveInvalid = false) {
  if (busy.value || !canSave.value) return
  if (status.value?.runtime.config_state === 'invalid' && !preserveInvalid) return
  const savedConfig = status.value?.device_config
  if (mediaChannel.value === 'running' && savedConfig && audioMode.value !== savedConfig.audio_mode) {
    error.value = '更改音频传输模式前，请先停止当前远端语音媒体通道。'
    return
  }
  if (preserveInvalid && !window.confirm('先将当前无法识别的设备配置原件归档，再保存这份新配置？')) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const host = isRemote.value ? {
      address: address.value.trim(), port: port.value, username: username.value.trim(),
      host_key_sha256: hostKey.value.trim(), credential_ref: credentialRef.value.trim(),
      ...(transportCredentialRef.value ? { transport_credential_ref: transportCredentialRef.value } : {}),
    } : null
    const config: DeviceConfig = {
      schema_version: 2, device_id: deviceId.value.trim(), desired_role: role.value,
      audio_mode: audioMode.value, host,
    }
    await nativeDeviceApi.saveDeviceConfig(config, preserveInvalid)
    notice.value = preserveInvalid
      ? '新配置已保存；无法识别的原件已放入 data/archives 并保留。'
      : '设备配置已保存。实际角色仍以启动状态为准。'
    await refresh()
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function connectHost() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await nativeDeviceApi.connectSshHost()
    connection.value = 'connected'
    taskChannel.value = 'stopped'
    notice.value = 'SSH 主机已连接。连接不自动恢复语音占用权。'
    await refresh()
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function startRemoteTaskChannel() {
  if (busy.value || connection.value !== 'connected' || !isRemote.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await nativeDeviceApi.remoteTaskChannel('start')
    taskChannel.value = result.state
    taskChannelError.value = result.error || ''
    notice.value = '已登记本机桌面能力，远端任务通道已启动。实际设备动作仍走 DSH 审批。'
  } catch (failure) {
    taskChannel.value = 'error'
    taskChannelError.value = failure instanceof Error ? failure.message : String(failure)
    error.value = taskChannelError.value
  } finally { busy.value = false }
}

async function stopRemoteTaskChannel() {
  if (busy.value || !isRemote.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    if (mediaChannel.value === 'running') {
      await nativeDeviceApi.remoteMediaChannel('stop')
      mediaChannel.value = 'stopped'
      mediaCaptureActive.value = null
    }
    const result = await nativeDeviceApi.remoteTaskChannel('stop')
    taskChannel.value = result.state
    taskChannelError.value = ''
    notice.value = result.stopped ? '远端桌面任务通道已停止，未完成任务保留为未知状态。' : '远端桌面任务通道当前未运行。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function startRemoteMediaChannel() {
  if (busy.value || connection.value !== 'connected' || taskChannel.value !== 'running' || !isRemote.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await nativeDeviceApi.remoteMediaChannel('start')
    mediaChannel.value = result.state
    mediaChannelError.value = result.error || ''
    mediaCaptureActive.value = result.capture_active ?? null
    notice.value = '远端语音媒体通道已建立；主机只在核心要求采集时开启本机默认麦克风。'
  } catch (failure) {
    mediaChannel.value = 'error'
    mediaChannelError.value = failure instanceof Error ? failure.message : String(failure)
    error.value = mediaChannelError.value
  } finally { busy.value = false }
}

async function stopRemoteMediaChannel() {
  if (busy.value || !isRemote.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await nativeDeviceApi.remoteMediaChannel('stop')
    mediaChannel.value = result.state
    mediaCaptureActive.value = null
    mediaChannelError.value = ''
    notice.value = result.stopped ? '远端语音媒体通道已停止并释放本机采集设备。' : '远端语音媒体通道当前未运行。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function refreshLocalDevices() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    localDeviceSnapshot.value = await productApi.deviceSnapshot()
    notice.value = `本机核心设备状态已刷新；当前语音设备：${localDeviceSnapshot.value.active_device_id || '未授予'}。`
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function switchToLocalDevice() {
  const snapshot = localDeviceSnapshot.value
  const localDevice = snapshot?.devices.find(item => item.device_id === 'local' && item.connected)
  if (busy.value || !snapshot || !localDevice || snapshot.active_device_id === 'local') return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await productApi.selectInteractionDevice('local', snapshot.interaction_generation)
    localDeviceSnapshot.value = await productApi.deviceSnapshot()
    notice.value = result.result === 'active'
      ? '已按本机明确操作切回主机语音设备；原后台任务未迁移。'
      : '本机切换正在等待设备就绪；请刷新状态确认结果。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function checkGateway() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  remoteDevices.value = null
  remoteCheckedAt.value = ''
  try {
    const health = await nativeDeviceApi.requestSshGateway('/v1/status', { protocol_version: 1 })
    if (health.status < 200 || health.status >= 300) throw new Error(`主机网关返回 HTTP ${health.status}。`)
    const snapshot = await nativeDeviceApi.remoteDeviceSnapshot()
    remoteDevices.value = snapshot
    remoteCheckedAt.value = new Date().toLocaleTimeString()
    notice.value = snapshot.ready
      ? `主机核心与设备快照已就绪，共登记 ${snapshot.devices.length} 台设备。`
      : 'SSH 隧道已连接，主机语音核心仍未就绪。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function checkDesktopHelper() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await nativeDeviceApi.startWorker()
    const result = await nativeDeviceApi.desktopHelperHealth()
    helperHealth.value = result.state
    if (result.state === 'ready' && result.ok === true) {
      const health = result.data?.health
      const details = health && typeof health === 'object'
        ? (health as Record<string, unknown>).result as Record<string, unknown> | undefined
        : undefined
      notice.value = typeof details?.session_id === 'number'
        ? `桌面辅助进程已就绪，Windows 交互会话编号 ${details.session_id}。`
        : '桌面辅助进程健康检查通过。'
    } else {
      const details = typeof result.data?.error === 'string' ? `：${result.data.error}` : ''
      error.value = `桌面辅助进程不可用${details}`
    }
  } catch (failure) {
    helperHealth.value = 'unavailable'
    error.value = failure instanceof Error ? failure.message : String(failure)
  } finally { busy.value = false }
}

async function disconnectHost() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await nativeDeviceApi.disconnectSshHost()
    connection.value = 'disconnected'
    taskChannel.value = 'stopped'
    taskChannelError.value = ''
    mediaChannel.value = 'stopped'
    mediaChannelError.value = ''
    mediaCaptureActive.value = null
    remoteDevices.value = null
    remoteCheckedAt.value = ''
    notice.value = 'SSH 会话已关闭。'
    await refresh()
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

onMounted(() => { void refresh() })
onUnmounted(stopOpenSshJobWatch)
</script>

<template>
  <section class="device-settings">
    <header class="device-heading">
      <div><p class="eyebrow">DEVICE RUNTIME · 设备运行</p><h2>设备与角色</h2><p>设置本机职责、主机信任和远端连接。断线不会切换语音占用或迁移任务。</p></div>
      <span class="role-state" :data-state="status?.runtime.config_state || 'loading'">{{ status?.runtime.config_state === 'valid' ? '配置有效' : status?.runtime.config_state === 'invalid' ? '配置需处理' : localCoreAvailable ? '单机默认' : '待配置主机' }}</span>
    </header>

    <p v-if="error" class="device-message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="device-message" role="status">{{ notice }}</p>
    <p v-if="status?.runtime.error" class="device-message warning">{{ status.runtime.error }} 原文件已保留，未自动覆盖。</p>
    <p v-if="!localCoreAvailable" class="device-message warning">当前工作区未配置本机核心运行组件。轻量客户端请选择“远端客户端”并连接已有共享主机；独立运行和共享主机需要具备核心组件的完整工作区。</p>
    <p v-if="status?.device_runtime" class="device-message" role="status">
      实际角色：{{ status.device_runtime.actual_role === 'standalone' ? '独立运行' : status.device_runtime.actual_role === 'shared_host' ? '共享主机' : status.device_runtime.actual_role === 'remote_client' ? '远端客户端' : '尚未就绪' }}；{{ status.device_runtime.actual_role === 'remote_client' ? '固定主机 SSH 连接已认证' : status.device_runtime.actual_role === 'shared_host' || role === 'shared_host' ? `共享网关：${!status.device_runtime.device_gateway.enabled ? '未启用' : status.device_runtime.device_gateway.ready ? '已就绪' : status.device_runtime.device_gateway.error || '尚未就绪'}` : status.device_runtime.core_ready ? '当前核心留在本机' : '本机核心未启动' }}。
    </p>

    <fieldset :disabled="loading || busy || openSshBusy || openSshJobPending">
      <label>运行角色
        <select v-model="role">
          <option value="standalone" :disabled="!localCoreAvailable">独立运行</option>
          <option value="shared_host" :disabled="!localCoreAvailable">共享主机</option>
          <option value="remote_client">远端客户端</option>
        </select>
      </label>
      <label>设备编号
        <input v-model="deviceId" autocomplete="off" spellcheck="false" maxlength="128" />
      </label>
      <label>音频传输
        <select v-model="audioMode">
          <option value="pcm">PCM 原始档（默认）</option>
          <option value="opus_experimental">Opus 实验档</option>
        </select>
      </label>
    </fieldset>
    <p v-if="audioMode === 'opus_experimental'" class="device-message warning">Opus 双向压缩档仍处于实验阶段；连接会按签名协商的上下行编码运行。音质和识别结果对照尚未完成，若组件或主机不支持会明确报错，不会自动改用 PCM。</p>

    <section v-if="isRemote" class="device-card">
      <h3>本机 SSH 身份</h3>
      <p>生成的私钥仅保存到当前 Windows 用户的本机凭据库；界面只显示公钥。</p>
      <div class="button-row">
        <button class="outline-button" :disabled="busy || !deviceId.trim()" @click="createIdentity">生成本机身份</button>
        <button v-if="credentialRef" class="quiet-button" :disabled="busy" @click="deleteIdentity">删除本机私钥</button>
      </div>
      <label v-if="identity">本机设备公钥（配对申请会自动提交其签名）
        <textarea :value="identity.public_key" rows="3" readonly spellcheck="false" />
      </label>
      <p v-if="identity" class="fingerprint">本设备公钥指纹：<code>{{ identity.fingerprint }}</code></p>
      <p v-if="credentialRef" class="small-text">本机凭据引用：<code>{{ credentialRef }}</code></p>

      <h3>独立 SSH 登录密钥</h3>
      <p>此密钥只用于登录主机 OpenSSH 账户，与上方产品设备身份分开。私钥文件由原生层直接读取并写入当前 Windows 用户凭据库；不会传入页面，也不会上传到主机。</p>
      <label>本机 OpenSSH Ed25519 私钥文件路径
        <input v-model="transportKeyPath" autocomplete="off" spellcheck="false" placeholder="例如 C:\\Users\\用户名\\.ssh\\id_ed25519" />
      </label>
      <div class="button-row">
        <button class="outline-button" :disabled="busy || !transportKeyPath.trim()" @click="importTransportIdentity">在本机导入登录密钥</button>
        <button v-if="transportCredentialRef" class="quiet-button" :disabled="busy || status?.runtime.config_state !== 'valid'" @click="deleteTransportIdentity">删除登录密钥</button>
      </div>
      <label v-if="transportIdentity">需在主机 SSH 账户单独准入的公钥
        <textarea :value="transportIdentity.public_key" rows="3" readonly spellcheck="false" />
      </label>
      <p v-if="transportIdentity" class="fingerprint">登录密钥指纹：<code>{{ transportIdentity.fingerprint }}</code></p>
      <p v-if="transportCredentialRef && !transportIdentity" class="small-text">配置引用了登录密钥，但本机凭据库中找不到该密钥；请重新导入或清除引用。</p>
      <p v-if="transportCredentialRef" class="small-text">登录密钥凭据引用：<code>{{ transportCredentialRef }}</code></p>
      <p v-else class="small-text">未单独设置时，旧配置会尝试用产品设备身份登录 OpenSSH；主机账户仍须预先准入相应公钥。</p>

      <h3>向主机提交配对申请</h3>
      <p>申请使用本机设备私钥签名，并经已固定指纹的 SSH 隧道发送。主机未明确批准前，本设备不会获得信任。</p>
      <div class="device-grid">
        <label>主机本机显示的 8 位配对码
          <input v-model="pairingCodeInput" autocomplete="off" inputmode="numeric" maxlength="8" pattern="[0-9]{8}" placeholder="8 位数字" />
        </label>
        <div class="button-row pairing-submit-row">
          <button class="primary-button" :disabled="busy || connection !== 'connected' || !identity || !/^[0-9]{8}$/.test(pairingCodeInput)" @click="submitPairingRequest">提交签名配对申请</button>
        </div>
      </div>
      <article v-if="pairingSubmission" class="remote-device-row" aria-live="polite">
        <div class="remote-device-title"><strong>等待主机批准</strong><code>{{ pairingSubmission.device_id }}</code></div>
        <p class="fingerprint">申请指纹：<code>{{ pairingSubmission.fingerprint }}</code></p>
        <small class="device-generation">申请编号 {{ pairingSubmission.request_id }}</small>
      </article>

      <h3>共享主机信任</h3>
      <div class="device-grid">
        <label>主机地址
          <input v-model="address" autocomplete="off" spellcheck="false" placeholder="例如 192.168.1.20" />
        </label>
        <label>SSH 端口
          <input v-model.number="port" type="number" min="1" max="65535" />
        </label>
      </div>
      <div class="button-row"><button class="outline-button" :disabled="busy || !address.trim()" @click="probeHost">读取主机指纹候选</button></div>
      <div v-if="candidateFingerprint" class="candidate-box">
        <span>候选指纹（尚未信任）</span><code>{{ candidateFingerprint }}</code>
        <button class="quiet-button" :disabled="busy" @click="acceptFingerprint">我已在主机本机核对，填入草稿</button>
      </div>
      <label>已确认的主机 SHA-256 指纹
        <input v-model="hostKey" autocomplete="off" spellcheck="false" placeholder="SHA256:…" />
      </label>
      <label>SSH 用户名
        <input v-model="username" autocomplete="off" spellcheck="false" placeholder="共享主机上的受管账户" />
      </label>
      <p class="small-text">SSH 客户端仅连接主机的 <code>127.0.0.1:18765</code> 受限业务网关；状态、设备、任务与媒体请求需经设备身份和能力校验，PCM 音频流仍处于开发验收阶段。</p>
      <section v-if="remoteDevices" class="remote-device-list" aria-live="polite">
        <div class="remote-list-heading"><h3>共享主机登记设备</h3><span>检查于 {{ remoteCheckedAt }}</span></div>
        <p v-if="!remoteDevices.ready" class="device-message warning">主机核心尚未完成启动。</p>
        <p v-else-if="remoteDevices.devices.length === 0" class="small-text">主机当前没有登记设备。</p>
        <article v-for="device in remoteDevices.devices" :key="device.device_id" class="remote-device-row">
          <div class="remote-device-title">
            <strong>{{ device.name }}</strong><code>{{ device.device_id }}</code>
            <span class="connection-state" :data-state="device.connected ? 'connected' : 'disconnected'">{{ device.connected ? '在线' : '离线' }}</span>
            <span v-if="device.interaction_active" class="interaction-state">当前语音设备</span>
          </div>
          <div class="remote-device-capabilities">
            <span v-for="capability in device.capabilities" :key="capability">{{ remoteDevices.capability_labels[capability] || capability }}</span>
            <span v-if="device.capabilities.length === 0">暂无可用能力</span>
          </div>
          <small class="device-generation">连接代次 {{ device.connection_generation }} · 执行器代次 {{ device.executor_generation }}</small>
          <p v-if="device.error" class="device-message warning">{{ device.error }}</p>
        </article>
      </section>
    </section>

    <section v-if="isSavedSharedHost && localDeviceSnapshot" class="device-card local-device-list" aria-live="polite">
      <h3>本机核心语音设备</h3>
      <p>当前语音设备：{{ localDeviceSnapshot.active_device_id || '未授予' }}；交互代次 {{ localDeviceSnapshot.interaction_generation }}。切换会先停止旧输入并等待目标就绪；后台任务不会迁移。</p>
      <div class="button-row">
        <button class="outline-button" :disabled="busy || loading || localDeviceSnapshot.active_device_id === 'local' || !localDeviceSnapshot.devices.some(item => item.device_id === 'local' && item.connected)" @click="switchToLocalDevice">明确切回主机本机语音</button>
        <button class="quiet-button" :disabled="busy || loading" @click="refreshLocalDevices">刷新本机语音设备</button>
      </div>
    </section>

    <section v-if="role === 'shared_host'" class="device-card pairing-card">
      <h3>主机设备配对</h3>
      <p v-if="!isSavedSharedHost" class="device-message warning">先保存“共享主机”角色，再管理配对窗口。更改期望角色不会自动启动或停止核心服务。</p>
      <p v-else-if="!canManagePairing" class="device-message warning">共享主机核心与网关尚未就绪，配对管理暂不可用。请先在具备核心组件的完整工作区启动共享主机。</p>
      <template v-else>
        <p>配对码仅在主机本机显示，窗口最长开放 10 分钟。新设备提交签名申请后仍需在此明确批准。</p>
        <div class="button-row">
          <label class="pairing-duration">窗口时长（秒）
            <input v-model.number="pairingTtlSeconds" type="number" min="30" max="600" step="30" />
          </label>
          <button class="primary-button" :disabled="busy || loading" @click="openPairingWindow">开启配对窗口</button>
          <button v-if="pairing?.window_open" class="outline-button" :disabled="busy" @click="closePairingWindow">关闭配对窗口</button>
          <button class="quiet-button" :disabled="busy || loading" @click="refreshPairing">刷新配对状态</button>
          <span class="connection-state" :data-state="pairing?.window_open ? 'connected' : 'disconnected'">{{ pairing?.window_open ? '配对窗口开放' : '配对窗口关闭' }}</span>
          <span v-if="pairing?.window_open" class="small-text">错误配对码剩余 {{ pairing.failed_attempts_remaining }} 次后自动关闭窗口</span>
        </div>
        <div v-if="pairingCode" class="pairing-code-box" role="status">
          <span>本次配对码 · 到期 {{ pairing?.window_expires_at_ms ? new Date(pairing.window_expires_at_ms).toLocaleTimeString() : '时间未知' }}</span>
          <output>{{ pairingCode }}</output>
          <small>离开本页或刷新后不会重新显示；需要时请重新开启配对窗口。</small>
        </div>
        <p v-if="pairing?.trust.state === 'invalid'" class="device-message warning">
          信任登记无法识别，原文件已保留；配对审批和撤销操作均被拒绝。{{ pairing.trust.error || '' }}
        </p>
        <section class="remote-device-list" aria-live="polite">
          <div class="remote-list-heading"><h3>待批准请求</h3><span>{{ pairing?.pending_requests.length || 0 }} 项</span></div>
          <p v-if="pairing && pairing.pending_requests.length === 0" class="small-text">当前没有待批准设备。</p>
          <article v-for="request in pairing?.pending_requests || []" :key="request.request_id" class="remote-device-row">
            <div class="remote-device-title"><strong>{{ request.device_id }}</strong><span class="connection-state">待批准</span></div>
            <p class="fingerprint">公钥指纹：<code>{{ request.fingerprint }}</code></p>
            <small class="device-generation">请求到期 {{ new Date(request.expires_at_ms).toLocaleTimeString() }}</small>
            <div class="button-row">
              <button class="primary-button" :disabled="busy || pairing?.trust.state === 'invalid'" @click="approvePairing(request.request_id, request.device_id, request.fingerprint)">明确批准</button>
              <button class="quiet-button" :disabled="busy" @click="rejectPairing(request.request_id, request.device_id)">拒绝</button>
            </div>
          </article>
        </section>
        <section class="remote-device-list" aria-live="polite">
          <div class="remote-list-heading"><h3>设备信任登记</h3><span>{{ pairing?.trust.devices.length || 0 }} 项</span></div>
          <p v-if="pairing && pairing.trust.devices.length === 0" class="small-text">尚无已批准设备。</p>
          <article v-for="device in pairing?.trust.devices || []" :key="device.device_id" class="remote-device-row">
            <div class="remote-device-title">
              <strong>{{ device.device_id }}</strong>
              <span class="connection-state" :data-state="device.state === 'trusted' ? 'connected' : 'disconnected'">{{ device.state === 'trusted' ? '可信' : '已撤销' }}</span>
            </div>
            <p class="fingerprint">公钥指纹：<code>{{ device.fingerprint }}</code></p>
            <button v-if="device.state === 'trusted'" class="quiet-button" :disabled="busy" @click="revokeDevice(device.device_id, device.fingerprint)">撤销信任</button>
          </article>
        </section>
      </template>
    </section>

    <section v-else-if="!isRemote" class="device-card">
      <h3>独立运行</h3>
      <p>本机独立运行不需要 SSH 服务或远端设备。更改期望角色不会自动启动服务或接管语音。</p>
    </section>

    <section v-if="role === 'shared_host'" class="device-card system-feature-card" aria-live="polite">
      <h3>共享主机 OpenSSH Server</h3>
      <p v-if="!localCoreAvailable" class="device-message warning">轻量客户端无需安装 SSH 入站服务；请改为“远端客户端”角色，并使用主机已有的 SSH 服务连接。</p>
      <p v-else-if="!isSavedSharedHost" class="device-message warning">先保存“共享主机”角色，再查询或管理 Windows 系统功能。当前草稿不会触发状态查询、安装或服务启动。</p>
      <template v-else>
        <p>共享主机需要接收客户端主动发起的 SSH 连接。此处只查询系统功能与 sshd 状态；停止 Voice Terminal 共享不会关闭用户的 SSH 服务。</p>
        <div v-if="openSshStatus" class="system-feature-status">
          <div><span>Windows 可选功能</span><strong>{{ openSshStatus.capability_state === 'Unknown' ? '未知（只读查询未提权）' : openSshStatus.capability_state }}</strong></div>
          <div><span>sshd 服务</span><strong>{{ openSshStatus.service_present ? (openSshStatus.service_state || '状态未知') : '未安装' }}</strong></div>
          <div><span>当前启动类型</span><strong>{{ openSshStatus.startup_type || '未报告' }}</strong></div>
          <div><span>共享主机 SSH 状态</span><strong>{{ openSshStatus.ready ? '已就绪' : '未就绪' }}</strong></div>
        </div>
        <p v-else-if="openSshStatusLoading" class="small-text">正在读取 OpenSSH Server 状态……</p>
        <p v-else class="device-message warning">尚未取得 OpenSSH Server 状态。</p>
        <p class="device-message warning">安装 OpenSSH Server 时，Windows 会自动创建并启用 <code>OpenSSH-Server-In-TCP</code> 入站防火墙规则（TCP 端口 22）。这会允许网络上的 SSH 入站连接。程序不会另行修改防火墙规则、sshd 启动类型、SSH 配置或账户公钥；如不接受，请取消下一步确认或 Windows 管理员授权。</p>
        <p v-if="openSshStatusError" class="device-message error" role="alert">{{ openSshStatusError }}</p>
        <article v-if="openSshJob" class="remote-device-row" aria-live="polite">
          <div class="remote-device-title">
            <strong>{{ openSshJobPending ? '系统功能任务进行中' : openSshJob.state === 'complete' ? '系统功能任务完成' : openSshJob.state === 'failed' ? '系统功能任务失败' : `系统功能任务：${openSshJob.state}` }}</strong>
            <code>{{ openSshJob.job_id }}</code>
          </div>
          <p>{{ openSshJob.phase }}</p>
          <p v-if="openSshJob.error" class="device-message warning">{{ openSshJob.error }}</p>
        </article>
        <div class="button-row">
          <button class="outline-button" :disabled="busy || loading || openSshBusy || openSshJobPending || openSshStatusLoading" @click="refreshOpenSshStatus">刷新系统状态</button>
          <button class="primary-button" :disabled="!openSshStatus || openSshStatus.ready || openSshCapabilityPending || busy || loading || openSshBusy || openSshJobPending" @click="enableOpenSshServer">
            {{ openSshStatus?.capability_state === 'Installed' ? '启动 sshd 服务' : openSshStatus?.capability_state === 'Unknown' ? '安装或启动 OpenSSH Server' : '安装并启动 OpenSSH Server' }}
          </button>
        </div>
        <p v-if="openSshCapabilityPending" class="device-message warning">Windows 正在处理该可选功能；刷新系统状态确认完成前，不允许再次启动管理任务。</p>
      </template>
    </section>

    <section v-if="isRemote" class="device-card task-channel-card">
      <h3>远端桌面任务通道</h3>
      <p>只有 SSH 已连接且主机已批准本机设备身份后才能启动。启动前会检查受管桌面辅助进程；DSH（代理执行宿主）仍需对每次动作单独审批，断线不会把任务改投本机。</p>
      <div class="button-row">
        <span class="connection-state" :data-state="taskChannel">{{ taskChannel === 'running' ? '任务通道运行中' : taskChannel === 'error' ? '任务通道错误' : '任务通道未启动' }}</span>
        <button v-if="taskChannel !== 'running'" class="primary-button" :disabled="busy || loading || connection !== 'connected'" @click="startRemoteTaskChannel">启动远端桌面通道</button>
        <button v-else class="outline-button" :disabled="busy" @click="stopRemoteTaskChannel">停止远端桌面通道</button>
      </div>
      <p v-if="taskChannelError" class="device-message warning">{{ taskChannelError }}</p>
    </section>

    <section v-if="isRemote" class="device-card media-channel-card">
      <h3>远端语音设备</h3>
      <p>连接由用户显式启动。主机只有在核心监听时才会请求麦克风帧；媒体断线不会自动切回主机或重放旧音频。</p>
      <div class="button-row">
        <span class="connection-state" :data-state="mediaChannel">{{ mediaChannel === 'running' ? '语音媒体通道运行中' : mediaChannel === 'error' ? '语音媒体通道错误' : '语音媒体通道未启动' }}</span>
        <span v-if="mediaChannel === 'running'" class="interaction-state">{{ mediaCaptureActive ? '主机已请求麦克风采集' : '当前未采集麦克风' }}</span>
        <button v-if="mediaChannel !== 'running'" class="primary-button" :disabled="busy || loading || connection !== 'connected' || taskChannel !== 'running'" @click="startRemoteMediaChannel">连接并启用远端语音设备</button>
        <button v-else class="outline-button" :disabled="busy" @click="stopRemoteMediaChannel">停止远端语音设备</button>
      </div>
      <p v-if="mediaChannelError" class="device-message warning">{{ mediaChannelError }}</p>
    </section>

    <section class="device-card helper-card">
      <h3>本机桌面辅助进程</h3>
      <p>仅检查已安装 DSH 功能包中的常驻辅助进程，不会截图、录音、播放或操作窗口。</p>
      <div class="button-row">
        <span class="connection-state" :data-state="helperHealth">{{ helperHealth === 'ready' ? '辅助进程就绪' : helperHealth === 'unknown' ? '尚未检查' : helperHealth === 'stopped' ? '设备进程未启动' : helperHealth === 'unsupported' ? '当前系统不支持' : '辅助能力不可用' }}</span>
        <button class="outline-button" :disabled="busy || loading" @click="checkDesktopHelper">检查桌面辅助进程</button>
      </div>
    </section>

    <footer class="device-footer">
      <div class="connection-state" :data-state="connection">SSH：{{ connection === 'connected' ? '已连接' : connection === 'unavailable' ? '当前构建不支持' : '已断开' }}</div>
      <div class="button-row">
        <button v-if="status?.runtime.config_state !== 'invalid'" class="outline-button" :disabled="!canSave || busy" @click="saveConfig()">保存设备配置</button>
        <button v-else class="outline-button" :disabled="!canSave || busy" @click="saveConfig(true)">归档原件并修复</button>
        <button v-if="isRemote && connection !== 'connected'" class="primary-button" :disabled="busy || !status || role !== 'remote_client' || status.runtime.config_state === 'invalid'" @click="connectHost">连接主机</button>
        <button v-if="isRemote && connection === 'connected'" class="outline-button" :disabled="busy" @click="checkGateway">刷新主机状态与设备</button>
        <button v-if="connection === 'connected'" class="quiet-button" :disabled="busy" @click="disconnectHost">断开</button>
        <button class="quiet-button" :disabled="loading || busy" @click="refresh">刷新</button>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.system-feature-status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:14px 0}.system-feature-status div{display:grid;gap:6px;padding:12px;border:1px solid #344157;border-radius:9px;background:rgba(17,26,41,.6)}.system-feature-status span{font-size:11px;color:#9aa9bf}.system-feature-status strong{font-size:13px;color:#dce5f4;overflow-wrap:anywhere}@media(max-width:700px){.system-feature-status{grid-template-columns:repeat(2,minmax(0,1fr))}}
.device-settings{padding:24px;max-width:900px;margin:0 auto;color:var(--text,#e5eaf3)}.device-heading{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:20px}.eyebrow{font-size:11px;letter-spacing:.14em;color:#8c9bb4}.device-heading h2{margin:5px 0 8px;font-size:24px}.device-heading p{max-width:640px;color:#aab4c5;line-height:1.65}.role-state,.connection-state{border:1px solid #344157;border-radius:999px;padding:7px 12px;color:#aebad0;font-size:12px;white-space:nowrap}.role-state[data-state=valid],.connection-state[data-state=connected]{color:#8ee0c0;border-color:#286c5a}.role-state[data-state=invalid]{color:#ffbe8b;border-color:#825333}.device-settings fieldset,.device-card{border:1px solid #273348;border-radius:14px;padding:18px;margin:14px 0;background:rgba(18,25,38,.54)}fieldset{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.device-settings label{display:flex;flex-direction:column;gap:7px;font-size:13px;color:#b8c4d6;margin:12px 0}.device-settings input,.device-settings select,.device-settings textarea{width:100%;border:1px solid #35435a;border-radius:8px;background:#111a29;color:#e8edf6;padding:10px 11px;min-width:0}.device-settings textarea{resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:12px}.device-card h3{margin:4px 0 8px;font-size:16px}.device-card p{color:#aab4c5;line-height:1.65}.device-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(120px,1fr);gap:14px}.button-row{display:flex;flex-wrap:wrap;gap:9px;margin:12px 0}.device-settings button{border-radius:8px;padding:9px 13px;cursor:pointer}.device-settings button:disabled{opacity:.48;cursor:not-allowed}.primary-button{background:#6686df;color:white;border:1px solid #7b9aeb}.outline-button,.quiet-button{background:transparent;color:#cdd7e8;border:1px solid #3a4962}.quiet-button{color:#aab4c5}.candidate-box{display:grid;gap:8px;padding:12px;border:1px solid #785f35;border-radius:10px;background:#211e18;color:#e1c78e}.candidate-box code,.fingerprint code,.small-text code,.remote-device-title code{overflow-wrap:anywhere}.small-text{font-size:12px}.fingerprint{font-size:13px;color:#d9e4f5}.device-footer{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding-top:12px}.device-footer .button-row{margin:0}.device-message{padding:11px 14px;border-radius:9px;background:#1c2b3e;color:#c7d7f0}.device-message.error{background:#422727;color:#ffc5b8}.device-message.warning{background:#392f1d;color:#efce8b}.remote-device-list{margin-top:20px;border-top:1px solid #273348;padding-top:16px}.remote-list-heading,.remote-device-title{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.remote-list-heading{justify-content:space-between}.remote-list-heading h3{margin:0}.remote-list-heading span,.device-generation{font-size:12px;color:#9aa9bf}.remote-device-row{padding:13px 0;border-bottom:1px solid #273348}.remote-device-title strong{font-size:14px}.remote-device-title code{font-size:12px;color:#aab4c5}.remote-device-title .connection-state{margin-left:auto}.interaction-state,.remote-device-capabilities span{border:1px solid #355a54;border-radius:999px;padding:4px 9px;color:#9bdec6;font-size:11px}.remote-device-capabilities{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.remote-device-capabilities span{border-color:#344157;color:#c4d0e2}.device-generation{display:block}.pairing-code-box{display:grid;gap:8px;border:1px solid #785f35;border-radius:10px;padding:14px;background:#211e18;color:#e1c78e}.pairing-code-box output{font:700 28px ui-monospace,Consolas,monospace;letter-spacing:.18em;color:#fff}.pairing-code-box small{color:#cdbb97}.pairing-duration{max-width:180px}.pairing-duration input{max-width:140px}@media(max-width:700px){.device-settings{padding:16px}fieldset{grid-template-columns:1fr}.device-heading{display:block}.device-grid{grid-template-columns:1fr}.device-footer{align-items:flex-start;flex-direction:column}}
</style>
