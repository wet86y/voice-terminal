# DeepSeek Responses 返回无效决策 JSON

状态：**开放，P1，已接入有界 JSON 自修复；纯文本降级未启用，provider 主因仍在定位**  

首次单独建档：2026-09-10（Asia/Hong_Kong）  
当前活动路径：官方 DeepSeek `Responses`、`deepseek-flash`、strict `json_schema`

官方契约复核日期：2026-09-11（Asia/Hong_Kong）

## 问题摘要

活动 LLM 请求虽然携带 strict JSON Schema，且上游以 HTTP 200 和
`response.completed` 正常结束，最终 `content` 仍高频不能通过本地 V0.6 决策信封校验。
失败输出不会写入会话语义账本；系统当前只允许一次即时协议恢复。连续失败时，用户可能只看到
`LLM_PROTOCOL_RECOVERY_EXHAUSTED`，且本轮没有可播放回复。

本问题只追踪模型输出格式可靠性及其直接恢复行为。用户打断、模型主动替换播放队列、深度模式
推理展示等问题应分别记录，不能混入本问题的有效率统计。

## 同批调查总览（不计入 JSON 失败率）

为避免本轮已查明的问题随临时日志丢失，这里保留同批调查结论；它们与本 P1 有时间上的关联，但不是
`43/90` 格式失败的组成部分。

| 问题 | 调查结论 | 当前处置状态 |
| --- | --- | --- |
| 深度模式没有真正运行 | `SessionRuntime.reset_session()` 会在会话重置时把客户端已选择的模式覆盖回配置默认值；后续请求因此仍可能记录 `reasoning_effort=none`，推理展示区自然没有 reasoning 内容 | 已修改为重置会话身份时保留当前显式选择，并增加保持 `deep` 的回归测试；仍需下一轮真实会话复验 UI 与上游 effort |
| Responses 高频无效 JSON | 完整终态中 43/90 为 JSON 语法错误，SSE 拼接与 authoritative final 全部一致 | 本文主问题，P1；已增加结构指纹和有界 JSON 自修复，纯文本仍拒绝，provider 主因未修复 |
| speech cleanup 明确代码 Bug | `EpisodeSpeechOutput` 清理路径读取未定义局部变量 `was_active`，历史日志中重复产生 `NameError`，会使 speech task 异常结束 | 已在 settlement 清理 handle 前捕获 active ownership；代码已修，待后续长会话确认异常不再出现 |
| “模型自打断”与用户打断混淆 | 用户所指的是 LLM 在播放期间产生新决定、替换播放队列造成的自我截断，不是 VAD 用户 barge-in 失灵 | 现已接入 AEC 保护：播放期只有处理后 `near_voice=true`、反向参考新鲜时才可确认 barge-in；AEC 异常按可配置策略恢复或停止并监听，不退回原始 VAD |
| 用户讲话时提前触发 LLM | `USER_SEGMENT_UPDATED` 上的 speculative dispatch 是有意的首字延迟优化，不是调度 Bug | 保留；当前体感收益良好，但恢复预算必须与 finalized 请求隔离，避免推测请求提前耗尽唯一 recovery |

这里特别记录一次修正：不能再把“播放中的新 LLM 决定”当作麦克风抢话保护漏洞来修。两条链路分别是：

```text
用户真实打断：VAD candidate → barge-in confirmed → 当前播放取消
模型自我截断：新 LLM 决定 → 输出/播放队列替换 → 旧 speech 被 supersede/cancel
```

前者是已存在且需要保持的功能；后者的频率和意图服从性需要用独立日志指标与提示策略评估。播放期用户打断现受 AEC 保护：只有处理后确认的近端语音可进入取消路径，AEC 不可用时不会退回原始 VAD。

## 当前现场证据

数据源：`logs/voice-server.log`，后端 PID `6720`，完整会话区间
`2026-09-10 23:04:06` 至 `23:18:39 session.stop`。

| 指标 | 数量 |
| --- | ---: |
| `LLM_DISPATCHED` | 147 |
| 完整收到 `deepseek.responses_completed` | 90 |
| `SPEAK_ENVELOPE_INVALID` | 43 |
| `LLM_RECOVERY_QUEUED` / `STARTED` | 26 / 26 |
| `LLM_RECOVERY_COMPLETED` | 14 |
| `LLM_RECOVERY_EXHAUSTED` | 17 |

