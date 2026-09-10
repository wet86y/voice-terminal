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
| `speech.appended`、`speech.append_waiting_prefix`、`speech.append_successor` | 严格前缀续写、suffix 后续队列与追加关联 |
| `error` | 可恢复或不可恢复错误 |

`speech.queued`、`speech.delta`、`speech.progress` 和 `speech.completed` 是文本进入语音事务后的真实状态；它们不等待完整 JSON 信封提交。用户讲话期间队列不启动 TTS，不生成 PCM。只有实际播放的文本才可能出现在 DeliveryReport。`speak.preview`（如启用）仅是模型流调试观察，不是队列提交或撤销状态。模型请求尾部的 `runtime.speak` 使用 `state=empty|queued|suspended|speaking`、`owner_scope=none|current_turn|previous_turn` 和 `continuation_allowed` 表达语音所有权；只有当前轮会收到可续写的 `target_text`，上一轮活动语音只以 `observed_text` 可见。`speech.append_waiting_prefix` 表示同轮新流尚未超出当前完整文本；`speech.appended` 表示已提取并提交 suffix；`speech.append_successor` 表示同轮逻辑 speech 已进入追加窗口或完成，suffix 被安排为不重播前缀的后续项。前缀已经合法但后续 JSON/工具错误时，已追加文本不会撤销；错误通过 `error`/`agent.trace` 和即时恢复请求报告。`speech.replaced` 仍表示实际文本分歧、用户 barge-in 或其他显式抢占，不用于普通 suffix 追加。`agent.reasoning` 仅限受控本地调试环境。

客户端可发送 `session.reasoning_mode` 命令，payload 为 `{"mode":"fast"|"balanced"|"deep"}`。服务端在运行时快照中回传 `reasoning_mode`；无效模式返回可恢复的 `INVALID_REASONING_MODE`。模式变更不会重写当前已冻结的 Agent schema，新的 Agent episode 使用新选择；`session.reset` 会恢复配置默认模式。

追加相关 `speech.progress` 可带 `event=append_window_opened` 或 `event=append_window_expired`。追加事件中的 `speech_id`、`successor_speech_id`、`baseline_speech_id`、`baseline_generation`、`appended_chars` 和 `provider_generation` 只用于关联，不构成新的语义版本；客户端必须继续按 `seq` 排序并按 speech/generation 丢弃迟到事件。

`agent.trace` 可使用 `LLM_SPEAK_CONTEXT`、`SPEECH_PREFIX_RECONCILED` 和 `CROSS_TURN_SPEECH_PREFIX_STRIPPED` 重建所有权及防重决策。服务端持久日志只记录状态、baseline 来源和字符长度，不记录用户或 assistant 正文。`CROSS_TURN_SPEECH_PREFIX_STRIPPED` 表示模型违反跨轮隔离，但旧前缀已在进入 TTS 前被本地剥离。

不存在 native `tool_calls`、`tool_call_id`、`speak(queue|modify)`、`session_id`、`input_id`、`episode_id`、`decision_id`、`generation_id`、`input_revision`、`context_version` 或 V0.4/V0.5 客户端映射。V0.6 是同步破坏性升级，旧客户端不受支持。
