# Voice Terminal v0.1 WebSocket 协议

本文档是前端与 Voice Server 之间的冻结协议。前端只负责发送会话命令和展示服务端数据，不采集音频、不运行模型、不计算延迟。

## 连接

默认地址：

```text
ws://127.0.0.1:8765/ws
```

前端页面加载后自动建立连接。地址可以通过 `VITE_VOICE_WS_URL` 修改，但不在页面中提供连接切换按钮。

连接失败时，前端显示错误并禁用 `开始说话`。本版本不自动重连，也不提供重试按钮；刷新页面会重新执行一次自动连接。

### 按钮与状态的真值来源

按钮只反映连接状态和后端最近一次快照/状态事件，不反映前端本地推测：

| 条件 | 按钮显示 | 是否可用 | 前端动作 |
| --- | --- | --- | --- |
| 正在连接 | `连接中…` | 否 | 等待连接结果 |
| 未连接、连接错误或后端异常 | `开始说话` | 否 | 只展示错误 |
| 已连接且 `can_start=true`、当前未监听 | `开始说话` | 是 | 发送 `session.start` |
| 后端 `listening=true` | `停止监听` | 是 | 发送 `session.stop` |
| 已连接但后端暂不可启动 | `开始说话` | 否 | 等待后端状态，不自行修改 `can_start` |

点击命令发出后，前端不会提前修改 `state`、`listening`、`turn_id` 或任何延迟值。后端必须通过 `runtime.snapshot` 或 `session.state`确认结果。

## 消息 Envelope

所有服务端消息都必须使用以下结构：

```json
{
  "type": "latency.snapshot",
  "seq": 42,
  "timestamp": "2026-09-04T08:30:10.123Z",
  "turn_id": "turn_000021",
  "payload": {}
}
```

字段要求：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | 消息类型 |
| `seq` | number | 当前连接内单调递增，从 0 或 1 开始均可，但不能重复 |
| `timestamp` | ISO-8601 string | 后端产生消息的时间，仅用于展示和调试 |
| `turn_id` | string/null | 当前轮 ID；运行时级消息为 `null` |
| `payload` | object | 当前消息的结构化内容 |

前端以 `seq` 去重和丢弃乱序消息。`timestamp` 不能用于前端延迟计算。

## 客户端命令

客户端发送：

```json
{
  "type": "command",
  "request_id": "req_000001",
  "command": "session.start",
  "payload": {}
}
```

固定命令：

| 命令 | 发送时机 | 后端行为 |
| --- | --- | --- |
| `subscribe` | WebSocket 建立后自动发送 | 初始化当前客户端的状态推送 |
| `session.start` | 用户点击 `开始说话` | 启动后端持续监听、KWS、VAD、ASR 和后续语音链路 |
| `session.stop` | 用户点击 `停止监听` | 停止后端持续监听，并按后端策略取消当前回答/播放 |

`request_id` 由前端生成，仅用于后端确认和日志关联，不参与延迟计算。

`subscribe` 示例：

```json
{
  "type": "command",
  "request_id": "req_000001",
  "command": "subscribe",
  "payload": {
    "client": "voice-terminal-web",
    "version": "0.1.0"
  }
}
```

## 服务端消息

### `runtime.snapshot`

连接建立后发送，也可以在服务端状态恢复时发送。它是前端初始化界面的完整快照。

```json
{
  "type": "runtime.snapshot",
  "seq": 1,
  "timestamp": "2026-09-04T08:30:00.000Z",
  "turn_id": null,
  "payload": {
    "state": "IDLE",
    "listening": false,
    "can_start": true,
    "services": {
      "audio": "READY",
      "kws": "RUNNING",
      "asr": "READY",
      "llm": "READY",
      "tts": "CONNECTED"
    },
    "current": {
      "partial_text": "",
      "final_text": "",
      "assistant_text": "",
      "tts_text": "",
      "tts_status": "IDLE",
      "audio_buffer_ms": 0
    }
  }
}
```

