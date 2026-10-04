/** WorkbenchModel（工作台呈现数据）：样例数据和业务状态使用同一模板。 */
export interface WorkbenchModel {
  state: string; // state（运行状态）。
  title: string; // title（当前状态名称）。
  description: string; // description（用户可见状态说明）。
  action: string; // action（语音会话操作文案）。
  disabled?: boolean;
  modeDisabled?: boolean; // modeDisabled（离线时禁止切换运行模式，仍可重试连接）。
  canReset?: boolean;
  mode: string; // mode（下一轮使用的运行模式）。
  latency: string; // latency（真实首音延迟或样例值）。
  latencyPending?: boolean;
  voiceModel: string;
  shadowModel: string;
  ttsModel?: string; // ttsModel（当前语音合成模型）。
  conversation: Array<{
    key: string;
    role: "user" | "assistant";
    text: string;
    time: string;
    status: string;
    speaking?: boolean;
    delivered?: string;
  }>; // conversation（保持服务端顺序的呈现条目）。
  partial?: string; // partial（实时识别的未完成文本）。
  thinking?: boolean;
  conversationHint: string;
  voiceContext?: string;
  voiceCache?: string;
  shadow: {
    title: string;
    status: string;
    focus?: string;
    expanded: boolean;
    context: string;
    cache: string;
    trace: Array<{
      key: string;
      title: string;
      text?: string;
      status: string;
      time: string;
    }>;
    event?: string;
  };
}
