# Voice Terminal V0.7

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器负责控制和可视化，并在产品首页本地维护
“用户说完 → AI 开口”的感知延迟；音频采集、VAD、ASR、Smart Turn、SenseVoice、主 Agent、
Shadow Agent、TTS、播放和后端诊断指标由 Voice Server 管理。

> V0.7 当前生产路径使用 DeepSeek Responses + strict `json_schema`。主语音链保持低延迟交互，
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
普通回复在短暂 VAD 触发时仍按既有规则 hold，不取消在飞 LLM；但仅用于静默兜底的未播放
system-check 回复会在新一段用户语音开始时立即失效，不再等 ASR final 后才撤销，也不会在用户说完后补播。

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
若流在兼容的空白、BOM 或单层 JSON code fence 之后连续出现 10 个非空白字符，且正文仍未以
`{` 开始，运行时会立即关闭该 provider 流并进入既有的一次有界协议恢复；合法分片 JSON 和以
`{` 开始但最终损坏的 JSON 不会被这条纯文本早停规则截断。

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

### Web 产品页与日志回放

`/` 是当前接入 Product API 和 Voice Server 的真实产品页，展示工作台、实时会话、Shadow
摘要、延迟与设置。开发环境可以使用下面的只读日志回放入口检查长会话布局和拟真对话：

```text
http://127.0.0.1:5173/?replay=voice-shadow
```

回放 fixture 来源于本地 `logs/voice-server-llm-context.jsonl` 与 `logs/voice-server.log.1`，
只保留产品允许展示的会话投影，不包含 prompt、工具参数、原始 provider payload、API key 或
内部 trace。回放仅在 Vite development 环境启用，带有只读提示，不向后端提交操作；移除
`replay` 参数即可恢复真实运行时页面。

`/ui-demo.html` 仍是隔离的 UX Prototype P0.1，只使用固定模拟数据，不建立 WebSocket，也不
读取或写入真实设置。它用于对照旧版视觉原型，不能作为真实运行时或页面切换性能的验收入口。

### 独立验证 KWS

KWS 模型可在不启动 VAD、ASR、LLM、TTS、Web 前端或 Voice Server 的情况下单独验证。它会使用
当前配置的输入设备、16 kHz 单声道 PCM 与 `wake` 配置中的模型/关键词文件；即使正常运行配置仍将
`wake.enabled` 设为 `false`，该探针也会明确提示后继续运行，避免为了验收模型而接入完整语音链路。

```powershell
# 持续监听；说出配置中的 wake.keyword，按 Ctrl+C 结束。
python -m voice_terminal.kws.probe

# 用于检查设备、PCM 读取和模型初始化的短时冒烟测试。
python -m voice_terminal.kws.probe --duration 10
```

检测成功时会输出 `WAKE_DETECTED label=<keyword_label> ...`。探针不发送网络请求，也不创建
ASR/LLM/TTS 会话，适合在调整关键词、阈值或麦克风设备后做单独验收。

启用 KWS 即表示语音会话期间持续进入唤醒词待机：设置 `wake.enabled: true`，也可通过
`VOICE_WAKE_ENABLED=true` 覆盖。关闭 KWS 后，点击开始会直接进入 ASR。常驻模式下“开始监听”先进入 KWS 待机，唤醒后切换到 ASR；
用户最终静默后，当前回复的 TTS 播放器确认耗尽（两者取较晚时间）才启动 30 秒最终静默窗口，最终检查及其播报结算后自动回到 KWS。
没有可播放音频时以本轮事务结算作为兜底。“停止监听”仍会完整停止本次监听。

产品页中的“灵敏度”是面向用户的易唤醒程度，不是 sherpa 的原始阈值：
`sensitivity = 100 - threshold * 100`。因此默认 sherpa 阈值 `0.25` 在页面显示为 `75%`，
`98%` 对应 `0.02`，数值越高越容易唤醒；通常建议先从 `60–80%` 开始。当前中文模型使用
`phone+ppinyin` 生成词条，中文短语（例如“执事”或“嘿，小爪”）无需出现在英文词典中即可编译。

服务运行时可通过 `GET http://127.0.0.1:8765/health` 检查 KWS 链路：`wake_enabled` 表示产品开关，
`input_owner` 在待机时应为 `kws`，`kws.submitted_frames` 应持续增长；若 `dropped_frames` 增长，
说明 KWS 队列拥塞；若 `detected_count` 保持为零，则优先检查关键词、灵敏度、麦克风输入和
`kws.error`。`wake_always_on` 如果仍出现在运行时状态中只是兼容性状态字段，不是另一个设置项。

## AEC 与播放期插话

默认启用 AEC。助手播放期间，只有 AEC 健康、反向参考新鲜且处理后近端 VAD 明确为 true 的
连续用户语音才可中断 TTS；unknown 不回退到原始麦克风 VAD。`audio.aec_failure_policy` 支持
`pause_recover`、`stop_and_listen` 和兼容用 `continue_without_barge_in`。

## 文档与验证

- [架构与组件契约](docs/architecture.md)
- [上下文与工具规范](docs/context-and-tools.md)
- [WebSocket 协议](docs/websocket-protocol.md)
- [LLM 上下文缓存契约](docs/llm-context-cache-contract.md)
- [JSON Schema 迁移与回滚 Runbook](docs/llm-json-schema-migration.md)
- [运行与质量门槛](docs/operations-and-quality.md)
- [V0.7 发布审计](docs/v07-release-audit.md)
- [当前已知 LLM 可靠性问题](docs/known-issues/llm-invalid-json-responses.md)
- [KWS 常驻待机与交互隔离处置记录](docs/known-issues/kws-always-on-standby.md)

```powershell
python -m pytest -q
npm test
npm run typecheck
npm run build
```

所有命令均在仓库根目录执行；根 `package.json` 会转发到 `apps/web` workspace。运行时日志和
诊断报告固定写入根目录的 `logs/` 与 `reports/`，两者都是本地运行产物，已被 Git 忽略。
