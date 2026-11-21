<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount } from 'vue'
import UiDialog from './UiDialog.vue'
import { desktopPort, APP_UPDATE_EVENT } from '../product/desktop-port'
import type { AppUpdateStatus } from './app-update'
import { lanVersionSync as sync, confirmLanVersionSync, cancelLanVersionSync, dismissLanVersionSync, updateLanVersionProgress } from '../product/lan-version-sync'
const target = computed(() => sync.check?.comparison === 'remote_older' ? '服务端' : '本机')
const version = computed(() => sync.check?.comparison === 'remote_older' ? sync.check?.local_version : sync.check?.remote_version)
const progress = computed(() => sync.totalBytes > 0 ? Math.min(100, Math.round(sync.completedBytes / sync.totalBytes * 100)) : undefined)
let disposed = false
let unlisten: (() => void) | undefined
onMounted(() => {
  void desktopPort().listen?.<AppUpdateStatus>(APP_UPDATE_EVENT, event => updateLanVersionProgress(event.payload))
    .then(stop => { if (disposed) stop(); else unlisten = stop }).catch(() => undefined)
})
onBeforeUnmount(() => { disposed = true; unlisten?.() })
</script>

<template>
  <UiDialog :open="sync.open" title="同步程序版本" kicker="局域网连接" :busy="sync.busy" @close="dismissLanVersionSync">
    <p>本机版本：{{ sync.check?.local_version }}；服务端版本：{{ sync.check?.remote_version }}。</p>
    <p>需要将{{ target }}更新到 {{ version }} 后继续连接。确认后将通过局域网传输分发器，{{ target }}会退出并在更新完成后重新启动。</p>
    <p v-if="sync.check?.reason && !sync.busy" role="status">{{ sync.check.reason }}</p>
    <div v-if="sync.busy" class="lan-version-progress" role="status" aria-live="polite">
      <p>{{ sync.phase }}</p>
      <progress aria-label="局域网版本同步进度" :value="progress" :max="100" />
      <p v-if="sync.totalBytes">{{ (sync.completedBytes / 1024 / 1024).toFixed(1) }} / {{ (sync.totalBytes / 1024 / 1024).toFixed(1) }} 兆字节</p>
    </div>
    <p v-if="sync.error" role="alert">{{ sync.error }}</p>
    <template #actions>
      <button v-if="!sync.busy" class="outline-button" type="button" @click="dismissLanVersionSync">暂不同步</button>
      <button v-if="sync.canCancel" class="outline-button" type="button" @click="cancelLanVersionSync">取消同步</button>
      <button v-if="!sync.busy" class="save-button" type="button" :disabled="!sync.check?.can_sync" @click="confirmLanVersionSync">{{ sync.error ? '重新同步并更新' : '同步并更新' }}</button>
    </template>
  </UiDialog>
</template>

<style scoped>
p{line-height:1.7;overflow-wrap:anywhere}.lan-version-progress{margin-top:16px}.lan-version-progress progress{width:100%;accent-color:var(--accent)}[role=alert]{color:var(--danger)}
</style>
