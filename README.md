# Voice Terminal V0.6

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器只负责控制与可视化；音频采集、VAD、ASR、Smart Turn、SenseVoice、Agent、工具、TTS、播放和延迟均由 Voice Server 负责。

## 当前运行模型

系统只有一条统一运行链：`SessionRuntime → EpisodeController → ConversationScheduler + SpeechQueue`。核心关联字段为 `conversation_id`、`turn_id`、`request_id`、`speech_id` 和 `output_generation`；它们只用于追踪。普通事件不会因为文本修订或工具晚到而失效，只有停止监听、用户反打断、会话重置和播放中 `speak(text)` 才会关闭显式取消作用域。

普通 LLM 请求只由 400ms 调度 tick 放行；异常协议恢复独立调度，全程最多一条 LLM 请求在飞。Agent 每轮以 DeepSeek JSON mode 返回必填的 `{"speak":{"state":"speak|cancel|wait"},"actions":[]}` 信封，不传递 provider native tools。`speak.state/text/emotion` 通过前缀校验后即可进入文本语音队列；完整 JSON 与 `actions` 仍需最终校验。播放中收到新的合法非空 `speak` 前缀时，系统先将新文本与当前完整播报文本做严格前缀比对：相同前缀只追加超出的 suffix，不停止或重播当前内容；已接受文本范围内出现差异时才立即替换旧播报。替换 generation 的旧 provider 取消在后台完成；`cancel` 会立即停止输出。当前输出完成后保留 1 秒追加窗口，窗口内的 suffix 排在当前播放后；窗口外仍只合成新增文本，不重播已听前缀。若追加前缀已经合法但后续完整协议无效，已追加文本保留，并触发一次恢复请求。讲话期间队列只保存文本，不预生成或保存 PCM，端点确认后才启动 TTS。会话上下文来自唯一的传达感知账本和 `LLMContextProjector`：最终用户输入、实际听到的语音前缀、被打断说明与不可信工具来源块。

所有 Agent 请求固定启用 DeepSeek thinking、`reasoning_effort: low` 与 `response_format=json_object`。情绪枚举只接受 `calm`、`happy`、`sad`、`angry`、`fearful`、`disgusted`、`surprised`；兼容性别名 `friendly` 会安全映射为 `calm`，并在下一次模型轮询声明为轻度协议修复。推理原文只经本地调试 Web UI 的 `agent.reasoning` 事件显示，不写服务端日志、Agent trace、DeliveryReport 或持久会话历史。上下文精确 tokenizer 在后台预热，未完成预热时使用本地 fallback 估算，不阻塞实时事件循环。V0.6 是破坏性主线：不读取旧 prediction 配置，不映射原生 `tool_calls` / `speak(queue|modify)` 或旧 WebSocket 事件。

破坏性升级的规则是“删除旧接口，不删除仍需的能力”：每项被移除实现的能力必须先迁移到唯一、可达且有行为测试的新主链 owner；旧配置和协议则直接失败，不做静默映射。完整准则见[运行与质量门槛](docs/operations-and-quality.md#破坏性升级与能力保全)。

## 启动

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
npm run dev
```

在根目录的已忽略 `.env.local` 中配置 `DEEPSEEK_API_KEY`、`MINIMAX_API_KEY` 和可选的 `MINIMAX_API_HOST`。浏览器默认连接 `ws://127.0.0.1:8765/ws`，不请求麦克风权限、不上传 PCM、不自行计算延迟。

真实耳机/扬声器、自然语音统计与第三方服务稳定性仍是独立发布门槛，不能由 fake provider 或内存播放器代替。

详见：[架构与组件契约](docs/architecture.md)、[WebSocket V0.6 协议](docs/websocket-protocol.md)、[上下文与工具规范](docs/context-and-tools.md)、[V0.6 可靠性审计](docs/v06-reliability-audit.md)、[运行与质量门槛](docs/operations-and-quality.md)。
