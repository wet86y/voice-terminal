# 语音插话增强、Bidi TTS 接入与已传达上下文执行记录

更新时间：2026-09-06

本文记录“语音插话增强、MiniMax WebSocket Bidi TTS、已传达上下文”计划的实施结果。它与设计说明配套：设计和运行约定见 [`docs/v0.2-spec.md`](v0.2-spec.md)、调度策略复用说明见 [`docs/tts-scheduling.md`](tts-scheduling.md)，协议字段见 [`docs/websocket-protocol.md`](websocket-protocol.md)。

## 目标和已确定边界

本次实现的行为目标是：只有近端真人语音得到确认后才自动插话；确认后立即停止旧回复的本地新增播放并接收下一轮；上一轮写入 LLM 的 assistant 历史只包含可靠的已传达前缀。插话开始到软件停播之间的重叠内容单独记录为不确定，停播后的尾部不作为用户已经听到的内容。

产品边界已经固定：不做本人声纹注册，不承诺排除旁边其他人的讲话；耳机和扬声器都要验收；首版使用约 250 ms 的语音确认窗，不等待 ASR 文字；优先使用 MiniMax Bidi 的字幕边界，无法可靠定位时保守降级；不引入本地强制对齐模型、HTTP TTS 迁移、长期记忆、RAG、自动切换账号区域或外部账号操作。v0.1 仍是默认运行配置，v0.2 通过独立配置启用。

## 阶段完成清单

- [x] **阶段 1：基线、协议核实和执行文档。** 开始实施时保留工作区已有未提交修改，在其上增量读取和修改播放器、两个控制器、MiniMax provider、测试与文档。Bidi 官方文档和当前账号的实际事件已核对，协议事实和限制见下文。
- [x] **阶段 2：抽离旧播放窗口策略。** 新增 `voice_terminal/tts/scheduling.py`。`PlaybackWindowPolicy` 保留有界文本队列、首段发送、片段完成和 `remaining_ms <= lead_ms` 释放规则、超时、取消唤醒、生产结束和诊断；默认提前量仍为 2000 ms。`DirectStreamingPolicy` 与播放器本地 PCM 队列、供应商音频暂存区分开。旧 WebSocket 字段仍可读取，Bidi 示例不再使用旧 pacing 字段。
- [x] **阶段 3：Bidi 适配器和直接流式策略。** `voice_terminal/providers/minimax_tts.py` 保持连接生命周期，支持一次 `task_start`、多轮 `task_continue`/`task_flush`、`task_flushed`、`task_cancel`/`task_canceled`、连接代次隔离、keepalive、受限音频暂存、2205 有关联片段重试和取消唤醒。v0.2 `auto` 解析为 direct；假服务端覆盖正常、多轮、取消、超时、背压和迟到事件。
- [x] **阶段 4：播放时间线、字幕和原子停播。** 播放器新增 callback 消费范围、设备输出估计、尾部延迟和 `stop_and_snapshot(playback_key)`；旧 `block()` 保留为兼容包装。对当前 Bidi 实际返回的 `timestamped_words` 做词级解析，以每个 `sentence_start` 的累计 PCM 位置将句子局部时间换算为任务全局时间；累计快照按词索引替换，临时句子包络在词字幕到达后移除。内部 `SpeechAlignment`/`DeliveryResolver` 仍只接受可验证边界，不能用字数比例伪造定位；未映射尾部被标为 `unheard` 或 `uncertain`。
- [x] **阶段 5：回声消除、降噪和插话门控。** 新增 `AudioPreprocessor` 和 `BargeInGate`。处理链按 10 ms 帧运行，参考播放器实际提交的 PCM；默认降噪、关闭 AGC，使用 `webrtcvad-wheels==2.0.14` 在 AEC/降噪后的帧上做近端语音确认，处理失败时标记降级并禁用受保护自动插话。`aec-audio-processing==1.0.1` Windows wheel 的 `has_voice()` 内部 VAD 未初始化，不能单独作为确认信号。控制器保留候选起点前约 200 ms 加完整确认窗的处理后预录，并以约 250 ms、至少 200 ms 有效语音的门控条件确认。
- [x] **阶段 6：取消竞态、已传达历史和前端状态。** 旧 generation 原子失效、保存快照、结算历史、取消 LLM/调度等待、后台发送 Bidi cancel，新轮立即使用预录音频；新轮 TTS 等待取消屏障。`turn.completed` 仍是历史唯一写入口并扩展终止状态与 `delivery`，另发即时 `turn.interrupted`；前端展示已传达、不确定、未传达和 AEC/TTS 状态。
- [x] **阶段 7：集成和外部验收准备。** 自动化和本机回放链路已接入，假服务端、假播放器、假时钟覆盖主要竞态；当前账号的真实 MiniMax Bidi 已完成短文本字段、句子重置、取消屏障和同连接续用 smoke test；完整多轮和耳机/扬声器自然语音验收仍待执行，不能以短探针替代。

