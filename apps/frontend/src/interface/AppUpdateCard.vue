<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AppUpdateStatus } from './app-update'
import UiDialog from './UiDialog.vue'
import ReleaseNotes from './ReleaseNotes.vue'
import { releaseHistory } from './release-notes'

const props = withDefaults(defineProps<{
  /** status（原生更新状态）。 */ status: AppUpdateStatus
  /** currentVersion（工作区当前版本）。 */ currentVersion?: string
  /** disabled（工作区操作或管理权限限制）。 */ disabled?: boolean
  /** integrity（当前本地版本完整性维护状态）。 */ integrity?: AppUpdateStatus
}>(), { currentVersion: '', disabled: false })
const emit = defineEmits<{
  /** check（检查发行版）、download（下载更新）、cancel（取消下载）。 */
  check: []
  download: []
  cancel: []
  downloadIntegrity: [] // downloadIntegrity（下载匹配本地构建的校验程序）。
  checkIntegrity: [] // checkIntegrity（关闭程序并开始完整性维护）。
  /** repository（在系统浏览器打开项目仓库）。 */ repository: []
}>()
const historyOpen = ref(false) // historyOpen（更新日志弹窗是否打开）。
const expandedVersions = ref(new Set<string>()) // expandedVersions（已展开的版本）。
const history = computed(() => releaseHistory(props.status.target_version, props.status.release_notes))
function toggleVersion(version: string) {
  const next = new Set(expandedVersions.value)
  if (next.has(version)) next.delete(version)
  else next.add(version)
  expandedVersions.value = next
}
const progress = computed(() => {
  const { completed_bytes: done, total_bytes: total } = props.status
  if (!total || total <= 0 || done == null) return undefined
  return Math.max(0, Math.min(100, Math.round(done / total * 100)))
})
const canDownload = computed(() => Boolean(props.status.target_version)
  && ['available', 'failed', 'cancelled'].includes(props.status.state))
const actionLabel = computed(() => {
  if (props.status.state === 'checking') return '正在检查…'
  if (props.status.state === 'downloading') return '取消下载'
  if (props.status.state === 'handing_off') return '正在启动安装器…'
  if (canDownload.value) return props.status.state === 'available' ? '下载并安装' : '重新下载并安装'
  return props.status.state === 'current' ? '重新检查' : '检查更新'
})
const actionDisabled = computed(() => props.disabled
  || integrityBusy.value
  || ['checking', 'handing_off'].includes(props.status.state)
  || (props.status.state === 'downloading' && !props.status.can_cancel))
const integrityBusy = computed(() => ['checking', 'downloading', 'handing_off'].includes(props.integrity?.state ?? 'idle'))
const integrityDisabled = computed(() => props.disabled || ['checking', 'handing_off'].includes(props.integrity?.state ?? '')
  || ['checking', 'downloading', 'handing_off'].includes(props.status.state)
  || (props.integrity?.state === 'downloading' && !props.integrity.can_cancel))
const integrityLabel = computed(() => props.integrity?.state === 'downloading' ? '取消下载校验程序'
  : props.integrity?.state === 'checking' ? '正在准备校验程序…'
    : props.integrity?.state === 'handing_off' ? '正在启动完整性校验…'
      : props.integrity?.installer_ready ? '校验完整性' : '下载校验程序')
