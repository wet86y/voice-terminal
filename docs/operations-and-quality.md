# 运行与质量门槛

> **当前状态：生产运行。** 默认活动 LLM 路径为 DeepSeek Responses + strict `json_schema`；冻结的 Chat/`json_object` adapter 只作为显式整体回退入口。快速、均衡和深度思考模式均属于生产主链：均衡模式可请求一次本地 `request_thinking` 续请求，深度模式使用 `effort=low`。普通影子双跑仍不属于生产路径。

默认 `config/default.yaml` 是当前唯一活动运行线。`smart_turn`、`agent` 与 `tools` 是能力配置，不是版本开关。文件工具默认仅限 `docs`；不包含 Shell、写文件、外部账号操作、RAG 或长期记忆。

思考模式在 episode 创建时冻结：`fast` 为 `effort=none` 且不暴露思考 action，`balanced` 的基础请求为 `effort=none` 并可通过本地 `request_thinking` 获得一次 `effort=low` 续请求，`deep` 直接使用 `effort=low`。`request_thinking` 不是 provider native tool；它不能执行外部操作，且新输入、停止监听和 session reset 必须立即取消其等待或续请求。`config/local.yaml` 是本机覆盖文件；正常请求的 Schema 可见性和实际 effort 仍以启动时冻结的 episode `reasoning_mode` 为准，故该文件不能作为生产基线证据。

默认 `audio.output_device: null`，由 Windows 选择当前活跃默认输出设备。这样可避免固定的 MME 端点在 PortAudio 实际打开 32 kHz PCM 流时失败，也能自然适应耳机和外接音箱切换。只有部署环境需要强制固定设备时，才在被忽略的 `config/local.yaml` 中设置 `audio.output_device`；修改后应先以真实输出流验证，而不只依赖设备能力查询。

运行检查：

```powershell
python -m pytest -q
npm run typecheck --workspace @voice-terminal/web
npm run test --workspace @voice-terminal/web -- --run
npm run build --workspace @voice-terminal/web
python -m compileall -q voice_terminal apps tests
git diff --check
```

调度器验收必须确认当前只有一个 `AgentDecisionScheduler` 和一个 Agent `_run()` 循环。400ms
heartbeat 使用从上一个 deadline 推进的固定时间轴，不能等待 active LLM、TTS 播放或 provider
返回，也不能因为 heartbeat 本身增加 provider 请求。普通事件在400ms边界合并并优先于同边界
的强制窗口；无普通语义事件时才允许第3、6、9、16、22窗口的 `forced_poll`，第25窗口的
`final_silence` 只能消费一次。普通窗口忙碌时跳过，最终窗口忙碌时保留到恢复空闲后补发。

TTS 开始、结束、取消、替换、失败和语音活动不属于普通调度事件，不重置用户静默锚点，也不
触发 provider；TTS 状态只负责阻止实际 dispatch。普通、`forced_poll` 和 `final_silence` 三类
请求必须共用 `_step()`、`_provider_stream()`、原有 schema/transport/reasoning 和 TTS 输出协议。
system-check 必须使用独立 `system_check_id`，不创建用户 `InputRecord` 或用户 turn 结算；只有
已被听到的输出才进入 assistant ledger。reset、stop 和 episode end 后应清理 heartbeat、runner、
active request 与 scheduler 状态。

软件通过后仍必须在真实设备完成：[V0.6 可靠性审计](v06-reliability-audit.md)
定义的核心 10 条人工验收，覆盖自然语音、耳机和扬声器下的打断、连续多轮、
停止/重置、真实 ASR/Analysis/LLM/TTS 链路，以及结束至实际首音的 p50/p95。
内存播放器结果不能替代这些证据。

## LLM 上下文缓存质量门槛

新增任何模型字段前，必须先完成[缓存契约的字段准入表](llm-context-cache-contract.md#标准字段准入表)，明确语义 owner、lane、持久化、schema 与 UI 影响。涉及 provider payload、上下文、schema 或动作目录的改动必须运行缓存 placement/digest 单测和完整 Python 回归。

官方 DeepSeek 直连验收要连续记录多轮 `input_tokens`、`cached_tokens` 与 hit ratio，确认命中会随 append-only 历史增长；中转端点的结果单独记录。若端点未报告 cache usage，状态为不可观测，不得写成或口头解释为“零命中”。

## 语音运行排障

当用户报告“播放后监听退出”或 `audio capture gap` 时，先按以下顺序判断，
不要仅凭 UI 的监听按钮推断原因：

1. 查 `audio.capture_progress`：`dropped=0` 且队列水位稳定时，优先排查控制器缓存连续性，不要归因为声卡丢帧。
2. 查 `controller.input_frame_failed`、`controller.input_capture_read_failed` 与 `AUDIO_*` 事件。前者是可恢复帧处理故障；后者才表示无法继续读取麦克风。
3. 对照 `audio.player_first_pcm_submitted`、`audio.player_drained` 和对应的 `speech_id`。播放阶段不能自动成为 `session.stop` 的理由。
4. 查 WebSocket 命令日志；只有明确的 `command=session.stop` 才能归因为用户停止。恢复性错误、ASR 重连和输出结算必须保持与用户停止不同的状态投影。
5. 查 `agent.speak_context`：新轮应为 `owner_scope=none|previous_turn` 且 `continuation_allowed=False`；只有同轮活动 handle 才允许 `current_turn/True`。若紧随其后出现 `CROSS_TURN_SPEECH_PREFIX_STRIPPED`，说明模型返回了累计旧前缀，但本地已在 TTS 前安全剥离。日志中的 `baseline_chars/new_chars/suffix_chars` 可用于核对，不需要记录会话正文。

受保护插话拒绝的麦克风帧仍必须写入当前轮的有界 PCM 缓存，以避免下一帧
被误判为逻辑缺口；详见[V0.6 可靠性审计](v06-reliability-audit.md)。

## 破坏性升级与能力保全

项目允许破坏性升级：旧配置、旧公开事件、旧类型和旧实现可以直接删除，**但删除实现不等于删除产品能力**。每次版本收敛必须先做能力归属检查，再开始移除工作。

1. 为每个将删除的模块列出它提供的用户可见能力、可靠性边界和协议职责；没有明确归属的模块不得删除。
2. 每项仍需保留的能力必须在新主链有唯一、可达的 owner，并能从生产装配入口追踪到它。例如，V0.6 JSON 决策属于 `AgentEpisodeRuntime + SpeechDecisionEnvelopeStream`，本地动作属于 `ActionRunner + SessionLedger`；TTS 取消与复用属于 Speech Queue / TTS Session Manager，而不是旧单轮回答链。
3. 先迁移或新增行为级测试，再删除旧实现。测试应验证能力结果和安全边界，不能只断言旧类、旧函数或旧事件仍存在。
4. 删除后，旧配置键必须失败、旧协议不得被静默映射、旧接口不得由 `try/except` 或别名回退继续支持。运行时可靠性恢复（断连重建、取消屏障、zero-PCM 安全重试）属于当前版本能力，不属于兼容回退。
5. 合并前必须完成 Python/Web 回归、生产构建、静态搜索和真实设备验证；静态搜索需确认旧实现不再被生产装配入口构造或导入。

升级评审应优先回答两个问题：**“这段代码承载的能力现在由谁唯一负责？”**以及 **“该能力在新主链中如何被测试证明？”**。若回答不完整，应保留或先迁移能力，而不是为了代码整洁提前删除。
