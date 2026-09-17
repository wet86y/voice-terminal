# LLM 上下文缓存契约

本文件是所有模型可见字段的唯一准入规范。目标是在不减少 Agent 的语音语义决策权、不中断已完成历史的前提下，让 DeepSeek 可以持续命中稳定请求前缀。

## 缓存模型与观测

DeepSeek 的前缀缓存按请求开头连续 token 匹配。官方 Responses 在 `usage.input_tokens_details.cached_tokens` 报告命中；兼容 Chat 端点可能使用 `prompt_cache_hit_tokens` 与 `prompt_cache_miss_tokens`。中转端点若不返回这些字段，UI 与 trace 显示“未提供”，绝不能解释为零命中。

每轮观察 `provider_input_tokens`、`provider_cached_tokens`、`provider_cache_miss_tokens` 和 `provider_cache_hit_ratio`。标准累积对话的第二、第三轮 cached tokens 应随 append-only 历史增长。若只固定不增长，先检查是否把时间、可用性或动态 system 文本拼进了 `instructions`。

## 三层 ContextLane

| Lane | 允许内容 | 生命周期与位置 | 禁止内容 |
| --- | --- | --- | --- |
| `STATIC_INSTRUCTIONS` | 固定 Agent 规则、固定 schema 兼容说明、会话日期基准、冻结动作目录、episode 级人格/用户/行为文档 | episode 内不变；Responses `instructions` | 秒级时间、播放状态、工具可用性、恢复反馈 |
| `HISTORY` | 最终用户输入、冻结的 `<voice_delivery>` 播放结算、结算的零已听打断/替换、完成工具资料、持久精修 | 只能在末尾追加；Responses `input` | 未结算文本、重排/摘要旧条目、运行时 ID |
| `REQUEST_TAIL` | 当前输入候选、秒级时间、日期 epoch、活跃 speak、精修、工具可用性、恢复反馈 | 只存在于本请求末尾；Responses `input` | 固定规则、需要在未来复用的事实 |

`ContextFrame` 是唯一模型消息构造器。没有 lane 的帧会在 provider 投影时失败；内部
`context_lane` 与 `context_kind` 均不会发送到 provider。Responses 仅将
`STATIC_INSTRUCTIONS` 合并到 `instructions`，其余 role、顺序和 content 进入 `input`。
冻结的 Chat Completions 回退同样去除内部元数据，保留模型可见的消息顺序。

已结算语音在 `commit_delivery()` 时一次性生成并冻结模型可见内容。完整播放使用
`<voice_delivery status="full">…</voice_delivery>`；部分播放使用同一包装中的
`heard`、`uncertain`、`unheard` 子元素。投影后不得重新渲染、补字段或修改既有包装，
后续结算只能在 HISTORY 末尾追加，以保持 DeepSeek 连续 token 前缀缓存稳定。

### 语音传达包装语义

`voice_delivery` 是 assistant 历史消息的模型可见包装，不是新的 assistant 回复，也不应
被复制到新的 `speak.text` 中。包装只在播放结算完成后生成一次：

```xml
<voice_delivery status="full">已完整播放并被用户听到的文本</voice_delivery>
```

被打断或替换时，如果用户已经听到部分内容，使用：

```xml
<voice_delivery status="partial" termination="interrupted"><heard>确定听到</heard><uncertain>边界不确定</uncertain><unheard>确定未听到</unheard></voice_delivery>
```

`heard` 是已经传达的事实，`uncertain` 不得当作已传达，`unheard` 也不代表下一次调度
必须自动补播。下一次 `forced_poll` 是否 `speak`，只取决于最近一次已传达内容之后是否出现
新的、仍然相关且值得用户现在听到的信息。没有信息增量时使用 `wait`；Shadow 仍 active、
后台任务仍运行、播放队列变空、时间经过以及调度器触发本身都不构成信息增量。

