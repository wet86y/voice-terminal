# 运行与质量门槛

> **当前状态：协议切换后的 Bug 检修期。** 默认活动 LLM 路径已经切换为 DeepSeek Responses + strict `json_schema`；冻结的 Chat/`json_object` adapter 只作为显式整体回退入口。检修期不启用普通影子双跑或 `request_thinking`，也不把当前默认配置视为已经完成生产验收。

默认 `config/default.yaml` 是当前唯一活动运行线。`smart_turn`、`agent` 与 `tools` 是能力配置，不是版本开关。文件工具默认仅限 `docs`；不包含 Shell、写文件、外部账号操作、RAG 或长期记忆。

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

软件通过后仍必须在真实设备完成：[V0.6 可靠性审计](v06-reliability-audit.md)
定义的核心 10 条人工验收，覆盖自然语音、耳机和扬声器下的打断、连续多轮、
停止/重置、真实 ASR/Analysis/LLM/TTS 链路，以及结束至实际首音的 p50/p95。
内存播放器结果不能替代这些证据。

## 语音运行排障

当用户报告“播放后监听退出”或 `audio capture gap` 时，先按以下顺序判断，
不要仅凭 UI 的监听按钮推断原因：

1. 查 `audio.capture_progress`：`dropped=0` 且队列水位稳定时，优先排查控制器缓存连续性，不要归因为声卡丢帧。
2. 查 `controller.input_frame_failed`、`controller.input_capture_read_failed` 与 `AUDIO_*` 事件。前者是可恢复帧处理故障；后者才表示无法继续读取麦克风。
3. 对照 `audio.player_first_pcm_submitted`、`audio.player_drained` 和对应的 `speech_id`。播放阶段不能自动成为 `session.stop` 的理由。
4. 查 WebSocket 命令日志；只有明确的 `command=session.stop` 才能归因为用户停止。恢复性错误、ASR 重连和输出结算必须保持与用户停止不同的状态投影。

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
