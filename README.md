# Voice Terminal v0.1

本仓库是本地模块化实时语音终端的前端控制台。前端不采集音频、不运行模型，也不计算延迟；它通过本地 WebSocket 连接 Voice Server，发起持续监听并展示后端状态、转录、LLM 回复和链路指标。

## 当前前端边界

- 页面加载后自动连接 `ws://127.0.0.1:8765/ws`。
- 页面只有一个主要操作按钮：`开始说话`。
- 点击后启动后端持续监听，按钮变为 `停止监听`。
- 麦克风、扬声器、VAD、Paraformer、LLM、TTS 和播放器均由后端负责；KWS 作为可选模块保留，当前默认关闭。
- 当前轮展示 partial/final 转录、LLM 流式回答、TTS 状态和后端音频缓冲。
- 当前轮展示完整后端延迟，历史保留最近 10 轮。
- 没有演示模式、实时模式、前端假数据、前端计时器或本地 fallback。

后端未启动时，页面会显示连接错误并禁用操作按钮，不会自行模拟语音流程。

## 当前已实现的前端行为

前端已经按“后端驱动、只读监控”完成 v0.1 的控制台改造：

- Vue 组件挂载后自动建立一次 WebSocket 连接，并自动发送 `subscribe`。
- 只有收到后端允许启动的状态后，`开始说话` 才会启用。
- 点击按钮只发送 `session.start` 或 `session.stop`；按钮和页面状态不会乐观更新，必须等待 `session.state` 或 `runtime.snapshot`。
- 所有 partial/final 转录、LLM 增量、TTS 状态、音频缓冲和延迟值都直接渲染服务端事件。
- 消息按当前连接内的 `seq` 去重，并按 `turn_id` 隔离当前轮；已完成记录才会进入最近 10 轮历史。
- 连接断开或后端报告错误时只显示真实错误并禁用启动操作，不生成 fallback 数据，也不在后台重连。

浏览器不会调用 `getUserMedia`，因此页面本身不会请求麦克风权限；Windows Voice Server 负责音频设备和完整语音链路。

## 后端联调清单

Voice Server 接入前端时，至少需要满足以下顺序和约束：

1. WebSocket 建立后接收 `subscribe`，随后发送包含完整服务状态和当前内容的 `runtime.snapshot`。
2. `runtime.snapshot.payload.can_start` 决定空闲时 `开始说话` 是否可用；前端不会根据连接成功自行推断服务就绪。
3. 接收 `session.start` 后，先通过 `session.state` 告知真实状态，并在开始新轮时携带新的 `turn_id`。
4. 当前轮的转录、回答、TTS 和 latency 事件都必须携带同一个 `turn_id`；partial 是替换值，assistant 是增量值。
5. 延迟字段必须是后端已计算的毫秒数；尚未产生的值发送 `null`，前端显示为 `—`。
6. 本轮完成只通过 `turn.completed` 落历史；前端不会从其他事件拼装历史记录。
7. 不可恢复故障应发送 `error`，必要时同时发送 `session.state`（`ERROR`）；前端不会替后端切换状态。

完整的字段定义、时序和错误处理见 [docs/websocket-protocol.md](docs/websocket-protocol.md)。

## Windows Voice Server

后端实现位于 `voice_terminal/` 和 `apps/server/`，由 Windows 原生 Python 3.12 运行。真实模型缓存固定为 `D:\模型\voice-terminal`。当前默认 `wake.enabled=false`，点击 `开始说话` 后直接进入 VAD / ASR 语音轮，不再等待 `hei claw`；KWS 模型和关键词配置仍保留，改为 `true` 即可恢复唤醒模式。后端负责麦克风、VAD、ASR Worker、DeepSeek、MiniMax TTS 和 PCM 播放，浏览器仍然只是控制和监控入口。

首次安装（建议显式使用本机 Python 3.12）：

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe
```

首次配置密钥（推荐写入仓库根目录 `.env.local`，该文件已被 Git 忽略，后端会自动加载）：

```powershell
@"
DEEPSEEK_API_KEY=你的_DeepSeek_Key
MINIMAX_API_KEY=你的_MiniMax_中国区_Token_Plan_Key
MINIMAX_API_HOST=https://api.minimaxi.com
VOICE_WAKE_ENABLED=false
VOICE_LOG_LEVEL=DEBUG
"@ | Set-Content -Encoding utf8 .env.local

# 也可只在当前 PowerShell 会话中临时设置；进程环境变量优先级更高
# $env:DEEPSEEK_API_KEY = "..."
# $env:MINIMAX_API_KEY = "..."
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe -Online
```

启动 ASR Worker 和 Voice Server：

```powershell
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

模型下载是幂等的，失败下载会使用 `.part` 文件，不会把未完成文件当成模型。离线检查只验证本地缓存：

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Offline
```

运行期间的详细日志写入 `logs\voice-server.log` 和 `logs\asr-worker.log`；设置 `VOICE_LOG_LEVEL=DEBUG` 后会额外记录音频进度、VAD、ASR、LLM、TTS 和 turn 生命周期，不会写入 API Key 或原始 PCM。

具体后端模块、模型来源、环境排查和中断语义见 [docs/backend-setup.md](docs/backend-setup.md)。

## 启动前端

```powershell
npm install
npm run dev
```

浏览器打开 Vite 输出的本地地址即可。当前默认入口为：

```text
http://127.0.0.1:5173/
```

常用检查命令：

```powershell
npm run typecheck
npm run test
npm run build
```

如果 Voice Server 地址不同，可以复制 [.env.example](.env.example) 为 `.env.local`：

```powershell
Copy-Item .env.example .env.local
```

然后修改：

```dotenv
VITE_VOICE_WS_URL=ws://127.0.0.1:8765/ws
```

## 目录

```text
voice-terminal/
├── apps/
│   ├── web/                       # Vue 控制台
│   │   └── src/
│   │       ├── App.vue            # 页面布局与只读展示
│   │       ├── types.ts           # 前后端协议类型
│   │       └── composables/
│   │           └── useVoiceTerminal.ts
│   └── server/                   # Voice Server / ASR Worker 入口
├── voice_terminal/                # 后端运行时和 provider
├── config/                        # 默认配置和本地配置示例
├── scripts/                       # setup / doctor / start
├── docs/
│   ├── v0.1-spec.md               # v0.1 方案边界
│   └── websocket-protocol.md      # WebSocket 协议
├── package.json                   # npm workspace 入口
└── README.md
```

## WebSocket 约定

消息统一使用：

```json
{
  "type": "latency.snapshot",
  "seq": 42,
  "timestamp": "2026-09-04T08:30:10.123Z",
  "turn_id": "turn_000021",
  "payload": {}
}
```

前端只发送以下命令：

```text
subscribe
session.start
session.stop
```

服务端事件包括：

```text
runtime.snapshot
session.state
transcript.partial
transcript.final
assistant.delta
llm.context
tts.status
latency.snapshot
turn.completed
error
```

详细字段、序号去重和 Turn ID 规则见 [docs/websocket-protocol.md](docs/websocket-protocol.md)。

## 后续工作

下一步实现独立的 Windows Voice Server 和 ASR Worker，并严格按照协议文档推送状态与指标。后端应能够脱离浏览器运行，网页只是查看和控制入口。
