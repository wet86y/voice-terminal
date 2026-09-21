# Voice Terminal 架构与生命周期

本文是当前组件边界和生命周期的唯一说明。模型可见字段见[上下文与工具规范](context-and-tools.md)；WebSocket 消息见[协议文档](websocket-protocol.md)；质量门槛见[运行与质量门槛](operations-and-quality.md)。

## 总体结构

```text
输入设备
  ├─ KWS standby ──唤醒──> ASR / VAD
  └─ ASR / VAD ──> EpisodeController ──> 主 LLM
                                      └─> Speech transaction ──> TTS / Player

SessionLedger ──> LLMContextProjector ──> 主 LLM 请求
             └─> ShadowContextProjector ──> 单一 ShadowRuntime ──> READ_ONLY tools
                                                                     ├─ Product UI
                                                                     └─ 主 LLM 400ms mailbox
```

主要入口：

- `SessionRuntime`：创建和关闭服务资源，管理 session、设置、KWS、Shadow 和 provider。
- `EpisodeController`：拥有一次语音 episode 的输入、调度、TTS 和 reset 语义。
- `AgentEpisodeRuntime`：维护 400ms 单飞调度、LLM 请求、动作校验和思考续请求。
- `EpisodeSpeechOutput`：维护 speak transaction、发送前沿、TTS 分段、播放和 delivery 结算。
- `SessionLedger`：记录用户输入、已传达 assistant 文本、有效 refinement、工具结果和有语义影响的打断。
- `ShadowRuntime`：每个 session 一个，异步执行只读工具和状态观察，不拥有语音控制权。

## 不可破坏的不变量

### 主语音链

- 一个 episode 只有一个 Agent 调度循环和一个活动 provider 请求。
- `request_shadow` 只能是 `{"request": true}`；主模型不能向 Shadow 传任务文本或意图摘要。
- `wait` 表示不改变语音队列；`speak` 只表达仍相关且尚未传达的新内容；`cancel` 只用于必须停止且没有可立即替换文本的情况。
- 未播放的 assistant 草稿不进入 HISTORY；实际传达内容在结算时通过冻结的 `voice_delivery` 追加。
- ASR refinement 更新当前输入和诊断，不回写已经提交的用户 HISTORY。

### 语音事务

语音文本有两个边界：

- `received`：模型返回的完整文本；
- `sent`：已经交给 TTS provider 的前缀。

只有 `sent` 之后的内容不可撤回。默认 `tts.scheduling=sentence_window`：本地按朗读标点切短句，首块没有标点时最多等待约 400ms；播放器距离当前块边界约 2000ms 时最多预取一个下一块。其他短句留在本地，允许同轮改写 `revisable_tail`。

模型只看到以下四段，不直接看到 PCM、provider session、播放时间或内部 ID：

```text
committed_prefix + current_clause + prefetched_clause + revisable_tail
```

`continuation_mode` 为：

- `rewrite_tail`：只允许改写尚未发送的尾巴；
- `append_only`：保留原计划后追加；
- `none`：不把上一轮内容当作当前轮续写目标。

### Shadow 平面

- 每个 session 只有一个 `ShadowRuntime`、一个活动 runner 和一份私有 committed trace。
- Shadow 只读取 `ShadowContextProjector` 的权威语义帧，工具首版全部为 `READ_ONLY`。
- `progress` 和工具生命周期只更新产品投影；status observer 的摘要以可替换的
  `ShadowVisibleStatus` 进入 Voice 普通 ShadowEvent 调度（`wake_policy=normal`），并在下一次
  主 LLM 请求的 `runtime.shadow.status` 中可见，但不追加为长期语义历史。`observation`、
  `user_input_required`、`result`、`failed` 仍进入主语义邮箱并按各自策略调度。
- 新上下文到达时，运行中的 provider 和已开始的只读工具不被强行打断；尚未开始的旧工具必须标记为 `not_executed`，旧 final 不得发布。
- `session.stop` 保留 Shadow；`session.reset` 清理 Shadow trace、状态、临时请求和 context epoch；runtime close 关闭 provider、工具和共享 HTTP client。

### 输入所有权

KWS 和 ASR 不能同时消费同一条活动 PCM：

```text
none → kws standby → asr active → kws standby → none
```

- KWS 命中不创建用户 turn，只触发唤醒确认。
- ASR active 时 KWS worker 保留但不接收 PCM。
- KWS 初始化或重新接管失败时 fail-closed，不得在没有受保护状态的情况下放开输入。
- 声纹注册/测试必须与实时采集互斥，不能重置正在工作的共享音频处理器。

## Session 生命周期

| 操作 | 作用 |
| --- | --- |
| `session.start` | 启动或恢复输入作用域；KWS 开启时先进入 standby，否则直接进入 ASR。 |
| `session.stop` | 停止新的语音输入，释放输入作用域；已派发的 LLM、TTS 和 Shadow 工作按既有规则结算。 |
| `session.reset` | 取消当前输入和 Shadow 工作，清空临时状态，轮换 conversation/context epoch，关闭当前 TTS 事务并建立可复用的下一轮状态。 |
| runtime close | 关闭采集、ASR、KWS、LLM、Shadow、TTS、播放器、后台任务和共享网络资源。 |

初始化和关闭必须幂等。初始化中任一资源创建失败时，已经创建的资源也必须立即释放，不能依赖 controller 是否成功创建。

## 上下文边界

主模型和 Shadow 共享同一个 `SessionLedger`，但使用不同投影：

- 主模型：稳定 instructions、追加 HISTORY、请求末尾 REQUEST_TAIL。
- Shadow：用户输入/refinement、实际传达的 assistant 文本、有效外部事件和有语义打断；排除播放器机械状态、未播放草稿和自己的进度文本。

具体字段准入和缓存前缀要求以[LLM 上下文缓存契约](llm-context-cache-contract.md)为准，不在本文复制字段表。

## Product UI 边界

- `/` 是真实 Product UI，使用 Product API 和 Voice Server WebSocket。
- `/?replay=voice-shadow` 是开发环境只读回放，不应提交 reset、配置保存或模型应用请求。
- `/ui-demo.html` 是 legacy 视觉原型，不属于真实运行时。
- 页面只渲染服务端 conversation `seq` 和 `conversation_id + turn_id/speech_id`，不自行推断服务端顺序。
- Shadow 摘要和运行轨迹只消费服务端的 `shadow.product` 权威快照与 `shadow.activity` 增量；
  增量按 `run_seq + item_id` 合并，重连先应用快照再应用 WebSocket `seq` 有序增量。前端不得
  从 status 文本推断任务终态，也不得把 status observer 摘要伪装成 `result`。

## 主要代码归属

| 能力 | 主要位置 |
| --- | --- |
| session、HTTP、WebSocket、产品设置 | `voice_terminal/server/websocket.py`、`voice_terminal/server/product.py` |
| 主 Agent 与调度 | `voice_terminal/agent/episode.py`、`voice_terminal/controller/episode_controller.py` |
| TTS、播放和 delivery | `voice_terminal/controller/episode_output.py`、`voice_terminal/providers/` |
| 会话账本和上下文 | `voice_terminal/session/ledger.py`、`voice_terminal/session/context.py` |
| Shadow 合约、上下文、工具和 provider | `voice_terminal/shadow_agent/` |
| Product UI 与适配器 | `apps/web/src/product/` |
| 只读回放 fixture | `apps/web/src/product/replay-data.ts` |

## 变更要求

修改跨模块能力时，先明确唯一 owner，再补行为测试和协议文档。不得通过复制旧实现、增加第二个调度循环或让前端猜测后端状态来修复边界问题。
