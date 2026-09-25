# TTS（语音合成）运行时切换

Voice Server（语音服务）负责对话状态、文本分段与调度、中断决策、播放、完成结算和历史记录。MiniMax 与 CosyVoice provider（语音供应方）只合成已提交的文本块，并返回 PCM（原始音频）与文本块完成回执。每条语音事务在创建时绑定供应方；切换只影响下一条新事务。

## 本地环境

在 Windows PowerShell 中运行 `scripts/setup-cosyvoice.ps1`。脚本将 Python 3.10、固定版本的 CosyVoice 上游代码、依赖与上游示例参考 WAV 安装到项目内 `.runtime/cosyvoice`；权重仍从配置的模型根目录下 `cosyvoice3-0.5b-2512` 读取。安装不会修改主服务 Python 环境。若模型根目录不同，使用 `-ModelDir` 指定完整的 CosyVoice 权重目录。

启动 Voice Server 后，在设置页的“TTS 语音”中选择供应方。若 Worker（工作进程）启动或健康检查失败，API 返回错误并保留原选择；本地合成失败不会自动转发到 MiniMax。

## 产品接口

`PUT /api/v1/product/settings/tts` 接收 `{ "backend": "minimax" }` 或 `{ "backend": "cosyvoice" }`。`backend`（语音供应方选择）只接受这两个值，旧设置默认为 `minimax`。成功响应包含 `settings.tts.backend`（已保存的供应方）、`effective_from: "next_speech"`（从下一条新建语音事务生效）、`pending: false`（已完成切换）和 `worker`（本地工作进程状态）。

`GET /api/v1/tts/status` 与产品快照的 `runtime.tts_worker` 返回非敏感的 Worker 状态：`stopped`（已停止）、`starting`（启动中）、`ready`（已就绪）、`draining`（等待旧本地事务结束）或 `error`（失败）。失败响应的 `detail.code`（错误代码）与 `detail.message`（中文错误说明）可供设置页显示。

Worker 状态中的 `state` 是运行阶段，`message` 是失败说明，`model_present` 表示模型目录是否存在。语音运行事件和完成记录中的 `provider` 是该事务实际绑定的供应方。`TtsSegmentReceipt`（文本块完成回执）的 `index` 是块序号，`text_chars` 是该块字符数，`end_received_bytes` 是事务内累计 PCM 字节边界；Voice Server 使用该边界结算中断。

本地输出固定转换为 32 kHz 单声道 PCM16（16 位脉冲编码调制）。本地中断结算以 provider 已确认的文本块为精度边界。

## 真实模型验收

在项目根目录运行 `python -m scripts.probe_cosyvoice`，检查首个音频块、连续文本块、完成回执、24→32 kHz 时长和取消响应。增加 `--play` 可通过系统默认扬声器检查实际播放，并以真实回执及播放器停止快照核对打断后的文本块结算；命令结束会关闭 Worker（工作进程）。
