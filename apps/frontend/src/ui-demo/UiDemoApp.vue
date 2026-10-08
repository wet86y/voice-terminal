<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import AppFrame from "../interface/AppFrame.vue";
import SettingsFrame from "../interface/SettingsFrame.vue";
import Workbench from "../interface/Workbench.vue";
import UiDialog from "../interface/UiDialog.vue";
import UiButton from "../interface/UiButton.vue";
import type { WorkbenchModel } from "../interface/workbench";
import type { SessionOperation } from "../types";
import DemoSettings from "./DemoSettings.vue";
import {
  demoConversation,
  demoShadowActivities,
  demoSystemComponents,
  longIdentifier,
  longText,
  overlayChoices,
  previewRows,
  runtimePresentation,
  sceneChoices,
  settingsTabs,
  stateChoices,
  type DemoRuntimeState,
  type PreviewOverlay,
  type PreviewPage,
  type PreviewScene,
  type ReasoningMode,
} from "./demo-data";
import { voiceShadowReplayDemo } from './voice-shadow-replay'
import { useVoiceSessionDemo } from './useVoiceSessionDemo'

// URL（页面地址）只记录展示选择，方便刷新和热更新后继续查看同一画面。
const query = new URLSearchParams(window.location.search);
// replay（日志回放）仅在桌面样例入口读取，不会进入 ProductApp（产品界面）。
const replayMode = ref(query.get('replay') === 'voice-shadow')
const pageChoices = [{ id: "home" as const, label: "工作台" }, ...settingsTabs];
function validChoice<T extends string>(
  value: string | null,
  values: readonly T[],
  fallback: T,
): T {
  return values.includes(value as T) ? (value as T) : fallback;
}
// 旧 silence（静默页）与 system（系统页）地址继续打开对应合并页。
const requestedPage =
  query.get("page") === "silence" ? "wake" : query.get("page") === "system" ? "compute" : query.get("page");
const page = ref<PreviewPage>(
  validChoice(
    requestedPage,
    pageChoices.map((item) => item.id),
    "home",
  ),
);
const scene = ref<PreviewScene>(
  validChoice(
    query.get("scene"),
    sceneChoices.map((item) => item.value),
    "normal",
  ),
);
const runtimeState = ref<DemoRuntimeState>(
  validChoice(
    query.get("runtime"),
    stateChoices.map((item) => item.value),
    replayMode.value ? 'speaking' : 'standby',
  ),
);
const voiceSession = useVoiceSessionDemo(runtimeState, query.get('session') === 'active');
const overlay = ref<PreviewOverlay>(
  validChoice(
    query.get("overlay"),
    overlayChoices.map((item) => item.value),
    "none",
  ),
);
/** confirmPurpose（确认画面用途）：只决定文案，不执行退出或重启。 */
const confirmPurpose = ref<"apply" | "exit">("apply");
const toolsOpen = ref(false);
const reasoningMode = ref<ReasoningMode>("fast"); // reasoningMode（运行模式）：默认快速。
const feedback = ref("");
const feedbackError = ref(false);
const resetKey = ref(0);
const sessionOperation = ref<SessionOperation | null>(null); // sessionOperation（会话操作加载状态样例）。
let sessionOperationTimer: ReturnType<typeof setTimeout> | undefined; // sessionOperationTimer（样例完成计时器）。
function cancelSessionOperation() {
  if (sessionOperationTimer !== undefined) clearTimeout(sessionOperationTimer);
  sessionOperationTimer = undefined;
  sessionOperation.value = null;
}
function performSessionOperation(operation: SessionOperation) {
  if (sessionOperation.value || voiceSession.active.value || replayMode.value || scene.value === 'loading') return;
  sessionOperation.value = operation;
  sessionOperationTimer = setTimeout(() => {
    sessionOperationTimer = undefined;
    sessionOperation.value = null;
    notify(operation === 'reset' ? '上下文整理完成，会话已重置' : '旧会话已归档，全新会话已就绪');
  }, operation === 'reset' ? 1800 : 350);
}
// effectiveRuntimeState（有效运行状态）：错误场景优先，其次固定回放状态。
const effectiveRuntimeState = computed<DemoRuntimeState>(() =>
  scene.value === "error"
    ? "error"
    : replayMode.value
      ? "speaking"
      : runtimeState.value,
);
const currentRuntime = computed(
  () => runtimePresentation[effectiveRuntimeState.value],
);
const messages = computed(() => previewRows(replayMode.value ? voiceShadowReplayDemo.conversation : demoConversation, scene.value));
// replaySpeakingMessageId（当前回放回复标识）：长场景复制消息时仅高亮最后一条回复。
const replaySpeakingMessageId = computed(() =>
  [...messages.value].reverse().find(item => item.role === "assistant")?.id ?? null,
);
const activities = computed(() =>
  previewRows(
    replayMode.value
      ? voiceShadowReplayDemo.activities
      : demoShadowActivities.map((item, index) => ({ ...item, id: `activity-${index}` })),
    scene.value,
  ),
);
const showNotice = computed(
  () =>
    Boolean(feedback.value) || ["success", "failure"].includes(overlay.value),
);
const noticeError = computed(() =>
  feedback.value ? feedbackError.value : overlay.value === "failure",
);
const noticeText = computed(
  () =>
    feedback.value ||
    (overlay.value === "failure"
      ? "设置保存失败，请稍后重试。"
      : "设置已保存，将在下一次对话生效。"),
);
const layoutScene = computed(() =>
  scene.value === "empty"
    ? "暂无内容"
    : scene.value === "loading"
      ? "正在加载…"
      : "当前内容读取失败，可以重试。",
);

