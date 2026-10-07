import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { onMounted } from "vue";
import { useApplicationHost } from "./application-host";
import type { ReasoningMode, ShadowActivity } from "../types";
import type { CustomInstruction } from "./types";
import {
  useProductAdapter,
  type AssignmentSlot,
  type ConversationRow,
  type PendingSpeechRow,
  type ProductSettingsTab,
  type ProductUiModel,
} from "./useProductAdapter";
import type { WorkbenchModel } from "../interface/workbench";

/** useProductControls（页面控件组合逻辑）：复用业务适配器并集中保留表单和录音生命周期。 */
export function useProductControls() {
  const adapter = useProductAdapter();
  const applicationHost = useApplicationHost();
  const { snapshot: applicationSnapshot } = applicationHost;
  onMounted(() => {
    void applicationHost.start();
    const query = new URLSearchParams(window.location.search);
    if (
      query.get("page") === "silence" ||
      query.get("settings") === "silence"
    ) {
      adapter.page.value = "settings";
      adapter.settingsTab.value = "wake";
    }
    if (
      ['system', 'compute'].includes(query.get('settings') ?? query.get('page') ?? '')
    ) {
      applicationHost.inspect();
      adapter.page.value = "settings";
      adapter.settingsTab.value = "compute";
    }
  });
  onBeforeUnmount(applicationHost.stop);
  function showApplicationSystem() {
    adapter.page.value = "settings";
    adapter.settingsTab.value = "compute";
  }
  // inspecting（已跳过加载画面）：最早原生页面的选择也直接进入本地组件。
  if (applicationHost.inspecting.value) showApplicationSystem();
  const {
    voice,
    page: activePage,
    settingsTab: activeSettingsTab,
    settings,
    loading,
    saving,
    connected,
    runtimeState,
    coreUnavailable,
    current,
    conversationRows,
    pendingSpeechRows,
    currentTurnLogged,
    currentTurnSpeechLogged,
    currentTurnSpeechPending,
    shadow,
    latencyMs,
    latencyRunning,
    wakeForm,
    wakeTasks,
    addingWakeTask,
    wakeTaskCreateDraft,
    addWakeTask,
    createWakeTask,
    editingWakeTaskId,
    wakeTaskDraft,
    refreshWakeTasks,
    editWakeTask,
    saveWakeTask,
    removeWakeTask,
    maxWakeKeywords,
    addWakeKeyword,
    removeWakeKeyword,
    silenceSeconds,
    personaDocument: activePersona,
    personaDocuments,
    customInstructions,
    providerForm,
    providerStep,
    discoveredModels,
    modelAssignments,
    uiModels: modelEntries,
    availableLlmModels,
    availableTtsModels,
    visibleLocalPool,
    hiddenPool,
    managedApiModelPool,
    selectedModels,
    activeModels,
    activeTtsModel,
    registry,
    computeStatus,
    computeSavedDevices,
    refreshComputeStatus,
    saveComputeSettings,
    modelScanState,
    restartState,
    applyImpact,
    dsh,
    openDshConsole,
    lastError,
    notice,
    noticeVisible,
    runtimeAction: performRuntimeAction,
    resetSession,
    setReasoningMode,
    toggleProviderReasoning,
    saveWake,
    saveSilence,
    quietHoursForm,
    quietStatus,
    saveQuietHours,
    saveShadowGate,
    savePersona,
    saveCustomInstructions,
    scanModels,
    discoverProvider,
    verifyCustomModel,
    addVerifiedProvider,
    requestModelApply,
    confirmModelApply,
    retry,
    setNotice,
    speakerForm,
    speakerStatus,
    speakerProfile,
    speakerTestResult,
    ambientContext,
    speakerEnrollment,
    saveSpeaker,
    refreshSpeaker,
    startSpeakerEnrollment,
    addSpeakerEnrollmentSegment,
    commitSpeakerEnrollment,
    cancelSpeakerEnrollment: cancelEnrollmentSession,
    deleteSpeakerTemplate,
    setSpeakerTemplateCentroid,
    testSpeakerAudio,
    captureSpeakerEnrollmentSegment,
    captureSpeakerTest,
    refreshAmbientContext,
    clearAmbientContext,
  } = adapter;
  const computeModelRows = [
    {
      id: "paraformer",
      title: "Paraformer（流式语音识别）",
      detail: "实时语音识别工作进程。",
    },
    {
      id: "sensevoice",
      title: "SenseVoiceSmall（语音识别与分析）",
      detail: "包含同设备运行的内部 FSMN-VAD（语音活动检测）。",
    },
    {
      id: "voice_vad",
      title: "FSMN-VAD（语音活动检测）",
      detail: "实时语音输入与分段检测。",
    },
    {
      id: "kws",
      title: "KWS（关键字唤醒）",
      detail: "Sherpa-ONNX（唤醒引擎）关键字唤醒模型。",
    },
    {
      id: "smart_turn",
      title: "SmartTurn（语音结束判断）",
      detail: "ONNX Runtime（推理引擎）语音轮次结束分类。",
    },
    {
      id: "wespeaker",
      title: "WeSpeaker（声纹识别）",
      detail: "本地 ONNX（推理引擎）声纹向量提取。",
    },
  ] as const;
  function formatComputeBytes(value: number | null | undefined) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
      return "不可用";
    const units = ["B", "KB", "MB", "GB"];
    let amount = value;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return amount.toFixed(unit > 1 ? 1 : 0) + " " + units[unit];
  }
  function deviceLabel(value: string | null | undefined) {
    if (value === "cpu") return "CPU（中央处理器）";
    if (value?.startsWith("cuda")) return "GPU（图形处理器）";
    return "未运行";
  }
  function requestedDeviceLabel(value: string | null | undefined) {
    if (value === "auto") return "自动选择";
    return deviceLabel(value);
  }
  function computeChangePending() {
    return computeModelRows.some((row) => {
      const runtimeModel = computeStatus.value?.models[row.id];
      if (!runtimeModel) return false;
      const selected = settings.compute.devices[row.id];
      const actual = runtimeModel.device?.startsWith("cuda")
        ? "cuda"
        : runtimeModel.device;
      return (
        selected !== runtimeModel.requested_device &&
        !(runtimeModel.load_error && selected === actual)
      );
    });
  }
  function hasUnsupportedGpuChoice() {
    return computeModelRows.some(
      (row) =>
        settings.compute.devices[row.id] === "cuda" &&
        computeStatus.value?.models[row.id]?.gpu_available !== true,
    );
  }
  function serviceLabel(value: string | undefined) {
    return (
      (
        {
          READY: "运行中",
          DEGRADED: "降级运行",
          ERROR: "运行异常",
          UNAVAILABLE: "无法连接",
          DISABLED: "已停用",
          STARTING: "启动中",
          UNKNOWN: "状态未知",
        } as Record<string, string>
      )[value || "UNKNOWN"] ||
      value ||
      "状态未知"
    );
  }
  const reasoningMode = voice.reasoningMode;
  const listening = voice.listening;
  const waitingForWake = computed(
    () =>
      listening.value &&
      voice.wakeAlwaysOn.value &&
      voice.inputOwner.value === "kws",
  );
  const contextUsagePercent = computed(() => {
    const episode = voice.agentEpisode.value;
    // An idle Agent reports a zero-initialized estimate before the first real
    // provider/context measurement.  Keep that implementation default out of
    // the product resource meter instead of presenting a fabricated 0%.
    if (
      !episode ||
      typeof episode.context_token_estimate !== "number" ||
      !Number.isFinite(episode.context_token_estimate) ||
      episode.context_token_estimate <= 0
    )
      return null;
    return typeof episode.context_usage_percent === "number" &&
      Number.isFinite(episode.context_usage_percent)
      ? episode.context_usage_percent
      : null;
  });
  const showApplyConfirm = ref(false);
  const expandedModelSections = reactive({
    add: true,
    config: true,
    pool: true,
  });
  const editingModelId = ref<string | null>(null);
  const deleteCandidateId = ref<string | null>(null);
  const modelEditDraft = reactive({
    name: "",
    maxContextTokens: 131072,
    allowedReasoning: [] as string[],
  });
  const roleLabels: Record<
    AssignmentSlot,
    { title: string; hint: string; kind: string }
  > = {
    voice: {
      title: "语音交互主模型",
      hint: "正式回答与播报队列",
      kind: "语言模型 01",
    },
    shadow: {
      title: "影子代理模型",
      hint: "后台理解、工具与任务推进",
      kind: "语言模型 02",
    },
    status: {
      title: "状态汇报模型",
      hint: "影子代理的实时工作简报",
      kind: "语言模型 03",
    },
  };
  const llmAssignmentSlots: AssignmentSlot[] = ["voice", "shadow", "status"];
  const personaItems: Array<{
    id: keyof typeof personaDocuments;
    label: string;
    path: string;
    owner: string;
  }> = [
    {
      id: "personality",
      label: "人格风格",
      path: "人格风格文档",
      owner: "主语音模型",
    },
    {
      id: "user",
      label: "用户资料",
      path: "用户资料文档",
      owner: "主语音模型",
    },
  ];
  const emptyInstruction = (): CustomInstruction => ({
    id: "",
    trigger: "",
    target: "voice",
    instruction: "",
    enabled: true,
  });
  const instructionDraft = reactive<CustomInstruction>(emptyInstruction());
  const instructionFormError = ref("");
  function addInstructionRow() {
    customInstructions.value.push({ ...emptyInstruction(), id: crypto.randomUUID() });
  }
  function editInstruction(item: CustomInstruction) {
    Object.assign(instructionDraft, item);
    instructionFormError.value = "";
  }
  function clearInstructionDraft() {
    Object.assign(instructionDraft, emptyInstruction());
    instructionFormError.value = "";
  }
  function stageInstruction() {
    const trigger = instructionDraft.trigger.trim();
    const instruction = instructionDraft.instruction.trim();
    if (!trigger || !instruction) {
      instructionFormError.value = "请填写触发语义和执行目标。";
      return false;
    }
    if (trigger.length > 200 || instruction.length > 2000) {
      instructionFormError.value =
        "触发语义最多 200 字，执行目标最多 2000 字。";
      return false;
    }
    const item = {
      ...instructionDraft,
      id: instructionDraft.id || crypto.randomUUID(),
      trigger,
      instruction,
    };
    const index = customInstructions.value.findIndex(
      (row) => row.id === item.id,
    );
    if (index >= 0) customInstructions.value.splice(index, 1, item);
    else customInstructions.value.push(item);
    clearInstructionDraft();
    return true;
  }
  async function saveInstructionSettings() {
    instructionFormError.value = "";
    if (customInstructions.value.some(item => !item.trigger.trim() || !item.instruction.trim())) {
      instructionFormError.value = "请填写每条指令的触发语义和执行目标。";
      return;
    }
    if (
      (instructionDraft.id ||
        instructionDraft.trigger.trim() ||
        instructionDraft.instruction.trim()) &&
      !stageInstruction()
    )
      return;
    await saveCustomInstructions();
  }
  function removeInstruction(id: string) {
    customInstructions.value = customInstructions.value.filter(
      (item) => item.id !== id,
    );
    if (instructionDraft.id === id) clearInstructionDraft();
  }
  const modelContextOptions = [32768, 65536, 131072, 262144, 524288];
  const reasoningOptions = ["off", "low", "medium", "high", "max"];
  const runtimeMeta = computed(() => {
    if (applicationSnapshot.value?.startup_complete === false) return {
      label: "应用启动中",
      description: "本地组件正在启动，可以先调整设置，启动完成后再开始语音会话。",
      chip: "STARTING（启动中）",
      timestamp: "本地组件准备中",
    };
    if (coreUnavailable.value) return {
      label: "需要检查",
      description: applicationSnapshot.value?.components.find(component => component.id === 'voice')?.error || "语音能力尚未就绪，可以先配置模型或查看本地组件。",
      chip: "ERROR（不可用）",
      timestamp: "本机接口已连接",
    };
    const labels = {
      offline: ["离线", "后端连接已断开，页面仍保留，可重试连接。", "OFF"],
      standby: [
        "待机",
        "语音服务已准备好，点击即可开始语音会话。",
        "LIVE",
      ],
      listening: ["正在聆听", "正在接收本机音频并等待可靠的语句结束。", "LIVE"],
      thinking: ["正在思考", "语音助手正在准备下一段可播报回复。", "LIVE"],
      speaking: ["正在播报", "正在播放语音回复，随时可以打断。", "LIVE"],
      error: ["需要检查", "运行时报告了错误，请查看服务状态或重试。", "ERROR"],
    } as const;
    const meta = waitingForWake.value
      ? ([
          "等待唤醒",
          "正在监听唤醒词；唤醒后才会进入语音识别。",
          "LIVE",
        ] as const)
      : labels[runtimeState.value];
    const [label, description, chip] = meta;
    return {
      label,
      description,
      chip,
      timestamp: connected.value ? "实时运行时状态" : "等待语音服务",
    };
  });
  const currentDocument = computed(
    () =>
      personaItems.find((item) => item.id === activePersona.value) ||
      personaItems[0],
  );
  const hasConversation = computed(
    () =>
      conversationRows.value.length > 0 ||
      pendingSpeechRows.value.length > 0 ||
      Boolean(
        current.value.partial_text ||
          current.value.final_text ||
          current.value.assistant_text ||
          current.value.tts_text,
      ),
  );
  const conversationEntries = computed(() => conversationRows.value);
  const liveSpeechEntries = computed(() => pendingSpeechRows.value);
  const liveUserText = computed(() =>
    currentTurnLogged.value ? "" : current.value.final_text,
  );
  const liveAssistantText = computed(() =>
    current.value.assistant_text &&
    !currentTurnSpeechLogged.value &&
    !currentTurnSpeechPending.value
      ? current.value.assistant_text
      : "",
  );
  const shadowContext = computed(() => {
    const context = shadow.value.context;
    if (
      !context ||
      (context.used_tokens == null &&
        context.limit_tokens == null &&
        context.usage_percent == null)
    )
      return null;
    return context;
  });
  const shadowCacheHitRatio = computed(() => {
    const ratio = shadow.value.provider?.cache_hit_ratio;
    return typeof ratio === "number" &&
      Number.isFinite(ratio) &&
      ratio >= 0 &&
      ratio <= 1
      ? ratio
      : null;
  });
  const shadowFocusText = computed(() => {
    const focus = (shadow.value.focus || "").trim();
    const summary = (shadow.value.summary || "").trim();
    if (!focus || focus === summary) return "";
    return focus;
  });
  const conversationContext = computed(() => {
    const episode = voice.agentEpisode.value;
    if (
      !episode ||
      typeof episode.context_token_estimate !== "number" ||
      !Number.isFinite(episode.context_token_estimate) ||
      episode.context_token_estimate <= 0
    )
      return null;
    return {
      usedTokens: episode.context_token_estimate,
      limitTokens: episode.context_limit_tokens,
      usagePercent: episode.context_usage_percent,
    };
  });
  const conversationCacheHitRatio = computed(() => {
    const ratio = voice.agentEpisode.value?.provider_cache_hit_ratio;
    return typeof ratio === "number" &&
      Number.isFinite(ratio) &&
      ratio >= 0 &&
      ratio <= 1
      ? ratio
      : null;
  });
  const shadowContextTooltip = computed(() => {
    if (!shadowContext.value) return "上下文占用 · 后端尚未提供 telemetry";
    return `上下文占用 · ${formatTokens(shadowContext.value.used_tokens)} / ${formatTokens(shadowContext.value.limit_tokens)} · ${shadowContext.value.usage_percent ?? "—"}%`;
  });
  const conversationContextTooltip = computed(() => {
    if (!conversationContext.value)
      return "上下文占用 · 后端尚未提供 telemetry";
    return `上下文占用 · ${formatTokens(conversationContext.value.usedTokens)} / ${formatTokens(conversationContext.value.limitTokens)} · ${conversationContext.value.usagePercent ?? "—"}%`;
  });
  const modelPoolSummary = computed(
    () =>
      `${managedApiModelPool.value.length} 个可编辑服务 · ${availableLlmModels.value.length} 个语言模型 · ${availableTtsModels.value.length} 个语音模型`,
  );
  const apiModelPool = computed(() =>
    modelEntries.value.filter(
      (item) =>
        (item.capability === "llm" || item.capability === "tts") &&
        !item.hidden,
    ),
  );
  const hiddenApiModelPool = computed(() =>
    modelEntries.value.filter(
      (item) =>
        (item.capability === "llm" || item.capability === "tts") && item.hidden,
    ),
  );
  const hiddenLocalPool = computed(() =>
    modelEntries.value.filter(
      (item) =>
        item.capability !== "llm" && item.capability !== "tts" && item.hidden,
    ),
  );
  const assignedModelIds = computed(
    () =>
      new Set([
        modelAssignments.voice,
        modelAssignments.shadow,
        modelAssignments.status,
        activeModels.value.tts?.model_id,
      ]),
  );
  const selectedShadowReasoning = computed(() => "high");
  const shadowActivities = computed(() => shadow.value.activities || []);
  const shadowLastActivityAt = computed(
    () =>
      shadow.value.last_activity_at ||
      shadowActivities.value[shadowActivities.value.length - 1]?.timestamp ||
      shadow.value.timeline[shadow.value.timeline.length - 1]?.timestamp ||
      "",
  );
  const shadowDisplayStatus = computed(() => {
    if (!shadow.value.available) return "不可用";
    if (shadow.value.user_input_required || shadow.value.phase === "waiting")
      return "等待输入";
    if (shadow.value.active || shadow.value.phase === "working")
      return "运行中";
    if (shadow.value.phase === "error" || shadow.value.phase === "failed")
      return "失败";
    if (shadow.value.phase === "completed" || shadow.value.phase === "done")
      return "已完成";
    return "空闲";
  });
  const shadowStatusClass = computed(() => {
    if (!shadow.value.available) return "unavailable";
    if (shadow.value.user_input_required || shadow.value.phase === "waiting")
      return "waiting";
    if (shadow.value.active || shadow.value.phase === "working")
      return "working";
    if (shadow.value.phase === "error" || shadow.value.phase === "failed")
      return "failed";
    if (shadow.value.phase === "completed" || shadow.value.phase === "done")
      return "completed";
    return "idle";
  });
  function legacyShadowEvents(): ShadowActivity[] {
    const eventKinds: Record<string, ShadowActivity["event_kind"]> = {
      后台处理中: "progress",
      "Voice 信息": "information",
      等待你的输入: "input_required",
      完成: "result",
      无需动作: "no_action",
      需要注意: "failed",
    };
    return shadow.value.timeline.map((item, index) => ({
      run_seq: 0,
      item_id: `legacy-event-${index}`,
      stream: "event",
      phase: item.state === "error" ? "failed" : "completed",
      text: item.text || item.label,
      event_kind: eventKinds[item.label],
      timestamp: item.timestamp,
    }));
  }
  const shadowRunningTrace = computed(() => {
    if (shadow.value.backend !== "dsh") return [];
    const latestRun = shadowActivities.value.reduce<number | null>(
      (current, item) => current === null ? item.run_seq : Math.max(current, item.run_seq),
      null,
    );
    if (latestRun === null) return [];
    const currentRun = shadowActivities.value.filter((item) => item.run_seq === latestRun);
    const lifecycle = [...currentRun].reverse().find((item) => item.stream === "lifecycle");
    if (!lifecycle || lifecycle.phase === "completed" || lifecycle.phase === "failed") return [];
    const currentAction = currentRun.filter(
      (item) => (item.stream === "thinking" || item.stream === "tool")
        && !item.item_id.startsWith("observer-"),
    ).at(-1);
    return currentAction ? [currentAction] : [];
  });
  const shadowEventActivities = computed(() => {
    const values = shadowActivities.value.filter(
      (item) => item.stream === "event",
    );
    return values.length ? values : legacyShadowEvents();
  });
  const shadowLatestEventActivity = computed<ShadowActivity | null>(() => {
    const values = shadowEventActivities.value;
    return values.length ? values[values.length - 1] : null;
  });
  function shadowActivityPhaseLabel(activity: ShadowActivity) {
    if (activity.stream === "tool") {
      const labels: Record<string, string> = {
        preparing: "准备调用",
        running: "执行中",
        completed: "已完成",
        failed: "失败",
        waiting: "等待输入",
        skipped: "已跳过",
        ready: "待执行",
      };
      if (activity.tool_status && labels[activity.tool_status])
        return labels[activity.tool_status];
    }
    if (activity.event_kind === "input_required") return "等待输入";
    if (activity.event_kind === "failed" || activity.phase === "failed")
      return "失败";
    if (activity.phase === "started" || activity.phase === "delta")
      return "进行中";
    return "已完成";
  }
  function shadowActivityText(activity: ShadowActivity) {
    if (activity.text) return activity.text;
    if (activity.stream === "tool")
      return activity.tool_name
        ? `正在处理 ${activity.tool_name}`
        : "正在处理工具调用";
    if (activity.event_kind === "input_required")
      return "Shadow 正在等待你的输入";
    if (activity.phase === "failed") return "这项活动没有完成";
    return activity.phase === "started" || activity.phase === "delta"
      ? "正在运行"
      : "已更新";
  }
  function shadowActivityKindLabel(activity: ShadowActivity) {
    const labels: Record<string, string> = {
      progress: "处理中",
      information: "Voice 信息",
      input_required: "需要输入",
      result: "结果",
      no_action: "无需动作",
      failed: "失败",
    };
    return activity.event_kind
      ? labels[activity.event_kind] || activity.event_kind
      : "事件";
  }
  function shadowActivityClass(activity: ShadowActivity) {
    return {
      active: activity.phase === "started" || activity.phase === "delta",
      complete: activity.phase === "completed",
      failed: activity.phase === "failed" || activity.tool_status === "failed",
      waiting:
        activity.tool_status === "waiting" ||
        activity.event_kind === "input_required",
    };
  }
  function formatTime(value: string | undefined) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function formatResponseLatency(value: number | null | undefined) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (value < 1000) return `${Math.round(value)} ms`;
    const seconds = Number((value / 1000).toFixed(2));
    return `${seconds} s`;
  }
  function formatTokens(value: number | null | undefined) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
    if (value >= 1000) return `${Math.round(value / 1000)}K`;
    return String(Math.round(value));
  }
  function contextPercent(value: number | null | undefined) {
    return typeof value === "number" && Number.isFinite(value)
      ? Math.max(0, Math.min(100, value))
      : 0;
  }
  function providerClass(item: ProductUiModel | undefined) {
    return (
      item?.provider
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/-+$/, "") || "managed"
    );
  }
  function modelTitle(item: ProductUiModel | undefined) {
    return item ? `${item.provider} · ${item.name}` : "未选择模型";
  }
  function selectedModel(slot: AssignmentSlot) {
    const id = modelAssignments[slot];
    return modelEntries.value.find((item) => item.id === id);
  }
  function modelStatusLabel(item: ProductUiModel | undefined) {
    if (!item) return "未选择";
    if (item.verification === "verified") return "已验证";
    if (item.verification === "inherited") return "运行时导入";
    if (item.health === "healthy") return "健康";
    return item.health || item.verification || "待检查";
  }
  function formatContext(value: number | undefined) {
    return value ? `${formatTokens(value)} tokens` : "未设置";
  }
  function turnStatusLabel(row: ConversationRow) {
    const labels: Record<string, string> = {
      completed: "已完成识别",
      empty_input: "没有听到内容",
      stopped: "已停止",
      reset: "已重置",
      failed: "识别失败",
      pending: "识别中",
    };
    return (
      labels[row.status] ||
      labels[row.termination] ||
      row.status ||
      row.termination ||
      "已记录"
    );
  }
  function speechStatusLabel(row: ConversationRow) {
    const labels: Record<string, string> = {
      completed: "已完成播报",
      failed: "播报失败",
      interrupted: "播报中断",
      replaced: "已被新内容替换",
      superseded: "已被新内容替换",
      speaking: "正在播报",
      playing: "正在播报",
      queued: "等待播放",
      waiting: "等待播放",
    };
    const status = row.status || "";
    return labels[status.toLowerCase()] || status || "已记录";
  }
  function speechStatusKey(row: ConversationRow) {
    return row.status ? row.status.toLowerCase() : "recorded";
  }
  function pendingSpeechStatusLabel(row: PendingSpeechRow) {
    return row.state === "playing" ? "正在播报" : "等待播放";
  }
  function deliveryStatusLabel(
    delivery: { status?: string; termination?: string } | undefined,
  ) {
    if (!delivery) return "";
    const statusLabels: Record<string, string> = {
      full: "已完整传达",
      partial: "已传达前缀",
      none: "未传达",
      unknown: "传达边界未知",
    };
    const terminationLabels: Record<string, string> = {
      interrupted: "被插话打断",
      failed: "TTS 失败",
      stopped: "手动停止",
      completed: "正常结束",
    };
    const status =
      statusLabels[delivery.status || ""] ||
      delivery.status ||
      "播放信息不可用";
    const termination = terminationLabels[delivery.termination || ""];
    return termination ? `${status} · ${termination}` : status;
  }
  function reasoningSummary(item: ProductUiModel | undefined) {
    if (!item || item.capability !== "llm") return "不适用";
    const values = item.allowedReasoning || [];
    return values.length ? values.join(" / ") : "—";
  }
  function modelPolicySummary(item: ProductUiModel | undefined) {
    if (!item) return "尚未分配";
    if (item.capability === "tts") return "语音合成 · profile 管理";
    return `${formatContext(item.maxContextTokens)} · ${reasoningSummary(item)}`;
  }
  function toggleModelSection(section: keyof typeof expandedModelSections) {
    expandedModelSections[section] = !expandedModelSections[section];
  }
  function isAssignedModel(item: ProductUiModel) {
    return assignedModelIds.value.has(item.id);
  }
  function isEditableApiModel(item: ProductUiModel) {
    return (
      item.origin === "product_managed" &&
      (item.capability === "llm" || item.capability === "tts")
    );
  }
  function startEditingModel(item: ProductUiModel) {
    if (!isEditableApiModel(item)) return;
    editingModelId.value = item.id;
    deleteCandidateId.value = null;
    modelEditDraft.name = item.name;
    modelEditDraft.maxContextTokens = item.maxContextTokens || 131072;
    modelEditDraft.allowedReasoning = [...(item.allowedReasoning || ["off"])];
  }
  function toggleReasoningLevel(value: string) {
    if (value === "off") {
      modelEditDraft.allowedReasoning = ["off"];
      return;
    }
    const withoutOff = modelEditDraft.allowedReasoning.filter(
      (item) => item !== "off",
    );
    const next = withoutOff.includes(value)
      ? withoutOff.filter((item) => item !== value)
      : [...withoutOff, value];
    modelEditDraft.allowedReasoning = next.length ? next : ["off"];
  }
  async function saveModelEdit(item: ProductUiModel) {
    const name = modelEditDraft.name.trim();
    if (!name) {
      adapter.setError(new Error("显示名称不能为空。"));
      return;
    }
    const changes: Record<string, unknown> = { name };
    if (item.capability === "llm") {
      changes.max_context_tokens = modelEditDraft.maxContextTokens;
      changes.allowed_reasoning = [...modelEditDraft.allowedReasoning];
    }
    if (await updateModel(item, changes)) editingModelId.value = null;
  }
  async function updateModel(
    item: ProductUiModel,
    changes: Record<string, unknown>,
  ) {
    return await adapter.updateModel(item, changes);
  }
  function requestDeleteModel(item: ProductUiModel) {
    if (!isEditableApiModel(item)) return;
    if (isAssignedModel(item)) {
      adapter.setError(
        new Error("该模型正在被分配使用，请先在模型配置中改用其他条目。"),
      );
      return;
    }
    editingModelId.value = null;
    deleteCandidateId.value =
      deleteCandidateId.value === item.id ? null : item.id;
  }
  async function confirmDeleteModel(item: ProductUiModel) {
    if (deleteCandidateId.value !== item.id || isAssignedModel(item)) return;
    if (await deleteModel(item)) deleteCandidateId.value = null;
  }
  async function deleteModel(item: ProductUiModel) {
    return await adapter.deleteModel(item);
  }
  async function toggleHidden(item: ProductUiModel) {
    if (isAssignedModel(item)) {
      adapter.setError(new Error("该模型正在被分配使用，请先改用其他条目。"));
      return;
    }
    if (item.hidden) await restoreModel(item);
    else await hideModel(item);
  }
  async function hideModel(item: ProductUiModel) {
    await adapter.hideModel(item);
  }
  async function restoreModel(item: ProductUiModel) {
    await adapter.restoreModel(item);
  }
  async function openApplyDialog() {
    const result = await requestModelApply();
    if (result?.requires_confirmation) showApplyConfirm.value = true;
  }
  async function confirmApply() {
    await confirmModelApply();
    if (restartState.value === "complete") showApplyConfirm.value = false;
  }
  async function selectDiscoveredModel(event: Event) {
    const target = event.target as HTMLSelectElement;
    providerForm.customModelId = target.value;
  }
  const speakerRecordingSegment = ref<number | null>(null);
  const speakerTestRecording = ref(false);
  const speakerRecordingSeconds = ref(0);
  const speakerServerCapture = ref(false);
  type SpeakerCapture = {
    index: number;
    label: string;
    kind: "enrollment" | "test";
    context: AudioContext;
    stream: MediaStream;
    source: MediaStreamAudioSourceNode;
    processor: ScriptProcessorNode;
    sink: GainNode;
    chunks: Float32Array[];
    startedAt: number;
    timeout?: number;
    ticker?: number;
  };
  let speakerCapture: SpeakerCapture | null = null;
  let captureGeneration = 0; // captureGeneration（录音请求代次）：拒绝已取消请求的迟到设备授权。
  let controlsDisposed = false; // controlsDisposed（页面生命周期结束）：禁止卸载后启用采集。
  let enrollmentStarting = false; // enrollmentStarting（注册请求未完成）：防止快速重复创建会话。
  const speakerServiceLabel = computed(() => {
    if (speakerStatus.value.model_present === false) return "未安装";
    const profile = speakerStatus.value.profile;
    if (profile && (profile as Record<string, unknown>).available === false)
      return "未注册";
    const service = String(speakerStatus.value.service || "").toUpperCase();
    if (service === "READY") return "就绪";
    if (service === "STARTING") return "启动中";
    if (service === "DEGRADED") return "降级直通";
    if (service === "ERROR") return "错误";
    return service === "DISABLED" ? "已关闭" : "不可用";
  });
  const speakerRollingSummary = computed(() => {
    if (speakerForm.mode === "off") return "";
    const rolling = speakerStatus.value.rolling_state;
    if (!isRecord(rolling)) return "当前声音：证据不足";
    const age = Number(rolling.age_ms);
    const ageText = Number.isFinite(age)
      ? ` · ${Math.round(age)}ms（毫秒）前`
      : "";
    return `当前声音：${speakerStateLabel(rolling.identity)}${ageText}`;
  });
  const speakerTemplates = computed(() => {
    const templates = speakerProfile.value.templates;
    return Array.isArray(templates)
      ? (templates.filter((item) => item && typeof item === "object") as Array<
          Record<string, unknown>
        >)
      : [];
  });
  const ambientRecords = computed(() => {
    const records = ambientContext.value.records;
    return Array.isArray(records)
      ? (records.filter((item) => item && typeof item === "object") as Array<
          Record<string, unknown>
        >)
      : [];
  });
  const speakerHasProfile = computed(
    () =>
      speakerProfile.value.available === true ||
      (speakerStatus.value.profile &&
        (speakerStatus.value.profile as Record<string, unknown>).available ===
          true),
  );
  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }
  const speakerNeedsReenrollment = computed(() => {
    const profile = speakerProfile.value;
    if (!isRecord(profile)) return false;
    if (profile.stale === true || profile.needs_reenrollment === true)
      return true;
    const nested = speakerStatus.value.profile;
    return (
      isRecord(nested) &&
      (nested.stale === true || nested.needs_reenrollment === true)
    );
  });
  const speakerStaleReason = computed(() => {
    const profile = isRecord(speakerProfile.value) ? speakerProfile.value : {};
    const nested = isRecord(speakerStatus.value.profile)
      ? (speakerStatus.value.profile as Record<string, unknown>)
      : {};
    const reason = String(profile.stale_reason || nested.stale_reason || "");
    const labels: Record<string, string> = {
      feature_frontend_changed: "特征前端版本已更新；",
      feature_numeric_mismatch: "特征数值指纹与注册时不一致；",
      speaker_model_changed: "声纹模型已更换；",
      profile_schema_upgraded: "声纹资料格式已升级；",
      profile_missing_probe: "旧资料缺少特征指纹；",
    };
    return labels[reason] || "";
  });
  const speakerFilterAllowed = computed(() => {
    if (!speakerHasProfile.value || speakerStatus.value.model_present === false)
      return false;
    const service = String(speakerStatus.value.service || "").toUpperCase();
    // When the saved mode is off, the runtime intentionally keeps the
    // sidecar disconnected.  A registered profile should still be able to
    // request filter mode; the server will connect/probe the Worker during the
    // save and reject the change if it is genuinely unavailable.  Requiring
    // worker_connected here made the option permanently impossible to select
    // from the default off state (a circular UI gate).
    return (
      service === "DISABLED" ||
      service === "STARTING" ||
      (service === "READY" && speakerStatus.value.worker_connected === true)
    );
  });
  const speakerEnrollmentComplete = computed(
    () => speakerEnrollment.value?.segments.length === 3 && speakerRecordingSegment.value === null && !speakerTestRecording.value && !enrollmentStarting,
  );
  function speakerStateLabel(value: unknown) {
    const labels: Record<string, string> = {
      owner: "确认是主人",
      non_owner: "明确非主人",
      owner_confirmed: "确认是本人",
      owner_continuity: "连续保持本人",
      non_owner_confirmed: "明确非本人",
      unknown: "证据不足",
      unavailable: "声纹不可用",
    };
    return labels[String(value || "")] || String(value || "—");
  }
  function speakerTestReasonLabel(
    result: Record<string, unknown> | null | undefined,
  ) {
    if (!result) return "";
    const reason = String(result.reason || "");
    const candidateWindows = Math.max(
      0,
      Number(result.candidate_window_count) || 0,
    );
    const failedWindows = Math.max(0, Number(result.failed_window_count) || 0);
    const minWindowMs = Math.max(
      1,
      Number(result.minimum_window_speech_ms) || 700,
    );
    // minimum_window_speech_ratio（窗口最小人声占比）与最短人声时长同时生效。
    const minWindowRatio = Math.max(
      0,
      Math.min(1, Number(result.minimum_window_speech_ratio) || 0),
    );
    const windowMs = Math.max(1, Number(result.window_ms) || 2000);
    const minTurnMs = Math.max(
      minWindowMs,
      Number(result.minimum_turn_speech_ms) || 1500,
    );
    const qualityCounts = (result.window_quality_counts || {}) as Record<
      string,
      unknown
    >;
    const tooQuietWindows = Math.max(0, Number(qualityCounts.too_quiet) || 0);
    const clippedWindows = Math.max(0, Number(qualityCounts.clipped) || 0);
    if (reason === "speaker_test_no_usable_window") {
      return failedWindows > 0
        ? `${candidateWindows} 个候选窗口中有 ${failedWindows} 个未能生成声纹特征，服务端已记录诊断。`
        : "没有生成可用的声纹特征，请检查录音内容和声纹模型状态。";
    }
    if (reason === "speaker_test_insufficient_window_speech") {
      if (candidateWindows > 0 && tooQuietWindows === candidateWindows) {
        return `所有 ${candidateWindows} 个候选窗口的电平都过低，请提高麦克风输入音量后再测。`;
      }
      if (candidateWindows > 0 && clippedWindows === candidateWindows) {
        return `所有 ${candidateWindows} 个候选窗口都发生削波，请降低麦克风输入增益后再测。`;
      }
      const ratioRule =
        minWindowRatio > 0
          ? `，并覆盖完整 ${(windowMs / 1000).toFixed(1)} 秒窗口的 ${Math.round(minWindowRatio * 100)}%`
          : "";
      return `每个滑窗至少需要 ${(minWindowMs / 1000).toFixed(1)} 秒有效人声${ratioRule}；本次没有窗口达到门槛。`;
    }
    if (reason === "speaker_test_insufficient_turn_speech") {
      return `整段测试至少需要 ${(minTurnMs / 1000).toFixed(1)} 秒有效人声。`;
    }
    if (reason === "speaker_score_ambiguous") {
      return "已提取到有效声纹，但相似度处于不确定区间。";
    }
    return "";
  }
  function speakerTestWindowSummary(
    result: Record<string, unknown> | null | undefined,
  ) {
    if (!result) return "";
    const measuredMs = Math.max(0, Number(result.measured_speech_ms) || 0);
    const candidates = Math.max(0, Number(result.candidate_window_count) || 0);
    const eligible = Math.max(0, Number(result.eligible_window_count) || 0);
    const scored = Math.max(0, Number(result.window_count) || 0);
    const diagnostics = Array.isArray(result.window_diagnostics)
      ? (result.window_diagnostics as Array<Record<string, unknown>>)
      : [];
    const vadMeasured = diagnostics.some(
      (item) => typeof item.speech_ratio === "number",
    );
    const measurementLabel = vadMeasured
      ? "VAD检测人声"
      : diagnostics.length > 0
        ? "能量估算人声（VAD不可用）"
        : "检测人声";
    const maxWindowMs = diagnostics.reduce(
      (max, item) => Math.max(max, Number(item.speech_ms) || 0),
      0,
    );
    return `${measurementLabel} ${(measuredMs / 1000).toFixed(1)} 秒 · 单窗最高 ${(maxWindowMs / 1000).toFixed(1)} 秒 · 门槛通过 ${eligible} / ${candidates} 个窗口 · 参与判定 ${scored} 个`;
  }
  function restoreSpeakerDefaults() {
    // Mirrors config/default.yaml.  Changing the feature frontend moved the raw
    // cosine operating point, so these numbers are not comparable with any
    // threshold calibrated before that change.
    speakerForm.confirm_threshold = 0.6;
    speakerForm.continuity_threshold = 0.5;
    setNotice("声纹阈值已恢复默认值（0.60 / 0.50），点击保存后应用。");
  }
  function mergeFloat32(chunks: Float32Array[]) {
    const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
    const result = new Float32Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.length;
    }
    return result;
  }
  function pcmWavBase64(samples: Float32Array, sampleRate: number) {
    const targetRate = 16000;
    const sourceRate = Math.max(1, sampleRate);
    const ratio = sourceRate / targetRate;
    const count = Math.max(1, Math.round(samples.length / ratio));
    const pcm = new Int16Array(count);
    for (let index = 0; index < count; index += 1) {
      const source = index * ratio;
      const left = Math.floor(source);
      const right = Math.min(samples.length - 1, left + 1);
      const fraction = source - left;
      const value =
        (samples[left] || 0) * (1 - fraction) +
        (samples[right] || 0) * fraction;
      const clipped = Math.max(-1, Math.min(1, value));
      pcm[index] =
        clipped < 0 ? Math.round(clipped * 32768) : Math.round(clipped * 32767);
    }
    const buffer = new ArrayBuffer(44 + pcm.byteLength);
    const view = new DataView(buffer);
    const writeText = (offset: number, value: string) => {
      for (let index = 0; index < value.length; index += 1)
        view.setUint8(offset + index, value.charCodeAt(index));
    };
    writeText(0, "RIFF");
    view.setUint32(4, 36 + pcm.byteLength, true);
    writeText(8, "WAVE");
    writeText(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, targetRate, true);
    view.setUint32(28, targetRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeText(36, "data");
    view.setUint32(40, pcm.byteLength, true);
    new Int16Array(buffer, 44).set(pcm);
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(
        ...bytes.subarray(offset, Math.min(offset + 0x8000, bytes.length)),
      );
    }
    return btoa(binary);
  }
  async function openSpeakerCapture(
    index: number,
    label: string,
    kind: SpeakerCapture["kind"],
  ) {
    if (speakerRecordingSegment.value !== null || speakerTestRecording.value)
      return;
    if (!navigator.mediaDevices?.getUserMedia) {
      adapter.setError(new Error("当前浏览器不支持麦克风录音。"));
      return;
    }
    const generation = ++captureGeneration;
    speakerRecordingSegment.value = kind === "enrollment" ? index : null;
    speakerTestRecording.value = kind === "test";
    let acquiredStream: MediaStream | null = null;
    let acquiredContext: AudioContext | null = null;
    try {
      // Fallback path only.  The primary enrollment/test flow records on the
      // server so that it uses the same device and processing chain as a live
      // turn.  If that path is unavailable, at least ask for a 16 kHz context so
      // the browser resamples properly, and keep WebRTC AEC/NS/AGC off so the
      // signal is not reshaped differently from the runtime chain.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      acquiredStream = stream;
      if (controlsDisposed || generation !== captureGeneration) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const context = new AudioContext({ sampleRate: 16000 });
      acquiredContext = context;
      await context.resume();
      if (controlsDisposed || generation !== captureGeneration) {
        stream.getTracks().forEach((track) => track.stop());
        await context.close();
        return;
      }
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(4096, 1, 1);
      const sink = context.createGain();
      sink.gain.value = 0;
      const capture: SpeakerCapture = {
        index,
        label: label.trim() || "自定义",
        kind,
        context,
        stream,
        source,
        processor,
        sink,
        chunks: [] as Float32Array[],
        startedAt: performance.now(),
      };
      processor.onaudioprocess = (event) => {
        const active = speakerCapture;
        if (!active) return;
        active.chunks.push(
          new Float32Array(event.inputBuffer.getChannelData(0)),
        );
        speakerRecordingSeconds.value = Math.floor(
          (performance.now() - active.startedAt) / 1000,
        );
      };
      source.connect(processor);
      processor.connect(sink);
      sink.connect(context.destination);
      speakerCapture = capture;
      speakerRecordingSegment.value = kind === "enrollment" ? index : null;
      speakerTestRecording.value = kind === "test";
      speakerRecordingSeconds.value = 0;
      capture.ticker = window.setInterval(() => {
        if (speakerCapture)
          speakerRecordingSeconds.value = Math.floor(
            (performance.now() - speakerCapture.startedAt) / 1000,
          );
      }, 200);
      capture.timeout = window.setTimeout(() => {
        void stopSpeakerRecording();
      }, kind === "enrollment" ? 8000 : 6000);
    } catch (error) {
      acquiredStream?.getTracks().forEach((track) => track.stop());
      if (acquiredContext) void acquiredContext.close().catch(() => undefined);
      if (!controlsDisposed)
        adapter.setError(
          error instanceof Error ? error : new Error("无法打开麦克风。"),
        );
    } finally {
      if (!speakerCapture && generation === captureGeneration) {
        speakerRecordingSegment.value = null;
        speakerTestRecording.value = false;
      }
    }
  }
  const SPEAKER_ENROLLMENT_SECONDS = 8;
  const SPEAKER_TEST_SECONDS = 6;
  let serverCapture: { controller: AbortController; ticker: number; timeout: number; id: string; owner: string | null } | null = null;
  function cancelServerCapture() {
    if (!serverCapture) return;
    void adapter.stopSpeakerCapture(serverCapture.owner, serverCapture.id, true).catch(() => {});
    serverCapture.controller.abort();
    window.clearInterval(serverCapture.ticker);
    window.clearTimeout(serverCapture.timeout);
    serverCapture = null;
  }
  function cancelActiveSpeakerCapture() {
    captureGeneration += 1;
    cancelServerCapture();
    const capture = speakerCapture;
    speakerCapture = null;
    if (capture) {
      if (capture.timeout !== undefined) window.clearTimeout(capture.timeout);
      if (capture.ticker !== undefined) window.clearInterval(capture.ticker);
      capture.processor.disconnect();
      capture.source.disconnect();
      capture.sink.disconnect();
      capture.stream.getTracks().forEach((track) => track.stop());
      void capture.context.close();
    }
    speakerRecordingSegment.value = null;
    speakerTestRecording.value = false;
    speakerServerCapture.value = false;
    speakerRecordingSeconds.value = 0;
  }
  async function cancelSpeakerEnrollment(options: unknown = {}) {
    cancelActiveSpeakerCapture();
    await cancelEnrollmentSession(options);
  }
  watch(() => {
    const current = applicationSnapshot.value;
    return !applicationHost.businessReady.value || current?.stopping || current?.components.some(component =>
      (['asr', 'analysis', 'voice', 'speaker'].includes(component.id) && component.retrying)
      || (['voice', 'speaker'].includes(component.id) && component.state === 'starting'));
  }, blocked => {
    if (blocked) void cancelSpeakerEnrollment({ notify: false });
  }, { flush: 'sync' });
  async function captureOnServer(
    kind: SpeakerCapture["kind"],
    index: number,
    label: string,
    seconds: number,
  ) {
    const generation = ++captureGeneration;
    const id = crypto.randomUUID(); // id（本次采集请求标识）。
    const owner = kind === "enrollment" ? speakerEnrollment.value?.id ?? null : null;
    speakerRecordingSeconds.value = 0;
    speakerRecordingSegment.value = kind === "enrollment" ? index : null;
    speakerTestRecording.value = kind === "test";
    speakerServerCapture.value = true;
    const controller = new AbortController();
    const ticker = window.setInterval(() => {
      if (!controlsDisposed && !controller.signal.aborted) speakerRecordingSeconds.value += 1;
    }, 1000);
    const timeout = window.setTimeout(() => {
      void adapter.stopSpeakerCapture(owner, id, true).catch(() => {});
      controller.abort();
      if (!controlsDisposed) adapter.setError(new Error("声纹采集等待超时，请重试。"));
    }, (seconds + 15) * 1000);
    serverCapture = { controller, ticker, timeout, id, owner };
    try {
      const ok =
        kind === "test"
          ? await captureSpeakerTest(seconds, controller.signal, id)
          : await captureSpeakerEnrollmentSegment(index, label, seconds, controller.signal, id);
      return controlsDisposed || controller.signal.aborted || generation !== captureGeneration ? null : ok;
    } finally {
      window.clearInterval(ticker);
      window.clearTimeout(timeout);
      if (serverCapture?.controller === controller) serverCapture = null;
      if (generation === captureGeneration) {
        speakerServerCapture.value = false;
        speakerRecordingSegment.value = null;
        speakerTestRecording.value = false;
        speakerRecordingSeconds.value = 0;
      }
    }
  }
  async function beginSpeakerRecording(index: number, label: string) {
    if (!applicationHost.businessReady.value || applicationSnapshot.value?.startup_complete === false) {
      adapter.setError(new Error("语音服务尚未就绪，不能开始声纹录音。"));
      return;
    }
    if (
      enrollmentStarting ||
      speakerRecordingSegment.value !== null ||
      speakerTestRecording.value
    )
      return;
    enrollmentStarting = true;
    try {
      if (!speakerEnrollment.value) await startSpeakerEnrollment();
      if (!speakerEnrollment.value || controlsDisposed) return;
      const started = await captureOnServer(
        "enrollment",
        index,
        label,
        SPEAKER_ENROLLMENT_SECONDS,
      );
      if (started === false)
        await openSpeakerCapture(index, label, "enrollment");
    } finally {
      enrollmentStarting = false;
    }
  }
  async function beginSpeakerTest() {
    if (!applicationHost.businessReady.value || applicationSnapshot.value?.startup_complete === false) {
      adapter.setError(new Error("语音服务尚未就绪，不能开始声纹测试。"));
      return;
    }
    if (speakerRecordingSegment.value !== null || speakerTestRecording.value)
      return;
    if (!speakerHasProfile.value) {
      adapter.setError(new Error("请先完成声纹注册，再进行现场测试。"));
      return;
    }
    const started = await captureOnServer(
      "test",
      -1,
      "现场测试",
      SPEAKER_TEST_SECONDS,
    );
    if (started === false) await openSpeakerCapture(-1, "现场测试", "test");
  }
  function speakerPrompt(index: number) {
    const prompts = speakerEnrollment.value?.prompts || [];
    return prompts[index] || "";
  }
  function speakerSegmentLabel(index: number, fallback: string) {
    if (index !== 2 || typeof document === "undefined") return fallback;
    const input = document.getElementById(
      `speaker-label-${index}`,
    ) as HTMLInputElement | null;
    return input?.value?.trim() || "自定义";
  }
  async function stopSpeakerRecording() {
    if (speakerRecordingSeconds.value < 5) return;
    if (serverCapture) {
      try { await adapter.stopSpeakerCapture(serverCapture.owner, serverCapture.id, false); }
      catch (error) { adapter.setError(error); }
      return;
    }
    const capture = speakerCapture;
    if (!capture) return;
    speakerCapture = null;
    speakerRecordingSegment.value = null;
    speakerTestRecording.value = false;
    if (capture.timeout !== undefined) window.clearTimeout(capture.timeout);
    if (capture.ticker !== undefined) window.clearInterval(capture.ticker);
    capture.processor.disconnect();
    capture.source.disconnect();
    capture.sink.disconnect();
    capture.stream.getTracks().forEach((track) => track.stop());
    const sampleRate = capture.context.sampleRate;
    const samples = mergeFloat32(capture.chunks);
    await capture.context.close();
    speakerRecordingSeconds.value = 0;
    const durationMs = Math.round((samples.length * 1000) / sampleRate);
    if (durationMs < 5000 || durationMs > 10000) {
      adapter.setError(
        new Error("每段声纹录音需要 5–10 秒；本段未上传，请重新录制。"),
      );
      return;
    }
    const encoded = pcmWavBase64(samples, sampleRate);
    if (capture.kind === "test") {
      await testSpeakerAudio(encoded);
    } else {
      await addSpeakerEnrollmentSegment(capture.index, capture.label, encoded);
    }
  }
  onBeforeUnmount(() => {
    controlsDisposed = true;
    captureGeneration += 1;
    cancelServerCapture();
    // Release any segments already uploaded to the short-lived server session
    // when the settings view itself is closed.
    void cancelSpeakerEnrollment({ notify: false });
  });
  watch([activePage, activeSettingsTab], ([page, tab], [oldPage, oldTab]) => {
    if (oldPage === "settings" && oldTab === "speaker" && (page !== "settings" || tab !== "speaker"))
      void cancelSpeakerEnrollment({ notify: false });
  });
  function pageLabel() {
    return activePage.value === "home" ? "工作台" : "设置";
  }
  function runtimeAction() {
    if (coreUnavailable.value) {
      applicationHost.inspect();
      showApplicationSystem();
      return false;
    }
    return performRuntimeAction();
  }
  // workbenchConversation（消息呈现缓存）：实时音量更新不重新映射聊天条目。
  const workbenchConversation = computed(() => {
    const conversation: WorkbenchModel["conversation"] =
      conversationEntries.value.map((entry) => ({
        key: entry.key,
        role: entry.role,
        text: entry.text || "（没有可显示的转写）",
        time: formatTime(entry.at),
        status:
          entry.role === "user"
            ? turnStatusLabel(entry)
            : speechStatusLabel(entry),
        speaking: entry.speaking,
        delivered: entry.role === 'assistant' && entry.delivery?.status
          ? deliveryStatusLabel(entry.delivery) +
            (entry.delivery.heard_text ? " · " + entry.delivery.heard_text : "")
          : "",
      }));
    for (const entry of liveSpeechEntries.value)
      conversation.push({
        key: entry.key,
        role: "assistant",
        text: entry.text,
        time: "本轮",
        status: pendingSpeechStatusLabel(entry),
        speaking: entry.speaking,
      });
    if (liveUserText.value)
      conversation.push({
        key: "current-user",
        role: "user",
        text: liveUserText.value,
        time: "本轮",
        status: "已完成识别 · 等待回复",
      });
    if (liveAssistantText.value)
      conversation.push({
        key: "current-assistant",
        role: "assistant",
        text: liveAssistantText.value,
        time: "本轮",
        status: runtimeState.value === "speaking" ? "正在播报" : "等待播报结算",
        speaking: runtimeState.value === "speaking",
      });
    return conversation;
  });
  const workbenchModel = computed<WorkbenchModel>(() => {
    return {
      state: runtimeState.value,
      audioLevels: voice.audioLevels?.value ?? [0, 0, 0, 0, 0],
      sessionActive: connected.value && listening.value, // sessionActive（真实会话开关）：等待唤醒时仍保持会话布局。
      title: runtimeMeta.value.label,
      description: runtimeMeta.value.description,
      action: applicationSnapshot.value?.startup_complete === false
        ? "组件启动中"
        : coreUnavailable.value
        ? "查看本地组件"
        : connected.value
          ? listening.value
            ? "停止当前会话"
            : "开始语音会话"
          : "重试连接",
      disabled: loading.value || applicationSnapshot.value?.startup_complete === false,
      modeDisabled: !connected.value || applicationSnapshot.value?.startup_complete === false,
      canReset: connected.value && !listening.value && applicationSnapshot.value?.startup_complete !== false,
      mode: reasoningMode.value,
      latency: formatResponseLatency(latencyMs.value),
      latencyPending: latencyRunning.value,
      voiceModel: modelTitle(activeModels.value.voice),
      shadowModel:
        shadow.value.backend === "dsh"
          ? "代理宿主管理"
          : modelTitle(activeModels.value.shadow),
      ttsModel: modelTitle(activeTtsModel.value),
      conversation: workbenchConversation.value,
      partial: current.value.partial_text,
      thinking:
        runtimeState.value === "thinking" && !current.value.assistant_text,
      conversationHint: waitingForWake.value
        ? "正在等待唤醒词"
        : listening.value
          ? "正在聆听你的声音"
          : connected.value
            ? "语音由本机服务处理"
            : "等待本机服务",
      voiceContext: conversationContext.value
        ? formatTokens(conversationContext.value.usedTokens) +
          " / " +
          formatTokens(conversationContext.value.limitTokens)
        : "—",
      voiceCache:
        conversationCacheHitRatio.value === null
          ? "—"
          : Math.round(conversationCacheHitRatio.value * 100) + "%",
      shadow: {
        title:
          shadow.value.summary ||
          (shadow.value.available ? "等待本轮协作摘要" : "后台协作暂不可用"),
        status: shadowDisplayStatus.value,
        focus: shadowFocusText.value,
        context: shadowContext.value
          ? formatTokens(shadowContext.value.used_tokens) +
            " / " +
            formatTokens(shadowContext.value.limit_tokens)
          : "—",
        cache:
          shadowCacheHitRatio.value === null
            ? "—"
            : Math.round(shadowCacheHitRatio.value * 100) + "%",
        trace: shadowRunningTrace.value.map((item) => ({
          key: item.run_seq + "-" + item.item_id,
          title: item.stream === "tool"
            ? (item.tool_name ? `工具 · ${item.tool_name}` : "工具调用")
            : "DSH 思考",
          text: item.stream === "thinking" ? item.text || "" : "",
          status: shadowActivityPhaseLabel(item),
          time: formatTime(item.timestamp),
        })),
        event: shadowLatestEventActivity.value
          ? shadowActivityText(shadowLatestEventActivity.value)
          : "",
      },
    };
  });

  return {
    applicationSnapshot,
    showApplicationSystem,
    voice,
    activePage,
    activeSettingsTab,
    settings,
    saving,
    connected,
    runtimeState,
    shadow,
    wakeForm,
    wakeTasks,
    addingWakeTask,
    wakeTaskCreateDraft,
    addWakeTask,
    createWakeTask,
    editingWakeTaskId,
    wakeTaskDraft,
    refreshWakeTasks,
    editWakeTask,
    saveWakeTask,
    removeWakeTask,
    maxWakeKeywords,
    addWakeKeyword,
    removeWakeKeyword,
    silenceSeconds,
    activePersona,
    personaDocuments,
    customInstructions,
    providerForm,
    providerStep,
    discoveredModels,
    modelAssignments,
    availableLlmModels,
    visibleLocalPool,
    hiddenPool,
    registry,
    computeStatus,
    computeSavedDevices,
    refreshComputeStatus,
    saveComputeSettings,
    modelScanState,
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
    saveWake,
    saveSilence,
    quietHoursForm,
    quietStatus,
    saveQuietHours,
    saveShadowGate,
    savePersona,
    scanModels,
    discoverProvider,
    verifyCustomModel,
    addVerifiedProvider,
    setNotice,
    setError: adapter.setError,
    speakerForm,
    speakerStatus,
    speakerTestResult,
    speakerEnrollment,
    saveSpeaker,
    startSpeakerEnrollment,
    commitSpeakerEnrollment,
    cancelSpeakerEnrollment,
    deleteSpeakerTemplate,
    setSpeakerTemplateCentroid,
    refreshAmbientContext,
    clearAmbientContext,
    computeModelRows,
    formatComputeBytes,
    deviceLabel,
    requestedDeviceLabel,
    computeChangePending,
    hasUnsupportedGpuChoice,
    serviceLabel,
    showApplyConfirm,
    expandedModelSections,
    editingModelId,
    deleteCandidateId,
    modelEditDraft,
    roleLabels,
    llmAssignmentSlots,
    personaItems,
    instructionDraft,
    instructionFormError,
    editInstruction,
    clearInstructionDraft,
    stageInstruction,
    addInstructionRow,
    saveInstructionSettings,
    removeInstruction,
    modelContextOptions,
    reasoningOptions,
    currentDocument,
    modelPoolSummary,
    apiModelPool,
    hiddenApiModelPool,
    hiddenLocalPool,
    formatTime,
    formatTokens,
    providerClass,
    selectedModel,
    modelStatusLabel,
    formatContext,
    reasoningSummary,
    modelPolicySummary,
    toggleModelSection,
    isEditableApiModel,
    startEditingModel,
    toggleReasoningLevel,
    saveModelEdit,
    requestDeleteModel,
    confirmDeleteModel,
    toggleHidden,
    openApplyDialog,
    confirmApply,
    selectDiscoveredModel,
    speakerRecordingSegment,
    speakerTestRecording,
    speakerRecordingSeconds,
    speakerServerCapture,
    speakerServiceLabel,
    speakerRollingSummary,
    speakerTemplates,
    ambientRecords,
    speakerHasProfile,
    speakerNeedsReenrollment,
    speakerStaleReason,
    speakerFilterAllowed,
    speakerEnrollmentComplete,
    speakerStateLabel,
    speakerTestReasonLabel,
    speakerTestWindowSummary,
    restoreSpeakerDefaults,
    beginSpeakerRecording,
    beginSpeakerTest,
    speakerPrompt,
    speakerSegmentLabel,
    stopSpeakerRecording,
    cancelActiveSpeakerCapture,
    workbenchModel,
  };
}
