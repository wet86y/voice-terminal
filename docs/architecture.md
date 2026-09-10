# 架构与组件契约

> 当前处于 Responses/`json_schema` 协议切换后的 Bug 检修期。活动 LLM 路径只有 Responses；Chat/`json_object` adapter 已冻结并隐藏，仅用于显式回退。检修期不得同时启用普通双路镜像或 `request_thinking`，避免协议缺陷与新功能缺陷混杂。

`SessionRuntime` 是唯一装配入口。它协调但不实现输入、Agent 或语音细节。

| 组件 | 输入 | 输出 | 唯一职责 |
| --- | --- | --- | --- |
| Conversation runtime | 生命周期事件 | `conversation_id`、`turn_id`、取消作用域 | 生命周期与显式取消 authority |
| Input pipeline | PCM、VAD、ASR、分析结果 | `TurnWorkspace` | 维护当前轮次、阶段、Paraformer 主文本与 SenseVoice 补充证据 |
| Session ledger | 最终输入、传达报告、选中的工具交换 | 实际模型 messages、历史投影 | 唯一上下文事实源 |
| LLM provider boundary | 同一份语义 messages、运行时 action schema | 统一 `content/reasoning/usage/finish` 事件 | Responses 投影、strict Schema、终止状态归一化；保留冻结 Chat 回退 adapter |
| Agent runtime | 账本投影、句子流、环境事件 | V0.6 JSON `speak` 决策 | 400ms 单飞调度、信封校验与异常恢复 |
| Action runtime | 本地 `actions` 请求 | `ToolExchange` | 只读执行、缓存与来源标记 |
| Speech runtime | 文本 speak 队列、TTS、播放器 | `SpeechItem`、`DeliveryReport` | `empty/queued/suspended/speaking`、严格前缀追加、实质分歧抢占、取消与传达结算 |
| Protocol projection | 内部事件 | V0.6 Envelope | 唯一网络协议边界 |

输入中断只能通过显式取消作用域停止当前语音；普通定稿、精修和工具结果不会取消在飞请求。新的完整 `speak.text` 先与当前逻辑 speech 文本做严格字符前缀比对：完整覆盖旧文本时只创建 suffix 后续队列，前缀范围内出现差异时才创建 replacement generation。Speech Queue 中未开始的项目不会写入 heard_text，只保存文本；当前输出耗尽后保留 1 秒追加窗口，窗口外后续项仍只合成新增 suffix。`request_id`、`speech_id` 和 `output_generation` 只用于追踪与播放器代际校验，不参与语义判旧。等待结束当前决策，而不结束连续对话。

Responses 边界因供应商 strict 子集要求所有声明属性均为 required：`wait/cancel` 使用空 `speak.text`，异构 action 使用中性占位字段；进入 Agent 后立即投影回原有内部契约。本地信封校验和 action adapter 不得因 provider 返回 `completed` 或标记 `strict=true` 而跳过。
