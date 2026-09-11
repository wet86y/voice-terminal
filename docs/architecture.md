# 架构与组件契约

> 当前处于 Responses/`json_schema` 协议切换后的 Bug 检修期。活动 LLM 路径只有 Responses；Chat/`json_object` adapter 已冻结并隐藏，仅用于显式回退。检修期不得同时启用普通双路镜像或 `request_thinking`，避免协议缺陷与新功能缺陷混杂。

`SessionRuntime` 是唯一装配入口。它协调但不实现输入、Agent 或语音细节。

## LLM provider profiles

LLM provider 只影响上游模型的连接目标，不改变 `SessionRuntime`、Responses/strict
JSON Schema 路径、Agent 决策信封或 V0.6 WebSocket 协议。真实对话默认使用官方
DeepSeek 的 `deepseek-flash`，读取 `DEEPSEEK_API_KEY`；调试 OpenCode Go 时可在启动
命令中显式切换：

```powershell
.\scripts\start.ps1 -LlmProvider opencode_go
```

该调试 profile 使用 `https://opencode.ai/zen/go/v1`、`deepseek-flash` 和
`OPENCODE_GO_API_KEY`。也可以通过 `VOICE_LLM_PROVIDER=opencode_go` 临时覆盖当前
进程的 provider；未指定覆盖时，配置文件中的 `provider: deepseek` 保持真实测试默认值。
两种 profile 都复用同一套 Responses 解析、strict schema 校验、重试与协议恢复逻辑，
不启用自动 provider fallback。切回官方路径只需移除覆盖变量，或显式使用
`-LlmProvider deepseek`。

| 组件 | 输入 | 输出 | 唯一职责 |
| --- | --- | --- | --- |
| Conversation runtime | 生命周期事件 | `conversation_id`、`turn_id`、取消作用域、日期 epoch | 生命周期、显式取消 authority 与稳定日期基准 |
| Input pipeline | PCM、VAD、ASR、分析结果 | `TurnWorkspace` | 维护当前轮次、阶段、Paraformer 主文本与 SenseVoice 补充证据 |
| Session ledger | 最终输入、传达报告、选中的工具交换 | append-only 可缓存 HISTORY | 唯一持久上下文事实源 |
| LLM provider boundary | 带 `ContextLane` 的 frames、冻结 schema | 统一 `content/reasoning/usage/finish` 事件 | 缓存稳定投影、lane 校验、usage cache 指标归一化、strict Schema；保留冻结 Chat 回退 adapter |
| Agent runtime | 账本投影、当前运行态、episode 级人格文档快照 | V0.6 JSON `speak` 决策 | 400ms 单飞调度、构建 static/history/tail context、不改写历史、信封校验、思考模式控制与异常恢复 |
| Action runtime | 本地 `actions` 请求 | `ToolExchange` | 只读执行、缓存与来源标记 |
| Speech runtime | 文本 speak 队列、TTS、播放器 | `SpeechItem`、`DeliveryReport` | `empty/queued/suspended/speaking`、同轮所有权、严格前缀追加、跨轮防重、实质分歧抢占与传达结算 |
| Protocol projection | 内部事件 | V0.6 Envelope | 唯一网络协议边界 |

Agent episode 在第一次真正建立 `episode_id` 时读取 `config/persona/` 下的
`instructions.md`、`personality.md` 和 `user.md`。加载器只处理 UTF-8 文件读取，
返回不可变的文档快照；缺失、空文件或读取失败只记录 warning，不阻止内置 Agent
继续工作。快照在 episode 内不重新读取，因此磁盘上的修改不会破坏静态上下文缓存
前缀；新的 episode 才会获得新版本。

三份文档以独立的 `STATIC_INSTRUCTIONS` system frame 追加到内置协议和冻结动作目录
之后，顺序固定为“指令集合 → 人格主体 → 用户资料”。内置协议、本地 Schema、动作
权限和语音运行时始终是最终权威，文档只能补充对话行为、人格风格和用户事实。

输入中断只能通过显式取消作用域停止当前语音；普通语音活动、定稿、精修和工具结果不会取消在飞请求。用户在 `playback_started=false` 阶段开口时，输出层将未播放候选标记为 `HELD`，保留文本、handle 和当前推理状态；没有候选时则设置全局 hold，使随后完成的候选也等待。TTS session 或 provider 首段音频已经存在，都不代表已经开始播放。`SPEECH_ENDED` 后才释放最新的 `HELD` 候选，恢复同一文本事务；用户的新 ASR 信息仍由 400ms 单飞调度窗口合并，不直接启动第二个请求。只有播放器首次实际提交非零 PCM 后，确认的 barge-in 才进入显式取消路径。完整 `speak.text` 的前缀继承由 `conversation_id + turn_id` 所有权限定：只有活动 handle 属于当前轮时，模型才获得 `target_text` 和 `continuation_allowed=true`，完整覆盖旧文本时只创建 suffix 后续队列，前缀范围内出现差异时才创建 replacement generation。上一轮仍在播放的文本以 `observed_text` 暴露并标记 `owner_scope=previous_turn`，只提供会话认知，不授予续写权；上一轮已经结算后通过 ledger 的 assistant 历史继续可见。Speech Queue 中未开始的项目不会写入 heard_text，只保存文本；当前输出耗尽后保留 1 秒同轮追加窗口。最近一次 full delivery 只作为跨轮异常防重 guard，不进入活动快照：检测到“已听文本 + 非空后缀”时仅提交后缀，完全相同文本则保留合法复述。`request_id`、`speech_id` 和 `output_generation` 只用于追踪与播放器代际校验，不参与语义判旧。等待结束当前决策，而不结束连续对话。

Responses 边界因供应商 strict 子集要求所有声明属性均为 required：`wait/cancel` 使用空 `speak.text`，异构 action 使用中性占位字段；进入 Agent 后立即投影回原有内部契约。本地信封校验和 action adapter 不得因 provider 返回 `completed` 或标记 `strict=true` 而跳过。
