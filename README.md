# Voice Terminal

Voice Terminal 是一个运行在 Windows 上的本地实时语音终端。浏览器只负责控制和展示，麦克风、VAD、ASR、LLM、TTS、播放器和延迟测量都由本地 Voice Server 负责。

当前状态（2026-09-06）：v0.1 是稳定运行基线，v0.2 是当前主开发和验收路径；运行时仍通过独立配置启用，默认配置暂保留 v0.1 作为发布闸门。Bidi TTS、可复用播放窗口、AEC/降噪状态、受保护真人插话门控、原子播放快照和已传达历史已经接入。当前自动化基线为 Python 120 项、前端 10 项，类型检查、构建、编译检查和差异检查均通过。当前账号的 MiniMax Bidi 已完成连接、词字幕字段、句子时间基准、取消屏障和同连接续用 smoke test；真实麦克风、耳机/扬声器和自然语音统计仍需单独验收。

## 能力边界

```text
浏览器 Vue 控制台
        │ WebSocket
        ▼
本机 Voice Server
  ├─ Audio Capture / PCM Player
  ├─ KWS（可选）/ FSMN-VAD
  ├─ TurnController 或 RealtimeController
  ├─ ASR Worker 客户端
  ├─ DeepSeek 客户端
  └─ MiniMax TTS 客户端
        │ WebSocket
        ├──────── ASR Worker（Paraformer Streaming）
  └──────── Analysis Worker（Smart Turn / SenseVoice，按配置启用）
```

默认配置使用手动开始模式：`wake.enabled=false`，点击 `session.start` 后直接进入 VAD/ASR。KWS 仍然支持 `hei claw`，但需要显式打开。浏览器不调用 `getUserMedia`，不上传 PCM，不运行模型，也不根据时间戳自行计算延迟。

v0.1 提供稳定的单轮语音对话链路：VAD/ASR → LLM → 文本桥接 → TTS → PCM 播放。v0.2 在此基础上增加 Smart Turn、SenseVoice 结束后精修、语音情绪上下文、持续讲话期间的预测和只读文件工具预取、generation/revision 隔离、Bidi TTS、受保护真人插话和按实际传达前缀结算的历史。

## 快速开始

环境要求：Windows、Python 3.12、Node.js/npm，以及项目配置所需的模型和 API Key。模型默认放在 `D:\模型\voice-terminal`，真实模型不会进入 Git。

安装后端依赖和模型：

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe
```

在仓库根目录创建 `.env.local`（该文件已被 Git 忽略）：

```powershell
@"
DEEPSEEK_API_KEY=你的_DeepSeek_Key
MINIMAX_API_KEY=你的_MiniMax_Token
MINIMAX_API_HOST=https://api.minimaxi.com
VOICE_WAKE_ENABLED=false
"@ | Set-Content -Encoding utf8 .env.local
```

检查本机环境：

```powershell
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe
```

启动默认 v0.1 后端：

```powershell
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

启动 v0.2 示例配置（需要 Analysis Worker 模型）：

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe -Config config\v0.2.yaml
```

另开终端启动前端：

```powershell
npm install
npm run dev
```

浏览器打开 Vite 输出的本地地址，默认通常是 `http://127.0.0.1:5173/`。前端 WebSocket 地址可通过 `.env.local` 中的 `VITE_VOICE_WS_URL` 修改。

## 配置规则

配置按以下顺序生效：

1. `config/default.yaml` 提供基础值。
2. 启动参数指定的 YAML（例如 `config/v0.2.yaml`）按对象递归覆盖基础值。
3. `.env.local` 只补充没有出现在进程环境中的变量。
4. 已存在的进程环境变量优先于 `.env.local`，特定环境变量还会覆盖 YAML 中对应字段。

ASR Worker 地址统一来自最终的 `asr.worker_url`，或 `ASR_WORKER_URL`/`ASR_WORKER_HOST`/`ASR_WORKER_PORT` 覆盖。Voice Server、ASR readiness probe 和 v0.2 回放脚本都使用这个最终地址。Analysis 地址来自 `v02.analysis_url`。

`config/default.yaml` 中 `v02.enabled=false`。启用 v0.2 不会自动改变默认配置；只有显式使用 `config/v0.2.yaml` 或其他启用 v0.2 的配置时，RealtimeController、Analysis、预测、工具、Bidi TTS 和受保护插话才会生效。

