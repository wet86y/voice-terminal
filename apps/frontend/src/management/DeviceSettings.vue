<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { matchesRemoteManagementEnvelope, nativeDeviceApi, parseRemoteSshSystemFeatureSnapshot, type DeviceConfig, type DeviceConfigStatus, type DiscoveredDevice, type ManagedResourceJob, type OpenSshServerStatus, type RemoteSshSystemFeatureSnapshot } from '../product/native-device-api'
import { ensureRemoteClientRuntime } from '../product/remote-client-runtime'

const status = ref<DeviceConfigStatus | null>(null)
const mode = ref<DeviceConfig['mode']>('local')
const broadcastEnabled = ref(false)
const clientOnlineTtsLocal = ref(true)
const audioCompression = ref(false)
const deviceId = ref('')
const pairingCode = ref('')
const pairingCodeInput = ref('')
const pairingExpiresAt = ref(0)
const discoveredDevices = ref<DiscoveredDevice[]>([])
const selectedHostId = ref('')
const searchingDevices = ref(false)
const connection = ref<'connected' | 'disconnected' | 'unavailable'>('disconnected')
const taskChannel = ref<'running' | 'stopped' | 'error'>('stopped')
const taskChannelError = ref('')
const mediaChannel = ref<'running' | 'stopped' | 'error'>('stopped')
const mediaChannelError = ref('')
const mediaCaptureActive = ref<boolean | null>(null)
const remoteCoreState = ref<'ready' | 'starting' | 'unavailable' | 'unknown'>('unknown')
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
const openSshLongWaitNotice = ref(false) // openSshLongWaitNotice（本机 SSH 准备任务超过前台等待时间后仍在后台跟踪）。
const broadcastPreflightBusy = ref(false) // broadcastPreflightBusy（开启本机广播前检查 SSH 并等待准备任务）。
const openSshJobTimer = ref<ReturnType<typeof setInterval> | null>(null)
const remoteOpenSshSnapshot = ref<RemoteSshSystemFeatureSnapshot | null>(null) // remoteOpenSshSnapshot（配对服务端只读 SSH 快照）。
const remoteOpenSshHostId = ref<string | null>(null) // remoteOpenSshHostId（当前快照绑定的服务端编号）。
const remoteOpenSshError = ref('') // remoteOpenSshError（服务端 SSH 检查或管理操作失败原因）。
const remoteOpenSshBusy = ref(false) // remoteOpenSshBusy（服务端 SSH 远程操作进行中）。
const remoteOpenSshPollTimer = ref<ReturnType<typeof setTimeout> | null>(null) // remoteOpenSshPollTimer（服务端受管任务状态补查）。
let remoteOpenSshGeneration = 0 // remoteOpenSshGeneration（配对目标或服务端状态操作代次）。
const pairingExpiryTimer = ref<ReturnType<typeof setTimeout> | null>(null)
const pairingStatusTimer = ref<ReturnType<typeof setInterval> | null>(null)
const reconnectTimer = ref<ReturnType<typeof setTimeout> | null>(null)
const reconnectAttempt = ref(0)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const localCoreAvailable = computed(() => status.value?.local_core_available !== false)
const openSshJobPending = computed(() => !!openSshJob.value && ['queued', 'running', 'cancelling'].includes(openSshJob.value.state))
const remoteOpenSshJobPending = computed(() => !!remoteOpenSshSnapshot.value?.job
  && ['queued', 'running', 'cancelling'].includes(remoteOpenSshSnapshot.value.job.state))
const isRemote = computed(() => mode.value === 'remote')
const remoteOpenSshCanEnable = computed(() => {
  const hostId = status.value?.device_config?.paired_host?.host_id
  return isRemote.value && !!hostId && remoteOpenSshHostId.value === hostId
    && connection.value === 'connected' && !remoteOpenSshBusy.value
    && !remoteOpenSshJobPending.value && remoteOpenSshSnapshot.value?.status.ready === false
})
const isBroadcasting = computed(() => mode.value === 'local' && broadcastEnabled.value)
const canSave = computed(() => !loading.value && !busy.value && !openSshBusy.value && !broadcastPreflightBusy.value && !openSshJobPending.value
  && !!status.value && (mode.value === 'remote' || localCoreAvailable.value))

/** localOpenSshJobStateLabel（本机 SSH 任务状态中文说明）：长任务仍在后台运行时明确呈现状态。 */
function localOpenSshJobStateLabel(state: string) {
  return ({ queued: '等待开始', running: '进行中', cancelling: '正在取消', complete: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[state] ?? state
}

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
    } else if (next.runtime.config_state === 'missing') {
      if (!deviceId.value) deviceId.value = newDeviceId()
      mode.value = next.local_core_available === false ? 'remote' : 'local'
      broadcastEnabled.value = false
      clientOnlineTtsLocal.value = true
      audioCompression.value = false
    }
    if (mode.value === 'local') {
      clearRemoteOpenSshSnapshot()
      await Promise.all([refreshOpenSshStatus(), refreshOpenSshJob()])
      connection.value = 'disconnected'
      taskChannel.value = 'stopped'
      mediaChannel.value = 'stopped'
      mediaCaptureActive.value = null
    } else {
      const ssh = await nativeDeviceApi.sshHostStatus()
      connection.value = ssh.connected ? 'disconnected' : ssh.state
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
          taskChannel.value = tasks.state
          taskChannelError.value = tasks.error || ''
          mediaChannel.value = media.state
          mediaChannelError.value = media.error || ''
          mediaCaptureActive.value = media.capture_active ?? null
        } else {
          clearRemoteOpenSshSnapshot()
          taskChannel.value = 'stopped'
          mediaChannel.value = 'stopped'
          mediaChannelError.value = ''
          mediaCaptureActive.value = null
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
    }
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    loading.value = false
  }
}

