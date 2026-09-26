# Voice Terminal 唤醒任务插件

本 DSH（影子代理宿主）插件提供项目级唤醒任务工具。任务规则和冻结参考由
Voice Server（语音服务端）保存；插件负责把当前 DSH 文本上下文交给服务端。

工具包括 `schedule_wake`（创建唤醒任务）、`cancel_wake`（取消唤醒任务）和
`read_wake_reference`（读取创建时冻结的参考上下文）。工具字段均带中文说明。

Voice Server（语音服务端）会向项目受管 DSH 进程提供
`VOICE_DSH_BRIDGE_TOKEN`（桥接鉴权令牌）和
`VOICE_DSH_CALLBACK_ORIGIN`（本机回调地址）。