`forced_poll` 可以在有新结果、必要纠错、事实变化或确实需要传达的未听内容时再次开口，
但只能表达新增或修正部分，不能复述已传达内容。`actions` 与 `speak` 独立决定：需要
继续后台处理但没有新的用户可见内容时，可以保持 `wait` 并请求相应 action。

### DeepSeek 缓存切换不变量

新协议采用直接切换：新建 episode 后所有 speech history 都使用 `voice_delivery_v1`。
同一 episode 内，已经发送过的 HISTORY 永远不回写；`model_content` 在结算时冻结，后续
请求只复制该字符串。不得在一次请求中先发送裸 assistant 文本、下一次请求再为同一条文本
补上包装，也不得把 speech ID、request ID、时间戳、播放时长、队列状态或 Shadow 状态放入
包装。这样 DeepSeek 请求仍保持“稳定 STATIC + 只追加 HISTORY + 动态 REQUEST_TAIL”的
连续 token 前缀，新增包装只作为当轮新增后缀参与首次计算。

### Shadow Responses 回放不变量

Shadow 的 thinking+tools 请求遵循同一条追加原则：正常完成的 `reasoning`、
`function_call`、`function_call_output` 和 `message` item 原样保留，并在下一次 activation
按原顺序回传；不能因为一次 activation 暂停/恢复而批量重写或重新包装整个历史。DeepSeek
可能接受一轮成功响应，却因该响应缺少非空 `reasoning_text` 而拒绝下一轮工具回放。此时
provider 只标记这一轮为不可直接回放，并把该轮（包括对应工具结果）转换为协议无关的
`system` 数据记录；此前合法前缀、缓存重叠和后续追加顺序保持不变。该兜底只处理实际
触发问题的单轮，不改变标准协议路径。

## V1 请求尾部信封

主 Agent 的模型可见请求固定按以下顺序排列：

1. episode 内冻结的 `STATIC_INSTRUCTIONS`；
2. 只追加、不改写的 `HISTORY`；
3. 可选的 `current_user_input`；
4. 恰好一个 `voice_request_context`；
5. 恰好一条最终 JSON 输出约束。

尚未进入历史的当前用户输入无损包装为：

```json
{"type":"current_user_input","version":1,"content":"原始完整文本"}
```

已经按 `turn_id` 提交到 ledger 的用户输入仍只保留在 `HISTORY`，不得在 tail 重复。恢复反馈、
秒级运行状态、工具状态和强制调度提示不再作为彼此分散的 system 消息发送，而是保持原始文本和顺序，
合并为：

```json
{
  "type": "voice_request_context",
  "version": 1,
  "entries": [
    {"index": 0, "kind": "runtime_state", "content": "原始完整状态文本"}
  ]
}
```

已定义的 `kind` 为 `minor_protocol_feedback`、`immediate_recovery`、
`deferred_recovery`、`runtime_state`、`pending_tools`、`forced_poll`、
`final_silence` 和兜底的 `system_context`。`content` 不得被摘要、清洗或重新解释；只有既有的
上下文超限策略可以处理请求副本，语义 ledger 不变。

状态信封之后固定追加简短输出边界，要求最终 `output_text` 只能是一个符合
`voice_decision_v06` 的完整 JSON 对象。这条约束必须是整个模型输入的最后一条消息；工具、恢复、
runtime、Shadow 或调度代码都不得在其后追加内容。Provider 的 `text.format` Schema 仍是权威结构约束，
尾部提示不能替代本地解析与校验。

人格上下文文件位于 `config/persona/`，由 Agent 在新的 episode 首次建立时读取一次。
`instructions.md`、`personality.md` 和 `user.md` 均属于 `STATIC_INSTRUCTIONS`，并按
“指令集合 → 人格主体 → 用户资料”的固定顺序追加到内置协议和动作目录之后。文件
内容改变不会在当前 episode 内重新读取，因此不会改变已建立的 static digest；停止后
重新启动 Agent 或 session reset 创建新 episode 后，新的文档内容才会进入新的静态前缀。
缺失、空文件或读取失败的文档不会替换为隐藏默认文本，只会被跳过并记录 warning。

