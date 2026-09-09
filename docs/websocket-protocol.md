# Voice Terminal WebSocket V0.6

所有服务端事件使用一个连接内严格递增的 `seq`：

```json
{
  "type": "speech.progress",
  "seq": 42,
  "timestamp": "2026-09-07T10:00:00.000Z",
  "conversation_id": "conversation_abc",
  "turn_id": "turn_abc",
  "speech_id": "speech_abc",
  "payload": {}
}
```

`conversation_id` 必填，`turn_id` 与 `speech_id` 只在相应资源存在时提供。`request_id`、`output_generation` 仅用于关联和显式取消后的播放器代际诊断，客户端不得把它们当作语义版本。`payload` 始终是对象；客户端先按 `seq` 去重，再按资源身份处理事件。

客户端命令为 `subscribe`、`session.start`、`session.stop`、`session.reset`，均需非空 `request_id` 与对象型 `payload`。

| 事件 | 责任 |
| --- | --- |
| `runtime.snapshot`、`session.state`、`metrics.snapshot` | 权威会话与服务状态、后端延迟 |
| `input.started`、`input.updated`、`input.finalized`、`input.completed` | 用户输入生命周期 |
| `agent.state`、`agent.trace`、`agent.reasoning`、`context.snapshot` | Agent 状态、本地推理调试、有限诊断、实际模型请求 |
| `tool.state` | 只读工具执行、完成或失效 |
| `speech.queued`、`speech.started`、`speech.delta`、`speech.progress`、`speech.completed` | 已授权语音输出、队列与传达结算 |
| `error` | 可恢复或不可恢复错误 |

`speech.queued`、`speech.progress` 和 `speech.completed` 只描述文本 speak 事务；用户讲话期间队列不启动 TTS，不生成 PCM。只有实际播放的文本才可能出现在 DeliveryReport。`agent.reasoning` 仅限受控本地调试环境。

不存在 native `tool_calls`、`tool_call_id`、`speak(queue|modify)`、`session_id`、`input_id`、`episode_id`、`decision_id`、`generation_id`、`input_revision`、`context_version` 或 V0.4/V0.5 客户端映射。V0.6 是同步破坏性升级，旧客户端不受支持。
