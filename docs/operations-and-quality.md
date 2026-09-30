# 运行与质量门槛

本文是当前测试、启动、现场验收和排障入口。架构规则见[架构文档](architecture.md)，LLM 质量门槛见[LLM Runbook](llm-json-schema-migration.md)，开放风险见[当前已知问题](known-issues.md)。

## 自动化门槛

仓库根目录执行：

```powershell
python -m pytest -q
npm test
npm run typecheck
npm run build
git diff --check
```

修改以下区域时优先运行对应测试：

| 区域 | 测试入口 |
| --- | --- |
| Bridge 2.0（桥接协议 2.0）与 DSH（代理运行时）事件 | `tests/test_dsh_bridge.py`、`tests/test_bridge_v2_information.py`、`tests/test_durable_shadow_mailbox.py`、`tests/test_shadow_agent_harness.py` |
| Shadow、上下文和 ledger | `tests/test_shadow_agent.py`、`tests/test_shadow_agent_harness.py`、`tests/test_session_ledger.py`、`tests/test_conversation_runtime.py` |
| TTS、speech admission、分段发送 | `tests/test_minimax_tts.py`、`tests/test_speech_admission.py`、`tests/test_sentence_scheduling.py` |
| 声纹 | `tests/test_speaker.py`、`tests/test_speaker_features.py` |
| KWS、输入所有权与静默窗口 | `tests/test_kws_always_on.py`、`tests/test_scheduler_staircase.py`、`tests/test_kws_wake_window.py` |
| Product UI | `npm test`、`npm run typecheck` |

完整回归不能只运行局部测试；协议、上下文、生命周期或公共事件变更必须运行 Python 全量、前端测试、类型检查和构建。

## 完整启动验收

使用：

```powershell
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

至少确认：

1. `http://127.0.0.1:5173/` 在限定时间内可访问；
2. `http://127.0.0.1:8765/health` 返回服务快照；
3. ASR worker（默认 8766）和 analysis worker（默认 8767）按配置启动；
4. WebSocket `subscribe` 可以获取 `runtime.snapshot`；
5. KWS 开启时 `input_owner=kws`，关闭时可以直接进入 ASR；
6. 唤醒确认播完后麦克风仍归 ASR：窗口内接话不需要再喊唤醒词，静默窗口到期后由 `final_silence`
   检查交回 `input_owner=kws`（日志依次出现 `SCHEDULER_FINAL_PENDING window_index=225`、
   `prompt_kind=final_silence`（最终静默检查请求类型），且交接完成后调度器不再派发检查请求）；
7. `session.stop` 停止输入但不误取消已经进入 TTS 的输出；
8. `session.reset` 清理 conversation、Shadow 和 speech 临时状态。

`-NoWeb` 只用于后端调试，不能作为完整 Web 语音终端验收结果。

## Bridge 2.0 信息观察

`progress`（界面进度）只更新产品活动视图，不创建语音模型请求。`information`（Voice 信息）由 DSH 根 Agent 或状态观察器写入同一个内存单槽；最后到达的信息覆盖旧值，并在调度安全点合并为一次 Voice 请求。请求审计快照完成后，信息按本地版本认领并从共享槽清除；重试只能使用该逻辑请求的本地快照，不能把旧信息带入后续无关请求。是否播报由 Voice 主模型结合当前用户上下文判断。

现场检查 `SHADOW_INFORMATION`（影子信息事件）、`SHADOW_INFORMATION_CONSUMED`（影子信息认领）与 `LLM_DISPATCHED`（模型请求派发）的数量和版本；确认 UI 进度变化没有引发模型请求、被覆盖的信息没有进入请求上下文、静默决策不会重新消费旧信息。终态应清除尚未认领的信息，并且每次激活只接受一个终态。

协议结构、会话迁移要求、本次自动化测试结果和隔离 HTTP（超文本传输协议）验收边界见 [Bridge 2.0 可替换信息协议](bridge-2.0-protocol.md)。现场端到端验收从 `SessionRuntime.start_listening()`（启动 Voice 输入会话）进入；若要验证真实模型推理，必须在启动服务后观察实际模型请求和语音端决策，不能将请求消息构造测试视为模型实测。

## Shadow 验收

必须确认：

- 每个 session 只有一个 Shadow runner；相同 revision 幂等，运行中 wake 合并到最新 revision；
- 当前 dispatch 的原始用户输入进入 Shadow snapshot；后续 delta 无损且按 revision 连续；
- 活跃期重复 `request_shadow` 不创建第二个 activation；下一个安全点前 inbox 只有一条合并上下文输入，
  且保留窗口内所有 frames；消息被 claim 后才开启下一合并窗口；
- `shadow_agent.runtime_reminder_interval_ms` 默认 20000 ms；长推理在 20、40、60 秒等阈值注入软性
  收敛提醒，提醒只走 `inject` 的 next-step 安全边界，不唤醒 idle、不打断工具，终态后不再注入；