字段说明：

- `state`：后端真实状态。
- `listening`：后端持续监听是否已启动。
- `can_start`：后端是否允许启动监听。
- `services`：各服务的真实状态。
- `current`：当前轮已经产生的文本和播放器状态。

### `session.state`

推送状态变化。开始新一轮时必须携带新的 `turn_id`。

```json
{
  "type": "session.state",
  "seq": 2,
  "timestamp": "2026-09-04T08:30:01.000Z",
  "turn_id": "turn_000021",
  "payload": {
    "state": "LISTENING",
    "listening": true,
    "can_start": false
  }
}
```

允许的 `state`：

```text
STARTING
IDLE
LISTENING
FINALIZING
THINKING
SPEAKING
INTERRUPTING
ERROR
```

### `transcript.partial`

替换当前轮的 partial 文本，不是增量追加。

```json
{
  "type": "transcript.partial",
  "seq": 3,
  "timestamp": "2026-09-04T08:30:02.000Z",
  "turn_id": "turn_000021",
  "payload": {
    "text": "帮我看看今天"
  }
}
```

### `transcript.final`

写入当前轮最终用户文本。

```json
{
  "type": "transcript.final",
  "seq": 4,
  "timestamp": "2026-09-04T08:30:03.000Z",
  "turn_id": "turn_000021",
  "payload": {
    "text": "帮我看看今天 OpenClaw 更新了什么"
  }
}
```

### `assistant.delta`

推送 LLM 流式增量文本。前端按顺序拼接 `text_delta`。

```json
{
  "type": "assistant.delta",
  "seq": 5,
  "timestamp": "2026-09-04T08:30:03.500Z",
  "turn_id": "turn_000021",
  "payload": {
    "text_delta": "我已经查到了，"
  }
}
```

### `llm.context`

在发起 DeepSeek 请求前推送本轮实际使用的完整 messages。该事件用于本地调试和 WebUI 展示，内容与发给 LLM 的请求上下文一致；前端不得自行根据历史记录重新拼接。

```json
{
  "type": "llm.context",
  "seq": 6,
  "timestamp": "2026-09-04T08:30:03.450Z",
  "turn_id": "turn_000021",
  "payload": {
    "message_count": 4,
    "messages": [
      {"role": "system", "content": "你是本地实时语音终端的中文助手。"},
      {"role": "user", "content": "第一句话"},
      {"role": "assistant", "content": "第一句回答"},
      {"role": "user", "content": "第二句话"}
    ]
  }
}
```

### `tts.status`

推送 TTS 等待文本、TTS 状态和播放器音频缓冲。

```json
{
  "type": "tts.status",
  "seq": 6,
  "timestamp": "2026-09-04T08:30:04.000Z",
  "turn_id": "turn_000021",
  "payload": {
    "provider": "minimax",
    "status": "STREAMING",
    "waiting_text": "今天的更新主要集中在启动速度和插件稳定性。",
    "audio_buffer_ms": 60
  }
}
```

`audio_buffer_ms` 必须由后端测量或维护，前端只显示。

### `latency.snapshot`

推送当前轮已经计算出的延迟值。值是毫秒，前端不能根据 Envelope 时间戳自行推导。

```json
{
  "type": "latency.snapshot",
  "seq": 7,
  "timestamp": "2026-09-04T08:30:04.100Z",
  "turn_id": "turn_000021",
  "payload": {
    "complete": false,
    "values": {
      "wake_detection_ms": 120,
      "asr_first_text_ms": 180,
      "speech_end_confirmation_ms": 700,
      "llm_first_token_ms": 320,
      "llm_request_to_first_tts_text_ms": 410,
      "tts_text_to_first_audio_ms": 210,
      "first_audio_to_playback_ms": 60,
      "speech_end_to_playback_ms": 1270,
      "turn_total_ms": null
    }
  }
}
```

固定指标及其语义：

