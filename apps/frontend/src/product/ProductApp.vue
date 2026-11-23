<script setup lang="ts">
import TtsSettings from "./panels/TtsSettings.vue";
import OperationNotice from './OperationNotice.vue';
import { provideTtsVoiceState } from "../interface/useTtsVoiceState";
provideTtsVoiceState();
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useProductControls } from "./useProductControls";
import ApplicationStartup from "./ApplicationStartup.vue";
import ApplicationMotion from './ApplicationMotion.vue';
import { useApplicationHost } from './application-host';
import AppFrame from "../interface/AppFrame.vue";
import SettingsFrame from "../interface/SettingsFrame.vue";
import Workbench from "../interface/Workbench.vue";
import type { DesktopSettingsSection } from "../interface/settings";
import { desktopPort } from "./desktop-port";
import "./application.css";
import WakeSettings from "./panels/WakeSettings.vue";
import HotwordSettings from "./panels/HotwordSettings.vue";
import PersonaSettings from "./panels/PersonaSettings.vue";
import SpeakerSettings from "./panels/SpeakerSettings.vue";
import WakeTasksSettings from "./panels/WakeTasksSettings.vue";
import ComputeSettings from "./panels/ComputeSettings.vue";
import WorkspaceSettings from "./panels/WorkspaceSettings.vue";
import DeviceSettings from "../management/DeviceSettings.vue";
import LanVersionDialog from '../interface/LanVersionDialog.vue'
import { provideProductContext } from "./product-context";
import { useWorkspaceResources } from './useWorkspaceResources'
import { startRemoteClientRuntimeSupervisor } from './remote-client-runtime'
import {
  FLOATING_MODE_EVENT, FLOATING_RESULT_EVENT, type FloatingDisplayMode, type FloatingDisplayReceipt,
} from '../interface/floating/floating-display'
import { FLOATING_WINDOW_CONTROL_EVENT, FLOATING_WINDOW_ERROR_EVENT, FLOATING_WINDOW_WORKBENCH_EVENT,
  type FloatingSessionAction, type FloatingWindowProjection } from '../interface/floating/floating-window'

const controls = useProductControls();
const application = useApplicationHost();
provideProductContext(controls);
const workspaceResources = useWorkspaceResources();
const {
  applicationSnapshot,
  showApplicationSystem,
  activePage,
  activeSettingsTab,
  settings,
  runtimeState,
  dsh,
  openDshConsole,
  lastError,
  notice,
  noticeVisible,
  runtimeAction,
  resetSession,
  clearSession,
  setReasoningMode,
  setNotice,
  workbenchModel,
  decideDshApproval,
  floatingModel,
  floatingSessionAction,
} = controls;
const desktop = desktopPort();
const expandAnifLiveTtsCard = ref(false) // expandAnifLiveTtsCard（从音色设置跳转时展开本地合成卡片）。
const operations = computed(() => [
  ...(controls.voice.runtimeOperations?.value ?? []),
  ...(applicationSnapshot.value?.runtime_operations ?? []),
  ...(application.runtimeOperations?.value ?? []),
]);
const noticeOperations = computed(() => operations.value.filter(operation =>
  !['reasoning_mode', 'session'].includes(operation.target))); // noticeOperations（通用操作提示列表）：排除主界面已显示状态的运行模式切换、会话重置与清除。
