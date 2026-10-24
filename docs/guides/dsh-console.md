# 项目隔离 DSH（代理执行宿主）控制台

Voice Terminal（语音终端）的项目插件使用第一方 DSH Bundle（功能包）`@voice-terminal/dsh-plugin`。`shadow_agent.backend`（后台代理后端）固定当前 DSH Session（代理会话）生命周期；同一会话不热切换，也不在 DSH 故障时自动转交 native（原生后台）。原生 DSH 定时任务和项目语音链路任务用途不同，按投递目标选择对应工具，规则见[连续性底座](../architecture/continuity-foundation.md)。

当前受管宿主目标、运行依赖和安装迁移规则见[DSH 包安装与迁移](dsh-npm-distribution.md)，插件源码、适配与构建流程见[DSH 插件维护手册](dsh-plugin-maintenance.md)。本指南说明控制台运行、权限及当前会话观察边界。

## 发行运行与源码准备

便携桌面包使用已闭合的 DSH（代理执行宿主）运行目录，附带 Node.js（脚本解释器）、物理依赖与原生运行库。发行端验证宿主版本、桥接协议及关键入口摘要，不要求 Git（源码工具）、pnpm（依赖管理工具）或源码检出；完整逐文件验证通过随包离线校验脚本执行。运行产物位于只读运行环境，用户配置与状态位于独立数据目录；应用搬迁后重建受管依赖联接。

构建、组件封装和迁移见[桌面应用说明](desktop-application.md)。下面的安装与源码漂移检查用于开发机和构建机，不是在终端设备安装依赖的步骤。

## 安装与漂移校验

```powershell
pwsh -NoProfile -File .\scripts\setup-dsh.ps1
pwsh -NoProfile -File .\scripts\setup-dsh.ps1 -ValidateOnly
```

脚本使用 UTF-8（统一字符编码）源码，须由 PowerShell 7 的 `pwsh` 运行。修改 Bridge（集成桥接）的构建输入后，先重建产物、运行 `-ValidateOnly`（仅校验模式），再重启 Voice Server（语音服务）；否则运行时会因构建指纹漂移而拒绝启动 DSH。

安装脚本会验证固定 source/tag/commit（源码、版本标签、提交号）、Node.js（解释器）、pnpm（依赖管理器）和 CLI（命令行程序），
构建一个包含桥接、任务、浏览器及正式影子预设的统一 Bundle（插件运行包），
用官方 `dsh plugin --profile web add` 安装到项目 profile，
移除旧的三个功能包登记并保留原设置，运行 `dump-config`（展开配置），记录完整运行包与构建输入的 hash（哈希值）、
协议版本、公共提示词哈希和受管配置哈希。浏览器插件构建会
使用临时 staging directory（暂存目录），结束后移除，不保留对固定 DSH source（源码）的修改。`-ValidateOnly`
会检查 Node Bundle（Node.js 插件运行包）、浏览器入口、插件页 manifest（清单）、Shadow preset 声明和安装状态；发现缺失或漂移即拒绝通过校验。

运行时 freshness check（新鲜度校验）覆盖统一插件的全部源码、预设、包清单、组件声明、构建与检查脚本、
宿主补丁、两套类型配置及 `scripts/build-dsh-client.mjs`（浏览器构建脚本）。文档和测试不参与启动检查。
产物 hash（哈希值）覆盖实际加载的包清单、Cordis patch（配置补丁）、组件声明和完整编译目录，包括 source map（源码映射）。

DSH 0.2.1-rc.2 的 Agent 预设只从 Cordis Bundle（Cordis 功能包）中的 `@deepseek-ai/dsh-agent-preset`
声明加载，不再扫描 DSH_HOME 的 `.agent-presets/<id>/` 目录。安装脚本从
`integrations/dsh/voice-terminal/preset/shadow-agent/` 生成统一功能包中的预设声明，并将正式预设注册到 Web
profile；profile patch（配置补丁）使用 `agent-preset-registry`（Agent 预设注册器）的 `default`（默认预设字段）。
旧运行数据中的唯一内容已保全到迁移导入区；旧目录验收后回收。

## 打开 DSH 界面

