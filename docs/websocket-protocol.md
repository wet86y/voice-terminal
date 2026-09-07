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
| `agent.state`、`agent.trace`、`agent.prefill`、`agent.reasoning`、`context.snapshot` | Agent 状态、预填充/本地推理调试、有限诊断、实际模型请求 |
| `tool.state` | 只读工具执行、完成或失效 |
| `speech.queued`、`speech.started`、`speech.delta`、`speech.progress`、`speech.completed` | 已授权语音输出、队列与传达结算 |
| `error` | 可恢复或不可恢复错误 |

`speech.queued`、`speech.started`、`speech.progress` 和 `speech.completed` 都带 `kind: prefill | formal`；只有实际播放的文本才可能出现在 DeliveryReport。`agent.prefill` 展示候选的 `EMPTY`、`PREPARED`、`CANCELLED`、`QUEUED`、`PLAYED` 或 `STALE` 状态。`agent.reasoning` 仅限受控本地调试环境。

不存在 `turn.*`、`assistant.delta`、`tts.*`、`llm.context`、预测专属事件或旧客户端事件映射。V1 即 V0.4 的唯一公开契约，旧客户端和旧配置不受支持。
