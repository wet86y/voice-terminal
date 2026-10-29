# LLM 上下文缓存契约

本文是所有模型可见字段的准入规范。目标是保持语音语义完整，同时让 DeepSeek 的稳定请求前缀可以持续命中缓存。上下文投影职责见[上下文与工具规范](context-and-tools.md)，不要在两份文档中重复维护同一字段规则。

## 缓存原则

- `STATIC_INSTRUCTIONS`（固定指令）在 episode（会话）内保持稳定；人格和用户资料在新会话开始时载入，保存修订不会改写当前会话；自定义指令显式保存后更新后续请求快照并重新建立前缀摘要，已派发请求保留原副本；
- `HISTORY` 只追加，不改写、不重排；
- `REQUEST_TAIL` 只放当前请求动态信息；
- `speech_controls`（当前合成能力及可选活动参数摘要）放入请求尾部；切换模型或“智能语音调节”开关不改固定前缀、历史及严格输出结构。活动参数仅在播放或有效续写窗口内非默认时简短提供，详见[语音调节](speech-controls.md)。
- 深度模式提示随本次冻结的 `reasoning.effort`（推理强度）放入 `REQUEST_TAIL`（请求尾部）；暂停后切换模式不修改 `STATIC_INSTRUCTIONS`（固定指令）或 `HISTORY`（语义历史）；
- 动态时间、可用性、revision、run ID、nonce、播放器和 provider 状态不能污染固定前缀；
- provider 未返回 usage 时显示“不可观测”，不能解释成零命中；
- 每次请求记录 `input_tokens`、`cached_tokens`、`miss_tokens` 和 `hit_ratio`，用于诊断而不是业务判断。

## 三层 ContextLane

| Lane | 可放内容 | 生命周期 | 禁止内容 |
| --- | --- | --- | --- |
| `STATIC_INSTRUCTIONS`（固定指令） | 固定协议、输出 schema（结构）说明、动作目录、人格/用户文档快照、日期基准 | episode（会话）内稳定；人格和用户资料只在新会话载入；自定义指令显式保存后可更新当前会话后续请求 | 秒级时间、播放状态、工具可用性、恢复反馈 |
| `HISTORY` | 最终用户输入、冻结 `voice_delivery`、有效 refinement、完成工具资料、有语义打断 | 只在末尾追加 | 未结算文本、运行时 ID、重排后的摘要 |
| `REQUEST_TAIL` | 当前输入、时间、日期 epoch、活动 speak、工具状态、恢复反馈 | 当前请求有效 | 固定规则和未来要复用的事实 |

内部 `context_lane`、`context_kind` 和诊断元数据不得发送给 provider。所有模型消息必须经过唯一的 `ContextFrame` 构造器。

## 传达历史 `voice_delivery`

assistant 历史只记录播放结算后的传达事实，不记录尚未发送或尚未播放的草稿：

```xml
<voice_delivery status="full">已完整播放的文本</voice_delivery>
```

部分传达：

```xml
<voice_delivery status="partial" termination="interrupted">
  <heard>确定听到</heard>
  <uncertain>边界不确定</uncertain>
  <unheard>确定未听到</unheard>
</voice_delivery>
```

`uncertain`（听到与否不确定）不能当作已听到，`unheard`（尚未听到）不自动要求下一轮补播。Bridge 2.0（第二版桥接协议）的 `information`（影子信息）保持可合并的普通推理调度，并仅作为该逻辑请求 `REQUEST_TAIL`（请求尾部）中的快照；是否说话由 Voice（语音主模型）结合最新用户可见信息和仍然相关的未传达内容决定。信息请求若选择 `wait`（静默），旧信息不会再次触发推理。

`progress`（影子进度）更新产品界面，并把实质变化登记到二等信箱；按当前有效激活及来源合并，只在本次 `REQUEST_TAIL`（请求尾部）的 `shadow.progress`（影子进度列表）提供各来源最新正文。普通调用可携带这些状态；没有普通事件时每 4 秒检查一次非空信箱，复用唯一 400 毫秒心跳及单请求执行路径。上下文构建、预算处理和审计成功后才确认所携带版本；旧版本确认不能清除构建期间到达的新版本。进度、去重状态和系统评估提示均不写 HISTORY（语义历史），不改变静态指令或历史缓存前缀。恢复与重试复用原请求仍有效的冻结进度，不重新消费共享信箱的新版本。

声学信箱以 `runtime.acoustic_events`（待消费声音观察列表）独立加入请求尾部，沿用上述四秒检查及冻结版本消费，不依赖影子激活或 ASR（语音识别）文本。活跃监听和静默窗口成立时，只有声音观察也可在有效检查点发起推理；普通请求提前消费后不重复派发。`runtime.voice_emotion`（语音情绪背景标签）同步读取已有结果，不等待推理，不进入调度事件或长期历史，不单独唤醒模型。声学入箱和情绪更新均不改变静默计时起点，详见[音频增强说明](../guides/audio-enhancements.md)。

