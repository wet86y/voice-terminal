# Voice Terminal 项目审计报告

审计日期：2026-09-05
审计分支：`main`
审计起点：`d599325`
范围：配置与启动、v0.1 状态机、v0.2 轮次/代次/修订号、ASR/Analysis、音频采集与播放、LLM/工具/TTS、EventBus/WebSocket、Vue 控制台、协议文档和本机回放。

本报告记录已经由代码、确定性测试或本机服务证实的问题。没有因为命名不统一、理论上的性能可能性或缺少抽象而改写架构。报告不包含 API Key、原始 PCM、完整远程响应或完整 LLM 上下文。

## 基线和边界

审计开始时 Git 工作区干净，当前分支为 `main`。本轮保留用户已有的进程，没有停止或接管它们：Voice Server `8765`、ASR Worker `8766`、Analysis Worker `8767` 和 Vite `5173`。实时回放使用内存播放器，不能替代真实麦克风、耳机或扬声器验收。

审计完成后的自动化基线为：

| 检查 | 结果 |
| --- | --- |
| Python | `76 passed` |
| Vue/Vitest | `9 passed` |
| `npm run typecheck` | 通过 |
| `npm run build` | 通过 |
| `python -m compileall -q voice_terminal apps tests` | 通过 |
| `git diff --check` | 通过 |
| `scripts/start.ps1`、`scripts/replay-v02.ps1` PowerShell 解析 | 通过 |

审计前验证记录没有包含本轮新增的恢复、协议、并发和控制台回归；当前自动化基线以本报告列出的结果为准。

## 架构与契约矩阵

| 链路 | 真实实现 | 身份/状态约束 | 证据 |
| --- | --- | --- | --- |
| 配置/启动 | `voice_terminal/config.py`、`scripts/start.ps1`、`scripts/replay-v02.ps1` | `default.yaml` 后合并可选 local YAML；进程环境变量优先于 `.env.local`；ASR URL 来自 `asr.worker_url`/环境覆盖；v0.2 是当前主路径，默认配置暂作为回滚闸门 | 配置代码检查、脚本解析、ASR 探针 |
| VoiceRuntime | `voice_terminal/server/websocket.py` | 初始化后应用层 health 才将 ASR 置为 READY；VAD 失败以 `vad:` 记录，公开 ASR 链状态为 ERROR；start/stop 由会话锁串行化 | `tests/test_server.py` |
| v0.1 控制器 | `voice_terminal/controller/turn_controller.py` | 轮次由 `turn_id` 隔离；final 超时、音频异常均取消当前轮并回到 IDLE；停止会取消 completion/TTS/播放器并关闭 ASR | `tests/test_turn_controller_recovery.py`、全量测试 |
| v0.2 控制器 | `voice_terminal/controller/realtime_controller.py`、`TurnWorkspace` | 结果使用 `(turn_id, generation_id, input_revision)` 校验；续说会使旧 generation 失效并清理工具状态/缓存；60 秒封口后新轮使用新 turn | `tests/test_v02.py`、真实回放 |
| ASR/Analysis | `voice_terminal/asr/*`、`voice_terminal/analysis/*` | ASR health 回显 request ID；Analysis 每 lane 单槽，取消不提前释放原生推理槽；请求回显身份 | 协议测试、`8766/8767` health |
| TTS/播放器 | `minimax_tts.py`、`audio/player.py`、v0.2 `_answer` | TTS 失败可降级并提交完整文本；播放键隔离旧 PCM；pacing 使用整轮 returned/played/remaining PCM | TTS/播放器/v0.2 测试、回放指标 |
| EventBus/WebSocket | `event_bus.py`、`server/websocket.py` | 每连接独立 seq；订阅队列 256 事件，慢客户端断开，不阻塞音频主链；命令必须含非空 `request_id` | EventBus 背压测试、协议测试 |
| Vue 控制台 | `useVoiceTerminal.ts`、`types.ts` | 先按 seq，再按 turn/generation/revision；重连、空当前轮快照清除旧文本/context/指标；延迟只读后端值 | 9 项 Vitest、typecheck/build |

