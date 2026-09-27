# 跨会话衔接与 DSH 定时任务

## 一小时共同静默

Voice（语音）主线与当前关联的 DSH（影子代理宿主）会话共同静默一小时后，系统读取两侧本轮上下文，生成最多 800 字符的衔接文档。文档作为普通背景注入下一轮 Voice 请求和新的 DSH 会话。其他普通 DSH 会话不参与这次静默判定。

Voice 发声、用户输入或关联 DSH 会话恢复活动都会取消未提交的整理。双方再次空闲后，从最新活动结束时重新计时。整理成功时，Voice 和关联 DSH 会话同步轮换；整理失败则保留原会话及旧文档，等待下一次静默窗口。交接标记保存在 `continuity-handoff.json`（会话交接标记文件），进程重启后先完成未结束的交接。

衔接文档保存在 `continuity-state.json`（衔接文档文件）。没有值得延续的信息时写入空文档；模型请求失败时不清空旧文档。

## DSH 定时任务

`voice-terminal-wake-tasks`（语音终端唤醒任务插件）是项目任务的唯一存储与调度来源。任意 DSH 根会话均可创建任务；任务与创建会话的存活状态无关。插件在创建时冻结该会话的文本上下文，任务到期时只在消息中提供 `task_id`（任务标识），模型需要背景时调用 `read_wake_reference`（读取冻结参考工具）。

到期时插件请求桥接层在 Voice 会话交接锁内解析当前语音关联 DSH 会话，并将任务作为普通 `user/message`（用户消息）持久投递到该会话。旧会话已退役时使用新会话；投递暂时失败时保留任务并重试。任务投递不直接控制 Voice 侧的语音模型。DSH 模型需要通知 Voice 时调用 `publish_voice_event`（投递语音事件），将自由文本及可选附加内容写入 Voice 持久信箱。Voice 手动停止监听时，事件留待下次手动启动处理。

插件提供 `schedule_wake`（创建唤醒任务）、`cancel_wake`（取消唤醒任务）、`read_wake_reference`（读取冻结参考），以及与原生工具同名的 `schedule_create`（创建任务）、`schedule_list`（列出任务）、`schedule_update`（更新任务）、`schedule_delete`（取消任务）。原生工具支持的延迟、绝对时间、固定间隔、每日、每周和 Cron（定时表达式）规则由同一 DSH 规则模块解析。Voice Terminal（语音终端）的“唤醒任务”页读取插件任务目录，显示项目内所有 DSH 任务。

插件存储位于 DSH home（DSH 用户数据目录）的 `storages/voice-terminal-wake-tasks.json`（统一任务文件）。安装前，`scripts/check-dsh-wake-migration.mjs`（任务迁移预检脚本）验证原生 `schedule.json`（原生任务文件）和旧 Voice `wake-tasks.json`（旧任务文件）。插件首次启动时幂等导入，保留任务标识、状态及可用投递历史；无法转换的任务会阻止切换。安装脚本禁用 DSH 内置 Schedule（调度器）及其原生任务页，防止两套计时器重复投递。

## 安装

停止当前 Voice Server（语音服务端）及其受管 DSH 进程后，运行 `scripts/setup-dsh.ps1`（DSH 安装脚本），然后按项目日常方式重新启动 Voice Server。安装脚本会备份现有 DSH home、预检旧任务、构建两个插件并更新 Web profile（网页配置）。
