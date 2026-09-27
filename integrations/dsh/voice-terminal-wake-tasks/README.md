# Voice Terminal Wake Tasks（语音终端唤醒任务）

This DSH（影子代理宿主） plugin（插件） adds project-level scheduled-wake
tools to the formal root Agent（正式根代理）. Voice Server（语音服务端） stores
the task rules and runs the scheduler（调度器）; they are independent of any one
DSH（影子代理宿主） or Voice（语音） session（会话）.

## Tools（工具）

| Tool（工具） | Purpose（用途） | Fields（字段） |
| --- | --- | --- |
| `schedule_wake`（创建唤醒） | Create a project-level wake task（创建项目级唤醒任务） | `target`（唤醒目标）, `when`（触发时间）, optional `repeat`（固定重复间隔）, `text`（任务说明） |
| `cancel_wake`（取消唤醒） | Cancel a pending task（取消待执行任务） | `task_id`（任务标识） |
| `read_wake_reference`（读取唤醒参考） | Read the frozen context（读取冻结上下文） on demand | `task_id`（任务标识） |

`target`（唤醒目标） accepts `voice_llm`（语音大模型）, `shadow_agent`（影子代理）,
or `both`（双方）. `when`（触发时间） accepts a delay such as `30m`（延迟 30 分钟）
or an ISO date-time（ISO 日期时间）; timestamps without an explicit time zone use
the project local time zone（项目本地时区）. `repeat`（固定重复间隔） is optional
and accepts seconds, minutes, hours, or days; omitting it creates a one-shot task
（一次性任务）. `text`（任务说明） must be non-empty and no longer than 500 characters.

When creating a task, the plugin freezes text from recent user, assistant, and tool
records in the current DSH（影子代理宿主） session（会话）. This reference（参考上下文）
is read-only and is not automatically injected into a future wake. The Agent can use
`read_wake_reference`（读取唤醒参考） when the DSH Agent（影子代理） needs that context. Editing a task
does not change its frozen reference.

## Storage and management（存储与管理）

Voice Server（语音服务端） persists tasks in the project configuration file
`wake-tasks.json`（唤醒任务文件）. The Voice Terminal（语音终端） settings page
lists pending（待执行）, archived（已归档）, and cancelled（已取消） tasks. Users can
edit or remove pending tasks there; removing one cancels its wake. One-shot tasks
are archived after the target accepts delivery, without tracking the target's later
work. See [continuity foundation](../../../docs/continuity-foundation.md)
（连续性底座说明） for delivery, restart, and recovery behavior.

Voice Server（语音服务端） supplies `VOICE_DSH_BRIDGE_TOKEN`（桥接鉴权令牌） and
`VOICE_DSH_CALLBACK_ORIGIN`（本机回调地址） only to the project-managed DSH child
process（受管影子代理宿主子进程）. The plugin uses these values to call the
authenticated local task API（本机唤醒任务接口）; it does not expose the token to
the browser.

Build and install this plugin with `scripts/setup-dsh.ps1`（DSH 插件安装脚本）.

## Known Limitations and Deferred Work

- The tool-disposal registry keys agents with a `WeakMap`（弱引用映射）so an Agent that
  disappears without a matching `agent/disposed`（代理已释放）event is not kept alive
  by this plugin. Removal order is tracked by the detach closures（拆卸闭包）, which
  hold no Agent reference.
- The three tools do not observe `exec.signal`（执行取消信号）: an in-flight loopback
  request to Voice Server（语音服务端）is not cancelled when the call is aborted. DSH
  still aborts the call at the `tools/execute` layer.
