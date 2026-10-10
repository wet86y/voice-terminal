# Voice Terminal

Voice Terminal 是 Windows（操作系统）本机实时语音会话应用。Tauri（桌面框架）窗口是主产品唯一的运行、开发和界面验收入口，负责托盘、单实例及服务管理进程；应用宿主负责业务组件的启动、监督和退出。Voice Server（语音服务）负责音频采集、KWS（关键词唤醒）、VAD（语音活动检测）、ASR（自动语音识别）、主 Agent（智能体）、Shadow（影子代理）、TTS（语音合成）和播放。DSH（代理执行宿主）作为内部组件受管，其独立管理控制台继续由系统默认浏览器打开。

当前代码包版本为 `0.9.0`。默认 LLM（大语言模型）路径是 DeepSeek Responses（响应接口）与 strict `json_schema`（严格结构化输出）；`voice_decision_v06`（语音决策协议兼容标识）保持冻结，不是发布版本号。

> 安全边界：默认服务只绑定 `127.0.0.1`。当前管理 API、WebSocket 和模型探测接口不适合直接暴露到局域网或公网；需要远程部署时，先完成 [当前已知问题](docs/known-issues.md) 中的认证和 SSRF 防护工作。

## 运行结构

```text
用户 → ASR → 主 Agent/LLM → Speech transaction → TTS / Player
                         └→ request_shadow({"request": true})
                                      │
                                      ▼
                         单一 ShadowRuntime → 当前后端已授予的工具
                                      │
                                      ├─ progress（进度）→ 二等信箱 → 每 4 秒检查 / 随普通请求携带
                                      ├─ information（影子信息）→ 可替换信息槽 → 普通事件调度
                                      └─ input_required（需用户输入）/ result（结果）/ failed（失败）→ 语义历史
```

主语音链只有一个调度循环和一个语音事务 owner（所有者）。未播放的 assistant（助手）草稿不会写入历史；只有用户输入、实际传达的助手文本、有效 refinement（转写精修）和有语义影响的打断会进入会话账本。Shadow（影子代理）是同一 session（会话）的认知旁路；DSH（代理执行宿主）根代理仅可在用户明确要求或已启用的影子自定义指令明确规定时，请求提前执行静默前最终检查，其余语音、TTS（语音合成）、播放器和主调度控制仍由 Voice（语音主线）拥有。status observer（状态观察器）的 information（影子信息）摘要保持普通事件调度、单槽覆盖和版本消费，不追加为长期语义历史。progress（影子进度）继续更新产品 UI（界面），实质变化合并进入二等信箱；普通请求会携带，否则在活跃静默窗口内每 4 秒复用唯一 400 毫秒心跳检查非空信箱。上下文冻结成功后确认版本，空信箱不请求，最终静默优先，二等检查不延长最终期限。详见 [Bridge 2.0（第二版桥接协议）](docs/bridge-2.0-protocol.md)。

## 快速开始

本地布局与覆盖更新的权威说明见[本地目录与覆盖发布](docs/local-layout.md)。构建编号只记录在清单，常态不保留整包或环境备份。

已实施的目录、清退体积、备份恢复和启动验证见[本地整理验收](docs/local-layout-acceptance-20261008.md)。

本地开发和用户使用同一套正式程序。本机正式入口是 `.local/VoiceTerminal.exe`（正式启动程序）；对外分发 `.local/releases/installer/VoiceTerminalSetup.exe`（安装器），只负责选择工作区、展开或重装同一正式程序包、创建桌面快捷方式与启动交接。组件下载、导入、配置及维护全部由正式程序承担。详见[统一正式程序与分发安装器](docs/unified-product-installer.md)及[当前交付](docs/current-release.md)。`.local/VoiceTerminalUIDemo.exe`（前端演示入口）继续使用隔离缓存。

源码开发复用项目 `.local/portable.json`（布局配置）指定的开发业务环境：Python（后端解释器）位于 `.local/runtimes/python/`，Node.js（脚本解释器）从 `.local/runtimes/dsh/runtime.json`（代理宿主运行清单）解析。开发依赖由 npm（包管理器）安装在根目录 `node_modules/`；原生构建工具保存在 `.local/tools/`（本机工具目录），构建输出统一进入 `.local/build/`（受控构建缓存）。

```powershell
.\scripts\setup.ps1 -SkipInstall -SkipDsh
npm ci
npm run desktop:start
```

