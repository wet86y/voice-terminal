# Voice Terminal V0.6

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器只负责控制与可视化；音频采集、VAD、ASR、Smart Turn、SenseVoice、Agent、工具、TTS、播放和延迟均由 Voice Server 负责。

> **项目状态：协议切换后的 Bug 检修期。** 当前所有测试流量已直接切到 DeepSeek Responses + strict `json_schema`；冻结的 Chat Completions + `json_object` 仅作为隐藏回退路径。此阶段优先修复协议兼容、流式终止、信封校验、动作参数投影、恢复预算和语音前缀衔接问题，不启用 `request_thinking`，也不把当前状态视为发布完成。

## 当前运行模型

系统只有一条统一运行链：`SessionRuntime → EpisodeController → ConversationScheduler + SpeechQueue`。核心关联字段为 `conversation_id`、`turn_id`、`request_id`、`speech_id` 和 `output_generation`；它们只用于追踪。普通事件不会因为文本修订或工具晚到而失效，只有停止监听、用户反打断、会话重置和播放中 `speak(text)` 才会关闭显式取消作用域。

普通 LLM 请求只由 400ms 调度 tick 放行；异常协议恢复独立调度，全程最多一条 LLM 请求在飞。Agent 每轮通过 Responses strict Schema 返回必填的 `{"speak":{"state":"speak|cancel|wait"},"actions":[]}` 信封，不传递 provider native tools。Schema 是生成约束，本地 `SpeechDecisionEnvelopeStream` 仍是最终权威。`speak.state/text/emotion` 通过前缀校验后即可进入文本语音队列；完整 JSON 与 `actions` 仍需最终校验。播放中收到新的合法非空 `speak` 前缀时，系统先将新文本与当前完整播报文本做严格前缀比对：相同前缀只追加超出的 suffix，不停止或重播当前内容；已接受文本范围内出现差异时才立即替换旧播报。替换 generation 的旧 provider 取消在后台完成；`cancel` 会立即停止输出。当前输出完成后保留 1 秒追加窗口，窗口内的 suffix 排在当前播放后；窗口外仍只合成新增文本，不重播已听前缀。讲话期间队列只保存文本，不预生成或保存 PCM，端点确认后才启动 TTS。会话上下文来自唯一的传达感知账本和 `LLMContextProjector`：最终用户输入、实际听到的语音前缀、被打断说明与不可信工具来源块。

LLM transport 由 `llm.transport` 原子选择。当前测试期唯一活动路径是 Responses + strict `json_schema`；冻结的 Chat Completions + `json_object` adapter 被隐藏为显式回退入口，正常请求不会调用它，也不做普通双路镜像。本机 `config/local.yaml` 将 thinking 保持为 disabled，因此基础 Responses 请求使用 `effort=none`。`request_thinking(effort=low)` 是后续独立、默认关闭的本地控制 action，不是 provider native tool。情绪枚举只接受 `calm`、`happy`、`sad`、`angry`、`fearful`、`disgusted`、`surprised`；兼容性别名 `friendly` 会安全映射为 `calm`。推理原文只经本地调试 Web UI 的 `agent.reasoning` 事件显示，不写服务端日志、Agent trace、DeliveryReport 或持久会话历史。上下文精确 tokenizer 在后台预热，未完成预热时使用本地 fallback 估算，不阻塞实时事件循环。迁移和回退说明见 [Schema 迁移运行手册](docs/llm-json-schema-migration.md)。

破坏性升级的规则是“删除旧接口，不删除仍需的能力”：每项被移除实现的能力必须先迁移到唯一、可达且有行为测试的新主链 owner；旧配置和协议则直接失败，不做静默映射。完整准则见[运行与质量门槛](docs/operations-and-quality.md#破坏性升级与能力保全)。

## 启动

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
npm run dev
```

在根目录的已忽略 `.env.local` 中配置 `DEEPSEEK_API_KEY`、`MINIMAX_API_KEY` 和可选的 `MINIMAX_API_HOST`。浏览器默认连接 `ws://127.0.0.1:8765/ws`，不请求麦克风权限、不上传 PCM、不自行计算延迟。

真实耳机/扬声器、自然语音统计与第三方服务稳定性仍是独立发布门槛，不能由 fake provider 或内存播放器代替。

详见：[架构与组件契约](docs/architecture.md)、[WebSocket V0.6 协议](docs/websocket-protocol.md)、[上下文与工具规范](docs/context-and-tools.md)、[V0.6 可靠性审计](docs/v06-reliability-audit.md)、[运行与质量门槛](docs/operations-and-quality.md)。
