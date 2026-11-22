# 代理调试模式与调用入口

后续开发、维护、排障和验收应尽可能优先使用本项目专用调试接口。界面操作从快照发现控件，业务检查走接口目录和正式分发入口。Codex（开发代理）电脑控制仅在专用接口确实无法覆盖所需操作时作为补充；页面代次、产品权限、参数校验及已配对设备边界继续生效。

本文是调试入口的独立使用文档，统一维护运行模式、授权与启停、调用工具、标准流协议、界面与业务操作及常见错误。实测数据与交付结果另存[验收记录](../testing/agent-debug-20261120.md)。

开发新界面、设置或接口时，先看[新功能默认接入方式](#新功能默认接入方式)，按原业务路径开发即可获得通用调试能力。

## 模式与入口职责

普通模式用于日常运行；代理调试模式使用同一正式程序、工作区、组件和持久配置，显式开放界面观察、控件操作、业务调用、事件订阅及介入能力。进入代理调试模式仍须允许外部调试，所有业务沿用正式权限与参数校验。

| 入口 | 用途与前提 |
| --- | --- |
| `VoiceTerminal.exe --agent-debug`（代理调试启动） | 启动正式宿主并进入代理调试模式。已有普通实例时先正常退出，再以此入口启动。 |
| `VoiceTerminal.exe --agent-stdio`（标准输入输出连接） | 连接同一工作区中已运行的宿主，逐行收发调试消息；本身不启动宿主或切换运行模式。 |
| `npm run agent:call --`（轻量调用工具） | 在源码工作区封装标准流连接，处理请求编号、超时、回执与截图保存；先通过正式入口启动所需模式。 |
| `VoiceTerminal.exe --exit`（正常退出） | 请求同一工作区宿主正常退出，并等待业务进程结束及工作区释放。 |

`npm run dev`（源码热更新）用于源码开发，不代替代理调试模式的显式启动。进入模式后用 `instance.status`（实例状态）检查 `mode`（运行模式）：`agent-debug`（代理调试）或 `normal`（普通运行）。

## 进入和退出调试模式

在「设置 → 关于」页面底部开启“允许外部调试”滑块。普通模式仅提供实例状态和正常退出，完整接口需要显式进入调试模式。在仓库根目录执行：

```powershell
$entry = (Resolve-Path .\.local\VoiceTerminal.exe).Path
$stop = Start-Process -FilePath $entry -ArgumentList '--exit' -WindowStyle Hidden -PassThru
$stop.WaitForExit()
if ($stop.ExitCode) { throw '正式程序退出未完成。' }
$start = Start-Process -FilePath $entry -ArgumentList '--agent-debug' -WindowStyle Hidden -PassThru
$start.WaitForExit()
if ($start.ExitCode) { throw '调试窗口未就绪。' }
npm run agent:call -- --method instance.status
```

`--exit`（正常退出）等待宿主退出及工作区释放；`--agent-debug`（代理调试启动）等待窗口启动交接，组件就绪需另查应用状态。关闭主窗口通常只隐藏到托盘。结束调试时用同一退出命令，再以不带参数的正式入口启动普通模式：

```powershell
$stop = Start-Process -FilePath $entry -ArgumentList '--exit' -WindowStyle Hidden -PassThru
$stop.WaitForExit()
if ($stop.ExitCode) { throw '正常退出未完成。' }
Start-Process -FilePath $entry -WindowStyle Hidden
```

已授权的 SSH（安全外壳协议）可在目标机直接运行同一入口；远程启动要求同账户有活动交互桌面。业务应用于当前工作区或产品当前连接的已配对设备。工具不会自动授予权限、启动宿主或切换目标。

## 轻量调用工具

`npm run agent:call --`（正式代理调试调用）执行 `scripts/agent-call.mjs`（轻量客户端）。工具位于源码工作区，不随正式用户程序包分发；复用工作区已有 Node（脚本解释器），无需额外依赖。也可直接运行 `node scripts/agent-call.mjs`（脚本解释器执行），便于获取纯结构化标准输出。

```powershell
npm run agent:call -- --method ui.snapshot
npm run agent:call -- --method ui.snapshot --params '{"window":"main"}'
npm run agent:call -- --method module.list --output .local/cache/reports/agent-debug/modules.json
npm run agent:call -- --method ui.snapshot --params '{"window":"main"}' --screenshot .local/cache/reports/agent-debug/main.jpg --output .local/cache/reports/agent-debug/main.json
```

| 选项 | 中文说明 |
| --- | --- |
| `--method`（方法名） | 必填，使用正式方法。 |
| `--params`（参数对象） | 直接传入 JSON（结构化数据）对象，默认空对象。 |
| `--params-file`（参数文件） | 从 UTF-8（统一字符编码）文件读取复杂参数对象，支持文件开头的编码标记；与直接参数互斥。文件不含外层请求。 |
| `--timeout-ms`（等待毫秒数） | 正整数，默认 30000；超时结束工具的转发进程，已提交业务可能继续运行。 |
| `--entry`（正式入口路径） | 默认当前仓库 `.local/VoiceTerminal.exe`（正式入口）。 |
| `--output`（原始回执文件） | 保存完整回执，包括错误和图片编码；指定后终端只显示保存位置。 |
| `--screenshot`（截图文件） | 仅用于界面快照，自动请求截图，默认主窗口，保存原始 JPEG（图片格式）。终端始终省略图片编码。 |
| `--help`（帮助） | 显示参数及退出码。 |

退出码为 `0`（完整成功）、`2`（参数、连接或文件错误）、`3`（正式接口错误或未返回所需截图）、`4`（等待超时）。输出目录按需创建，同名文件会覆盖。截图及验收回执放在 `.local/cache/reports`（受管本机报告目录），不要提交图片编码或凭据。

复杂参数文件示例：

```powershell
$report = '.local/cache/reports/agent-debug'
New-Item -ItemType Directory -Force -Path $report | Out-Null
@{ surface='service'; operation='GET /api/v1/application/snapshot'; arguments=@{} } |
  ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 "$report/application-params.json"
npm run agent:call -- --method module.call --params-file "$report/application-params.json" --timeout-ms 45000 --output "$report/application.json"
```

`surface`（调用面）、`operation`（目录操作名）、`arguments`（业务参数对象）由正式分发解释。先读当前目录再调用，不把示例当成所有窗口和设备都具备的权限。

## 单次管道和结束语义

`--agent-stdio`（标准输入输出调试入口）逐行传递 UTF-8（统一字符编码）的 JSON-RPC（结构化调用协议）消息。`jsonrpc`（协议版本）固定 `2.0`；`id`（请求编号）使用整数或字符串；`method`（方法名）、`params`（方法参数）描述操作。返回 `result`（结果）或 `error`（错误对象，含 `code` 错误码、`message` 错误说明）。必须按编号匹配响应，不依赖返回顺序。

```powershell
# Windows PowerShell 5.1（系统脚本环境）发送中文时显式使用统一字符编码。
$OutputEncoding = [Text.UTF8Encoding]::new($false)
'{"jsonrpc":"2.0","id":1,"method":"ui.snapshot","params":{"window":"main"}}' | & $entry --agent-stdio | Out-String
@(
  '{"jsonrpc":"2.0","id":"view","method":"ui.snapshot","params":{"window":"main"}}'
  '{"jsonrpc":"2.0","id":"catalog","method":"module.list"}'
  '{"jsonrpc":"2.0","id":3,"method":"instance.status"}'
) | & $entry --agent-stdio | Set-Content -Encoding utf8 .local/cache/reports/agent-debug/batch.jsonl
if ($LASTEXITCODE) { throw '调试传输未完整结束，请查询业务状态。' }
```

`Out-String`（收集文本输出）或文件输出使 PowerShell（脚本环境）等待图形程序入口退出。标准输入结束表示不再追加请求：转发端发送内部 `transport.finish`（输入结束通知），宿主释放该连接的介入等待，完成已接受请求，写出全部回执，再发送内部 `transport.finished`（回执排空确认）。转发端收到确认才成功结束，终端不输出内部消息。单次查询无需人为延时或保持输入开放。

真实断连及授权撤销会取消仍在等待的调试请求并释放介入，不等同于输入正常结束；已提交业务未必能取消。缺少排空确认、写出失败或提前断连以失败结束，不将部分回执视作完整成功。每连接最多同时等待 16 个异步请求，超出者收到对应编号错误；回执队列有界等待，不因瞬时队列满而丢失。单请求调试等待上限 150 秒，单帧写出等待上限 10 秒；超时不代表业务已回滚。

## 连续调用与事件

订阅和介入在同一持续连接内完成。`AgentClient`（代理调试客户端）的 `request`（发送请求）返回完整回执，调用者检查业务错误；`notification`（通知事件）承载事件；`close`（结束输入并等待排空）可在请求仍等待时调用。在仓库根目录运行：

```powershell
@'
import { AgentClient } from './scripts/agent-call.mjs'
const client = new AgentClient({ timeoutMs: 45000 }) // timeoutMs（等待毫秒数）。
client.on('notification', event => console.log(JSON.stringify(event)))
try {
  const subscription = await client.request('trace.subscribe')
  if (subscription.error) throw new Error(subscription.error.message)
  const replies = await Promise.all([
    client.request('ui.snapshot', { window: 'main' }),
    client.request('module.list'),
  ])
  for (const reply of replies) {
    if (reply.error) throw new Error(reply.error.message)
    console.log(JSON.stringify(reply))
  }
} finally { await client.close() }
'@ | node --input-type=module
```

独立订阅在操作及观察期间保留客户端，最后关闭；关闭输入会结束订阅。`trace.lagged`（订阅积压提示）的 `count`（遗漏事件数）表示缓冲溢出，应重新查询状态；不能把缺少事件解释为业务没有执行。

## 正式方法目录

| 方法 | 参数与结果 |
| --- | --- |
| `instance.status`（实例状态） | 返回 `mode`（运行模式）、`pid`（桌面进程号）、`stopping`（正在退出）、`window_ready`（窗口就绪）。 |
| `ui.snapshot`（界面快照） | 省略 `window`（窗口编号）列出窗口，指定窗口返回 `controls`（控件列表）及 `revision`（页面代次）。控件含 `id`（编号）、`name`（名称）、`role`（角色）、`state`（状态）、`disabled`（禁用）、`actions`（支持动作）。`screenshot`（附带截图）为真时，网页窗口返回 `screenshot_jpeg`（以 Base64 文本编码的 JPEG 截图）。 |
| `ui.act`（控件操作） | 使用快照中的 `window`（窗口编号）、`control`（控件编号）、`revision`（页面代次）、`action`（动作）。输入使用 `text`（内容），按键使用 `key`（按键名）；动作取控件支持项，页面重载后重读快照。 |
| `module.list`（接口目录） | 返回 `desktop`（原生命令）、`service`（宿主或核心服务目录）、`remote`（已配对设备操作）、`remote_connected`（远端连接状态）、`observed_points`（已观察交接点）。服务目录含 `core_ready`（核心就绪）、`operations`（超文本协议路由）、`schemas`（对象约束定义）及 `realtime_operations`（实时操作）。 |
| `module.call`（业务调用） | 使用 `operation`（目录操作名）、`arguments`（参数对象）。`surface`（调用面）取 `desktop`（原生，默认）、`service`（宿主或核心服务）、`remote`（已配对设备）；可指定 `window`（窗口编号）。HTTP（超文本传输协议）路由使用 `path`（路径参数）、`query`（查询参数）、`body`（正文）。远程可携带 `request_id`（原业务请求编号）。结果含 `call_id`（关联调用编号）、`value`（正式业务回执）。 |
| `trace.subscribe`（订阅运行事件） | 返回 `subscribed`（订阅已建立）。`trace.event`（调试事件）含 `point`（真实节点）、`stage`（阶段）、`call_id`（关联调用编号）、`data`（脱敏数据）。 |
| `hook.arm`（设置一次介入） | 指定 `point`（节点）、`stage`（阶段，`input` 输入或 `result` 结果），可附 `call_id`（指定调用）。命中后收到 `hook.paused`（暂停事件），包含 `pause_id`（暂停编号）及原数据。 |
| `hook.resume`（继续执行） | 用 `pause_id`（暂停编号）恢复。省略 `replacement`（替换值）按原值继续；替换数据沿用正式业务格式、身份字段及校验，例如 `asr.final`（最终识别结果）保留身份并替换 `text`（文本）。20 秒到期、输入结束或断连时按原值继续。 |

资源任务、设备操作和服务请求始终使用正式权限及校验。测试局域网连接故障时，可在 `device.connection`（设备连接）输入点介入一次，提交正常范围的 `timeout_ms`（连接等待毫秒数），再观察真实重连逻辑。

## 界面操作与业务完成

从工作台开始时，先在快照中选择“打开设置”，操作后重新读取快照确认导航出现。启动反馈可能遮挡后方控件，应先查询启动完成状态，再按快照关闭已确认的反馈。以下示例从设置侧栏选择组件页，不改变用户设置；不能仅凭控件操作回执就断言页面已经到达。

```powershell
$report = '.local/cache/reports/agent-debug'
node scripts/agent-call.mjs --method ui.snapshot --params '{"window":"main"}' --output "$report/before.json"
$view = (Get-Content "$report/before.json" -Raw | ConvertFrom-Json).result
$control = @($view.controls | Where-Object { $_.name -eq '组件设置' -and $_.role -eq 'button' -and -not $_.disabled })[0]
if (-not $control) { throw '当前页没有组件设置按钮，请重新发现导航入口。' }
@{window=$view.window; revision=$view.revision; control=$control.id; action='click'} |
  ConvertTo-Json | Set-Content -Encoding utf8 "$report/action-params.json"
node scripts/agent-call.mjs --method ui.act --params-file "$report/action-params.json" --output "$report/action.json"
node scripts/agent-call.mjs --method ui.snapshot --params '{"window":"main"}' --screenshot "$report/components.jpg" --output "$report/after.json"
node scripts/agent-call.mjs --method module.call --params '{"operation":"resource_jobs","arguments":{}}' --output "$report/jobs.json"
node scripts/agent-call.mjs --method module.call --params '{"surface":"service","operation":"GET /api/v1/application/snapshot","arguments":{}}' --output "$report/application.json"
```

`accepted`（已接受）、`submitted`（已发出）或任务编号只确认操作已进入流程，不证明完成。点击后重读快照；异步任务查询 `resource_jobs`（资源任务），按 `job_id`（编号）检查 `state`（状态）、`phase`（阶段）、`error`（错误）；应用就绪查询应用快照。实时命令由后续事件或状态确认效果。失败或超时后先查原任务及当前状态，避免重复提交下载、保存或设备准备。

常见错误处理：

- 通道不可用：确认路径、正式宿主及当前工作区，通过正式入口启动。
- 未开放调试：确认关于页授权及启动模式，授权撤销会结束已有连接。
- 页面切换或代次过期：重读快照并发现控件，保留代次检查。
- 并发达到上限：等待已有回执，未完成请求使用不同编号。
- 提前断连或超时：保存已有回执及错误，再查询业务结果，不靠延时或盲目重发掩盖。

## 扩展与维护

### 新功能默认接入方式

普通新界面、新设置及新接口只维护自身的正式实现，调试能力随正式注册和标准语义自动获得。开发开始时按下表选择现有入口，不为每个功能增加调试命令、调试编号或另一套参数处理器。

| 新增内容 | 正常开发方式 | 自动获得的调试能力 |
| --- | --- | --- |
| 网页控件 | 优先使用 `button`（按钮）、`input`（输入框）、`select`（选择框）等标准元素，以可见文字或关联标签提供名称。自定义控件声明 `role`（角色）、可访问名称及对应状态。 | `ui.snapshot`（界面快照）通过浏览器可访问树发现控件及状态，`ui.act`（控件操作）复用通用动作；无需手工编号或专用调试属性。 |
| 前端原生命令调用 | 使用现有业务端口，底层复用 `locateNativeInvoker`（共用原生命令调用器）。 | 正常调用自动关联输入、结果与错误事件，沿用正式权限及介入校验。 |
| 桌面原生命令 | 在[正式命令注册表](../../apps/desktop/src-tauri/src/commands.rs)登记一次处理器，按既有规则配置所需窗口权限。 | 同一注册表生成正式处理器、权限清单和调试目录；不再同步两份命令表，也不修改调试分发代码。 |
| 核心服务接口或设置字段 | 在正式服务注册路由，沿用原请求类型、参数校验及持久化流程。能从类型派生的接口描述直接复用；现有字典参数接口不必为调试另写模型。 | `module.list`（接口目录）读取当前 OpenAPI（接口描述）；`module.call`（业务调用）透传参数给原处理器。新增字段、嵌套对象及约束定义自动进入目录。 |
| 宿主路由、实时命令 | 分别维护既有宿主路由注册表、实时协议枚举及正式处理器。 | 调试目录及分发从同一正式声明读取，新增业务不添加调试专用路由。 |
| 远程业务 | 按既有远程管理契约登记操作及授权目标。 | 调试目录复用正式远程操作清单；自动发现不扩大设备权限或把远程业务改投本机。 |

例如新的开关按钮只需正常声明语义，浏览器会提供名称、角色及按下状态：

```vue
<button type="button" :aria-pressed="enabled" @click="toggle">
  启用新功能
</button>
```

其中 `aria-pressed`（按下状态）由业务状态驱动，`enabled`（启用值）和 `toggle`（切换处理器）属于原业务。不要另加调试专用开关、事件处理器或状态副本。

快照的 `state`（控件状态）保留原有 `checked`（勾选）、`selected`（选中）、`expanded`（展开）及 `value`（值），并自动保留浏览器提供的其他标量状态，例如 `pressed`（按下）、`busy`（忙碌）、`readonly`（只读）、`invalid`（验证状态）及 `valuetext`（可读值）。节点关系不展开，受保护输入的值和可读值继续脱敏。业务新增状态不需要到调试层逐项登记。

服务目录的 `schemas`（对象约束定义）提供正式接口中的类型定义，`request_body`（请求正文描述）及参数中的 `$ref`（约束引用）可据此解析。调试层不枚举业务字段、不放宽类型检查，也不重复实现保存逻辑。字段名和专业术语的中文说明维护在原业务类型、接口描述或模块文档中。

### 验证融入现有流程

日常沿用原有组件与业务测试、`npm test`（前端及入口测试）和正式构建。已有流程自动检查共用调用器、正式注册一致性、新路由与新字段的自动发现及原参数校验；不要求每个页面补一份调试适配测试，也不新增开发前置任务。`npm run agent:check`（调试基础设施定向检查）仅供修改或排查公共适配层时快速使用，其检查已包含在现有测试内。

功能验收直接从最新快照和接口目录发现新增能力，再确认真实操作或业务完成状态。已有权限拒绝、页面代次、请求编号及异步完成约定继续适用。只有新增通用交互类型、传输机制、可替换交接边界或权限边界时，才需要扩展公共调试适配器；普通页面布局、业务名称和设置字段变化不应触发此类改动。

需要更细的内部观察或替换时，复用 `agent_debug.point`（单次介入点）或 `observe_operation`（观察生产调用）；这属于按需增加的业务交接，不是普通新功能的接入前提。可替换边界按真实消息校验，身份、连接代次及授权目标由原处理器核对。

回归位于原生共用模块 `agent_debug::session`（调试连接）和 `tests/agent-call.test.mjs`（工具测试），覆盖输入结束、乱序、队列背压、错误、超时、授权撤销及介入释放。实机验收必须使用项目专用接口，日志及源码分析辅助定位。最近实测及未覆盖场景见[本轮验收记录](../testing/agent-debug-20261120.md)。