## 标准字段准入表

| 字段 | 语义 owner | lane | 持久化 | 影响 schema | UI/测试责任 |
| --- | --- | --- | --- | --- | --- |
| Agent 固定规则、schema compatibility | Agent runtime | STATIC | 否 | 是（冻结） | provider placement test |
| 会话日期基准 | Conversation runtime | STATIC | reset 时重建 | 否 | 跨午夜 test |
| `instructions.md`、`personality.md`、`user.md` 文档快照 | Agent runtime | STATIC | 否 | 否 | episode snapshot / placement test |
| 最终用户文本 | Session ledger | HISTORY | 是 | 否 | ledger replay test |
| 已完成 speak `heard_text` | Speech/ledger | HISTORY | 是 | 否 | delivery test |
| 工具资料、结算中断、持久精修 | ledger | HISTORY | 是 | 否 | replay/order test |
| 尚未提交的当前用户输入 | Context projector | REQUEST_TAIL / `current_user_input` | 否 | 否 | wrapper/order test |
| `HH:MM:SS` 秒级时间 | Context projector | REQUEST_TAIL / `runtime_state` | 否 | 否 | tail placement test |
| 跨午夜日期 epoch | Conversation runtime | REQUEST_TAIL | 否 | 否 | midnight test |
| 活跃 speak state/target_text | Speech runtime | REQUEST_TAIL | 否 | 否 | append/replace/cancel test |
| 工具实时可用性、恢复反馈 | Agent runtime | REQUEST_TAIL / typed entry | 否 | 否 | frozen schema test |
| 最终 JSON 输出边界 | Agent runtime | REQUEST_TAIL / `output_constraint` | 否 | 否 | final-position test |

时间尾部格式为 `{"runtime":{"clock":{"local_time":"HH:MM:SS","precision":"second"},"speak":...}}`。非 empty speak 才带 `target_text`，让 LLM 自行决定 append、replace、wait 或 cancel；当前进度不得伪造为实时 heard cursor。会话跨午夜不 reset：静态基准保留，尾部持续附加新的日期 epoch、星期和时区。

## 不可违反的规则

- 不要把动态 system 状态、时间、可用性或恢复反馈追加到 `instructions`。
- 不要改写、截断、摘要、重排 HISTORY，也不要把未听到的文本写为 assistant 历史。
- 不要向模型发送 request/speech/turn ID、调度 tick、性能时间或 Unix timestamp。
- 不要在会话中更换动作目录、strict schema 或 `request_thinking` 可见性；不可用状态只能进入 tail，本地仍执行参数和权限校验。
- REQUEST_TAIL 必须位于所有 HISTORY 之后；可选 current user 必须位于状态信封之前。最终输出约束必须
  恰好出现一次并处于整个请求末尾。新字段须先填准入表并补 placement、digest 与回归测试。

运行时守护计算静态 instructions、历史序列和 schema/action catalog 的 canonical digest：同一 session 的静态/schema digest 不得变化；新的 history 必须以前次 history 为前缀。`llm.context` 诊断事件保留完整内部投影，供人工核对 lane placement。

## 官方 DeepSeek 手动验收

使用官方 `DEEPSEEK_API_KEY` 和 DeepSeek Responses profile，而非中转：

```powershell
python .\scripts\probe_context_cache.py --model deepseek-chat
python .\scripts\probe_context_cache.py --model deepseek-chat --dynamic-instructions
```

首个命令执行三轮 append-only 历史并让尾部秒级时间变化，输出每轮 `input_tokens/cached_tokens/hit_ratio`；第 2、3 轮 cached tokens 应随着历史增长。第二个命令是把秒级时间错误放进 instructions 的反例，命中会退化为较短的固定前缀；它只用于报告，绝不能进入生产代码。中转 profile 单独记录，缺少 usage 字段时结论为“不可观测”。
