# WebSocket 协议

这是浏览器与本机 Voice Server 的公共协议。组件边界见[架构文档](architecture.md)；前端适配错误和降级规则必须保留后端结构化错误码。

## 连接和安全边界

默认连接：

```text
ws://127.0.0.1:8765/ws
```

当前协议按本机开发服务设计，默认绑定回环地址，尚未提供远程客户端认证。不要把服务直接暴露到局域网或公网；远程部署前必须先完成认证、Origin allowlist 和管理 API 防护。

## Envelope

客户端命令是 JSON 对象，至少包含非空 `request_id`、命令名和对象型 `payload`。服务端事件使用统一 envelope：

```json
{
  "seq": 12,
  "timestamp": "2026-09-20T00:00:00.000Z",
  "conversation_id": "conversation-id",
  "turn_id": "turn-id",
  "payload": {
    "type": "session.state"
  }
}
```

`seq` 只在当前 conversation 内递增；`session.reset` 后重新开始。客户端应按 `seq` 应用事件，不能根据本地到达时间重排服务端记录。

## 客户端命令

| 命令 | payload | 作用 |
| --- | --- | --- |
| `subscribe` | 对象，可为空 | 获取 runtime、conversation、Shadow 和指标快照，并开始接收事件。 |
| `session.start` | 对象，可为空 | 启动输入作用域；KWS 开启时先进入 KWS standby。 |
| `session.stop` | 对象，可为空 | 停止新的麦克风输入，不自动取消已经进入 Agent/TTS 队列的输出。 |
| `session.reset` | 对象，可为空 | 清理当前会话临时状态、Shadow trace、TTS 事务和 conversation epoch。 |
| `session.reasoning_mode` | `{"mode":"fast\|balanced\|deep"}` | 修改后续主 Agent 思考模式。 |

未知命令返回结构化错误，不应关闭主 session。

## 错误格式

HTTP API 和 WebSocket 事件都应尽量保留结构化错误码：

```json
{
  "code": "SPEAKER_CAPTURE_UNAVAILABLE",
  "message": "server capture is unavailable",
  "details": {}
}
```

前端不得只使用 HTTP status 或一段通用文本判断是否降级。`code` 用于程序分支，`message` 用于展示，`details` 只包含非敏感诊断信息。

## Runtime 和会话事件

常用事件：

- `runtime.snapshot`：重连或订阅后的全量运行快照；
- `session.state`：`IDLE`、`LISTENING`、`FINALIZING`、`ERROR` 等会话状态；
- `input.partial` / `input.finalized`：ASR partial 和最终用户输入；
- `metrics.snapshot`：后端诊断指标，不覆盖前端本地感知延迟计时；
- `conversation.append`：服务端会话顺序流；
- `conversation.snapshot`：当前 conversation 的权威全量列表；
- `error`：当前命令或运行链路的结构化错误。

`session.stop` 只关闭输入作用域。已经派发的 LLM、TTS 和 Shadow 工作按既有生命周期继续结算；需要跨会话清理时使用 `session.reset`。

## Speech 事件

语音事务以 `speech_id` 标识，前端不得把多个事务合并为一条“当前回复”：

- `speech.queued`：事务进入队列，文本可能为空；
- `speech.delta`：追加正式输出文本；
- `speech.started`：播放器第一次实际消费该事务 PCM；
- `speech.progress`：缓冲、播放、替换和结算诊断；`trigger=playback_finished` 表示物理播放结束；
- `speech.tail_rewritten`：只改写同一事务的未发送尾巴，`speech_id` 和播放键不变；
- `speech.completed`：delivery 和最终事务状态结算完成。

物理播放结束和服务端结算不是同一边界：

- `speech.started` 用于产品“用户说完 → AI 开口”计时的结束；
- `playback_finished` 用于播放面板更新；
- `speech.completed` 用于 ledger、历史和 delivery 最终结算。

## Conversation 顺序

`conversation.append` 的 payload 至少包含：

```json
{
  "seq": 3,
  "kind": "user_turn|assistant_speech|system_speech",
  "conversation_id": "conversation-id",
  "turn_id": "turn-id",
  "speech_id": "speech-id",
  "text": "...",
  "status": "...",
  "delivery": "...",
  "termination": "...",
  "at": "2026-09-20T00:00:00.000Z"
}
```

规则：

- 用户轮次在输入提交/ASR final 时取得 `seq`，不能等自己的回复结算后才创建；
- assistant speech 在 `speech.completed` 时进入顺序流；
- 同一个 `turn_id` 或 `speech_id` 的后续事件只更新状态和文本，不改变 `seq`；
- `conversation.snapshot` 的 `entries` 是权威列表；刷新或重连时整体替换本地列表；
- 客户端使用 `conversation_id + turn_id/speech_id` 去重。

这样可以保证用户问题排在其回复之前，也能让 KWS 唤醒确认和最终静默检查出现在真实发生的位置。

## 感知延迟

Product UI 的“用户说完 → AI 开口”由浏览器单调时钟测量：

1. 收到本轮 `session.state.state=FINALIZING` 时开始；没有该状态时兼容使用 `input.finalized`；
2. 收到同一轮的 `speech.started` 时停止；
3. 取消、替换或播放失败时丢弃未完成计时；
4. `metrics.snapshot` 只用于诊断、历史和回放，不覆盖实时值。

小于 `1000 ms` 显示整数毫秒；大于等于 `1000 ms` 显示最多两位小数的秒。

## Shadow 产品事件

Shadow UI 使用：

- `shadow.product`：当前可用性、active/phase/focus、summary、最近活动和资源指标的有界快照；
- `shadow.activity`：当前运行中的 lifecycle、thinking、tool 和公开事件增量。

活动 item 使用 `run_seq + item_id` 标识，状态为 `started`、`delta`、`completed` 或 `failed`。provider request、trace ID、私有 reasoning 和内部工具参数不得进入公共协议。重连时先应用 `shadow.product`，再按 WebSocket `seq` 应用增量；旧 `run_seq` 的迟到事件直接丢弃。

调试协议只有在 `shadow_agent.debug_websocket_enabled=true` 时可用：

```json
{
  "command": "shadow.debug.wake",
  "request_id": "debug-1",
  "payload": {}
}
```

调试唤醒仍使用 session 唯一 ShadowRuntime，不创建连接级 task manager，不接受任务文本、task ID、modify/reply/cancel，也不绕过权威上下文。

## 兼容和测试

- `wake_always_on`、旧的单个 `wake_keyword` 等字段仅为兼容读取或展示，不应被当作新的控制入口。
- 未知命令、协议错误和 Shadow 调试关闭分别返回结构化错误，不应让主会话退出。
- 修改事件顺序、字段、错误码或 session 生命周期时，必须同时更新 Web 类型、Product adapter 和对应测试。
