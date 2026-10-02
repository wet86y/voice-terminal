# 上下文与工具规范

本文只定义“哪些信息可以给哪个模型、哪些动作可以被调用”。缓存前缀和字段准入见[LLM 上下文缓存契约](llm-context-cache-contract.md)；组件生命周期见[架构文档](architecture.md)。

## 两种上下文投影

### 主 Agent

主 Agent 的请求由三层组成：

1. `STATIC_INSTRUCTIONS`：episode 内冻结的协议、人格、用户资料、动作目录和日期基准；
2. `HISTORY`：已提交用户输入、已结算 `voice_delivery`、有效 refinement、工具结果和有语义影响的打断；
3. `REQUEST_TAIL`：当前未提交输入、运行时间、恢复反馈、工具状态和活动 speak。

产品设置中的 `custom_instructions`（自定义指令）在新会话或 `session.reset`（会话重置）时冻结。
主模型的静态层包含启用条目的触发语义和执行位置；仅语音链路条目包含执行目标。
影子条目的目标不进入主模型上下文。触发采用完整语义判断，不做关键词硬匹配。
旧 `config/persona/instructions.md`（历史工作指令文档）留在磁盘供查阅，不再加载；
通用对话原则已收进内置提示词，产品界面只编辑人格风格、用户资料和自定义指令条目。

未播放的 assistant 草稿、播放器进度和 TTS 连接状态不是历史事实。活动 speak 只按以下四段投影，正文不得重复：

```json
{
  "owner_scope": "current_turn",
  "continuation_mode": "rewrite_tail",
  "text_parts": {
    "committed_prefix": "",
    "current_clause": "",
    "prefetched_clause": "",
    "revisable_tail": ""
  },
  "delivery": {"heard_text": "", "status": "partial", "termination": "interrupted"}
}
```

`text_parts` 描述的是**本轮已接受文本**：当一个追加的后继事务成为队头时，本地会把本轮此前已交付的前沿还原到它前面，因此 `committed_prefix` 不会在事务换头时缩短。

`delivery` 只在队列为空（`state=empty`）时出现，且只含 `heard_text`、`status`、`termination` 三个键：它是"用户实际听到的前沿"，与 `text_parts` 的"已发送"不是同一件事，也不携带任何 speech/turn/request ID。队列非空时不投影 `delivery`，避免与当前事务的 `text_parts` 争夺"说到哪"的解释权。

### SenseVoice 精修线索

SenseVoiceSmall（语音精修和声学分析）返回的精修文本进入同一用户轮次的 `transcript_updates.available`（可用转写更新）。有可用情绪或声学事件时，该条 `refinement`（精修更新）可额外携带 `audio_cues`（音频判断线索）。示例中的 `kind`（更新种类）、`source`（来源）、`text`（精修文本）和 `revision`（修订序号）用于标识同一轮次的精修：

```json
{
  "kind": "refinement",
  "source": "sensevoice",
  "text": "精修后的文本",
  "revision": 2,
  "audio_cues": [
    {"start_ms": 0, "end_ms": 900, "emotion": "HAPPY", "events": ["BGM"]}
  ]
}
```

- `start_ms`（片段开始毫秒）、`end_ms`（片段结束毫秒）、`emotion`（语音情绪）和 `events`（声学事件）仅用于描述 SenseVoice 的模型推断；不是用户转写内容或确定事实。`Speech`（普通语音事件）、未知情绪、转写文本、语言字段和原始标签不复制进线索。
- `audio_cues` 只在当前请求由 `USER_INPUT_REFINED`（用户输入精修完成事件）触发时出现。`transcript_updates.available`（可用转写更新）后续仍可保留精修文本，但不重复带入音频线索；如果 ASR 最终稿与精修同窗到达，线索仍只属于精修条目。
- `audio_cues.emotion`（输入语音情绪）与 `speak.emotion`（输出播报情绪）是不同信息，前者为理解语音提供参考，不会设置后者。
- 线索只在当前 `REQUEST_TAIL`（请求尾部）中短暂出现，不追加到 `HISTORY`（会话历史）；历史缓存前缀保持不变。