先从[独立应用入口](application-lifecycle.md)启动 Voice Terminal，再使用工作台“后台协作”面板底部的“代理控制台”入口。主界面网页优先使用 `http://127.0.0.1:5173/`，占用时以应用打开的实际地址为准。
该入口会向 Voice Server（语音服务）请求带访问令牌的 launch URL（启动地址），并在 DSH（代理执行宿主）界面就绪后打开控制台。主产品只支持桌面窗口，控制台统一交给系统默认浏览器打开。DSH（代理执行宿主）仍由应用管理生命周期，关闭控制台网页不会结束后台代理服务。
DSH Web Host（网页服务）固定监听 `127.0.0.1:3081`；直接访问该端口时，必须使用受管入口生成的临时访问令牌。
不要收藏、复制给他人或写入日志带有 `?token=` 的完整地址。

若界面显示 `Failed to load plugins`（插件加载失败），并提示
`@voice-terminal/dsh-plugin: pending`（插件等待服务），先确认 DSH 设置页服务已经就绪，再执行以下校验：

```powershell
pwsh -NoProfile -File .\scripts\setup-dsh.ps1 -ValidateOnly
```

该命令确认 Node Bundle（Node.js 插件运行包）和浏览器插件页与锁定版本一致。v0.2.1-rc.2 使用
`configForms`（设置表单服务）；旧版 `settingsScope`（设置作用域接口）已移除。Bridge（插件桥接层）通过 `configForms.whileServed(...)`
（仅在设置服务可用时注册页面）延迟注册设置页，因此不应把 `settingsScope` 当作回退接口。

若设置侧栏已出现 `Voice Terminal`，但右侧内容为空，并且浏览器控制台报告 React error #130
（React 渲染错误：组件类型未定义），请检查 Bridge 使用的 UI icon export（界面图标导出）是否属于锁定 DSH 版本。
v0.2.1-rc.2 的图标名称已更新；当前页面使用 `IconChevronDownOutlineRegular` 和
`IconFolderCloseRegular`。修改客户端后需重新运行 `setup-dsh.ps1` 重建插件，并从 Voice Terminal 工作台的“代理控制台”入口重新打开
DSH，以载入新产物和新的 launch token（临时访问令牌）。

## Voice Terminal 插件设置

DSH 启动后，在设置侧栏“Agent 预设”下方打开独立的 `Voice Terminal` 页面。Bridge（插件桥接层）优先复用已保存且有效的工作区，
不会额外登记运行目录下的空工作区。没有有效选择时，便携程序在 `VOICE_DATA_DIR`（用户数据目录）下确保
`workspace`（工作目录）存在；源码与正式业务统一使用 `.local/data/workspace`（持久工作区），不再按程序版本生成工作区登记。之后可为语音新会话选择 DSH 已注册的工作区，
工作区菜单使用 DSH 原生控件且不会切换当前工作区或会话。也可选择一个专用模型与该模型支持的推理强度；
模型默认设置是跟随 DSH 全局模型。设置保存在 DSH 的 settings document（设置文档）中，
更改会作用于下一次新建的语音 DSH 会话，无需重启。

插件设置使用 `voice-terminal-shadow-bridge` namespace（命名空间），持久化字段如下：

| 字段 | 中文说明 | 用途 |
| --- | --- | --- |
| `workspaceId` | 选择的工作区标识 | 指向 DSH 已注册的工作区；仅影响新语音会话的 `cwd`（会话工作目录）。 |
| `defaultWorkspaceId` | 项目默认工作区标识 | Bridge 维护的有效默认登记；需要创建备用目录时，便携程序使用稳定用户数据目录中的 `workspace`（工作目录）。 |
| `provider` | 模型供应方 | 与 `model` 一起指定插件专属模型；未设置时继承 DSH 全局模型。 |
| `model` | 模型标识 | 指定插件专属模型；模型不可用时回退至 DSH 全局模型。 |
| `reasoningEffort` | 推理强度 | 指定所选模型支持的强度；未设置时使用模型默认值。 |
| `fallbackNotice` | 回退提示状态 | 记录工作区、模型或推理强度回退原因，并在设置页提示。 |

工作区和模型选择只应用于 Voice Terminal 触发的新 `session.open`（会话打开请求）。手动新建的 DSH
会话、已经运行的语音会话以及恢复的语音会话都继续沿用原有工作目录和模型选择。已保存工作区失效
时回退到项目默认 `workspace`；专属模型失效时回退到当前 DSH 全局模型。Bridge 会记录回退诊断并在
该设置页显示提示。新建会话须成功挂接工作区后才会接受语音唤醒；目录创建、注册或挂接失败会拒绝
本次语音会话启动。

