# 项目隔离 DSH Shadow 执行端

Voice Terminal 内置第一方 DSH Bundle `voice-terminal-shadow-bridge`。当
`shadow_agent.backend: dsh` 时，项目 DSH Web Host 中的正式根 Agent承担 Shadow 任务；当前默认值
已经切换为 `dsh`，`native` 仅作为显式人工回滚后端保留。后端在新 Voice Session 创建时固定，同一 Session不热切换，也不在 DSH
故障时自动转交 native。

固定基线为 `dsh-v0.1.7-rc.2` / commit
`477b4f420553e8a52c2fbccc464d7561b239c443`。Bundle 只使用该版本的公开 Cordis、Web Host、Agent、
Session、Tool 和事件扩展点；不得修改 `.runtime/dsh/source`、解析私有 Session 文件、依赖私有
Web RPC，或启动第二个长期持有同一 Session 的 DSH 进程。

## 安装与漂移校验

```powershell
pwsh -NoProfile -File .\scripts\setup-dsh.ps1
pwsh -NoProfile -File .\scripts\setup-dsh.ps1 -ValidateOnly
```

脚本使用 UTF-8（统一字符编码）源码，须由 PowerShell 7 的 `pwsh` 运行。修改 Bridge（集成桥接）的构建输入后，先重建产物、运行 `-ValidateOnly`（仅校验模式），再重启 Voice Server（语音服务）；否则运行时会因构建指纹漂移而拒绝启动 DSH。

setup 会验证固定 source/tag/commit、Node、pnpm 和 CLI，构建 TypeScript Bridge Bundle（插件运行包）、
Wake Tasks Bundle（唤醒任务插件运行包）与正式 Shadow preset Bundle（Shadow 预设功能包），
用官方 `dsh plugin --profile web add` 安装到项目 profile，
运行 `dump-config`，并记录 Node Bundle（Node.js 插件运行包）、浏览器插件页、预设功能包的 hash（哈希值）、
协议版本、公共提示词哈希和受管配置哈希。浏览器插件构建会
使用临时 staging directory（暂存目录），结束后移除，不保留对固定 DSH source（源码）的修改。`-ValidateOnly`
会检查 Node Bundle（Node.js 插件运行包）、浏览器入口、插件页 manifest（清单）、Shadow preset 声明和安装状态；发现缺失或漂移即拒绝通过校验。

运行时 freshness check（新鲜度校验）覆盖 Bridge 的 `src/`、`package.json`、`build.mjs`、`cordis.patch.yml`、
`scripts/build-dsh-client.mjs`（浏览器构建脚本）和正式 Shadow preset（Shadow 预设）源文件；README、`test/`
目录和 source map（源码映射）变更不会阻止启动。产物 hash（哈希值）覆盖 DSH 实际加载的包清单、Cordis patch
（配置补丁）和 JavaScript 运行文件，不包含声明文件或文档。

DSH 0.1.7-rc.2 的 Agent 预设只从 Cordis Bundle（Cordis 功能包）中的 `@deepseek-ai/dsh-agent-preset`
声明加载，不再扫描 DSH_HOME 的 `.agent-presets/<id>/` 目录。安装脚本从
`config/dsh/agent-presets/shadow-agent/` 生成项目本地 preset Bundle（预设功能包），并将正式预设注册到 Web
profile；profile patch（配置补丁）使用 `agent-preset-registry`（Agent 预设注册器）的 `default`（默认预设字段）。
旧目录保留供回滚和数据核对，不再参与新版本加载。

## 打开 DSH 界面

先启动 Voice Terminal 项目服务，再打开 `http://127.0.0.1:5173/`，使用产品顶栏的 DSH 控制台入口。
该入口会向 Voice Server 请求一次性 launch URL（启动地址），并在 DSH 界面就绪后打开控制台。
DSH Web Host（网页服务）固定监听 `127.0.0.1:3081`；直接访问该端口时，必须使用受管入口生成的临时访问令牌。
不要收藏、复制给他人或写入日志带有 `?token=` 的完整地址。