大量派发会因用户继续讲话而取消，因此格式失败率应以完整响应为分母：

```text
43 / 90 = 47.8%
```

所有 90 个完整 Responses 终态均记录为：

```text
streamed_chars == authoritative_chars
match=True
```

现场没有发现 `response.incomplete`、`stream_final_mismatch`、流断开、HTTP 5xx 或 transport timeout。
因此当前证据不支持“本地 SSE 拼接丢片”假设；无效内容就是上游返回的权威最终 content。

43 次失败均为非空 `envelope_validation`，常见长度包括 5、9、11、12、13、22、39、41、46、
116、147 等字符；记录均为 `provisional_chars=0`、`partial_speech=False`。这说明解析器在这些样本中
没有识别出可安全提前播放的合法 `speak.text` 前缀。

旧日志只保留长度和异常类别，没有保留原始正文或结构指纹，因此不能仅凭长度判断这 43 次的内容完整性。
重新聚合后其长度范围为 5–260 字符，中位数 58；其中 25/43 少于 68 字符、18/43 不少于 68 字符。

## 真实节奏复放实验（2026-09-11）

探针 [`scripts/probe_real_frequency_json.py`](../../scripts/probe_real_frequency_json.py) 从 PID `6720`
日志恢复 147 个 `LLM_DISPATCHED` 时间点、39 个 turn 和可获得的输入阶段，以原始时间间隔复放同版
生产 prompt、Schema、上下文投影和 Responses 请求。实验不启动 TTS/PCM，也不把用户、助手或新生成正文
写入报告。由于旧日志没有保存精确取消时刻，复放允许所有请求跑到终态；因此它适合测输出格式和请求密度，
不能复原原会话的取消率、听见边界或播放队列状态。

完整 1× 节奏报告：
[`reports/real-frequency-json-replay-20260911.json`](../../reports/real-frequency-json-replay-20260911.json)

| 指标 | 结果 |
| --- | ---: |
| 请求 / 完成 | 147 / 147 |
| 严格合法信封 | 120（81.63%） |
| 严格失败 | 27（18.37%） |
| provider/transport 错误 | 0 |
| 延迟中位数 / P95 / 最大值 | 1704.2 / 2818.3 / 3440.2 ms |
| 失败内容分类 | 完整自然语言 22；合法 JSON 但信封不合约 4；歧义容器 1 |

按阶段看，`speculative` 为 18/66 失败（27.27%），`finalized` 为 7/28（25%），`recovery` 和
`refined` 各 1 次失败。真实节奏下能够稳定复现，证明先前短探针全通过并不能代表正式运行；但数据不支持
“单位时间触发越密集，越容易坏”这一简单因果：失败请求之前 5 秒的平均派发数为 1.04，合法请求为 1.44；
前序间隔不小于 5 秒时失败率约 26%，小于 0.5 秒时该批样本为 0%。更可能的触发因素是动态输入阶段、
上下文内容/长度，或 provider 结构化约束的间歇性失效，而不是纯 QPS。

为核查内容完整性，另以 20× 时间压缩复放前 95 个时间点，并只在终端临时查看失败正文；持久报告仍只保存
哈希、长度、结构和合约形状。第一次人工检查的 15 个严格失败中，12 个是语义完整、以终止标点结束的中文
用户回复，2 个是完整 `speak` 对象但漏掉顶层 `actions`，1 个仅把 `emotion` 返回为空字符串。没有发现
这 15 个包含截断正文或推理过程。该检查支持“相当一部分失败只是信封丢失，正文完整”的判断，但不能据此
把任意纯文本都当作安全输出。

兼容候选复放报告：
[`reports/real-frequency-json-compatibility-candidate-20260911.json`](../../reports/real-frequency-json-compatibility-candidate-20260911.json)

该次 95 请求中有 19 个严格失败，保守解析器恢复 16 个（84.2%）：14 个完整中文自然语言、2 个缺
`actions`。其余 3 个包含两个“正文混入 JSON 容器”与一个额外闭合符/容器歧义，全部拒绝。注意 20×
复放只是候选规则验证，不用于推断正式频率本身的错误率。

## 已接入的本地 JSON 兼容解析边界