普通停止／开始、程序重启、模型组件重新初始化和 DSH（后台执行宿主）恢复均复用当前 Voice（语音）与 DSH（后台代理）绑定。
恢复先查询 `session.state`（当前绑定状态）：已有活动绑定直接接回；宿主重启丢失内存绑定时使用
`session.resume`（恢复持久会话），不会对已有身份再次调用 `session.open`（创建会话）。关闭语音组件只取消执行并保留绑定；
手动重置或双方共同静默一小时交接成功后才退役旧绑定并轮换两侧身份。恢复操作不再次发出历史 `wake`（任务激活）请求。

`setup-dsh.ps1`（DSH 插件安装脚本）也会安装独立的
`voice-terminal-wake-tasks`（语音终端唤醒任务插件）。任意 DSH 根会话均可使用 `wake_task_create/list/update/delete`
（创建、列出、更新、删除语音链路任务工具），或用 `schedule_wake`（创建简易唤醒任务）和
`cancel_wake`（取消简易唤醒任务）管理项目任务。原生 DSH 的 `schedule_create/list/update/delete`
（创建、列出、更新、删除会话任务工具）则由官方 Automation Bundle（自动化功能包）提供，名称与项目工具不同。
项目任务与创建会话的生命周期无关。创建时的 DSH 文本上下文快照保存在项目任务记录中，默认不注入；
任务模型可通过 `read_wake_reference`（读取冻结参考工具）按需读取。

项目任务到期后，插件经桥接层在 Voice 会话交接锁（防止旧会话被唤醒的互斥锁）内解析当前关联的 DSH
主线，并用普通 `user/message`（用户消息）投递任务。创建任务的会话不作为投递目标；如果旧主线已退役，
任务转投当前主线。暂时无法解析或投递时保留任务并重试。Voice Terminal（语音终端）主设置中的
“唤醒任务”页只读取项目任务目录。原生 DSH Schedule（会话定时任务）保存在官方调度存储并投递到所属会话，
由 Automation Bundle（自动化功能包）的原生任务页管理；它不会经过 Voice 会话交接锁或 Voice 持久信箱。

Wake Tasks（唤醒任务插件）只把旧 Voice `wake-tasks.json`（旧语音任务文件）中的可转换记录迁入项目任务目录；
已有项目任务及历史保持不变。它不读取或导入原生 Schedule（会话定时任务），两套调度器不会因为启用而重复
投递同一任务。官方 `@deepseek-ai/dsh-experimental-schedule-bundle`（原生调度自动化功能包）默认关闭，用户可按需
启用；启动和构建会保留该选择。

旧 profile（配置档案）或 `--patch`（配置补丁）中直接针对根级 `schedule` / `ui-schedule`（原生调度服务／任务页）
的禁用行，在默认未挂载这些条目的组合中可能产生 `patch: entry <id> not found`（补丁目标缺失警告）。需要切换
官方功能时，请通过 Automation Bundle（自动化功能包）管理；无需为了项目 Wake Tasks（唤醒任务插件）关闭它。

DSH 模型需要通知 Voice 时，必须显式调用 `publish_voice_event`（投递语音事件工具），写入 Voice
持久信箱（可在进程重启后恢复的事件队列）。事件不要求预先存在唤醒标识或上下文版本；重复事件按
`event_id`（事件标识）去重，省略该字段时插件按 DSH 会话标识和 `callId`（工具调用标识）生成稳定标识。
Voice 停止监听时，事件保留到下次手动启动处理，不会自动打开语音监听；
普通 DSH 助手正文不会自动转成语音事件。完整工具字段、规则格式和交接恢复行为见
[跨会话衔接与 DSH 定时任务](../architecture/continuity-foundation.md)。

项目私有运行数据位于 `.local/build/dsh`：

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

Voice 模型向后台仍只发送无正文的 `request_shadow({"request": true})`（固定后台唤醒）；本地 `continue_listening`（继续倾听）不发送到后台。Voice Terminal 是唯一权威上下文
源；Bundle 先强制注入带来源、epoch、revision 的完整 snapshot/连续 update，再唤醒根 Agent。
DSH 已经处于运行/推理状态时，后续 `request_shadow` 只同步 Voice 的最新权威上下文，不会创建第二个
activation、打断模型或取消工具。下一个 `next-step` 安全点之前，连续 delta 会按 revision 合并为一条
待处理输入并保留该窗口内的全部 frames；snapshot 会覆盖尚未 claim 的旧输入。消息已被安全点 claim
后才会进入下一个窗口，完全重复的 revision/digest 不会再次注入。上下文更新的传输超时会通过
`session.state` 核对并以同一 request ID 幂等重试，不能仅凭丢失的 HTTP 响应判定 DSH 失败。

