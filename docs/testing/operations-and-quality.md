# 测试、启动与现场验收入口

本文集中记录自动化测试入口、人工验收步骤和测试边界。架构与协议约束见[架构目录](../architecture/architecture.md)，当前交付状态见[正式程序与分发安装器](../releases/current-release.md)，未关闭风险见[当前已知问题](known-issues.md)。历史测试数字以各专项报告中的日期为准，不作为当前全量结果。
新验证的现场、报告与保留规则遵循[开发资产治理](../development/asset-governance.md)。后端、更新交接和音色设计验证已接入受管任务；成功回收现场，正常失败有限保留，未知中断不自动删除。提交前运行 `npm run assets:check`（暂存资产检查），维护时运行 `npm run assets:audit`（只读预算审计）。

## 自动化门槛

在仓库根目录执行。后端测试使用项目正式 Python（后端运行环境）及轻量测试工具；成功后自动回收临时夹具。

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q
npm test
npm run typecheck
npm run build
npm --prefix integrations/dsh/voice-terminal test
git diff --check
```

仅修改 Markdown（文档标记语言）时，执行文档内链与标题锚点检查及差异格式检查：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/check-doc-links.py
git diff --check
```

正式程序固定文件校验使用 `.local/Verify-Package.ps1`（正式包逐文件校验）。测试报告记录自己的执行范围和日期；不能把定向测试写成全量通过，也不能把自动化检查当作麦克风、真实供应商或干净设备验收。

屏幕抓取与多模态用例需要 Pillow（Python 图片处理库）。项目运行依赖已在 `apps/server/requirements.txt`（服务依赖清单）声明 `Pillow>=11,<13`，运行锁固定为 12.3.0；优先使用项目私有解释器。若用独立 Python（运行环境）运行这些用例，先在该解释器安装同一范围的 Pillow，避免 `ModuleNotFoundError: No module named 'PIL'`（找不到图片处理模块）。

## 区域与测试入口

| 区域及中文说明 | 测试入口及中文说明 |
| --- | --- |
| 独立应用宿主、启动预算、进程树、状态接口与退出 | `tests/test_application_host.py`、`tests/test_startup_optimizations.py`、`tests/test_dsh_runtime.py`、`apps/frontend/src/product/application-host.test.ts` |
| 能力就绪分档、探测去抖与能力级恢复 | `tests/test_state.py`、`tests/test_application_host.py`、`tests/test_capability_recovery.py`、`tests/test_listening_capability_gate.py`、`apps/frontend/src/product/application-host.test.ts`、`apps/frontend/src/product/ProductApp.test.ts` |
| 便携路径、旧数据导入、组件校验与安全归档 | `tests/test_portable_paths.py`、`tests/test_desktop_packaging.py` |
| 桌面原生路径与启用组合 | `cargo test --manifest-path apps/desktop/Cargo.toml -p voice-desktop-common`（原生公共模块测试） |
| 资源下载、复制、摘要与首次安装启动 | `apps/frontend/src/management/ManagementApp.test.ts`（管理页状态回归）；`cargo test --manifest-path apps/desktop/Cargo.toml -p voice-desktop-common -p voice-terminal-desktop --locked`（原生资源与宿主回归）；公共模块可增加 `--features qualification`（隔离下载验收） |
| ASR（自动语音识别）分块与最终刷新 | `tests/test_asr_worker.py`、`tests/test_asr_worker_protocol.py` |
| 屏幕抓取、图片预处理与多模态上下文 | `tests/test_screen_capture.py`、`tests/test_multimodal_context.py`；需要 Pillow（Python 图片处理库），已列入 `apps/server/requirements.txt`（服务依赖清单）并固定在运行锁中 |
| Bridge 2.0（第二版桥接协议）、二等信箱与 DSH（代理执行宿主）事件 | `tests/test_dsh_bridge.py`、`tests/test_bridge_v2_information.py`、`tests/test_secondary_mailbox.py`、`tests/test_scheduler_staircase.py`、`tests/test_durable_shadow_mailbox.py`、`tests/test_shadow_agent_harness.py` |
| Shadow（后台代理）、上下文与台账 | `tests/test_shadow_agent.py`、`tests/test_shadow_agent_harness.py`、`tests/test_session_ledger.py`、`tests/test_conversation_runtime.py` |
| TTS（语音合成）、语音准入与分段发送 | `tests/test_minimax_tts.py`、`tests/test_speech_admission.py`、`tests/test_sentence_scheduling.py` |
| 声纹身份识别 | `tests/test_speaker.py`、`tests/test_speaker_features.py` |
| KWS（关键词唤醒）、输入所有权与静默窗口 | `tests/test_kws_always_on.py`、`tests/test_scheduler_staircase.py`、`tests/test_kws_wake_window.py` |
| Product UI（产品界面）与空输入展示 | `apps/frontend/src/product/useProductAdapter.test.ts`、`apps/frontend/src/conversation-log.test.ts`、`npm test`、`npm run typecheck` |
| 组件内存、进程显存与设备环境 | `tests/test_gpu_environment.py`、`tests/test_runtime_resources.py`、`tests/test_application_host.py`、`tests/test_product_api.py`、`apps/frontend/src/product/ProductApp.test.ts` |
| 唤醒任务归档删除与旧任务迁移 | `tests/test_wake_task_api.py`、`integrations/dsh/voice-terminal/test/wake-tasks/task-service.test.mjs`、`apps/frontend/src/product/ProductApp.test.ts` |
| DSH 统一插件、组件依赖与加载生命周期 | `npm --prefix integrations/dsh/voice-terminal test`、`tests/test_dsh_profile.py`、`tests/test_dsh_setup_backup.py`；维护要求见[DSH 插件维护手册](../guides/dsh-plugin-maintenance.md) |
| 快速／深度模式与策略归档 | `tests/test_reasoning_modes.py`、`tests/test_reasoning_policy.py`、`apps/frontend/src/composables/useVoiceTerminal.test.ts`、`apps/frontend/src/ui-demo/UiDemoApp.test.ts` |