## Bidi 协议事实和脱敏样例

实施依据是 [MiniMax Bidi 官方文档](https://platform.minimax.io/docs/api-reference/speech-t2a-websocket-bidi)。当前适配器按以下事实工作：

| 事实 | 实现约定 |
| --- | --- |
| 路径 | 当前配置使用 `/ws/v1/t2a_v2_bidi`，完整域名和凭据由现有配置提供。 |
| 建立任务 | 连接后发送一次 `task_start`，等待 `connected_success`/`task_started`；重复 start 属于协议错误。 |
| 持续文本 | 以 `task_continue` 发送文本，服务端负责重新断句；客户端发送片段不等于服务端句子。 |
| 结束本轮 | LLM 文本结束后发送 `task_flush`，收到 `task_flushed` 才认为供应商已返回本次 flush 的音频；仍需等待本地设备输出。 |
| 取消本轮 | `task_cancel` 到 `task_canceled` 形成取消屏障；屏障期间旧音频和字幕全部丢弃，超时关闭连接并在需要时重连。 |
| 字幕 | 请求开启 `subtitle_enable` 和 `subtitle_type=word_streaming`；适配器只接受实际返回中带有明确文本和时间字段的边界，缺失时保守降级。 |
| 保活 | Bidi 连接保持客户端 ping，默认间隔 30 秒。 |
| 背压 | `2205` 只重试协议明确拒绝且已关联的片段，100 ms 起指数退避至 1 s，总预算 10 s；无法关联时终止本轮，不能猜测重发。 |

当前账号实际收到的字幕字段（内容脱敏）形状如下；值经过替换，不含 API Key、完整文本或原始音频：

```json
{"event":"connected_success","connect_id":"conn-<redacted>"}
{"event":"task_started","task_id":"task-<redacted>"}
{"event":"task_continued","text":"片段<redacted>"}
{"event":"sentence_start","sentence_id":"s-1"}
{"event":"task_continued","data":{"audio":"<hex-redacted>","subtitle":{"text":"<redacted>","time_begin":0,"time_end":597.33,"text_begin":0,"text_end":8,"timestamped_words":[{"word":"<redacted>","time_begin":42.67,"time_end":213.33,"word_begin":0,"word_end":1}]}},"is_final":false}
{"event":"task_continued","data":{"audio":"<hex-redacted>","subtitle":{"text":"<redacted>","time_begin":0,"time_end":1432.0,"text_begin":0,"text_end":8,"timestamped_words":[{"word":"<redacted>","time_begin":42.67,"time_end":213.33,"word_begin":0,"word_end":1},{"word":"<redacted>","time_begin":1152.0,"time_end":1432.0,"word_begin":7,"word_end":8}]}},"is_final":false}
{"event":"task_flush"}
{"event":"task_flushed","audio_bytes":6400}
{"event":"task_cancel"}
{"event":"task_canceled"}
```

当前账号实测 `time_begin/time_end` 和词内同名时间字段以毫秒返回；`timestamped_words` 是句子内累计快照，后续快照重复前缀并追加新词；第二个 `sentence_start` 后时间再次从 0 开始。官方 AsyncAPI 对这些字幕字段的 schema 描述并不完整，因此适配器仍保留字段缺失时的保守降级。实测一个约 10.37 秒 PCM 任务的最后字幕边界约提前 100–160ms 于本地累计 PCM，属于服务端尾部差异，不能用它推断设备已播放位置。

取消实测记录：直接协议探针在发送 `task_cancel` 后、收到 `task_canceled` 前仍收到约 69 KiB 的旧音频帧；当前 `MiniMaxTtsSession` 在取消请求置位后丢弃这些帧，本地 spool 未增长。通过当前类的复用探针，取消确认约 62ms，确认后同一连接可发送新一代文本并收到第二轮 `task_flushed`。这说明 Bidi 取消只能阻止服务端继续合成/发送，已经到达本机的 PCM 仍必须由 `stop_and_snapshot` 和 generation gate 处理。

## 配置迁移

v0.2 示例配置现在明确使用：

```yaml
tts:
  transport: websocket_bidi
  scheduling: direct
  subtitle_enable: true
  subtitle_type: word_streaming
  audio_cache_max_bytes: 67108864
  cancel_timeout_ms: 2000
  keepalive_seconds: 30
audio:
  processing_enabled: true
  echo_cancellation: true
  noise_suppression: true
  automatic_gain_control: false
  device_latency_ms: 60
```

播放器本地 PCM 待播上限继续由独立的 `tts_local_buffer_ms`（默认约 2000 ms）控制。旧 WebSocket 的 `v02.tts_pacing`/`v02.tts_lead_ms` 仍可读并映射到 `PlaybackWindowPolicy`；该策略保留 callback `played_ms` 的诊断含义，但在有设备延迟估计时使用 `output_estimated_ms` 计算剩余窗口。显式新策略优先，Bidi 显式选择 `playback_window` 会报配置错误。旧 v0.1 默认配置和运行路径没有切换。

AEC 依赖为 `aec-audio-processing==1.0.1`，近端语音确认依赖为 `webrtcvad-wheels==2.0.14`。前者负责 AEC/降噪和反向参考，后者对处理后的 10 ms 帧提供独立 WebRTC VAD；不能使用当前 Windows wheel 中未初始化的 `has_voice()` 代替后者。任一依赖初始化失败、真实播放器反向参考尚未到达或参考流持续超过约 500 ms 没有更新时，运行时会显示降级，并关闭受保护自动插话；没有活动输出流时普通 VAD 路径仍可用，显式停止始终可用。`device_latency_ms` 是没有可靠 DAC 时间戳时的保守设备尾部估计，不代表测得的用户听感延迟。依赖安装和实际 native 处理能力仍需要在目标机器上复核。

## 自动化、回放和真实条件

自动化验证命令：

```powershell
python -m pytest -q
npm test --workspace @voice-terminal/web
npm run typecheck --workspace @voice-terminal/web
npm run build --workspace @voice-terminal/web
python -m py_compile <changed-python-files>
git diff --check
```

本次实施最后一次完整检查结果如下；Python 套件耗时约 28.67 秒，两个告警均为 AEC wheel 的 SWIG 类型弃用告警：

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | 120 passed，2 warnings |
| `python -m compileall -q voice_terminal apps tests` | 通过 |
| `npm test --workspace @voice-terminal/web` | 10 passed |
| `npm run typecheck --workspace @voice-terminal/web` | 通过 |
| `npm run build --workspace @voice-terminal/web` | 通过 |
| `git diff --check` | 通过 |

测试覆盖调度策略、Bidi 生命周期/背压/取消隔离、播放快照和设备延迟估计、字幕重复/缺失、门控和预录、历史幂等、v0.2 generation/revision、WebSocket seq 过滤及前端中断展示。测试使用假时钟、假播放器和假服务端控制竞态，不以长时间 sleep 作为同步手段。

已完成的本机依赖 smoke test 验证 `aec-audio-processing==1.0.1` 与 `webrtcvad-wheels==2.0.14` 能在当前 Python 3.12 环境导入，16 kHz 20 ms 帧处理、独立近端 VAD 和 32 kHz 反向参考路径保持样本长度；这不等于目标设备的声学效果验收。

真实服务和设备验收仍分开记录：

- **真实 MiniMax Bidi：已完成 smoke test，完整验收未完成。** 当前配置域名和已有凭据已验证连接、`task_start`、两句累计词字幕、句子时间重置、一次取消确认、迟到音频隔离和同连接第二轮 `flush`。仍需在当前区域完成至少 3 轮 flush、至少 3 次 cancel 后新轮、无标点短尾 flush，并记录字幕、2205、取消确认顺序；区域不支持时保持报告缺口，不切换账号区域、不改 HTTP TTS。
- **真实耳机/扬声器：未验证。** 当前服务已能启动 AEC/降噪和受保护门控，但还没有把自然语音设备试验结果写成通过。每种模式至少 20 次有效插话，成功率目标不低于 95%；确认到停止新增软件音频 p95 不超过 50 ms，语音起点到停播 p95 目标不超过 500 ms；另测 10 分钟纯助手播放不自我打断、固定噪声不触发和插话首字不丢失。
- **自然语音统计：未验证。** 需要固定录音和人工核对，单独报告误触、漏触、设备输出尾部和历史中已传达前缀的准确性。

## 交付证据边界

当前代码交付的是可运行的模块、兼容配置、协议适配、确定性自动化和前端状态链路。自动化通过只能证明软件状态机和数据隔离满足测试；AEC wheel smoke test 只能证明当前环境可加载；本文件不会把未执行的真实云服务、声学设备或自然语音结果写成“已通过”。
