# LLM 上下文缓存契约

本文是所有模型可见字段的准入规范。目标是保持语音语义完整，同时让 DeepSeek 的稳定请求前缀可以持续命中缓存。上下文投影职责见[上下文与工具规范](context-and-tools.md)，不要在两份文档中重复维护同一字段规则。

## 缓存原则

- `STATIC_INSTRUCTIONS` 在 episode 内冻结；
- `HISTORY` 只追加，不改写、不重排；
- `REQUEST_TAIL` 只放当前请求动态信息；
- 动态时间、可用性、revision、run ID、nonce、播放器和 provider 状态不能污染固定前缀；
- provider 未返回 usage 时显示“不可观测”，不能解释成零命中；
- 每次请求记录 `input_tokens`、`cached_tokens`、`miss_tokens` 和 `hit_ratio`，用于诊断而不是业务判断。

## 三层 ContextLane

| Lane | 可放内容 | 生命周期 | 禁止内容 |
| --- | --- | --- | --- |
| `STATIC_INSTRUCTIONS` | 固定协议、输出 schema 说明、动作目录、人格/用户文档快照、日期基准 | episode 内不变 | 秒级时间、播放状态、工具可用性、恢复反馈 |
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

同一 speech 只能在结算时生成一次包装；不得先写裸 assistant 文本、下一轮再补包装，也不得把 `speech_id`、request ID、时间戳、播放时长或 Shadow 状态放进包装。

## REQUEST_TAIL 信封

主 Agent 请求顺序固定为：

1. 静态 instructions；
2. 追加 HISTORY；
3. 可选 `current_user_input`；
4. 恰好一个 `voice_request_context`；
5. 恰好一条最终 JSON 输出约束，并位于整个请求末尾。

当前输入：

```json
{"type":"current_user_input","version":1,"content":"原始完整文本"}
```

运行时状态、恢复反馈、待处理工具和强制调度提示统一进入 `voice_request_context.entries`，不得在最终输出约束之后追加消息。原始 content 不能被摘要或重新解释；只有请求副本的超限处理可以截取，语义 ledger 不变。

### 活动 Speak 四段投影

```json
{
  "runtime": {
    "speak": {
      "state": "speaking",
      "owner_scope": "current_turn",
      "continuation_mode": "rewrite_tail",
      "text_parts": {
        "committed_prefix": "已经发送且离开活动窗口的前缀",
        "current_clause": "当前短句",
        "prefetched_clause": "最多一个预取短句",
        "revisable_tail": "仍在本地、尚未发送的尾巴"
      }
    }
  }
}
```

规则：

- 非 `empty` 状态必须包含四个 `text_parts` 键，空内容使用 `""`，不能使用 `null` 或随机省略；
- `committed_prefix`、`current_clause`、`prefetched_clause` 都已经锁定；只有 `revisable_tail` 可重写；
- 输出空闲后创建的同轮新播报成为队头时，`committed_prefix`（已提交前缀）必须包含本轮此前已交付的前沿，不得比上一次请求更短；
- `owner_scope` 取 `current_turn`、`previous_turn` 或 `none`，只表达所有权；
- `continuation_mode`（尾巴修订权限）取 `rewrite_tail`（允许尾巴修订）或 `none`（禁止尾巴修订），表达修改权限；
- `delivery.heard_text` 是独立的传达事实，不等于 TTS 已发送或播放器已播放；
- `delivery` 只在队列为空（`state=empty`）时出现，只含 `heard_text`、`status`、`termination`；队列非空时不出现，避免与 `text_parts` 争夺"说到哪"的解释权；
- `sent_chars`、`played_ms`、PCM 边界、provider session、TTS 状态、speech ID 和 generation 永远留在执行层。

### 决策状态

`speak.state` 取 `speak`、`revision`、`cancel`、`wait`：

- `speak`（新播报）：`text`（播报文本）是完整文本，仅输出空闲时新建事务，并保留已交付前缀的去重保护；忙碌时拒绝，不追加、不替换、不延迟重放；
- `revision`（尾巴修订）：`text`（新尾巴文本）只承载新的尾巴，仅在 `continuation_mode=rewrite_tail`（允许尾巴修订）时可用；本地把锁定前缀与新尾巴拼成完整文本，模型不再复述前缀；
- `cancel`：`text` 为空表示只停止，非空表示停止并替换；
- `wait`：`text` 必须为空。

执行层先结算正在发送的文本，并按推理准备规则决定是否补交旧事务的一块文本，再构建快照；从快照生成到结果应用完成冻结后续提交，音频接收与播放继续。应用结果前仍须重新读取当前执行状态；revision（尾巴修订）要求事务标识、输出代数、已提交字符数、原计划文本和修订版本与请求快照一致，且修订窗口仍开放。发送中、原始文本尚未全部分段入队、跨轮或边界失效均拒绝，不降级为追加。`sent_chars`（已提交字符数）、PCM receipt（音频回执）和确认标识留在执行层，冻结时序不改变既有缓存分区。

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
| 活动 speak（语音事务）四段 | Speech runtime（语音运行时） | REQUEST_TAIL（请求尾部） | 否 | 创建／修订／取消替换 |
| 最近一次传达前沿 `delivery`（仅 `state=empty`） | Speech runtime | REQUEST_TAIL | 否 | delivery/order |
| revision（尾巴修订）拒绝的轻度反馈 | Agent runtime | REQUEST_TAIL | 否 | placement/final-position |
| 时间、工具可用性、恢复反馈 | Runtime | REQUEST_TAIL | 否 | placement/final-position |
| 最终 JSON 输出约束 | Agent runtime | REQUEST_TAIL 尾部 | 否 | schema/final-position |

## 不可违反的规则

- 不得把动态状态、时间或恢复反馈追加到 instructions。
- 不得把活动 speak 或 TTS 窗口写入 HISTORY；只有结算后的 `voice_delivery` 可以进入 HISTORY。
- 不得把未听到的文本写成完整 assistant 历史。
- 不得向模型发送 request/speech/turn ID、调度 tick、性能时间或 Unix timestamp。
- 同一 session 内不得动态更换 action catalog、strict schema 或 action 可见性。
- REQUEST_TAIL 必须位于 HISTORY 之后；最终输出约束必须位于整个请求末尾且只出现一次。
- 静态 instructions、schema/action catalog 的 canonical digest 在同一 session 内不得变化；新的 HISTORY 必须以前一次 HISTORY 为前缀。

## 验证

单元测试：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py tests/test_session_ledger.py tests/test_conversation_runtime.py tests/test_v06_contracts.py -q
```

需要真实 provider 时使用无用户数据的缓存探针：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/probe_context_cache.py --model deepseek-flash
.\.local\runtimes\python\python.exe -B -X utf8 scripts/probe_context_cache.py --model deepseek-flash --dynamic-speak
```

第二、第三轮的 `cached_tokens` 应随稳定 HISTORY 增长；provider 不提供 usage 时只记录“不可观测”。不要把真实 prompt、用户内容或密钥写入探针输出。
