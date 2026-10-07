# WebSocket 协议

这是浏览器与本机 Voice Server 的公共协议。组件边界见[架构文档](architecture.md)；前端适配错误和降级规则必须保留后端结构化错误码。

## 连接和安全边界

运行快照新增 `quiet_hours`（被动静默状态），包含 `passive_quiet`（当前抑制主动语音）、`user_interaction`（用户交互窗口开启）、`within_quiet_hours`（当前处于配置时段）三个布尔值。旧客户端可以忽略。待传达槽位没有正文事件或读取命令；逻辑会话重置先完成成对归档和交接，失败返回 `HANDOFF_RESET_FAILED`（交接重置失败）并保留旧会话。设置接口与恢复规则见[交接说明](continuity-foundation.md)。

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
| `session.reasoning_mode`（设置运行模式） | `{"mode":"fast\|deep"}`，`mode`（运行模式） | 修改后续主 Agent（主代理）模式：`fast`（快速）或 `deep`（深度），默认快速。 |

未知命令返回结构化错误，不应关闭主 session。

已归档的 `balanced`（均衡）及其他非法模式返回 `INVALID_REASONING_MODE`（运行模式无效），保持当前模式和连接。

上述双模式契约对应当前源码，正式目录更新待与其他修订合并处理；本轮回归和构建证据见[双模式源码验收](frontend-ui-business-migration.md#2026-10-07-双模式源码修订待统一发布)。

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
- `kind=user_turn`（用户消息类型）、`status=empty_input`（空输入状态）且 `text`（文本）为空或仅含空白字符的记录仍可存在于权威列表；产品对话区在展示层隐藏它，客户端存储保留记录与 `seq`（服务端顺序号）。增量消息与快照应用相同展示规则，非空短回答及其他消息类型不受影响。占位文案只是界面回退，空输入记录本身不触发 LLM（大语言模型）请求。

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

- `shadow.product`：当前可用性、active/phase/focus、summary、最近活动和资源指标的有界权威快照；
- `shadow.activity`：当前运行中的 lifecycle、thinking、tool 和公开事件增量。DSH status observer
  摘要通过 Voice 侧 `status` 投影更新 summary，不作为 event 输出区域中的终态事件。

活动 item（活动项）使用 `run_seq + item_id`（运行序号与条目标识）标识，状态为 `started`、`delta`、`completed` 或 `failed`（开始、增量、完成或失败），并且必须带有服务端生成的 `timestamp`（时间戳）。绑定 DSH 根 Agent（后台代理主智能体）的插件可将运行时 `reasoning-delta`（思考增量）和 `tool/call`、`tool/result`（工具开始与结果状态）投影到 `shadow.activity`，仅供“运行轨迹”固定单槽即时显示；同一思考项累加增量，新思考块或工具动作覆盖当前项。该界面投影只保存在有界的运行时活动快照中，不追加 Voice 对话或产品长期活动历史，也不新增 DSH 归档写入；DSH runtime（运行时）原有会话存档行为保持不变，Bridge（桥接层）不会额外复制该单槽正文。`action_seq`（动作序号）在每次激活内递增，Voice 侧只接收当前会话、代次、激活及序号有效的动作。工具活动只包含工具名称与状态，不包含参数或结果正文。DSH 根 Agent（后台代理主智能体）空闲或运行结束后，单槽清空；重连先应用 `shadow.product`（产品快照）恢复最新运行项，再按 WebSocket `seq`（消息序号）应用增量；旧 `run_seq`（运行序号）的迟到事件直接丢弃。除该单槽专用增量外，provider request（供应商请求）、trace ID（追踪标识）、其他私有 reasoning（思考内容）、凭据和内部工具参数不得进入公共协议。浏览器在应用快照前冲刷同一帧内的增量，不能让旧 activity（活动）覆盖新 summary（摘要）。

插件内部回调的 `action_seq`（动作序号）同时约束思考、工具和空闲状态，统一串行发送；乱序空闲状态不能清空新动作。该序号在桥接接收端校验，前端继续使用 WebSocket `seq`（消息序号）及 `run_seq + item_id`（运行序号与活动标识）应用活动。完整输入字段、错误回执及取消规则见 [Bridge 2.0（桥接协议）](bridge-2.0-protocol.md)。

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
