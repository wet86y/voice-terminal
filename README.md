# Voice Terminal V0.6

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器只负责控制与可视化；音频采集、VAD、ASR、Smart Turn、SenseVoice、Agent、工具、TTS、播放和延迟均由 Voice Server 负责。

> **项目状态：生产运行。** 实时会话使用 DeepSeek Responses + strict `json_schema`；冻结的 Chat Completions + `json_object` 仅作为显式回退路径。语音、工具、思考控制和输出前缀都经同一条 Agent 主链处理。

## 当前运行模型

系统只有一条统一运行链：`SessionRuntime → EpisodeController → AgentDecisionScheduler + SpeechQueue`。核心关联字段为 `conversation_id`、`turn_id`、`request_id`、`speech_id` 和 `output_generation`；它们只用于追踪。普通事件不会因为文本修订或工具晚到而失效，只有停止监听、用户反打断、会话重置和播放中 `speak(text)` 才会关闭显式取消作用域。

所有 Agent 请求都由同一个 `AgentDecisionScheduler` 和同一个 `_run()` 循环管理。调度器以固定 deadline 持续推进本地 400ms 心跳：下一次 deadline 从上一次 deadline 加 400ms 计算，不等待在飞 LLM、TTS 或 provider 返回。普通事件在心跳边界合并并优先调度；没有普通语义事件时，才在第 3、6、9、16、22 个窗口进行 `forced_poll`，第 25 个窗口进行一次独立的 `final_silence` 检查。普通事件和强制窗口同一边界出现时，普通请求覆盖强制窗口，不创建 system-check。TTS 和语音活动事件不构成普通语义事件，也不重置用户静默锚点；TTS 状态只作为实际发起 LLM 请求的 busy gate。异常协议恢复仍沿用同一循环和既有有限预算，不形成第二个 watchdog 或无限自唤醒。Agent 每轮通过 Responses strict Schema 返回必填的 `{"speak":{"state":"speak|cancel|wait"},"actions":[]}` 信封，不传递 provider native tools。Schema 是生成约束，本地 `SpeechDecisionEnvelopeStream` 仍是最终权威。`speak.state/text/emotion` 通过前缀校验后即可进入文本语音队列；完整 JSON 与 `actions` 仍需最终校验。完整目标文本只允许在同一 `conversation_id + turn_id` 的活动 speech transaction 内续写：`runtime.speak` 通过 `owner_scope` 和 `continuation_allowed` 区分当前轮目标与上一轮可观察语音。相同的当前轮前缀只追加超出的 suffix；上一轮文本不能成为新轮 continuation baseline，模型若异常返回“已完整播放文本 + 新后缀”，本地会在 TTS 前剥离旧前缀。完全相同文本仍允许显式复述。替换 generation 的旧 provider 取消在后台完成；`cancel` 会立即停止输出。当前输出完成后保留 1 秒同轮追加窗口，讲话期间队列只保存文本，不预生成或保存 PCM，端点确认后才启动 TTS。会话上下文来自唯一的传达感知账本和 `LLMContextProjector`：最终用户输入、实际听到的语音前缀、被打断说明与不可信工具来源块；跨轮隔离不删除 assistant 历史。

没有普通事件时的强制请求仍使用同一个 provider stream 入口，但会创建独立 `system_check_id`，不调用 `ConversationRuntime.begin_turn()`，不创建用户 `InputRecord`，也不产生用户轮次的 `turn.completed`。已被用户听到的 system-check speech 通过现有 assistant delivery 机制进入账本；新用户输入会使尚未播放的 system-check 结果失效。

