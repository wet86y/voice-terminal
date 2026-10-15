# 运行与质量门槛

本文是当前测试、启动、现场验收和排障入口。架构规则见[架构文档](architecture.md)，LLM 质量门槛见[LLM Runbook](llm-json-schema-migration.md)，开放风险见[当前已知问题](known-issues.md)。

## 自动化门槛

仓库根目录执行。后端测试复用正式 Python（后端解释器），由测试入口加载 `.local/tools/testing/`（轻量测试工具），成功后自动回收夹具；无需另建完整业务测试环境。

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q
npm test
npm run typecheck
npm run build
git diff --check
```

仅修改文档时执行 `.local/runtimes/python/python.exe -B -X utf8 scripts/check-doc-links.py`（工程内链接与标题锚点检查）和 `git diff --check`（差异格式检查）。当前安装的完整摘要校验执行 `.local/Verify-Package.ps1`（发行文件校验）。目录、数据和事务更新最近一次完整验收见[2026-10-08 本地整理验收](local-layout-acceptance-20261008.md)。

2026-10-10 内存优化与必需组件更新首轮的完整前端为 248 项通过，原生公共模块为 45 项通过，桌面宿主为 18 项通过；完整后端为 1058 项通过、1 项跳过、1 项失败。唯一失败是冻结基线已复现的 `test_secondary_and_final_prompts_are_mutually_exclusive`（次级与最终提示互斥用例），不能将该轮结果写为全量通过。组件资源针对性检查 51 项通过，正式处理器语音检测 42 组流式输入通过，36 组黄金对照事件一致。真实模型更新、损坏候选拒绝、原生下载及保留数据重装的证据见[组件更新验收](model-resource-updates.md)。

随后语音环境校验与下载、首次启动修复的前端回归为 255 项通过，原生公共模块 57 项及桌面宿主 20 项通过；带隔离下载验收的公共模块共 65 项通过，其中额外八项是下载专项检查，不能与 57 项重复相加。类型检查、前端构建、原生正式与调试构建及普通安装器构建通过。开发、用户测试及全新中文空格工作区分别通过 297 项固定程序文件核对，开发与用户测试目录的 423 项及 45 项保护文件摘要保持原值。详情见[下载与首次启动修复](download-startup-flow-20261010.md)及[当前交付记录](current-release.md)。

提交前再次执行完整后端回归，结果为 1058 项通过、1 项跳过、1 项失败，耗时约 149 秒。失败仍为上述提示互斥基线用例：最后一条提示记录是 `speech_queue_policy`（语音队列策略），用例预期为 `secondary_mailbox`（次级信箱）。本轮未修改对应后端业务源码或测试，不能将该结果表述为全量通过。本机日志位于 `.local/cache/download-flow-20261010/backend-precommit.log`（提交前全量回归日志），基线问题继续登记在[当前已知问题](known-issues.md)。

2026-10-10 随后的环境声、严格声纹拦截与重置修复全量后端为 1096 项通过、1 项跳过、1 项失败，约 103 秒；失败仍为上述提示互斥基线用例。前端 255 项及开发入口 3 项通过，类型检查、前端构建和桌面调试构建通过。新增缺陷回归、现有归档材料对照和实际设备边界见[语音可靠性修复](voice-reliability-fixes-20261010.md)。本轮完成源码验证，未覆盖正式程序。

发布验证须同时核验源码、统一程序包、本机已发布本体和安装器。重新构建后仍须执行本体发布；仅生成 `.local/build`（构建缓存）不能证明日常启动程序已更新。固定文件与统一包逐项比较，组件登记按工作区保留内容单独核对，用户设置、历史、模型及运行环境保持原值。组件管理页就绪与模型实际推理、麦克风采样、云端连接是不同验收项目。

修改以下区域时优先运行对应测试：

| 区域 | 测试入口 |
| --- | --- |
| 独立应用宿主、进程树、状态接口与退出 | `tests/test_application_host.py`、`tests/test_dsh_runtime.py`、`apps/frontend/src/product/application-host.test.ts` |
| 能力就绪分档、探测去抖与能力级恢复 | `tests/test_state.py`、`tests/test_application_host.py`、`tests/test_capability_recovery.py`、`tests/test_listening_capability_gate.py`、`apps/frontend/src/product/application-host.test.ts`、`apps/frontend/src/product/ProductApp.test.ts` |
| 便携路径、旧数据导入、组件校验与安全归档 | `tests/test_portable_paths.py`、`tests/test_desktop_packaging.py` |
| 桌面原生路径与启用组合 | `cargo test --manifest-path apps/desktop/Cargo.toml -p voice-desktop-common`（原生公共模块测试） |
| 资源下载、复制、完整摘要与首次安装后的启动 | `apps/frontend/src/management/ManagementApp.test.ts`（刷新竞态、计时、终态补查和启动动画）；`cargo test --manifest-path apps/desktop/Cargo.toml -p voice-desktop-common -p voice-terminal-desktop --locked`（资源与宿主回归）；公共模块增加 `--features qualification`（隔离下载验收）时验证本机测试服务器、缓存、断点及全局并发。正式构建不开放该测试来源。 |
| ASR（自动语音识别）分块与最终刷新 | `tests/test_asr_worker.py`、`tests/test_asr_worker_protocol.py` |
| Bridge 2.0（第二版桥接协议）、二等信箱与 DSH（代理运行时）事件 | `tests/test_dsh_bridge.py`、`tests/test_bridge_v2_information.py`、`tests/test_secondary_mailbox.py`、`tests/test_scheduler_staircase.py`、`tests/test_durable_shadow_mailbox.py`、`tests/test_shadow_agent_harness.py` |
| Shadow、上下文和 ledger | `tests/test_shadow_agent.py`、`tests/test_shadow_agent_harness.py`、`tests/test_session_ledger.py`、`tests/test_conversation_runtime.py` |
| TTS、speech admission、分段发送 | `tests/test_minimax_tts.py`、`tests/test_speech_admission.py`、`tests/test_sentence_scheduling.py` |
| 声纹 | `tests/test_speaker.py`、`tests/test_speaker_features.py` |
| KWS、输入所有权与静默窗口 | `tests/test_kws_always_on.py`、`tests/test_scheduler_staircase.py`、`tests/test_kws_wake_window.py` |
| Product UI（产品界面）与空输入展示 | `apps/frontend/src/product/useProductAdapter.test.ts`、`apps/frontend/src/conversation-log.test.ts`、`npm test`、`npm run typecheck` |
| 组件内存、进程显存与设备环境 | `tests/test_gpu_environment.py`（真实计算门禁）、`tests/test_runtime_resources.py`（去重指标）、`tests/test_application_host.py`（受管进程树）、`tests/test_product_api.py`（设备保存与重新检测）、`apps/frontend/src/product/ProductApp.test.ts`（未知指标隐藏与设备选项） |
| 唤醒归档持久删除与旧任务迁移 | `tests/test_wake_task_api.py`（取消与归档删除接口）、`integrations/dsh/voice-terminal/test/wake-tasks/task-service.test.mjs`（持久化失败、重启与旧存储导入）、`apps/frontend/src/product/ProductApp.test.ts`（删除回执与错误展示） |
| DSH 统一插件、组件依赖与加载生命周期 | `npm --prefix integrations/dsh/voice-terminal test`（含类型、依赖与真实加载器组合测试）、`tests/test_dsh_profile.py`（单包迁移）、`tests/test_dsh_setup_backup.py`（安装事务与配置保留）；维护要求见[DSH 插件维护手册](dsh-plugin-maintenance.md) |
| 快速／深度模式与均衡归档 | `tests/test_reasoning_modes.py`（双模式验收）、`tests/test_reasoning_policy.py`（思考策略）、`apps/frontend/src/composables/useVoiceTerminal.test.ts`（前端会话协议）、`apps/frontend/src/ui-demo/UiDemoApp.test.ts`（演示界面） |

完整回归不能只运行局部测试；协议、上下文、生命周期或公共事件变更必须运行 Python 全量、前端测试、类型检查和构建。

2026-10-07 的 DSH（代理执行宿主）插件合并已完成项目内安装和数据核对。当次工作区后端全量为 855 项通过、1 项跳过，前端为 164 项界面测试及 3 项启动脚本测试通过；统一插件 33 项回归、真实加载器组合、隔离安装和当前项目安装校验均通过。结果及真实模型、设备和桌面发行的验收边界见[插件安装与验证记录](dsh-plugin-maintenance.md#2026-10-07-插件合并与项目安装验收历史记录)。

历史常驻运行期间曾出现[已登记的全量挂起问题](known-issues.md)。2026-10-07 的双模式源码回归完成，780 项通过、1 项跳过；随后界面与固定服务密钥修订全量回归为 815 项通过、1 项跳过；组件与归档删除修复全量回归为 825 项通过、1 项跳过，隔离解释器入口最终 40 项定向检查通过，均未重现挂起；历史原因仍待定位。再次挂起时记录受阻的全量命令及定向回归范围，不将定向通过表述为全量通过。

需要只验证源码、暂缓更新正式程序时，执行上方后端全量命令、`npm test`（前端测试）、`npm run frontend:build`（类型检查及前端构建）和 `scripts/build-desktop.ps1 -Debug -SkipFrontend`（沿用前端产物的桌面调试构建），随后检查差异格式；正式目录发布作为后续独立步骤。双模式及界面修订的历史验收见[统一发布记录](desktop-acceptance.md)，当前覆盖发布规则见[本地目录与覆盖发布](local-layout.md)。均衡模式历史由 Git（版本控制）保存，不保留工作目录副本。

二等信箱恢复的定向回归命令如下。覆盖空信箱、4 秒检查、进度合并与去重、普通调用消费、版本竞争、
构建失败、讲话与忙碌时保留、最终静默优先、生命周期清理、单请求执行以及恢复重试的场景约束：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q tests/test_secondary_mailbox.py tests/test_scheduler_staircase.py tests/test_json_schema_shadow.py tests/test_context_cache_contract.py tests/test_bridge_v2_information.py tests/test_dsh_bridge.py tests/test_shadow_agent.py tests/test_shadow_agent_harness.py tests/test_durable_shadow_mailbox.py tests/test_product_sidecar.py tests/test_speaker_production.py
npm --prefix integrations/dsh/voice-terminal test
```

