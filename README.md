# Voice Terminal V0.5

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器只负责控制与可视化；音频采集、VAD、ASR、Smart Turn、SenseVoice、Agent、工具、TTS、播放和延迟均由 Voice Server 负责。

## 当前运行模型

系统只有一条 V0.5 运行链：`SessionRuntime → EpisodeController → AgentEpisodeRuntime + SpeechQueue`。核心业务身份为 `conversation_id`、`turn_id` 和 `speech_id`；统一 `epoch` 判断异步任务是否仍属于当前语义世界，模型决策 ID 仅用于本地 trace。

只有 Agent 明确调用 native `speak` 的正文可以进入 TTS；`set_prefill` 只在输入 finalized 后进入同一 Speech Queue。会话上下文来自唯一的传达感知账本和 `LLMContextProjector`：最终用户输入、用户实际听到的语音前缀、被打断说明与已接纳的工具来源块。运行时身份、epoch、播放器状态和临时预填充绝不进入模型上下文；重置会创建新对话并清空账本。

所有 Agent 请求固定启用 DeepSeek thinking 和 `reasoning_effort: low`。推理原文只经本地调试 Web UI 的 `agent.reasoning` 事件显示，不写服务端日志、Agent trace、DeliveryReport 或持久会话历史。V0.5 是破坏性主线：不读取旧 prediction 配置、不映射旧 WebSocket 事件，也不保留 V0.4 回退路径。

破坏性升级的规则是“删除旧接口，不删除仍需的能力”：每项被移除实现的能力必须先迁移到唯一、可达且有行为测试的新主链 owner；旧配置和协议则直接失败，不做静默映射。完整准则见[运行与质量门槛](docs/operations-and-quality.md#破坏性升级与能力保全)。

## 启动

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
npm run dev
```

在根目录的已忽略 `.env.local` 中配置 `DEEPSEEK_API_KEY`、`MINIMAX_API_KEY` 和可选的 `MINIMAX_API_HOST`。浏览器默认连接 `ws://127.0.0.1:8765/ws`，不请求麦克风权限、不上传 PCM、不自行计算延迟。

真实耳机/扬声器、自然语音统计与第三方服务稳定性仍是独立发布门槛，不能由 fake provider 或内存播放器代替。

详见：[架构与组件契约](docs/architecture.md)、[WebSocket V0.5 协议](docs/websocket-protocol.md)、[上下文与工具规范](docs/context-and-tools.md)、[V0.5 可靠性审计](docs/v05-reliability-audit.md)、[运行与质量门槛](docs/operations-and-quality.md)。