## 运行检查和回放

查看 ASR Worker 应用层 readiness：

```powershell
python -m voice_terminal.asr.probe --url ws://127.0.0.1:8766/asr --timeout 2
```

查看 Analysis Worker readiness：

```powershell
Invoke-RestMethod http://127.0.0.1:8767/health | ConvertTo-Json -Depth 5
```

使用已有录音进行 v0.2 进程间回放。回放使用内存播放器，不能代表物理扬声器：

```powershell
.\scripts\replay-v02.ps1 `
  -Python D:\DevTools\Python312\python.exe `
  -Config config\v0.2.yaml `
  -Audio logs\evaluation\audio\query-first12.wav `
  -Output logs\evaluation\audit-replay.json
```

回放报告只记录协议状态、事件计数、延迟和音频进度等摘要；`logs/` 已被 Git 忽略。长音频夹具和离线分段命令见 [后端安装与联调](docs/backend-setup.md)。

## 验证结果

完整当前证据见 [项目审计报告](docs/project-audit.md) 和 [当前验证记录](docs/v0.2-validation.md)。主要结论：

- Python 120 项测试通过，前端 10 项测试通过。
- `npm run typecheck`、`npm run build`、Python compileall 和 `git diff --check` 通过。
- 本机 ASR Worker health 为 READY，真实回放产生 partial/final，Analysis 两路可用。
- v0.2 的轮次、generation、revision、工具预取、Bidi/旧 TTS 调度、播放快照、受保护插话、传达历史、断连恢复、重连清理和慢订阅者边界已有确定性回归。
- v0.2 已通过真实 MiniMax Bidi 的短文本 smoke test，但仍未完成完整多轮、真实设备和自然语音统计验收，因此默认配置暂保持 v0.1。

运行自动化检查：

```powershell
python -m pytest -q
npm run typecheck
npm run test
npm run build
python -m compileall -q voice_terminal apps tests
git diff --check
```

## 协议和前端约定

前后端统一使用 WebSocket Envelope。客户端命令必须包含非空 `request_id`；服务端事件的 `seq` 是当前连接内递增的整数；`payload` 必须是对象。前端先按 seq 去重，再按 `turn_id`、v0.2 的 `generation_id` 和 `input_revision` 丢弃过期事件。`turn.completed` 是历史记录的唯一写入口，前端不自行拼接历史或计算延迟。

完整定义见 [WebSocket 协议](docs/websocket-protocol.md)。稳定 v0.1 行为见 [v0.1 规范](docs/v0.1-spec.md)，v0.2 能力边界见 [v0.2 规范](docs/v0.2-spec.md)。

## 文档索引

```text
docs/
├── backend-setup.md       Windows 后端安装、启动、health 和回放
├── websocket-protocol.md  Voice Server 与 Vue 控制台协议
├── v0.1-spec.md           默认 v0.1 链路边界和验收条件
├── v0.2-spec.md           当前主实时链路能力和约束
├── v0.2-validation.md     当前验证结果、证据和限制
├── project-audit.md       最近一轮代码、逻辑和协议审计
├── v0.2-next-steps.md     后续自然语音、设备和发布目标
├── tts-scheduling.md      可复用 TTS 播放窗口与 Bidi direct 策略
└── voice-interruption-bidi-execution.md  本次实施清单和验收记录
```

## 后续目标

后续工作按验收依赖推进：

1. 准备至少 20 条有人工核对文本的自然语音，分别测量 v0.1/v0.2 的 CER、误结束率、结束至首音 p50/p95、回退率和资源峰值。
2. 在耳机和扬声器条件下验证播放前续说、播放中断、多轮连续使用、停止和重启；内存播放器时间不能替代真实设备时间。
3. 在稳定的第三方服务窗口复测 MiniMax Bidi 的字幕字段、时间单位、flush/cancel 顺序、2205 重试和连续多轮；区分 provider 问题与本地调度问题。
4. 只有当旧轮数据不泄漏、自然语音没有明显退步、真实设备路径稳定且 provider 限制可接受时，才评估是否把 v0.2 切换为默认运行配置。

明确不在当前路线中的能力包括声纹注册、本地强制对齐模型、HTTP TTS 迁移、长期记忆、RAG、外部账号操作、shell 工具和文件写操作。