Responses 严格解析失败后会尝试一次本地兼容解析；成功时记录 `LLM_OUTPUT_COMPAT_RECOVERED`、修复类型、
长度和结构指纹，不记录正文，也不消耗 protocol recovery。当前只接受以下可确定解释：

- 单个完整 JSON code fence、UTF-8 BOM、容器尾逗号、JSON 字符串内未转义控制字符；
- 顶层只有一个完整合法 `speak` 时补 `actions=[]`；
- 信封形状完全正确但 `emotion=""` 时使用既有中性默认 `calm`；

兼容器明确拒绝任意花括号片段提取、截断补全、歧义分隔符插入、普通纯文本、正文与 JSON 混合、额外字段，
以及从普通文本推断 wait/cancel/action。每次修复都重新执行完整信封校验；最终仍不合约时继续进入原有
protocol recovery，不会让损坏 JSON 静默通过。

接入后的 95 请求压缩复放报告：
[`reports/real-frequency-json-bounded-repair-20260911.json`](../../reports/real-frequency-json-bounded-repair-20260911.json)

该次随机样本有 13 个严格失败：11 个完整普通正文、1 个不完整容器、1 个损坏容器；有界 JSON 修复命中
为 0。这说明当前改动不会覆盖仍占主导的“完全丢失 JSON 外壳”，短期不应预期总体无回应率大幅下降；它
处理的是先前已人工确认的缺 `actions`、空 `emotion` 等低频但无歧义子类。纯文本兼容仍只保留为离线候选，
没有接入运行时。

至少以下 finalized 输入在连续无效后没有创建 TTS，其中后四项有明确的用户追问或继续指令，
可视为可感知的无回应：

| turn | 用户输入摘要 | 结果 |
| --- | --- | --- |
| `input_20260910_230819_000034` | 不配合导致自己被考核 | 连续无效，用户随后问“听到了吗” |
| `input_20260910_231045_000039` | 更正“总监”为“总院” | 连续无效，用户随后问“听到了吗” |
| `input_20260910_231214_000043` | 请求继续刚才没说完的内容 | 连续无效，无 TTS |
| `input_20260910_231746_000055` | “你可以说话了” | 连续无效，用户随后说“喂”唤醒 |

## 同批现场的恢复与无回应问题

这组问题与错误 JSON 来自同一 PID `6720` 会话，应共同保留证据，但修复顺序仍以 JSON 主因为先。

### 恢复耗尽与最终输出

- 43 次语法无效响应中，26 次进入即时 recovery，17 次因 `attempts=1 limit=1` 直接耗尽。
- 有恢复耗尽的轮次不一定全部沉默：同一 turn 的其他并发或后续普通事件有时仍产生合法输出。
- 6 个 finalized turn 在聚合日志中同时满足 `SPEAK_ACCEPTED=0`、`TTS_STARTED=0`；其中 4 个有
  明确的用户追问或继续指令，可确认是用户可感知的无回应。
- `input_20260910_230404_000023` 与 `input_20260910_230604_000029` 处在连续倾诉/“先听着”上下文，
  没有立即发声可能符合 `wait` 意图，不能和解析失败导致的无回应混为一类。

### 历史基线：400ms 调度器并非轮询器

以下内容描述的是本报告采样时的旧实现，不是当前实现。旧版 `AgentDecisionScheduler` 只实现请求
合并窗口：`delay()`、`tick()` 和 `reset()`；Agent runner 最外层等待 `queue.get()`，没有周期任务、
finalized-turn deadline 或 response watchdog。恢复耗尽后代码明确不插入新事件，runner 会回到
`WAITING_EVENT`，直至后续用户、refinement、endpoint 或工具事件唤醒。

现场中用户说“喂”“听到了吗”后系统才恢复回复，实质上是用户输入代替了缺失的兜底唤醒。
现有 `test_whitespace_recovery_reproduction.py` 还明确断言耗尽后不得自重试，用于防止历史 2.5Hz
恢复风暴。因此当前行为不是 timer 偶发失效，而是既有设计选择与“finalized turn 必须有结果”职责冲突。