普通无参数启动从可执行文件位置确定工作区，读取已发布 `app`（程序本体）内的业务和静态界面；不查找源码祖先，也不读取或更新系统用户目录中的工作区定位记录。`npm run desktop:start`（正式启动）与本地双击完全一致。`npm run dev`（源码热更新）通过同一正式入口显式传入 `--dev`（调试开关）及 `--source-root`（源码目录），沿用正式工作区、组件管理、单实例及退出流程。`npm run desktop:publish`（本地发布）使用统一程序包事务替换本体，保留用户数据与组件。

设置、历史、声纹、DSH（代理宿主）状态及凭据引用按当前工作区的 `data`（用户数据）目录加载；凭据继续使用 Windows Credential Manager（系统凭据管理器）。不要将开发凭据提交到 Git（源码管理工具）：

```text
DEEPSEEK_API_KEY=
MINIMAX_API_KEY=
```

默认入口：

- Product UI（产品界面）：通过桌面程序打开；普通浏览器访问本机地址只显示启动说明。
- Voice Server（语音服务）：使用宿主 `endpoints.voice`（语音服务实际端点），首选端口 8765 被占用时由系统分配。
- WebSocket（实时通信）：使用实际语音服务地址的 `/ws`（实时通信路径）。
- 健康检查：使用实际语音服务地址的 `/health`（健康检查路径）。
- DSH（代理执行宿主）控制台：从工作台“后台协作”面板底部的“代理控制台”进入；桌面应用在系统默认浏览器打开网页。
- 无界面后端调试：`.\scripts\start.ps1`

正式组件均为必需组件。新安装按当前程序随包清单安装；已安装但版本落后的组件显示“更新、导入、跳转”。任何组件缺失、损坏、待更新，或全量文件摘要复核未通过时，整体语音服务不会启动，程序会进入本地组件管理页。旧模型不会通过推理适配层继续运行；所有组件按当前清单校验就绪后，才允许启动或恢复此前运行的服务。`/health`（语音服务健康检查）成功仅代表服务入口可应答，不代表全部组件或模型推理已就绪。

从原生首屏开始统一显示应用图标、当前启动组件、进度条和“跳过”按钮；组件完整摘要检查通过后，ASR（自动语音识别）、分析进程和语音本地模型按实时资源预算最多同时加载三路，资源不足时减少并发。组件和服务启动门槛见[组件更新说明](docs/model-resource-updates.md)，调度细节见[启动调度梳理](docs/startup-optimization-plan.md)及[并行启动验收](docs/startup-parallel-acceptance.md)。

## 本地组件与模型设备

语音服务、模型和 DSH（代理执行宿主）由随包清单作为整体必需组件管理。更新由用户点击卡片中的更新或导入入口触发，也可打开官方来源；待更新组件会阻止整体服务启动。模型版本、文件摘要校验、事务恢复和安装验证见[程序升级与必需组件更新](docs/model-resource-updates.md)。

组件提交或下载任务结束后立即重读清单，活动任务每两秒补查、空闲每十秒刷新，并显示实际更新时间。全部组件和接口配置就绪后，点击“启动程序”立即进入统一启动动画；宿主重新读取新安装的组件路径，完成完整摘要复核后启动，无需退出重进。下载、复制、共享依赖复用及校验进度的规则和实测见[下载与首次启动修复](docs/download-startup-flow-20261010.md)。

2026-10-10 的默认实现包括 PyTorch（深度学习框架）权重内存映射加载、FunASR（语音模型框架）按需导入、声纹临时实例生命周期释放，以及 CPU（中央处理器）模式下自动选择已校验的轻量 ONNX（开放神经网络交换格式）FSMN-VAD（语音活动检测）模型。SenseVoice（语音识别与理解）量化候选未通过默认启用条件，仍使用原精度模型。各项实测和资源口径见[运行资源管理与内存优化](docs/runtime-resource-management.md)及[本地模型运行设备](docs/local-model-compute.md)。

便携桌面启动由应用宿主提供预构建界面，不在用户启动期间安装依赖或执行开发构建。正式界面地址优先使用 5173 端口，被占用时分配空闲端口，以桌面实际加载地址为准。私有进程管道提供一次性桌面入口凭据；界面资源、运行配置和宿主管理操作均需本实例会话。关闭产品窗口隐藏到托盘；从应用菜单、托盘或“设置 → 系统运行”明确退出时，回收全部受管组件。

构建桌面程序及根目录启动入口：

```powershell
.\scripts\build-desktop.ps1
```

正式程序发布与数据备份见[本地目录与覆盖发布](docs/local-layout.md)。对外交付单个 `VoiceTerminalSetup.exe`（分发安装器），内嵌同一正式程序包。使用系统 WebView2 Evergreen Runtime（自动更新网页引擎）；没有 Python（后端环境）也能由正式程序进入组件管理和关于页。组件由正式程序维护，详见[统一正式程序与分发安装器](docs/unified-product-installer.md)。

