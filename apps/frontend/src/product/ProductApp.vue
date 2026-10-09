<script setup lang="ts">
import { computed } from 'vue';
import { useProductControls } from "./useProductControls";
import ApplicationStartup from "./ApplicationStartup.vue";
import ApplicationMotion from './ApplicationMotion.vue';
import { useApplicationHost } from './application-host';
import AppFrame from "../interface/AppFrame.vue";
import SettingsFrame from "../interface/SettingsFrame.vue";
import Workbench from "../interface/Workbench.vue";
import { desktopPort } from "./desktop-port";
import "./application.css";
import WakeSettings from "./panels/WakeSettings.vue";
import PersonaSettings from "./panels/PersonaSettings.vue";
import SpeakerSettings from "./panels/SpeakerSettings.vue";
import WakeTasksSettings from "./panels/WakeTasksSettings.vue";
import ComputeSettings from "./panels/ComputeSettings.vue";
import WorkspaceSettings from "./panels/WorkspaceSettings.vue";
import ModelSettings from "./panels/ModelSettings.vue";
import { provideProductContext } from "./product-context";
import { useWorkspaceResources } from './useWorkspaceResources'

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
} = controls;
const desktop = desktopPort();
// resourceMaintenance（资源维护）：服务停机不等于应用退出，组件操作和下载进度继续可用。
const resourceMaintenance = computed(() => workspaceResources.startingResources.value.size > 0
  || workspaceResources.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state)));
function nativeAction(action: () => Promise<void>) {
  void action().catch((error) => {
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
    <template #banner
      ><div
        v-if="applicationSnapshot?.state === 'degraded'"
        class="application-degraded"
        role="status"
      >
        <span>部分组件暂不可用，核心语音功能仍可使用。</span
        ><button @click="showApplicationSystem">查看本地组件</button>
      </div></template
    >
    <Workbench
      v-show="activePage === 'home'"
      :model="workbenchModel"
      :console-enabled="dsh.state === 'ready'"
      :console-label="dsh.state === 'starting' ? '代理启动中' : '代理控制台'"
      @settings="activePage = 'settings'"
      @console="openDshConsole"
      @action="runtimeAction"
      @reset="resetSession"
      @clear="clearSession"
      @mode="setReasoningMode"
    />
    <SettingsFrame
      v-show="activePage === 'settings'"
      :section="activeSettingsTab"
      :desktop="desktop.available"
      @back="activePage = 'home'"
      @logs="nativeAction(desktop.openLogs)"
      @exit="nativeAction(desktop.exit)"
      @section="activeSettingsTab = $event"
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
        <SpeakerSettings v-if="activeSettingsTab === 'speaker'" />
        <WakeTasksSettings v-if="activeSettingsTab === 'wakes'" />
        <ComputeSettings v-if="activeSettingsTab === 'compute' || activeSettingsTab === 'system'" :resource-controller="workspaceResources" />
        <ModelSettings v-if="activeSettingsTab === 'models'" />
        <WorkspaceSettings v-if="activeSettingsTab === 'workspace'" :controller="workspaceResources" /></article
    ></SettingsFrame>
    <template #overlays>
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
      <ApplicationStartup v-if="!resourceMaintenance || entering || exiting" :entering="entering" :exiting="exiting" /></template>
  </AppFrame>
  </ApplicationMotion>
</template>
