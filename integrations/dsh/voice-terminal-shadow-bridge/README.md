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
