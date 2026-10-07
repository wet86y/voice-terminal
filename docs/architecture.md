# Voice Terminal 架构与生命周期

本文是当前组件边界和生命周期的唯一说明。模型可见字段见[上下文与工具规范](context-and-tools.md)；WebSocket 消息见[协议文档](websocket-protocol.md)；质量门槛见[运行与质量门槛](operations-and-quality.md)。

Tauri 2（桌面框架）拥有窗口、托盘、单实例与 Python（后端运行环境）服务管理进程。应用宿主直接管理语音识别、分析和语音服务，委托语音服务管理 DSH（代理执行宿主）与声纹工作进程；每个进程只有一个直接生命周期所有者。关闭桌面窗口隐藏到托盘，明确退出应用回收整棵受管进程树。主产品与源码界面调试只支持桌面入口，独立样例由原生宿主管理其开发服务。启动反馈和进程状态契约见[独立应用启动与系统管理](application-lifecycle.md)，便携路径与数据边界见[桌面应用说明](desktop-application.md)。

语音当前会话始终持久化到用户历史目录的 `archives/.journals/active-session.sqlite3`（当前会话数据库）；源码布局使用
`.runtime/continuity/`（历史数据目录），便携布局使用独立数据目录的 `history/`（历史目录）。启动先恢复会话身份、日期基准、
模型语义历史、已播出回复、转写精修、聊天顺序和后台上下文代次，再创建业务组件。仅保存旧身份文件不足以恢复聊天。
聊天列表继续保留最近 200 条，完整模型历史及交付归档按原顺序保存在数据库，有界读取缓存不会截断正文。
当前会话持久化不依赖 `disk_history`（其他历史落盘候选开关）。检查点与待写正文在同一数据库事务提交，损坏或缺失时报告恢复失败，
不以空历史覆盖已有记录。重启只恢复已提交材料，不重新播放语音或执行旧模型动作；手动重置和一小时共同静默交接才开始新会话。

## 总体结构

应用进程的所有权关系：

```mermaid
flowchart TD
    R[根启动入口：解析启用组合，启动后退出] --> T[Tauri 桌面：窗口、托盘、单实例]
    T --> A[Python 应用宿主：网页服务、管理接口、业务监督]
    T --> W[网页渲染进程：正式页面]
    A --> B[语音识别工作进程]
    A --> C[分析工作进程]
    A --> D[语音服务]
    D --> E[声纹工作进程]
    D --> F[DSH：代理执行宿主]
    W -.业务接口.-> A
    W -.业务接口.-> D
```

应用宿主先提供管理入口，通过私有标准输入／输出管道报告就绪，桌面确认接管后才启动业务组件；受管模式停用旧浏览器、托盘和应用单实例职责。启动过程不在宿主中导入音频或模型推理模块。网页服务与宿主共享进程，网页状态和退出入口因此不依赖语音服务就绪。桌面的 Windows Job Object（作业对象）覆盖受管进程树，组件组可独立回收；外部调试进程只标记为外部，不加入回收范围。该进程所有权层不增加语音事务或代理调度循环。

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

- `ApplicationHost`（应用宿主）：管理整套系统的启动、恢复、健康检查、指标采样和退出，保留失败时的独立诊断入口。
- `SessionRuntime`：创建和关闭服务资源，管理 session、设置、KWS、Shadow 和 provider。
- `EpisodeController`：拥有一次语音 episode 的输入、调度、TTS 和 reset 语义。
- `AgentEpisodeRuntime`：拥有唯一活动 LLM 请求和调度循环；确定性时钟、提示文本和流式语音准入由内部协作模块实现。
- `EpisodeSpeechOutput`：维护 speak transaction、发送前沿、TTS 分段、播放和 delivery 结算。
- `SessionLedger`：记录用户输入、已传达 assistant 文本、有效 refinement、工具结果和有语义影响的打断。
- `ShadowRuntime`：每个 session 一个，异步执行只读工具和状态观察，不拥有语音控制权；DSH 根代理仅有提前静默请求这一受限例外。

## 不可破坏的不变量

### 主语音链

