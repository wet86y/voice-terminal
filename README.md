# Voice Terminal

Voice Terminal 是 Windows（操作系统）本机实时语音会话应用。Tauri（桌面框架）窗口是主产品唯一的运行、开发和界面验收入口，负责托盘、单实例及服务管理进程；应用宿主负责业务组件的启动、监督和退出。Voice Server（语音服务）负责音频采集、KWS（关键词唤醒）、VAD（语音活动检测）、ASR（自动语音识别）、主 Agent（智能体）、Shadow（影子代理）、TTS（语音合成）和播放。DSH（代理执行宿主）作为内部组件受管，其独立管理控制台继续由系统默认浏览器打开。

当前代码包版本为 `0.7.0`。默认 LLM 路径是 DeepSeek Responses + strict `json_schema`；`voice_decision_v06` 只是冻结的协议兼容标识，不是发布版本号。

> 安全边界：默认服务只绑定 `127.0.0.1`。当前管理 API、WebSocket 和模型探测接口不适合直接暴露到局域网或公网；需要远程部署时，先完成 [当前已知问题](docs/known-issues.md) 中的认证和 SSRF 防护工作。

## 运行结构

```text
用户 → ASR → 主 Agent/LLM → Speech transaction → TTS / Player
                         └→ request_shadow({"request": true})
                                      │
                                      ▼
                         单一 ShadowRuntime → READ_ONLY tools
                                      │
                                      ├─ status observer → ShadowVisibleStatus → 主 LLM runtime.shadow.status
                                      └─ observation / input / result / failed → 主 LLM 400ms mailbox
```

主语音链只有一个调度循环和一个语音事务 owner。未播放的 assistant 草稿不会写入历史；只有用户输入、实际传达的 assistant 文本、有效 refinement 和有语义影响的打断会进入会话账本。Shadow 是同一 session 的认知旁路；DSH 根代理仅可在用户明确要求或已启用的影子自定义指令明确规定时，请求提前执行静默前最终检查，其余语音、TTS、播放器和主调度控制仍由 Voice 拥有。status observer 摘要属于可替换的运行状态：会被 Voice 主 LLM 消费，但不追加为长期语义历史；progress 仍只更新产品 UI，不触发主 LLM。

## 快速开始

本机只保留两个使用入口：`.runtime/delivery/VoiceTerminal/VoiceTerminal.exe`（正式程序）和 `.runtime/delivery/VoiceTerminalUIDemo.exe`（前端演示程序）。正式程序沿用原有完整用户数据、设置、凭据和历史记录；演示程序只加载前端，不启动业务后端。构建和已验证范围见[桌面开发与分发说明](docs/desktop-application.md)及[实施验收记录](docs/desktop-acceptance.md)。

以下为源码开发环境要求：Windows x64（64 位操作系统）、Python（后端运行环境）3.12、Node.js（脚本运行环境）22.19 或更高兼容版本（项目隔离 DSH 的最低要求）。

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
npm ci
npm run desktop:start
```

正式程序及源码业务调试默认共用正式安装中 `portable.json`（便携配置）指定的完整数据，本机为 `.runtime/delivery/VoiceTerminal/data`（正式用户数据）。凭据沿用其中的 `.env.local`（本机环境文件）及已有凭据存储；明确指定数据目录时使用该目录的设置。也可通过进程环境提供凭据，不要提交到 Git（源码管理工具）：

```text
DEEPSEEK_API_KEY=
MINIMAX_API_KEY=
```

默认入口：

- Product UI（产品界面）：通过桌面程序打开；普通浏览器访问本机地址只显示启动说明。
- Voice Server（语音服务）：`http://127.0.0.1:8765/`
- WebSocket（实时通信）：`ws://127.0.0.1:8765/ws`
- 健康检查：`http://127.0.0.1:8765/health`
- DSH（代理执行宿主）控制台：从 Product UI（产品界面）顶栏的“代理控制台”进入；桌面应用在系统默认浏览器打开网页
- 无界面后端调试：`.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe`

启动结果以应用宿主的组件状态和核心能力为准：所有启用组件结束启动尝试后进入正常、降级或诊断状态。可选组件失败允许降级进入工作台；只看到 `/health`（语音服务健康检查）返回成功不代表整套应用已经准备好。启动完成后保持待机，由用户开始语音监听。

便携桌面启动由应用宿主提供预构建界面，不在用户启动期间安装依赖或执行开发构建。正式界面地址优先使用 5173 端口，被占用时分配空闲端口，以桌面实际加载地址为准。私有进程管道提供一次性桌面入口凭据；界面资源、运行配置和宿主管理操作均需本实例会话。关闭产品窗口隐藏到托盘；从应用菜单、托盘或“设置 → 系统运行”明确退出时，回收全部受管组件。