watch([page, scene, runtimeState, overlay, replayMode, voiceSession.active], () => {
  const url = new URL(window.location.href);
  url.searchParams.set("page", page.value);
  url.searchParams.set("scene", scene.value);
  url.searchParams.set("runtime", runtimeState.value);
  url.searchParams.set("overlay", overlay.value);
  if (voiceSession.active.value) url.searchParams.set('session', 'active');
  else url.searchParams.delete('session');
  if (replayMode.value) url.searchParams.set("replay", "voice-shadow");
  else url.searchParams.delete("replay");
  window.history.replaceState(null, "", url);
  feedback.value = "";
});
function notify(message: string) {
  feedbackError.value = scene.value === "error";
  feedback.value = feedbackError.value
    ? "操作失败，请稍后重试。"
    : `${message}（展示效果）`;
}
function closeOverlays() {
  overlay.value = "none";
  feedback.value = "";
}
function showConfirm(purpose: "apply" | "exit") {
  confirmPurpose.value = purpose;
  overlay.value = "confirm";
}
function resetPreview() {
  cancelSessionOperation();
  voiceSession.stop();
  replayMode.value = false;
  page.value = "home";
  scene.value = "normal";
  runtimeState.value = "standby";
  reasoningMode.value = "fast";
  closeOverlays();
  resetKey.value += 1;
}
function toggleVoiceSession() {
  if (replayMode.value || scene.value === 'loading' || sessionOperation.value) return;
  if (voiceSession.active.value) voiceSession.stop();
  else voiceSession.start();
}
function handleEscape(event: KeyboardEvent) {
  if (event.key === "Escape") closeOverlays();
}
onMounted(() => window.addEventListener("keydown", handleEscape));
onBeforeUnmount(() => { cancelSessionOperation(); window.removeEventListener("keydown", handleEscape); });

