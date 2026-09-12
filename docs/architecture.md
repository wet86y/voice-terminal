# 架构与组件契约

> 当前为生产运行的 Responses/`json_schema` 协议。活动 LLM 路径只有 Responses；Chat/`json_object` adapter 已冻结，仅用于显式回退。快速、均衡与深度思考模式均经过同一 Agent 主链；普通双路镜像仍不进入生产流量。

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
| Agent runtime | 账本投影、当前运行态、episode 级人格文档快照 | V0.6 JSON `speak` 决策 | 唯一 `AgentDecisionScheduler`、固定400ms心跳、普通优先的阶梯强制调度、static/history/tail context、信封校验、思考模式控制与有限恢复 |
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

### AEC 保护的播放期插话

播放回调把实际提交给设备的 PCM（包括补零）异步送入 `AudioPreprocessor` 的反向参考流；麦克风 PCM 固定按 16 kHz 单声道、10 ms 帧执行 AEC/NS，并由处理后的 WebRTC VAD 生成 `near_voice=true|false|unknown`。播放期间的 `BargeInGate` 只接受 `true`，并要求反向参考新鲜、处理器处于 `READY` 或 `RECOVERED`、持续语音达到既有最小时长。`false` 与 `unknown` 都清空候选，不提交 ASR，不触发用户打断；不存在原始麦克风 VAD 的自动回退。

`audio.aec_failure_policy` 决定保护链在已播放 PCM 期间失效时的行为。默认 `pause_recover` 原子暂停播放器队列、重建处理器并等待新的反向参考后续播；恢复失败或 750 ms 超时会转为 `stop_and_listen`，停止当前输出并释放正常监听，避免无限暂停或用户被静默锁死。可选 `stop_and_listen` 直接采用该安全终止路径；`continue_without_barge_in` 仅继续播放并暂禁自动插话，供兼容性排障使用。状态转换与结果记录为 `AEC_RECOVERING`、`AEC_RECOVERED`、`AEC_FAILED` 或 `AEC_BARGE_IN_SUPPRESSED`；每个 `BARGE_IN_CONFIRMED` 必须同时记录 `near_voice=true` 和 `protected_available=true`。

Responses 边界因供应商 strict 子集要求所有声明属性均为 required：`wait/cancel` 使用空 `speak.text`，异构 action 使用中性占位字段；进入 Agent 后立即投影回原有内部契约。本地信封校验和 action adapter 不得因 provider 返回 `completed` 或标记 `strict=true` 而跳过。

## AgentDecisionScheduler 调度契约

Agent runtime 只有一个 `AgentDecisionScheduler` 实例和一个 `_run()` 循环。`_run()` 以固定 deadline 持续推进本地 400ms 心跳：下一次 deadline 等于上一次 deadline 加 400ms，不使用“当前时间加400ms”的漂移式计时。心跳只读取唤醒信号、合并 `pending`、观察 active request/TTS/用户输入状态、推进窗口并决定是否创建一个 LLM task；它不等待正在运行的 provider，也不为每个心跳调用 provider。

普通事件在400ms边界合并为一次请求。普通语义事件包括 ASR segment/final、SenseVoice 有语义变化的 refinement、工具完成和 thinking barrier 完成等现有事件。`SPEECH_STARTED`、`SPEECH_ENDED`、`ENDPOINT_CANDIDATE`、`VOICE_ACTIVE` 只作为本地活动信号，不覆盖强制窗口；所有 `TTS_*` 事件完全属于输出侧生命周期，不进入调度 mailbox、不重置静默锚点、不触发普通或强制请求。

### 流式 ASR final 确认与连续输入

同一用户轮次的 ASR 有两个正常阶段：稳定分段 `USER_SEGMENT_UPDATED` 用于降低首响延迟，端点后的 `USER_INPUT_FINALIZED` 是输入完成的权威信号。若稳定分段已经以相同规范化文本发起请求，且该请求最终形成有效 `speak`，同文 final 只确认该输入已结束，不得再创建第二个普通 LLM 请求。若先前请求返回 `wait`、被取消或失败，则释放 final 回到普通调度，保证权威输入不会因推测失败而丢失。

final 与流式稿不同时，final 始终是权威主文本；本地仅保守地区分“追加后缀”和“修订”，并将流式稿到最终稿的差异作为本轮语义修订上下文交给模型。模型沿用现有同轮 append/replace/wait 决策，不重复已传达的回答。该机制不改变 LLM JSON 信封协议，也不关闭 `USER_SEGMENT_UPDATED` 的低延迟抢发。

连续用户输入采用 latest-wins 的对话策略，而非逐 `turn_id` 排队播放回答：只要每个已 final 的输入已写入 append-only session ledger，最终请求就应基于累计会话内容形成一次整体回答。没有单独 TTS 的中间 turn 不自动视为漏答；只有已 final 内容未进入后续模型上下文，或在用户明显转题后补播过时回答，才属于需要修复的行为偏差。该策略持续通过真实会话观察，不因日志中缺少单独 speech transaction 而改为 FIFO。

输出 handle 在 PCM 耗尽后仍保留一秒同轮追加窗口；它是逻辑续写边界而非可听音频。barge-in 只能在播放器的保守 `remaining_ms > 0` 时取消语音，避免播尽后的新用户发言将完整 delivery 错记为 `interrupted/full`。MiniMax one-shot 会话在本地主动 `close()` 时取消 receiver 属于正常资源回收，不应记录为未确认 `task_finished` 的接收失败；非本地关闭的 receiver 取消仍保留为错误。

用户有内容的 `USER_INPUT_FINALIZED`，或有语义变化的非空 `USER_INPUT_REFINED`，会重新 arm 当前静默阶梯。无普通语义事件时，窗口 3、6、9、16、22 选择 `forced_poll`，窗口 25 选择一次 `final_silence`；窗口 10 仅表示进入慢速阶段。普通事件和强制窗口在同一个400ms边界同时存在时，普通调度覆盖强制调度，记录 `covered_by_normal_dispatch`，不添加强制提示词、不创建 `system_check_id`，也不在普通请求后补发强制请求。普通强制窗口在 busy 状态下直接跳过；最终窗口在 busy 状态下保留 `final_pending`，恢复空闲后只补发一次。

三类 dispatch 仍共用 `AgentEpisodeRuntime._step()`、`_provider_stream()` 和原有 provider 参数，只在 request tail 增加互斥的 `forced_poll` 或 `final_silence` 提示词。system-check 使用独立 `system_check_id`，不开始用户 turn、不创建用户 `InputRecord`；已听到的 speech 仍通过 assistant delivery 写入 ledger。active request、TTS 播放/待播内容或用户输入繁忙时不会创建并发 LLM 请求。恢复/backoff 继续使用既有有限队列和优先级，不拆出第二个调度器或 watchdog。
