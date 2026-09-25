# 项目隔离 DSH Shadow 执行端

Voice Terminal 内置第一方 DSH Bundle `voice-terminal-shadow-bridge`。当
`shadow_agent.backend: dsh` 时，项目 DSH Web Host 中的正式根 Agent承担 Shadow 任务；当前默认值
已经切换为 `dsh`，`native` 仅作为显式人工回滚后端保留。后端在新 Voice Session 创建时固定，同一 Session不热切换，也不在 DSH
故障时自动转交 native。

固定基线为 `dsh-v0.1.5-rc.2` / commit
`fb2c4b9e698e30edb738bca4cf0618587db7d203`。Bundle 只使用该版本的公开 Cordis、Web Host、Agent、
Session、Tool 和事件扩展点；不得修改 `.runtime/dsh/source`、解析私有 Session 文件、依赖私有
Web RPC，或启动第二个长期持有同一 Session 的 DSH 进程。

## 安装与漂移校验

```powershell
.\scripts\setup-dsh.ps1
.\scripts\setup-dsh.ps1 -ValidateOnly
```

setup 会验证固定 source/tag/commit、Node、pnpm 和 CLI，构建 TypeScript Bridge Bundle（插件运行包），用官方
`dsh plugin --profile web add` 安装到项目 profile，同步正式 preset，运行 `dump-config`，并记录
Node Bundle（Node.js 插件运行包）与浏览器插件页的 hash（哈希值）、协议版本、公共提示词哈希和受管配置哈希。浏览器插件构建会
使用临时 staging directory（暂存目录），结束后移除，不保留对固定 DSH source（源码）的修改。`-ValidateOnly`
会检查 Node Bundle（Node.js 插件运行包）、浏览器入口、插件页 manifest（清单）和安装状态；发现缺失或漂移即拒绝通过校验。

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

项目私有运行数据位于 `.runtime/dsh`：

- `source`：固定上游源码和构建物；
- `home`：项目 DSH_HOME、正式/retired Session、Harness 设置和凭据；
- `session-mappings.json`：不含正文、凭据或 token 的 Voice↔DSH 映射；
- `build-state.json`：不含秘密的构建与 hash 审计；
- `web.dump-config.yml`：最后一次受管配置展开结果。

## 通信与安全边界

DSH 固定监听 `127.0.0.1:3081`。Voice Server 每次启动生成独立 Bridge Bearer token，仅通过受管
子进程环境交给 Bundle；它与浏览器 launch token、DSH Cookie 和模型 API key完全分离。Bridge
路由和 Voice 内部回调都拒绝非 loopback、错误 token、错误 Session/epoch/revision/activation、
跨 Session发布、冲突重放和 retired Session。

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