| 字段 | 计算责任 | 含义 |
| --- | --- | --- |
| `wake_detection_ms` | 后端 | 开始监听到 KWS 检测到唤醒词 |
| `asr_first_text_ms` | 后端 | 用户开始讲话到 ASR 首次出字 |
| `speech_end_confirmation_ms` | 后端 | 最后语音到后端确认本轮结束 |
| `llm_first_token_ms` | 后端 | 请求 LLM 到收到首 token |
| `llm_request_to_first_tts_text_ms` | 后端 | 请求 LLM 到首段文本进入 TTS |
| `tts_text_to_first_audio_ms` | 后端 | 首段文本进入 TTS 到收到首个音频包 |
| `first_audio_to_playback_ms` | 后端 | 收到首个音频包到播放器真正开始播放 |
| `speech_end_to_playback_ms` | 后端 | 用户说完到 AI 开始播放 |
| `turn_total_ms` | 后端 | 本轮从开始讲话到本轮结束 |

指标尚未产生时使用 `null`。前端显示为 `—`，不替换为 `0`。

### `turn.completed`

本轮结束后发送完整记录。前端将其加入最近 10 轮历史。

```json
{
  "type": "turn.completed",
  "seq": 8,
  "timestamp": "2026-09-04T08:30:08.000Z",
  "turn_id": "turn_000021",
  "payload": {
    "transcript": "帮我看看今天 OpenClaw 更新了什么",
    "response": "我已经查到了，今天的更新主要集中在启动速度和插件稳定性。",
    "completed_at": "2026-09-04T08:30:08.000Z",
    "latency": {
      "wake_detection_ms": 120,
      "asr_first_text_ms": 180,
      "speech_end_confirmation_ms": 700,
      "llm_first_token_ms": 320,
      "llm_request_to_first_tts_text_ms": 410,
      "tts_text_to_first_audio_ms": 210,
      "first_audio_to_playback_ms": 60,
      "speech_end_to_playback_ms": 1270,
      "turn_total_ms": 5300
    }
  }
}
```

### `error`

推送后端错误。严重错误应同时通过 `session.state` 推送 `ERROR`。

```json
{
  "type": "error",
  "seq": 9,
  "timestamp": "2026-09-04T08:30:09.000Z",
  "turn_id": "turn_000021",
  "payload": {
    "code": "ASR_WORKER_UNAVAILABLE",
    "message": "ASR Worker 当前不可用",
    "recoverable": false
  }
}
```

## 前端处理规则

1. 收到消息后先校验 `type`、`seq`、`timestamp` 和 `turn_id`。
2. `seq` 小于或等于最近处理序号的消息直接丢弃。
3. 当前轮消息必须匹配当前 `turn_id`；旧轮次迟到消息不得覆盖当前页面。
4. 新的 `session.state` 携带不同 `turn_id` 时，前端清空当前轮文本和当前轮指标，但不删除历史。
5. `turn.completed` 是历史记录的唯一写入口，历史最多保存 10 轮。
6. `null` 指标显示为 `—`，不得使用前端默认值补全。
7. 所有页面状态、文本、服务状态和延迟都以服务端数据为准。
8. 前端禁止使用本地计时器或本地时间差计算语音链路延迟。

## 后端实现检查清单

实现 Voice Server 时应将以下约束视为协议的一部分：

- `subscribe` 是连接建立后的初始化命令；后端应返回可直接渲染的 `runtime.snapshot`，不要要求浏览器补默认服务状态。
- 新一轮开始时，所有当前轮事件使用同一个新 `turn_id`；上一轮迟到事件必须可以被前端安全丢弃。
- `transcript.partial` 发送当前完整 partial 文本，`assistant.delta` 才是需要由前端顺序拼接的文本增量。
- `llm.context` 发送本轮请求实际使用的完整 messages；前端只展示，不重新拼接或修改。
- `tts.status.audio_buffer_ms` 和 `latency.snapshot.payload.values` 都是后端测量/维护的只读结果，不应要求浏览器回传开始时间或音频时间戳。
- `turn.completed` 应包含完整 transcript、response 和最终 latency；这是前端写入历史的唯一事件。
- 发生不可恢复错误时发送 `error`，并在状态已改变时同步发送 `session.state`；不要依赖浏览器自行推断 `ERROR`。
- `seq` 只在当前 WebSocket 连接内递增；重连后可以从 0 重新开始，前端会重置该连接的去重游标。