const floatingUnlisteners: Array<() => void> = [];
let disposed = false;
function subscribeFloating<T>(name: string, handler: (payload: T) => void) {
  void desktop.listen?.<T>(name, event => { if (!disposed) handler(event.payload); })
    .then(unlisten => { if (disposed) unlisten(); else floatingUnlisteners.push(unlisten); }).catch(() => undefined);
}
if (desktop.available && desktop.listen) {
  subscribeFloating<string>(FLOATING_WINDOW_ERROR_EVENT, value => { lastError.value = value; });
  subscribeFloating<FloatingSessionAction>(FLOATING_WINDOW_CONTROL_EVENT, action => {
    if (action === 'silence' || action === 'start') floatingSessionAction(action);
  });
  subscribeFloating(FLOATING_WINDOW_WORKBENCH_EVENT, () => { activePage.value = 'home'; });
}
// 主工作台持有唯一语音实时连接，子窗口只接收原生宿主的展示请求。
const displayMode = ref<FloatingDisplayMode | null>(null)
if (desktop.available && desktop.floatingWindow && desktop.listen) {
  const invokeWindow = desktop.floatingWindow
  const voice = controls.voice
  let invalidating: Promise<FloatingDisplayMode> | undefined
  function acceptDisplayMode(value: FloatingDisplayMode) {
    if (!disposed && value.generation >= (displayMode.value?.generation ?? 0)
      && (value.generation !== displayMode.value?.generation || value.enabled !== displayMode.value?.enabled)) {
      displayMode.value = value
    }
  }
  void desktop.listen<FloatingDisplayMode>(FLOATING_MODE_EVENT, event => acceptDisplayMode(event.payload))
    .then(async unlisten => {
      if (disposed) { unlisten(); return }
      floatingUnlisteners.push(unlisten)
      acceptDisplayMode(await invokeWindow<FloatingDisplayMode>({ kind: 'mode_status' }))
    }).catch(() => undefined)
  subscribeFloating<FloatingDisplayReceipt>(FLOATING_RESULT_EVENT, receipt => {
    if (displayMode.value?.generation === receipt.generation) voice.reportFloatingDisplay(receipt)
  })
  watch([displayMode, voice.connection, () => applicationSnapshot.value?.remote_mode], async ([, connection, remote], previous) => {
    const lostConnection = previous?.[1] === 'connected' && connection !== 'connected'
    const switchedToRemote = remote === true && previous?.[2] !== true
    if ((lostConnection || switchedToRemote) && displayMode.value?.enabled && !invalidating) {
      const pending = invokeWindow<FloatingDisplayMode>({ kind: 'invalidate_display' })
      invalidating = pending
      try { acceptDisplayMode(await pending) } catch { /* 宿主正在退出。 */ }
      finally { if (invalidating === pending) invalidating = undefined }
    }
    if (remote || connection !== 'connected') return
    if (invalidating) {
      try { await invalidating } catch { return }
    }
    if (!disposed && voice.connection.value === 'connected' && displayMode.value && !applicationSnapshot.value?.remote_mode) {
      voice.setFloatingDisplayMode(displayMode.value)
    }
  }, { immediate: true, flush: 'sync' })
  watch(voice.floatingDisplayDelivery, delivery => {
    if (!delivery || !displayMode.value?.enabled || displayMode.value.generation !== delivery.generation
      || applicationSnapshot.value?.remote_mode) return
    void invokeWindow({ kind: 'submit', ...delivery }).catch(() => {
      if (displayMode.value?.generation === delivery.generation) {
        voice.reportFloatingDisplay({ generation: delivery.generation, delivery: delivery.delivery,
          status: 'rejected', reason: '展示窗口暂未受理本次投递。' })
      }
    })
  }, { flush: 'sync' })
}
onBeforeUnmount(() => { disposed = true; floatingUnlisteners.forEach(unlisten => unlisten()); });
let stopRemoteClientRuntime: (() => void) | null = null
watch(() => applicationSnapshot.value?.remote_mode === true, remote => {
  if (remote && stopRemoteClientRuntime === null) {
    stopRemoteClientRuntime = startRemoteClientRuntimeSupervisor()
  } else if (!remote && stopRemoteClientRuntime !== null) {
    stopRemoteClientRuntime()
    stopRemoteClientRuntime = null
  }
}, { immediate: true })
onBeforeUnmount(() => { stopRemoteClientRuntime?.(); stopRemoteClientRuntime = null })
// floatingModel（悬浮窗投影）：语音快照包含字幕，仍由主页面独占真实会话。
let floatingLatestModel: FloatingWindowProjection | null = null;
let floatingPublishTimer: ReturnType<typeof setTimeout> | undefined;
let floatingPublishing = false;
let floatingPublishAt = 0;
let floatingPublishFailed = false;
let lastPublishedFloatingModel: FloatingWindowProjection | null = null;
const floatingWatchSources = [
  () => floatingModel.value.state, () => floatingModel.value.sessionActive,
  () => floatingModel.value.silenced, () => floatingModel.value.canControl,
  () => floatingModel.value.caption,
  ...Array.from({ length: 5 }, (_, index) => () => floatingModel.value.audioLevels[index] ?? 0),
];
function publishFloatingModel() {
  const latest = floatingLatestModel;
  if (disposed || !latest || latest === lastPublishedFloatingModel || !desktop.publishFloatingModel || floatingPublishing) return;
  const now = performance.now();
  if (now < floatingPublishAt) {
    scheduleFloatingPublish(floatingPublishAt - now);
    return;
  }
  if (floatingPublishTimer !== undefined) {
    clearTimeout(floatingPublishTimer);
    floatingPublishTimer = undefined;
  }
  floatingPublishAt = now + 34; // 音量由实时事件主动更新，静止时用计时器补送最后一帧。
  floatingPublishing = true;
  void desktop.publishFloatingModel(latest).then(() => {
    lastPublishedFloatingModel = latest;
    floatingPublishFailed = false;
  }, (error: unknown) => {
    if (!floatingPublishFailed) lastError.value = error instanceof Error ? error.message : '悬浮窗状态同步失败。';
    floatingPublishFailed = true;
    floatingPublishAt = performance.now() + 1000;
  }).finally(() => {
    floatingPublishing = false;
    publishFloatingModel();
  });
}
function scheduleFloatingPublish(delay: number) {
  if (floatingPublishTimer !== undefined) return;
  floatingPublishTimer = setTimeout(() => {
    floatingPublishTimer = undefined;
    publishFloatingModel();
  }, delay);
}
if (desktop.available && desktop.publishFloatingModel) {
  watch(floatingWatchSources, () => {
    floatingLatestModel = { ...floatingModel.value, audioLevels: [...floatingModel.value.audioLevels] };
    publishFloatingModel();
  }, { immediate: true, flush: 'post' });
  onBeforeUnmount(() => {
    if (floatingPublishTimer !== undefined) clearTimeout(floatingPublishTimer);
  });
}
// resourceMaintenance（资源维护）：服务停机不等于应用退出，组件操作和下载进度继续可用。
const resourceMaintenance = computed(() => workspaceResources.startingResources.value.size > 0
  || application.recovery?.value?.suspended || application.runtimeOperations?.value.some(row => row.state === 'running')
  || workspaceResources.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state)));
