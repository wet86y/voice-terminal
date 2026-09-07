# Voice Terminal WebSocket V1

所有服务端事件使用一个连接内严格递增的 `seq`：

```json
{
  "type": "speech.progress",
  "seq": 42,
  "timestamp": "2026-09-07T10:00:00.000Z",
  "session_id": "session_abc",
  "input_id": "input_abc",
  "episode_id": "episode_abc",
  "decision_id": "decision_abc",
  "speech_id": "speech_abc",
  "payload": {}
}
```

`session_id` 必填。其他身份仅在资源存在时提供。`payload` 始终是对象。客户端先按 `seq` 去重，再按资源身份忽略陈旧更新；客户端不生成身份、不拼装模型上下文，也不计算延迟。

客户端命令为 `subscribe`、`session.start`、`session.stop`、`session.reset`，均需非空 `request_id` 与对象型 `payload`。

| 事件 | 责任 |
| --- | --- |
| `runtime.snapshot`、`session.state`、`metrics.snapshot` | 权威会话与服务状态、后端延迟 |
| `input.started`、`input.updated`、`input.finalized`、`input.completed` | 用户输入生命周期 |
| `agent.state`、`agent.trace`、`context.snapshot` | Agent 状态、有限诊断、实际模型请求 |
| `tool.state` | 只读工具执行、完成或失效 |
| `speech.started`、`speech.delta`、`speech.progress`、`speech.completed` | 已授权语音输出与传达结算 |
| `error` | 可恢复或不可恢复错误 |

不存在 `turn.*`、`assistant.delta`、`tts.*`、`llm.context` 或预测专属的公共事件。
