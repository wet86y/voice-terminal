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
