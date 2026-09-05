# Windows Voice Server 后端安装与联调

## 运行边界

后端只监听本机回环地址：

```text
Voice Server: ws://127.0.0.1:8765/ws
ASR Worker:   ws://127.0.0.1:8766/asr（以 `asr.worker_url` 最终配置为准）
```

当前运行配置默认关闭唤醒词（`wake.enabled: false`），点击 `session.start` 后直接创建语音 turn 并进入 VAD / ASR。KWS 原始关键词仍保留，重新设置 `wake.enabled: true` 后才会启用 `hei claw`：

```text
HEI CLAW @HEI_CLAW
```

最终 `keywords.txt` 由 sherpa-onnx 的 `text2token` 工具生成，不能手写音素。

## 安装顺序

1. 使用 Python 3.12 执行 `scripts/setup.ps1`。
2. 脚本从 CUDA Wheel 源安装 Torch/torchaudio，再安装 Voice Server 依赖。
3. 下载 sherpa-onnx KWS、Paraformer streaming 和 FSMN-VAD 到 `D:\模型\voice-terminal`。
4. 运行 `scripts/doctor.ps1` 检查模型、GPU、音频设备、端口和密钥。
5. 使用 `scripts/start.ps1` 启动 ASR Worker 和 Voice Server。
6. 再运行 Vue 前端：`npm run dev`。

真实模型不进入 Git。模型目录中会生成 `manifests/model-manifest.json`，记录下载来源、文件尺寸和 SHA-256。

## 密钥

后端启动时会自动读取仓库根目录的 `.env.local`，并且不会覆盖已经存在的进程环境变量。建议把本机密钥持久化在这个文件中；它已被 `.gitignore` 忽略，不会提交到 Git：

```powershell
# .env.local（示例，不要提交真实值）
DEEPSEEK_API_KEY=你的_DeepSeek_Key
MINIMAX_API_KEY=你的_MiniMax_中国区_Token_Plan_Key
MINIMAX_API_HOST=https://api.minimaxi.com
VOICE_WAKE_ENABLED=false
VOICE_LOG_LEVEL=DEBUG
```

也可以临时使用 PowerShell 环境变量；显式设置的进程变量优先于 `.env.local`。启动、诊断和 ASR Worker 都会加载同一个 `.env.local`，不需要每次重新输入 Key。

DeepSeek 默认使用 `https://api.deepseek.com`、`deepseek-v4-flash`、流式 Chat Completions，并显式关闭 thinking。

MiniMax 默认使用中国区 `https://api.minimaxi.com/ws/v1/t2a_v2`、`speech-2.8-hd` 和 PCM。当前播放器只接受 PCM；如果账户权限不支持该模型或格式，诊断会报告错误，不静默播放 MP3。

## 设备配置

默认配置使用当前机器上检测到的设备：

```yaml
audio:
  input_device: "麦克风阵列 (适用于数字麦克风的英特尔® 智音技术)"
  output_device: "扬声器 (Realtek(R) Audio)"
```

如果设备名称不同，将 `config/local.yaml.example` 复制为 `config/local.yaml`，修改 `input_device` 和 `output_device`。也可以填写 PortAudio 设备索引。

## 诊断命令

```powershell
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe -Online
.\scripts\doctor.ps1 -Python D:\DevTools\Python312\python.exe -Json
```

`-Online` 会检查 DeepSeek 鉴权和 provider 配置；MiniMax 真实模型/权限仍会在第一次 `task_start` 时由服务端最终确认，因为不发送文本的探测请求不能完整模拟一次 TTS 任务。

## 启动日志

```text
logs/voice-server.log
logs/asr-worker.log
logs/asr-worker.out.log
logs/asr-worker.err.log
```

`voice-server.log` 和 `asr-worker.log` 是应用自身的 UTF-8 轮转日志，默认保留 5 个 10 MB 文件；`VOICE_LOG_LEVEL=DEBUG` 会记录更细的音频块、ASR 推理、LLM delta、TTS PCM 块和 turn 生命周期，但不会记录 API Key 或原始 PCM 内容。两个 `.out/.err` 文件是 PowerShell 启动器的重定向输出。如果 ASR Worker 无法启动，优先检查 `asr-worker.log`、`asr-worker.err.log`、模型目录和 `doctor.ps1` 输出。Voice Server 即使没有模型或密钥也会启动控制面，但会在 `runtime.snapshot` 中标记对应服务为 `ERROR`，并将 `can_start` 设为 `false`。

## 实时链路

唤醒模式开启时，麦克风 PCM 先进入 2 秒环形缓冲，再由 sherpa-onnx 等待 `hei claw`；KWS 触发后暂停 KWS。当前默认的手动模式不运行 KWS，`session.start` 直接创建 turn。两种模式都使用 250 ms 最小有效语音和 700 ms 结束静音，ASR Worker 返回 partial/final；final 文本进入 DeepSeek，文本桥接器按标点和等待时间分段，MiniMax 音频块进入独立 PCM 播放队列。v0.2 示例配置还会按整个待播放 PCM 的剩余时长调度 TTS：剩余不超过 2000 ms 时释放下一段完整文本。