// unavailableCapabilityLabel（不可用能力文案）：运行期故障用横幅提示，并用组件页单独恢复。
const unavailableCapabilityLabel = computed(() => {
  const rows = (applicationSnapshot.value?.unavailable_capabilities ?? [])
    .filter(row => !['READY', 'RUNNING', 'STREAMING', 'CONNECTED', 'DISABLED'].includes(row.state));
  return rows.length ? `：${rows.map(row => row.label).join('、')}` : '';
});
function nativeAction(action: () => Promise<void>) {
  void action().catch((error) => {
    lastError.value = String(error);
  });
}
function openAnifLiveComponentCard() {
  expandAnifLiveTtsCard.value = true
  activeSettingsTab.value = 'compute'
}
function setSettingsTab(section: DesktopSettingsSection) {
  activeSettingsTab.value = section
  if (section !== 'compute') expandAnifLiveTtsCard.value = false
}
function exportLogs() {
  void desktop.exportLogs().then(path => {
    if (path) setNotice('日志压缩包已保存到所选文件夹。');
  }).catch(error => {
    lastError.value = String(error);
  });
}
</script>
<template>
  <!-- 桌面应用退出只接受原生退出事件，后台服务停止不能触发不可恢复的退出动画。 -->
  <ApplicationMotion v-slot="{ entering, exiting }" :blocked="application.overlay.value && !resourceMaintenance" :stopping="!desktop.available && !resourceMaintenance && Boolean(applicationSnapshot?.stopping || ['stopping', 'stopped'].includes(applicationSnapshot?.state ?? ''))">
  <AppFrame
    :runtime="runtimeState"
  >
    <template #banner>
      <div
        v-if="!applicationSnapshot?.remote_mode && (applicationSnapshot?.state === 'degraded' || application.runtimeFailure.value)"
        class="application-degraded"
        role="status"
      >
        <span>{{
          application.runtimeFailure.value
            ? `语音能力暂不可用${unavailableCapabilityLabel}，可在本地组件页单独恢复。`
            : "部分组件暂不可用，核心语音功能仍可使用。"
        }}</span><button @click="showApplicationSystem">查看本地组件</button>
      </div>
    </template>
    <Workbench
      v-show="activePage === 'home'"
      :model="workbenchModel"
      :remote-mode="applicationSnapshot?.remote_mode === true"
      :console-enabled="dsh.state === 'ready'"
      :console-label="dsh.state === 'starting' ? '代理启动中' : '代理控制台'"
      @settings="activePage = 'settings'"
      @console="openDshConsole"
      @action="runtimeAction"
      @reset="resetSession"
      @clear="clearSession"
      @mode="setReasoningMode"
      @approval="decideDshApproval"
    />
    <SettingsFrame
      v-show="activePage === 'settings'"
      :section="activeSettingsTab"
      :desktop="desktop.available"
      @back="activePage = 'home'"
      @logs="exportLogs"
      @exit="nativeAction(desktop.exit)"
      @section="setSettingsTab"
    >
      <template #warnings
        ><div v-if="settings.warnings.length" class="model-safety">
          <span>!</span>
          <p>{{ settings.warnings.join(" ") }}</p>
        </div></template
      >
      <article class="settings-panel panel" :class="`settings-panel-${activeSettingsTab}`">
        <WakeSettings v-if="activeSettingsTab === 'wake'" />
        <PersonaSettings v-if="activeSettingsTab === 'persona'" />
        <TtsSettings v-if="activeSettingsTab === 'tts'" @open-aniflive-components="openAnifLiveComponentCard" />
        <SpeakerSettings v-if="activeSettingsTab === 'speaker'" />
        <HotwordSettings v-if="activeSettingsTab === 'speaker'" />
        <WakeTasksSettings v-if="activeSettingsTab === 'wakes'" />
        <ComputeSettings v-if="activeSettingsTab === 'compute' || activeSettingsTab === 'system'" :resource-controller="workspaceResources" :operations="operations" :expand-anif-live-tts="expandAnifLiveTtsCard" />
        <WorkspaceSettings v-if="activeSettingsTab === 'workspace'" :controller="workspaceResources" @open-settings="setSettingsTab" />
        <DeviceSettings v-if="activeSettingsTab === 'devices'" @notice="setNotice" /></article
    ></SettingsFrame>
    <template #overlays>
      <LanVersionDialog />
      <div class="notification-stack">
      <OperationNotice :operations="noticeOperations" :retryable="Boolean(application.recovery?.value?.suspended && application.recovery?.value?.operation?.state === 'failed')" @retry="nativeAction(async () => { await application.recover?.() })" />
      <Transition name="save-notice"
        ><div v-if="lastError" class="save-notice error-notice" role="alert">
          <span>!</span>{{ lastError
          }}<button aria-label="关闭错误" @click="lastError = ''">×</button>
        </div></Transition
      >
      <Transition name="save-notice"
        ><div
          v-if="notice"
          class="save-notice"
          :class="{ 'is-fading': !noticeVisible }"
          role="status"
        >
          <span>✓</span>{{ notice
          }}<button aria-label="关闭提示" @click="setNotice('')">×</button>
        </div></Transition
      >
      </div>
      <ApplicationStartup v-if="!resourceMaintenance || entering || exiting" :entering="entering" :exiting="exiting" /></template>
  </AppFrame>
  </ApplicationMotion>
</template>