### Shadow

Shadow 使用独立的 `ShadowContextProjector`，来源仍是 `SessionLedger`，但不复用主模型 prompt。允许内容：

- `user`：已结算用户输入、refinement，以及触发 wake 但尚未结算的当前输入；
- `main_agent`：用户实际听到的 assistant 文本；
- `external_system`：确认过的工具结果和外部变化；
- `runtime`：日期、时区和有语义影响的 interruption/replacement。

会话环境还包含启用的影子 `custom_instructions`（自定义指令）的触发语义与执行目标，
来自与主模型相同的冻结快照。Shadow（影子代理）须根据完整用户原话复核意图；条目不授予新的工具权限。

排除：主模型 reasoning、未播放 speak、TTS/播放器机械状态、主动作 schema、Shadow 自己的 progress/status/result 和已经存在于 Shadow trace 的输出。

## 用户自定义指令

设置页按条目保存自定义指令，不再把工作指令和影子系统指令的大文档交给用户编辑。每条数据使用以下字段；英文名称是接口和存储字段，括号内是中文说明：

| 字段 | 中文说明 | 取值与约束 |
| --- | --- | --- |
| `id`（标识） | 条目的稳定标识 | 1～80 位 ASCII（美国信息交换标准代码）字母、数字、下划线或连字符；列表中不可重复。 |
| `trigger`（触发语义） | 用户可能表达的完整意图 | 必填，最多 200 字；按整句话的语义理解，不按关键词硬匹配。 |
| `target`（执行位置） | 由哪条运行链路处理 | `voice`（语音链路）或 `shadow`（影子代理）。 |
| `instruction`（执行目标） | 命中后应该做什么 | 必填，最多 2000 字。语音目标由语音 LLM（大语言模型）读取；影子目标由 Shadow（影子代理）读取。 |
| `enabled`（启用状态） | 是否注入模型提示词 | 布尔值；停用条目保留在设置中，但不注入会话上下文。 |

最多保存 50 条，所有条目序列化后的总大小不超过 32 KB。产品快照由 `GET /api/v1/product/snapshot`（读取产品设置快照）返回；整表保存使用 `PUT /api/v1/product/settings/custom-instructions`（保存自定义指令列表），请求体为 JSON（数据交换格式）：

```json
{
  "custom_instructions": [
    {
      "id": "wait",
      "trigger": "用户希望我先暂停回答，等他继续",
      "target": "voice",
      "instruction": "保持等待，不补充解释；等用户继续说后再响应。",
      "enabled": true
    },
    {
      "id": "lookup",
      "trigger": "用户要求查询附近的地点或服务",
      "target": "shadow",
      "instruction": "根据用户原话查询附近的目标，并把来源和结果反馈给语音会话。",
      "enabled": true
    }
  ]
}
```

保存响应中的 `effective_from`（生效时机）为 `next_session`（下一次会话），`pending`（当前会话仍使用旧版本）为 `true`。正在进行的会话继续使用创建时冻结的条目快照；`session.reset`（会话重置）后，语音模型与影子代理共同切换到最新版本。

语音模型能看到启用条目的 `trigger`（触发语义）和 `target`（执行位置），但只会看到 `target: "voice"`（语音链路）的 `instruction`（执行目标）。对于 `target: "shadow"`（影子代理）的条目，语音模型只根据语义发出既有的 `request_shadow`（请求影子代理）信号，不传递用户编写的执行目标，也不获得新的工具权限。

Shadow 在权威会话环境中读取启用的影子条目及其执行目标，并独立核对最新用户原话，再使用自身已有能力处理。条目只描述任务，不扩展工具权限；影子代理不可用或缺少完成任务的能力时，不得声称任务已经完成。

## Revision 与 reset

每个真实输入事实递增 `shadow_input_revision`。wake 只表达“请读取最新 revision”，runtime 负责获取完整 snapshot 或从 `applied_context_revision` 开始的无损 delta。