构建桌面程序及根目录启动入口：

```powershell
.\scripts\build-desktop.ps1
```

完整便携组装、独立运行环境构建、旧数据导入及离线校验见[桌面开发与分发说明](docs/desktop-application.md)。发布文件与用户数据分离，根入口根据便携配置及本地启用记录选择组件；终端启动不执行依赖安装。

源码界面开发统一使用桌面命令：

```powershell
npm run dev       # 真实业务桌面热更新；别名 npm run desktop:dev
npm run dev:ui    # 桌面样例热更新；别名 npm run desktop:ui
npm run build    # 正式界面、原生主程序及根启动程序；别名 npm run desktop:build
npm run desktop:publish # 退出正式程序并关闭演示后，构建并更新原正式目录，同时同步演示入口
```

源码开发命令启动前执行增量原生构建，静态开发命令同时构建当前界面；双击正式或演示入口直接打开已构建的主程序。真实开发端口固定 5173，演示固定 5174；冲突时明确失败，不连接未知服务。真实开发默认复用正式安装的完整数据和运行组件，不再自动生成空白数据产品；与正式程序共享业务资源，启动前应退出正式实例。渲染缓存按模式分开。演示只使用独立渲染缓存，不需要 Python（后端运行时）、模型或业务凭据，可以与正式程序同时运行。浏览器预览和旧网页启动器已撤销。启动诊断见[独立应用启动与系统管理](docs/application-lifecycle.md)。

## 产品页面

- `/`：当前真实 Product UI，连接 Product API 和 Voice Server。
- `/?settings=system`：独立应用的“系统运行”设置页；管理状态来自应用宿主，语音服务失败时仍可诊断、重试和退出。
- 语音事件回放：从桌面样例工具选择固定回放场景；产品地址不再通过查询参数绕过宿主管理。
- 桌面样例：覆盖工作台和七个设置页（唤醒词与静默设置已合并）；使用固定样例，不连接业务后端、不申请麦克风权限，也不读写真实设置。

调优外观时，双击 `.runtime/delivery/VoiceTerminalUIDemo.exe`（前端演示入口）直接打开演示主窗口，随后在同一窗口内加载界面服务；重复双击唤回已有主窗口。双击启动不执行源码构建，不另开过渡窗口。修改原生宿主源码后，执行 `npm run dev:ui`（构建并启动演示开发）或 `npm run desktop:publish`（构建并更新两入口）。保存 Vue（页面组件）或 CSS（样式表）后自动更新。右下角工具可切换页面、内容场景、运行状态及浮层。展示选择保留在页面地址中；表单编辑仅保留在内存，点击“重置展示”恢复初值。关闭演示窗口立即回收其开发服务。界面修订在共享组件和样式中完成；验收后退出正式程序并关闭演示窗口，执行 `npm run desktop:publish`（更新正式程序），再启动正式入口验证真实业务。

正式界面与样例共享 `apps/frontend/src/interface/`（应用框架、工作台、设置导航和基础控件）及 `design.css`（设计变量与样式）。正式业务位于 `apps/frontend/src/product/`（产品逻辑），样例位于 `apps/frontend/src/ui-demo/`（样例展示）。两套入口先验证原生桌面状态再动态加载对应界面；正式入口随后读取宿主运行配置。正式构建只包含产品入口，便携组装依据 `desktop-manifest.json`（界面产物清单）验证依赖闭包，拒绝样例和陈旧资源。当前收敛范围与验证状态见[第一阶段记录](docs/desktop-frontend-consolidation.md)。

Product UI 只有工作台和设置两个一级页面。页面切换应保留组件状态，真实协议和会话排序以服务端事件为准，详见 [WebSocket 协议](docs/websocket-protocol.md)。