源码界面开发统一使用桌面命令：

```powershell
npm run dev       # 真实业务桌面热更新；别名 npm run desktop:dev
npm run dev:ui    # 桌面样例热更新；别名 npm run desktop:ui
npm run build    # 统一正式界面、原生宿主及根入口；别名 npm run desktop:build
npm run desktop:publish # 退出业务实例后，事务更新固定本体及正式入口
npm run desktop:dist # 独立 EXE（安装器），嵌入同一正式程序包
```

正式启动直接读取已发布本体。源码调试使用同一正式入口和工作区，显式切换业务源码与前端输入；普通模式与调试模式共享单实例，冲突时提示退出后重启。前端演示使用隔离缓存，只启动界面服务，可与正式程序同时运行。实际端点以宿主端点表为准。启动诊断见[独立应用启动与系统管理](docs/application-lifecycle.md)，入口、安装与恢复规则见[统一正式程序与分发安装器](docs/unified-product-installer.md)。

## 产品页面

- `/`：当前真实 Product UI，连接 Product API 和 Voice Server。
- `/?settings=system`：独立应用的“系统运行”设置页；管理状态来自应用宿主，语音服务失败时仍可诊断、重试和退出。
- 语音事件回放：从桌面样例工具选择固定回放场景；产品地址不再通过查询参数绕过宿主管理。
- 桌面样例：覆盖工作台和七个设置页（唤醒词与静默设置已合并）；使用固定样例，不连接业务后端、不申请麦克风权限，也不读写真实设置。

调优外观时，双击 `.local/VoiceTerminalUIDemo.exe`（前端演示入口）直接打开演示主窗口，随后在同一窗口内加载界面服务；重复双击唤回已有主窗口。该入口直接运行已构建的宿主，不执行源码构建。修改原生宿主源码后，执行 `npm run dev:ui`（构建并启动演示开发）。保存 Vue（页面组件）或 CSS（样式表）后自动更新。右下角工具可切换页面、内容场景、运行状态及浮层。展示选择保留在页面地址中；表单编辑仅保留在内存，点击“重置展示”恢复初值。关闭演示窗口立即回收其开发服务。界面修订在共享组件和样式中完成；验收后退出正式程序并关闭演示窗口，执行 `npm run desktop:publish`（更新正式程序），再启动正式入口验证真实业务。

正式界面与样例共享 `apps/frontend/src/interface/`（应用框架、工作台、设置导航和基础控件）及 `design.css`（设计变量与样式）。正式业务位于 `apps/frontend/src/product/`（产品逻辑），无后端管理界面位于 `apps/frontend/src/management/`（原生组件管理），样例位于 `apps/frontend/src/ui-demo/`（样例展示）。入口先验证原生桌面状态再动态加载对应界面；正式业务入口随后读取宿主运行配置。正式构建包含产品和原生管理入口，便携组装依据 `desktop-manifest.json`（界面产物清单）验证依赖闭包，拒绝样例和陈旧资源。当前收敛范围与验证状态见[第一阶段记录](docs/desktop-frontend-consolidation.md)及[统一启动实现](docs/unified-product-installer.md)。

Product UI 只有工作台和设置两个一级页面。页面切换应保留组件状态，真实协议和会话排序以服务端事件为准，详见 [WebSocket 协议](docs/websocket-protocol.md)。

