// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import CaptureImages from './CaptureImages.vue'
import { acquireImage, acceptImageChunk, clearImageChannel, configureImageChannel, releaseImage, suspendImageChannel } from './image-channel'
import type { ImageAttachment } from '../types'

const attachment = (role: 'global' | 'local'): ImageAttachment => ({ attachment_id: role, conversation_id: 'c', role,
  mime_type: 'image/png', width: 960, height: 640, bytes: 3, sha256: '', preview_url: `data:image/png;base64,AQID` })
afterEach(() => { clearImageChannel(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('图片气泡与放大观察', () => {
  it('双图分别打开，支持切换、实际像素、复位及退出焦点恢复', async () => {
    const wrapper = mount(CaptureImages, { attachTo: document.body, props: { attachments: [attachment('global'), attachment('local')] } })
    await flushPromises()
    expect(wrapper.findAll('.capture-thumbnail')).toHaveLength(2)
    const thumbnail = wrapper.get('[aria-label="放大全局画面"]')
    ;(thumbnail.element as HTMLElement).focus()
    await thumbnail.trigger('click'); await flushPromises()
    const dialog = document.querySelector('[role="dialog"]')!
    expect(dialog.textContent).toContain('全局画面')
    const click = async (text: string) => { [...dialog.querySelectorAll('button')].find(b => b.textContent === text)!.click(); await flushPromises() }
    await click('局部区域'); expect(dialog.querySelector('img')?.alt).toBe('局部区域')
    await click('实际像素'); expect(dialog.querySelector('img')?.classList.contains('actual')).toBe(true)
    await click('复位'); expect(dialog.querySelector('img')?.classList.contains('actual')).toBe(false)
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await flushPromises()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(thumbnail.element)
    wrapper.unmount()
  })

  it('乱序分块组装一次，缓存共享引用并在退出后释放对象地址', async () => {
    const create = vi.fn(() => 'blob:picture'), revoke = vi.fn()
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke })
    // 这里测试分块、字节校验与生命周期；浏览器摘要验证由真实环境验收。
    vi.stubGlobal('crypto', {})
    const send = vi.fn(() => true); configureImageChannel(send)
    const a = { ...attachment('global'), preview_url: undefined }
    const first = acquireImage(a), second = acquireImage(a)
    expect(send).toHaveBeenCalledTimes(1)
    await acceptImageChunk({ conversation_id: 'wrong', attachment_id: 'global', index: 0, total: 2, mime_type: 'image/png', data: 'AQ' })
    await acceptImageChunk({ conversation_id: 'c', attachment_id: 'global', index: 1, total: 2, mime_type: 'image/png', data: 'ID' })
    await acceptImageChunk({ conversation_id: 'c', attachment_id: 'global', index: 0, total: 2, mime_type: 'image/png', data: 'AQ' })
    expect(await first).toBe('blob:picture'); expect(await second).toBe('blob:picture')
    releaseImage(a); expect(revoke).not.toHaveBeenCalled()
    releaseImage(a); expect(revoke).toHaveBeenCalledWith('blob:picture')
    vi.unstubAllGlobals()
  })

  it('无连接时显示错误和重试入口，不隐藏整个气泡', async () => {
    configureImageChannel(() => false)
    const wrapper = mount(CaptureImages, { props: { attachments: [{ ...attachment('global'), preview_url: undefined }] } })
    await flushPromises()
    expect(wrapper.text()).toContain('图片连接尚未就绪')
    expect(wrapper.text()).toContain('重试')
    expect(wrapper.text()).toContain('全局画面')
    wrapper.unmount()
  })

  it('断线终止未完成取图，重试从新连接获取而不复用旧等待', async () => {
    const send = vi.fn(() => true); configureImageChannel(send)
    const a = { ...attachment('global'), preview_url: undefined }
    const first = acquireImage(a).catch(error => error.message)
    suspendImageChannel()
    expect(await first).toContain('断开')
    const second = acquireImage(a).catch(error => error.message)
    expect(send).toHaveBeenCalledTimes(2)
    suspendImageChannel(); await second
  })

  it('卸载后同一附件重新加载，迟到的摘要结果不会覆盖新请求或遗留对象地址', async () => {
    let finish!: (value: ArrayBuffer) => void
    const create = vi.fn(() => 'blob:new'), revoke = vi.fn()
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke })
    vi.stubGlobal('crypto', { subtle: { digest: () => new Promise<ArrayBuffer>(resolve => { finish = resolve }) } })
    configureImageChannel(() => true)
    const a = { ...attachment('global'), preview_url: undefined, sha256: '00'.repeat(32) }
    const first = acquireImage(a).catch(error => error.message)
    const oldDecode = acceptImageChunk({ conversation_id: 'c', attachment_id: 'global', index: 0, total: 1, mime_type: 'image/png', data: 'AQID' })
    releaseImage(a)
    const second = acquireImage(a)
    finish(new ArrayBuffer(32)); await oldDecode
    expect(await first).toContain('退出显示范围')
    expect(create).not.toHaveBeenCalled()
    vi.stubGlobal('crypto', {})
    await acceptImageChunk({ conversation_id: 'c', attachment_id: 'global', index: 0, total: 1, mime_type: 'image/png', data: 'AQID' })
    expect(await second).toBe('blob:new')
    releaseImage(a); expect(revoke).toHaveBeenCalledWith('blob:new')
  })
})
