/** UI dialog layout（弹窗渲染夹具）：使用正式组件和固定数据，禁止真实网络与业务写入。 */
import { createApp, h, nextTick, ref } from 'vue'
import AppUpdateCard from '../../apps/frontend/src/interface/AppUpdateCard.vue'
import UiDialog from '../../apps/frontend/src/interface/UiDialog.vue'
import CaptureImages from '../../apps/frontend/src/interface/CaptureImages.vue'
import LanVersionDialog from '../../apps/frontend/src/interface/LanVersionDialog.vue'
import ResourceInstallDialog from '../../apps/frontend/src/product/panels/ResourceInstallDialog.vue'
import HotwordSettings from '../../apps/frontend/src/product/panels/HotwordSettings.vue'
import WorkspaceSettings from '../../apps/frontend/src/product/panels/WorkspaceSettings.vue'
import { provideProductContext } from '../../apps/frontend/src/product/product-context'
import { useHotwordSettings } from '../../apps/frontend/src/product/useHotwordSettings'
import { productApi } from '../../apps/frontend/src/product/api'
import { lanVersionSync } from '../../apps/frontend/src/product/lan-version-sync'
import '../../apps/frontend/src/interface/design.css'

const scene = ref('history') // scene（当前验收场景）。
const blocked = [] // blocked（被阻止的真实网络请求或业务写入）。
const deny = name => { blocked.push(name); throw new Error(`隔离验收禁止调用：${name}`) }
window.fetch = async input => deny(String(input))
const entries = Array.from({ length: 80 }, (_, index) => ({ word: `测试词条${index}`, aliases: [], blacklist: [] })) // entries（完整测试词表）。
productApi.hotwords = async () => ({ settings: { enabled: true, stream_enabled: true, threshold: .85, entries }, status: 'ready' })
productApi.saveHotwords = async () => deny('保存真实词表')

const resource = { // resource（资源弹窗只读状态）：所有确认回调拒绝写入。
  pendingResetPlan: ref(null), pendingDownloadPlan: ref(null), pendingDownloadBundlePlan: ref(null), pendingImport: ref(null), resources: ref([]),
  confirmDownload: () => deny('真实资源下载'), confirmDownloadBundle: () => deny('真实分类下载'), confirmReset: () => deny('真实重置'),
  confirmImport: accepted => { if (accepted) deny('真实导入'); resource.pendingImport.value = null },
}
const workspace = { // workspace（工作区只读状态）：清理只返回固定计划，带令牌的实际清理被禁止。
  canManage: ref(true), workspace: ref({ root: 'D:/测试工作区', version: '1.0.0', can_manage: true }),
  activeWorkspaceJob: ref(null), latestWorkspaceJob: ref(null), loading: ref(false), workspaceError: ref(''), eventError: ref(''), workspaceOperationStarting: ref(false),
  appUpdate: ref({ state: 'idle', current_version: '1.0.0', fields: {} }), integrity: ref({ state: 'idle', current_version: '1.0.0', fields: {} }),
  cleanupWorkspace: async token => {
    if (token) return deny('真实工作区清理')
    return { token: 'fixture', entries: Array.from({ length: 40 }, (_, index) => ({ path: `cache/测试条目-${index}`, reason: '可重新生成的测试文件', bytes: 1024 })), skipped: [], total_bytes: 40960 }
  },
}
const storagePort = { available: true, workspaceInfo: async () => ({root:'D:/测试工作区'}), storageScan: () => workspace.cleanupWorkspace(), storageCleanup: () => deny('真实本机清理'), uninstallPlan: () => deny('真实卸载预览'), uninstallResource: () => deny('真实组件卸载') } // storagePort（仅注入固定本机盘点，不连接业务）。
const picture = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><rect width="1200" height="800" fill="#202530"/><path d="M0 600L320 250 650 550 920 150 1200 450V800H0Z" fill="#6974b8"/></svg>') // picture（内置测试图片）。

window.__UI_LAYOUT__ = {
  ready: false, // ready（夹具挂载完成）。
  blocked, // blocked（被禁止的外部操作）。
  async show(value) { // show（切换只读验收场景）。
    resource.pendingDownloadBundlePlan.value = null
    resource.pendingDownloadPlan.value = null
    resource.pendingResetPlan.value = null
    resource.pendingImport.value = null
    lanVersionSync.open = false
    scene.value = value
    if (value === 'resource') resource.pendingDownloadBundlePlan.value = {
      title: '完整资源分类',
      entries: Array.from({ length: 30 }, (_, index) => ({ id: `resource-${index}`, label: `资源 ${index + 1}`, state: 'missing', status: '等待确认' })),
      plans: Array.from({ length: 30 }, (_, index) => ({ resource_id: `resource-${index}`, title: `资源 ${index + 1}`, model_bytes: 1024, environment: null })),
    }
    if (value === 'sync') Object.assign(lanVersionSync, {
      open: true, busy: false, error: '',
      check: { host_id: 'fixture', local_version: '1.0.0', remote_version: '1.1.0', comparison: 'local_older', can_sync: true, fields: {} },
    })
    await nextTick()
  },
}

createApp({
  setup() {
    provideProductContext(useHotwordSettings(() => {}))
    return () => h('main', { class: 'layout-fixture' }, [
      scene.value === 'history' && h(AppUpdateCard, { status: { state: 'idle', current_version: '1.0.0', fields: {} }, currentVersion: '1.0.0' }),
      scene.value === 'resource' && h(ResourceInstallDialog, { controller: resource }),
      scene.value === 'hotword' && h(HotwordSettings),
      scene.value === 'workspace' && h(WorkspaceSettings, { controller: workspace, storagePort }),
      scene.value === 'sync' && h(LanVersionDialog),
      scene.value === 'image' && h(CaptureImages, { attachments: [{ attachment_id: 'fixture', conversation_id: 'fixture', role: 'global', mime_type: 'image/png', width: 1200, height: 800, bytes: 100, sha256: '', preview_url: picture }] }),
      scene.value === 'long' && h(UiDialog, { open: true, title: '长说明与确认', onClose: () => { scene.value = 'history' } }, {
        default: () => Array.from({ length: 60 }, (_, index) => h('p', `说明 ${index + 1}：长路径与中文内容保持自然高度，超出部分通过内容区滚动查看。`)),
        actions: () => h('button', { type: 'button', onClick: () => { scene.value = 'history' } }, '关闭'),
      }),
    ])
  },
}).mount('#app')
window.__UI_LAYOUT__.ready = true
