# WebSocket 协议

## 连接与 envelope

浏览器连接 `ws://127.0.0.1:8765/ws`。客户端命令均为 JSON 对象，并携带非空
`request_id` 与对象型 `payload`。公共服务事件由 `ProtocolSequencer` 包装为带 `seq`、
`timestamp`、`conversation_id`、可选 `turn_id` 和 `payload` 的 envelope。

常规命令：

- `subscribe`
- `session.start`
- `session.stop`
- `session.reset`
- `session.reasoning_mode`，payload 为 `{"mode":"fast|balanced|deep"}`

## 语音输出观测事件

播放看板只消费事件，不反向控制输出链路：

- `speech.queued`：文本事务已进入等待队列，初始 `text` 可以为空；
- `speech.delta`：按 `speech_id` 追加正式输出文本；
- `speech.started`：播放器首次实际消费该事务的 PCM，payload 包含 `speech_id`、
  `conversation_id`、`request_id`、`turn_id`、`output_generation`、当前完整 `text` 和情绪字段；
- `speech.progress`：提供缓冲、播放时长和替换等诊断。`trigger=playback_finished` 表示物理播放已经结束，
  `status=FINISHED` 为正常播完，`status=DEGRADED` 为播放失败；
- `speech.completed`：追加窗口和 delivery 结算后的最终事务记录，可能晚于物理播放结束。

`speech.started` 和 `playback_finished` 是看板的实时播放边界；`speech.completed` 是账本与历史边界，
两者不得互相替代。前端可以在收到 `playback_finished` 后，把队列中已经取得的文本转入当前会话的本地
历史轨迹，并以 `conversation_id + speech_id` 合并后续事件。这样物理播放结束后仍保留该条播报的独立
轨迹，同时不把播放结束误报为服务端已经完成结算；后到的 `speech.completed` 负责补齐最终状态、
delivery 与延迟等字段。切换会话、`session.reset` 或断开连接时，客户端应清理对应的会话级投影。

`subscribe` 返回的 `runtime.snapshot.agent` 从当前 Episode 实时生成，用于刷新页面或断线重连后恢复活动
输出和等待队列；快照中的已结算 outputs 仍应与前端刚刚形成的本地轨迹按同一复合键去重合并。

## Shadow 调试协议

生产 Shadow 没有 task API，也不为每个 WebSocket 连接创建 manager。主模型只能通过内部
`request_shadow({"request":true})` 唤醒会话唯一 runtime；Shadow 事件走正常 heartbeat，
不会直接向浏览器暴露私有 trace、revision、计划或工具栈。

当 `shadow_agent.debug_websocket_enabled=true` 时，开发连接可发送：

```json
{
  "command": "shadow.debug.wake",
  "request_id": "debug-1",
  "payload": {}
}
```

服务器返回连接本地的 `shadow.debug.wake` 观察事件，payload 包含内部 wake 结果和经过裁剪的
runtime snapshot；后续 `shadow.debug.event` 只镜像该会话产生的 status/progress/observation/
user_input_required/result/failed。该命令唤醒的仍是生产会话唯一 ShadowRuntime，不创建 connection-local
task manager，不接受任务文本、ID、modify/reply/cancel，也不绕过权威上下文。

默认关闭时返回 `SHADOW_DEBUG_DISABLED`；Shadow 不可用时返回 `SHADOW_UNAVAILABLE`。
断开调试连接不会取消 session-owned Shadow。

## Shadow 与 session 生命周期

- `session.stop` 只关闭语音 episode。Shadow 继续运行，重要事件缓存在 session event bridge；
- 下一次 `session.start` 把缓存事件交回主 Agent 的 400ms mailbox；
- `session.reset` 取消 Shadow provider/observer/compaction，清空 trace/status，增加上下文 epoch；
- reset 同时轮换 `conversation_id` 和 OpenCode Go 的 `x-opencode-session`；
- runtime close 才关闭 Shadow 组件、只读工具和共享 HTTP client。

## 兼容性说明

Shadow 调试面只支持上文的 wake/event 协议，不提供其他命令的静默兼容。收到未知命令时
服务器返回 `UNKNOWN_COMMAND`；协议错误不会停止主会话。
