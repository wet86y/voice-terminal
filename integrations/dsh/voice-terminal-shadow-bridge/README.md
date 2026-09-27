# Voice Terminal Shadow Bridge

Project-owned DSH bundle for the formal Voice Terminal Shadow root Agent. It is
the only supported integration boundary between Voice Terminal and DSH.

The bundle exposes an authenticated loopback HTTP protocol, owns the mapping to
formal root Agents, injects Voice-authoritative context, registers root-scoped
Voice control tools, and projects committed DSH activity into bounded observer
snapshots. Each accepted activation also receives a non-waking convergence
advisory at the configured runtime reminder interval (20 seconds by default);
unclaimed reminders are replaced in place and are consumed only at a DSH
next-step boundary. It never transports audio, model credentials, browser
launch tokens, or ordinary assistant prose to the Voice mailbox.

The runtime requires `VOICE_DSH_BRIDGE_TOKEN` and `VOICE_DSH_CALLBACK_ORIGIN`.
Both are supplied only to the project-managed DSH child process.

## Continuity and wake tasks（上下文衔接与唤醒任务）

This Bridge（插件桥接层） works with the separate
`voice-terminal-wake-tasks`（语音终端唤醒任务插件）. The wake-task plugin exposes
`schedule_wake`（创建唤醒）, `cancel_wake`（取消唤醒）, and
`read_wake_reference`（读取冻结参考） to the formal root Agent（正式根代理）. Voice
Server（语音服务端） owns task persistence（任务持久化） and scheduling（任务调度）;
this Bridge does not store or schedule jobs.

When a `shadow_agent`（影子代理） wake arrives through `wake.open`（打开唤醒会话接口）,
the Bridge creates an independent DSH（影子代理宿主） session（会话） and sends the task description together
with the current continuity document（会话衔接文档）. The frozen task reference is
not injected; the DSH Agent（影子代理） reads it only through `read_wake_reference`（读取冻结参考工具）.
The wake-task lifecycle, Voice Terminal task-management page（唤醒任务管理页）, and
recovery behavior are documented in
[continuity foundation](../../../docs/continuity-foundation.md)（连续性底座说明）.

The browser half registers an independent Voice Terminal settings page（独立设置页面）
in the DSH Settings sidebar（设置侧栏）, after Agent Presets（Agent 预设）. Its
workspace menu（工作区菜单） lists registered DSH workspaces and does not
change the active workspace or session. The settings document（设置文档） persists
`workspaceId`（所选工作区标识）, `defaultWorkspaceId`（项目默认工作区标识）,
`provider`（模型供应方）, `model`（模型标识）, `reasoningEffort`（推理强度）, and
`fallbackNotice`（回退提示状态）. The first Bridge（插件桥接层） startup
creates or registers the project-root `workspace` directory and uses its stable
workspace id as the default. These choices apply only to new voice-triggered
sessions; manual DSH sessions and resumed Voice sessions keep their existing
settings. An unavailable saved workspace or model falls back to the project
workspace or current DSH global model, records a diagnostic（诊断日志）, and
shows a notice（提示） on the settings page.

The settings page uses the DSH `Menu`（菜单控件） and `Button`（按钮控件） primitives
for workspace, model, and reasoning-effort choices. The model menu reads the
currently loaded DSH model catalog（模型目录） and groups entries by provider
（模型供应方）. Leaving the provider/model fields unset follows the DSH global
model; leaving reasoning effort unset uses the model default（模型默认推理强度）.

An unset provider/model pair follows the DSH global model; unset reasoning
effort uses the model default（模型默认推理强度）. The Node Bundle（Node.js 插件运行包）
and browser module（浏览器插件模块） are both built and installed by
`scripts/setup-dsh.ps1`. A Bridge build（插件构建） verifies the pinned DSH
source（固定版本源码） remains unchanged after its temporary browser build
staging directory（浏览器构建暂存目录） is removed.

Bridge（插件桥接层） context（上下文） messages（消息） that DSH（影子代理宿主）
records as ordinary injected context（注入上下文） are shown in the Chat（对话）
transcript（对话记录） with their complete content（完整内容） and source（消息来源）.
Messages（消息） already shown as native DSH wake notices（原生唤醒提示） keep that
presentation. Assistant（助手） and Tool（工具） execution records remain in DSH's
existing Chat（对话） and Trajectory（执行轨迹） views（视图）.