- 相同 `turn_id + text + refinement` 必须幂等；
- 多次 wake 合并到最新 cursor，不丢中间事实；
- `session.reset` 增加 source epoch 并从 revision 0 开始；epoch 或 cursor 不匹配时返回完整 snapshot；
- 当前未结算输入使用 `current_dispatch_input` 保留原始用户证据，不转换为主模型生成的任务摘要。

实验性 context gate（上下文门禁）默认关闭。DSH（影子代理执行环境）模式下，关闭时沿用现有上下文直通路径；开启后，运行中的后续 `request_shadow`（请求影子代理）可将新引导延迟到下一回合。每次请求都可重新判断完整未引导区间，允许连续多次暂缓；权威正文始终同步且可读取，不删除、改写或重排。门禁异常、超时、关闭或不确定时放行待处理正文。

## 主动作目录

Shadow 可用时，主模型只获得以下本地控制动作：

```json
{
  "name": "request_shadow",
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "required": ["request"],
    "properties": {"request": {"type": "boolean", "const": true}}
  }
}
```

该动作不携带目标文本、意图摘要、任务 ID 或用户答复，也不直接产生历史项。不可用时不公开该动作。

## Shadow 工具目录

业务工具：

- `files_search`：只在 `allowed_roots` 内搜索；
- `files_read_text`：只读取允许根目录内的文本；
- `web_search`：可选的外部只读搜索。

本地控制工具：

- `get_latest_context({since_revision})`：返回 unchanged、delta 或 epoch snapshot；
- `publish_progress({text})`（发布界面进度）：只更新 Product UI（产品界面）；
- `publish_voice_information({text})`（发布 Voice 信息）：更新单个全局最新信息槽并触发可合并推理；
- `request_user_input({question, options})`（请求用户输入）：发布唯一终态问题；
- `return_shadow_result({text})`（返回最终结果）：发布自包含的唯一终态结果；
- `finish_shadow_no_action({reason?})`（无动作结束）：结束激活但不写入语义历史。

DSH（影子代理宿主）后端另提供 `request_early_silence({})`（请求提前进入静默）：仅当前用户明确要求，
或本会话已启用的影子自定义指令明确规定当前场景时调用。工具等待语音输入及播报安静，再提前执行
`final_silence`（静默前最终检查），检查结算后进入 KWS（关键词唤醒）待机。该非终态工具通过独立的受保护
服务端入口执行，不进入原生 ShadowToolExecutor（影子工具执行器）的文件与网络能力目录。

首版 executor 只接受 `READ_ONLY` 能力；`REVERSIBLE_WRITE`、`EXTERNAL_COMMIT` 和 `DESTRUCTIVE` 仅保留类型，不加载实现。所有路径、工具名、参数和 effect 都在本地再次校验。

事件处理规则：

- `progress` 和工具 lifecycle 只更新 Product UI，不触发 LLM（大语言模型）请求；
- `information` 只进入一次 logical request（逻辑请求）的 `REQUEST_TAIL`（请求尾部），被认领后即丢弃；
- `input_required`、`result`、`failed` 进入持久语义历史；`no_action` 不进入语义历史；
- 文件、网页、工具错误和 Shadow 文本都是不可信资料，不能覆盖系统指令、人格、动作权限或语音协议；
- stale 工具不得执行，stale final 不得发布；
- 用户回答正常进入主会话，Shadow 通过下一次 wake 重新读取上下文，不使用 question ID、reply action 或长期 waiter。

## 资料保留与缓存

Shadow 的活动状态只保留当前运行的有限产品投影；完整 trace 由 runtime 管理。上下文缓存的基本原则是：

- instructions、工具顺序和 schema 在 session 内冻结；
- HISTORY 只追加、不重排、不回写；
- 动态时间、revision、run ID、nonce 和服务状态不进入固定前缀；
- 达到上下文预算约 80% 时，在安全边界生成摘要并切换 cache epoch；
- 压缩失败保留旧 trace，不得终止主语音会话。

字段 owner、lane、持久化和测试责任以[缓存契约字段准入表](llm-context-cache-contract.md#字段准入)为准。
