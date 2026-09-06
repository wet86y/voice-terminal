# Windows Voice Server 后端安装与联调

本文档描述当前仓库的安装、配置、启动、健康检查和 v0.2 回放方式。后端只监听本机回环地址，浏览器不直接连接 ASR/Analysis Worker。

## 服务地址

默认地址如下，ASR 和 Analysis 的最终地址以配置为准：

```text
Voice Server:    ws://127.0.0.1:8765/ws
Voice health:    http://127.0.0.1:8765/health
ASR Worker:      ws://127.0.0.1:8766/asr
Analysis Worker: ws://127.0.0.1:8767/analysis
Analysis health: http://127.0.0.1:8767/health
```

默认运行手动模式：`wake.enabled=false`，点击 `session.start` 后进入 VAD/ASR。打开 `wake.enabled=true` 后，KWS 使用 `hei claw` / `HEI_CLAW`，麦克风先进入约 2 秒 Ring Buffer，再等待唤醒词。

## 环境要求和安装

- Windows；
- Python 3.12；
- Node.js/npm（前端）；
- 与配置匹配的模型缓存；
- DeepSeek 和 MiniMax API Key。

模型默认目录为 `D:\模型\voice-terminal`，模型和密钥不进入 Git。安装后端依赖和 v0.1 模型：

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe
```

运行当前 v0.2 主路径所需的 Analysis Worker：

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
```

模型下载完成后会写入 manifest，失败下载使用 `.part` 文件，不会把未完成文件当成有效模型。

## 密钥和环境变量

推荐在仓库根目录创建 `.env.local`。它已被 `.gitignore` 忽略：

```powershell
@"
DEEPSEEK_API_KEY=你的_DeepSeek_Key
MINIMAX_API_KEY=你的_MiniMax_Token
MINIMAX_API_HOST=https://api.minimaxi.com
VOICE_WAKE_ENABLED=false
"@ | Set-Content -Encoding utf8 .env.local
```

配置优先级为：默认 YAML → 启动参数指定的 YAML → `.env.local` 补充 → 已存在的进程环境变量覆盖。常用覆盖项：

```text
VOICE_MODEL_DIR
VOICE_SERVER_HOST / VOICE_SERVER_PORT
ASR_WORKER_URL 或 ASR_WORKER_HOST / ASR_WORKER_PORT
MINIMAX_API_HOST
VOICE_WAKE_ENABLED
VITE_VOICE_WS_URL（前端）
```

DeepSeek 默认使用 `https://api.deepseek.com` 的流式接口并关闭 thinking。v0.1 使用传统 MiniMax T2A WebSocket；`config/v0.2.yaml` 显式选择 `/ws/v1/t2a_v2_bidi`、`direct` 调度、`speech-2.8-hd` 和 32 kHz 单声道 PCM。播放器只接受 PCM，不会静默把 MP3 当成 PCM。

## 音频设备

默认配置包含当前机器的设备名称：

```yaml
audio:
  input_device: "麦克风阵列 (适用于数字麦克风的英特尔® 智音技术)"
  output_device: "扬声器 (Realtek(R) Audio)"
```

设备名称不一致时，复制 `config/local.yaml.example` 为 `config/local.yaml`，只覆盖 `audio.input_device` 和 `audio.output_device`；也可以填写 PortAudio 设备索引。v0.2 要求 16 kHz、单声道、int16 采集。

## 诊断、启动和停止

```powershell
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe -Online
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe -Json
```

启动默认 v0.1：

```powershell
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

启动 v0.2：

```powershell
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe -Config config\v0.2.yaml
```

启动脚本会：

1. 从最终配置读取 ASR/Analysis 地址；
2. 通过应用层 health 确认 ASR 模型 READY；
3. 只为当前脚本启动的 Worker 保存 PID；
4. 退出时只清理自己启动的进程。

`-NoWorker` 要求外部 ASR Worker 已通过 health；`-NoAnalysis` 使用外部 Analysis 或允许 v0.2 降级。现有用户进程不会被强制关闭。

## 健康检查

ASR readiness probe：

```powershell
python -m voice_terminal.asr.probe --url ws://127.0.0.1:8766/asr --timeout 2
```

探针发送带 `request_id` 的 health 请求。只有响应 `status=READY` 且 `model_loaded=true` 才算就绪；超时、断连、协议错误和未加载分别报告明确错误类别。

Analysis health：

```powershell
Invoke-RestMethod http://127.0.0.1:8767/health | ConvertTo-Json -Depth 5
```

启用 v0.2 时，实际启用的 lane 必须为 READY。`refine.model` 通常为 `SenseVoiceSmall`；它同时提供最终文字、语言、情绪和声音事件标签，不存在另一个独立的“情绪模型”。精修发生在本轮结束后，实时预测阶段没有情绪字段是预期行为。

## 日志

常用日志：

```text
logs/voice-server.log
logs/asr-worker.log
logs/asr-worker.out.log
logs/asr-worker.err.log
```

应用日志使用 UTF-8 轮转文件。`VOICE_LOG_LEVEL=DEBUG` 会记录更细的生命周期和音频进度，但不应写入 API Key、原始 PCM 或完整敏感上下文。控制面即使无法初始化模型或密钥也会启动，但 `runtime.snapshot` 会将服务标记为 ERROR，`can_start=false`。

## v0.2 回放

回放使用内存播放器，只证明进程间协议和软件队列，不代表物理扬声器：

```powershell
.\scripts\replay-v02.ps1 `
  -Python D:\DevTools\Python312\python.exe `
  -Config config\v0.2.yaml `
  -Audio logs\evaluation\audio\query-first12.wav `
  -Output logs\evaluation\audit-replay.json
