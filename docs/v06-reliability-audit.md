# V0.6 语音链路可靠性基线（历史）

本文件保留 V0.6 切换到 JSON 响应信封时的可靠性基线，以便追溯协议与交付不变量。当前产品
发布基线为 [V0.7 发布审计](v07-release-audit.md)；`voice_decision_v06` 仍是兼容 Schema 名称。

> 本文同时保留切换前的可靠性基线。当前项目已在生产运行 Responses + strict `json_schema` 协议；下文涉及 Chat Completions/`json_object` 的内容均属于冻结历史路径，不代表当前活动配置。

| 业务能力 | 唯一 owner | 自动化证据 |
| --- | --- | --- |
| 单一400ms心跳、普通优先与阶梯强制调度 | `AgentEpisodeRuntime + AgentDecisionScheduler` | 固定 deadline、窗口覆盖、忙碌跳过、最终 pending、单飞和生命周期测试 |
| 必填语音决策 | `SpeechDecisionEnvelopeStream` | JSON 分片、前端参数半校验、完整信封与非法动作测试 |
| 文本候选、前缀追加与 generation | `EpisodeSpeechOutput` + `AgentEpisodeRuntime` | `empty/queued/suspended/speaking`、严格前缀追加、抢占状态测试 |
| TTS 隔离与零 PCM | `EpisodeSpeechOutput + MiniMaxTtsSession` | flush、cancel、新建会话、一次重试 |
| 情绪协议容错 | `SpeechDecisionEnvelopeStream + AgentEpisodeRuntime` | `friendly → calm` 映射、下一轮轻度反馈测试 |
| 上下文 token 冷启动 | `AgentEpisodeRuntime` | 后台 tokenizer prewarm、实时路径 fallback 估算测试 |
| 播放中改口与连续补充 | generation 替换/追加后续项 + `DeliveryResolver` | 旧输出结算、suffix 不重播、文本分歧替换、用户反打断测试 |
| 只读动作资料 | `ActionRunner + SessionLedger` | 三个 schema、失败资料和下一 tick 测试 |
| 真人插话、停止和 reset | `EpisodeController + ConversationRuntime` | 生命周期和 ASR 断连测试 |

## 发布边界

- 用户在 `playback_started=false` 阶段讲话时，普通语音候选进入 `HELD`；保留文本和 handle，不取消在飞 LLM。TTS session 即使已经建立，也不得继续发送文本、拉取新的 provider PCM 或让播放器消费队列；已经进入播放器的未播放 PCM 保留在队列中并由 player hold 阻止消费。唯一例外是静默阶梯产生的未播放 system-check 候选：新一段用户语音开始时立即 supersede，避免在用户说完后补播过期检查结果。
- 普通语音活动、ASR segment/final、SenseVoice 精修和动作完成不会取消在飞 LLM；这些用户信息继续进入 pending mailbox，由默认 400ms scheduler tick 合并处理。只有确认 barge-in、stop、reset、输入不可用、传输清理和播放中实质改口会显式取消。
- Paraformer final 与 SenseVoice refinement 是同一 `turn_id` 下的 `transcript_updates`，各自在到达时独立进入现有调度器；同窗口只自然合并，不互相等待。首次合法更新即使文本相同也必须对模型可见，本地只按 source revision 幂等，不再推断 `confirmed/suffix/rewrite`。模型通过 `available/triggered_by + runtime.speak + assistant HISTORY` 决定 wait、追加、替换或 cancel。晚到 refinement 只能更新 request tail 和诊断/前端转录状态；已经发送给主模型的 user HISTORY frame 在该 Episode 内冻结，绝不因精修回写。
- `cancel` 立即停止待播或正在播放的输出并清理 hold；`wait` 完全不改变事务。`SPEECH_ENDED` 只释放最新且仍有效的 `HELD` 候选并恢复原有文本事务，不重新调用 LLM 或重复发送整段文本；已由用户语音 epoch 判定过期的 system-check 候选不得释放。
- 播放中收到首个合法且非空的 `speak` 文本前缀时，先按 `conversation_id + turn_id` 检查 speech 所有权。同轮新文本完整覆盖旧文本时只提取 suffix，当前 PCM 不停止、不清空、不重播；跨轮活动文本只作为 `observed_text`，不得调用 `append_successor`。已接受的同轮文本范围内出现差异时，输出层才原子关闭旧 PCM generation 并启动新的 TTS transaction，旧 provider cancel/close 在后台完成，迟到 PCM 不得进入新 playback key。
- 当前输出耗尽后保留 1 秒追加窗口。窗口内的同前缀扩展会进入不重播前缀的后续队列；窗口外仍只合成新增 suffix。`speech.append_waiting_prefix`、`speech.appended`、`speech.append_successor` 和 `append_window_*` 进度事件用于重建该路径。
- 如果已追加的流式 `speak` 随后完整 JSON 或动作校验失败，已获合法前缀授权的追加文本保留，并使用一次既有恢复请求纠正协议；已播放旧内容不会恢复或续播。
- 完成播放后，播放器先冻结快照，再进入 1 秒追加窗口。满足 completed、非零 PCM、播放器开始、缓冲清空、设备尾部完成且输出时长覆盖入队时长时，完整播放事实强制得到 `delivery=full`，不能被 provider 字幕降级；完整 speech 文本按 `speech_id` 幂等写入 assistant ledger。handle 清理与 history commit 之间由内部 settling 保持 TTS busy。后续模型请求可通过 assistant HISTORY 确认已经完整传达的文本；播放中或历史提交前的在飞请求只能看到 `runtime.speak` 的当前目标，不能获得之后才发生的完成状态。最近一次同会话 full delivery 默认只用于检测异常跨轮累计，跨轮完整同文仍保留显式复述能力；但本轮由 `USER_INPUT_FINALIZED/REFINED` 重评估触发时，已完成 full delivery 可作为严格逐字基线：同文不再进入 TTS，完整旧前缀后的内容只播放新增 suffix。不同前缀仍按真正修正处理；partial delivery、不同会话和普通 heartbeat 不参与同轮追加比较。播放器确认 PCM 与设备尾部耗尽的 monotonic 时间在 append window 前写入；常驻 KWS 用它和用户最终静默时间的较晚者激活延后的静默锚点。
- 当前活动快照不得借用其他 speech handle 的 `last_delivery`。无法证明与当前 `speech_id` 绑定的 `heard_text` 必须省略，避免把上一轮交付错误投影到新目标。
- 冻结历史路径曾对 DeepSeek thinking 请求使用 `response_format=json_object`。当前活动路径改为 Responses + strict `json_schema`，仍不传递 provider native tools；工具资料仅经本地 `actions` 和不可信账本来源块进入下一请求。快速与均衡模式的基础请求保持 `effort=none`；均衡模式可通过本地 `request_thinking` 触发一次 `effort=low` 续请求，深度模式直接使用 `effort=low`。

