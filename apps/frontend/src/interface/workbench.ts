import type { ReasoningMode, SessionOperation } from "../types";

/** WorkbenchModel（工作台呈现数据）：样例数据和业务状态使用同一模板。 */
export interface WorkbenchModel {
  state: string; // state（运行状态）。
  sessionActive?: boolean; // sessionActive（会话是否进行中，等待阶段也保持开启）。
  audioLevels?: readonly number[]; // audioLevels（五个音频频带的归一化音量，范围 0 至 1；无数据时保持安静）。
  title: string; // title（当前状态名称）。
  description: string; // description（用户可见状态说明）。
  action: string; // action（语音会话操作文案）。
  disabled?: boolean;
  modeDisabled?: boolean; // modeDisabled（离线时禁止切换运行模式，仍可重试连接）。
  canReset?: boolean;
  sessionOperation?: SessionOperation | null; // sessionOperation（正在整理上下文或清除会话）。
  mode: ReasoningMode; // mode（下一轮使用的运行模式，仅快速或深度）。
  latency: string; // latency（真实首音延迟或样例值）。
  latencyPending?: boolean;
  voiceModel: string;
  shadowModel: string;
  ttsModel?: string; // ttsModel（当前语音合成模型）。
  conversation: Array<{
    attachments?: import('../types').ImageAttachment[]; // attachments（双图描述）。
    key: string; // key（贯穿流式与结算阶段的气泡标识）。
    role: "user" | "assistant" | "tool"; // role（消息发送方）。
    text: string; // text（最新完整正文）。
    time: string; // time（消息显示时间）。
    status: string; // status（识别或播报状态文案）。
    speaking?: boolean; // speaking（是否实际播放中）。
    delivered?: string; // delivered（实际播报交付说明）。
    partial?: boolean; // partial（是否为正在识别的用户文本）。
  }>; // conversation（保持服务端顺序的呈现条目）。
  partial?: string; // partial（实时识别的未完成文本）。
  partialKey?: string; // partialKey（识别气泡标识）：与最终用户条目保持一致。
  thinking?: boolean;
  conversationHint: string;
  voiceContext?: string;
  voiceCache?: string;
  shadow: {
    status: string;
    context: string;
    cache: string;
    trace: Array<{
      key: string;
      title: string;
      text?: string;
      status: string;
      time: string;
      approvalId?: string; // approvalId（待决权限标识）。
      runSeq?: number; // runSeq（所属运行序号）。
      approvalPending?: boolean; // approvalPending（权限是否仍待决定）。
    }>;
    approvalBusy?: string; // approvalBusy（正在提交决定的请求）。
    continuousApproval?: boolean; // continuousApproval（当前语音窗口已启用连续授权）。
    approvalWindowActive?: boolean; // approvalWindowActive（当前录音窗口可开启连续授权）。
    event?: string;
  };
}