- 一个 episode 只有一个 Agent 调度循环和一个活动 provider 请求。
- `request_shadow` 只能是 `{"request": true}`；主模型不能向 Shadow 传任务文本或意图摘要。
- 用户自定义指令以同一会话快照分别投影给主模型与 Shadow；保存后的修改在新会话或 reset 后同时生效。
- `wait` 表示不改变语音队列；`speak` 只表达仍相关且尚未传达的新内容；`revision` 只改尚未发送的尾巴且只写新尾巴；`cancel` 空文本用于必须立即停止、`cancel` 带文本用于停止并替换。
- 未播放的 assistant 草稿不进入 HISTORY；实际传达内容在结算时通过冻结的 `voice_delivery` 追加。
- ASR refinement 更新当前输入和诊断，不回写已经提交的用户 HISTORY。

#### 语音端点与分析模型

- Voice Server 的 FSMN‑VAD（语音活动检测模型）以 `vad.end_silence_ms`（VAD 结束静音时长）200ms 判断停顿。每次 `speech_ended`（语音结束事件）只启动一次 SmartTurn（语句结束判断）；`candidate_ms`（候选等待时长）和 `recheck_ms`（重试间隔）只为旧配置兼容读取，不再影响端点判断。
- VAD 在 SmartTurn 推理时持续接收音频。若检测到再次开口，控制器立即使该次判断失效，并通过分析 WebSocket 的 `cancel`（取消消息）通知工作进程；SmartTurn 每次推理使用独立的 ONNX Runtime `RunOptions`（推理选项）请求终止。分析通道只有在原生推理实际结束后才释放，因此取消不成功时不会排队重叠推理。
- SmartTurn 使用当前停顿前的最近 8 秒音频窗口；窗口不会因一次分析而从输入缓冲中删除。分析繁忙、失败、超时或未判定结束时继续收音，`hard_silence_ms`（硬静音封口时长）1400ms 执行兜底封口。
- 确认封口后，Paraformer（流式语音识别）最终稿与 SenseVoiceSmall（语音精修和声学分析）并行处理。SenseVoice 的精修稿与音频线索随 `USER_INPUT_REFINED`（用户输入精修完成事件）进入对应请求尾部；线索只用于该次精修触发的请求，不写入冻结 HISTORY，也不在后续心跳重复发送。线索是模型推断，不是转写事实。
- Paraformer（流式语音识别模型）按 `chunk_size`（流式分块尺寸）聚合音频，默认中心块为 600ms。结束消息必须让已开始推理的轮次执行 `is_final=True`（最终刷新）：即使完整分块已消费、剩余 PCM（脉冲编码音频）为空，也要刷新缓存尾字后再释放轮次资源；不重送已处理音频。纯空轮次和已清理轮次的重复结束包不启动模型推理。
- 空的最终转写以 `empty_input`（空输入状态）结算并保留诊断记录，不提交 `USER_INPUT_FINALIZED`（用户输入定稿事件）；智能体入口再次拦截空定稿和空精修。迟到精修要求该轮已有定稿输入。排查 LLM（大语言模型）调用时按事件来源关联，不能把同轮编号下的非空分句或旁路事件请求归因为空转写。

### 语音事务

语音文本有两个边界：

- `received`：模型返回的完整文本；
- `sent`：已经交给 TTS provider 的前缀。

`sent`（语音合成已接受）前缀不可撤回，其后的内容仍可修改。默认 `tts.scheduling=sentence_window`（短句窗口调度）：本地按朗读标点分段，提交前依次合并至 `tts.sentence_min_effective_chars`（锁定文本块最少有效字符，默认 10），中文、字母和数字计数，标点、空白与符号不计；生成结束时不足下限的尾块也正常提交。首段没有标点时约 400 毫秒释放分段缓冲，但提交仍遵守有效字符下限。播放器距离当前块边界约 2000 毫秒时最多预取一个下一块。LLM（大语言模型）可见的不可修改前缀以实际提交的合并块为边界，其后文本留在本地，允许同轮改写 `revisable_tail`（可改尾巴）。

模型只看到以下四段，不直接看到 PCM、provider session、播放时间或内部 ID：

```text
committed_prefix + current_clause + prefetched_clause + revisable_tail
```

`continuation_mode` 为：