### 当前单一400ms调度边界

当前 Agent 由每个 Episode 的 daemon clock thread 使用 `time.monotonic()` 维护绝对400ms heartbeat，线程只通过容量为 1 的合并邮箱投递 pulse。唯一 `_run()` 主 event-loop 循环仍串行推进 scheduler、整理 mailbox 和启动 LLM；它不使用 timeout 模拟心跳，也不等待 TTS 播放。线程不读写会话或 provider 状态，不建立第二个 LLM event loop。`active_request` 始终是独立 task，单请求约束仍为 `max_request_concurrency == 1`。

关闭 KWS 时，每个首次合法的非空 `USER_INPUT_FINALIZED` 或 `USER_INPUT_REFINED` 都会重新开始静默阶梯，不要求本地先证明语义发生变化。常驻 KWS 时同类事件先使旧 generation 失效并记录用户最终静默，不立即让新阶梯计时；在对应 TTS 播放器耗尽，或无音频事务最终结算后，使用 `max(用户静默时间, 输出边界时间)` 激活同一 generation。没有普通语义事件时，第3、6、9、16、22窗口发起 `forced_poll`，第25窗口发起一次 `final_silence`；第10窗口只切换到慢速阶段。普通事件与强制窗口同边界时，普通调度覆盖强制窗口并记录 `covered_by_normal_dispatch`，不会创建 system-check，也不会补发强制请求。普通强制窗口只在 active LLM 时记为 `covered_by_active_request`，或在 provider backoff 时记为 `skipped_backoff`；TTS 和用户讲话不参与该决定。最终窗口在 active LLM 或 backoff 时保留，恢复空闲后只消费一次。

`TTS_*` 和语音活动不会成为普通语义事件，不更新静默锚点，也不会单独触发 provider；它们同时不能作为任何 LLM dispatch 的门槛。常驻 KWS 只通过内部播放器耗尽回调激活已经登记的 pending anchor，不把该回调伪装成 scheduler mailbox 事件。新一段用户语音只递增输出有效性 epoch：不取消普通 LLM，也不重置普通静默阶梯，但会替换 KWS 的 pending anchor、撤销尚未播放的 system-check 候选；迟到的旧 TTS 回调必须同时通过 generation、turn 和 request identity 校验。`forced_poll` 与 `final_silence` 使用互斥的 request-tail 提示词，均复用普通 `_step()`、`_provider_stream()`、provider schema/transport/reasoning 和 `EpisodeSpeechOutput`。`forced_poll` 继续使用正常动作目录、工具可用性和动作派发规则；没有必要时由模型输出 `actions=[]`，而不是由 system-check 强制清空或拒绝合法动作。`final_silence` 保留独立的无动作约束。system-check 保留独立 `system_check_id`，并记录 dispatch 时的 scheduler generation、用户语音 epoch 与 speaking 状态。检查在用户讲话期间发出，或发出后出现新一段用户语音时，其返回只能记录 `SYSTEM_CHECK_STALE`，不得创建或释放语音。它不开始用户 turn、不创建 `InputRecord`，已听到的输出才写入 assistant ledger。

真实设备仍须完成连续五轮、SenseVoice 修正、动作后回复、同前缀长播报追加、文本分歧改口、耳机/扬声器插话和停止监听验收。跨轮验收应确认 `owner_scope` 从 `current_turn` 正确切换为 `none/previous_turn`，新 TTS 只收到本轮文本；若模型仍累计输出，必须出现 `CROSS_TURN_SPEECH_PREFIX_STRIPPED` 且 suffix 之前没有旧 PCM。追加验收应确认同轮新事件中只有 suffix，且没有因普通补充产生 `speech.replaced` 或 `local_playback_stopped`。
