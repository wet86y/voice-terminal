# Voice Terminal Shadow Bridge

`progress`（影子进度）继续更新产品界面，实质变化按当前 `activation_id`（激活标识）和
`source`（来源）合并进入语音端二等信箱。它不立即唤醒主模型，在活跃静默窗口内每 4 秒复用单一
400 毫秒心跳检查非空信箱，也可随普通请求携带。`information`（影子信息）保持普通事件调度及单槽版本消费，
同一正文应只选择一个入口。二等状态仅为临时请求上下文，不进入语义历史；终态和激活失效清理旧进度。
详见 [Bridge 2.0（第二版桥接协议）](../../../docs/bridge-2.0-protocol.md)。

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

## 提前进入静默

`request_early_silence({})`（请求提前进入静默，空参数对象）仅允许当前语音会话绑定的活动根代理调用。
当前用户明确要求，或本会话已启用、目标为影子代理的自定义指令明确规定当前场景时，才可使用该工具。
普通任务完成、外部资料及代理自行推断不构成调用依据；此语义条件由工具说明和受管指令约束。

工具通过 `/api/internal/dsh-bridge/input.early-silence`（提前静默端点）受理请求，不结束 DSH（影子代理宿主）回合。
Voice Server（语音服务）等待当前输入、推理和播报空闲后，提前执行 `final_silence`（静默前最终检查），
并在检查及必要播报结算后从 ASR（自动语音识别）活跃期进入 KWS（关键词唤醒）待机。
重复请求合并；新输入到达时继续等待最新一轮的安全边界；停止、重置或输入所有权代次变化时清除请求。
`accepted`（已受理）不表示交接已经完成，受理状态详见 [DSH 控制台说明](../../../docs/dsh-console.md)。

插件重建后须启动或重启语音服务，并使用新语音会话加载工具与受管规则。麦克风现场验收步骤见
[运行与质量验收](../../../docs/operations-and-quality.md)。

## Continuity and wake tasks（上下文衔接与唤醒任务）

`session.export`（完整会话导出）使用既有鉴权和会话身份边界。`export_id`（冻结导出标识）冻结完整记录，`offset`（起点）、`limit`（页大小，1—500）与 `total`（记录上界）确定分页；响应提供 `records`（完整记录）及 `next_offset`（下一页起点）。Voice（语音端）交接使用此入口保存成对归档，普通摘要接口继续服务既有调用。

`session.resume`（恢复会话）的 `archive_only=true`（仅为归档恢复）保留持久历史，不追加语音管理提示。`session.cancel`（取消会话）和 `session.retire`（退役会话）等待 `whenIdle`（工具与回合结算至空闲）并确认持久保存，随后才返回；重复退役返回 `duplicate`（重复确认）。`session.state`（状态查询）的 `record_count`（完整记录水位）用于提交前检测整理期间恢复活动。Voice（语音端）等待结算时释放事件锁，过时回传可以及时得到拒绝。

This Bridge（插件桥接层） works with `voice-terminal-wake-tasks`（语音终端唤醒任务插件）.
The plugin owns task persistence（任务持久化） and scheduling（任务调度）. On due delivery,
the Bridge writes a normal `user/message`（用户消息） to the current Voice-linked
DSH Session（语音关联影子会话） while Voice Server（语音服务端） holds its session handoff lock（会话交接锁）.
The creating Session（创建会话） is not a delivery target. The frozen task reference（冻结任务参考）
is read only on demand via `read_wake_reference`（读取冻结参考工具）. DSH reports to Voice
through `publish_voice_event`（投递语音事件工具） and a durable mailbox（持久信箱）.
See [continuity foundation](../../../docs/continuity-foundation.md)（连续性底座说明）.

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

Bridge（桥接层）不再在任意 `tool/result`（工具结果）后调用 `agent.cancel()`（取消代理）。
Voice activation（语音激活）是否仍有效只由现有 `activation_id`（语音激活标识）表达；终端回调
只清理发起该回调的同一激活，较晚返回的旧回调不能结束新的激活。普通定时任务因此可以在同一
主线会话中继续使用 DSH 的完整 Harness（执行环境）能力。

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
- 定时任务现在投递到当前关联的普通 DSH 会话，`wake.open`（旧独立唤醒会话接口）
  不再是调度路径；语音事件通过 `publish_voice_event`（投递语音事件工具）写入持久信箱。
- `source.kind = schedule`（来源类型为定时任务）不属于 Voice activation（语音激活），不得使用
  Voice terminal tool（语音终端工具）；其正常回合与 Voice activation（语音激活）共享会话历史，
  但不共享终态状态。
- **事件注入工具不响应取消。** `post()` 与 `get_latest_context` / `read_ambient_context`
  向 Voice Server 发起的循环回环请求不接 `exec.signal`；DSH 仍会在 `tools/execute` 层
  中止调用，但已发出的 HTTP 请求不会被取消。
