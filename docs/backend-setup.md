# Windows Voice Server 后端安装与联调

## 运行边界

后端只监听本机回环地址：

```text
Voice Server: ws://127.0.0.1:8765/ws
ASR Worker:   ws://127.0.0.1:8766/asr
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

唤醒模式开启时，麦克风 PCM 先进入 2 秒环形缓冲，再由 sherpa-onnx 等待 `hei claw`；KWS 触发后暂停 KWS。当前默认的手动模式不运行 KWS，`session.start` 直接创建 turn。两种模式都使用 250 ms 最小有效语音和 700 ms 结束静音，ASR Worker 返回 partial/final；final 文本进入 DeepSeek，文本桥接器按标点和等待时间分段，MiniMax 音频块进入独立 PCM 播放队列。

每一轮都有唯一 `turn_id`。取消或打断时会取消 LLM、关闭 TTS、清空播放器，并丢弃旧 turn 的迟到文本和音频。