普通对话区隐藏 `empty_input`（空输入状态）且 `text`（转写文本）为空或仅含空白字符的用户气泡，后台诊断记录和服务端顺序号仍保留。“嗯”等非空短回答正常展示；空输入记录本身不触发 LLM（大语言模型）请求。识别工作进程在完整音频分块后的空结束包上执行最终刷新，避免遗漏缓存尾字，详见[架构与生命周期](docs/architecture.md#语音端点与分析模型)。

## 主要配置

下列 `.local/data`（开发数据）路径描述开发渠道；分发渠道的对应位置为所选工作区的 `data`（用户数据）。

- `config/default.yaml`（默认配置模板）：默认运行参数、首选端口、模型、KWS（关键词唤醒）、音频和 Shadow（影子代理）配置；分发程序使用工作区 `app/config/default.yaml`（随本体发布的模板）。
- `.local/data/config/local.yaml`（本机配置覆盖）：保存当前非密钥运行参数，不提交真实凭据。
- `.local/data/persona/`（当前人格资料）：`personality.md`（人格风格）和 `user.md`（用户资料）首次激活时读取；通过设置页保存后，当前会话的后续模型请求立即使用新快照。旧 `instructions.md`（历史工作指令）仅留档，不再加载。源码 `config/templates/persona/`（人格模板）只用于补齐缺失资料。
- `config/shadow_agent/`：Shadow（影子代理）指令和工具策略。
- `config/dsh/`（代理宿主基线）：新分发资源锁定官方 DSH（代理执行宿主）`0.2.0-rc.2`（候选版）安装器；现有开发物理运行包继续匹配 commit（提交号）`639ed015397290b3745d163aafe02ffee4aa3f84`。项目插件源码为 `integrations/dsh/voice-terminal/` 下的 `@voice-terminal/dsh-plugin`（统一功能包）0.2.0，包含桥接、项目唤醒任务、浏览器和影子预设；Bridge protocol（桥接协议）保留 2.0。开发宿主位于 `.local/runtimes/dsh/`，状态存放在 `.local/data/dsh/`；分发宿主与状态分别使用工作区 `runtimes/dsh`（运行资源）及 `data/dsh`（代理数据）。官方 Automation Bundle（自动化功能包）的用户选择得到保留；原生 `schedule_*`（会话定时任务工具）绑定 DSH 会话，项目 `wake_task_*`（语音链路任务工具）保持独立存储和投递目标。维护要求见[DSH 插件维护手册](docs/dsh-plugin-maintenance.md)，分发和当前验收见[0.9.0 独立启动实现](docs/desktop-independent-implementation.md)，上游版本来源见[下载与分发规划](docs/download-distribution-plan.md)。
- `.local/data/config/product-settings.json`（本机产品设置）：包含 KWS（关键词唤醒）、静默、Shadow（影子代理）、自定义指令和声纹设置等非密钥内容。
- `.local/data/speaker/`（当前声纹资料）：保存注册资料、模板和特征指纹；详见 [声纹增强](docs/speaker-enhancement.md)。

配置模板随代码更新，已有用户设置由数据目录保存。普通覆盖发布复用模型与运行环境，不覆盖 `.local/data/`（真实用户数据）；重要数据的唯一滚动备份和显式交付导出命令见[本地目录与覆盖发布](docs/local-layout.md)。

### 运行模式

当前程序使用两个运行模式，已与界面精简、连续播放衔接、识别优化和显卡支持一起更新原正式目录；构建标识、数据核对与现场验收边界见[统一发布记录](docs/desktop-acceptance.md)。

- `fast`（快速，默认）：主 LLM（大语言模型）使用 `reasoning.effort=none`（关闭思考）。
- `deep`（深度）：主请求直接使用 `reasoning.effort=low`（低强度思考）。
- 均衡模式的实现、测试及探测脚本保存在 Git（版本控制）中的均衡模式历史，不参与当前启动和默认测试。

`llm.reasoning_mode`（运行模式配置）和 `session.reasoning_mode`（会话模式命令）仅允许 `fast`（快速）与 `deep`（深度）。升级前将显式使用 `balanced`（均衡）的本机配置改为其中一种；历史 `thinking_action_enabled`（追加思考开关）不再读取。参数及恢复说明见[思考模式说明](docs/llm-json-schema-migration.md#思考模式)。

- KWS：`wake.enabled=true` 时进入唤醒词待机；关闭后 `session.start` 直接进入 ASR。
  唤醒命中或模型主动发声把麦克风交给 ASR 后，输入所有权只在静默窗口（`silence.seconds`，默认 90s）
  到期时由 `final_silence`（静默前最终检查）系统检查交回 KWS；DSH 的 `request_early_silence`（请求提前静默）调用后立即标记 `final_pending`（最终检查待处理），不会打断用户输入或当前播报。新输入重置静默窗口时，请求会保留并在新窗口重新标记；最终检查只在输入、模型处理和实际输出都空闲后派发。窗口内接话不需要再喊唤醒词，语音结算和播放结束都不改变所有权。
- Shadow（后台执行环节）：默认开启、异步运行，与语音交互链路共同构成同一助手。实际操作、外部查询和定时请求直接交给后台执行；DSH（后台执行宿主）使用执行环境已授予的工具和权限，原生后端仍只提供只读业务工具。DSH 根执行环节另有明确授权下的提前静默工具。它不是主语音链路启动的必要条件；活跃期上下文更新按安全点合并，
  长推理每 20 秒通过 `inject` 发送一次不打断执行的收敛提醒（可由
  `shadow_agent.runtime_reminder_interval_ms` 调整）。

KWS（关键词唤醒）和声纹是独立能力。前者负责输入所有权和唤醒待机；声纹拦截模式在身份结算前暂缓流式输入派发，最终仅放行本段已确认的主人，未知及服务异常均拦截。仅标记模式继续允许普通输入；主人打断采用独立开关。详见[声纹增强](docs/speaker-enhancement.md)。

## 验证命令

在仓库根目录执行：

```powershell
.\.local\runtimes\python\python.exe -B -X utf8 scripts/test-backend.py -q
npm test
npm run typecheck
npm run build
git diff --check
```

后端测试使用正式解释器，测试入口加载 `.local/tools/testing/`（轻量测试工具）并在成功后回收临时夹具。文档修改可执行 `.local/runtimes/python/python.exe -B -X utf8 scripts/check-doc-links.py`（工程内文档链接检查）；安装摘要校验执行 `.local/Verify-Package.ps1`（完整发行校验）。需要真实设备或 provider（模型供应方）的验收时，按照 [运行与质量门槛](docs/operations-and-quality.md) 执行；本地 `.local/cache/logs/` 和 `.local/cache/reports/` 只作为诊断产物，不属于项目文档。

## 文档入口

- [当前开发与分发交付](docs/current-release.md)：两个测试入口、重建命令、过期产物清理和最近一次构建核对结果。
- [独立桌面应用、数据分离与便携构建](docs/desktop-application.md)：Tauri 2（桌面应用框架）窗口、独立预览、构建与迁移命令。
- [本地目录与覆盖发布](docs/local-layout.md)：唯一资源路径、日常更新、用户数据备份与最新交付导出。
- [本地目录整理验收](docs/local-layout-acceptance-20261008.md)：2026-10-08 迁移、清退体积、数据完整性、实机启动与失败恢复结果。
- [桌面实施验收记录](docs/desktop-acceptance.md)：实际构建、运行、退出、迁移及待补实机检查。
- [前端界面阶段性交接](docs/frontend-ui-handoff.md)：已确认的界面修订、产品接入差异及正式迁移顺序。
- [真实业务验收与统一发布](docs/desktop-acceptance.md)：已发布界面的验收证据、双模式归档、固定服务密钥与最新正式组件。
- [独立应用启动与系统管理](docs/application-lifecycle.md)
- [启动调度梳理与资源预算](docs/startup-optimization-plan.md)：当前三路模型调度、代理独立启动及历史串行路径。
- [运行资源管理与内存优化](docs/runtime-resource-management.md)：当前策略、CPU（中央处理器）/GPU（图形处理器）资源口径及内存优化验收边界。
- [本地模型运行设备](docs/local-model-compute.md)：模型实际设备、显存计量及已验证 GPU 环境规则。
- [程序升级与必需组件更新](docs/model-resource-updates.md)：当前目标版本、完整摘要启动门禁及组件更新恢复。
- [下载、状态刷新与首次启动修复](docs/download-startup-flow-20261010.md)：刷新计时、首次安装后的启动动画、有界下载与复制、完整校验及真实文件对照。
- [语音环境更新校验修复](docs/runtime-validation-20261010.md)：目录核对、并行摘要、取消响应及磁盘缓存对耗时的影响。
- [统一加载页验收](docs/startup-loading-acceptance.md)：首屏、跳过入口与本地组件状态。
- [识别按需导入验收](docs/startup-optimization-acceptance.md)：真实模型对照、兼容回退与首轮交付记录。
- [当前并行启动验收](docs/startup-parallel-acceptance.md)：95% 内存门槛、设置提前可用、退出回收与正式更新范围。
- [架构与生命周期](docs/architecture.md)
- [连续性底座：上下文衔接与定时唤醒](docs/continuity-foundation.md)
- [上下文与工具规范](docs/context-and-tools.md)
- [自定义指令条目与提示词投影](docs/context-and-tools.md#用户自定义指令)
- [WebSocket 协议](docs/websocket-protocol.md)
- [LLM 上下文缓存契约](docs/llm-context-cache-contract.md)
- [LLM JSON Schema 迁移与回滚 Runbook](docs/llm-json-schema-migration.md)
- [可选声纹增强](docs/speaker-enhancement.md)
- [环境声、严格声纹拦截与重置修复](docs/voice-reliability-fixes-20261010.md)：输出许可、整理材料构造及当前验证边界。
- [项目隔离 DSH 控制台](docs/dsh-console.md)
- [运行与质量门槛](docs/operations-and-quality.md)
- [当前已知问题](docs/known-issues.md)

`docs/llm-protocol-baseline.json` 是历史 Chat Completions 回滚基线，供显式兼容验证使用，不代表当前默认 transport。`docs/` 不再保存版本发布审计、产品草案或一次性故障调查；这些内容应在修复后直接进入 Git 历史或测试记录。
