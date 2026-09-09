# V0.6 语音链路可靠性审计

V0.6 以 JSON 响应信封取代 DeepSeek 原生工具调用，但不移除已验证的输入、TTS、播放和交付能力。

| 业务能力 | 唯一 owner | 自动化证据 |
| --- | --- | --- |
| 400ms 单飞调度与事件合并 | `AgentEpisodeRuntime + AgentDecisionScheduler` | 调度、退避、恢复测试 |
| 必填语音决策 | `SpeechDecisionEnvelopeStream` | JSON 分片、前端参数半校验、完整信封与非法动作测试 |
| 文本候选与 generation | `EpisodeSpeechOutput` | `empty/queued/suspended/speaking`、抢占状态测试 |
| TTS 隔离与零 PCM | `EpisodeSpeechOutput + MiniMaxTtsSession` | flush、cancel、新建会话、一次重试 |
| 播放中改口 | generation 替换 + `DeliveryResolver` | 旧输出结算、新文本直接 TTS、用户反打断测试 |
| 只读动作资料 | `ActionRunner + SessionLedger` | 三个 schema、失败资料和下一 tick 测试 |
| 真人插话、停止和 reset | `EpisodeController + ConversationRuntime` | 生命周期和 ASR 断连测试 |

## 发布边界

- 用户讲话时，语音候选只保存文本；端点确认前绝不启动 TTS 或持有 PCM。
- 正常 ASR final、SenseVoice 精修和动作完成不会取消在飞 LLM；只有 stop、barge-in、reset、传输清理和播放中实质改口会显式取消。
- `cancel` 立即停止待播或正在播放的输出；`wait` 完全不改变事务。讲话中 `speak(text)` 仍只保存文本，不启动 TTS。
- 播放中收到首个合法且非空的 `speak` 文本前缀时，输出层原子关闭旧 PCM generation 并直接启动新的 TTS transaction；旧 provider cancel/close 在后台完成，迟到 PCM 不得进入新 playback key。
- 如果已启动的流式 `speak` 随后完整 JSON 或动作校验失败，停止这条临时新输出，并使用一次既有恢复请求纠正协议；旧输出不会恢复或续播。
- DeepSeek thinking 请求使用 `response_format=json_object`，不传递 provider native tools；工具资料仅经本地 `actions` 和不可信账本来源块进入下一请求。

真实设备仍须完成连续五轮、SenseVoice 修正、动作后回复、播放中改口、耳机/扬声器插话和停止监听验收。
