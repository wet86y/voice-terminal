# WebSocket 协议

这是浏览器与本机 Voice Server 的公共协议。组件边界见[架构文档](architecture.md)；前端适配错误和降级规则必须保留后端结构化错误码。

## 连接和安全边界

运行快照新增 `quiet_hours`（被动静默状态），包含 `passive_quiet`（当前抑制主动语音）、`user_interaction`（用户交互窗口开启）、`within_quiet_hours`（当前处于配置时段）三个布尔值。旧客户端可以忽略。待传达槽位没有正文事件或读取命令；`session.reset`（重置会话）先完成成对归档和交接整理，失败返回 `HANDOFF_RESET_FAILED`（会话重置失败）并保留旧会话；`session.clear`（清除会话）将旧衔接材料随会话归档，失败返回 `SESSION_CLEAR_FAILED`（会话清除失败）并通过原有恢复标记继续提交。设置接口与恢复规则见[交接说明](continuity-foundation.md)。

默认连接：

```text
ws://127.0.0.1:8765/ws
```

当前协议按本机开发服务设计，默认绑定回环地址，尚未提供远程客户端认证。不要把服务直接暴露到局域网或公网；远程部署前必须先完成认证、Origin allowlist 和管理 API 防护。

## Envelope

客户端命令是 JSON 对象，至少包含非空 `request_id`、命令名和对象型 `payload`。服务端事件使用统一 envelope：

```json
{
  "seq": 12,
  "timestamp": "2026-09-20T00:00:00.000Z",
  "conversation_id": "conversation-id",
  "turn_id": "turn-id",
  "payload": {
    "type": "session.state"
  }
}
```

`seq` 只在当前 conversation 内递增；`session.reset` 后重新开始。客户端应按 `seq` 应用事件，不能根据本地到达时间重排服务端记录。

## 客户端命令

| 命令 | payload | 作用 |
| --- | --- | --- |
| `subscribe` | 对象，可为空 | 获取 runtime、conversation、Shadow 和指标快照，并开始接收事件。 |
| `session.start` | 对象，可为空 | 用户手动开始会话后直接连接 ASR（自动语音识别）；静默最终检查完成后仍按现有流程交回 KWS（关键词唤醒）待机。 |
| `session.stop` | 对象，可为空 | 停止新的麦克风输入，不自动取消已经进入 Agent/TTS 队列的输出。 |
| `session.reset` | 对象，可为空 | 清理当前会话临时状态、Shadow trace、TTS 事务和 conversation epoch。 |
| `session.reasoning_mode`（设置运行模式） | `{"mode":"fast\|deep"}`，`mode`（运行模式） | 修改后续主 Agent（主代理）请求模式：`fast`（快速，关闭思考）或 `deep`（深度，低强度思考），默认深度。用户仍可选择快速；暂停后切换并恢复无需重置会话，已经派发的请求保留原推理强度。 |

未知命令返回结构化错误，不应关闭主 session。

已归档的 `balanced`（均衡）及其他非法模式返回 `INVALID_REASONING_MODE`（运行模式无效），保持当前模式和连接。

双模式请求及尾部上下文快照契约见[上下文与工具规范](context-and-tools.md)。

## 错误格式

HTTP API 和 WebSocket 事件都应尽量保留结构化错误码：

```json
{
  "code": "SPEAKER_CAPTURE_UNAVAILABLE",
  "message": "server capture is unavailable",
  "details": {}
}
```

前端不得只使用 HTTP status 或一段通用文本判断是否降级。`code` 用于程序分支，`message` 用于展示，`details` 只包含非敏感诊断信息。

## Runtime 和会话事件

常用事件：

- `runtime.snapshot`：重连或订阅后的全量运行快照；
- `session.state`：`IDLE`、`LISTENING`、`FINALIZING`、`ERROR` 等会话状态；
- `session.operation`（会话操作完成回执）：携带 `request_id`（请求标识）、`operation`（`reset` 重置整理或 `clear` 清除归档）和 `status=completed`（操作完成）；失败通过同一请求标识的 `error`（错误事件）返回；
- `input.partial` / `input.finalized`：ASR partial 和最终用户输入；
- `metrics.snapshot`：后端诊断指标，不覆盖前端本地感知延迟计时；
- `conversation.append`：服务端会话顺序流；
- `conversation.snapshot`：当前 conversation 的权威全量列表；
- `error`：当前命令或运行链路的结构化错误。