async function refreshOpenSshStatus() {
  if (mode.value !== 'local') {
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

function stopPairingStatusWatch() {
  if (pairingStatusTimer.value !== null) clearInterval(pairingStatusTimer.value)
  pairingStatusTimer.value = null
}

async function refreshPairingWindowStatus() {
  try {
    const current = await nativeDeviceApi.pairingWindowStatus()
    if (current.open) return
    const hadVisibleCode = !!pairingCode.value
    pairingCode.value = ''
    pairingExpiresAt.value = 0
    if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
    pairingExpiryTimer.value = null
    stopPairingStatusWatch()
    if (hadVisibleCode) notice.value = '配对窗口已使用、关闭或到期，配对码已清除。'
  } catch { /* 状态读取失败时保留当前码，直到有效期计时器到期。 */ }
}

async function refreshOpenSshJob() {
  if (mode.value !== 'local') return
  try {
    openSshJob.value = (await nativeDeviceApi.resourceJobs())
      .find(item => item.job_id === 'system-feature-openssh-server') || null
    if (openSshJobPending.value) {
      if (openSshJobTimer.value === null) openSshJobTimer.value = setInterval(() => { void refreshOpenSshJob() }, 1500)
      return
    }
    stopOpenSshJobWatch()
    const job = openSshJob.value
    if (job?.state === 'complete' || job?.state === 'failed') await refreshOpenSshStatus()
    if (openSshLongWaitNotice.value) {
      openSshLongWaitNotice.value = false
      if (!job) {
        notice.value = ''
        openSshStatusError.value = '无法读取 Windows SSH 准备任务状态；请再次检查 SSH 服务后重试。'
      } else if (job.state === 'complete' && openSshStatus.value?.ready) {
        openSshStatusError.value = ''
        notice.value = `微软 OpenSSH Server 已就绪：${openSshStatus.value.source_version || '版本未报告'}，实际监听端口 ${openSshStatus.value.effective_port}。`
      } else if (job.state === 'failed') {
        notice.value = ''
        openSshStatusError.value = job.error || 'Windows SSH 服务准备任务失败；请检查任务状态后重试。'
      } else if (job.state === 'cancelled') {
        notice.value = ''
        openSshStatusError.value = job.phase || 'Windows SSH 服务准备已取消；可以再次检查并重试。'
      } else if (job.state === 'complete') {
        notice.value = ''
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
  if (!window.confirm(`配对服务端“${status.value?.device_config?.paired_host?.display_name || '已配对服务端'}”的 OpenSSH Server 尚未就绪。继续后会在服务端请求安装或启动微软 OpenSSH Server，并在服务端等待 Windows 管理员确认；客户端不会改动本机 SSH 服务。是否继续？`)) return

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
  let current = await nativeDeviceApi.openSshServerStatus()
  openSshStatus.value = current
  if (current.ready) return current
  const details = [
    `Windows 功能：${current.capability_state}`,
    `sshd 服务：${current.service_present ? current.service_state || '状态未知' : '不存在'}`,
    `微软程序：${current.source_path || '未找到'}（${current.source_version || '无版本信息'}；签名 ${current.signature_status || '未知'}）`,
    `有效端口：${current.effective_port ?? '未验证'}；实际监听：${current.listening_ports?.join(', ') || '无'}`,
  ].join('\n')
  if (!window.confirm(`检查发现本机 SSH 服务未就绪：\n${details}\n\n同意后，程序会通过 Windows 官方可选功能安装或启动微软 OpenSSH Server，并复核程序签名、配置和实际监听端口。系统可能显示管理员授权窗口。继续吗？`)) {
    throw new Error('你取消了 SSH 服务准备；本机配置未改动。')
  }
  if (openSshJobPending.value || openSshBusy.value) throw new Error('Windows SSH 功能任务正在进行，请完成后再检查。')
  openSshBusy.value = true
  try {
    const started = await nativeDeviceApi.enableOpenSshServer(true)
    const deadline = Date.now() + 5 * 60_000
    let completed = false
    while (Date.now() < deadline) {
      const job = (await nativeDeviceApi.resourceJobs()).find(item => item.job_id === started.job_id) || null
      openSshJob.value = job
      if (job?.state === 'failed' || job?.state === 'cancelled') throw new Error(job.error || 'Windows SSH 服务准备任务未完成。')
      if (job?.state === 'complete') { completed = true; break }
      await delay(1200)
    }
    if (!completed) {
      if (openSshJobPending.value) {
        openSshLongWaitNotice.value = true
        notice.value = ''
        throw new Error('Windows SSH 准备任务仍在进行；页面会继续自动跟踪结果，请等待任务完成后再重试。')
      }
      throw new Error('未能确认 Windows SSH 准备任务结果；请刷新状态后重试。')
    }
    current = await nativeDeviceApi.openSshServerStatus()
    openSshStatus.value = current
    if (!current.ready) throw new Error('系统任务已结束，但 OpenSSH 有效配置尚未确认；请检查服务配置、主机密钥权限和实际监听端口。')
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
  notice.value = ''
  try {
    const current = await nativeDeviceApi.openSshServerStatus()
    openSshStatus.value = current
    if (current.ready) {
      notice.value = `微软 OpenSSH Server 已就绪：${current.source_version || '版本未报告'}，实际监听端口 ${current.effective_port}。未重新安装或重启服务。`
      return
    }
    openSshBusy.value = false
    const ready = await ensureOpenSshServerReady()
    notice.value = `微软 OpenSSH Server 已就绪：${ready.source_version || '版本未报告'}，实际监听端口 ${ready.effective_port}。`
  } catch (failure) {
    openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    openSshBusy.value = false
  }
}

async function openPairingWindow() {
  if (busy.value || !isBroadcasting.value || !openSshStatus.value?.ready) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await nativeDeviceApi.openPairingWindow()
    pairingCode.value = result.pairing_code
    pairingExpiresAt.value = Date.now() + result.expires_in_seconds * 1000
    if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
    stopPairingStatusWatch()
    pairingStatusTimer.value = setInterval(() => { void refreshPairingWindowStatus() }, 1000)
    pairingExpiryTimer.value = setTimeout(() => {
      pairingCode.value = ''
      pairingExpiresAt.value = 0
      stopPairingStatusWatch()
      notice.value = '配对窗口已过期；如需再次配对，请重新获取配对码。'
    }, result.expires_in_seconds * 1000)
    try {
      await navigator.clipboard.writeText(result.pairing_code)
      notice.value = '配对窗口已开启，六位配对码已复制。'
    } catch {
      notice.value = '配对窗口已开启。剪贴板不可用，请手动抄录六位配对码。'
    }
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function copyPairingCode() {
  if (!pairingCode.value) return
  try {
    await navigator.clipboard.writeText(pairingCode.value)
    notice.value = '六位配对码已复制到剪贴板。'
  } catch { error.value = '剪贴板不可用，请手动抄录本页显示的配对码。' }
}

async function closePairingWindow() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await nativeDeviceApi.closePairingWindow()
    pairingCode.value = ''
    pairingExpiresAt.value = 0
    stopPairingStatusWatch()
    if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
    pairingExpiryTimer.value = null
    notice.value = '六位码和本机配对监听器已关闭。'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function saveConfig(preserveInvalid = false): Promise<boolean> {
  if (busy.value || !canSave.value) return false
  if (status.value?.runtime.config_state === 'invalid' && !preserveInvalid) return false
  const savedConfig = status.value?.device_config
  const nextAudioMode: DeviceConfig['audio_mode'] = audioCompression.value ? 'opus_experimental' : 'pcm'
  if (mediaChannel.value === 'running' && savedConfig && nextAudioMode !== savedConfig.audio_mode) {
    error.value = '更改音频传输模式前，请先停止当前远端语音媒体通道。'
    return false
  }
  if (preserveInvalid && !window.confirm('先将当前无法识别的设备配置原件归档，再保存这份新配置？')) return false
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const config: DeviceConfig = {
      schema_version: 3,
      device_id: deviceId.value.trim() || newDeviceId(),
      mode: mode.value,
      broadcast_enabled: mode.value === 'local' && broadcastEnabled.value,
      client_online_tts_local: clientOnlineTtsLocal.value,
      audio_compression: audioCompression.value,
      reconnect_suspended_reason: null,
      desired_role: mode.value === 'remote' ? 'remote_client' : broadcastEnabled.value ? 'shared_host' : 'standalone',
      audio_mode: nextAudioMode,
      paired_host: savedConfig?.paired_host ?? savedConfig?.host ?? null,
    }
    await nativeDeviceApi.saveDeviceConfig(config, preserveInvalid)
    notice.value = preserveInvalid ? '新配置已保存；无法识别的原件已归档并保留。' : '设备配置已保存并开始切换。'
    await refresh()
    return true
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure)
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
  error.value = ''
  notice.value = ''
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

    if (connection.value === 'connected' && wasTaskRunning && ttsChanged) {
      const tasks = await nativeDeviceApi.remoteTaskChannel('start')
      taskChannel.value = tasks.state
      taskChannelError.value = tasks.error || ''
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
    notice.value = '本设备连接设置已保存。'
  } catch (failure) {
    if (!configSaved) {
      clientOnlineTtsLocal.value = saved.client_online_tts_local
      audioCompression.value = saved.audio_compression
      if (wasTaskRunning && ttsChanged && connection.value === 'connected') {
        try {
          const tasks = await nativeDeviceApi.remoteTaskChannel('start')
          taskChannel.value = tasks.state
          taskChannelError.value = tasks.error || ''
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
    if (!configSaved) await refresh()
  }
}

async function onModeChanged() {
  const savedMode = status.value?.device_config?.mode
  if (!status.value || savedMode === mode.value) return
  if (!await saveConfig()) mode.value = savedMode || (localCoreAvailable.value ? 'local' : 'remote')
}

async function onBroadcastChanged() {
  const savedValue = status.value?.device_config?.broadcast_enabled ?? false
  if (broadcastEnabled.value && !savedValue) {
    if (broadcastPreflightBusy.value || openSshBusy.value || openSshJobPending.value) {
      broadcastEnabled.value = savedValue
      openSshStatusError.value = 'Windows SSH 功能任务正在进行，请完成后再开启广播。'
      return
    }
    broadcastPreflightBusy.value = true
    openSshStatusError.value = ''
    try {
      await ensureOpenSshServerReady()
    } catch (failure) {
      broadcastEnabled.value = savedValue
      openSshStatusError.value = failure instanceof Error ? failure.message : String(failure)
      return
    } finally {
      broadcastPreflightBusy.value = false
    }
    if (!window.confirm('开启后将创建本程序专用的局域网规则，只允许当前活动私有子网访问微软 SSH 服务和六位码配对端口。可能需要 Windows 管理员授权。继续吗？')) {
      broadcastEnabled.value = savedValue
      return
    }
  }
  if (!await saveConfig()) broadcastEnabled.value = savedValue
}

async function searchForDevices() {
  if (busy.value || searchingDevices.value || !isRemote.value) return
  searchingDevices.value = true; error.value = ''; notice.value = ''
  try {
    if (status.value?.device_config?.mode !== 'remote') {
      if (!await saveConfig()) throw new Error(error.value || '无法保存远程配对模式。')
    }
    const result = await nativeDeviceApi.searchDevices(3000)
    discoveredDevices.value = result.devices
    selectedHostId.value = result.devices.find(device => device.compatible && device.state !== 'unavailable')?.host_id || ''
    notice.value = result.devices.length === 0
      ? '没有发现可配对服务端。请确认服务端已开启本地广播。'
      : `搜索到 ${result.devices.length} 台服务端设备。`
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { searchingDevices.value = false }
}

async function activateRemoteChannels() {
  const state = await ensureRemoteClientRuntime()
  if (state.state === 'waiting') throw new Error('远程模式尚未完成配对，连接通道没有启动。')
  if (state.state === 'management_only') {
    taskChannel.value = 'stopped'
    mediaChannel.value = 'stopped'
    taskChannelError.value = ''
    mediaChannelError.value = ''
    mediaCaptureActive.value = null
    notice.value = state.core_state === 'starting'
      ? '服务端管理已连接，语音核心正在准备；组件管理仍可使用。'
      : '服务端管理已连接，语音核心暂不可用；组件管理仍可使用。'
    return
  }
  taskChannel.value = state.task.state
  taskChannelError.value = state.task.error || ''
  mediaChannel.value = state.media.state
  mediaChannelError.value = state.media.error || ''
  mediaCaptureActive.value = state.media.capture_active ?? null
}

async function pairWithSelectedDevice() {
  if (busy.value || !selectedHostId.value || !/^[0-9]{6}$/.test(pairingCodeInput.value)) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await nativeDeviceApi.pairWithDiscoveredDevice(selectedHostId.value, pairingCodeInput.value)
    pairingCodeInput.value = ''
    if (!result.connected) {
      status.value = await nativeDeviceApi.deviceConfigStatus()
      connection.value = 'disconnected'
      error.value = `配对和服务端身份保存成功，但 SSH 桥接连接失败：${result.connection_error || '原因未提供'}。可点击“连接服务端”重试，不必重新获取配对码。`
      return
    }
    connection.value = 'connected'
    reconnectAttempt.value = 0
    notice.value = `已连接到 ${result.display_name}。正在准备任务与语音通道。`
    await activateRemoteChannels()
    await refresh()
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

async function connectHost(automatic = false) {
  if (busy.value || !isRemote.value || !status.value?.device_config?.paired_host) return
  busy.value = true
  if (!automatic) { error.value = ''; notice.value = '' }
  try {
    const saved = status.value.device_config
    if (saved.reconnect_suspended_reason) {
      const resumed = { ...saved, reconnect_suspended_reason: null }
      await nativeDeviceApi.saveDeviceConfig(resumed)
      status.value = await nativeDeviceApi.deviceConfigStatus()
    }
    const runtime = await ensureRemoteClientRuntime()
    if (runtime.state === 'waiting') throw new Error('当前设备尚未完成远程配对，不能连接服务端。')
    connection.value = 'connected'
    reconnectAttempt.value = 0
    if (runtime.state === 'ready') {
      taskChannel.value = runtime.task.state
      taskChannelError.value = runtime.task.error || ''
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
    if (!automatic && runtime.state === 'ready') notice.value = `已连接到 ${status.value?.device_config?.paired_host?.display_name || '已配对服务端'}。`
    if (!automatic && runtime.state === 'management_only') {
      notice.value = runtime.core_state === 'starting'
        ? '服务端管理已连接，语音核心正在准备；组件管理仍可使用。'
        : '服务端管理已连接，语音核心暂不可用；组件管理仍可使用。'
    }
  } catch (failure) {
    connection.value = 'disconnected'
    if (!automatic) error.value = failure instanceof Error ? failure.message : String(failure)
    scheduleReconnect()
  } finally {
    busy.value = false
    await refresh()
  }
}

function scheduleReconnect() {
  if (reconnectTimer.value !== null) clearTimeout(reconnectTimer.value)
  if (mode.value !== 'remote' || !status.value?.device_config?.paired_host
      || status.value.device_config.reconnect_suspended_reason) return
  const delays = [1000, 2000, 4000, 8000, 15000]
  const wait = delays[Math.min(reconnectAttempt.value, delays.length - 1)]
  reconnectAttempt.value = Math.min(reconnectAttempt.value + 1, delays.length - 1)
  reconnectTimer.value = setTimeout(() => { void connectHost(true) }, wait)
}

async function disconnectHost() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    if (mediaChannel.value === 'running') await nativeDeviceApi.remoteMediaChannel('stop')
    if (taskChannel.value === 'running') await nativeDeviceApi.remoteTaskChannel('stop')
    await nativeDeviceApi.disconnectSshHost()
    const saved = status.value?.device_config
    if (saved) {
      await nativeDeviceApi.saveDeviceConfig({ ...saved, reconnect_suspended_reason: 'user_disconnected' })
    }
    connection.value = 'disconnected'
    taskChannel.value = 'stopped'
    mediaChannel.value = 'stopped'
    mediaCaptureActive.value = null
    if (reconnectTimer.value !== null) clearTimeout(reconnectTimer.value)
    reconnectTimer.value = null
    notice.value = '已断开服务端连接。点击“连接服务端”可重新连接。'
    await refresh()
  } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { busy.value = false }
}

onMounted(async () => {
  await refresh()
  if (mode.value === 'remote' && status.value?.device_config?.paired_host
      && !status.value.device_config.reconnect_suspended_reason && connection.value !== 'connected') {
    await connectHost(true)
  }
})

onUnmounted(() => {
  stopOpenSshJobWatch()
  stopRemoteOpenSshStatusWatch()
  if (pairingExpiryTimer.value !== null) clearTimeout(pairingExpiryTimer.value)
  stopPairingStatusWatch()
  if (reconnectTimer.value !== null) clearTimeout(reconnectTimer.value)
})
</script>

<template>
  <section class="device-settings">
    <header class="device-heading">
      <div><p class="eyebrow">DEVICE RUNTIME · 设备运行</p><h2>设备连接</h2><p>选择本地运行或远程配对。模式切换会停止旧角色，再按目标模式启动；实际状态单独显示。</p></div>
      <span class="role-state" :data-state="status?.runtime.config_state || 'loading'">{{ status?.runtime.config_state === 'valid' ? '配置有效' : status?.runtime.config_state === 'invalid' ? '配置需处理' : localCoreAvailable ? '本地运行' : '远程待配对' }}</span>
    </header>

    <p v-if="error" class="device-message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="device-message" role="status">{{ notice }}</p>
    <p v-if="status?.runtime.error" class="device-message warning">{{ status.runtime.error }} 原文件已保留，未自动覆盖。</p>
    <p v-if="!localCoreAvailable && mode === 'local'" class="device-message warning">当前工作区未配置本机核心运行组件。请选择“远程配对”；无组件客户端不会启动本机语音核心。</p>
    <p v-if="status?.device_runtime" class="device-message" role="status">
      目标模式：{{ status.device_runtime.expected_mode === 'remote' ? '远程配对' : '本地运行' }}；实际模式：{{ status.device_runtime.actual_mode === 'remote' ? '远程配对' : status.device_runtime.actual_mode === 'local' ? '本地运行' : '尚未就绪' }}；阶段：{{ status.device_runtime.switch_phase || '未知' }}。{{ status.device_runtime.actual_mode === 'remote' ? '管理连接和语音核心就绪状态分别显示。' : status.device_runtime.core_ready ? '本机语音核心已就绪。' : '本机语音核心尚未就绪。' }}
    </p>

    <fieldset :disabled="loading || busy || openSshBusy || openSshJobPending">
      <label>运行模式
        <select v-model="mode" @change="onModeChanged">
          <option value="local" :disabled="!localCoreAvailable">本地运行</option>
          <option value="remote">远程配对</option>
        </select>
      </label>
      <span class="small-text">选择后立即保存并切换运行模式。</span>
    </fieldset>
    <section v-if="mode === 'local'" class="device-card">
      <h3>局域网连接</h3>
      <label class="device-toggle"><span><strong>广播并接受配对</strong><small>开启后本机出现在局域网搜索结果中，并接受六位码配对。</small></span>
        <input v-model="broadcastEnabled" type="checkbox" aria-label="广播并接受配对" :disabled="broadcastPreflightBusy || busy || loading || openSshBusy || openSshJobPending" @change="onBroadcastChanged" />
      </label>
      <div class="button-row">
        <button class="outline-button" :disabled="busy || loading || broadcastPreflightBusy || openSshBusy || openSshStatusLoading || openSshJobPending" @click="checkOpenSshServer">检查 SSH 服务</button>
        <button v-if="isBroadcasting" class="primary-button" :disabled="busy || loading || !openSshStatus?.ready || !openSshStatus.project_access_valid || !openSshStatus.pairing_access_valid" @click="openPairingWindow">获取配对码</button>
        <button v-if="pairingCode" class="outline-button" :disabled="busy" @click="closePairingWindow">关闭配对窗口</button>
      </div>
      <p v-if="openSshStatus" class="small-text" role="status">
        官方程序：{{ openSshStatus.official_binary ? '微软签名已验证' : '未验证' }}；版本：{{ openSshStatus.source_version || '未检测到' }}；路径：{{ openSshStatus.source_path || '未检测到' }}。
        服务：{{ openSshStatus.service_present ? (openSshStatus.service_state || '状态未知') : '不存在' }}；有效端口：{{ openSshStatus.effective_port ?? '未验证' }}；实际监听：{{ openSshStatus.listening_ports?.join(', ') || '无' }}。
      </p>
      <p v-if="openSshStatus" class="small-text">当前账户公钥：{{ openSshStatus.authorized_keys_exists ? openSshStatus.authorized_keys_path : '尚未创建' }}；本程序 SSH 规则：{{ openSshStatus.project_access_valid ? openSshStatus.project_network_scope.join(', ') : openSshStatus.project_access_enabled ? '已启用但范围或端口未通过复核' : '未启用' }}；配对规则：{{ openSshStatus.pairing_access_valid ? '已通过程序、端口和子网复核' : openSshStatus.pairing_access_enabled ? '已启用但未通过复核' : '未启用' }}。</p>
      <details v-if="openSshStatus?.existing_ssh_inbound_rules.length" class="device-message warning ssh-rule-details">
        <summary>检测到 {{ openSshStatus.existing_ssh_inbound_rules.length }} 条可能涉及 SSH 的其他入站规则。本程序只读报告，不会修改原规则。展开查看摘要。</summary>
        <ul>
          <li v-for="rule in visibleSshRuleDetails" :key="rule">{{ rule }}</li>
        </ul>
        <p v-if="hiddenSshRuleDetailCount">还有 {{ hiddenSshRuleDetailCount }} 条规则未展开显示。</p>
      </details>
      <p v-if="status?.device_runtime" class="small-text">广播意图：{{ status.device_runtime.broadcast_requested ? '开启' : '关闭' }}；mDNS 公告：{{ status.device_runtime.broadcast_actual ? '已发布' : '未发布' }}。</p>
      <p class="small-text">“检查 SSH 服务”先只读检查。服务未就绪时会说明缺口，并在你同意后通过 Windows 官方功能安装或启动；误取消后可再次点击。</p>
      <p v-if="openSshStatusError" :class="['device-message', openSshLongWaitNotice ? 'warning' : 'error']" :role="openSshLongWaitNotice ? 'status' : 'alert'">{{ openSshStatusError }}</p>
      <p v-if="openSshJob" class="small-text" role="status">任务状态：{{ localOpenSshJobStateLabel(openSshJob.state) }}；{{ openSshJob.phase }}<span v-if="openSshJob.error">：{{ openSshJob.error }}</span></p>
      <div v-if="pairingCode" class="pairing-code-box" role="status">
        <span>本机六位配对码 · 有效至 {{ new Date(pairingExpiresAt).toLocaleTimeString() }}</span>
        <output>{{ pairingCode }}</output>
        <button class="quiet-button" :disabled="busy" @click="copyPairingCode">复制配对码</button>
        <small>配对成功、关闭窗口或两分钟到期后立即失效。</small>
      </div>
    </section>
    <section v-else class="device-card">
      <h3>本设备连接</h3>
      <label class="device-toggle"><span><strong>在线语音在本机合成并播放</strong><small>配对后由本机语音服务合成，音频不回传服务端。</small></span>
        <input v-model="clientOnlineTtsLocal" type="checkbox" aria-label="在线语音在本机合成并播放" :disabled="busy || loading" @change="onRemotePreferenceChanged" />
      </label>
      <label class="device-toggle"><span><strong>压缩远程音频</strong><small>通过 Opus 编码减少局域网传输数据。</small></span>
        <input v-model="audioCompression" type="checkbox" aria-label="压缩远程音频" :disabled="busy || loading" @change="onRemotePreferenceChanged" />
      </label>
    </section>
    <p v-if="mode === 'remote' && audioCompression" class="device-message warning">Opus 双向压缩仍需完成真实音质及识别对照。</p>

    <section v-if="isRemote" class="device-card remote-pairing-card">
      <h3>远程配对</h3>
      <p v-if="status?.device_config?.paired_host" class="device-message" role="status">
        当前服务端：{{ status.device_config.paired_host.display_name }}；{{ connection === 'connected' ? '已连接' : '连接中断，可重新搜索并连接' }}。
      </p>
      <div class="button-row">
        <button class="primary-button" :disabled="busy || searchingDevices" @click="searchForDevices">{{ searchingDevices ? '正在搜索…' : '搜索设备' }}</button>
      </div>
      <p v-if="discoveredDevices.length === 0" class="small-text">搜索到的服务端会显示在这里；无需填写地址、账户或密钥。</p>
      <label v-if="discoveredDevices.length > 0">选择服务端
        <select v-model="selectedHostId">
          <option v-for="device in discoveredDevices" :key="device.host_id" :value="device.host_id" :disabled="!device.compatible || device.state === 'unavailable'">
            {{ device.display_name }} · {{ device.state === 'ready' ? '可连接' : device.state === 'starting' ? '正在准备' : '不可用' }}{{ device.compatible ? '' : ' · 需更新程序' }}
          </option>
        </select>
      </label>
      <label v-if="selectedHostId">六位配对码
        <input v-model="pairingCodeInput" autocomplete="one-time-code" inputmode="numeric" maxlength="6" pattern="[0-9]{6}" placeholder="输入服务端显示的配对码" @input="pairingCodeInput = pairingCodeInput.replace(/[^0-9]/g, '').slice(0, 6)" />
      </label>
      <div v-if="selectedHostId" class="button-row">
        <button class="primary-button" :disabled="busy || !/^[0-9]{6}$/.test(pairingCodeInput)" @click="pairWithSelectedDevice">连接</button>
      </div>
      <p v-if="discoveredDevices.some(device => !device.compatible)" class="device-message warning">列表中有服务端使用不兼容的发现协议，请更新其中一端。</p>
    </section>

    <section v-if="isRemote" class="device-card remote-ssh-feature-card" aria-label="服务端 SSH 服务设置">
      <h3>服务端 SSH 服务</h3>
      <p>检查和准备当前配对服务端的微软 OpenSSH Server。准备操作在服务端执行；如果 Windows 要求管理员授权，请在服务端完成确认。</p>
      <div class="button-row">
        <button class="outline-button" :disabled="busy || loading || remoteOpenSshBusy || connection !== 'connected' || !status?.device_config?.paired_host" @click="checkRemoteOpenSshStatus">
          {{ remoteOpenSshBusy ? '正在读取服务端状态…' : '检查 SSH 服务' }}
        </button>
        <button v-if="remoteOpenSshCanEnable" class="primary-button" :disabled="busy || loading || remoteOpenSshBusy" @click="enableRemoteOpenSshServer">
          {{ remoteOpenSshSnapshot?.job?.state === 'failed' ? '重试服务端 SSH 准备' : '请求服务端安装或启动' }}
        </button>
      </div>
      <p v-if="!remoteOpenSshSnapshot && !remoteOpenSshError" class="small-text" role="status">
        {{ connection === 'connected' ? '点击“检查 SSH 服务”读取服务端快照。' : '连接服务端后才能读取其 SSH 状态。' }}
      </p>
      <template v-if="remoteOpenSshSnapshot && remoteOpenSshHostId === status?.device_config?.paired_host?.host_id">
        <p class="device-message" :data-state="remoteOpenSshSnapshot.status.ready ? 'ready' : 'not-ready'" role="status">
          {{ remoteOpenSshSnapshot.status.ready ? '服务端微软 OpenSSH Server 已就绪。' : remoteOpenSshSnapshot.status.capability_state === 'Unknown' ? '服务端 SSH 检查需要管理员授权；当前状态尚未就绪。' : '服务端微软 OpenSSH Server 尚未就绪。' }}
        </p>
        <p class="small-text">
          功能状态：{{ remoteOpenSshSnapshot.status.capability_state }}；服务：{{ remoteOpenSshSnapshot.status.service_present ? (remoteOpenSshSnapshot.status.service_state || '状态未知') : '不存在' }}；
          官方程序与签名：{{ remoteOpenSshSnapshot.status.official_binary ? '已通过微软签名检查' : remoteOpenSshSnapshot.status.signature_status || '未通过检查' }}；版本：{{ remoteOpenSshSnapshot.status.source_version || '未检测到' }}。
        </p>
        <p class="small-text">
          有效端口：{{ remoteOpenSshSnapshot.status.effective_port ?? '未验证' }}；实际监听：{{ remoteOpenSshSnapshot.status.listening_ports.join(', ') || '无' }}；
          配置：{{ remoteOpenSshSnapshot.status.configuration_valid ? '有效' : '未验证' }}；本程序局域网规则：{{ remoteOpenSshSnapshot.status.project_access_valid ? '已验证' : '未验证' }}；配对规则：{{ remoteOpenSshSnapshot.status.pairing_access_valid ? '已验证' : '未验证' }}。
        </p>
        <article v-if="remoteOpenSshSnapshot.job" class="remote-device-row" aria-live="polite">
          <div class="remote-device-title">
            <strong>服务端任务：{{ remoteOpenSshSnapshot.job.state }}</strong>
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
    </section>

    <section v-if="isRemote" class="device-card remote-channel-status">
      <h3>服务端连接状态</h3>
      <p>连接、任务和语音通道由程序自动准备；断线时不会把操作改投本机。</p>
      <div class="button-row">
        <span class="connection-state" :data-state="connection">管理连接：{{ connection === 'connected' ? '已连接' : connection === 'unavailable' ? '当前构建不支持' : '已断开' }}</span>
        <span class="connection-state" :data-state="remoteCoreState">语音核心：{{ remoteCoreState === 'ready' ? '就绪' : remoteCoreState === 'starting' ? '准备中' : remoteCoreState === 'unavailable' ? '暂不可用' : '状态未知' }}</span>
        <span class="connection-state" :data-state="taskChannel">服务端任务：{{ taskChannel === 'running' ? '已就绪' : taskChannel === 'error' ? '错误' : '准备中' }}</span>
        <span class="connection-state" :data-state="mediaChannel">服务端语音：{{ mediaChannel === 'running' ? '已就绪' : mediaChannel === 'error' ? '错误' : '准备中' }}</span>
        <span v-if="mediaChannel === 'running'" class="interaction-state">{{ mediaCaptureActive ? '服务端已请求麦克风采集' : '当前未采集麦克风' }}</span>
      </div>
      <p v-if="taskChannelError || mediaChannelError" class="device-message warning">{{ taskChannelError || mediaChannelError }}</p>
    </section>

    <footer class="device-footer">
      <div class="connection-state" :data-state="connection">服务端管理连接：{{ connection === 'connected' ? '已连接' : connection === 'unavailable' ? '当前构建不支持' : '已断开' }}</div>
      <div class="button-row">
        <button v-if="status?.runtime.config_state === 'invalid'" class="outline-button" :disabled="!canSave || busy" @click="saveConfig(true)">归档原件并修复</button>
        <button v-if="isRemote && status?.device_config?.paired_host && connection !== 'connected'" class="primary-button" :disabled="busy || !status || status.runtime.config_state === 'invalid'" @click="connectHost(false)">连接服务端</button>
        <button v-if="connection === 'connected'" class="quiet-button" :disabled="busy" @click="disconnectHost">断开</button>
        <button class="quiet-button" :disabled="loading || busy" @click="refresh">刷新</button>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.system-feature-status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:14px 0}.system-feature-status div{display:grid;gap:6px;padding:12px;border:1px solid #344157;border-radius:9px;background:rgba(17,26,41,.6)}.system-feature-status span{font-size:11px;color:#9aa9bf}.system-feature-status strong{font-size:13px;color:#dce5f4;overflow-wrap:anywhere}@media(max-width:700px){.system-feature-status{grid-template-columns:repeat(2,minmax(0,1fr))}}
.device-settings{padding:24px;max-width:900px;margin:0 auto;color:var(--text,#e5eaf3)}.device-heading{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:20px}.eyebrow{font-size:11px;letter-spacing:.14em;color:#8c9bb4}.device-heading h2{margin:5px 0 8px;font-size:24px}.device-heading p{max-width:640px;color:#aab4c5;line-height:1.65}.role-state,.connection-state{border:1px solid #344157;border-radius:999px;padding:7px 12px;color:#aebad0;font-size:12px;white-space:nowrap}.role-state[data-state=valid],.connection-state[data-state=connected]{color:#8ee0c0;border-color:#286c5a}.role-state[data-state=invalid]{color:#ffbe8b;border-color:#825333}.device-settings fieldset,.device-card{border:1px solid #273348;border-radius:14px;padding:18px;margin:14px 0;background:rgba(18,25,38,.54)}fieldset{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.device-settings label{display:flex;flex-direction:column;gap:7px;font-size:13px;color:#b8c4d6;margin:12px 0}.device-settings input,.device-settings select,.device-settings textarea{width:100%;border:1px solid #35435a;border-radius:8px;background:#111a29;color:#e8edf6;padding:10px 11px;min-width:0}.device-settings textarea{resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:12px}.device-card h3{margin:4px 0 8px;font-size:16px}.device-card p{color:#aab4c5;line-height:1.65}.device-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(120px,1fr);gap:14px}.button-row{display:flex;flex-wrap:wrap;gap:9px;margin:12px 0}.device-settings button{border-radius:8px;padding:9px 13px;cursor:pointer}.device-settings button:disabled{opacity:.48;cursor:not-allowed}.primary-button{background:#6686df;color:white;border:1px solid #7b9aeb}.outline-button,.quiet-button{background:transparent;color:#cdd7e8;border:1px solid #3a4962}.quiet-button{color:#aab4c5}.candidate-box{display:grid;gap:8px;padding:12px;border:1px solid #785f35;border-radius:10px;background:#211e18;color:#e1c78e}.candidate-box code,.fingerprint code,.small-text code,.remote-device-title code{overflow-wrap:anywhere}.small-text{font-size:12px}.fingerprint{font-size:13px;color:#d9e4f5}.device-footer{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding-top:12px}.device-footer .button-row{margin:0}.device-message{padding:11px 14px;border-radius:9px;background:#1c2b3e;color:#c7d7f0}.device-message.error{background:#422727;color:#ffc5b8}.device-message.warning{background:#392f1d;color:#efce8b}.remote-device-list{margin-top:20px;border-top:1px solid #273348;padding-top:16px}.remote-list-heading,.remote-device-title{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.remote-list-heading{justify-content:space-between}.remote-list-heading h3{margin:0}.remote-list-heading span,.device-generation{font-size:12px;color:#9aa9bf}.remote-device-row{padding:13px 0;border-bottom:1px solid #273348}.remote-device-title strong{font-size:14px}.remote-device-title code{font-size:12px;color:#aab4c5}.remote-device-title .connection-state{margin-left:auto}.interaction-state,.remote-device-capabilities span{border:1px solid #355a54;border-radius:999px;padding:4px 9px;color:#9bdec6;font-size:11px}.remote-device-capabilities{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.remote-device-capabilities span{border-color:#344157;color:#c4d0e2}.device-generation{display:block}.pairing-code-box{display:grid;gap:8px;border:1px solid #785f35;border-radius:10px;padding:14px;background:#211e18;color:#e1c78e}.pairing-code-box output{font:700 28px ui-monospace,Consolas,monospace;letter-spacing:.18em;color:#fff}.pairing-code-box small{color:#cdbb97}.pairing-duration{max-width:180px}.pairing-duration input{max-width:140px}@media(max-width:700px){.device-settings{padding:16px}fieldset{grid-template-columns:1fr}.device-heading{display:block}.device-grid{grid-template-columns:1fr}.device-footer{align-items:flex-start;flex-direction:column}}
.ssh-rule-details{overflow-wrap:anywhere;word-break:break-word}.ssh-rule-details>summary{cursor:pointer;font-weight:600;line-height:1.5}.ssh-rule-details ul{max-height:120px;overflow:auto;margin:8px 0 0;padding-left:20px}.ssh-rule-details li{font-size:12px;line-height:1.5;overflow-wrap:anywhere;word-break:break-word}.ssh-rule-details p{margin:8px 0 0;font-size:12px}
</style>