## 配置、协议和文档核对

- `load_config()` 以 `default.yaml` 为基础，按顺序深度合并传入 local YAML；当前仓库没有 `config/local.yaml`，因此运行时使用默认配置或启动参数指定的配置文件。
- `.env.local` 只补充未存在的进程变量；已有进程环境变量优先。`VOICE_SERVER_HOST/PORT`、`ASR_WORKER_HOST/PORT/URL`、`VOICE_MODEL_DIR` 和 `MINIMAX_API_HOST` 的覆盖路径与代码一致。
- Voice Server、ASR Worker、回放脚本和 readiness probe 使用同一份 `asr.worker_url`。启动脚本不会再固定探测 `127.0.0.1:8766`。
- `v02.enabled`、`smart_turn`、`sensevoice`、`predictions`、`tools` 仍是独立开关。启动/回放 readiness 现在只要求已经启用的 Analysis lane 为 READY；禁用的 lane 不会阻塞启动。`config/default.yaml` 中 v0.2 仍关闭，`config/v0.2.yaml` 才显式开启。
- WebSocket Envelope、`request_id`、整数 seq、对象 payload、服务端延迟和 `turn.completed` 唯一历史入口与 `docs/websocket-protocol.md`、`apps/web/src/types.ts` 对齐。本轮没有新增 Envelope 字段，也没有改变旧客户端可忽略的 v0.2 扩展。

## 问题分级和修复状态

### P0

没有发现或复现 P0 问题。

### P1

| 问题 | 证据和影响 | 最小修复 | 回归证据 |
| --- | --- | --- | --- |
| v0.1 ASR final 超时会遗留 `FINALIZING` 轮 | `_complete_turn()` 超时只发错误，旧轮继续占用 controller，下一轮可能无法正常开始 | 超时后调用 `_cancel_turn()`，发布 `IDLE` | `test_asr_final_timeout_releases_turn_and_returns_to_idle` |
| v0.1 音频/ASR/VAD 异常会继续使用坏依赖 | `_listen_loop()` 原先只记录错误；采集循环可能持续运行，旧 turn 和连接仍在 | 将 `read()` 也纳入异常边界；取消 turn、停止采集、关闭 ASR、恢复服务状态并退出循环 | `test_audio_pipeline_failure_stops_broken_session` |
| v0.2 采集读取或 ASR 写入失败会保留监听状态 | v0.2 覆盖了基类 `_listen_loop()`，原异常路径没有停采集；ASR sender 的非 `ConnectionClosed` 写入失败也会继续取队列 | 失败统一取消 listen task/sender、停止采集、清空 ASR 队列、关闭连接并回到 `IDLE`；所有发送失败使用同一恢复路径 | `test_asr_send_failure_stops_capture_and_sender`、`test_audio_read_failure_returns_v02_controller_to_idle`、已有 ASR 断连测试 |
| 多个 WebSocket 客户端的 start/stop 存在交错窗口 | `start_listening()` 在 health/初始化 await 期间，另一个客户端的 stop 可能先返回，随后 start 又把监听拉起 | `VoiceRuntime` 用一个会话锁串行化 start/stop | `RuntimeConcurrencyTests.test_start_and_stop_commands_are_serialized` |
| 续说会保留旧 generation 的工具缓存和可见状态 | 同一 `turn_id` 的工具缓存原来可以跨 generation 复用，旧工具状态也会进入 `turn.resumed` 快照 | 续说时取消并重建该 turn 的工具缓存，清空工具状态、旧 Paraformer 输出和过期分析结果 | `test_resume_drops_old_generation_tool_state_and_cache`、多次续说测试 |
| 采集停止时残留回调帧会进入下一 session | PortAudio callback 可能在 stop/close 期间向队列写入最后几帧，下一轮会先消费旧 PCM | start 前及 stop 后排空待处理帧 | `AudioCaptureQueueTests.test_stop_drops_frames_queued_by_previous_session` |