`session.stop`（停止监听）只关闭输入作用域。已经派发的 LLM（语言模型）、TTS（语音合成）和 Shadow（后台代理）工作按既有生命周期继续结算。`session.reset`（重置会话）归档两侧记录并整理衔接文档；`session.clear`（清除会话）归档旧语音和关联后台会话，并将旧衔接文档与待传达材料归档后停止带入新会话，跳过模型整理。新的逻辑会话等待 `session.start`（开始监听）。

`runtime.snapshot`（全量运行快照）中的 `session_operation`（当前手动会话操作）为 `reset`（上下文整理）、`clear`（清除归档）或 `null`（无操作）。操作期间 `can_start=false`（禁止开始监听）。前端本地发出请求后立即锁定，等待完成或失败回执，并同步后端快照；重连依赖后端快照恢复锁定状态。

## Speech 事件

语音事务以 `speech_id` 标识，前端不得把多个事务合并为一条“当前回复”：

- `speech.queued`：事务进入队列，文本可能为空；
- `speech.delta`：追加正式输出文本；
- `speech.started`：播放器第一次实际消费该事务 PCM；
- `speech.progress`（播报进度）：缓冲、播放、替换和结算诊断；`trigger=playback_finished`（触发原因为物理播放结束）表示物理播放结束。可选 `playback_caption`（实时语音字幕片段）包含 `text`（纯语音原文）、`start`／`end`（当前片段的字符起止下标）和 `precision`（对齐精度，`word` 为词级、`sentence` 为句级）。下标按 Unicode（统一字符编码）字符计数，浏览器须转换为自身字符串下标。片段来自真实设备输出位置估算与已有音频对齐；音频尚未开始、原文不匹配或没有可信片段时为空，不按提交字符数推算。该字段不包含截图、聊天附件及工具消息；
- `speech.tail_rewritten`（尾巴修订事件）：改写本轮事务的完整未发送尾巴，全部锁定时也可接受新尾巴。`text`（本事务正文与待接播尾巴）直接替换展示，不包含前序事务文字；`text_revision`（单事务递增修订版本）及 `output_generation`（输出代数）用于拒绝过期事件。原发送管线仍在时使用同一 `speech_id`（播报标识）和播放键；发送已经关闭时由后继播放段通过自己的 `speech.queued`（语音入队）、`speech.delta`（正文增量）等事件接播，旧事务送达结算只含自身正文，前端不恢复已结束事务；
- `speech.completed`：delivery 和最终事务状态结算完成。

物理播放结束和服务端结算不是同一边界：

- `speech.started` 用于产品“用户说完 → AI 开口”计时的结束；
- `playback_finished` 用于播放面板更新；
- `speech.completed` 用于 ledger、历史和 delivery 最终结算。

设备实际播放结束后的 1 秒续写资格属于模型请求边界：`speech.state=continuation`（续写窗口）仍使用 `revision`（尾巴修订）动作，原播放段按时结算，不额外延后 `speech.completed`（语音完成事件）。窗口内已获权限的模型请求可以跨截止返回；新尾巴准备好后立即按后继段事件接播。

## Conversation 顺序

`conversation.append` 的 payload 至少包含：

```json
{
  "seq": 3,
  "kind": "user_turn|assistant_speech|system_speech",
  "conversation_id": "conversation-id",
  "turn_id": "turn-id",
  "speech_id": "speech-id",
  "text": "...",
  "status": "...",
  "delivery": "...",
  "termination": "...",
  "at": "2026-09-20T00:00:00.000Z"
}
```

规则：

- 用户轮次在输入提交/ASR final 时取得 `seq`，不能等自己的回复结算后才创建；
- assistant speech 在 `speech.completed` 时进入顺序流；
- 同一个 `turn_id` 或 `speech_id` 的后续事件只更新状态和文本，不改变 `seq`；
- `conversation.snapshot` 的 `entries` 是权威列表；刷新或重连时整体替换本地列表；
- 客户端使用 `conversation_id + turn_id/speech_id` 去重。
- `kind=user_turn`（用户消息类型）、`status=empty_input`（空输入状态）且 `text`（文本）为空或仅含空白字符的记录仍可存在于权威列表；产品对话区在展示层隐藏它，客户端存储保留记录与 `seq`（服务端顺序号）。增量消息与快照应用相同展示规则，非空短回答及其他消息类型不受影响。占位文案只是界面回退，空输入记录本身不触发 LLM（大语言模型）请求。

这样可以保证用户问题排在其回复之前，也能让 KWS 唤醒确认和最终静默检查出现在真实发生的位置。

## 感知延迟