若界面显示 `Failed to load plugins`（插件加载失败），并提示
`@voice-terminal/dsh-shadow-bridge: pending`（插件等待服务），先确认 DSH 设置页服务已经就绪，再执行以下校验：

```powershell
pwsh -NoProfile -File .\scripts\setup-dsh.ps1 -ValidateOnly
```

该命令确认 Node Bundle（Node.js 插件运行包）和浏览器插件页与锁定版本一致。v0.1.7-rc.2 使用 `configForms`（设置表单服务）；旧版
`settingsScope` 接口已移除。Bridge（插件桥接层）通过 `configForms.whileServed(...)`
（仅在设置服务可用时注册页面）延迟注册设置页，因此不应把 `settingsScope` 当作回退接口。

若设置侧栏已出现 `Voice Terminal`，但右侧内容为空，并且浏览器控制台报告 React error #130
（React 渲染错误：组件类型未定义），请检查 Bridge 使用的 UI icon export（界面图标导出）是否属于锁定 DSH 版本。
v0.1.7-rc.2 的图标名称已更新；当前页面使用 `IconChevronDownOutlineRegular` 和
`IconFolderCloseRegular`。修改客户端后需重新运行 `setup-dsh.ps1` 重建插件，并从 Voice Terminal 顶栏入口重新打开
DSH，以载入新产物和新的 launch token（临时访问令牌）。

## Voice Terminal 插件设置

DSH 启动后，在设置侧栏“Agent 预设”下方打开独立的 `Voice Terminal` 页面。首次启动时 Bridge（插件桥接层）会在项目根目录确保
`workspace` 文件夹存在，并把它注册为项目默认工作区。之后可为语音新会话选择 DSH 已注册的工作区，
工作区菜单使用 DSH 原生控件且不会切换当前工作区或会话。也可选择一个专用模型与该模型支持的推理强度；
模型默认设置是跟随 DSH 全局模型。设置保存在 DSH 的 settings document（设置文档）中，
更改会作用于下一次新建的语音 DSH 会话，无需重启。

插件设置使用 `voice-terminal-shadow-bridge` namespace（命名空间），持久化字段如下：

| 字段 | 中文说明 | 用途 |
| --- | --- | --- |
| `workspaceId` | 选择的工作区标识 | 指向 DSH 已注册的工作区；仅影响新语音会话的 `cwd`（会话工作目录）。 |
| `defaultWorkspaceId` | 项目默认工作区标识 | Bridge 维护的回退目标，对应项目根目录的 `workspace` 文件夹。 |
| `provider` | 模型供应方 | 与 `model` 一起指定插件专属模型；未设置时继承 DSH 全局模型。 |
| `model` | 模型标识 | 指定插件专属模型；模型不可用时回退至 DSH 全局模型。 |
| `reasoningEffort` | 推理强度 | 指定所选模型支持的强度；未设置时使用模型默认值。 |
| `fallbackNotice` | 回退提示状态 | 记录工作区、模型或推理强度回退原因，并在设置页提示。 |

工作区和模型选择只应用于 Voice Terminal 触发的新 `session.open`（会话打开请求）。手动新建的 DSH
会话、已经运行的语音会话以及恢复的语音会话都继续沿用原有工作目录和模型选择。已保存工作区失效
时回退到项目默认 `workspace`；专属模型失效时回退到当前 DSH 全局模型。Bridge 会记录回退诊断并在
该设置页显示提示。新建会话须成功挂接工作区后才会接受语音唤醒；目录创建、注册或挂接失败会拒绝
本次语音会话启动。

`setup-dsh.ps1`（DSH 插件安装脚本）也会安装独立的
`voice-terminal-wake-tasks`（语音终端唤醒任务插件）。插件只向 DSH 正式根 Agent（正式根代理）
提供创建、取消、读取冻结参考和投递语音事件的工具。插件负责项目任务的唯一持久化与调度；任意 DSH
根会话均可创建任务，任务与创建会话的生命周期无关。创建时的 DSH 文本上下文快照保存在任务记录，
默认不注入；任务模型可通过 `read_wake_reference`（读取冻结参考工具）按需读取。

