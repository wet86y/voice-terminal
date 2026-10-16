# 本地 TTS 解码器计算图生命周期修复与重测

日期：2026-10-16

## 变更内容

混合音色样音试听使用 `full_decode`（完整解码），解码器计算图会扩展到覆盖整段音频的容量。此前该大容量 CUDA Graph（CUDA 计算图）会留在 VoxCPM2 会话中；紧接着进行正式流式合成时，小句段可能继续使用不合适的大图。

修复在每个 `full_decode`（完整解码）请求退出时释放 AudioVAE（音频变分自编码器）解码器计算图，包括请求正常完成和异常退出的路径。后续正式流式请求按首个句段的实际尺寸重建小图，再由后续句段复用。该处理仅释放解码器计算图，不主动清除编码器图或音色参考缓存；原有 `mem_saver`（节省显存）清理逻辑仍然生效。

实现位于 audio.cpp 原生源码提交 `1895381df44bc95f6ee5c186871a161385adb5a4`，涉及 `audiovae.h`、`audiovae.cpp` 和 `session.cpp`。应用资源目录已更新到运行版本 `v0.9.1-1895381df44b`，原生程序 SHA-256（安全哈希算法）为 `cf691668249763a922108e572355ee0a436160c42c6606b37872aa2b2a19efcf`。

## 重启服务后的验证

第一次重测期间发现另一 TTS（文本转语音）会话并行占用 GPU（图形处理器），并出现播放器欠载，因此未用作验收。该会话退出后，通过应用的 `SessionRuntime`（会话运行时）、TTS 后端路由、音色任务、正式语音输出事务和 `PcmPlayer`（PCM 脉冲编码调制播放器）重新启动隔离原生服务并重测。测试采用参考音频 `暴力小小妹12.wav`，混合音色描述为“保留参考音频的整体音色特征，音调稍高一些，发声清晰自然。”测试目录使用只读硬链接复用已安装模型，测试设置、音色库和日志单独保存。

| 字段 | 结果 |
| --- | --- |
| `server_binary_sha256`（原生服务程序 SHA-256 摘要） | `cf691668249763a922108e572355ee0a436160c42c6606b37872aa2b2a19efcf` |
| `text_chars`（正式文本字符数） | 293 |
| `segment_count`（正式句段数） | 19 |
| `audio_duration_s`（输出音频时长，秒） | 51.36 |
| `first_half_mean_rtf`（前半段平均实时因子） | 0.6237 |
| `second_half_mean_rtf`（后半段平均实时因子） | 0.6376 |
| `underrun_ms`（播放器欠载时长，毫秒） | 0 |
| `underrun_episodes`（播放器欠载片段数） | 0 |
| `byte_exact`（入队与回调采样逐字节一致） | 是 |
| `application_playback_degraded_warning`（应用播放降级警告） | 否 |
| `acoustic_continuation`（声学续接） | 已启用；19 个句段沿用同一上下文，后续 18 段均命中原生缓存 |

原生追踪日志显示，混合音色试听把 `latent_capacity`（潜变量容量）扩至 256；正式合成首段重新构建容量为 16 的解码器图，余下正式句段均复用容量为 16 的图。正式输出事务完成，续接上下文在结束时释放。

## 验收结论与范围

本次干净重测满足播放器无欠载、采样一致、声学续接有效和解码器图从 256 帧回落到 16 帧的验收条件。该结论针对解码器图生命周期和本次应用播放链路。

本次没有评估混合音色随文本推进出现的升调或音色漂移；该问题由另一会话处理，不作为本修复的结论。测试仅覆盖一条约 300 字文本和一次混合音色生成。

## 本地测试记录

- 测试报告：`.local/cache/reports/tts-decoder-graph-fix-clean-20261016/report.json`
- 应用日志：`.local/cache/reports/tts-decoder-graph-fix-clean-20261016/application.log`
- 原生追踪：`.local/cache/reports/tts-decoder-graph-fix-clean-20261016/native-trace.log`
- 正式合成追踪：`.local/cache/reports/tts-decoder-graph-fix-clean-20261016/formal-native-trace.log`
- 长文本音频：`.local/cache/reports/tts-decoder-graph-fix-clean-20261016/混合音色长文本正式合成.wav`