## DSH 插件专项回归

在仓库根目录运行插件组合测试和相关后端测试：

```powershell
npm --prefix integrations/dsh/voice-terminal test
.local/runtimes/python/python.exe -B -X utf8 scripts/test-backend.py -q tests/test_dsh_profile.py tests/test_dsh_preset.py tests/test_dsh_runtime.py tests/test_dsh_setup_backup.py
```

插件测试覆盖宿主和浏览器类型检查、组件与依赖检查、官方浏览器构建、桥接及任务回归，以及真实官方加载器的隔离组合测试。组合测试使用独占临时数据目录、随机回环端口和本地模型替身，不访问真实模型供应方，也不读写生产任务；测试产物位于 `.local/build/dsh/bundles/voice-terminal-test`（隔离测试包），正式构建不包含测试专用依赖链接。回归需验证行为边界，不能只用字符串匹配代替执行测试，也不能用手工上下文组合替代真实加载器测试。加载器测试应覆盖唯一安装包、完整预设、浏览器模块唯一注册和组件卸载清理。

修改安装、配置迁移或摘要规则时，另运行隔离安装检查：

```powershell
python -X utf8 scripts/dsh-diagnose/plugin-install-smoke.py
```

该检查使用当前解释器加载隔离测试夹具，验证受管补丁保留、生产宿主闭包及覆盖更新恢复；成功后自动回收夹具，不创建项目或环境副本。

## Bridge 2.0 协议回归

修改事件顺序、字段、错误码或 session（会话）生命周期时，同步更新 Web（网页端）类型、Product adapter（产品数据适配器）及对应回归。测试范围包括 `tests/test_dsh_bridge.py`、`tests/test_bridge_v2_information.py`、`tests/test_secondary_mailbox.py`、`tests/test_scheduler_staircase.py`、`tests/test_durable_shadow_mailbox.py` 和 `tests/test_shadow_agent_harness.py`；协议契约见[Bridge 2.0 可替换信息协议](../architecture/bridge-2.0-protocol.md)。

## 专项性能基准

`scripts/benchmark_asr_streaming.py`（真实流式识别回放工具）使用本地录音，通过本机网络连接按实际时钟发送音频，收包与模型执行相互独立；最终包会排在先前音频之后。工具输出逐轮结果、首字时间、最终等待及模型窗口统计，不启用麦克风，也不访问在线模型。比较不同构建时固定录音摘要、模型、分块、发送节奏和测量环境，核对实际设备；设备回退时该基准应判失败。

`scripts/benchmark_smart_turn.py`（语音结束判断回放工具）使用同一录音中的短片段、完整语音、语音加静音和纯静音，记录完整请求、线程等待、预处理、模型计算及判断概率。比较 CPU（中央处理器）量化模型和 GPU（图形处理器）单精度模型时，检查阈值两侧案例及实际设备；有限样本一致不能证明所有输入具有相同概率或结果。识别与设备行为说明见[流式识别延迟与图形处理器支持](../guides/asr-latency-and-cuda.md)。

单模型回放不替代整套应用并发、麦克风采集、实时打断及真实设备验收。