任务到期后，插件经桥接层在 Voice 会话交接锁（防止旧会话被唤醒的互斥锁）内解析当前关联的 DSH
主线，并用普通 `user/message`（用户消息）投递任务。创建任务的会话不作为投递目标；如果旧主线已退役，
任务转投当前主线。暂时无法解析或投递时保留任务并重试。Voice Terminal（语音终端）主设置中的
“唤醒任务”页读取同一插件任务目录，因此能查看、修改和取消任意 DSH 会话创建的任务。

插件安装时会幂等导入 DSH 原生 `schedule.json`（原生任务文件）及旧 Voice `wake-tasks.json`
（旧任务文件）中的可转换记录，并保留任务标识、状态和可用投递历史。安装脚本在切换调度来源前运行
迁移预检；有效记录无法转换时中止安装。安装完成后，DSH 原生 `schedule`（调度器）与
`ui-schedule`（原生任务页面）会停用，以免任务被两套调度器重复触发。

DSH 模型需要通知 Voice 时，必须显式调用 `publish_voice_event`（投递语音事件工具），写入 Voice
持久信箱（可在进程重启后恢复的事件队列）。事件不要求预先存在唤醒标识或上下文版本；重复事件按
`event_id`（事件标识）去重。Voice 停止监听时，事件保留到下次手动启动处理，不会自动打开语音监听；
普通 DSH 助手正文不会自动转成语音事件。完整工具字段、规则格式和交接恢复行为见
[跨会话衔接与 DSH 定时任务](continuity-foundation.md)。

项目私有运行数据位于 `.runtime/dsh`：

- `source`：固定上游源码和构建物；
- `home`：项目 DSH_HOME、正式/retired Session、Harness 设置和凭据；
- `session-mappings.json`：不含正文、凭据或 token 的 Voice↔DSH 映射；
- `build-state.json`：不含秘密的构建与 hash 审计；
- `web.dump-config.yml`：最后一次受管配置展开结果。

## 通信与安全边界

DSH 固定监听 `127.0.0.1:3081`。Voice Server 每次启动生成独立 Bridge Bearer token，仅通过受管
子进程环境交给 Bundle；它与浏览器 launch token、DSH Cookie 和模型 API key完全分离。Bridge
路由和 Voice 内部回调都拒绝非 loopback（本机回环地址）和错误 token（访问令牌）。有会话边界的
上下文与生命周期操作继续校验 Session（会话）、epoch（会话代次）及 revision（上下文版本）；定时任务
只允许投递当前未退役主线。`publish_voice_event`（投递语音事件工具）通过桥接鉴权、事件标识去重和
退役会话校验，但不要求预先建立 Voice activation（语音激活）或匹配上下文版本。

Voice 模型仍只发送无正文的 `request_shadow({"request": true})`。Voice Terminal 是唯一权威上下文
源；Bundle 先强制注入带来源、epoch、revision 的完整 snapshot/连续 update，再唤醒根 Agent。
DSH 已经处于运行/推理状态时，后续 `request_shadow` 只同步 Voice 的最新权威上下文，不会创建第二个
activation、打断模型或取消工具。下一个 `next-step` 安全点之前，连续 delta 会按 revision 合并为一条
待处理输入并保留该窗口内的全部 frames；snapshot 会覆盖尚未 claim 的旧输入。消息已被安全点 claim
后才会进入下一个窗口，完全重复的 revision/digest 不会再次注入。上下文更新的传输超时会通过
`session.state` 核对并以同一 request ID 幂等重试，不能仅凭丢失的 HTTP 响应判定 DSH 失败。

