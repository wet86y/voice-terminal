# TTS 调度策略

TTS provider 只负责建立会话、发送文本、接收音频和报告协议事件；文本何时发送由独立策略决定。MiniMax WebSocket Bidi 可以持续接收 LLM 输出，旧 WebSocket 或其他供应商仍可以复用原来的播放窗口算法。调度模块不解析供应商原始消息，也不把供应商 session id 当作本地轮次身份。

## 统一接口

策略的最小输入是当前 `generation_id`、待发送文本、`PlaybackProgress`（已回调消费、设备输出估计、待输出范围）和片段完成/供应商可用性判断。`played_ms` 保留回调消费的历史含义；若提供 `output_estimated_ms`，播放窗口用这个保守设备位置计算 `remaining_ms`。输出是“现在可以发送”或“继续等待”的决定，并带有可诊断的等待原因。策略不执行网络、音频回调、模型推理或磁盘 I/O。

`TtsGenerationScheduler` 是生命周期薄层：

- `begin(generation_id)` 建立新的代次并清理上一代的等待信号；
- `send(text, generation_id)` 串行执行策略，检查代次没有过期后交给 provider；
- `end(generation_id)` 标记 LLM 文本生产结束，允许策略排空可发送文本；
- `cancel(generation_id)` 使代次失效并唤醒所有窗口等待者；
- `snapshot()` 返回待发送数、已发送数、当前等待原因和取消状态。

provider 适配器仍需提供 `start`、`send_text`、`flush`、`cancel`、`finish`、`audio_chunks` 和 `close`。`flush` 只结束当前轮文本，不关闭可复用会话；`finish` 才结束 provider 会话。控制器可以在调度器外围处理 provider 专用背压、音频读取和字幕事件，但必须把它们绑定到本地 `turn_id`、`generation_id` 与连接代次。

## `DirectStreamingPolicy`

`DirectStreamingPolicy` 收到文本后立即释放，不读取播放余量，也不等待上一段合成完成。它适合服务端自己做句子缓冲和背压的双向流式接口，例如 Bidi：服务端会在 `task_continue` 后自行断句，客户端不能假设一个发送片段等于一个句子。

控制器在 direct 策略前使用 `BidiTextMerger` 做轻量合并：最多等待约 30 ms 或 128 个字符，先达到者发送；超过字符上限的单次 LLM delta 会拆成有界片段，短尾立即释放，原有标点和字符顺序保持不变。即使尾部没有标点，`flush` 也会把它发给 provider。

## `PlaybackWindowPolicy`

该策略完整保留旧链路的“文本播放窗口”行为，默认提前量 `lead_ms=2000`：

1. 文本进入有界待发送队列；队列满时由上层背压或报告生产异常，不能无限增长。
2. 第一段可立即发送，避免没有任何音频时发生互等。
3. 每段发送后等待相应片段的合成完成信号。该信号来自 provider 适配器，策略不解析 MiniMax 原始事件。
4. 读取播放器进度，计算当前 generation 的总已接收/已合成音频减去已消费或设备输出估计后的 `remaining_ms`。
5. 当 `remaining_ms <= lead_ms` 时释放下一段；超过窗口则继续等待，并把“等待音频余量”写入调度诊断。
6. 文本生产结束后排空仍可安全发送的队列；超时、取消、供应商异常会唤醒等待并结束代次，不遗留后台 waiter。

可复用实现位于 `voice_terminal/tts/scheduling.py`。它只依赖 `PlaybackProgress`、片段完成信号和代次有效性函数，因此其他能提供可靠片段完成信号的 TTS provider 可以接入。如果供应商只能返回不可关联的整体音频，就不要默认启用此策略，应使用 direct 或供应商自己的明确背压。

## 三类缓冲不能混用

这里的播放窗口只控制**文本发送时机**，不是播放器容量。三类状态分别是：

- **文本播放窗口**：`PlaybackWindowPolicy` 的待发送片段和 2 秒提前量；Bidi 主流程使用 direct 后它仍作为兼容模块保留。
- **本地 PCM 队列**：播放器内等待设备消费的音频，仍由独立的 `tts_local_buffer_ms`（默认约 2 秒）限制。音频回调必须保持可消费，队列满不能阻塞协议控制消息。
- **设备输出尾部**：`audio.device_latency_ms` 只用于没有可靠 DAC 时间戳时的保守输出位置估计；它不是本地 PCM 队列上限，也不等同于用户听清的延迟。
- **供应商音频暂存区**：Bidi 接收任务把 PCM 顺序写入可落盘 spool，首版每个活动 generation 默认上限 64 MiB；超限终止本轮并报告降级，不能无限占用内存或磁盘。

取消、错误和新 generation 会关闭旧 PCM 入队权限，并清理对应 spool。迟到音频不能因为本地队列仍有空间而进入下一轮。

## 配置兼容和接入示例

新配置使用 `tts.transport`（`websocket` 或 `websocket_bidi`）和 `tts.scheduling`（`auto`、`direct`、`playback_window`）：

| transport | scheduling | 实际策略 |
| --- | --- | --- |
| `websocket_bidi` | `auto` | `direct` |
| `websocket_bidi` | `direct` | `direct` |
| `websocket_bidi` | `playback_window` | 报配置错误，避免把旧片段等待语义误用到 Bidi |
| `websocket` | `auto` | 读取旧 `v02.tts_pacing` 和 `v02.tts_lead_ms` 映射 |
| 任一 | 显式新策略 | 新字段优先于旧字段 |

旧字段继续可读，主要用于既有 WebSocket 配置和回滚；它们从 Bidi 示例中移除。播放窗口阈值和本地 PCM 上限使用不同字段，不能都解释成“2 秒缓存”。

一个其他 TTS provider 的适配流程如下：先把 provider 的连接、文本发送、片段完成和音频读取封装成适配器；再以 `PlaybackProgress` 创建 `PlaybackWindowPolicy` 或 `DirectStreamingPolicy`；每次发送前校验 generation；取消时调用 scheduler 的 `cancel` 并关闭旧音频入口；最后将 provider 事件转换成内部对齐/进度事件。策略内部不应引用 HTTP 响应字段、MiniMax event name 或具体播放器实现。
