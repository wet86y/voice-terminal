# TTS（语音合成）运行模式

Voice Server（语音服务）当前固定使用 MiniMax API（在线语音接口）合成已提交的文本块，并负责文本分段、调度、播放、中断结算和历史记录。当前产品模型配置继续使用 `speech-2.8-hd`（语音模型）和 `female-shaonv`（音色）。设置页不提供服务商切换，模型分配也不能更改当前 TTS profile（语音配置）。

旧产品设置中的 `tts.backend`（TTS 运行后端）如果为 `cosyvoice`，加载时会改为并写回 `minimax`。TTS API（在线语音接口）未配置或不可用时，服务会报告错误，不会回退到本地模型。

连接或 `task_started`（任务已启动）确认前发生临时网络故障，或 MiniMax 返回可重试的 `408`、`425`、`429`、`5xx` 状态码时，客户端会丢弃失效连接并新建连接重试一次。文本提交后不会自动重发，避免网络中断时重复播报；认证、参数等明确拒绝不会重试。只有错误明确指向不支持的音色情绪字段时，才会去掉该可选字段后重新建连。

## 只读状态

`GET /api/v1/tts/status` 返回固定的 `backend: "minimax"`（当前语音后端）和本地 Worker（工作进程）状态。产品快照中的 `runtime.tts_worker`（本地 Worker 状态）保留作诊断信息；正常产品运行不会启动该 Worker。

模型分配接口保留 `tts_profile_id`（当前 TTS 语音配置 ID）用于读取和恢复现有配置。请求可以省略该字段以沿用已保存的配置；尝试更换时会被拒绝。

## 本地兼容实现

`TtsBackendRouter`（TTS 兼容路由器）、CosyVoice 客户端、Worker（工作进程）和安装脚本仍保留，供本地模型调试完成后重新接入产品运行时。当前产品入口不会启动或切换到该实现。

需要单独调试本地 Worker 时，可在 Windows PowerShell 运行 `scripts/setup-cosyvoice.ps1`。要直接检查兼容 Worker 的 PCM（脉冲编码调制）输出、分段回执和取消行为，可运行 `python -m scripts.probe_cosyvoice`；增加 `--play` 可检查实际播放。