当前实现已将该边界改为单一固定心跳：只有一个 `AgentDecisionScheduler` 和一个 Agent
`_run()` 循环。400ms heartbeat 使用固定 deadline 持续推进本地窗口，active LLM、TTS 和用户输入
不会暂停 heartbeat，也不会为每个 heartbeat 调用 provider。普通事件在心跳边界积累并优先调度；
没有普通语义事件时，第3、6、9、16、22窗口进行 `forced_poll`，第25窗口进行一次
`final_silence` 检查。普通事件与强制窗口同边界时，普通请求覆盖强制窗口；TTS 和语音活动不重置
用户静默锚点、不进入普通语义队列。普通强制窗口忙碌时跳过，最终窗口忙碌时保留并在恢复空闲后
只补发一次。

### recovery 预算作用域过粗

`_recovery_attempts_by_turn` 仅以 `turn_id` 为 key，且只在 episode 启停时清空。它不会在 recovery
成功、recovery 被用户讲话取消、新 `input_revision` 或 speculative → finalized 阶段转换时重置。

典型失败链：

```text
USER_SEGMENT_UPDATED 的推测请求格式失败
→ 消耗本 turn 唯一即时恢复额度
→ recovery 成功或被后续讲话取消
→ USER_INPUT_FINALIZED 的权威请求再次格式失败
→ 直接 LLM_RECOVERY_EXHAUSTED
→ 旧实现没有强制检查，最终无回应
```

`input_20260910_230819_000034` 是明确样本：早期请求在 `23:08:23.253` 首次失败并消耗 recovery；
finalized/refined 请求在 `23:08:25.476` 再次失败，随后立即耗尽；直到用户在 `23:08:36.893`
询问“听到了吗”才产生新请求。

### recovery 取消缺少终态可观测性

同批日志记录 `LLM_RECOVERY_STARTED=26`、`COMPLETED=14`，但 `FAILED=0`、`SUPERSEDED=0`。
剩余 12 次多数紧邻 `agent.request_cancelled reason=user_interrupted`。`CancelledError` 路径没有记录
recovery cancelled 终态，且已消耗的 turn 额度不会归还。这既影响恢复机会，也使运行统计无法闭合。

### 当前恢复与强制检查边界

协议恢复仍保持有限次数和既有 recovery queue 优先级，不因为强制调度而变成无限 retry。当前实现
区分以下边界：

- speculative 输入恢复预算；
- authoritative finalized revision 恢复预算；
- 同一个400ms调度器中的 `forced_poll` 预算；
- 一次性的 `final_silence` 窗口。

`forced_poll` 只在没有普通语义事件且实际 dispatch 门槛满足时触发；`final_silence` 在 active LLM、
TTS/待播内容或用户输入繁忙时保留，恢复后只消费一次。两类强制请求都复用同一个 `_step()` 和
provider stream 入口，使用互斥提示词，不能作为第二个 scheduler、watchdog 或 provider 调用链路。
普通事件、有效新输入或有效修订仍拥有更高优先级；新输入还会使未播放的 system-check 结果失效。

## 与历史问题的区别

### 2026-09-09 Chat Completions 空白风暴

历史报告 [`reports/recovery-context-and-prompt-analysis-20260910.md`](../../reports/recovery-context-and-prompt-analysis-20260910.md)
记录的是 Chat Completions/`json_object` 返回纯空白 content，加上无上限同态恢复形成请求风暴。

当前问题不同：

- transport 是 Responses；
- content 非空；
- stream 与 authoritative final 完全一致；
- 恢复已有 turn 级上限，不会形成无界风暴；
- 主要风险从“请求风暴”转为“恢复耗尽后无回应”。

### strict Schema capability probe

[`reports/deepseek-current-schema-acceptance-20260910.json`](../../reports/deepseek-current-schema-acceptance-20260910.json)
和 [`reports/responses-schema-capabilities-implementation-20260910.json`](../../reports/responses-schema-capabilities-implementation-20260910.json)
证明上游接受当前 schema 请求，且少量样本可以返回合法信封。它们没有证明 strict schema 在长会话中
被稳定强制执行，也不能代替迁移 runbook 规定的 300 次可靠性验收。

## 已确认的请求与解析边界

Responses 请求由 `DeepSeekClient._responses_payload()` 构造，包含：

```json
{
  "reasoning": {"effort": "none"},
  "text": {
    "format": {
      "type": "json_schema",
      "name": "voice_decision_v06",
      "strict": true,
      "schema": "<本地决策 schema>"
    }
  }
}
```