协议消费方不应把前端控制台当作音频管线：浏览器不授予麦克风权限、不上传 PCM，也不承担 KWS、VAD、ASR、LLM、TTS、播放或延迟测量。
# v0.2 增量扩展

现行 Envelope、subscribe/session.start/session.stop 和 v0.1 事件保持兼容。
启用 `v02.enabled` 后，轮次事件增加 `input_revision`、`generation_id` 和 `workspace`。
`workspace` 包含当前分段、预测摘要、只读工具状态、Paraformer 草稿、最终文本、分段语音线索及分析服务降级原因；不含 PCM、密钥或模型思考过程。

| 新事件 | 含义 |
| --- | --- |
| turn.started | 新用户轮次，前端采用新的 turn_id |
| turn.endpoint_candidate | VAD 候选，携带 audio_end_sample |
| turn.semantic_state | Smart Turn probability 或降级原因 |
| turn.finalizing | 接受结束，reason 为 smart_turn/hard_silence/vad_end/max_duration |
| turn.resumed | 播放前续说，同 turn_id、新 generation；清空旧回答和 TTS 展示 |
| transcript.segment | text 累计快照、new_segment、corrected、segments |
| agent.prediction | LISTEN/ACT/READY、简短 intent 或预测错误 |
| agent.tool | name、RUNNING/DONE/FAILED、speculative/reused、等待和重叠时间 |
| agent.prediction.requested | 预测快照已发出；携带 input_revision、generation_id 和快照摘要 |
| agent.prediction.discarded | 预测结果未采用；reason 说明 revision/generation 过期、轮次结束、输入封口、超时或无效返回 |
| transcript.refined | 最终采用 text、source=sensevoice/paraformer |
| speech.emotion | segments，每段 start_ms/end_ms/language/emotion/events/raw_tags |
| turn.committed | 本次 generation 最终输入封定；与历史提交不同 |

`transcript.final` 始终为最终采用文本。预测不发送 assistant.delta。`turn.completed` 仍是唯一新增历史记录事件；订阅快照额外提供 `recent_turns`，用于刷新恢复已有历史。

前端先按连接 seq 去重，再按 turn_id 隔离；同轮丢弃低 generation，同 generation 丢弃低 input_revision。`runtime.snapshot.current.turn_id` 支持重新订阅时恢复当前轮。旧客户端可忽略扩展字段。

新增后端延迟：first_segment_ms、first_prediction_request_ms、first_prediction_ms、smart_turn_ms、sensevoice_refine_ms、commit_to_final_llm_ms。v0.2 中 llm_first_token_ms 从最终请求开始计算，speech_end_confirmation_ms 从 VAD 音频结束边界计算；null 表示未测得。工具事件可以包含 selected_at_ms、execution_start_ms、finished_at_ms、execution_ms、wait_ms 和 overlap_ms；它们来自单调时钟的实际时间线。overlap_ms 是预取执行与最终选择之前的实际交集，不是反事实加速承诺。

慢订阅者积压超过 256 事件后断开 WebSocket（1013）；音频主链不等待浏览器。

## Analysis Worker 内部接口

默认 `ws://127.0.0.1:8767/analysis`。每连接一次请求。JSON 包含 operation（endpoint/refine）、request_id、turn_id、generation_id、pcm_base64（16 kHz/mono/int16，最多 62 秒）。结果回显身份、result、elapsed_ms；失败返回 error。health 操作及 HTTP `/health` 分别报告 endpoint/refine READY/DEGRADED。

每模型独立单执行槽，无等待队列；忙碌返回错误，由主服务降级。取消请求不提前释放尚未结束的原生推理槽。
