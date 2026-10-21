// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import LocalComponents from './LocalComponents.vue'
import type { LocalComponentsModel } from './local-components'

function model(): LocalComponentsModel {
  return {
    status: '运行正常', state: 'ready', sampledAt: '当前', resources: [], resourceActionsAvailable: true,
    rows: [{
      id: 'voice-system', name: 'Voice Server（语音服务）', state: 'ready', status: '运行中', available: true,
      runtime: '本地系统服务', description: '完整语音运行环境', facts: [],
      resource: { id: 'voice-server', label: 'Voice Server（语音服务）', state: 'installed', status: '已配置' },
    }],
  }
}

async function expandCategory(wrapper: ReturnType<typeof mount>, category: string) {
  const toggle = wrapper.get(`[data-category="${category}"] .local-category-toggle`)
  if (toggle.attributes('aria-expanded') !== 'true') await toggle.trigger('click')
}

async function expandComponent(wrapper: ReturnType<typeof mount>, category: string, id: string) {
  await expandCategory(wrapper, category)
  const card = wrapper.get(`[data-component="${id}"]`)
  const toggle = card.find('.local-component-action')
  if (toggle.exists() && toggle.attributes('aria-expanded') !== 'true') await toggle.trigger('click')
  return card
}

describe('LocalComponents（本地组件）运行环境与导入入口', () => {
  it('增强开关在组件详情可操作，切换期间锁定并显示持久化状态', async () => {
    const value = model()
    const toggle = vi.fn(async () => {})
    value.rows[0]!.enhancement = { label: '双路精修', enabled: true, busy: false, toggle }
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    const control = card.get('[role="switch"]')
    expect((control.element as HTMLInputElement).checked).toBe(true)
    await control.setValue(false)
    expect(toggle).toHaveBeenCalledTimes(1)
    await wrapper.setProps({ model: { ...value, rows: [{ ...value.rows[0]!, enhancement: { label: '双路精修', enabled: false, busy: true, toggle } }] } })
    expect(control.attributes('disabled')).toBeDefined()
    expect(card.get('.component-enhancement-copy small').text()).toContain('切换中')
    wrapper.unmount()
  })
  it('资源齐全时保留一键下载与资源导入入口，标题不显示旧说明', async () => {
    const wrapper = mount(LocalComponents, { props: { model: model() } })
    const heading = wrapper.get('.local-managed-heading')
    expect(heading.get('h3').text()).toBe('组件分类')
    expect(heading.text()).not.toContain('个组件')
    expect(heading.findAll('button').map(button => button.text())).toEqual(['一键下载', '资源导入'])
    await heading.get('.resource-download-required').trigger('click')
    expect(wrapper.emitted('resourceDownloadRequired')).toEqual([[]])
    wrapper.unmount()
  })

  it('共享框架任务只属于框架卡片，不激活两款本地模型的进度', async () => {
    const value = model()
    value.rows[0]!.runtimeDependencies = [
      { id: 'voice-runtime-cpu', label: '中央处理器依赖', state: 'installed', status: '已安装', required: true },
      { id: 'tts-audiocpp-runtime', label: '原生语音框架', state: 'missing', status: '缺失', required: false },
    ]
    value.rows.push(...['tts-voxcpm1', 'tts-voxcpm2'].map(id => ({ ...value.rows[0]!, id, name: id,
      category: 'tts' as const, runtimeDependencies: undefined,
      resource: { id, label: id, state: 'installed' as const, status: '已安装', required: false },
      sharedResource: { id: 'tts-audiocpp-runtime', label: '原生语音框架', state: 'missing' as const, status: '缺失', required: false } })))
    value.managedResourceJobs = [{ id: 'framework', resourceId: 'tts-audiocpp-runtime', label: '框架', state: 'running', phase: '下载固定资源', progressKind: 'download', canCancel: true }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    expect(wrapper.find('[data-category="tts"] [data-job]').exists()).toBe(false)
    await expandCategory(wrapper, 'tts')
    expect(wrapper.find('[data-category="tts"] [data-job]').exists()).toBe(false)
    await expandCategory(wrapper, 'voice-server')
    expect(wrapper.findAll('[data-job="framework"]')).toHaveLength(1)
    wrapper.unmount()
  })

  it('分类摘要会跟随子组件显示启动中、运行中和错误状态', async () => {
    const value = model()
    value.rows = [{
      id: 'fsmn-vad', name: 'FSMN-VAD（语音活动检测）', category: 'speech',
      state: 'loading', status: '启动中', available: false, runtime: 'ONNX Runtime',
      description: '正在加载语音检测组件。', facts: [],
    }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const summary = wrapper.get('[data-category="speech"] .local-category-observability .local-state')
    expect(summary.text()).toBe('启动中')
    expect(summary.classes()).toContain('loading')

    await wrapper.setProps({ model: { ...value, rows: [{ ...value.rows[0]!, state: 'ready', status: '运行中', available: true }] } })
    expect(summary.text()).toBe('运行中')
    expect(summary.classes()).toContain('ready')

    await wrapper.setProps({ model: { ...value, rows: [{ ...value.rows[0]!, state: 'failed', status: '不可用', available: false,
      error: '组件启动失败', facts: [{ label: '语音门禁', value: '未就绪时暂不能开始会话' }] }] } })
    expect(summary.text()).toBe('错误')
    expect(summary.classes()).toContain('failed')
    wrapper.unmount()
  })

  it('语音服务展开三个依赖卡片，可选项不影响父级就绪且分类下载标记为可选', async () => {
    const value = model()
    value.rows[0]!.runtimeDependencies = [
      { id: 'voice-runtime-cpu', label: 'CPU（中央处理器）运行依赖', state: 'installed', status: '已安装', required: true },
      { id: 'tts-audiocpp-runtime', label: 'audio.cpp（语音合成引擎）运行依赖', state: 'missing', status: '缺失', required: false },
      { id: 'voice-runtime-gpu', label: 'GPU（图形处理器）运行依赖', state: 'missing', status: '缺失', required: false },
    ]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    expect(wrapper.findAll('[data-category="voice-server"] [data-component]')).toHaveLength(3)
    expect(card.get('.local-state').text()).toBe('已安装')
    expect(wrapper.find('.resource-batch-import').exists()).toBe(true)
    await wrapper.get('[data-component="voice-runtime-gpu"] button').trigger('click')
    expect(wrapper.emitted('resourceDownload')).toEqual([['voice-runtime-gpu']])
    expect(wrapper.find('[data-category="voice-server"] .category-download').exists()).toBe(false)
    await wrapper.setProps({ model: { ...value, rows: [{ ...value.rows[0]!, runtimeDependencies: value.rows[0]!.runtimeDependencies!.map(item => item.id === 'voice-runtime-cpu' ? { ...item, state: 'missing' as const } : item) }] } })
    await wrapper.get('[data-category="voice-server"] .category-download').trigger('click')
    const bundle = wrapper.emitted('resourceBundleDownload')![0]![0] as { entries: Array<{ id: string; optional: boolean }> }
    expect(bundle.entries.map(entry => [entry.id, entry.optional])).toEqual([['voice-runtime-cpu', false]])
    wrapper.unmount()
  })

  it('运行依赖子卡片显示任务进度并取消，CPU（中央处理器）兼容父级任务编号', async () => {
    const value = model()
    value.rows[0]!.runtimeDependencies = [
      { id: 'voice-runtime-cpu', label: 'CPU（中央处理器）运行依赖', state: 'missing', status: '缺失', required: true },
      { id: 'voice-runtime-gpu', label: 'GPU（图形处理器）运行依赖', state: 'missing', status: '缺失', required: false },
    ]
    value.managedResourceJobs = [{ id: 'env-cpu', resourceId: 'voice-server', label: '处理器运行依赖', state: 'running', phase: '下载基础依赖', completedBytes: 20, totalBytes: 100, canCancel: true }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    const cpu = wrapper.get('[data-component="voice-system"]')
    expect(cpu.get('[data-job="env-cpu"]').text()).toContain('20%')
    expect(card.findAll('[data-job="env-cpu"]')).toHaveLength(1)
    await cpu.get('button').trigger('click')
    expect(wrapper.emitted('cancelResource')).toEqual([['env-cpu']])
    expect(wrapper.get('[data-component="voice-runtime-gpu"] button').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('语音合成在分类详情中列出模型，分类下载清单包含可选本地资源', async () => {
    const value = model()
    value.rows = [{ id: 'tts', name: 'TTS（语音合成）', state: 'failed', status: '待配置', available: true,
      runtime: '在线或本地', description: '任一本地模型或在线接口可用即可启动。', facts: [],
      resourceChoices: [
        { id: 'tts-voxcpm2', label: 'VoxCPM2（第二代模型）', state: 'missing', status: '缺失', required: false },
        { id: 'tts-voxcpm1', label: 'VoxCPM1（第一代模型）', state: 'missing', status: '缺失', required: false },
      ],
      sharedResource: { id: 'tts-audiocpp-runtime', label: 'audio.cpp（共用运行程序）', state: 'missing', status: '缺失', required: false },
    }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'tts', 'tts')
    expect(card.find('select').exists()).toBe(false)
    expect(card.text()).toContain('VoxCPM2（第二代模型）')
    expect(card.text()).toContain('VoxCPM1（第一代模型）')
    await card.get('button[aria-label="VoxCPM2（第二代模型）：下载"]').trigger('click')
    expect(wrapper.emitted('resourceDownload')).toEqual([['tts-voxcpm2']])
    expect(wrapper.find('[data-category="tts"] .category-download').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('自动准备共用运行程序')
    wrapper.unmount()
  })
  it('全部安装就绪后保留批量导入，并保持语音服务详情入口与运行时摘要', async () => {
    const wrapper = mount(LocalComponents, { props: { model: model() } })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(true)
    expect(wrapper.find('.local-install-actions').exists()).toBe(false)
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    expect(card.get('[data-metric="runtime"]').text()).toBe('运行时本地系统服务')
    expect(card.find('.local-managed-resource').exists()).toBe(true)
    expect(wrapper.emitted('resourceRefresh')).toEqual([[], []])
    wrapper.unmount()
  })

  it('语音依赖不完整时在语音服务条目下载或导入，并在完成后隐藏批量入口', async () => {
    const value = model()
    value.rows[0].resource!.state = 'incomplete'
    const wrapper = mount(LocalComponents, { props: { model: value } })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(true)
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    await card.get('button[aria-label="Voice Server（语音服务）：下载"]').trigger('click')
    await card.get('button[aria-label="Voice Server（语音服务）：导入"]').trigger('click')
    expect(wrapper.emitted('resourceDownload')).toEqual([['voice-server']])
    expect(wrapper.emitted('resourceImport')).toEqual([['voice-server']])
    const complete = model()
    await wrapper.setProps({ model: complete })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(true)
    wrapper.unmount()
  })

  it('单个模型缺失时允许批量导入，进程故障本身不会重新显示批量入口', async () => {
    const value = model()
    value.rows.push({ ...value.rows[0], id: 'paraformer', name: 'Paraformer（语音识别）', state: 'failed', status: '不可用', available: false,
      resource: { id: 'paraformer-streaming', label: 'Paraformer（语音识别）', state: 'missing', status: '缺失' } })
    const wrapper = mount(LocalComponents, { props: { model: value } })
    await wrapper.get('.resource-batch-import').trigger('click')
    expect(wrapper.emitted('resourceImportAll')).toEqual([[]])
    await wrapper.setProps({ model: { ...value, rows: value.rows.map(row => ({ ...row, resource: { ...row.resource!, state: 'installed' } })) } })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(true)
    wrapper.unmount()
  })

  it('下载进度与取消入口直接呈现在原组件卡片，开始前显示不确定进度', async () => {
    const value = model()
    value.rows[0].resource!.state = 'missing'
    value.rows[0].resource!.error = '旧安装错误'
    value.managedResourceJobs = [{ id: 'env-all', resourceId: 'voice-server', label: '语音环境', state: 'running', phase: '计算本次依赖总下载量', canCancel: true }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    expect(card.get('[data-job="env-all"]').text()).toContain('计算本次依赖总下载量')
    expect(card.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
    expect(card.text()).not.toContain('旧安装错误')
    expect(wrapper.find('.managed-resource-jobs').exists()).toBe(false)
    expect(card.find('.local-install-actions').exists()).toBe(false)
    await card.get('button[aria-label="Voice Server（语音服务）：取消任务"]').trigger('click')
    expect(wrapper.emitted('cancelResource')).toEqual([['env-all']])
    await wrapper.setProps({ model: { ...value, managedResourceJobs: [{ ...value.managedResourceJobs[0], completedBytes: 150, totalBytes: 300 }] } })
    expect(card.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('50')
    expect(card.get('[data-job="env-all"]').text()).toContain('150 B / 300 B · 50%')
    wrapper.unmount()
  })

  it('同一组件运行中的新任务覆盖旧失败，完成后恢复详情入口', async () => {
    const value = model()
    value.rows[0].resource!.state = 'missing'
    const active = { id: 'env-new', resourceId: 'voice-server', label: '语音环境', state: 'running', phase: '正在下载语音运行依赖', completedBytes: 50, totalBytes: 100, canCancel: true }
    value.managedResourceJobs = [active, { ...active, id: 'env-old', state: 'failed', error: '旧失败' }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    expect(wrapper.findAll('[data-job]')).toHaveLength(1)
    expect(wrapper.get('[data-job]').attributes('data-job')).toBe('env-new')
    expect(wrapper.text()).not.toContain('旧失败')
    await wrapper.setProps({ model: { ...model(), managedResourceJobs: [{ ...active, state: 'complete', canCancel: false }] } })
    expect(wrapper.find('[data-job]').exists()).toBe(false)
    expect(wrapper.find('.local-install-actions').exists()).toBe(false)
    await card.get('.local-component-action').trigger('click')
    expect(card.find('.local-component-detail').exists()).toBe(true)
    expect(card.get('[data-metric="runtime"]').text()).toBe('运行时本地系统服务')
    wrapper.unmount()
  })

  it('失败原因与重试操作保留在所属组件卡片中', async () => {
    const value = model()
    value.rows[0].resource!.state = 'missing'
    value.managedResourceJobs = [{ id: 'env-all', resourceId: 'voice-server', label: '语音环境', state: 'failed', phase: '资源处理失败', error: '_socket（网络基础扩展）无法加载', canCancel: false }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'voice-server', 'voice-system')
    expect(card.get('[role="alert"]').text()).toContain('_socket（网络基础扩展）无法加载')
    expect(card.get('button[aria-label="Voice Server（语音服务）：重试下载"]').text()).toBe('重试下载')
    await card.get('button[aria-label="Voice Server（语音服务）：重试下载"]').trigger('click')
    expect(wrapper.emitted('resourceDownload')).toEqual([['voice-server']])
    wrapper.unmount()
  })

  it('批量导入进度复用批量入口所在区域，提交时关闭取消按钮', () => {
    const value = model()
    value.managedResourceJobs = [{ id: 'import-all', resourceId: 'batch', label: '批量资源导入', state: 'running', phase: '正在启用资源', canCancel: false }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const heading = wrapper.get('.local-managed-heading')
    expect(heading.get('[data-job="import-all"]').text()).toContain('正在启用资源')
    expect(heading.get('button').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.managed-resource-jobs').exists()).toBe(false)
    wrapper.unmount()
  })

  it('已安装模型有随包更新时作为必需资源显示更新、导入和跳转操作', async () => {
    const value = model()
    value.rows.push({ ...value.rows[0]!, id: 'paraformer', name: 'Paraformer（流式识别）',
      state: 'ready', status: '运行中', available: true, retryable: false,
      resource: { id: 'paraformer-streaming', label: 'Paraformer（流式识别）', state: 'installed', status: '已安装',
        updateAvailable: true, installedVersion: '1.0.0', targetVersion: '1.1.0' } })
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'speech', 'paraformer')
    expect(wrapper.find('.resource-batch-import').exists()).toBe(true)
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="Paraformer（流式识别）：更新"]').text()).toBe('更新')
    expect(card.get('button[aria-label="Paraformer（流式识别）：导入"]').text()).toBe('导入')
    expect(card.get('button[aria-label="Paraformer（流式识别）：跳转"]').text()).toBe('跳转')
    expect(card.find('.local-component-action').exists()).toBe(false)
    expect(card.text()).not.toContain('可更新')
    await card.get('button[aria-label="Paraformer（流式识别）：更新"]').trigger('click')
    await card.get('button[aria-label="Paraformer（流式识别）：导入"]').trigger('click')
    await card.get('button[aria-label="Paraformer（流式识别）：跳转"]').trigger('click')
    expect(wrapper.emitted('resourceDownload')).toEqual([['paraformer-streaming']])
    expect(wrapper.emitted('resourceImport')).toEqual([['paraformer-streaming']])
    expect(wrapper.emitted('openResourceSource')).toEqual([['paraformer-streaming']])
    wrapper.unmount()
  })

  it('更新任务沿用进度和取消入口，运行期间隐藏重复资源操作', async () => {
    const value = model()
    const resource = { id: 'paraformer-streaming', label: 'Paraformer（流式识别）', state: 'installed' as const,
      status: '已安装', updateAvailable: true, installedVersion: '1.0.0', targetVersion: '1.1.0' }
    value.rows.push({ ...value.rows[0]!, id: 'paraformer', name: 'Paraformer（流式识别）', resource })
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'speech', 'paraformer')
    await card.get('button[aria-label="Paraformer（流式识别）：更新"]').trigger('click')
    await wrapper.setProps({ model: { ...value, managedResourceJobs: [{ id: 'update-1', resourceId: resource.id,
      label: '更新 Paraformer', state: 'running', phase: '正在安装新模型', completedBytes: 50, totalBytes: 100, canCancel: true }],
      rows: value.rows.map(row => row.id === 'paraformer' ? { ...row, resource: { ...resource, busy: true } } : row) } })
    expect(card.get('[data-job="update-1"]').text()).toContain('50%')
    expect(card.find('.local-install-actions').exists()).toBe(false)
    await card.get('button.local-resource-cancel').trigger('click')
    expect(wrapper.emitted('cancelResource')).toEqual([['update-1']])
    wrapper.unmount()
  })

  it('更新失败保留需更新状态并提供重试更新、导入和来源操作', async () => {
    const value = model()
    value.rows.push({ ...value.rows[0]!, id: 'paraformer', name: 'Paraformer（流式识别）',
      state: 'ready', status: '运行中', available: true,
      resource: { id: 'paraformer-streaming', label: 'Paraformer（流式识别）', state: 'installed', status: '已安装',
        updateAvailable: true, installedVersion: '1.0.0', targetVersion: '1.1.0', error: '更新校验失败' } })
    value.managedResourceJobs = [{ id: 'update-failed', resourceId: 'paraformer-streaming', label: '更新 Paraformer',
      state: 'failed', phase: '更新失败', error: '更新校验失败', canCancel: false }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'speech', 'paraformer')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="Paraformer（流式识别）：重试更新"]').text()).toBe('重试更新')
    expect(card.get('button[aria-label="Paraformer（流式识别）：导入"]').text()).toBe('导入')
    expect(card.get('button[aria-label="Paraformer（流式识别）：跳转"]').text()).toBe('跳转')
    expect(card.text()).toContain('更新校验失败')
    wrapper.unmount()
  })

  it('一个组件正在下载时仍允许启动其他组件下载', async () => {
    const value = model()
    value.rows[0].resource!.state = 'missing'
    value.resourceTasksBusy = true
    value.rows.push({ ...value.rows[0], id: 'paraformer', name: 'Paraformer（流式语音识别）',
      resource: { id: 'paraformer-streaming', label: '流式识别模型', state: 'missing', status: '待安装' } })
    value.managedResourceJobs = [{ id: 'env-all', resourceId: 'voice-server', label: '语音环境',
      state: 'running', phase: '正在下载语音运行依赖', canCancel: true }]
    const wrapper = mount(LocalComponents, { props: { model: value } })
    const card = await expandComponent(wrapper, 'speech', 'paraformer')
    const button = card.get('button[aria-label="流式识别模型：下载"]')
    expect(button.attributes('disabled')).toBeUndefined()
    expect(button.text()).toBe('下载')
    await button.trigger('click')
    expect(wrapper.emitted('resourceDownload')).toEqual([['paraformer-streaming']])
    expect(wrapper.find('[data-category="voice-server"] .local-install-actions').exists()).toBe(false)
    wrapper.unmount()
  })
})
