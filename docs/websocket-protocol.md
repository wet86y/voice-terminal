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
