# V0.6 语音链路可靠性审计

V0.6 以 JSON 响应信封取代 DeepSeek 原生工具调用，但不移除已验证的输入、TTS、播放和交付能力。

> 本文同时保留切换前的可靠性基线。当前项目已进入 Responses + strict `json_schema` 协议切换后的 Bug 检修期；下文涉及 Chat Completions/`json_object` 的内容均属于冻结历史路径，不代表当前活动配置。

| 业务能力 | 唯一 owner | 自动化证据 |
| --- | --- | --- |
| 400ms 单飞调度与事件合并 | `AgentEpisodeRuntime + AgentDecisionScheduler` | 调度、退避、恢复测试 |
| 必填语音决策 | `SpeechDecisionEnvelopeStream` | JSON 分片、前端参数半校验、完整信封与非法动作测试 |
| 文本候选、前缀追加与 generation | `EpisodeSpeechOutput` + `AgentEpisodeRuntime` | `empty/queued/suspended/speaking`、严格前缀追加、抢占状态测试 |
| TTS 隔离与零 PCM | `EpisodeSpeechOutput + MiniMaxTtsSession` | flush、cancel、新建会话、一次重试 |
| 情绪协议容错 | `SpeechDecisionEnvelopeStream + AgentEpisodeRuntime` | `friendly → calm` 映射、下一轮轻度反馈测试 |
| 上下文 token 冷启动 | `AgentEpisodeRuntime` | 后台 tokenizer prewarm、实时路径 fallback 估算测试 |
| 播放中改口与连续补充 | generation 替换/追加后续项 + `DeliveryResolver` | 旧输出结算、suffix 不重播、文本分歧替换、用户反打断测试 |
| 只读动作资料 | `ActionRunner + SessionLedger` | 三个 schema、失败资料和下一 tick 测试 |
| 真人插话、停止和 reset | `EpisodeController + ConversationRuntime` | 生命周期和 ASR 断连测试 |

## 发布边界

- 用户讲话时，语音候选只保存文本；端点确认前绝不启动 TTS 或持有 PCM。
- 正常 ASR final、SenseVoice 精修和动作完成不会取消在飞 LLM；只有 stop、barge-in、reset、传输清理和播放中实质改口会显式取消。
- `cancel` 立即停止待播或正在播放的输出；`wait` 完全不改变事务。讲话中 `speak(text)` 仍只保存文本，不启动 TTS。
- 播放中收到首个合法且非空的 `speak` 文本前缀时，先按 `conversation_id + turn_id` 检查 speech 所有权。同轮新文本完整覆盖旧文本时只提取 suffix，当前 PCM 不停止、不清空、不重播；跨轮活动文本只作为 `observed_text`，不得调用 `append_successor`。已接受的同轮文本范围内出现差异时，输出层才原子关闭旧 PCM generation 并启动新的 TTS transaction，旧 provider cancel/close 在后台完成，迟到 PCM 不得进入新 playback key。
- 当前输出耗尽后保留 1 秒追加窗口。窗口内的同前缀扩展会进入不重播前缀的后续队列；窗口外仍只合成新增 suffix。`speech.append_waiting_prefix`、`speech.appended`、`speech.append_successor` 和 `append_window_*` 进度事件用于重建该路径。
- 如果已追加的流式 `speak` 随后完整 JSON 或动作校验失败，已获合法前缀授权的追加文本保留，并使用一次既有恢复请求纠正协议；已播放旧内容不会恢复或续播。
- 完成播放后，assistant 的实际 `heard_text` 继续进入 ledger 供后续语义推理，但不再作为活动 continuation target。最近一次同会话 full delivery 仅用于检测异常跨轮累计；“已听文本 + 非空后缀”会在 TTS 前剥离旧前缀，完全相同文本保留显式复述能力，不同会话和 partial delivery 不参与比较。
- 当前活动快照不得借用其他 speech handle 的 `last_delivery`。无法证明与当前 `speech_id` 绑定的 `heard_text` 必须省略，避免把上一轮交付错误投影到新目标。
- 冻结历史路径曾对 DeepSeek thinking 请求使用 `response_format=json_object`。当前活动路径改为 Responses + strict `json_schema`，基础请求保持 `effort=none`，仍不传递 provider native tools；工具资料仅经本地 `actions` 和不可信账本来源块进入下一请求。预留的 `request_thinking(low)` 在检修期保持关闭。

真实设备仍须完成连续五轮、SenseVoice 修正、动作后回复、同前缀长播报追加、文本分歧改口、耳机/扬声器插话和停止监听验收。跨轮验收应确认 `owner_scope` 从 `current_turn` 正确切换为 `none/previous_turn`，新 TTS 只收到本轮文本；若模型仍累计输出，必须出现 `CROSS_TURN_SPEECH_PREFIX_STRIPPED` 且 suffix 之前没有旧 PCM。追加验收应确认同轮新事件中只有 suffix，且没有因普通补充产生 `speech.replaced` 或 `local_playback_stopped`。
