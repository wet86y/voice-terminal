# Voice Terminal Wake Tasks（语音终端唤醒任务）

本 DSH（影子代理宿主）插件保存并调度 Voice（语音）项目链路任务，与 DSH 原生 Schedule（会话定时任务）分开。任意 DSH 根会话均可创建项目任务；到期消息进入当前 Voice 主线关联的 DSH 会话，而不是创建任务的会话。任务创建时的上下文快照保存在项目任务记录中，通过 `read_wake_reference`（读取冻结参考工具）按需访问。

项目工具：`schedule_wake`（创建简易唤醒任务）、`cancel_wake`（取消简易唤醒任务）、`read_wake_reference`（读取冻结参考）、`wake_task_create`（创建项目定时任务）、`wake_task_list`（列出项目任务）、`wake_task_update`（更新项目任务）、`wake_task_delete`（取消项目任务）、`publish_voice_event`（投递语音事件）。`publish_voice_event`（投递语音事件）将普通文本事件送入 Voice 持久信箱，不要求 Voice 预先发起唤醒。

DSH 原生 `schedule_create/list/update/delete`（创建、列出、更新、删除会话任务工具）由可选官方 Automation Bundle（自动化功能包）`@deepseek-ai/dsh-experimental-schedule-bundle` 提供，默认关闭但允许用户启用。原生任务绑定 DSH Session（会话），由原生调度器投递并在官方任务页管理；项目 `wake_task_*`（项目任务工具）独立保存并投递到当前 Voice 主线。两者名称、存储和投递目标不同，可同时运行。

每次触发复用 `occurrence_id`（任务发生标识）作为投递请求和 DSH 消息的幂等标识。调用
`publish_voice_event`（投递语音事件）时如果没有显式提供 `event_id`（事件标识），插件会使用
当前 DSH 会话标识和 `callId`（工具调用标识）生成稳定哈希；同一工具调用在恢复或重放后不会
重复写入 Voice 持久信箱。

项目任务存储位于 DSH home（用户数据目录）的 `storages/voice-terminal-wake-tasks.json`（项目任务文件）。插件启动时只迁移旧 Voice `wake-tasks.json`（旧语音任务文件）中的可转换记录，已有项目任务及历史保持不变；它不会读取或导入官方 Schedule（会话定时任务）数据。用户启用的 Automation Bundle（自动化功能包）由安装与构建流程保留。Voice Terminal（语音终端）的任务设置页通过插件的受管本机接口读取和修改项目任务目录。

待执行任务的取消与归档删除分别处理：插件 `DELETE tasks/{id}`（取消项目任务）保留取消记录，`DELETE tasks/{id}/archive`（删除归档）仅移除已归档或已取消的记录。存储文件的 `deleted_task_ids`（已删除任务标识）防止旧任务迁移在重启后恢复已删除记录。删除写入失败时恢复内存中的原记录并返回错误；重复删除已删除记录返回 `deleted: true`（已确认删除）。官方会话任务不受影响。

产品页使用 `POST /api/v1/wake-tasks/{id}/cancel`（取消待执行任务）与 `DELETE /api/v1/wake-tasks/{id}`（删除归档），删除接口必须收到插件的成功回执才报告成功。

安装与会话清理的操作说明见 [连续性基础能力](../../../docs/continuity-foundation.md)。