## DSH 0.1.7 对话显示兼容性

从 DSH 0.1.5 升级到 0.1.7 后，Bridge（插件桥接层）写入的 `user/message`（用户消息）
仍保存在会话事件流中并参与模型上下文，但 DSH Chat（对话页）的新节点投影默认不显示
普通 `context`（上下文）节点，因此这些注入内容会从对话记录中消失。Bridge（插件桥接层）
现在注册 `voice-terminal-context`（语音终端上下文节点），只呈现来源标记属于本插件的消息，
并保留完整内容和来源。已经由 DSH 原生显示的唤醒提示不会重复呈现。

从没有待办任务的空会话触发 DSH（影子代理宿主）时，代理可能以
`finish_shadow_no_action`（无操作结束工具）正常结束，而不生成助手文字。验证唤醒执行时，
应在唤醒说明或会话上下文中提供明确任务。

## 终端工具与回合终结

`request_user_input`、`return_shadow_result` 和 `finish_shadow_no_action`（三个终端工具）
在 Voice Server（语音服务端）确认收下事件后调用 `exec.concludeTurn()`，把当前 DSH 回合
标记为在该步终结。`accepted`（首次投递）与 `duplicate`（同一 `request_id` 的重放确认）
都算投递成功——两者都是 HTTP 200。缺少这个标记时，Agent Loop 的 `step()` 不返回终结原因，
`turn/end` 会带着 `reason: null` 落盘，而 DSH 0.1.7-rc.2 的 Chat（对话页）会在正常渲染
路径上读取 `turn/end.data.reason.kind`，于是抛出
`TypeError: Cannot read properties of null (reading 'kind')`，打死共享的 event feed 订阅，
使**所有**会话的对话页变空。

## Known Limitations and Deferred Work

- **无法从插件侧修复读取端崩溃。** 阻止 `reason: null` 写入是插件职责（见上节），但崩溃点
  在 DSH 官方源码内：`ui-chat` 与 `ui-trajectory` 的多个 definition 以
  `reason.kind` 直接解引用，`?.` 拦不住 `null`（`turn-process.ts`、`ChatGroupSeat.tsx`、
  `TurnProcessNodeView.tsx`、`turn-error.ts`、`turn-tail.ts`、`turn-max-tokens.ts`、
  `ui-chat/apply.ts`、`trajectory-assistant-definition.ts`）。`ConversationNodeDefinition`
  按 `kind` 唯一注册、无法替换内置 definition，因此插件无法覆盖这些读取。
  `config/dsh/README.md` 同时禁止修改 `.runtime/dsh/source`；一旦解除该禁令，应优先在
  DSH 侧加空值保护，并向上游反馈：`agent.ts` 让「工具未声明终结」退化成 `turn/end` 的
  `reason: null`，绕过了 `reason: TurnEndReason` 的非空类型契约。
- **残留风险：** 若模型在某一回合既没有调用任何终端工具、也没有以文本收尾，该回合仍可能写入
  `reason: null`。Bridge 的 protocol recovery（一次恢复回合）会尝试纠正，但那一回合的
  `turn/end` 仍会被读取端看到。遇到对话页空白时，先跑
  `node scripts/dsh-diagnose/find-null-kind-rows.mjs` 排查是否存在 `reason: null` 记录，
  必要时用 `node scripts/dsh-diagnose/purge-poisoned-sessions.mjs` 备份后清理。
- **`session.resume` 不安装根工具。** 只有新建会话（`session.open`）与唤醒会话
  （`wake.open`）会注册 Voice 控制工具；恢复会话沿用既有 composition，不重复安装。
- **唤醒会话的根工具注册尚未对齐。** `wake.open` 路径只挂载 Shadow preset，没有注册
  Voice 控制工具，因此唤醒会话的代理无法通过 Bridge 工具回投结果。补齐它需要让工具注册
  与 `ensure()` 的绑定校验共用同一份 binding 状态，属于行为改动，留待单独确认。
- **事件注入工具不响应取消。** `post()` 与 `get_latest_context` / `read_ambient_context`
  向 Voice Server 发起的循环回环请求不接 `exec.signal`；DSH 仍会在 `tools/execute` 层
  中止调用，但已发出的 HTTP 请求不会被取消。
