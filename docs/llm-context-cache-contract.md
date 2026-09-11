# LLM 上下文缓存契约

本文件是所有模型可见字段的唯一准入规范。目标是在不减少 Agent 的语音语义决策权、不中断已完成历史的前提下，让 DeepSeek 可以持续命中稳定请求前缀。

## 缓存模型与观测

DeepSeek 的前缀缓存按请求开头连续 token 匹配。官方 Responses 在 `usage.input_tokens_details.cached_tokens` 报告命中；兼容 Chat 端点可能使用 `prompt_cache_hit_tokens` 与 `prompt_cache_miss_tokens`。中转端点若不返回这些字段，UI 与 trace 显示“未提供”，绝不能解释为零命中。

每轮观察 `provider_input_tokens`、`provider_cached_tokens`、`provider_cache_miss_tokens` 和 `provider_cache_hit_ratio`。标准累积对话的第二、第三轮 cached tokens 应随 append-only 历史增长。若只固定不增长，先检查是否把时间、可用性或动态 system 文本拼进了 `instructions`。

## 三层 ContextLane

| Lane | 允许内容 | 生命周期与位置 | 禁止内容 |
| --- | --- | --- | --- |
| `STATIC_INSTRUCTIONS` | 固定 Agent 规则、固定 schema 兼容说明、会话日期基准、冻结动作目录、episode 级人格/用户/行为文档 | episode 内不变；Responses `instructions` | 秒级时间、播放状态、工具可用性、恢复反馈 |
| `HISTORY` | 最终用户输入、实际 `heard_text`、结算的打断/替换、完成工具资料、持久精修 | 只能在末尾追加；Responses `input` | 未播文本、重排/摘要旧条目、运行时 ID |
| `REQUEST_TAIL` | 当前输入候选、秒级时间、日期 epoch、活跃 speak、精修、工具可用性、恢复反馈 | 只存在于本请求末尾；Responses `input` | 固定规则、需要在未来复用的事实 |

`ContextFrame` 是唯一模型消息构造器。没有 lane 的帧会在 provider 投影时失败；内部 `context_lane` 不会发送到 provider。Responses 仅将 `STATIC_INSTRUCTIONS` 合并到 `instructions`，其余 role、顺序和内容原样进入 `input`。冻结的 Chat Completions 回退只去除内部 lane，保留原有消息顺序。

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
| `HH:MM:SS` 秒级时间 | Context projector | REQUEST_TAIL | 否 | 否 | tail placement test |
| 跨午夜日期 epoch | Conversation runtime | REQUEST_TAIL | 否 | 否 | midnight test |
| 活跃 speak state/target_text | Speech runtime | REQUEST_TAIL | 否 | 否 | append/replace/cancel test |
| 工具实时可用性、恢复反馈 | Agent runtime | REQUEST_TAIL | 否 | 否 | frozen schema test |

时间尾部格式为 `{"runtime":{"clock":{"local_time":"HH:MM:SS","precision":"second"},"speak":...}}`。非 empty speak 才带 `target_text`，让 LLM 自行决定 append、replace、wait 或 cancel；当前进度不得伪造为实时 heard cursor。会话跨午夜不 reset：静态基准保留，尾部持续附加新的日期 epoch、星期和时区。

## 不可违反的规则

- 不要把动态 system 状态、时间、可用性或恢复反馈追加到 `instructions`。
- 不要改写、截断、摘要、重排 HISTORY，也不要把未听到的文本写为 assistant 历史。
- 不要向模型发送 request/speech/turn ID、调度 tick、性能时间或 Unix timestamp。
- 不要在会话中更换动作目录、strict schema 或 `request_thinking` 可见性；不可用状态只能进入 tail，本地仍执行参数和权限校验。
- REQUEST_TAIL 必须在当前 user frame 之后。新字段须先填准入表并补 placement、digest 与回归测试。

运行时守护计算静态 instructions、历史序列和 schema/action catalog 的 canonical digest：同一 session 的静态/schema digest 不得变化；新的 history 必须以前次 history 为前缀。`llm.context` 诊断事件保留完整内部投影，供人工核对 lane placement。

## 官方 DeepSeek 手动验收

使用官方 `DEEPSEEK_API_KEY` 和 DeepSeek Responses profile，而非中转：

```powershell
python .\scripts\probe_context_cache.py --model deepseek-chat
python .\scripts\probe_context_cache.py --model deepseek-chat --dynamic-instructions
```

首个命令执行三轮 append-only 历史并让尾部秒级时间变化，输出每轮 `input_tokens/cached_tokens/hit_ratio`；第 2、3 轮 cached tokens 应随着历史增长。第二个命令是把秒级时间错误放进 instructions 的反例，命中会退化为较短的固定前缀；它只用于报告，绝不能进入生产代码。中转 profile 单独记录，缺少 usage 字段时结论为“不可观测”。