// conversationModel（会话条目）、traceModel（活动条目）不随音量帧重建，避免动效期间反复滚动列表。
const conversationModel = computed<WorkbenchModel['conversation']>(() => messages.value.map((item) => ({
  key: item.id,
  role: item.role,
  text: item.text,
  time: item.time,
  status: item.role === "assistant" ? replayMode.value ? '固定日志回放' : "已完成播报" : "已完成识别",
  speaking: item.role === "assistant" && effectiveRuntimeState.value === "speaking"
    && (!replayMode.value || item.id === replaySpeakingMessageId.value),
})));
const traceModel = computed<WorkbenchModel['shadow']['trace']>(() => activities.value.map((item) => ({
  key: item.id,
  title: item.label,
  text: scene.value === "long" ? longIdentifier : item.text,
  status: item.state === "active" ? "处理中" : item.state === 'failed' ? '失败' : "已完成",
  time: "刚刚",
})));
const workbenchModel = computed<WorkbenchModel>(() => ({
  state: effectiveRuntimeState.value,
  sessionActive: replayMode.value || voiceSession.active.value,
  sessionOperation: sessionOperation.value,
  audioLevels: replayMode.value || effectiveRuntimeState.value === 'error' ? [] : voiceSession.audioLevels.value,
  title: currentRuntime.value.label,
  description: currentRuntime.value.description,
  action: replayMode.value ? '日志回放 · 只读' : voiceSession.active.value
    ? "停止当前会话"
    : "开始语音会话",
  disabled: replayMode.value || scene.value === "loading" || Boolean(sessionOperation.value),
  modeDisabled: replayMode.value || Boolean(sessionOperation.value),
  canReset: !replayMode.value && !voiceSession.active.value && effectiveRuntimeState.value === "standby",
  mode: reasoningMode.value,
  latency: replayMode.value ? voiceShadowReplayDemo.latency : voiceSession.active.value
    ? voiceSession.latencyMs.value === null ? '—' : `${voiceSession.latencyMs.value} ms`
    : currentRuntime.value.latency,
  latencyPending: !replayMode.value && voiceSession.latencyPending.value,
  voiceModel: "DeepSeek Flash",
  shadowModel: replayMode.value ? '历史活动回放' : "代理宿主管理",
  conversation: conversationModel.value,
  partial: effectiveRuntimeState.value === "listening" ? "我想整理一下今天的安排…" : "",
  thinking: effectiveRuntimeState.value === "thinking",
  conversationHint: replayMode.value ? '日志回放 · 只读固定样例' : effectiveRuntimeState.value === "standby" ? "等待你的语音" : "本地语音交互",
  voiceContext: replayMode.value ? voiceShadowReplayDemo.context : "42K / 128K",
  voiceCache: replayMode.value ? voiceShadowReplayDemo.cache : "82%",
  shadow: {
    title: replayMode.value ? '影子协作日志回放' : scene.value === "empty"
        ? "等待新的任务"
        : scene.value === "error"
          ? "后台协作暂时不可用"
          : "整理下午安排并保留专注时间",
    status: replayMode.value ? '日志回放 · 只读' : scene.value === "loading" ? "处理中" : "随时可协作",
    focus: replayMode.value ? '根据本地记录查看对话与影子活动，不会连接真实服务。' : scene.value === "long" ? longText : "正在整理待确认事项与依赖关系。",
    context: replayMode.value ? voiceShadowReplayDemo.context : "86K / 256K",
    cache: replayMode.value ? voiceShadowReplayDemo.cache : "76%",
    trace: traceModel.value,
    event: replayMode.value ? '本机固定日志样例，无业务连接。' : scene.value === "empty" ? "" : "已识别时间偏好，等待下一步确认。",
  },
}));
</script>
<template>
  <AppFrame
    :runtime="effectiveRuntimeState"
    preview
  >
    <Workbench
      v-show="page === 'home'"
      :model="workbenchModel"
      console-enabled
      @settings="page = 'wake'"
      @console="notify('控制台入口')"
      @action="toggleVoiceSession"
      @reset="performSessionOperation('reset')"
      @clear="performSessionOperation('clear')"
      @mode="reasoningMode = $event"
    />
    <SettingsFrame
      v-show="page !== 'home'"
      :section="page"
      @back="page = 'home'"
      @section="page = $event"
      ><div
        v-if="page !== 'compute' && (scene === 'loading' || scene === 'error')"
        class="info-callout"
        :role="scene === 'error' ? 'alert' : 'status'"
      >
        <span>{{ layoutScene }}</span
        ><button v-if="scene === 'error'" @click="notify('重试读取')">
          重试
        </button>
      </div>
      <DemoSettings
        :key="resetKey"
        :tab="page === 'home' ? 'wake' : page"
        :scene="scene"
        @feedback="notify"
        @confirm="showConfirm"
    /></SettingsFrame>
    <template #overlays
      ><div
        v-if="showNotice"
        class="save-notice"
        :class="{ 'error-notice': noticeError }"
        :role="noticeError ? 'alert' : 'status'"
      >
        <span>{{ noticeError ? "!" : "✓" }}</span
        >{{ noticeText
        }}<button aria-label="关闭提示" @click="closeOverlays">×</button>
      </div>
      <UiDialog
        :open="overlay === 'confirm'"
        :title="
          confirmPurpose === 'exit' ? '退出整个应用？' : '应用新的模型配置？'
        "
        @close="closeOverlays"
        ><p>
          {{
            confirmPurpose === "exit"
              ? "退出将停止语音服务，并回收所属组件。"
              : "将结束当前语音交互并重新加载受影响的服务。"
          }}
        </p>
        <template #actions
          ><UiButton variant="quiet" @click="closeOverlays">暂不操作</UiButton
          ><UiButton
            @click="overlay = scene === 'error' ? 'failure' : 'success'"
            >确认{{ confirmPurpose === "exit" ? "退出" : "应用" }}</UiButton
          ></template
        ></UiDialog
      >
      <div
        v-if="overlay === 'startup'"
        class="application-startup"
        @click.self="closeOverlays"
      >
        <section
          class="application-startup-card"
          role="dialog"
          aria-modal="true"
          aria-labelledby="preview-startup-title"
        >
          <span class="application-brand">VOICE TERMINAL（语音终端）</span>
          <h1 id="preview-startup-title">
            {{
              scene === "error"
                ? "启动需要检查"
                : scene === "empty"
                  ? "等待启动系统"
                  : "正在启动系统"
            }}
          </h1>
          <div class="application-startup-progress">
            <span>已完成 {{ scene === "empty" ? 0 : 3 }} / 5 项启动检查</span
            ><span>已耗时 8 秒</span>
          </div>
          <progress :value="scene === 'empty' ? 0 : 3" max="5"></progress>
          <ul>
            <li
              v-for="(component, index) in demoSystemComponents"
              :key="component.id"
            >
              <div>
                <strong>{{ component.name }}</strong
                ><span
                  :class="scene === 'error' && index === 3 ? 'failed' : 'ready'"
                  >{{
                    scene === "empty"
                      ? "等待启动"
                      : scene === "error" && index === 3
                        ? "模型加载失败"
                        : index < 3
                          ? "已就绪"
                          : "启动中"
                  }}</span
                >
              </div>
              <small>{{ component.phase }}</small>
            </li>
          </ul>
          <div class="application-startup-actions">
            <button
              type="button"
              @click="
                page = 'compute';
                overlay = 'none';
              "
            >
              查看本地组件</button
            ><button
              type="button"
              @click="
                notify('重试启动组件');
                overlay = 'failure';
              "
            >
              重试失败组件</button
            ><button type="button" @click="closeOverlays">关闭预览蒙版</button>
          </div>
        </section>
      </div>
      <button
        v-if="!toolsOpen"
        class="preview-tools-trigger"
        @click="toolsOpen = true"
      >
        展开展示工具
      </button>
      <aside v-else class="preview-tools" aria-label="视觉展示工具">
        <label
          ><span>查看页面</span
          ><select v-model="page">
            <option
              v-for="choice in pageChoices"
              :key="choice.id"
              :value="choice.id"
            >
              {{ choice.label }}
            </option>
          </select></label
        ><label
          ><span>内容场景</span
          ><select v-model="scene">
            <option
              v-for="choice in sceneChoices"
              :key="choice.value"
              :value="choice.value"
            >
              {{ choice.label }}
            </option>
          </select></label
        ><label
          ><span>运行状态</span
          ><select v-model="runtimeState" @change="voiceSession.selectState(runtimeState)">
            <option
              v-for="choice in stateChoices"
              :key="choice.value"
              :value="choice.value"
            >
              {{ choice.label }}
            </option>
          </select></label
        ><label
          ><span>浮层展示</span
          ><select v-model="overlay">
            <option
              v-for="choice in overlayChoices"
              :key="choice.value"
              :value="choice.value"
            >
              {{ choice.label }}
            </option>
          </select></label
        ><label
          ><span>事件回放</span
          ><select v-model="replayMode">
            <option :value="false">固定演示</option>
            <option :value="true">语音与影子活动回放</option>
          </select></label
        ><button @click="resetPreview">重置展示</button
        ><button @click="toolsOpen = false">收起工具</button>
      </aside></template
    >
  </AppFrame>
</template>
