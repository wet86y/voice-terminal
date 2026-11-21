import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nativeDeviceApi, type DeviceVersionCheck } from './native-device-api'
import { ensureLanVersion, lanVersionSync, dismissLanVersionSync, showLanVersionSync, confirmLanVersionSync, cancelLanVersionSync, updateLanVersionProgress } from './lan-version-sync'

function check(comparison: DeviceVersionCheck['comparison'] = 'local_older'): DeviceVersionCheck {
  return { host_id: 'host-a', local_version: '1.0.0', remote_version: comparison === 'same' ? '1.0.0' : '1.1.0', comparison, can_sync: comparison !== 'same', reason: null, fields: {} }
}
describe('lan-version-sync（局域网版本同步）', () => {
  beforeEach(() => {
    Object.assign(lanVersionSync, { check: null, open: false, busy: false, waitingRestart: false, waitingSince: 0, canCancel: false, completedBytes: 0, totalBytes: 0, phase: '', error: '', dismissedKey: '' })
  })
  afterEach(() => vi.restoreAllMocks())

  it('正式版本号相同就继续连接，不要求构建信息或分发器', async () => {
    vi.spyOn(nativeDeviceApi, 'versionCheck').mockResolvedValue(check('same'))
    expect(await ensureLanVersion()).toBe(true)
    expect(lanVersionSync.open).toBe(false)
  })
  it('异版先弹窗，取消后自动重连不重复提醒，设备页可重新打开', async () => {
    vi.spyOn(nativeDeviceApi, 'versionCheck').mockResolvedValue(check())
    expect(await ensureLanVersion()).toBe(false)
    expect(lanVersionSync.open).toBe(true)
    dismissLanVersionSync()
    expect(await ensureLanVersion()).toBe(false)
    expect(lanVersionSync.open).toBe(false)
    showLanVersionSync()
    expect(lanVersionSync.open).toBe(true)
  })
  it('确认绑定弹窗展示的主机与版本，传输完成仍等待目标版本复核', async () => {
    const status = vi.spyOn(nativeDeviceApi, 'versionCheck').mockResolvedValue(check())
    const start = vi.spyOn(nativeDeviceApi, 'versionSync').mockResolvedValue({ source: 'lan', state: 'checking', current_version: '1.0.0', can_cancel: true, fields: {} })
    await ensureLanVersion()
    await confirmLanVersionSync()
    expect(start).toHaveBeenCalledWith('host-a', '1.1.0')
    expect(await ensureLanVersion()).toBe(false)
    expect(status).toHaveBeenCalledTimes(1)
    updateLanVersionProgress({ source: 'lan', state: 'current', current_version: '1.0.0', phase: '等待服务端更新重启', fields: {} })
    expect(lanVersionSync.busy).toBe(true)
    updateLanVersionProgress({source:'lan',state:'checking',current_version:'1.0.0',can_cancel:true,fields:{}})
    expect(lanVersionSync.canCancel).toBe(false)
    expect(await ensureLanVersion()).toBe(false)
    status.mockResolvedValue(check('same'))
    expect(await ensureLanVersion()).toBe(true)
    expect(lanVersionSync.busy).toBe(false)
    expect(lanVersionSync.open).toBe(false)
  })
  it('缺少高版本分发器只展示原因，不启动同步', async () => {
    vi.spyOn(nativeDeviceApi, 'versionCheck').mockResolvedValue({ ...check(), can_sync: false, reason: '没有匹配版本的分发器' })
    const start = vi.spyOn(nativeDeviceApi, 'versionSync')
    await ensureLanVersion()
    await confirmLanVersionSync()
    expect(start).not.toHaveBeenCalled()
    expect(lanVersionSync.check?.reason).toContain('分发器')
  })
  it('收到目标设备交接失败回执后结束等待，并显示目标的实际原因', async () => {
    vi.spyOn(nativeDeviceApi, 'versionCheck').mockResolvedValue({...check(),remote_update_state:'failed',remote_update_error:'目标设备磁盘空间不足'})
    Object.assign(lanVersionSync,{busy:true,waitingRestart:true,waitingSince:Date.now()})
    expect(await ensureLanVersion()).toBe(false)
    expect(lanVersionSync.busy).toBe(false)
    expect(lanVersionSync.error).toContain('磁盘空间不足')
  })
  it('局域网同步进度不接受普通应用查新的回执', () => {
    Object.assign(lanVersionSync,{busy:true,phase:'正在接收局域网分发器'})
    updateLanVersionProgress({state:'current',current_version:'1.0.0',phase:'已是最新版本',fields:{}})
    expect(lanVersionSync.phase).toBe('正在接收局域网分发器')
    expect(lanVersionSync.waitingRestart).toBe(false)
  })
  it('取消通过原生维护入口提交，等待实际取消结果后才结束进度', async () => {
    const cancel = vi.spyOn(nativeDeviceApi, 'cancelVersionSync').mockResolvedValue()
    Object.assign(lanVersionSync, { busy: true, canCancel: true })
    await cancelLanVersionSync()
    expect(cancel).toHaveBeenCalledOnce()
    expect(lanVersionSync.busy).toBe(true)
    updateLanVersionProgress({ source: 'lan', state: 'cancelled', current_version: '1.0.0', fields: {} })
    expect(lanVersionSync.busy).toBe(false)
    expect(lanVersionSync.error).toContain('取消')
  })
  it('重启等待的短暂断线不报同步失败，超过等待窗口后给出重试原因', async () => {
    vi.spyOn(nativeDeviceApi, 'versionCheck').mockRejectedValue(new Error('连接断开'))
    Object.assign(lanVersionSync, { busy: true, waitingRestart: true, waitingSince: Date.now() })
    expect(await ensureLanVersion()).toBe(false)
    expect(lanVersionSync.busy).toBe(true)
    lanVersionSync.waitingSince = Date.now() - 181000
    expect(await ensureLanVersion()).toBe(false)
    expect(lanVersionSync.busy).toBe(false)
    expect(lanVersionSync.error).toContain('尚未确认')
  })
})