## 本机测试入口

| 入口 | 中文说明 |
| --- | --- |
| `.local/VoiceTerminal.exe` | 当前工作区正式程序入口 |
| `npm run desktop:start` | 与双击相同的正式启动流程 |
| `npm run dev` | 使用业务源码与界面热更新的开发入口 |
| `.local/VoiceTerminalUIDemo.exe` | 隔离缓存的界面演示入口，不启动业务服务 |
| `npm run dev:ui` | 构建并启动界面演示入口 |
| `.local/releases/installer/VoiceTerminalSetup.exe` | 普通分发安装器，内嵌正式程序包 |
| `.local/build/program-package/program.zip` | 本地发布、安装器及后续正式更新共用的程序包 |

同一工作区只允许一个业务实例。切换普通启动与源码调试前，先从应用内退出当前业务实例。另一工作区的正式程序、界面演示和安装器可以同时运行。

## 重建与核对

通过应用内退出业务实例并关闭演示窗口，再执行：

```powershell
npm run desktop:publish
./scripts/distribute-desktop.ps1 -SkipProductBuild
./.local/Verify-Package.ps1
./.local/runtimes/python/python.exe -B -X utf8 scripts/test-backend.py -q
npm test
npm run typecheck
npm --prefix integrations/dsh/voice-terminal test
./.local/runtimes/python/python.exe -B -X utf8 scripts/check-doc-links.py
git diff --check
```

`npm run desktop:dist`（完整分发构建）可独立重建正式包和安装器。需要核对本机正式包与安装器逐文件一致时，先发布，再以 `-SkipProductBuild`（复用已发布程序包）构建安装器，避免比较两个不同构建。安装器的 `delivery.json`（交付记录）与 `build-stamp.json`（构建戳）中的 `program_package_sha256`（内嵌程序包摘要）应与当前统一程序包一致；正常交付的 `qualification`（隔离验收开关）为 `false`（关闭）。

## 用户测试顺序

1. 在本机正式程序检查现有设置、历史、组件管理、服务状态与正常退出；退出后使用 `npm run dev`（业务源码调试入口）检查相同工作区及组件路径。
2. 在界面演示入口核对交互。演示使用隔离缓存，不操作正式用户数据。
3. 在普通安装器选择空目录，检查安装进度与按钮；确认安装器退出后，正式程序的组件管理页能独立使用并可再次启动。
4. 退出目标正式实例，再选择原工作区覆盖重装；核对用户内容、模型、运行环境及组件登记保留。目标仍在运行时应提示退出后重试。
5. 在正式程序中验证已有工作区切换、空工作区初始化、整体迁移与失败恢复；搬迁完成后直接启动目标根目录正式入口。

## 完整启动验收

使用：

```powershell
npm run desktop:start
```

桌面入口使用当前静态界面；成品入口与托盘规则见[独立应用启动与系统管理](../guides/application-lifecycle.md)。启动完成保持待机，以下输入所有权和语音事务检查须由用户显式开始语音监听后执行。

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

通过 `npm run desktop:publish`（覆盖发布）换入当前正式程序后，直接双击 `.local/VoiceTerminal.exe`（桌面启动程序），脱离 harness（运行支架）进行以下检查；独立交付包使用解压根目录同名入口。构建命令见[桌面应用说明](../guides/desktop-application.md)。使用真实业务运行环境和启用组件，故障注入只针对本次实例拥有的进程。

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

声学信箱另外验证无转写场景：激活会话并启动静默窗口后，不提供任何 ASR（语音识别）文本，只输入可识别环境声音，确认每秒入箱，在有效四秒检查点发起一次二等推理；关闭影子代理仍须成立。再验证普通请求提前消费、讲话或忙碌跳过、最终静默优先以及声音入箱不重置期限。声学观察应出现在 `runtime.acoustic_events`（待消费声音观察列表），不伪装为影子进度；情绪单标签持续更新不增加请求次数。回归入口为 `tests/test_audio_enhancements.py`（音频增强契约测试），实测见[音频增强验收记录](audio-enhancements-20261020.md)。

协议结构、会话迁移要求、本次自动化测试结果和隔离 HTTP（超文本传输协议）验收边界见 [Bridge 2.0 可替换信息协议](../architecture/bridge-2.0-protocol.md)。现场端到端验收从 `SessionRuntime.start_listening()`（启动 Voice 输入会话）进入；若要验证真实模型推理，必须在启动服务后观察实际模型请求和语音端决策，不能将请求消息构造测试视为模型实测。

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