```

脚本先检查 ASR/Analysis health；缺少服务时只启动本次需要且由脚本拥有的进程。`-NoWorker`、`-NoAnalysis` 可用于外部服务或降级验证。报告存放在 Git 忽略的 `logs/evaluation/`，不保存 API Key 或原始 PCM。

离线音频夹具和 Analysis 检查：

```powershell
.\scripts\create-eval-audio.ps1 -OutputDir logs\evaluation\audio
python -m voice_terminal.analysis.evaluate `
  --manifest logs\evaluation\audio\manifest.json `
  --url ws://127.0.0.1:8767/analysis `
  --output logs\evaluation\analysis-matrix.json `
  --allow-over-max
```

`--allow-over-max` 只用于离线检查超过运行时上限的夹具，不改变 `max_turn_ms=60000`。运行时 SenseVoice 单段不超过 30 秒，超过 60 秒的用户输入会封口，后续讲话进入新轮。

## v0.2 配置和工具边界

v0.2 开关位于 `v02`：`enabled`、`smart_turn`、`sensevoice`、`predictions`、`tools`、`web_search` 和 `tts_local_buffer_ms`。`web_search` 只有在 `tools=true` 且自身为 `true` 时才会尝试暴露；`tools=false` 会关闭全部工具，`web_search=false` 则只保留文件工具。TTS 传输与调度位于顶层 `tts.transport` 和 `tts.scheduling`：示例使用 Bidi + direct，短文本持续进入服务端句子缓冲；旧 WebSocket 仍可通过 `v02.tts_pacing` 与 `v02.tts_lead_ms` 使用可复用的 2 秒播放窗口策略。`audio.processing_enabled` 启用 AEC/降噪，并在处理后的 10 ms 帧上使用 `webrtcvad-wheels==2.0.14` 做近端语音确认；缺少任一依赖或初始化失败时状态会显示 `DEGRADED`，受保护自动插话保持关闭。`aec-audio-processing==1.0.1` 的 Windows wheel 暴露的 `has_voice()` 不能作为此确认信号使用，因此不要删除独立 WebRTC VAD 依赖。

播放器本地 PCM 队列（`tts_local_buffer_ms`）和旧文本播放窗口（`tts_lead_ms`）是两个独立设置。Bidi 音频接收还会使用可配置的落盘暂存上限，示例为每个活动 generation 64 MiB。

文件工具默认只允许 `docs`：`files_search` 和 `files_read_text`。它们没有 shell、写操作或外部账号访问；`allowed_roots` 只能通过显式配置增加目录。

启用 `config/v0.2.yaml` 中的 `v02.web_search: true` 后，语音 Agent 还会获得 MiniMax Token Plan 的 `web_search(query)`。搜索客户端复用 `tts.base_url`、`tts.api_key_env`（默认即 `MINIMAX_API_KEY`）以及 `MINIMAX_API_HOST` 覆盖逻辑，不新增独立密钥配置；MiniMax Token Plan key 与普通按量 API key 不是同一类资源，key 还必须与区域 host 匹配。缺少 key、host 配置异常或明确鉴权失败时，服务状态会变为 `DISABLED`，搜索 schema 会从后续请求移除，但不会阻止语音会话启动；普通超时和临时 5xx 只让本次工具调用失败。
非临时的非法响应会将搜索服务标记为 `ERROR` 并移除后续 schema；重启服务后会重新按配置初始化。

`web_search` 只返回有界的搜索摘要和相关搜索建议，不抓取网页全文、不打开链接，也不执行摘要或页面中的指令。搜索结果会作为不可信数据提供给 DeepSeek，最终语音回答不会朗读搜索过程、原始 JSON、工具参数或协议内容。预测预取可能在用户完成讲话前发起搜索，因此启用该开关即表示相关查询会发送到 MiniMax。默认诊断和自动化测试不会发起付费搜索；真实联网 smoke test 需要单独显式执行。

## 前端启动

```powershell
npm install
npm run dev
```

前端默认连接 `ws://127.0.0.1:8765/ws`，修改 `VITE_VOICE_WS_URL` 后刷新页面即可。
