// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { productApi } from '../api'
import { useHotwordSettings } from '../useHotwordSettings'
import HotwordSettings from './HotwordSettings.vue'
import { exportHotwordText } from '../hotwords'

let controls: ReturnType<typeof useHotwordSettings> // controls（共享页面草稿）。
vi.mock('../product-context', () => ({ useProductContext: () => controls }))

describe('HotwordSettings（热词编辑页）', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    controls = useHotwordSettings(vi.fn())
    vi.spyOn(productApi, 'hotwords').mockResolvedValue({ settings: { enabled: true, stream_enabled: true, threshold: .85,
      entries: [{ word: 'Claude', aliases: ['克劳得'], blacklist: [] }] }, status: 'empty' })
  })

  it('编辑与切换页面保留草稿，保存提交目标词和别名', async () => {
    const save = vi.spyOn(productApi, 'saveHotwords').mockImplementation(async value => ({ settings: value, message: '已保存' }))
    const wrapper = mount(HotwordSettings)
    await flushPromises()
    await wrapper.get('[aria-label="目标词 1"]').setValue('Claude Code')
    await wrapper.get('[aria-label="读音或别名 1"]').setValue('克劳得代码；克劳德代码')
    expect(wrapper.text()).toContain('有未保存的修改')
    wrapper.unmount()
    const reopened = mount(HotwordSettings)
    await flushPromises()
    expect((reopened.get('[aria-label="目标词 1"]').element as HTMLInputElement).value).toBe('Claude Code')
    await reopened.get('.hotword-save button').trigger('click')
    await flushPromises()
    expect(save).toHaveBeenCalledWith({ enabled: true, stream_enabled: true, threshold: .85,
      entries: [{ word: 'Claude Code', aliases: ['克劳得代码', '克劳德代码'], blacklist: [] }] })
    expect(reopened.text()).not.toContain('有未保存的修改')
    reopened.unmount()
  })

  it('导入合并相同目标，解析失败保留现有词表，试测不保存', async () => {
    const wrapper = mount(HotwordSettings)
    await flushPromises()
    const importer = vi.spyOn(productApi, 'importHotwords').mockResolvedValue({ entries: [
      { word: 'Claude', aliases: ['克劳德'], blacklist: ['天气'] },
      { word: '科大讯飞', aliases: [], blacklist: [] },
    ] })
    await controls.importHotwords('导入样本')
    expect(controls.hotwordForm.entries).toHaveLength(1)
    expect(controls.hotwordImportPreview.value).toHaveLength(2)
    controls.applyHotwordImport()
    expect(controls.hotwordForm.entries).toHaveLength(2)
    expect(controls.hotwordForm.entries[0].aliases).toEqual(['克劳得', '克劳德'])
    importer.mockRejectedValue(new Error('第 1 行格式无效'))
    await controls.importHotwords('错误样本')
    expect(controls.hotwordForm.entries).toHaveLength(2)
    expect(wrapper.text()).toContain('第 1 行格式无效')
    const save = vi.spyOn(productApi, 'saveHotwords')
    vi.spyOn(productApi, 'previewHotwords').mockResolvedValue({ text: 'Claude。', raw_text: '克劳得。',
      matches: [{ original: '克劳得', replacement: 'Claude', score: 1 }], elapsed_ms: 2, status: 'corrected' })
    await wrapper.get('textarea').setValue('克劳得。')
    await wrapper.get('.hotword-preview button').trigger('click')
    await flushPromises()
    expect(wrapper.get('.hotword-result').text()).toContain('Claude。')
    expect(save).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('别名仅触发输入事件后切页，仍保留内容和尾部分隔符', async () => {
    const wrapper = mount(HotwordSettings)
    await flushPromises()
    const input = wrapper.get('[aria-label="读音或别名 1"]')
    ;(input.element as HTMLInputElement).value = '新的读音； '
    await input.trigger('input')
    wrapper.unmount()
    const reopened = mount(HotwordSettings)
    await flushPromises()
    expect((reopened.get('[aria-label="读音或别名 1"]').element as HTMLInputElement).value).toBe('新的读音； ')
    expect(controls.hotwordForm.entries[0].aliases).toEqual(['新的读音'])
    reopened.unmount()
  })

  it('保留词内斜杠和空格，支持标签删除及独立流式开关', async () => {
    const wrapper = mount(HotwordSettings)
    await flushPromises()
    await wrapper.get('[aria-label="读音或别名 1"]').setValue('I/O；open source、中文别名')
    expect(controls.hotwordForm.entries[0].aliases).toEqual(['I/O', 'open source', '中文别名'])
    await wrapper.get('[aria-label="移除别名 I/O"]').trigger('click')
    expect(controls.hotwordForm.entries[0].aliases).toEqual(['open source', '中文别名'])
    const switches = wrapper.findAll('[role="switch"]')
    await switches[1].setValue(false)
    expect(controls.hotwordForm.enabled).toBe(true)
    expect(controls.hotwordForm.stream_enabled).toBe(false)
    wrapper.unmount()
  })

  it('CSV（逗号分隔文件）导出按标准转义逗号、引号及完整短语', () => {
    expect(exportHotwordText([{ word: 'a,"b"', aliases: ['open source', 'I/O'], blacklist: [] }]))
      .toBe('热词,别名,排除词\r\n"a,""b""","open source；I/O",""')
  })
})