实验性 `experimental_context_gate_enabled`（上下文门禁开关）关闭时完全沿用上述直通行为。开启后，
运行中后续 `request_shadow` 的新正文仍先同步到 DSH 插件，侧载模型只判断何时将其排入模型消息。
有意义的正文合并到当前步骤；暂缓的正文在当前激活的唯一终态后，以新 `activation_id`（激活标识）
作为下一回合消息交付，并标记延迟来源。同一区间可在每次新请求时重新判断，不限制暂缓次数。
插件的 `context.snapshot/update`（上下文快照／更新）仅在显式携带
`guidance_delivery: "hold"`（仅同步暂存）时停止立即注入；字段缺省仍使用原路径。
`context.guidance`（引导决策操作）按激活与版本幂等放行或暂缓；`session.state`（会话状态）
报告 `queued_revision`（已排队版本）、`claimed_revision`（已领取版本）与
`deferred_revision`（暂缓版本），用于传输重试核对。结构化 `dsh.context_gate` 日志记录
判断、版本区间、队列目标、终态交接和失败放行，不包含权威正文或访问凭据。

## 模型、工具与可见性

正式 preset 继承固定版 `standard` 的完整默认工具组，包括文件、Shell、Web、任务、Skills、目标、
workflow 和子代理；Bridge 不调用 `tools.restrict()`。受管进程默认
`DSH_PERMISSION_MODE=read-only`，实际允许/拒绝由官方 Harness 权限面板人工控制。

DSH 全局模型、provider、reasoning 和凭据仍由 Harness UI 管理；Voice Terminal 插件设置只为
语音触发的新 DSH 会话提供工作区、可选模型与推理强度覆盖。Voice 设置还管理 Voice 主模型、
status observer 和 TTS。status observer 仍由 Voice 托管，但只能通过 Bundle 的有界
ObserverSnapshot 读取已提交事件，不接收 reasoning、流式草稿、完整敏感参数、环境变量、凭据或
子代理私有轨迹，也不回注 DSH 根 Agent。status observer 的真实摘要会在 Voice 侧生成
`ShadowEvent(kind=information, source=observer)`（影子信息，来源为状态观察器）：它与根 Agent 信息共用单槽，
触发一次可合并的 Voice LLM 调度，只在该 logical request（逻辑请求）的 `runtime.shadow.information`
中可见；它不追加为长期语义历史，也不会伪装成 `result` 或其他终态事件。

正式 Session 在官方 DSH UI 中显示完整 Voice 权威上下文，并标记 `Source: Voice Terminal`、epoch、
revision 和 snapshot/delta。reset 会取消旧 Turn、把旧 Session 标记 retired 并保留在 UI中，再为
新 epoch 创建全新 Session；旧 activation 的迟到事件永久拒绝。

## 故障边界

应用模式下，DSH 是语音服务管理器拥有的委托组件。应用宿主在语音服务开始监听后请求启动 DSH，汇总状态并统一调度退出；关闭网页或 DSH 控制台页面不会结束应用。托盘和“系统运行”页退出同一应用实例，语音服务收到命名事件后通过私有标准输入管道触发 DSH 原有正常清理流程。应用进入退出状态后禁止自动重启。

“系统运行”页可手动恢复失败的 DSH；恢复会重置自动重启计数，并验证桥接与关联会话。恢复后的语音服务保持待机，不自动重播语音或重新执行旧模型动作。独立后端调试继续沿用语音服务原来的启动与关闭链路。

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
null (reading 'kind')`。该报错说明存储中存在 `turn/end` 的 `reason` 为空值的会话，先运行
`node scripts/dsh-diagnose/find-null-kind-rows.mjs` 检查。受影响会话应先备份，再使用
`node scripts/dsh-diagnose/purge-poisoned-sessions.mjs` 从活动目录移出并重新建立 Voice（语音）绑定，
不要直接改写 DSH JSONL（逐行 JSON 日志）。插件通过 `exec.concludeTurn`（结束当前回合）在产生端
处理终端工具回合，细节见
[插件维护手册](dsh-plugin-maintenance.md)的「必须保持的兼容契约」。