每个 activation 从 `wake` 被接受时开始按 `shadow_agent.runtime_reminder_interval_ms` 计时，默认每
20 秒向最近的 next-step 安全边界注入一次软性收敛提醒（20、40、60 秒……）。提醒使用 `inject`，不
唤醒 idle Agent、不恢复工具前硬阻断，也不重置 activation 计时；上一条提醒尚未 claim 时原位替换，
因此安全点前最多保留一条待处理提醒。terminal、cancel、retire 或插件释放会清理计时器，不为提醒额外
开启模型步骤。
普通 DSH assistant 正文永远不进入 Voice 邮箱。正常回传只允许六个根 Agent控制工具：

- `get_latest_context`（仅同步补救）；
- `read_ambient_context`（旁路、不可信、Session 内）；
- `publish_main_event`；
- `request_user_input`；
- `return_shadow_result`；
- `finish_shadow_no_action`。

子代理可承担明确并行任务，但执行入口会拒绝子代理调用 Voice 控制工具。根 Agent统一决定澄清、
确认、观察和唯一终态。语音 confirmation 不会批准或修改 DSH Harness 权限。

## 模型、工具与可见性

正式 preset 继承固定版 `standard` 的完整默认工具组，包括文件、Shell、Web、任务、Skills、目标、
workflow 和子代理；Bridge 不调用 `tools.restrict()`。受管进程默认
`DSH_PERMISSION_MODE=read-only`，实际允许/拒绝由官方 Harness 权限面板人工控制。

DSH 全局模型、provider、reasoning 和凭据仍由 Harness UI 管理；Voice Terminal 插件设置只为
语音触发的新 DSH 会话提供工作区、可选模型与推理强度覆盖。Voice 设置还管理 Voice 主模型、
status observer 和 TTS。status observer 仍由 Voice 托管，但只能通过 Bundle 的有界
ObserverSnapshot 读取已提交事件，不接收 reasoning、流式草稿、完整敏感参数、环境变量、凭据或
子代理私有轨迹，也不回注 DSH 根 Agent。status observer 的真实摘要会在 Voice 侧生成
`ShadowEvent(kind=status, wake_policy=normal)`：它更新可替换的 `ShadowVisibleStatus`，触发一次
普通 Voice LLM 调度，并在下一次请求的 `runtime.shadow.status` 中可见；它不追加为长期语义历史，
也不会伪装成 `result` 或其他终态事件。

正式 Session 在官方 DSH UI 中显示完整 Voice 权威上下文，并标记 `Source: Voice Terminal`、epoch、
revision 和 snapshot/delta。reset 会取消旧 Turn、把旧 Session 标记 retired 并保留在 UI中，再为
新 epoch 创建全新 Session；旧 activation 的迟到事件永久拒绝。

## 故障边界

3081 被现有进程占用时 Runtime Manager拒绝附着。DSH 异常退出只做有限自动重启，达到上限后标记
unavailable；Voice 主链继续运行，并且不会为同一 Voice Session自动切到 native。首次 activation 无法
建立、显式协议拒绝，或状态核对与一次幂等重试仍无法确认且 Bridge 确实不可用时，系统才产生一次真实
`failed`。活跃 activation 的单次引导传输超时会保留当前 activation 并后台重同步；只要随后收到有效
activity/result 就恢复健康，不把瞬时超时升级为持久 `ERROR`。业务上的“没有结果”由根 Agent选择
澄清、result 或 no-action。

产品顶栏通过 Voice Server取得一次性 launch URL；不要收藏、记录或分享带 `?token=` 的地址。
Session upload、DeepSeek Session Log 和 OTel 保持禁用。固定上游许可证快照位于
`config/dsh/licenses/`。

排查提示：控制台「对话」页在启动时就显示为空（「轨迹」页仍有内容、普通会话刷新后也不恢复）
时，先看浏览器控制台是否有 `event feed subscriber failed: TypeError: Cannot read properties of
null (reading 'kind')`。该报错说明存储中存在 `turn/end` 的 `reason` 为空值的会话，按
`docs/dsh-plugin-audit-20260927.md` 的说明定位并处置；插件的写入侧修复见
`integrations/dsh/voice-terminal-shadow-bridge/README.md` 的「终端工具与回合终结」。