### P2

| 问题 | 证据和影响 | 最小修复 | 回归证据 |
| --- | --- | --- | --- |
| 前端断线/重连后保留上一轮文本、context、指标 | `onclose` 原先只改连接/服务状态；空当前轮快照也不会清除旧 turn | 增加统一连接数据清理；重连前、断开时和显式 `current.turn_id=null` 快照均清理 | `重连时清理旧轮状态，并按空当前轮快照恢复` |
| 前端接受 fractional seq 或非对象 payload | 解析器只检查有限 number，协议要求整数 seq 和 object payload | 严格校验整数 seq、对象 payload | `拒绝非整数序号和非对象 payload 的协议消息` |
| WebSocket 命令缺少 `request_id` 仍会执行 | `_valid_command()` 只校验 type/command/payload，实际协议要求非空 request ID | 缺少或空白 request ID 直接返回 `INVALID_COMMAND` | `test_websocket_rejects_command_without_request_id` |
| VAD 初始化错误显示为 ASR 错误 | 公共 service map 没有 VAD 槽位，原日志也写成 `asr:`，诊断会误导排查 | 公共 ASR 链仍标记 ERROR，但 startup diagnostic 保留真实 `vad:` 前缀 | `test_vad_startup_failure_keeps_public_asr_status_but_preserves_vad_diagnostic` |
| 启动/回放脚本强制要求未启用 Analysis lane READY | 只开 Smart Turn 或只开 SenseVoice 时，另一 lane 的 DEGRADED 会阻塞启动，与独立开关设计冲突 | readiness 按启用 lane 判定 | 两个脚本 PowerShell 解析通过；配置路径代码核对 |
| 审计前验证文档的阶段化表述已过时 | 旧文档把已经完成的执行过程当作当前计划 | 重写为当前有效结果、证据边界和后续目标 | `docs/v0.2-validation.md`、`docs/v0.2-next-steps.md` |

### P3

没有进行与运行正确性无关的结构重写、命名统一或 UI 美化。

## 已验证的关键不变量

- 不同 `turn_id` 的 ASR、Analysis、预测、工具、LLM 和 TTS 结果通过 workspace/token 检查隔离；旧 generation 或旧 revision 的预测不会创建新工具任务。
- 预测未完成时，封口路径直接等待 ASR/精修并发起最终 LLM，不等待旧预测；预测取消和过期属于正常控制分支。
- 续说会提升 generation，旧回答任务和播放器 key 失效；现在工具缓存也按 generation 边界清理。
- v0.2 60 秒上限通过现有确定性测试；后续讲话进入新 turn，旧 Workspace/PCM 不进入新 turn。
- TTS pacing 的窗口读取当前轮全部已返回 PCM 与播放器已消费 PCM。真实回放最终记录 `audio_returned_ms=62531`、`audio_played_ms=62531`、`audio_remaining_ms=0`，没有尾部积压。
- `turn.completed` 是历史提交入口；`InMemoryHistory.append_exchange()` 与 `append_record()` 按完整交换提交并按 turn ID 去重。失败/取消路径不会写半条历史。
- EventBus 队列达到 256 事件后断开慢订阅者；既有背压测试证明发布不会等待慢客户端。
- ASR Worker 只有在模型加载并通过应用层 health 后报告 READY；当前本机探针返回 READY、`model_loaded=true`，耗时约 80 ms。Analysis 两路当前均返回 READY，模型分别为 Smart Turn 和 SenseVoiceSmall。

## 本机回放矩阵