- `rewrite_tail`：只允许改写尚未发送的尾巴，此时模型用 `state=revision` 只输出新尾巴，本地把锁定前缀与新尾巴拼成完整文本；
- `append_only`：保留原计划后追加；
- `none`：不把上一轮内容当作当前轮续写目标。

speak 决策状态为 `speak`、`revision`、`cancel`、`wait`：

- `speak`：`text` 是完整文本，只提交尚未交付的部分进行合成；
- `revision`：`text` 只是新的尾巴，仅在 `rewrite_tail` 可用；本地负责拼接，任何情况下都不会重播已交付内容；
- `cancel`：`text` 为空表示立即停止；`text` 非空表示停止并替换，替换文本直接播给用户；
- `wait`：不改变语音队列。

队列为空时，`runtime.speak.delivery.heard_text` 是最近一次确认听到的前沿；被打断的投递会标记为不可续写（`delivered_resumable=false`），只允许作为"已听过前缀"的重复基线。

### Shadow 平面

- 每个 session 只有一个 `ShadowRuntime`、一个活动 runner 和一份私有 committed trace。
- Shadow 读取 `ShadowContextProjector` 的权威语义帧；文件与网络工具为 `READ_ONLY`（只读），DSH 根代理另可在明确授权时请求提前静默。
- `progress`（影子进度）更新产品投影，并把实质变化按当前 `activation_id`（激活标识）和
  `source`（来源：`agent` 根代理、`observer` 状态观察器）合并进入二等信箱；各有效来源只保留最新状态。
  相同正文及仅格式空白变化不反复登记。工具生命周期仍只更新产品投影。status observer（状态观察器）与根 Agent
  共用 `latest_shadow_information`（最新影子信息）单槽，严格按到达顺序覆盖。请求冻结时将最新快照
  放入 `runtime.shadow.information`（请求尾部影子信息），然后按 `version`（本地版本）比较清除；新版本保留给下一次请求。
  `information`（影子信息）保持普通事件优先级，不登记为二等进度；同一激活中相同正文只从一个入口评估。
  `input_required`（需要用户输入）、`result`（最终结果）、`failed`（失败）进入语义历史，
  `no_action`（无需动作）只结束激活。终态和激活替换清理旧进度。
- 新上下文到达时，运行中的 provider 和已开始的只读工具不被强行打断；尚未开始的旧工具必须标记为 `not_executed`，旧 final 不得发布。
- `session.stop` 保留 Shadow；`session.reset` 清理 Shadow trace、状态、临时请求和 context epoch；runtime close 关闭 provider、工具和共享 HTTP client。

### 输入所有权

KWS 和 ASR 不能同时消费同一条活动 PCM：

```text
none → kws standby → asr active → kws standby → none
```

- KWS 命中不创建用户 turn，只触发唤醒确认。
- ASR active 时 KWS worker 保留但不接收 PCM。
- ASR→KWS 的交接只由静默窗口（`final_silence_ms`，本机产品设置为 90s）到期或受限工具提前请求后的
  `final_silence` 系统检查触发；语音结算、播放结束和 LLM 输出都不改变输入所有权。
  模型主动发声触发的 KWS→ASR 轮换同样要挂这套窗口（`AgentEpisodeRuntime.arm_silence_window`）。
- 静默窗口内，影子代理的 `progress`（进度）更新合并进入二等信箱；普通事件请求会带出其最新状态，
  否则系统从静默窗口起点每 4 秒检查一次，只有信箱非空且用户未讲话、模型空闲时才请求模型。
  检查复用唯一的 400 毫秒心跳和单请求执行路径；忙碌、讲话、重试退避时保留信箱，等下一个有效检查点。
  普通事件优先；同一检查点同时满足二等和最终静默条件时，最终检查优先。暂停后只评估当前状态，不补发历史检查。
  普通请求和二等请求均在上下文冻结及审计成功后确认对应版本；构建失败保留状态，旧版本确认不能清除新版本。
  二等评估及其恢复、重试都明确没有新用户输入，可选择 `wait`（保持等待），
  只为仍相关、尚未传达且值得现在播报的新内容选择 `speak`（发声）；用户重新讲话或影子激活失效后，旧评估未播语音失效。
  二等检查不重置静默起点、最终期限或输入所有权。最终检查结算后清理期间到达的待评估进度，并沿用原交接规则。
  TTS（语音合成）、语音活动和交付结算继续更新本地状态与会话历史，本身不触发二等请求。