本地 `SpeechDecisionEnvelopeStream` 仍是权威校验器。不能为降低错误率而直接跳过本地校验、播放任意
普通文本、信任上游 `completed` 状态，或把失败正文写入持久会话历史。

## 官方契约核对（2026-09-11）

### DeepSeek 对 Responses 结构化输出的公开承诺

DeepSeek 当前的 [`Responses API` 参考](https://api-docs.deepseek.com/api/create-response/) 明确写明：

- `/responses` 按 OpenAI Responses API 格式创建响应；
- `deepseek-flash` 是当前文档列出的可用模型之一；
- `text.format.type=json_schema` 表示按给定 JSON Schema 生成结构化输出；
- `schema` 是输出“必须符合”的 JSON Schema；
- 流式终态可为 `response.completed`、`response.incomplete` 或 `response.failed`。

其 [`Responses API` 兼容性说明](https://api-docs.deepseek.com/guides/responses_api/) 进一步把
`text.format` 标为 fully supported。由此，用户的基本判断是成立的：只要服务端确实识别并执行了
`json_schema`，正常完成的 `output_text` 就不应高频出现连 JSON 语法都无法解析的内容。

现场恰好与该文档承诺冲突：90 个完整响应均以 `response.completed` 结束，权威 final 与 SSE 拼接结果
完全一致，但其中 43 个无法通过 `json.loads`。在没有发现请求被代理改写或客户端提取了错误 output item
之前，应把它按“provider 结构化输出契约未稳定兑现/兼容实现缺陷”调查，而不能先归因于本地业务 Schema
校验过严。

### `strict` 是真实兼容性疑点，但不足以解释全部现象

当前代码发送：

```json
{"type":"json_schema","name":"voice_decision_v06","strict":true,"schema":{}}
```

[OpenAI Responses API 参考](https://developers.openai.com/api/reference/cli/resources/beta/subresources/responses)
将 `json_schema` 定义为 Structured Outputs；OpenAI 的该格式包含 `strict`，用于要求严格遵循 Schema。
因此这段请求对 OpenAI 原生 Responses 是合理形状。

但是 DeepSeek 自己的 `text.format` 字段表只公开了 `type`、`name`、`schema`，没有公开嵌套的
`strict`。DeepSeek 同时说明不支持的 Responses 参数会被静默忽略。因此可以提出一个有依据但尚未证实的
推断：DeepSeek 可能接收请求却忽略 `strict`，所以 HTTP 200 只能证明请求被接受，不能证明 OpenAI 的
strict 语义已生效。

这一差异仍不足以为当前结果开脱。即使 `strict` 被忽略，DeepSeek 对其已文档化的
`type=json_schema + name + schema` 仍承诺输出符合 Schema；而合法 Schema 输出至少必须是合法 JSON。
因此：

1. `strict` 是否被忽略需要单独 A/B 验证；
2. “缺少 DeepSeek 文档中的 strict 支持”不能解释或合理化 `43/90` 的 JSON 语法失败；
3. 不能因为加入了 `strict:true` 就把 DeepSeek 的兼容层等同于 OpenAI 原生 Structured Outputs。

### `response.completed` 不是已通过 Schema 校验的独立凭据

DeepSeek 的 Responses 指南只把 `response.completed` 定义为响应正常完成的流式终态，把截断和失败分别
表示为 `response.incomplete`、`response.failed`。文档没有明确规定“Schema 不符合时服务端必定改发
`response.failed`”，也没有公开一个独立的 `schema_validated=true` 结果字段。

所以日志中的 `response.completed` 能排除已公开标记的截断/失败终态，却不能被我们单独当作“服务端已对
最终正文完成 Schema 校验”的证明。真正的格式证据仍应是：权威 `output_text` 能被解析，并通过本地同版
Schema/信封校验。

### 不应混用 Chat JSON mode 与 function strict mode 的规则

DeepSeek 的 [`JSON Output` 指南](https://api-docs.deepseek.com/guides/json_mode/) 针对的是 Chat
Completions 的 `response_format={"type":"json_object"}`。它要求提示中包含 JSON、给出格式示例，并合理
设置 `max_tokens` 防止中途截断。这些建议可用于诊断提示敏感性，但不是 Responses `json_schema` 契约生效
的前置条件，也不能用来解释已收到 `response.completed` 的非空 malformed output。

同理，DeepSeek [`Tool Calls` 指南](https://api-docs.deepseek.com/guides/tool_calls/) 中的 strict mode
针对 function tool arguments，不能当成
`text.format.json_schema` 的 strict 支持证明，也不应为了本问题盲目切换到 `/beta` 工具 strict 路径。

### 当前结论

截至 2026-09-11，证据支持以下分层结论：

- **已确认：**本地确实发送了 `text.format.type=json_schema`、`name` 和生产 Schema；DeepSeek 文档宣称
  这一路径 fully supported，且输出必须符合 Schema。
- **已确认：**43 个失败是权威 final content 的 JSON 语法错误，不是本地字段规则过严，也不是 SSE
  拼接与 final 不一致。
- **已确认：**`response.completed` 本身没有文档化的“Schema 校验已通过”含义，不能替代客户端验证。
- **兼容性疑点：**请求中的 `strict:true` 来自 OpenAI 格式，但没有出现在 DeepSeek 自己的
  `text.format` 字段表；它可能被静默忽略。
- **最可能方向：**DeepSeek Responses 兼容实现对 `json_schema` 的生成约束存在缺陷或不稳定行为；次要
  候选是请求经中间层改写、DeepSeek 对复杂生产 Schema 的未公开兼容限制，或客户端选错了 output item。
- **尚不能断言：**服务端究竟完全未执行 Schema、只做生成引导而不做结果校验，还是执行中存在特定触发
  条件。需要最小复现与请求矩阵才能向 provider 给出可证伪报告。

## 官方契约后的最小验证矩阵

后续无需启动完整语音/TTS 链路，使用相同模型、相同 `/responses` endpoint 和固定小输入即可交错测试：

| 组别 | `text.format` | 目的 |
| --- | --- | --- |
| A | 当前生产 Schema + `strict:true` | 复现当前路径并建立基线 |
| B | 当前生产 Schema，不发送 `strict` | 判断未文档化字段是否触发兼容层异常 |
| C | 最小对象 Schema，不发送 `strict` | 区分 provider 基础能力与生产 Schema 复杂度 |
| D | 当前生产 Schema + 明确完整 JSON 示例 | 判断生成引导是否能缓解，但不把提示当服务端校验替代品 |

每组至少记录 HTTP 状态、Responses 终态、response id、模型名、input/output/reasoning token、权威正文长度、
JSON 结构指纹和本地 Schema 结果；不记录用户正文或模型完整输出。A/B/C/D 应交错而非顺序批量执行，避免
把时段性 provider 波动误判成配置差异。

若最小 Schema 仍返回 malformed `response.completed`，即可形成较强的 provider 最小复现；若只有生产
Schema 失败，则继续二分 Schema 关键字/嵌套结构。若仅 A 失败而 B 稳定，才有依据移除 DeepSeek 路径的
`strict`；在数据出来之前不应直接修改生产请求。

## 历史恢复缺口（采样时）

`protocol_recovery_limit=1` 以 `turn_id` 计数。额度只在 episode 启停时清空，不会在以下边界重置：

- recovery 成功；
- recovery 被后续用户讲话取消；
- 新的 `input_revision`；
- 从 speculative 输入进入 authoritative finalized 输入。

因此，早期 `USER_SEGMENT_UPDATED` 请求可消耗整个 turn 唯一的恢复额度；真正的
`USER_INPUT_FINALIZED` 请求若再次返回无效 JSON，会直接进入 `LLM_RECOVERY_EXHAUSTED`。

采样时的 400ms `AgentDecisionScheduler` 只是事件合并/限频器，不是周期轮询器。恢复耗尽后 `_run()`
回到 `await queue.get()`，不会自行唤醒。现有回归测试还明确要求耗尽后不得自重试，以防旧式恢复风暴。

当前实现已经将这一缺口收敛到同一个固定400ms调度链路：恢复耗尽不会启动独立 retry/watchdog，
但后续阶梯窗口可以在没有普通语义事件时进行一次有界 `forced_poll` 或最终
`final_silence` 评估。普通事件始终优先；TTS/语音活动不参与覆盖或重置；最终窗口在 busy 状态下
保留并只补发一次。该机制不增加 provider 并发，也不改变既有 recovery 次数上限。

## 精确上下文审计日志（2026-09-11）

旧的 `llm.context` 是面向前端的瞬时事件，普通后端日志只持久化消息数量、估算 token 和上下文摘要，无法
还原 provider 实际收到的完整请求。现在主服务默认启用独立的
`logs/voice-server-llm-context.jsonl`，为每个活动 LLM 请求追加两类 JSONL 记录：

- `request_context`：完整、无 HTTP headers 的 wire payload，包含最终 instructions、顺序一致的 input、
  reasoning effort、完整 Schema、max output tokens，以及逐消息 role/lane/字符数/content SHA-256；
- `request_outcome`：完整终态，包含模型输出正文、reasoning、明确的取消原因，并分类为
  `strict_valid`、`compat_recovered`、`invalid_envelope`、`cancelled`、provider terminal failure 或 runtime error。

两类记录共享 `conversation_id + request_id + turn_id + wire_sha256`。普通 `voice-server.log` 同时记录
`LLM_CONTEXT_CAPTURED` 和 `LLM_REQUEST_COMPLETED` 摘要，因此可以先按错误事件的 request ID 定位，再在
JSONL 中精确比较异常请求和邻近正常请求，而无需根据 ASR/TTS 日志近似重建上下文。

该文件明确包含真实用户/助手上下文，因此与普通日志隔离，不写控制台；单文件 50 MiB，保留 9 个轮转备份。
磁盘写入通过后台日志队列完成，避免在实时事件循环中执行轮转 I/O。API key、Authorization header、模型
模型输出正文和 reasoning 会写入该文件，以便对异常请求做完整复放和根因审计。需要临时禁用时可设置
`VOICE_LLM_CONTEXT_AUDIT=0`，但问题定位期间应保持默认开启。

## 待验证的问题

旧会话的 43 次异常均发生在 `json.loads(raw)` 阶段，故可确认都是 JSON 语法错误；但旧日志没有保存原文
或结构指纹，无法事后证明它们与新复放中的分布完全相同。新复放已经确认当前 provider 会同时产生完整普通
正文、缺字段 JSON 和少量歧义容器。仍待回答的是：

1. `strict:true` 是否被 DeepSeek 静默忽略，移除后是否改善或恶化；
2. 最小 Schema 是否仍会返回普通正文，从而形成 provider 侧最小复现；
3. 生产 Schema 的复杂度、动态 runtime tail、上下文长度或特定输入语义中，哪一项显著提高失约概率；
4. `LLM_OUTPUT_COMPAT_RECOVERED` 在真实长会话中的命中率，以及仍进入 recovery 的结构分布；
5. 真实长会话中，单一心跳、阶梯窗口和有限 recovery 组合是否仍能降低“恢复耗尽后无回应”，
   同时不引入新的强制请求风暴。

## 下一步诊断顺序

已完成：结构指纹、真实节奏复放、按输入阶段统计、离线兼容候选实验、有界 JSON 自修复接入、
Prompt/Schema wire contract 对齐、短 canonical 示例、分层失败诊断、SSE sequence 观测和类型化 transport error。

接下来：

1. 用无 TTS 的交错探针对比：
   - 当前生产 schema + `strict:true` 的 Responses 基线；
   - 当前生产 schema，不发送 DeepSeek 未文档化的 `strict`；
   - 最小 schema，不发送 `strict`；
   - 当前 schema 加完整合法对象示例；
   - 冻结 Chat/`json_object`，仅作为显式诊断对照，不自动回退。
2. 在真实会话中统计严格成功、兼容成功、兼容拒绝、即时 recovery 成功和最终无回应，避免只观察总请求数。
3. 对兼容拒绝样本只保存结构指纹；若确需人工审查正文，使用短时诊断开关且不得进入持久报告。
4. 持续统计 `forced_poll`、`final_silence`、普通覆盖和最终 pending 的比例；调度兜底不能掩盖
   provider/schema 的高失败率，也不能扩展为第二个 watchdog 或无限自轮询。

## 验收条件

遵循 [`docs/llm-json-schema-migration.md`](../llm-json-schema-migration.md) 的发布门槛：至少 300 次直接
Responses 决策、跨三个使用日，本地信封有效率至少 99.5%，最近 100 次不存在 schema/local
分歧，且没有 blank-only 完成响应或超过预算的恢复链。在达到门槛前，本问题保持开放。
