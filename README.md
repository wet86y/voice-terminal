# Voice Terminal V0.6

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器负责控制和可视化；音频采集、VAD、
ASR、Smart Turn、SenseVoice、主 Agent、Shadow Agent、TTS、播放和延迟均由 Voice Server 管理。

> 当前生产路径使用 DeepSeek Responses + strict `json_schema`。主语音链保持低延迟交互，
> 会话级单一 Shadow Agent 负责高思考意图理解与只读工具调用。

## 当前运行模型

```text
用户 → ASR → 主 LLM → speak → Speech transaction → TTS / Player
                    └→ request_shadow({"request": true})
                                  │
                                  ▼
                    最新权威上下文 → ShadowRuntime
                                      ├→ READ_ONLY tools
                                      └→ ShadowEvent → 400ms heartbeat → 主 LLM
```

主语音链只有一个 `AgentDecisionScheduler` 和一个 provider 请求循环。400ms mailbox、Speech
transaction、TTS generation、barge-in 与传达感知 `SessionLedger` 保持单一权威。未播放的
assistant 草稿不会成为历史；实际听到的内容、ASR refinement 和有语义影响的打断才会落账。

每个 session 只有一个长期存在的 `ShadowRuntime`，不存在 task ID、任务列表、任务队列、
modify/reply/cancel/checkpoint 或多任务调度器。主 LLM不能发送任务文本或意图摘要，只能在完整
decision 校验后异步调用固定动作 `request_shadow({"request":true})`。Shadow 从专用
`ShadowContextProjector` 读取最新完整上下文，自行识别用户当前意图。

运行中出现新用户补充、修正或撤销时，wake 只合并最新 revision，不中断正在进行的 provider
请求或已开始的只读工具。在下一个安全边界追加无损上下文 delta 和意图重判提示；尚未开始的
旧工具返回 `conversation_context_updated`，任何 stale final 都不能发布。

Shadow 固定拥有只读文件/搜索业务工具以及三个本地控制端口：主动读取最新上下文、发布
progress/observation、请求 clarification。用户答复正常进入会话后，主 LLM只再次 wake，Shadow
从用户原话继续理解，不使用 question ID 或 reply action。副作用工具 effect 仅保留契约，首版
executor 拒绝所有非 `READ_ONLY` 能力。

每 5 个已提交 Shadow model response 会并行触发无思考、无工具的状态观察器。简报通过正常
heartbeat 进入主 LLM，主模型可以自然播报或 wait。Shadow 不设 activation 总墙钟/步骤上限；
单 provider 请求仍有 timeout 和有限 transport retry。默认在 524288 token 预算的 80% 处生成
稳定摘要并切换 cache epoch，不终止长期工作。

`session.stop` 只停止语音 episode，Shadow 继续并缓存重要事件；`session.reset` 清空 Shadow
trace/status、轮换上下文 epoch 与 OpenCode session header；runtime close 关闭所有资源。

## 思考模式与输出

Web 控制台可选择快速、均衡或深度模式。快速使用 `effort=none`；均衡基础请求无思考，必要时
可输出本地 `request_thinking({"request":true})`；深度使用低思考。`request_thinking` 和
`request_shadow` 都是主 decision 内的本地控制动作，不是 provider native tools。

Agent 每轮返回必填 `{"speak":{"state":"speak|cancel|wait","emotion":"...","text":"..."},
"actions":[]}`。本地流式解析器与最终校验仍是权威。情绪只接受 `calm`、`happy`、`sad`、
`angry`、`fearful`、`disgusted`、`surprised`。

## 人格上下文

会话级人格位于 `config/persona/`：

- `personality.md`：身份与表达风格；
- `user.md`：用户资料和沟通偏好；
- `instructions.md`：等待、回应、追问、打断和语音规则。

文件在新 Agent episode 创建时冻结为静态 instructions，当前 episode 内不热更新。优先级为
“内置协议 > 指令集合 > 人格主体 > 用户资料”。Shadow 不复用语音人格 prompt；稳定用户资料
可由其专用 projector 显式提供。

## 配置

默认 Shadow 配置：

```yaml
shadow_agent:
  enabled: true
  debug_websocket_enabled: false
  max_context_tokens: 524288
  compaction_threshold_ratio: 0.80
  provider:
    timeout_ms: 15000
    transport_retry_limit: 2
  status_observer:
    enabled: true
    every_committed_steps: 5
    reasoning_effort: none
    max_output_tokens: 512
    timeout_ms: 10000
  tools:
    files_enabled: true
    allowed_roots: [docs]
```

`config/local.yaml` 用于本机非密钥覆盖；API key 放入 `.env.local` 或进程环境。

## 启动

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

`start.ps1` 会依次准备 Analysis、ASR、Web 前端和 Voice Server，并确认
`http://127.0.0.1:5173/` 可访问。仅调试后端时使用 `-NoWeb`。真实运行需配置
`DEEPSEEK_API_KEY`、`MINIMAX_API_KEY`，OpenCode Go 调试另需 `OPENCODE_GO_API_KEY`。

## AEC 与播放期插话

默认启用 AEC。助手播放期间，只有 AEC 健康、反向参考新鲜且处理后近端 VAD 明确为 true 的
连续用户语音才可中断 TTS；unknown 不回退到原始麦克风 VAD。`audio.aec_failure_policy` 支持
`pause_recover`、`stop_and_listen` 和兼容用 `continue_without_barge_in`。

## 文档与验证

- [架构与组件契约](docs/architecture.md)
- [上下文与工具规范](docs/context-and-tools.md)
- [WebSocket 协议](docs/websocket-protocol.md)
- [LLM 上下文缓存契约](docs/llm-context-cache-contract.md)
- [运行与质量门槛](docs/operations-and-quality.md)

```powershell
python -m pytest -q
cd web
npm test
npm run typecheck
```