- KWS 初始化或重新接管失败时 fail-closed，不得在没有受保护状态的情况下放开输入。
- 声纹注册/测试必须与实时采集互斥，不能重置正在工作的共享音频处理器。

## Session 生命周期

| 操作 | 作用 |
| --- | --- |
| `session.start` | 启动或恢复输入作用域；KWS 开启时先进入 standby，否则直接进入 ASR。 |
| `session.stop`（停止输入会话） | 停止新的语音输入，释放输入作用域并清理二等进度；停止期间进度只更新界面。已派发的 LLM（大语言模型）、TTS（语音合成）和 Shadow（影子代理）工作按既有规则结算。 |
| `session.reset` | 取消当前输入和 Shadow 工作，清空临时状态，轮换 conversation/context epoch，关闭当前 TTS 事务并建立可复用的下一轮状态。 |
| runtime close | 关闭采集、ASR、KWS、LLM、Shadow、TTS、播放器、后台任务和共享网络资源。 |

初始化和关闭必须幂等。初始化中任一资源创建失败时，已经创建的资源也必须立即释放，不能依赖 controller 是否成功创建。

## 上下文边界

主模型和 Shadow 共享同一个 `SessionLedger`，但使用不同投影：

- 主模型：稳定 instructions、追加 HISTORY、请求末尾 REQUEST_TAIL。
- Shadow：用户输入/refinement、实际传达的 assistant 文本、有效外部事件和有语义打断；排除播放器机械状态、未播放草稿和自己的进度文本。

具体字段准入和缓存前缀要求以[LLM 上下文缓存契约](llm-context-cache-contract.md)为准，不在本文复制字段表。`audio_cues`（情绪与声学事件线索）属于精修更新的临时 `REQUEST_TAIL`（请求尾部），其准入和筛选规则见[上下文与工具规范](context-and-tools.md)。

## Product UI 边界

- `/` 是真实 Product UI，使用 Product API 和 Voice Server WebSocket。
- 语音事件回放位于桌面样例工具，仅投影固定事件数据；产品入口不再用查询参数绕过宿主管理。
- `/ui-demo.html` 是 legacy 视觉原型，不属于真实运行时。
- 页面只渲染服务端 conversation `seq` 和 `conversation_id + turn_id/speech_id`，不自行推断服务端顺序。
- 产品展示层仅隐藏 `kind=user_turn`（用户消息类型）、`status=empty_input`（空输入状态）且 `text`（文本）为空或仅含空白字符的气泡。实时消息和离线快照使用相同规则；权威记录、`seq`（服务端顺序号）及诊断保持完整。非空短回答、助手消息、系统消息和其他状态记录不受此规则影响。
- Shadow 摘要和运行轨迹只消费服务端的 `shadow.product` 权威快照与 `shadow.activity` 增量；
  增量按 `run_seq + item_id` 合并，重连先应用快照再应用 WebSocket `seq` 有序增量。前端不得
  从 status 文本推断任务终态，也不得把 status observer 摘要伪装成 `result`。
