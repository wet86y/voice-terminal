# Voice Terminal V0.6

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器只负责控制与可视化；音频采集、VAD、ASR、Smart Turn、SenseVoice、Agent、工具、TTS、播放和延迟均由 Voice Server 负责。

> **项目状态：DeepSeek Responses 主链。** 实时会话使用 DeepSeek Responses + strict `json_schema`；冻结的 Chat Completions + `json_object` 仅作为隐藏回退路径。语音、工具、思考控制和输出前缀都经同一条 Agent 主链处理。

## 当前运行模型

系统只有一条统一运行链：`SessionRuntime → EpisodeController → ConversationScheduler + SpeechQueue`。核心关联字段为 `conversation_id`、`turn_id`、`request_id`、`speech_id` 和 `output_generation`；它们只用于追踪。普通事件不会因为文本修订或工具晚到而失效，只有停止监听、用户反打断、会话重置和播放中 `speak(text)` 才会关闭显式取消作用域。

普通 LLM 请求只由 400ms 调度 tick 放行；异常协议恢复独立调度，全程最多一条 LLM 请求在飞。Agent 每轮通过 Responses strict Schema 返回必填的 `{"speak":{"state":"speak|cancel|wait"},"actions":[]}` 信封，不传递 provider native tools。Schema 是生成约束，本地 `SpeechDecisionEnvelopeStream` 仍是最终权威。`speak.state/text/emotion` 通过前缀校验后即可进入文本语音队列；完整 JSON 与 `actions` 仍需最终校验。完整目标文本只允许在同一 `conversation_id + turn_id` 的活动 speech transaction 内续写：`runtime.speak` 通过 `owner_scope` 和 `continuation_allowed` 区分当前轮目标与上一轮可观察语音。相同的当前轮前缀只追加超出的 suffix；上一轮文本不能成为新轮 continuation baseline，模型若异常返回“已完整播放文本 + 新后缀”，本地会在 TTS 前剥离旧前缀。完全相同文本仍允许显式复述。替换 generation 的旧 provider 取消在后台完成；`cancel` 会立即停止输出。当前输出完成后保留 1 秒同轮追加窗口，讲话期间队列只保存文本，不预生成或保存 PCM，端点确认后才启动 TTS。会话上下文来自唯一的传达感知账本和 `LLMContextProjector`：最终用户输入、实际听到的语音前缀、被打断说明与不可信工具来源块；跨轮隔离不删除 assistant 历史。

LLM transport 由 `llm.transport` 原子选择；默认真实接入为官方 DeepSeek Responses + strict `json_schema`。用户可在 Web 控制台选择快速、均衡或深度模式，选择在新的 Agent episode 生效：快速始终使用 `effort=none`；均衡基础请求使用 `effort=none`，模型仅在必要时可输出本地控制 action `{"name":"request_thinking","arguments":{"request":true}}`；深度每次使用 `effort=low`。该 action 不是 provider native tool，固定由本地解释为一次 low 推理。若同轮包含业务工具，均衡模式最多优先等待 2 秒；工具完成始终回到普通 400ms 调度队列，用户开口、停止监听和重置会立即使等待失效。情绪枚举只接受 `calm`、`happy`、`sad`、`angry`、`fearful`、`disgusted`、`surprised`；兼容性别名 `friendly` 会安全映射为 `calm`。推理原文只经本地调试 Web UI 的 `agent.reasoning` 事件显示，不写服务端日志、Agent trace、DeliveryReport 或持久会话历史。上下文精确 tokenizer 在后台预热，未完成预热时使用本地 fallback 估算，不阻塞实时事件循环。

破坏性升级的规则是“删除旧接口，不删除仍需的能力”：每项被移除实现的能力必须先迁移到唯一、可达且有行为测试的新主链 owner；旧配置和协议则直接失败，不做静默映射。完整准则见[运行与质量门槛](docs/operations-and-quality.md#破坏性升级与能力保全)。

## 启动

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
npm run dev
```

在根目录的已忽略 `.env.local` 中配置 `DEEPSEEK_API_KEY`、`OPENCODE_GO_API_KEY`、`MINIMAX_API_KEY` 和可选的 `MINIMAX_API_HOST`。真实测试对话默认使用官方 DeepSeek；调试时可临时运行 `./scripts/start.ps1 -LlmProvider opencode_go` 切到 OpenCode Go，脚本结束后不会保留覆盖。浏览器默认连接 `ws://127.0.0.1:8765/ws`，不请求麦克风权限、不上传 PCM、不自行计算延迟。

真实耳机/扬声器、自然语音统计与第三方服务稳定性仍是独立发布门槛，不能由 fake provider 或内存播放器代替。

上下文字段的准入、缓存命中诊断与官方 DeepSeek 验收说明见[LLM 上下文缓存契约](docs/llm-context-cache-contract.md)；Web UI 会显示 provider 输入与缓存命中指标。

详见：[架构与组件契约](docs/architecture.md)、[WebSocket V0.6 协议](docs/websocket-protocol.md)、[上下文与工具规范](docs/context-and-tools.md)、[LLM 上下文缓存契约](docs/llm-context-cache-contract.md)、[V0.6 可靠性审计](docs/v06-reliability-audit.md)、[运行与质量门槛](docs/operations-and-quality.md)。