普通对话区隐藏 `empty_input`（空输入状态）且 `text`（转写文本）为空或仅含空白字符的用户气泡，后台诊断记录和服务端顺序号仍保留。“嗯”等非空短回答正常展示；空输入记录本身不触发 LLM（大语言模型）请求。识别工作进程在完整音频分块后的空结束包上执行最终刷新，避免遗漏缓存尾字，详见[架构与生命周期](docs/architecture.md#语音端点与分析模型)。

## 主要配置

- `config/default.yaml`：默认运行参数、端口、模型、KWS（关键词唤醒）、音频和 Shadow（影子代理）配置。
- `config/local.yaml`：本机非密钥覆盖，不提交真实凭据。
- `config/persona/`：`personality.md`（人格风格）和 `user.md`（用户资料）在会话初始化时读取并冻结；旧 `instructions.md`（历史工作指令）仅留档，不再加载。
- `config/shadow_agent/`：Shadow（影子代理）指令和工具策略。
- `config/dsh/`：项目隔离 DSH（代理执行宿主）锁定至 npm `latest`（默认发行通道）的 `dsh-v0.2.0-rc.2`（候选版），commit（提交号）`639ed015397290b3745d163aafe02ffee4aa3f84`；当前便携应用位于 `apps/desktop-20261005-window-first`（正式程序构建目录）。Bridge（桥接插件）、Wake Tasks（唤醒任务插件）和 Shadow preset（影子代理预设功能包）为 0.1.1，Bridge protocol（桥接协议）为 2.0。官方 Automation Bundle（自动化功能包）默认关闭，但用户可按需启用；原生 `schedule_*`（会话定时任务工具）绑定 DSH 会话，项目 `wake_task_*`（语音链路任务工具）使用独立项目存储并投递到当前 Voice（语音）关联会话。首次部署记录见[本机验收记录](.runtime/dsh-upgrade-20261005/acceptance.md)，共存修复已完成并通过[本机验收](.runtime/dsh-coexist-20261005/acceptance.md)；修订细节见[任务共存修订记录](docs/dsh-task-coexistence.md)，安装和运行说明见[项目隔离 DSH 控制台](docs/dsh-console.md)。
- `config/product-settings.json`：本机产品设置，包含 KWS（关键词唤醒）、静默、Shadow（影子代理）、自定义指令和声纹设置等非密钥内容。
- `config/speaker/`：声纹资料、模板和特征指纹；详见 [声纹增强](docs/speaker-enhancement.md)。

### 运行模式

- `fast`：主 LLM 使用 `reasoning.effort=none`。
- `balanced`：基础请求不思考，必要时通过本地 `request_thinking` 触发一次低思考续请求。
- `deep`：主请求直接使用低思考。
- KWS：`wake.enabled=true` 时进入唤醒词待机；关闭后 `session.start` 直接进入 ASR。
  唤醒命中或模型主动发声把麦克风交给 ASR 后，输入所有权只在静默窗口（`silence.seconds`，默认 90s）
  到期时由 `final_silence` 系统检查交回 KWS；DSH 的 `request_early_silence` 可在输入与播报安静后提前执行同一检查。窗口内接话不需要再喊唤醒词，语音结算和播放结束都不改变所有权。
- Shadow（影子代理）：默认开启、异步运行；文件与网络操作受只读权限约束，DSH（影子代理宿主）根代理另有明确授权下的提前静默工具。它不是主语音链路启动的必要条件；活跃期上下文更新按安全点合并，
  长推理每 20 秒通过 `inject` 发送一次不打断执行的收敛提醒（可由
  `shadow_agent.runtime_reminder_interval_ms` 调整）。

KWS 和声纹是独立能力。KWS 负责输入所有权和唤醒待机，声纹负责 ASR final 后的可选路由增强；任一旁路发生故障，都不能默默改变另一条链路的语义。

## 验证命令

在仓库根目录执行：

```powershell
python -m pytest -q
npm test
npm run typecheck
npm run build
git diff --check
```

需要真实设备或 provider 的验收时，按照 [运行与质量门槛](docs/operations-and-quality.md) 执行；本地 `logs/` 和 `reports/` 只作为诊断产物，不属于项目文档。

## 文档入口

- [独立桌面应用、数据分离与便携构建](docs/desktop-application.md)：Tauri 2（桌面应用框架）窗口、独立预览、构建与迁移命令。
- [桌面实施验收记录](docs/desktop-acceptance.md)：实际构建、运行、退出、迁移及待补实机检查。
- [独立应用启动与系统管理](docs/application-lifecycle.md)
- [架构与生命周期](docs/architecture.md)
- [连续性底座：上下文衔接与定时唤醒](docs/continuity-foundation.md)
- [上下文与工具规范](docs/context-and-tools.md)
- [自定义指令条目与提示词投影](docs/context-and-tools.md#用户自定义指令)
- [WebSocket 协议](docs/websocket-protocol.md)
- [LLM 上下文缓存契约](docs/llm-context-cache-contract.md)
- [LLM JSON Schema 迁移与回滚 Runbook](docs/llm-json-schema-migration.md)
- [可选声纹增强](docs/speaker-enhancement.md)
- [项目隔离 DSH 控制台](docs/dsh-console.md)
- [运行与质量门槛](docs/operations-and-quality.md)
- [当前已知问题](docs/known-issues.md)

`docs/llm-protocol-baseline.json` 是历史 Chat Completions 回滚基线，供显式兼容验证使用，不代表当前默认 transport。`docs/` 不再保存版本发布审计、产品草案或一次性故障调查；这些内容应在修复后直接进入 Git 历史或测试记录。
