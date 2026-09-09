# V0.6 语音链路可靠性审计

V0.6 以 JSON 响应信封取代 DeepSeek 原生工具调用，但不移除已验证的输入、TTS、播放和交付能力。

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
- 播放中收到首个合法且非空的 `speak` 文本前缀时，先冻结当前完整播报文本并做严格字符前缀比对。新文本完整覆盖旧文本时只提取 suffix，当前 PCM 不停止、不清空、不重播；已接受文本范围内出现差异时，输出层才原子关闭旧 PCM generation 并启动新的 TTS transaction，旧 provider cancel/close 在后台完成，迟到 PCM 不得进入新 playback key。
- 当前输出耗尽后保留 1 秒追加窗口。窗口内的同前缀扩展会进入不重播前缀的后续队列；窗口外仍只合成新增 suffix。`speech.append_waiting_prefix`、`speech.appended`、`speech.append_successor` 和 `append_window_*` 进度事件用于重建该路径。
- 如果已追加的流式 `speak` 随后完整 JSON 或动作校验失败，已获合法前缀授权的追加文本保留，并使用一次既有恢复请求纠正协议；已播放旧内容不会恢复或续播。
- DeepSeek thinking 请求使用 `response_format=json_object`，不传递 provider native tools；工具资料仅经本地 `actions` 和不可信账本来源块进入下一请求。

真实设备仍须完成连续五轮、SenseVoice 修正、动作后回复、同前缀长播报追加、文本分歧改口、耳机/扬声器插话和停止监听验收。追加验收应确认新事件中只有 suffix，且没有因普通补充产生 `speech.replaced` 或 `local_playback_stopped`。