LLM transport 由 `llm.transport` 原子选择；默认生产接入为官方 DeepSeek Responses + strict `json_schema`。用户可在 Web 控制台选择快速、均衡或深度模式，选择在新的 Agent episode 生效：快速始终使用 `effort=none`；均衡基础请求使用 `effort=none`，模型仅在必要时可输出本地控制 action `{"name":"request_thinking","arguments":{"request":true}}`；深度每次使用 `effort=low`。该 action 不是 provider native tool，固定由本地解释为一次 low 推理。若同轮包含业务工具，均衡模式最多优先等待 2 秒；工具完成始终回到普通 400ms 调度队列，用户开口、停止监听和重置会立即使等待失效。`config/local.yaml` 是本机调试覆盖；正常请求的 Schema 可见性和实际 effort 以启动时冻结的 episode `reasoning_mode` 为准。情绪枚举只接受 `calm`、`happy`、`sad`、`angry`、`fearful`、`disgusted`、`surprised`；兼容性别名 `friendly` 会安全映射为 `calm`。推理原文只经本地调试 Web UI 的 `agent.reasoning` 事件显示，不写服务端日志、Agent trace、DeliveryReport 或持久会话历史。上下文精确 tokenizer 在后台预热，未完成预热时使用本地 fallback 估算，不阻塞实时事件循环。

破坏性升级的规则是“删除旧接口，不删除仍需的能力”：每项被移除实现的能力必须先迁移到唯一、可达且有行为测试的新主链 owner；旧配置和协议则直接失败，不做静默映射。完整准则见[运行与质量门槛](docs/operations-and-quality.md#破坏性升级与能力保全)。

## 人格上下文文件

模型的会话级人格配置位于 `config/persona/`，由三份 Markdown 文件组成：

- `personality.md`：语音模型的身份、人格特质和表达风格；
- `user.md`：用户资料、称呼、语言和沟通偏好；
- `instructions.md`：等待、回应、追问、打断、工具使用和语音表达规则。

三份文件在新的 Agent episode 首次创建时读取一次，并作为稳定的
`STATIC_INSTRUCTIONS` 注入模型。当前 episode 内修改文件不会热更新；停止后
重新启动或执行会话重置后才会重新加载。缺失、空文件或无法读取的文档会被跳过并
记录 warning，内置 Agent 协议和本地 JSON/语音校验仍然保持有效。文档的优先级为
“内置协议 > 指令集合 > 人格主体 > 用户资料”。

当前初始化内容为：模型名称暂不设定，身份是语音助理，表达风格稳重、中性，遵循
“理性优先、证据优先”；用户称呼为“老大”，背景是当前系统调试人员，并允许在信息
不足或需要继续讨论时主动追问。说话内容需要修改、补充或纠正时，优先追加；只有
原内容明显错误、会误导、与新信息冲突或必须撤回时才打断，并说明打断原因。

## 启动

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

`start.ps1` 默认会依次准备分析、ASR、Web 前端和 Voice Server；在继续启动前会确认 Web 前端实际可通过 `http://127.0.0.1:5173/` 访问。若 5173 已被其他进程监听、Vite 提前退出或页面在 30 秒内不可访问，脚本会失败并输出前端日志，而不会把仅后端健康误报为整套服务可用。只有需要独立调试后端时才使用 `-NoWeb`；前端单独开发仍可运行 `npm run dev`。

在根目录的已忽略 `.env.local` 中配置 `DEEPSEEK_API_KEY`、`OPENCODE_GO_API_KEY`、`MINIMAX_API_KEY` 和可选的 `MINIMAX_API_HOST`。真实测试对话默认使用官方 DeepSeek；调试时可临时运行 `./scripts/start.ps1 -LlmProvider opencode_go` 切到 OpenCode Go，脚本结束后不会保留覆盖。浏览器默认连接 `ws://127.0.0.1:8765/ws`，不请求麦克风权限、不上传 PCM、不自行计算延迟。

真实耳机/扬声器、自然语音统计与第三方服务稳定性仍是独立发布门槛，不能由 fake provider 或内存播放器代替。

上下文字段的准入、缓存命中诊断与官方 DeepSeek 验收说明见[LLM 上下文缓存契约](docs/llm-context-cache-contract.md)；Web UI 会显示 provider 输入与缓存命中指标。

详见：[架构与组件契约](docs/architecture.md)、[WebSocket V0.6 协议](docs/websocket-protocol.md)、[上下文与工具规范](docs/context-and-tools.md)、[LLM 上下文缓存契约](docs/llm-context-cache-contract.md)、[V0.6 可靠性审计](docs/v06-reliability-audit.md)、[运行与质量门槛](docs/operations-and-quality.md)。