同一 speech 只能在结算时生成一次包装；不得先写裸 assistant 文本、下一轮再补包装，也不得把 `speech_id`、request ID、时间戳、播放时长或 Shadow 状态放进包装。

## REQUEST_TAIL 信封

主 Agent 请求顺序固定为：

1. 静态 `instructions`（指令），包含唯一一份固定语音队列规则；
2. 追加 `HISTORY`（完整历史上下文）；
3. 可选 `current_user_input`（当前未提交用户输入）；
4. 独立的恢复反馈、推理模式、会话背景及调度场景消息，保留原正文和来源；
5. 恰好一个 `voice_request_context`（当前快照，第2版）；
6. `speech_queue_task`（本次决策任务），明确按快照判断并从锁定边界续写；
7. 恰好一条最终 JSON（结构化输出）约束，位于整个请求末尾。

当前输入：

```json
{"type":"current_user_input","version":1,"content":"原始完整文本"}
```

第2版将状态直接放入原生 JSON（结构化数据），不再使用 `entries.content`（条目正文字符串）嵌套另一层 JSON。`runtime`（本轮运行信息）、`speech`（当前语音事务）、`shadow`（后台状态）与 `tool_availability`（工具可用性）相互独立。恢复反馈和强制调度提示保持单独的动态系统消息，不得在最终输出约束之后追加消息。深度模式提示使用内部 `context_kind=reasoning_mode`（上下文类型为本次推理模式），与冻结的接口强度一致；快速和唤醒确认请求不携带该提示。

`runtime.speech_controls`（当前会话有效语音参数）只包含当前能力允许的非默认值：`guidance`（完整语气引导）、`speed`（语速倍率数值）及 `volume`（相对音量倍率数值）。播放和空闲状态的每次请求均可携带，全部默认或调节关闭时省略整个字段。该字段表示后续合成沿用的已受理参数，已生成音频继续保留自身快照；它不表示音频是否已经传达。参数及能力说明仅在请求尾部提供，不改写固定前缀、历史或输出结构；会话内继承和刷新边界见[语音调节说明](speech-controls.md)。

原始正文不能被摘要或重新解释。预算处理仅可截短过长的当前未提交输入；完整历史、当前锁定文本、可改尾巴和必要状态作为整体保留。固定字段说明不随可选动态字段的出现而改变，模式切换也只改变请求尾部。修改固定指令后的首批请求需建立新的缓存前缀；后续请求仍可复用稳定指令与追加历史，实际命中率以服务端用量为准。

### 当前语音事务投影

```json
{
  "type": "voice_request_context",
  "version": 2,
  "runtime": {"input_status": "finalized"},
  "speech": {
    "state": "speaking",
    "owner_scope": "current_turn",
    "continuation_mode": "rewrite_tail",
    "locked_text": "会议地点是会议室甲，",
    "editable_text": "三点开始，记得带上笔记本。"
  }
}
```

规则：

- 活动状态必须提供 `locked_text`（完整锁定文本）和 `editable_text`（完整可改文本），空段用 `""`（空字符串）；
- 锁定文本由本地 `committed_prefix`（已提交前缀）、`current_clause`（当前短句）与 `prefetched_clause`（预取短句）按原顺序拼接；可改文本来自 `revisable_tail`（未提交尾巴）。这些内部段名不再发送给模型；
- 同轮事务换头时，本地仍保留此前已交付前沿，模型投影不能丢失这段锁定内容；
- `owner_scope`（原播报轮次归属）取 `current_turn`（本轮）、`previous_turn`（前轮）或 `none`（无事务），只用于描述来源，不决定修订权限；
- `continuation_mode`（尾巴修订权限）取 `rewrite_tail`（允许尾巴修订）或 `none`（禁止尾巴修订），依据同会话当前事务的有效边界和可写状态判断；新用户轮次不撤销修订资格或续写窗口；
- `delivery.heard_text` 是独立的传达事实，不等于 TTS 已发送或播放器已播放；
- `delivery`（传达事实）只在队列为空（`state=empty`，输出空闲）时出现，只含 `heard_text`（已听文本）、`status`（传达状态）、`termination`（终止原因）；活动队列的锁定文本不能被当作确认已听；
- `sent_chars`、`played_ms`、PCM 边界、provider session、TTS 状态、speech ID 和 generation 永远留在执行层。

### 决策状态

`speak.state` 取 `speak`、`revision`、`cancel`、`wait`：

- `speak`（新播报）：`text`（播报文本）是完整文本，仅输出空闲时新建事务，并保留已交付前缀的去重保护；忙碌时拒绝，不追加、不替换、不延迟重放；
- `revision`（尾巴修订）：`speak.text`（新尾巴文本）承载完整替换尾巴，仅在 `continuation_mode=rewrite_tail`（允许尾巴修订）且有实质纠错或必要补充理由时可用；直接续接锁定边界，包括边界落在句中的情况，保留原尾巴仍有效的后续要点，不能重新作答或复述锁定内容；
- `cancel`：`text` 为空表示只停止，非空表示停止并替换；
- `wait`：`text` 必须为空。