function runIntegrity() {
  if (integrityDisabled.value) return
  if (props.integrity?.state === 'downloading') emit('cancel')
  else if (props.integrity?.installer_ready) emit('checkIntegrity')
  else emit('downloadIntegrity')
}
const stateLabel = computed(() => ({
  idle: '保持应用为最新版本', checking: '正在检查更新', current: '已是最新版本', available: '发现新版本',
  downloading: '正在下载更新', handing_off: '正在准备安装', cancelled: '下载已取消', failed: '更新未完成',
})[props.status.state])
const stateDetail = computed(() => {
  if (props.status.state === 'downloading') return props.status.phase || '正在下载并校验安装器…'
  if (props.status.state === 'handing_off') return '安装器已校验，正在关闭当前程序并启动更新。'
  if (props.status.state === 'cancelled') return '可以随时重新开始下载。'
  if (props.status.state === 'current') return '后续有新版本时，可以在这里检查并安装。'
  if (props.status.state === 'failed') return '请检查错误信息后重试。'
  if (props.status.state === 'checking') return '正在读取官方发行信息…'
  return '更新将保留你的设置、数据和已安装组件。'
})
function runAction() {
  if (actionDisabled.value) return
  if (props.status.state === 'downloading') emit('cancel')
  else if (canDownload.value) emit('download')
  else emit('check')
}
function formatBytes(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB（吉字节）`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB（兆字节）`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB（千字节）`
  return `${Math.round(value)} B（字节）`
}
</script>

<template>
  <section class="app-update-card" :class="{ 'update-failed': status.state === 'failed' }" aria-labelledby="app-update-title">
    <header class="update-heading">
      <div class="update-title"><span class="update-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4" /></svg></span><div><p class="section-label">UPDATE（更新）</p><h3 id="app-update-title">应用更新</h3></div></div>
      <span v-if="status.target_version" class="update-version-tag">新版 v{{ status.target_version }}</span>
    </header>
    <div class="update-summary">
      <div class="update-copy"><strong role="status" aria-live="polite">{{ stateLabel }}</strong><p>当前版本 v{{ currentVersion || status.current_version || '—' }}</p><p class="update-description">{{ stateDetail }}</p></div>
      <div class="update-actions"><button class="update-action" :class="canDownload ? 'save-button' : 'outline-button'" type="button" :disabled="actionDisabled" @click="runAction">{{ actionLabel }}</button>
      <button class="outline-button integrity-action" type="button" :disabled="integrityDisabled" @click="runIntegrity">{{ integrityLabel }}</button></div>
    </div>
    <div v-if="integrityBusy || integrity?.error" class="integrity-detail" aria-live="polite">
      <p v-if="integrityBusy && integrity?.phase">{{ integrity.phase }}</p>
      <p v-if="integrity?.state === 'downloading'">{{ formatBytes(integrity.completed_bytes ?? 0) }} / {{ formatBytes(integrity.total_bytes) }}</p>
      <p v-if="integrity?.error" class="update-error" role="alert">{{ integrity.error }}</p>
    </div>
    <div v-if="status.state === 'downloading'" class="update-download">
      <div class="update-progress" :class="{ indeterminate: progress === undefined }" role="progressbar" aria-label="安装器下载进度" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="progress"><span :style="progress === undefined ? undefined : { width: `${progress}%` }"></span></div>
      <div class="update-progress-caption"><span>{{ formatBytes(status.completed_bytes ?? 0) }} / {{ formatBytes(status.total_bytes) }}</span><strong>{{ progress === undefined ? '准备下载' : `${progress}%` }}</strong></div>
    </div>
    <p v-if="status.error" class="update-error" role="alert">{{ status.error }}</p>
    <section v-if="status.release_notes" class="update-release" aria-labelledby="update-release-title">
      <div class="update-release-heading"><h4 id="update-release-title">本次更新</h4><small v-if="status.total_bytes">安装包 {{ formatBytes(status.total_bytes) }}</small></div>
      <div class="update-release-notes" tabindex="0" role="region" aria-label="发行说明"><ReleaseNotes :notes="status.release_notes" /></div>
    </section>
    <footer class="update-about"><p>作者 <strong>wet86y</strong></p><div class="update-links"><button class="outline-button" type="button" aria-haspopup="dialog" :aria-expanded="historyOpen" @click="historyOpen = true">更新日志</button><button class="outline-button update-repository" type="button" @click="emit('repository')">GitHub主页</button></div></footer>
    <UiDialog :open="historyOpen" title="更新日志" kicker="版本记录" wide @close="historyOpen = false">
      <div class="release-history" role="region" aria-label="往期更新日志">
        <article v-for="entry in history" :key="entry.version" class="release-history-entry">
          <button class="release-history-toggle" type="button" :aria-expanded="expandedVersions.has(entry.version)" :aria-controls="`release-version-${entry.version}`" @click="toggleVersion(entry.version)">
            <span class="release-history-version">v{{ entry.version }}<small v-if="entry.version === (currentVersion || status.current_version)">当前版本</small></span>
            <span class="release-history-summary">{{ entry.summary }}</span><span class="release-history-chevron" aria-hidden="true">{{ expandedVersions.has(entry.version) ? '−' : '+' }}</span>
          </button>
          <div v-if="expandedVersions.has(entry.version)" :id="`release-version-${entry.version}`" class="release-history-body"><ReleaseNotes :notes="entry.notes" /></div>
        </article>
      </div>
      <template #actions><button class="outline-button" type="button" @click="historyOpen = false">关闭</button></template>
    </UiDialog>
  </section>
</template>

<style scoped>
.app-update-card{display:grid;gap:18px;min-width:0;padding:20px;border:1px solid var(--line);border-radius:14px;background:#ffffff02}
.update-heading,.update-title,.update-release-heading{display:flex;align-items:center;gap:12px;min-width:0}
.update-heading,.update-release-heading{justify-content:space-between;flex-wrap:wrap}
.update-title h3{margin-top:3px;font-size:var(--font-section);color:var(--text)}
.update-icon{display:grid;place-items:center;flex:none;width:36px;height:36px;border-radius:11px;background:#b2baff12;color:var(--accent)}
.update-icon svg{width:21px;height:21px}
.update-version-tag{flex:none;max-width:100%;padding:5px 10px;border:1px solid #b2baff30;border-radius:999px;background:#b2baff0a;color:var(--accent);font-size:var(--font-caption);overflow-wrap:anywhere}
.update-summary{display:flex;align-items:center;gap:18px;flex-wrap:wrap;min-width:0}
.update-actions,.update-links{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap;max-width:100%;margin-left:auto}
.update-actions>button,.update-links>button{display:inline-flex;align-items:center;justify-content:center;flex:none;max-width:100%;min-height:36px;padding:8px 14px;border-radius:10px;font-size:var(--font-body);line-height:1.4;text-align:center;white-space:nowrap}
.integrity-detail{font-size:var(--font-caption);color:var(--muted);line-height:1.65;overflow-wrap:anywhere}
.update-copy{display:grid;gap:5px;flex:1 1 220px;min-width:0;overflow-wrap:anywhere}
.update-copy strong{font-size:var(--font-section);font-weight:600;color:var(--text)}
.update-copy p{font-size:var(--font-body);color:var(--muted);line-height:1.65}
.update-copy .update-description{font-size:var(--font-caption)}
.update-download{display:grid;gap:8px}
.update-progress{height:6px;overflow:hidden;border-radius:999px;background:var(--line-strong)}
.update-progress span{display:block;height:100%;border-radius:inherit;background:var(--accent);transition:width 150ms ease}
.update-progress.indeterminate span{width:35%;opacity:.65}
.update-progress-caption{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:var(--font-caption);color:var(--muted);font-variant-numeric:tabular-nums}
.update-progress-caption strong{color:var(--accent);font-weight:500}
.update-release{display:grid;gap:12px;min-width:0;padding-top:16px;border-top:1px solid var(--line)}
.update-release-heading h4{font-size:var(--font-body);font-weight:600;color:var(--text)}
.update-release-heading small{font-size:var(--font-caption);color:var(--subtle)}
.update-release-notes{max-height:280px;overflow:auto;padding-right:8px}
.update-error{margin:0;padding:10px 12px;border-radius:9px;background:#efa0b10a;color:var(--danger);font-size:var(--font-body);line-height:1.6;overflow-wrap:anywhere;white-space:pre-wrap}
.update-about{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding-top:14px;border-top:1px solid var(--line);font-size:var(--font-body);color:var(--muted)}
.update-about strong{margin-left:6px;font-weight:500;color:var(--text)}
.release-history{display:grid;grid-auto-rows:max-content;gap:10px;min-width:0}
.release-history-entry{min-width:0;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.release-history-toggle{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 16px;width:100%;padding:16px 18px;border:0;border-radius:0;text-align:left;background:var(--panel-soft);color:var(--text)}
.release-history-version{display:flex;align-items:center;gap:10px;font-size:var(--font-section);font-weight:600}
.release-history-version small{padding:2px 7px;border-radius:6px;background:#b2baff12;color:var(--accent);font-size:var(--font-caption);font-weight:400}
.release-history-summary{grid-column:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:var(--font-body);font-weight:400;line-height:1.6}
.release-history-chevron{grid-column:2;grid-row:1/3;align-self:center;font-size:20px;color:var(--muted)}
.release-history-body{padding:18px;border-top:1px solid var(--line)}
.update-failed{border-color:color-mix(in srgb,var(--danger) 28%,transparent)}
@media(max-width:560px){.app-update-card{padding:16px;gap:16px}.update-summary{gap:12px}.update-release-heading{gap:6px}}
</style>