每一轮都有唯一 `turn_id`。取消或打断时会取消 LLM、关闭 TTS、清空播放器，并丢弃旧 turn 的迟到文本和音频。
# v0.2 可选分析和 Agent

现有默认配置保留 v0.1。安装新依赖与固定 revision 的模型：

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
# 仅当下载代理过慢时，对下载进程关闭代理；不会改系统代理。
python -m voice_terminal.analysis.model_setup --no-proxy
python -m voice_terminal.analysis.model_setup --offline
```

启用完整 v0.2（docs 为文件工具唯一默认允许根）：

```powershell
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe -Config config/v0.2.yaml
```

启动脚本按最终配置解析 ASR Worker 的完整 WebSocket 地址（host、port、path），使用应用层 health 确认 Paraformer 已加载；不再使用固定端口的 TCP 可连接作为 READY。已有且 READY 的 Worker 会复用；health 未通过但端口被占用时脚本会停止并报告原因，不会误杀其他进程。`-NoWorker` 仍会先检查外部 Worker 的 health，`-NoAnalysis` 使用外部运行的分析服务。脚本只终止自己启动的子进程，日志写入 `logs/`。

ASR readiness 探针可单独运行：

```powershell
python -m voice_terminal.asr.probe --url ws://127.0.0.1:8766/asr --timeout 2
```

探针发送带 `request_id` 的 health 请求，只输出 READY/错误类别、`model_loaded` 和耗时，不输出密钥或音频。Voice Server 连接成功后也会执行同一应用层检查；模型未就绪、health 超时、协议错误和已有连接断开分别映射到可恢复的 `ASR_WORKER_NOT_READY`、`ASR_WORKER_TIMEOUT`、`ASR_WORKER_PROTOCOL_ERROR` 和 `ASR_WORKER_DISCONNECTED`。停止监听再启动时会重新连接并 health。

Analysis Worker 的 health 可用于确认两个小模型是否实际加载：

```powershell
Invoke-RestMethod http://127.0.0.1:8767/health | ConvertTo-Json -Depth 5
```

在 v0.2 配置下，`refine.model` 应为 `SenseVoiceSmall`，并且 `refine.status=READY`、`refine.model_loaded=true`。SenseVoiceSmall 同一个模型同时提供最终文字、语言、情绪和声音事件标签；项目没有另外拉取一个独立的“情绪 1”模型。结束后这些分段标签会以 `speech_emotion.segments`（兼容保留 `audio_cues`）进入最终 LLM user 消息；流式预测阶段没有这项信息是正常的，因为精修要等本轮结束后才执行。

用于真实进程间回放（内存播放器，不代表物理扬声器）的脚本：

```powershell
.\scripts\replay-v02.ps1 `
  -Python D:\DevTools\Python312\python.exe `
  -Config config\v0.2.yaml `
  -Audio logs\evaluation\audio\query-first12.wav,logs\evaluation\audio\query-second8.wav `
  -Output logs\evaluation\replay-worker-query.json
```

该脚本从配置读取 ASR/Analysis 地址，先检查应用层 health；缺少服务时只启动本次回放需要的 Worker，结束时只关闭自己记录的 PID。`-NoWorker` 和 `-NoAnalysis` 可用于验证外部服务和 Analysis 降级。回放报告只保存协议状态、事件、时间线和计数，不保存 API Key 或原始 PCM。

长音频离线夹具可使用：

```powershell
python scripts\create-long-eval-audio.py
python -m voice_terminal.analysis.evaluate `
  --manifest logs\evaluation\long-stage4-manifest.json `
  --url ws://127.0.0.1:8767/analysis `
  --output logs\evaluation\sensevoice-long-stage4-matrix.json `
  --allow-over-max
```

`--allow-over-max` 只允许离线检查 60–62 秒输入，不改变运行时 `max_turn_ms=60000`；音频夹具和报告均在 Git 忽略目录中。

Smart Turn 使用 onnxruntime CPU；SenseVoice 使用 FunASR CPU，tokenizer 从模型字节加载，支持 Windows 中文目录。默认 v0.2 精修预算 3000 ms，超过预算使用 Paraformer，原生计算结束后槽位才可复用。

新能力开关位于 v02：enabled、smart_turn、sensevoice、predictions、tools、tts_pacing 和 tts_lead_ms。`tts_pacing` 在 `config/v0.2.yaml` 中打开时，第一段文本立即进入 MiniMax，后续文本只有在当前轮全部待播放音频剩余不超过 `tts_lead_ms` 时才发送。默认文件根为 docs，`allowed_roots` 可增加显式目录；API 工具名为 files_search / files_read_text，对应内部 files.search / files.read_text。没有 shell 或写操作。

可重复的合成中文回放（不是自然麦克风验收）：

```powershell
.\scripts\create-eval-audio.ps1
python -m apps.server.analysis_worker --config config/v0.2.yaml
python -m voice_terminal.analysis.evaluate --manifest logs/evaluation/audio/manifest.json
```

录音、报告保存在 Git 忽略的 logs/evaluation；该脚本使用本机合成语音，不记录用户麦克风。报告区分计算耗时与真实讲话到播放延迟。