| 场景 | 结果 | 依据 |
| --- | --- | --- |
| v0.1 final 超时、音频异常、旧采集帧 | 通过 | 本轮 3 个确定性回归 |
| v0.2 ASR 断连、写入失败、读取失败 | 通过 | 本轮 v0.2 回归和已有断连测试 |
| Worker health、partial/final | 通过 | `asr.probe`：READY；本轮真实 Worker 回放 21 partial/1 final/0 disconnect |
| Analysis endpoint/refine READY | 通过 | `8767/health` 两路 READY、模型已加载 |
| Analysis 不可用降级、Paraformer fallback | 通过 | 已有 Analysis 降级回放证据和确定性测试；本轮未修改该路径 |
| 60 秒封口、后续新轮 | 通过 | 现有 v0.2 长轮测试 |
| 播放前/播放中续说、旧回答清理 | 通过 | 现有多次续说/播放边界测试；本轮补工具缓存清理测试 |
| 预测过期、工具预取、最终复用 | 通过 | 现有 v0.2 竞争窗口测试；本轮真实回放请求 5 次、接受 5 次、丢弃 0 次 |
| TTS 部分音频、失败降级、pacing | 通过 | 现有 TTS 失败/pacing 测试；真实回放完整排空 |
| WebSocket snapshot、重连、慢订阅者 | 通过 | 前端 9 项测试、后端 snapshot/背压测试 |
| 多轮历史和 LLM context | 通过 | v0.2 历史/context 测试；真实回放提交 1 条完整记录 |

本轮真实回放使用 `config/v0.2.yaml`、本机 ASR/Analysis Worker、真实 DeepSeek/MiniMax provider 和内存播放器，输入为已存在的 12 秒中文夹具。回放报告只取健康状态、事件计数和音频进度等摘要；没有把远程回答写入本报告。真实服务结果不能替代自然语音 CER、物理设备声学效果或稳定限流窗口下的长期测试。

## 修改文件

- `voice_terminal/controller/turn_controller.py`：v0.1 final timeout 和音频循环恢复。
- `voice_terminal/controller/realtime_controller.py`：v0.2 音频/ASR 失败恢复、会话状态、续说 generation 工具隔离。
- `voice_terminal/audio/capture.py`：跨 session 队列清理。
- `voice_terminal/server/websocket.py`：命令 request ID、VAD 诊断标签、start/stop 会话锁。
- `apps/web/src/composables/useVoiceTerminal.ts`：严格 Envelope 校验、重连/空快照清理。
- `scripts/start.ps1`、`scripts/replay-v02.ps1`：按启用 lane 判定 Analysis readiness。
- 回归测试：`tests/test_turn_controller_recovery.py`、`tests/test_server.py`、`tests/test_v02.py`、`apps/web/src/composables/useVoiceTerminal.test.ts`。
- 本报告：`docs/project-audit.md`。

## 尚未完成的验收

- 真人麦克风、耳机、扬声器、播放中断的正式设备验收未完成。
- 自然语音 CER、误结束率、p50/p95 和资源峰值样本未形成统计结论。
- 真实第三方 provider 的限流、网关 close frame 和长期稳定性受账号/网络窗口影响；本地确定性 TTS 降级路径已覆盖，但不能据此宣称云端服务在所有限流窗口都成功。
- 只启用单个 Analysis lane 的独立真实进程回放尚未单独启动；脚本判定和控制器路径已按开关核对，真实设备阶段再补矩阵即可。

## 后续建议

1. 先按后续自然语音和真实设备验收计划补样本，保存只含统计量的报告，不把原始 PCM 或完整远程响应提交到仓库。
2. 在限流窗口稳定后，用同一组短文本复测 MiniMax 任务结束、部分 PCM 后 close 和 pacing；区分 provider 限流与本地调度故障。
3. 若继续扩展公开服务状态，先为 VAD/Analysis 建立明确的协议字段和前端类型，再修改 `RuntimeState`，不要用现有 ASR 槽位承载新的公开语义。

本轮没有 P0/P1 遗留项；v0.2 是当前主开发和验收路径，默认配置暂作为发布闸门保留 v0.1。
