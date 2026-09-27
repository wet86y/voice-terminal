# Voice Terminal Wake Tasks（语音终端唤醒任务）

本 DSH（影子代理宿主）插件统一保存并调度项目定时任务。任意 DSH 根会话均可创建任务；到期消息始终进入当前 Voice（语音）主线关联的 DSH 会话。任务创建时的上下文快照保存在插件任务记录中，通过 `read_wake_reference`（读取冻结参考工具）按需访问。

工具：`schedule_wake`（创建简易任务）、`cancel_wake`（取消任务）、`read_wake_reference`（读取冻结参考）、`schedule_create`（创建原生规则任务）、`schedule_list`（列出任务）、`schedule_update`（更新任务）、`schedule_delete`（取消任务）、`publish_voice_event`（投递语音事件）。`publish_voice_event` 将普通文本事件送入 Voice 持久信箱，不要求 Voice 预先发起唤醒。

任务存储位于 DSH home（用户数据目录）的 `storages/voice-terminal-wake-tasks.json`（统一任务文件）。插件启动时导入原生 `schedule.json`（原生任务文件）与旧 Voice `wake-tasks.json`（旧任务文件），按任务标识避免重复导入。Voice Terminal 的任务设置页通过插件的受管本机接口读取和修改同一任务目录。

安装与会话清理的操作说明见 [连续性基础能力](../../../docs/continuity-foundation.md)。