- 活跃期 context/wake 响应丢失时通过 `session.state` 和原 request ID 幂等恢复，不因已生效的请求发布
  假 `failed`；只有首次启动、明确拒绝或状态核对失败才记一次基础设施失败；
- stale 工具不执行，stale final 不发布；
- `progress`、status 和工具 lifecycle 不进入主 mailbox；只有语义事件通过唯一 heartbeat 进入主 Agent；
- `session.stop` 保留 Shadow，`session.reset` 和 close 清理 Shadow；
- 80% 上下文预算处换代失败时保留旧 trace，不终止主语音会话；
- Observer 不接收原始 reasoning，不调用工具，不控制主语音。

真实 provider 验收应从 `SessionRuntime.start_listening()` 进入，可以替换 ASR/TTS 的边界输入，但不得直接调用私有方法冒充端到端结果。至少覆盖查询中改口、补充筛选、clarification、长期多步工具循环、stop 后事件缓存和 reset 隔离。

## Product UI 验收

真实入口是 `/`；`/ui-demo.html` 是离线 legacy 原型。开发回放：

```text
http://127.0.0.1:5173/?replay=voice-shadow
```

外部 Edge 至少往返执行：

```text
工作台 → 设置 → 工作台 → 设置
```

确认设置表单状态保留、长 Shadow 时间线只在面板内滚动、页面整体高度不持续增长、隐藏页面没有无限动画或继续发送请求。导航只更新页面 ref，不应在 click handler 中重新连接 WebSocket、请求 snapshot 或等待后端。

Product UI 的会话排序只信任服务端 `conversation.append` / `conversation.snapshot`；不要用客户端到达顺序推断用户话语、assistant speech 和 system speech 的关系。

## LLM 与缓存验收

新增模型字段前先填写[缓存契约字段准入表](llm-context-cache-contract.md#字段准入)。至少确认：

- static instructions、schema、action catalog 在 episode 内 digest 不变；
- HISTORY 只追加；
- 动态时间、revision、run ID 和播放状态留在 REQUEST_TAIL；
- `voice_delivery` 只在结算后写入；
- 相邻 Responses 请求的 `cached_tokens` 随稳定前缀增长；没有 usage 时标记不可观测；
- thinking+tools 暂停/恢复不会产生孤立 `function_call_output`。

真实 provider 的非法 JSON、恢复耗尽、terminal event 缺失和 capability 变化，统一按 [LLM Runbook](llm-json-schema-migration.md) 记录，不再创建日期化调查文档。

## TTS 与音频生命周期

修改 MiniMax Bidi、播放、flush、cancel、replacement 或 reset 时，至少覆盖：

```powershell
python -m unittest tests.test_minimax_tts tests.test_tts_features.BidiLifecycleTests -v
python -m unittest discover -s tests -v
```

确认 flush/close、cancel/close 并发时不会出现 `NoneType` 内部异常；关闭后不存在继续运行的 provider receiver、后台任务或旧 playback key。

声纹注册/测试与实时音频必须互斥，尤其要验证 KWS standby 下设备和 `AudioPreprocessor` 的占用；具体门槛见[声纹增强](speaker-enhancement.md)。

## 运行排障

优先查：

1. `/health` 中的 `startup_errors`、`services`、`input_owner`、KWS 和 speaker 状态；
2. `audio.capture_progress`、`audio.player_first_pcm_submitted`、`audio.player_drained`；
3. `speech_id`、`turn_id`、`owner_scope`、`continuation_mode` 和 speech 结算事件；
4. 重复播报类问题按这些锚点定位：`SPEECH_PREFIX_RECONCILED`（`baseline_chars` 是否在事务换头时缩短）、`SPEECH_REVISED`（`path=rewrite|append|begin`）、`SPEECH_REVISION_NOOP`、`SPEECH_TAIL_REWRITE_UNAVAILABLE`、`SPEAK_CANCELLED_WITH_REPLACEMENT`、`SPEECH_IDENTICAL_IGNORED`，以及 `LLM_SPEAK_CONTEXT` 的 `heard_chars`；
5. LLM context audit 中的 `conversation_id + request_id + turn_id + wire_sha256`；
6. `logs/`、`reports/` 的本地诊断产物。

不要把本地日志路径或一次性 probe 结果写进当前规范文档。日志可能包含真实会话内容，不能提交到仓库；API key 和 Authorization header 永远不得出现在日志中。

## 变更质量要求

跨模块删除或替换前必须回答：

1. 被删除能力的新 owner 是谁？
2. 哪个行为测试证明新 owner 已覆盖原边界？
3. 旧配置或旧协议是明确失败，还是有兼容迁移？
4. 是否同步更新 README、公共协议和当前已知问题？
