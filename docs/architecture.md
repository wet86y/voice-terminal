# 架构与组件契约

`SessionRuntime` 是唯一装配入口。它协调但不实现输入、Agent 或语音细节。

| 组件 | 输入 | 输出 | 唯一职责 |
| --- | --- | --- | --- |
| Input pipeline | PCM、VAD、ASR、分析结果 | `InputWorkspace` | 维护转写修订、Paraformer 主文本与 SenseVoice 补充证据 |
| Session ledger | 最终输入、传达报告、选中的工具交换 | 实际模型 messages、历史投影 | 唯一上下文事实源 |
| Agent runtime | 账本投影、最新输入、环境事件 | `AgentDecision` | LOW 推理下选择 prefill/speak/wait/finish/tool |
| Tool runtime | 工具调用与决策身份 | `ToolExchange` | 只读执行、缓存与来源标记 |
| Speech runtime | 已授权 prefill/speak、TTS、播放器 | `SpeechItem`、`DeliveryReport` | FIFO 播放、取消与传达结算 |
| Protocol projection | 内部事件 | V1 Envelope | 唯一网络协议边界 |

输入中断只能使当前语音输出失效；它不能直接写播放器状态。Speech Queue 中未开始的项目不会写入 heard_text。工具只在活动 Agent 决策内执行，且只有仍属于当前 `episode_id + decision_id + context_version` 的结果可写入账本。等待结束当前决策，而不结束 Episode。
