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
| 独立应用宿主、进程树、状态接口与退出 | `tests/test_application_host.py`、`tests/test_dsh_runtime.py`、`apps/web/src/product/application-host.test.ts` |
| ASR（自动语音识别）分块与最终刷新 | `tests/test_asr_worker.py`、`tests/test_asr_worker_protocol.py` |
| Bridge 2.0（桥接协议 2.0）与 DSH（代理运行时）事件 | `tests/test_dsh_bridge.py`、`tests/test_bridge_v2_information.py`、`tests/test_durable_shadow_mailbox.py`、`tests/test_shadow_agent_harness.py` |
| Shadow、上下文和 ledger | `tests/test_shadow_agent.py`、`tests/test_shadow_agent_harness.py`、`tests/test_session_ledger.py`、`tests/test_conversation_runtime.py` |
| TTS、speech admission、分段发送 | `tests/test_minimax_tts.py`、`tests/test_speech_admission.py`、`tests/test_sentence_scheduling.py` |
| 声纹 | `tests/test_speaker.py`、`tests/test_speaker_features.py` |
| KWS、输入所有权与静默窗口 | `tests/test_kws_always_on.py`、`tests/test_scheduler_staircase.py`、`tests/test_kws_wake_window.py` |
| Product UI（产品界面）与空输入展示 | `apps/web/src/product/useProductAdapter.test.ts`、`apps/web/src/conversation-log.test.ts`、`npm test`、`npm run typecheck` |

完整回归不能只运行局部测试；协议、上下文、生命周期或公共事件变更必须运行 Python 全量、前端测试、类型检查和构建。

本机全量测试仍有[已登记的挂起问题](known-issues.md)。挂起时记录受阻的全量命令及定向回归范围，不将定向通过表述为全量通过。

## 完整启动验收

使用：

```powershell
npm run build
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

普通入口使用已构建的网页；成品入口与托盘规则见[独立应用启动与系统管理](application-lifecycle.md)。启动完成保持待机，以下输入所有权和语音事务检查须由用户显式开始语音监听后执行。

至少确认：

1. 启动程序打开的实际网页地址在限定时间内可访问；优先使用 `http://127.0.0.1:5173/`，占用时使用空闲端口；
2. `http://127.0.0.1:8765/health` 返回服务快照；
3. ASR worker（默认 8766）和 analysis worker（默认 8767）按配置启动；
4. WebSocket `subscribe` 可以获取 `runtime.snapshot`；
5. KWS 开启时 `input_owner=kws`，关闭时可以直接进入 ASR；
6. 唤醒确认播完后麦克风仍归 ASR：窗口内接话不需要再喊唤醒词，静默窗口到期后由 `final_silence`
   检查交回 `input_owner=kws`（日志依次出现 `SCHEDULER_FINAL_PENDING window_index=225`、
   `prompt_kind=final_silence`（最终静默检查请求类型），且交接完成后调度器不再派发检查请求）；
7. `session.stop` 停止输入但不误取消已经进入 TTS 的输出；
8. `session.reset` 清理 conversation、Shadow 和 speech 临时状态。

提前静默的现场验收：在 ASR（自动语音识别）活跃期明确要求影子代理调用
`request_early_silence({})`（请求提前进入静默），确认工具先返回受理状态，随后日志出现
`SCHEDULER_FINAL_PENDING`（最终检查待执行事件），其中 `reason=early_silence_request`（原因是提前静默请求），
再执行 `prompt_kind=final_silence`（静默前最终检查），检查及必要播报结束后才出现
`input_owner=kws`（输入交给关键词唤醒）。讲话中或播报期间请求时应等待；等待期间补充新输入时仍按最新一轮收尾，
停止或重置会话后旧请求不得恢复。该场景须使用重建后的插件和新语音会话。

`-NoWeb` 只用于后端调试，不能作为完整 Web 语音终端验收结果。

## 独立应用生命周期验收

在仓库根目录执行定向回归：

```powershell
python -m pytest -q tests/test_application_host.py tests/test_dsh_runtime.py tests/test_product_api.py tests/test_speaker_production.py tests/test_dsh_bridge.py tests/test_handoff.py tests/test_asr_worker.py tests/test_analysis_cancellation.py
npm test
npm run typecheck
npm run build
git diff --check
```