桥接插件测试同时执行两套 TypeScript（静态类型语言）配置的类型检查。最近一次执行的结果和全量后端回归边界见
[协议验证记录](bridge-2.0-protocol.md#本次实施与验证记录)。既有启动测试夹具失败已于 2026-10-07 修正并通过全量回归；尚未完成的二等现场验收仍登记在
[当前已知问题](known-issues.md)，不能把本地替身测试通过视为实际模型或播放设备验收通过。

## 完整启动验收

使用：

```powershell
npm run desktop:start
```

桌面入口使用当前静态界面；成品入口与托盘规则见[独立应用启动与系统管理](application-lifecycle.md)。启动完成保持待机，以下输入所有权和语音事务检查须由用户显式开始语音监听后执行。

至少确认：

1. 桌面窗口完成会话握手并在限定时间内加载真实界面，普通浏览器访问该地址只显示说明；优先使用 `http://127.0.0.1:5173/`，占用时使用空闲端口；
2. `http://127.0.0.1:8765/health` 返回服务快照；
3. ASR worker（默认 8766）和 analysis worker（默认 8767）按配置启动；
4. WebSocket `subscribe` 可以获取 `runtime.snapshot`；
5. 点击“开始语音会话”时，即使开启 KWS 也直接进入 ASR（自动语音识别）；静默最终检查完成后再交回 KWS（关键词唤醒）待机；
6. 唤醒确认播完后麦克风仍归 ASR：窗口内接话不需要再喊唤醒词，静默窗口到期后由 `final_silence`
   检查交回 `input_owner=kws`（日志依次出现 `SCHEDULER_FINAL_PENDING window_index=225`、
   `prompt_kind=final_silence`（最终静默检查请求类型），且交接完成后调度器不再派发检查请求）；
7. `session.stop` 停止输入但不误取消已经进入 TTS 的输出；
8. `session.reset` 清理 conversation、Shadow 和 speech 临时状态。

提前静默的现场验收：在 ASR（自动语音识别）活跃期明确要求影子代理调用
`request_early_silence({})`（请求提前进入静默），确认工具先返回受理状态，随后日志出现
`SCHEDULER_FINAL_PENDING`（最终检查待执行事件），其中 `reason=early_silence_request`（原因是提前静默请求），
讲话或播报仍在进行时也应立即登记请求，且当前内容不得被取消；输入、模型处理和实际输出空闲后再执行
`prompt_kind=final_silence`（静默前最终检查），检查及必要播报结束后才出现
`input_owner=kws`（输入交给关键词唤醒）。新输入重置静默窗口时，请求应保留、在新窗口重新标记，最终检查只执行一次；
已取消但残留在待播队列中的项目不得阻塞检查派发或 KWS（关键词唤醒）切换。停止或重置会话后旧请求不得恢复。
该场景须使用重建后的插件和新语音会话。

`scripts/start.ps1`（无界面后端调试）不能作为桌面界面验收结果。

## KWS（关键词唤醒）即时反馈验收

唤醒命中后本地播放约 180 毫秒的下降双音，同时启动 LLM（大语言模型）唤醒确认请求、
ASR（语音识别）后台连接检查及 TTS（语音合成）预热。准备期间最多缓存 8 秒连续音频，
就绪后由原采集循环顺序回放；确认语播放等待提示音完成及缓存回放完成。
用户抢先讲话时，过时的确认语沿用现有暂停和有效性检查。失败、停止、重置和设置切换清理准备任务。

唤醒确认沿用 `kws_wake` 提示词（唤醒确认提示词）、协议校验及 `reasoning_effort=none`
（不额外增加推理强度），不执行动作。调度候选立即唤醒现有串行调度循环，无需等下一次心跳；
已有请求运行时保留候选，并在请求结束后重新检查派发许可和激活代次。用户开始讲话或唤醒准备
失效后，旧候选及晚到结果会被丢弃。

ASR 连接、健康检查和输入边界重置在后台进行。准备期间采集循环继续运行并缓存音频，暂停向
KWS 和尚未就绪的 ASR 提交新帧；服务就绪后按采样位置顺序回放缓存，再处理实时输入。检测到
采样缺口、缓存超过 8 秒或 ASR 准备失败时，本次唤醒准备失败并恢复 KWS 待机，不拼接缺失音频。
提示音播放失败只报告可恢复错误，LLM、ASR 和 TTS 任务仍继续。

唤醒命中时按同一个 `source_id`（唤醒编号）启动 TTS 预热和确认语输出。预热只准备连接，不提交
虚构文字或生成预热音频；支持预热的后端由确认语输出领取连接，未命中或失败时走正常合成回退。
用户首句仍在 ASR 首次返回非空文字时启动用户回合的预热；若用户先开口，会清理尚未领取的唤醒
预热，再准备用户回合。

定向回归（唤醒并行准备与播放）：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q tests/test_kws_acceleration.py tests/test_kws_always_on.py tests/test_scheduler_staircase.py tests/test_player.py tests/test_tts_features.py
```

现场需覆盖正常唤醒、唤醒后立即说指令、重复命中、识别服务暂时不可用和准备期间停止。

播放器已启动时，提示音首帧目标为命中处理后 100 毫秒内。按 `source_id`（唤醒编号）关联
`controller.wake_detected`（检测边界）、`controller.wake_cue_started`（提示音首帧）和
`controller.wake_input_ready`（识别就绪及缓存回放完成），以及既有模型派发、预热与正式音频日志。
`turn.latency`（回合延迟）中的新增字段均以毫秒表示：

| 字段 | 中文说明 |
| --- | --- |
| `wake_to_cue_first_pcm_ms` | 命中处理到提示音首帧提交 |
| `wake_to_llm_request_ms` | 命中处理到模型请求派发 |
| `wake_to_asr_ready_ms` | 命中处理到识别服务及输入边界就绪 |
| `wake_to_buffer_drained_ms` | 命中处理到准备音频回放完成 |
| `wake_to_tts_prewarm_ms` | 命中处理到语音合成预热请求 |
| `wake_to_confirmation_first_pcm_ms` | 命中处理到确认语首帧提交 |

这些时间使用单调时钟；首帧提交表示播放器回调开始消费音频，实际听到提示音还受设备输出延迟影响。
网络准备耗时和设备输出耗时分别核对，替身测试不作为真实设备延迟达标的证据。

## 独立应用生命周期验收

在仓库根目录执行定向回归：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q tests/test_application_host.py tests/test_state.py tests/test_capability_recovery.py tests/test_listening_capability_gate.py tests/test_dsh_runtime.py tests/test_product_api.py tests/test_speaker_production.py tests/test_dsh_bridge.py tests/test_handoff.py tests/test_asr_worker.py tests/test_analysis_cancellation.py
npm test
npm run typecheck
npm run build
git diff --check
```

通过 `npm run desktop:publish`（覆盖发布）换入当前正式程序后，直接双击 `.local/VoiceTerminal.exe`（桌面启动程序），脱离 harness（运行支架）进行以下检查；独立交付包使用解压根目录同名入口。构建命令见[桌面应用说明](desktop-application.md)。使用真实业务运行环境和启用组件，故障注入只针对本次实例拥有的进程。

| 场景 | 验收条件 |
| --- | --- |
| 启动反馈 | 原生窗口与可访问管理入口先于业务组件就绪，加载期间持续显示真实阶段；所有启用组件成功或明确失败后结束等待。 |
| 缺环境、缺网页和端口冲突 | 有可见诊断，核心失败保留宿主入口；网页端口冲突使用空闲端口，业务端口冲突拒绝接管未知进程。 |
| 单实例 | 重复启动打开既有入口，不创建第二套业务组件；退出后可以再次启动。 |
| 系统运行页 | 状态和指标来自受管进程，创建时间与进程对应；共享进程不重复计入总量，进程退出后清除指标。能力失败而进程仍存活时仍展示实际占用。 |
| 手动恢复 | 分别注入语音识别、分析、声纹、语音服务故障；重试去重、恢复业务连接与能力，保持待机，不重播语音或重复执行模型动作。语音进程仍在运行时，恢复只重建出问题的能力，ASR、声纹与代理会话保持不断；只有进程退出才重建整个语音服务。 |
| 加载中与故障区分 | 切换本地合成模型等加载窗口内，组件显示“启动中”、主页按钮显示“组件启动中”并拦截开始会话，不出现全屏启动失败页；加载中点击开始会话会在加载完成后自动开始，用户主动停止则撤销该意图。单次健康检查抖动不判故障。 |
| DSH（代理执行宿主）恢复 | 有限自动重启和手动恢复按既有上限工作，桥接与关联会话重新绑定；手动恢复重置自动重启计数。 |
| 统一退出 | 正常运行、模型加载、恢复操作和自动重启等待期间均可退出；总退出预算 15 秒内回收组件并释放端口，强制结束宿主后也无受管子进程残留。 |
| 窗口与页面生命周期 | 关闭主窗口隐藏到托盘，托盘恢复和重复启动唤回已有窗口；托盘创建失败时保留窗口。页面重载先读快照再接收实时更新；源码调试沿用产品窗口隐藏规则，样例关闭则回收其开发服务。 |
| 便携环境与数据 | 最终压缩包在中文、空格目录及不同工作目录启动；停机搬迁后仍使用随包运行环境和独立用户数据，运行和退出后逐文件校验只读发布文件。无预装开发环境的干净设备、无显卡设备与目标系统需另外验收。 |
| 开发兼容 | `-Dev`（开发模式）的热更新、宿主管理接口代理及 SSE（服务器推送事件）可用，开发服务归属被核对，样例不启动业务子进程。 |

完成一次成品启动、运行页使用、退出和再次启动。该检查验证应用生命周期；麦克风、真实供应商请求、语音播报及长会话质量仍按本文其他现场门槛执行。测量、截图与日志留在本机诊断产物中，提交说明记录验证范围和结果。当前已完成的成品验证、内存测量解释及尚未完成的实机检查见[桌面实施验收记录](desktop-acceptance.md)，不能将开发机精简系统搜索路径验证表述为干净设备验收。

## 流式识别与空输入验收

自动化回归应覆盖：完整分块后发送空结束包仍输出缓存尾字、待处理音频只消费一次、带余量的结束包正常刷新、纯空轮次不调用模型、重复结束包不重复推理、轮次缓存在结束后释放。产品展示验证同时覆盖实时连接与离线快照：隐藏 `empty_input`（空输入状态）且 `text`（文本）为空或仅含空白字符的用户气泡，保留非空短回答、助手和系统消息、其他状态记录以及原始诊断记录和顺序号。

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q tests/test_asr_worker.py tests/test_asr_worker_protocol.py
npm run test --workspace @voice-terminal/frontend -- src/product/useProductAdapter.test.ts src/conversation-log.test.ts
```

真实模型验证使用同一段 16kHz（16 千赫兹）单声道录音，分别取一个完整分块、多个完整分块、完整分块边界截断及含剩余音频的长度。以“最后一个含音频块设置 `is_final=True`（最终刷新）”为参考，与“音频块先传入、最后单独传空结束包”的工作进程结果比较；检查末尾文字、重复输出和跨轮缓存清理。记录实际 FunASR（语音识别工具包）版本与模型来源，结果保存在本地诊断产物中。该检查只验证识别工作进程，不等同于麦克风到播报的端到端验收；模型参数调优应另外使用固定录音对照评估。

界面中有空输入记录不代表已请求 LLM（大语言模型）。按 `turn_id`（轮次编号）关联识别结果、`LLM_DISPATCHED`（模型请求派发事件）和完整请求上下文，再核对触发来源与实际文本；同轮编号的非空分句、旁路事件或系统检查不能直接归因为空转写。

## Bridge 2.0 信息观察

`progress`（影子进度）更新产品活动视图，实质变化按有效激活及来源登记到二等信箱；普通请求会携带，否则每 4 秒在单一心跳中检查非空信箱。用户讲话、模型忙碌或退避时保留状态，空信箱不发起二等请求。`information`（影子信息）由 DSH（代理运行时）根 Agent（根代理）或状态观察器写入同一个内存单槽；最后到达的信息覆盖旧值，并在普通调度安全点合并为一次 Voice（语音主模型）请求。请求审计快照完成后，两类快照均按本地版本确认消费；重试使用原逻辑请求的冻结快照，不能消费后来到达的新版本。是否播报由语音主模型结合当前用户上下文、实际传达语音和当前队列判断。

现场检查 `SECONDARY_MAILBOX_ENQUEUED`（二等状态登记）、`SECONDARY_MAILBOX_CONSUMED`（二等版本消费）、`SECONDARY_MAILBOX_DISPATCHED`（二等评估派发）、`SHADOW_INFORMATION`（影子信息事件）、`SHADOW_INFORMATION_CONSUMED`（影子信息认领）与 `LLM_DISPATCHED`（模型请求派发）的数量和版本；确认进度不立即唤醒模型，重复正文不重新登记，普通请求消费后不会随后再触发二等请求。检查讲话和退避跳过后状态保留、暂停不补发、同一内容不从两个入口重复触发、最终检查优先且二等评估不重置静默期限。终态和激活失效应清理进度，每次激活仍只接受一个终态。

协议结构、会话迁移要求、本次自动化测试结果和隔离 HTTP（超文本传输协议）验收边界见 [Bridge 2.0 可替换信息协议](bridge-2.0-protocol.md)。现场端到端验收从 `SessionRuntime.start_listening()`（启动 Voice 输入会话）进入；若要验证真实模型推理，必须在启动服务后观察实际模型请求和语音端决策，不能将请求消息构造测试视为模型实测。

## Shadow 验收

必须确认：

- 每个 session 只有一个 Shadow runner；相同 revision 幂等，运行中 wake 合并到最新 revision；
- 当前 dispatch 的原始用户输入进入 Shadow snapshot；后续 delta 无损且按 revision 连续；
- 活跃期重复 `request_shadow` 不创建第二个 activation；下一个安全点前 inbox 只有一条合并上下文输入，
  且保留窗口内所有 frames；消息被 claim 后才开启下一合并窗口；
- `shadow_agent.runtime_reminder_interval_ms` 默认 `0`（关闭）；收敛要求由首条受管系统提示词一次性承载，
  不按固定间隔重复注入。启用（设为正数毫秒值）后，长推理在对应阈值注入软性收敛提醒，提醒只走 `inject`
  的 next-step 安全边界，不唤醒 idle、不打断工具，终态后不再注入；
- 活跃期 context/wake 响应丢失时通过 `session.state` 和原 request ID 幂等恢复，不因已生效的请求发布
  假 `failed`；只有首次启动、明确拒绝或状态核对失败才记一次基础设施失败；
- stale 工具不执行，stale final 不发布；
- `progress`（影子进度）只登记二等信箱，`information`（影子信息）和终态走普通事件入口；工具 lifecycle（生命周期）不触发主推理，所有调度复用唯一 heartbeat（心跳）；
- `session.stop` 保留 Shadow，`session.reset` 和 close 清理 Shadow；
- 80% 上下文预算处换代失败时保留旧 trace，不终止主语音会话；
- Observer 不接收原始 reasoning，不调用工具，不控制主语音。

真实 provider 验收应从 `SessionRuntime.start_listening()` 进入，可以替换 ASR/TTS 的边界输入，但不得直接调用私有方法冒充端到端结果。至少覆盖查询中改口、补充筛选、clarification、长期多步工具循环、stop 后事件缓存和 reset 隔离。

## Product UI 验收

真实验收使用桌面窗口，不能用外部浏览器加载产品页面替代。`npm run dev:ui`（桌面样例）提供固定场景及语音事件回放。分别在真实桌面和样例桌面往返执行：

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
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q tests/test_minimax_tts.py tests/test_tts_features.py
```

确认 flush/close、cancel/close 并发时不会出现 `NoneType` 内部异常；关闭后不存在继续运行的 provider receiver、后台任务或旧 playback key。

修改短句窗口的锁定边界时，执行 `tests/test_sentence_scheduling.py`（短句调度测试）、`tests/test_speech_admission.py`（文本提交测试）、`tests/test_speak_revision.py`（播报改写测试）和 `tests/test_speech_inference.py`（推理冻结组合测试）。最少有效字符规则应覆盖：恰好达到下限、多个短句合并、标点与空白不计数、生成中不足下限继续等待、原始文本分段入队完成后未提交部分仍可修订、生成结束释放短回答与短尾块。核对 LLM（大语言模型）可见的当前块、预取块与可改尾巴使用实际提交边界，并确认预取仍最多一块。

声纹注册/测试与实时音频必须互斥，尤其要验证 KWS standby 下设备和 `AudioPreprocessor` 的占用；具体门槛见[声纹增强](speaker-enhancement.md)。

## 运行排障

应用尚未就绪或语音服务失败时，先打开托盘“系统运行”，查看宿主快照中的组件错误和阶段；再从托盘打开日志目录。管理入口独立于语音服务，不必等待 `/health`（语音健康检查）可访问。

语音服务已开始监听后优先查：

1. `/health` 中的 `startup_errors`、`services`、`input_owner`、KWS 和 speaker 状态；
2. `audio.capture_progress`、`audio.player_first_pcm_submitted`、`audio.player_drained`；
3. `speech_id`、`turn_id`、`owner_scope`、`continuation_mode` 和 speech 结算事件；
4. 重复播报类问题按这些锚点定位：`SPEAK_BUSY_IGNORED`（忙碌新播报被拒绝）、`SPEECH_REVISED`（尾巴修订成功，`path=rewrite` 仅表示改写）、`SPEECH_REVISION_REJECTED`（确认边界失效）、`SPEECH_REVISION_NOOP`（新尾巴已经锁定）、`SPEAK_CANCELLED_WITH_REPLACEMENT`（停止并替换）、`SPEECH_IDENTICAL_IGNORED`（重复文本被忽略），以及 `LLM_SPEAK_CONTEXT`（模型语音上下文日志）的 `heard_chars`（已听字符数）；同时核对 `speech.inference_frozen`（提交边界已冻结）与 `speech.inference_released`（本次请求已释放）是否配对；
5. LLM context audit 中的 `conversation_id + request_id + turn_id + wire_sha256`；
6. `.local/cache/logs/`（运行日志）及 `.local/cache/reports/`（本地诊断产物）。

不要把本地日志路径或一次性 probe 结果写进当前规范文档。日志可能包含真实会话内容，不能提交到仓库；API key 和 Authorization header 永远不得出现在日志中。

## 变更质量要求

跨模块删除或替换前必须回答：

1. 被删除能力的新 owner 是谁？
2. 哪个行为测试证明新 owner 已覆盖原边界？
3. 旧配置或旧协议是明确失败，还是有兼容迁移？
4. 是否同步更新 README、公共协议和当前已知问题？

### 语音控制入口回归

修改 speak（新播报）、revision（尾巴修订）或 cancel（取消）时，回归
tests/test_speak_revision.py（决策职责）、tests/test_speech_admission.py（提交确认）、
tests/test_speech_continuation.py（真实播放器边界）、tests/test_speech_inference.py（推理冻结组合）、
tests/test_v06_contracts.py（完整与流式契约）、tests/test_context_cache_contract.py（缓存分区），
tests/test_json_schema_shadow.py（模型提示与结构契约），
以及 apps/frontend/src/composables/useVoiceTerminal.test.ts（展示文本同步）。

必须确认：排队、播放、设备输出尾音及结算期间拒绝新 speak（新播报）；忙碌请求在推理期间等到空闲也不能补执行；
revision（尾巴修订）只能修改快照仍匹配的未提交尾巴，发送中、跨轮、版本过期及窗口关闭全部拒绝且不追加；
cancel（取消）带文本仍可硬替换并拒绝旧 PCM（原始音频）；修订事件以权威全文替换展示，拒绝迟到版本。

推理准备必须覆盖：剩余已返回音频恰好 3 秒时不补块、不足时最多补一块、当前块与预取块最多两块、等待期间已有音频足够时撤销补块、2 秒准备超时、正在发送的块与字符计数一起结算。推理中播放耗尽不能推进提交；revision（尾巴修订）后丢弃已取出的旧块，wait（等待）后恢复旧文本调度，speak（新播报）不能改尾巴或追加。失败和取消释放各自票据，旧请求释放不能解锁新请求；空二等信箱及过期恢复不冻结，有效重试仍使用匹配的信箱快照。

测试替身和真实播放器队列通过后，真实供应商与设备仍需要现场试听验收。
