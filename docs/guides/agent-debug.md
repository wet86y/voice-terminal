# 正式程序的代理调试模式

在「设置 → 关于」允许外部调试。普通模式提供状态和启停控制。使用当前工作区的正式入口切入调试模式。

```powershell
$entry = (Resolve-Path .\.local\VoiceTerminal.exe).Path
$stop = Start-Process -FilePath $entry -ArgumentList '--exit' -WindowStyle Hidden -PassThru
$stop.WaitForExit()
if ($stop.ExitCode) { throw '正式程序退出未完成。' }
$start = Start-Process -FilePath $entry -ArgumentList '--agent-debug' -WindowStyle Hidden -PassThru
$start.WaitForExit()
if ($start.ExitCode) { throw '调试窗口未就绪。' }
'{"jsonrpc":"2.0","id":1,"method":"ui.snapshot"}' | & $entry --agent-stdio
```

`--exit`（正常退出）等待宿主进程退出且工作区释放。`--agent-debug`（调试启动）沿用当前持久配置。启停和调用也可由已建立授权的 SSH（安全外壳协议）在目标机直接执行同一入口。远程启动需要同账户有活动的交互桌面。业务调用实际应用于当前工作区或产品当前连接的已配对设备。

## 调用协议

`--agent-stdio`（标准输入输出调试入口）每行传递一个 UTF-8（统一字符编码）的 JSON-RPC（结构化调用协议）消息。`id`（请求编号）使用整数或字符串。`method`（方法名）和 `params`（方法参数）描述操作。响应按请求编号匹配。订阅、暂停和业务调用应保持同一进程的输入输出连接，多个请求可同时等待回执。

| 方法 | 参数与结果 |
| --- | --- |
| `instance.status`（实例状态） | 返回模式、桌面进程号和窗口就绪状态。 |
| `ui.snapshot`（查看界面） | 省略 `window`（窗口编号）列出窗口。指定窗口返回控件及 `revision`（页面代次）。`screenshot`（附带截图）为真时，网页窗口返回 `screenshot_jpeg`（以 Base64 文本表示的 JPEG 截图）。 |
| `ui.act`（操作控件） | 使用快照返回的 `window`（窗口编号）、`control`（控件编号）、`revision`（页面代次）和 `action`（动作）。输入文字携带 `text`（内容），按键携带 `key`（按键名）。页面重载后重新读取。 |
| `module.list`（发现接口） | 返回当前窗口可用的原生命令、服务路由、实时消息以及已经观察到的交接点。远程配对模式同时列出既有固定管理操作。 |
| `module.call`（直接调用） | 使用 `operation`（目录内操作名）和 `arguments`（参数对象）。`surface`（调用面）取 `desktop`（原生，默认）、`service`（宿主或核心服务）、`remote`（已配对设备）。HTTP（超文本传输协议）路由参数使用 `path`（路径参数）、`query`（查询参数）和 `body`（请求正文）。远程可携带 `request_id`（原业务请求编号）。 |
| `trace.subscribe`（订阅运行事件） | `trace.event`（调试事件）含 `point`（实际接口或交接点）、`stage`（运行阶段）、`call_id`（关联调用编号）和 `data`（脱敏数据）。实时命令的 `submitted`（已发出）由后续真实事件确认效果。 |
| `hook.arm`（设置一次介入） | 指定 `point`（节点）、`stage`（`input` 输入或 `result` 结果），可附 `call_id`（指定调用）。节点命中时收到 `hook.paused`（暂停事件）。 |
| `hook.resume`（继续流程） | 用 `pause_id`（暂停编号）恢复。省略 `replacement`（替换值）即按原值继续。支持替换的交接点使用原数据格式及业务校验，例如 `asr.final`（最终识别结果）可提交保持原身份字段的消息并替换 `text`（文本）。20 秒到期或控制连接断开时按原值继续。 |

资源任务、设备操作和服务请求按正式权限和参数校验执行。测试局域网连接故障时，可在 `device.connection`（设备连接）输入点介入一次，提交正常范围内的 `timeout_ms`（连接等待毫秒数），由真实连接及重连逻辑处理。

## 扩展

新控件通过可访问树发现。正常声明按钮、选项和输入框的名称、角色和状态，并沿用通用原生命令调用器。正式权限清单、服务路由和实时消息契约同时提供目录。内部业务交接复用 `agent_debug.point`（单次介入点）或 `observe_operation`（观察生产调用）。新增可替换边界以实际业务消息为依据校验替换数据，身份、连接代次和受权目标继续由原处理器核对。实时采集和播放在异步的开始、停止及数据交接处记录。

## 开发验收

2026-11-20 在隔离工作区验证了真实「搜索设备」按钮与同一调用编号的搜索结果、原生文件选择框、一次性暂停与恢复，以及关闭授权后的会话撤销和调试启动拒绝。在另一台电脑上通过同账户 SSH（安全外壳协议），从非交互会话完成了正常退出、启动可见调试窗口、读取控件和切回普通模式。远端验收进程与一次性启动任务均已回收。

前端 538 项、开发入口 3 项、后端定向 169 项和原生共用模块 390 项通过。完整语音供应链路、产品双机配对后的重连、悬浮窗以及跨账户访问拒绝仍待实机验收。这些结果对应开发构建候选。