构建无终端窗口成品后，直接双击 `.runtime/launcher/VoiceTerminal/VoiceTerminal.exe`（独立应用程序），脱离 harness（运行支架）进行以下检查。使用真实业务运行环境和启用组件，故障注入只针对本次实例拥有的进程。

| 场景 | 验收条件 |
| --- | --- |
| 启动反馈 | 托盘和可访问网页先于业务组件就绪，加载期间持续显示真实阶段；所有启用组件成功或明确失败后结束等待。 |
| 缺环境、缺网页和端口冲突 | 有可见诊断，核心失败保留宿主入口；网页端口冲突使用空闲端口，业务端口冲突拒绝接管未知进程。 |
| 单实例 | 重复启动打开既有入口，不创建第二套业务组件；退出后可以再次启动。 |
| 系统运行页 | 状态和指标来自受管进程，创建时间与进程对应；共享进程不重复计入总量，进程退出后清除指标。能力失败而进程仍存活时仍展示实际占用。 |
| 手动恢复 | 分别注入语音识别、分析、声纹、语音服务故障；重试去重、恢复业务连接与能力，保持待机，不重播语音或重复执行模型动作。 |
| DSH（代理执行宿主）恢复 | 有限自动重启和手动恢复按既有上限工作，桥接与关联会话重新绑定；手动恢复重置自动重启计数。 |
| 统一退出 | 正常运行、模型加载、恢复操作和自动重启等待期间均可退出；总退出预算 15 秒内回收组件并释放端口，强制结束宿主后也无受管子进程残留。 |
| 网页生命周期 | 关闭网页后应用继续运行；重新打开先读快照再接收实时更新；退出状态送达已打开页面并保留提示。 |
| 开发兼容 | `-Dev`（开发模式）的热更新、宿主管理接口代理及 SSE（服务器推送事件）可用，外部调试进程不纳入回收。 |

完成一次成品启动、运行页使用、退出和再次启动。该检查验证应用生命周期；麦克风、真实供应商请求、语音播报及长会话质量仍按本文其他现场门槛执行。测量、截图与日志留在本机诊断产物中，提交说明记录验证范围和结果。

## 流式识别与空输入验收

自动化回归应覆盖：完整分块后发送空结束包仍输出缓存尾字、待处理音频只消费一次、带余量的结束包正常刷新、纯空轮次不调用模型、重复结束包不重复推理、轮次缓存在结束后释放。产品展示验证同时覆盖实时连接与离线快照：隐藏 `empty_input`（空输入状态）且 `text`（文本）为空或仅含空白字符的用户气泡，保留非空短回答、助手和系统消息、其他状态记录以及原始诊断记录和顺序号。

```powershell
python -m pytest -q tests/test_asr_worker.py tests/test_asr_worker_protocol.py
npm run test --workspace @voice-terminal/web -- src/product/useProductAdapter.test.ts src/conversation-log.test.ts
```

真实模型验证使用同一段 16kHz（16 千赫兹）单声道录音，分别取一个完整分块、多个完整分块、完整分块边界截断及含剩余音频的长度。以“最后一个含音频块设置 `is_final=True`（最终刷新）”为参考，与“音频块先传入、最后单独传空结束包”的工作进程结果比较；检查末尾文字、重复输出和跨轮缓存清理。记录实际 FunASR（语音识别工具包）版本与模型来源，结果保存在本地诊断产物中。该检查只验证识别工作进程，不等同于麦克风到播报的端到端验收；模型参数调优应另外使用固定录音对照评估。

界面中有空输入记录不代表已请求 LLM（大语言模型）。按 `turn_id`（轮次编号）关联识别结果、`LLM_DISPATCHED`（模型请求派发事件）和完整请求上下文，再核对触发来源与实际文本；同轮编号的非空分句、旁路事件或系统检查不能直接归因为空转写。

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

应用尚未就绪或语音服务失败时，先打开托盘“系统运行”，查看宿主快照中的组件错误和阶段；再从托盘打开日志目录。管理入口独立于语音服务，不必等待 `/health`（语音健康检查）可访问。

语音服务已开始监听后优先查：

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
