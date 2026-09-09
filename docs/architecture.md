# 架构与组件契约

`SessionRuntime` 是唯一装配入口。它协调但不实现输入、Agent 或语音细节。

| 组件 | 输入 | 输出 | 唯一职责 |
| --- | --- | --- | --- |
| Conversation runtime | 生命周期事件 | `conversation_id`、`turn_id`、取消作用域 | 生命周期与显式取消 authority |
| Input pipeline | PCM、VAD、ASR、分析结果 | `TurnWorkspace` | 维护当前轮次、阶段、Paraformer 主文本与 SenseVoice 补充证据 |
| Session ledger | 最终输入、传达报告、选中的工具交换 | 实际模型 messages、历史投影 | 唯一上下文事实源 |
| Agent runtime | 账本投影、句子流、环境事件 | V0.6 JSON `speak` 决策 | 400ms 单飞调度、信封校验与异常恢复 |
| Action runtime | 本地 `actions` 请求 | `ToolExchange` | 只读执行、缓存与来源标记 |
| Speech runtime | 文本 speak 队列、TTS、播放器 | `SpeechItem`、`DeliveryReport` | `empty/queued/suspended/speaking`、严格前缀追加、实质分歧抢占、取消与传达结算 |
| Protocol projection | 内部事件 | V0.6 Envelope | 唯一网络协议边界 |

输入中断只能通过显式取消作用域停止当前语音；普通定稿、精修和工具结果不会取消在飞请求。新的完整 `speak.text` 先与当前逻辑 speech 文本做严格字符前缀比对：完整覆盖旧文本时只创建 suffix 后续队列，前缀范围内出现差异时才创建 replacement generation。Speech Queue 中未开始的项目不会写入 heard_text，只保存文本；当前输出耗尽后保留 1 秒追加窗口，窗口外后续项仍只合成新增 suffix。`request_id`、`speech_id` 和 `output_generation` 只用于追踪与播放器代际校验，不参与语义判旧。等待结束当前决策，而不结束连续对话。