执行层先结算正在发送的文本，并按推理准备规则决定是否补交旧事务的一块文本，再构建快照；从快照生成到结果应用完成冻结后续提交，音频接收与播放继续。应用结果前仍须重新读取当前执行状态；revision（尾巴修订）要求事务标识、输出代数、已提交字符数、原计划文本和修订版本与请求快照一致，且修订窗口仍开放。发送中、原始文本尚未全部分段入队或边界失效均拒绝；跨用户轮次本身不构成拒绝理由，不降级为追加。`sent_chars`（已提交字符数）、PCM receipt（音频回执）和确认标识留在执行层，冻结时序不改变既有缓存分区。

## Shadow trace 回放

Shadow thinking+tools 的合法 `reasoning`、`function_call`、`function_call_output` 和 `message` item 按原顺序追加到 committed trace。若 provider 返回成功但缺少可用于下一轮回放的 `reasoning_text`，只将该轮及其工具结果转换为协议无关的 system data；此前合法前缀不改写。

## 字段准入

待传达槽位及原始静默输出属于交接整理输入，不能进入主模型或 DSH（后台代理）的请求。只有已提交的 continuity document（交接文档）通过既有交接入口进入新会话。被阻止的输出不写为用户已听到的 HISTORY（历史正文）；完整输出与实际交付记录另存归档。静默文本整理复用唯一主模型循环，使用无动作的独立请求，不重放原动作。详细提交与归档规则见[跨会话交接说明](continuity-foundation.md)。

新增字段前必须填写 owner、lane、持久化策略、schema 影响和测试责任：

| 字段 | Owner | Lane | 是否持久化 | 必要测试 |
| --- | --- | --- | --- | --- |
| 固定协议、动作目录、人格文档快照 | Agent runtime | STATIC | 否 | placement/digest |
| 最终用户输入 | SessionLedger | HISTORY | 是 | ledger/replay |
| 已完成 speech 的传达事实 | Speech/ledger | HISTORY | 是 | delivery/order |
| `transcript_updates.available.audio_cues`（可用精修更新中的情绪与声学事件线索） | LLMContextProjector（LLM 上下文投影器） | 仅由 USER_INPUT_REFINED（用户输入精修完成事件）触发的 REQUEST_TAIL（请求尾部） | 否 | 字段筛选/单次发送/缓存前缀 |
| 当前未提交输入 | Context projector | REQUEST_TAIL | 否 | wrapper/order |
| `speech.locked_text/editable_text`（语音锁定文本与可改文本） | Speech runtime（语音运行时） | REQUEST_TAIL（请求尾部） | 否 | 无损投影／原子保留／创建／修订／取消替换 |
| 最近一次传达前沿 `delivery`（仅 `state=empty`） | Speech runtime | REQUEST_TAIL | 否 | delivery/order |
| revision（尾巴修订）拒绝的轻度反馈 | Agent runtime | REQUEST_TAIL | 否 | placement/final-position |
| 时间、工具可用性、恢复反馈 | Runtime | REQUEST_TAIL | 否 | placement/final-position |
| `runtime.speech_controls`（当前会话有效语音参数） | EpisodeSpeechOutput（会话语音输出管理器） | REQUEST_TAIL（请求尾部） | 否，仅当前会话内存保持 | 播放及空闲投影／默认省略／完整描述／继承与刷新／固定前缀与输出结构稳定 |
| 最终 JSON 输出约束 | Agent runtime | REQUEST_TAIL 尾部 | 否 | schema/final-position |

## 不可违反的规则

- 不得把动态状态、时间或恢复反馈追加到 instructions。
- 不得把活动 speak 或 TTS 窗口写入 HISTORY；只有结算后的 `voice_delivery` 可以进入 HISTORY。
- 不得把未听到的文本写成完整 assistant 历史。
- 不得向模型发送 request/speech/turn ID、调度 tick、性能时间或 Unix timestamp。
- 同一 session 内不得动态更换 action catalog、strict schema 或 action 可见性。
- REQUEST_TAIL 必须位于 HISTORY 之后；最终输出约束必须位于整个请求末尾且只出现一次。
- 静态 instructions、schema/action catalog 的 canonical digest 在同一 session 内不得变化；新的 HISTORY 必须以前一次 HISTORY 为前缀。

## 诊断日志字段
诊断闭环使用本地 `episode_id`（会话阶段标识）、`request_id`（模型请求标识）、`turn_id`（用户轮次标识）和 `speech_id`（播报标识）关联，不将这些标识发给模型。`agent.speak_context`（请求语音状态日志）记录锁定和可改长度；`agent.speech_revision`（修订结果日志）记录 `context_locked_chars`（请求锁定长度）、`apply_locked_chars`（应用时锁定长度）和 `new_tail_chars`（提议尾巴长度）。`episode.speech_final`（最终播报结算日志）记录 `text_chars`（全文长度）、`heard_chars`（确认已听长度）、`uncertain_chars`（不确定长度）和 `unheard_chars`（确认未听长度），同时记录传达状态、终止原因与精度。这些长度按 Unicode（统一字符编码）码点计数；不确定部分不能解释为未听，日志不包含语音正文。
