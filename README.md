# Voice Terminal

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器只负责控制与可视化；音频采集、VAD、ASR、Smart Turn、SenseVoice、Agent、工具、TTS、播放和延迟均由 Voice Server 负责。

## 当前运行模型

系统只有一条运行链：`SessionRuntime` 协调输入、会话账本、Agent 决策、只读工具和语音输出。用户输入拥有 `input_id`，跨输入 Agent 会话拥有 `episode_id`，每次模型决策拥有 `decision_id`，每次授权语音拥有 `speech_id`。

只有 native `speak` 工具的正文可以进入 TTS。会话上下文来自唯一的传达感知账本：最终用户输入、用户实际听到的语音前缀、被打断说明与当前决策选中的工具来源块。重置会清空账本；系统不保存跨会话长期记忆。

## 启动

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
npm run dev
```

在根目录的已忽略 `.env.local` 中配置 `DEEPSEEK_API_KEY`、`MINIMAX_API_KEY` 和可选的 `MINIMAX_API_HOST`。浏览器默认连接 `ws://127.0.0.1:8765/ws`，不请求麦克风权限、不上传 PCM、不自行计算延迟。

真实耳机/扬声器、自然语音统计与第三方服务稳定性仍是独立发布门槛，不能由 fake provider 或内存播放器代替。

详见：[架构与组件契约](docs/architecture.md)、[WebSocket V1 协议](docs/websocket-protocol.md)、[上下文与工具规范](docs/context-and-tools.md)、[运行与质量门槛](docs/operations-and-quality.md)。
