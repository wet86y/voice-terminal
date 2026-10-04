<script setup lang="ts">
import { useProductControls } from "./useProductControls";
import ApplicationStartup from "./ApplicationStartup.vue";
import AppFrame from "../interface/AppFrame.vue";
import SettingsFrame from "../interface/SettingsFrame.vue";
import Workbench from "../interface/Workbench.vue";
import UiDialog from "../interface/UiDialog.vue";
import UiButton from "../interface/UiButton.vue";
import { desktopPort } from "./desktop-port";
import "./application.css";
import WakeSettings from "./panels/WakeSettings.vue";
import PersonaSettings from "./panels/PersonaSettings.vue";
import SpeakerSettings from "./panels/SpeakerSettings.vue";
import WakeTasksSettings from "./panels/WakeTasksSettings.vue";
import SystemSettings from "./panels/SystemSettings.vue";
import ComputeSettings from "./panels/ComputeSettings.vue";
import ModelSettings from "./panels/ModelSettings.vue";
import { provideProductContext } from "./product-context";

const controls = useProductControls();
provideProductContext(controls);
const {
  applicationSnapshot,
  showApplicationSystem,
  activePage,
  activeSettingsTab,
  settings,
  connected,
  replayMode,
  runtimeState,
  shadowExpanded,
  restartState,
  applyImpact,
  dsh,
  openDshConsole,
  lastError,
  notice,
  noticeVisible,
  runtimeAction,
  resetSession,
  setReasoningMode,
  setNotice,
  showApplyConfirm,
  confirmApply,
  workbenchModel,
} = controls;
const desktop = desktopPort();
function nativeAction(action: () => Promise<void>) {
  void action().catch((error) => {
    lastError.value = String(error);
  });
}
</script>
<template>
  <AppFrame
    :page="activePage"
    :runtime="runtimeState"
    :status="
      replayMode ? '日志回放 · 只读' : connected ? '本机已连接' : '等待连接'
    "
    :console-enabled="dsh.state === 'ready'"
    :console-label="dsh.state === 'starting' ? '代理启动中' : '代理控制台'"
    :desktop="desktop.available"
    @page="activePage = $event"
    @console="openDshConsole"
    @logs="nativeAction(desktop.openLogs)"
    @exit="nativeAction(desktop.exit)"
  >
    <template #banner
      ><div
        v-if="applicationSnapshot?.state === 'degraded'"
        class="application-degraded"
        role="status"
      >
        <span>部分组件暂不可用，核心语音功能仍可使用。</span
        ><button @click="showApplicationSystem">查看系统运行</button>
      </div></template
    >
    <Workbench
      v-show="activePage === 'home'"
      :model="workbenchModel"
      @action="runtimeAction"
      @reset="resetSession"
      @mode="setReasoningMode"
      @expand="shadowExpanded = !shadowExpanded"
      @models="
        activePage = 'settings';
        activeSettingsTab = 'models';
      "
    />
    <SettingsFrame
      v-show="activePage === 'settings'"
      :section="activeSettingsTab"
      :status="connected ? '已连接' : '离线 · 可重试'"
      @section="activeSettingsTab = $event"
    >
      <template #warnings
        ><div v-if="settings.warnings.length" class="model-safety">
          <span>!</span>
          <p>{{ settings.warnings.join(" ") }}</p>
        </div></template
      >
      <article class="settings-panel panel">
        <WakeSettings v-if="activeSettingsTab === 'wake'" />
        <PersonaSettings v-if="activeSettingsTab === 'persona'" />
        <SpeakerSettings v-if="activeSettingsTab === 'speaker'" />
        <WakeTasksSettings v-if="activeSettingsTab === 'wakes'" />
        <SystemSettings v-if="activeSettingsTab === 'system'" />
        <ComputeSettings v-if="activeSettingsTab === 'compute'" />
        <ModelSettings v-if="activeSettingsTab === 'models'" /></article
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
      <UiDialog
        :open="showApplyConfirm"
        title="应用新的模型配置？"
        :busy="restartState === 'restarting'"
        @close="showApplyConfirm = false"
        ><p>
          将结束当前语音交互并重启受影响的服务。重启成功后，新配置才会生效。
        </p>
        <ul>
          <li v-for="item in applyImpact" :key="item">{{ item }}</li>
        </ul>
        <template #actions
          ><UiButton variant="quiet" :disabled="restartState === 'restarting'" @click="showApplyConfirm = false"
            >暂不应用</UiButton
          ><UiButton :disabled="restartState === 'restarting'" @click="confirmApply">确认并应用</UiButton></template
        ></UiDialog
      >
      <ApplicationStartup @system="showApplicationSystem" @models="activePage = 'settings'; activeSettingsTab = 'models'"
    /></template>
  </AppFrame>
</template>
