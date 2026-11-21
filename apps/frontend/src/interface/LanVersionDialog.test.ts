// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import LanVersionDialog from './LanVersionDialog.vue'
import { lanVersionSync } from '../product/lan-version-sync'
import { nativeDeviceApi } from '../product/native-device-api'

vi.mock('../product/desktop-port', () => ({ APP_UPDATE_EVENT: 'desktop://app-update-progress', desktopPort: () => ({ listen: vi.fn().mockResolvedValue(() => undefined) }) }))

describe('LanVersionDialog（局域网版本确认项目弹窗）', () => {
  beforeEach(() => {
    Object.assign(lanVersionSync, {open:true,busy:false,waitingRestart:false,canCancel:false,error:'',phase:'',completedBytes:0,totalBytes:0,dismissedKey:'',
      check:{host_id:'server-a',local_version:'1.0.0',remote_version:'1.1.0',comparison:'local_older',can_sync:true,reason:null,fields:{}}})
    vi.spyOn(window, 'confirm').mockImplementation(() => { throw new Error('应使用项目弹窗') })
  })
  afterEach(() => vi.restoreAllMocks())
  it('显示版本与本机重启影响，只有点击确认才提交正式同步入口', async () => {
    const start=vi.spyOn(nativeDeviceApi,'versionSync').mockResolvedValue({source:'lan',state:'downloading',current_version:'1.0.0',phase:'接收分发器',completed_bytes:100,total_bytes:200,can_cancel:true,fields:{}})
    const wrapper=mount(LanVersionDialog)
    expect(wrapper.get('[role="dialog"]').attributes('aria-modal')).toBe('true')
    expect(wrapper.text()).toContain('本机会退出')
    expect(start).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(b=>b.text()==='同步并更新')!.trigger('click')
    await flushPromises()
    expect(start).toHaveBeenCalledWith('server-a','1.1.0')
    expect(wrapper.get('progress').attributes('aria-label')).toBe('局域网版本同步进度')
    expect(wrapper.get('progress').attributes('value')).toBe('50')
    expect(wrapper.text()).toContain('取消同步')
    expect(window.confirm).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('服务端较旧时明确更新服务端，暂不同步不启动更新', async () => {
    Object.assign(lanVersionSync.check!,{local_version:'1.2.0',remote_version:'1.0.0',comparison:'remote_older'})
    const start=vi.spyOn(nativeDeviceApi,'versionSync')
    const wrapper=mount(LanVersionDialog)
    expect(wrapper.text()).toContain('服务端会退出')
    await wrapper.findAll('button').find(b=>b.text()==='暂不同步')!.trigger('click')
    expect(lanVersionSync.open).toBe(false)
    expect(start).not.toHaveBeenCalled()
    expect(lanVersionSync.dismissedKey).toContain('server-a')
    wrapper.unmount()
  })
})
