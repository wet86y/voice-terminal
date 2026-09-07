# V0.5 语音链路可靠性审计

本表将 V0.3 保留的产品能力映射到 V0.5 唯一 owner 和验证方式。V0.5
不恢复旧协议或旧运行时；这些条目是当前主链必须承载的行为契约。

| 业务能力 | V0.5 owner | 自动化证据 | 现场验收 |
| --- | --- | --- | --- |
| 连续输入、等待与单飞决策 | `AgentEpisodeRuntime` | Agent/会话回归 | 连续多轮输入 |
| PCM 连续性与 ASR 背压 | `RealtimeController + AudioCapture` | 缺口、队列溢出、短帧测试 | 长时间监听 |
| TTS 会话隔离与零 PCM | `EpisodeSpeechOutput + MiniMaxTtsSession` | flush/cancel、新建会话、一次重试 | 首条及连续发声 |
| 已听内容结算 | `PcmPlayer + DeliveryResolver` | 首回调、取消、尾音测试 | 耳机和扬声器听感 |
| 真人插话 | `BargeInGate + AudioPreprocessor` | AEC 可用/不可用矩阵 | 耳机插话、扬声器回声 |
| 停止、重置、断连恢复 | `EpisodeController + SessionRuntime` | 生命周期与 ASR 断连测试 | stop/reset/start |
| 上下文和工具隔离 | `SessionLedger + AgentEpisodeRuntime` | 意图变化、迟到工具、上下文限制 | 多轮工具对话 |

## 可靠性原则

- 一次采集或 ASR 队列缺口只能放弃相关未完成输入，不能关闭监听或静音无关的已授权输出。
- 正式 `speak` 必须基于已定稿输入。端点候选可用于准备工具或 prefill，不能作为正式回答的播放授权。
- Bidi `task_flushed`、`task_canceled` 或任何传输异常后的连接都不复用；已发文本且零 PCM 只允许在未播放时重试一次。
- 播放期间 AEC 或反向参考不可用时禁止自动插话，显式 `session.stop` 始终可用。
- 日志中的“PCM 已提交”仅代表 PortAudio 回调提交，不代表人工已听到；耳机和扬声器验收仍是发布前必需证据。

## 已验证的运行态修复

### 受保护播放不能制造逻辑 PCM 缺口

当扬声器模式下 AEC/近端保护不可用时，插话门会拒绝当前麦克风帧，
防止将助手回声作为用户打断。该拒绝只能影响语义决策，**不能跳过当前
输入工作区的 PCM 连续性账本**。否则全局采集游标会正常前进，但
`CurrentTurnAudioBuffer` 少一个 20 ms 块；TTS 尾部结束后下一帧追加会被
误判为 `audio capture gap`。

`RealtimeController` 因此在调用插话保护前，先将有效采集帧追加到已有
工作区的有界 PCM 缓存。被拒绝的帧不会创建用户轮、不会发送给已冻结的
ASR 子段、也不会取消输出；它只维持样本连续性。若异步 Agent 操作在这段
等待期间替换了工作区，返回后仅向新工作区补写该帧一次。

回归测试必须覆盖“受保护插话返回空活动 + 两个连续 20 ms 帧”，并断言
缓存样本范围连续；运行日志中不应出现由该路径引起的
`controller.input_frame_failed` 或 `AUDIO_CAPTURE_GAP`。

### 诊断与恢复分级

- `AUDIO_CAPTURE_GAP`：物理采集或队列样本确有不连续；放弃当前输入并保持监听。
- `AUDIO_INPUT_FRAME_FAILED`：已读到的有效帧在 VAD/控制器/ASR 队列处理中异常；记录异常类型和样本位置，安全复位当前输入并保持监听。
- `controller.input_capture_read_failed`：无法继续读取物理采集源；这是唯一会结束当前监听的采集失败路径。

排障时需要同时检查 `audio.capture_progress` 的 `queue`/`dropped`、
`controller.input_*_failed` 的堆栈、`audio.player_first_pcm_submitted` 与
`audio.player_drained`。MiniMax 已返回 PCM、播放器已排空并不能证明监听
应当停止，也不能用来把内部恢复误投影成用户的 `session.stop`。

## 核心现场验收（耳机与扬声器）

每条记录设备、音量、`conversation_id`、`turn_id`、`speech_id`、首次 TTS
文本、首次 PCM、`audio.player_first_pcm_submitted`、最终 delivery 和日志中的
取消原因。十条均通过才可将本轮标记为设备验收完成。

1. 冷启动后第一条明确回答：确认人工可听、MiniMax PCM、PortAudio 首提交的顺序完整。
2. 连续五轮回答：确认每轮都建立有效 TTS 事务，且无上一轮残音。
3. 注入零 PCM、超时和取消：确认只在零播放前重试一次，失败不伪报 completed。
4. 耳机播放中持续真人插话：确认可信前缀结算、旧输出停止、新输入仍能识别。
5. 耳机短噪声/拍手：确认不产生插话或新用户轮次。
6. 扬声器且 AEC 禁用：确认播放声不会自动取消 TTS，显式停止仍立即生效。
7. 扬声器且 AEC READY：确认真人持续插话可停止输出，回声不能停止输出。
8. 人为制造采集或 ASR 队列缺口：确认 `AUDIO_CAPTURE_GAP` 可恢复、监听仍为 true、下一句话可识别。
9. 短暂停止并恢复 ASR Worker：确认旧输入/输出正确结算，延迟重连后可开始新轮。
10. 在采集、生成、首 PCM 前和播放中分别执行 stop/reset/start：确认无残音、无旧状态复活、无未处理 Task。