新增模型字段前先填写[缓存契约字段准入表](../architecture/llm-context-cache-contract.md#字段准入)。至少确认：

- static instructions、schema、action catalog 在 episode 内 digest 不变；
- HISTORY 只追加；
- 动态时间、revision、run ID 和播放状态留在 REQUEST_TAIL；
- `voice_delivery` 只在结算后写入；
- 相邻 Responses 请求的 `cached_tokens` 随稳定前缀增长；没有 usage 时标记不可观测；
- thinking+tools 暂停/恢复不会产生孤立 `function_call_output`。

真实 provider 的非法 JSON、恢复耗尽、terminal event 缺失和 capability 变化，统一按 [LLM Runbook](../guides/llm-json-schema-migration.md) 记录，不再创建日期化调查文档。

## TTS 与音频生命周期

修改 MiniMax Bidi、播放、flush、cancel、replacement 或 reset 时，至少覆盖：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q tests/test_minimax_tts.py tests/test_tts_features.py
```

确认 flush/close、cancel/close 并发时不会出现 `NoneType` 内部异常；关闭后不存在继续运行的 provider receiver、后台任务或旧 playback key。

修改短句窗口的锁定边界时，执行 `tests/test_sentence_scheduling.py`（短句调度测试）、`tests/test_speech_admission.py`（文本提交测试）、`tests/test_speak_revision.py`（播报改写测试）和 `tests/test_speech_inference.py`（推理冻结组合测试）。最少有效字符规则应覆盖：恰好达到下限、多个短句合并、标点与空白不计数、生成中不足下限继续等待、原始文本分段入队完成后未提交部分仍可修订、生成结束释放短回答与短尾块。核对 LLM（大语言模型）可见的当前块、预取块与可改尾巴使用实际提交边界，并确认预取仍最多一块。

声纹注册/测试与实时音频必须互斥，尤其要验证 KWS standby 下设备和 `AudioPreprocessor` 的占用；具体门槛见[声纹增强](../guides/speaker-enhancement.md)。

## 运行排障

应用尚未就绪或语音服务失败时，先打开托盘“系统运行”，查看宿主快照中的组件错误和阶段；需要导出诊断材料时，在设置页左下角点击“日志”，程序会刷新日志并打开文件夹选择器，选定目录后生成 ZIP（压缩包）。语音服务不可用时仍会尝试打包磁盘上已有的日志。管理入口独立于语音服务，不必等待 `/health`（语音健康检查）可访问。

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
tests/test_speech_continuation_window.py（续写窗口、截图与后台回传）、
tests/test_v06_contracts.py（完整与流式契约）、tests/test_context_cache_contract.py（缓存分区），
tests/test_json_schema_shadow.py（模型提示与结构契约），
以及 apps/frontend/src/composables/useVoiceTerminal.test.ts（展示文本同步）。

必须确认：排队、播放、设备输出尾音及结算期间拒绝新 speak（新播报）；忙碌请求在推理期间等到空闲也不能补执行；
revision（尾巴修订）只能修改快照仍匹配的未提交尾巴，用户换轮不构成拒绝理由；发送中、会话或输出代次失效、版本过期及窗口关闭全部拒绝且不追加；
cancel（取消）带文本仍可硬替换并拒绝旧 PCM（原始音频）；修订事件以权威全文替换展示，拒绝迟到版本。

续写窗口回归还必须覆盖：用户换轮后的截图识别和普通后续发言，以及后台 `information`（影子信息）、`result`（后台结果）、
`input_required`（需要用户输入）、`failed`（后台失败）、`durable`（持久语音事件）和 `system_check`（系统检查）
返回，均能在同会话当前事务仍可写时修订尾巴；旧播报轮次归属保持不变，最近修订来源轮次与请求单独记录。
实际播放结束后的窗口只允许带有效请求票据的推理完成修订，截止后到达的新请求不能借用旧票据。
`system_check`（系统检查）若在处理前已过期，不得恢复旧修订资格或创建后继播放段。

推理准备必须覆盖：剩余已返回音频恰好 3 秒时不补块、不足时最多补一块、当前块与预取块最多两块、等待期间已有音频足够时撤销补块、2 秒准备超时、正在发送的块与字符计数一起结算。推理中播放耗尽不能推进提交；revision（尾巴修订）后丢弃已取出的旧块，wait（等待）后恢复旧文本调度，speak（新播报）不能改尾巴或追加。失败和取消释放各自票据，旧请求释放不能解锁新请求；空二等信箱及过期恢复不冻结，有效重试仍使用匹配的信箱快照。

测试替身和真实播放器队列通过后，真实供应商与设备仍需要现场试听验收。