- 工作台“运行轨迹”使用固定单槽，只显示当前 DSH（代理执行宿主）根代理的最新思考块或工具动作；新动作覆盖当前项，同一思考块累加增量，空闲及运行结束时清空。卡片保持既有高度、字号与颜色变量，长正文内部滚动并跟随最新增量。内部回调、迟到事件拒收和重连规则见[桥接实时活动协议](bridge-2.0-protocol.md)，构建及项目安装结果见[插件维护记录](dsh-plugin-maintenance.md#2026-10-07-实时运行轨迹协议修订与构建)。

## 主要代码归属

| 能力 | 主要位置 |
| --- | --- |
| session、HTTP、WebSocket、产品设置 | `voice_terminal/server/websocket.py`、`voice_terminal/server/http_api.py`、`voice_terminal/server/speaker_runtime.py`、`voice_terminal/server/product.py` |
| 主 Agent 与调度 | `voice_terminal/agent/episode.py`、`voice_terminal/agent/scheduler.py`、`voice_terminal/agent/context_builder.py`、`voice_terminal/agent/decision_application.py`、`voice_terminal/agent/streamed_speech.py`、`voice_terminal/controller/episode_controller.py` |
| TTS、播放和 delivery | `voice_terminal/controller/episode_output.py`、`voice_terminal/controller/speech_frontier.py`、`voice_terminal/controller/speech_pipeline.py`、`voice_terminal/controller/speech_admission.py`、`voice_terminal/providers/` |
| 会话账本和上下文 | `voice_terminal/session/ledger.py`、`voice_terminal/session/context.py` |
| Shadow 合约、上下文、工具和 provider | `voice_terminal/shadow_agent/` |
| Product UI 与适配器 | `apps/frontend/src/product/` |
| 只读回放 fixture | `apps/frontend/src/ui-demo/`（桌面样例与固定回放夹具） |

## 变更要求

修改跨模块能力时，先明确唯一 owner，再补行为测试和协议文档。不得通过复制旧实现、增加第二个调度循环或让前端猜测后端状态来修复边界问题。

文件超过约 800 行或方法超过约 150 行时，审查是否混合了可独立描述的职责；行数是审查提醒，不是强制拆分条件。迁移代码时保留既有导入入口，逐批对照事件、模型上下文和语音交付行为。

`_step`（单次决策执行方法）与 `_play_stream`（流式播放方法）仍较长，但剩余流程分别属于一次模型请求和一次语音事务，且共享严格排序的可变状态。本轮到此结束，不因行数继续拆分。只有具体改动证明职责混合、逻辑重复或测试难以隔离时，才针对该边界迁移；届时须对照事件顺序、取消结果及已发送／已听到边界。

当前职责与重复实现核对：

| 区域 | 状态归属与复用边界 |
| --- | --- |
| 主决策 | `AgentEpisodeRuntime` 仍是唯一调度和请求 owner；`scheduler.py` 保存确定性计时状态，`context_builder.py` 构造并校验模型上下文，`decision_application.py` 应用已校验的最终决策，`streamed_speech.py` 只处理一次请求内的语音准入。`prompts.py` 和 `runtime_events.py` 分别保存提示文本与时钟／恢复信号。 |
| 会话与接口 | `SessionRuntime` 负责资源生命周期；`speaker_runtime.py` 实现声纹工作进程、注册与评分操作，仍使用会话运行时的唯一状态；`http_api.py` 注册 HTTP 和 WebSocket 路由，并通过旧导入入口公开 `create_app`。 |
| 输入与语音 | `RealtimeController` 管输入设备与识别，`input_ownership.py` 实现 KWS／ASR 所有权和恢复路径；`EpisodeController` 的同名方法是特定 episode 的覆盖钩子，不按名称合并。`EpisodeSpeechOutput` 管事务，`speech_frontier.py` 投影已传达与可改写前沿，`SpeechPipeline` 管在线语音流和播放器边界；`speech_admission.py` 管尚未提交的可撤回文本，`stream_buffer.py` 管模型文本的本地刷新时限。 |
| Shadow | `ShadowRuntime` 是单一运行实例；`projection.py` 共享只读上下文与状态观察投影，`tool_loop.py` 实现工具调用、结果入账与事件发布，供原生 Shadow 和 DSH 桥接使用。 |
| 前端 | `voiceTerminalProjection.ts` 负责服务端消息解析和快照投影，`normalization.ts` 负责产品设置与模型列表归一化；两处重复的 `isRecord`（对象类型收窄函数）已统一到 `record.ts`。语音与 Shadow 的上下文投影语义不同，不能合并。 |
| DSH | `protocol.ts` 负责桥接协议校验、鉴权和响应；`index.ts` 继续负责会话绑定、观察者和工具注册。 |
| 本地 TTS | CosyVoice（本地语音合成）实现与脚本归档到 `archive/cosyvoice/`，当前产品仅实例化 MiniMax 在线客户端；`tts/router.py` 保留未来接入本地后端的事务兼容层。历史本地设置继续归一化为在线模式，旧 `tts_worker` 状态字段固定报告 `stopped`（已停止）。 |