Product UI 的“用户说完 → AI 开口”由浏览器单调时钟测量：

1. 收到本轮 `session.state.state=FINALIZING` 时开始；没有该状态时兼容使用 `input.finalized`；
2. 收到同一轮的 `speech.started` 时停止；
3. 取消、替换或播放失败时丢弃未完成计时；
4. `metrics.snapshot` 只用于诊断、历史和回放，不覆盖实时值。

小于 `1000 ms` 显示整数毫秒；大于等于 `1000 ms` 显示最多两位小数的秒。

## Shadow 产品事件

Shadow UI 使用：

- `shadow.product`（后台产品快照）：当前可用性、运行状态、最近活动和资源指标。`latest_event`（最新进展）保留向语音模型投递的完整事件正文，最多 8000 字符；`continuous_approval`（连续授权状态）表示本录音窗口是否已放行后续审批。兼容字段 `summary`（旧摘要字段）不再从运行时 `status`（状态观察器摘要）映射，工作台不显示摘要卡。
- `shadow.activity`（后台活动增量）：当前运行中的生命周期、思考、工具、输出和公开事件增量。DSH（代理执行宿主）观察器摘要仅进入二等信箱，排除前台活动和最新进展映射。

活动项使用 `run_seq + item_id`（运行序号与条目标识）标识，状态为 `started`、`delta`、`completed` 或 `failed`（开始、增量、完成或失败），并带有服务端生成的 `timestamp`（时间戳）。绑定 DSH（代理执行宿主）根 Agent（根代理）的插件将根代理思考和工具事件，以及同一运行所属子代理的思考、回复与工具事件，投影到 `shadow.activity`（运行轨迹增量）。终端工具与后台作业显示参数、结果、进度和标准输出／错误输出；待决权限请求显示请求内容与专用 `approval_id`（权限请求标识），用户决定通过受保护的产品接口转回原始请求。思考、输出与同一工具的增量按条目标识合并，完成项保留供展开查看。

活动流类型为 `lifecycle`（生命周期）、`thinking`（思考）、`tool`（工具）、`output`（执行输出）、`event`（结构化结果）和 `approval`（权限请求）；公开正文限于 `text`（展开正文）、`delta`（增量）、`tool_name`（工具或作业名称）、`tool_status`（执行状态）、`approval_id`（权限请求标识）及 `event_kind`（事件类别）。每个快照最多保留 96 项，单项正文最多 12,000 字符；增量最多 2,000 字符，裁剪时保留未决定的权限请求。服务端只接收受支持字段并限制长度，插件在发送前净化可识别凭据。

这些界面活动只进入有界运行时活动快照，不追加 Voice 对话或产品长期活动历史，也不改变 DSH（代理运行时）本来的会话存档。`action_seq`（动作序号）在每次激活内递增；Voice 侧只接受当前会话、代次、激活及序号有效的动作。运行结束或切换后不再接收旧活动，重连先应用 `shadow.product`（产品快照）恢复最近活动，再按 WebSocket `seq`（消息序号）应用增量。provider request（供应商请求）、trace ID（追踪标识）、模型内部推理、凭据及未净化的工具参数不得进入公共协议。浏览器在应用快照前冲刷同一帧内的增量，避免旧活动覆盖新摘要。

插件内部回调的 `action_seq`（动作序号）同时约束思考、工具和空闲状态，统一串行发送；乱序空闲状态不能清空新动作。该序号在桥接接收端校验，前端继续使用 WebSocket `seq`（消息序号）及 `run_seq + item_id`（运行序号与活动标识）应用活动。完整输入字段、错误回执及取消规则见 [Bridge 2.0（桥接协议）](bridge-2.0-protocol.md)。

调试协议只有在 `shadow_agent.debug_websocket_enabled=true` 时可用：

```json
{
  "command": "shadow.debug.wake",
  "request_id": "debug-1",
  "payload": {}
}
```

调试唤醒仍使用 session 唯一 ShadowRuntime，不创建连接级 task manager，不接受任务文本、task ID、modify/reply/cancel，也不绕过权威上下文。

## 兼容约束

- `wake_always_on`、旧的单个 `wake_keyword` 等字段仅为兼容读取或展示，不应被当作新的控制入口。
- 未知命令、协议错误和 Shadow 调试关闭分别返回结构化错误，不应让主会话退出。

事件顺序、字段、错误码或 session（会话）生命周期变更所需的跨端回归见[测试、启动与现场验收入口](../testing/operations-and-quality.md#bridge-20-协议回归)。
