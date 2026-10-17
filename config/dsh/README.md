# 项目 DSH 基线

Voice Terminal 选定的 DeepSeek Harness（DSH，代理执行宿主）目标版本为预发布版
`dsh-v0.2.0-rc.2`，提交号为
`639ed015397290b3745d163aafe02ffee4aa3f84`。这不是稳定版；升级必须显式更新锁定文件、许可证快照和回归检查，
不追踪浮动版本。当前源码将 Bridge（桥接）、Wake Tasks（项目唤醒任务）和 Shadow preset（影子代理预设）合并为
`@voice-terminal/dsh-plugin`（统一功能包）0.2.0，Bridge protocol（桥接协议）仍为 2.0。组件职责、兼容契约和维护门槛见
[DSH 插件维护手册](../../docs/guides/dsh-plugin-maintenance.md)。2026-10-07 已完成项目内统一包安装迁移，
构建记录格式为 6，117 个原有数据文件摘要核对一致；完整结果见[本轮安装与验证](../../docs/guides/dsh-plugin-maintenance.md)。
桌面发行目录仍按独立发布流程管理，本轮没有重新发布便携程序。

该宿主版本于 2026-10-05 完成首次源码及便携桌面部署，并完成共存修复部署；当时三个项目功能包均为 0.1.1。
当次便携应用位于 `apps/20261005-dsh-task-coexist`；首次部署目录为 `apps/20261005-dsh020rc2`。DSH component（DSH 组件）标识为
`639ed015-e7dd1315c977`；构建编号仅保留在清单；当前运行目录固定，重要用户数据最多保留一份滚动备份。首次部署的验收范围、任务与会话数据核验见[本机部署验收记录](../../docs/guides/local-layout.md)；共存修复已完成部署并通过[本机验收](../../docs/guides/local-layout.md)。
工具选择和投递目标见[连续性底座](../../docs/architecture/continuity-foundation.md)与[DSH 插件维护手册](../../docs/guides/dsh-plugin-maintenance.md)。

首次部署、共存修复前的测试结果：backend（后端）666 passed（通过）、17 skipped（跳过）；frontend（前端）64 passed（通过）；
Bridge（桥接插件）22 passed（通过）；Wake Tasks（唤醒任务插件）3 passed（通过）；official DSH tests
（DSH 官方测试）102 passed（通过）。本次共存修复另有 backend（后端）671 passed（通过）、17 skipped（跳过），Bridge（桥接插件）22 passed（通过）及 Wake Tasks（唤醒任务插件）4 passed（通过）。frontend（前端）64 passed（通过）、typecheck（类型检查）、build（构建）和 official DSH tests（DSH 官方测试）102 passed（通过）沿用首次部署记录，本次修复未改动这些部分。

使用 PowerShell 7 的 `pwsh -NoProfile -File scripts/setup-dsh.ps1` 安装或修复项目运行时，使用
`pwsh -NoProfile -File scripts/setup-dsh.ps1 -ValidateOnly` 检查安装状态。官方源码和项目专属 DSH_HOME（DSH 数据目录）
分别位于 Git（版本控制）忽略的 `.local/build/dsh/source/`（唯一上游源码缓存）和 `.local/data/dsh/`（唯一代理持久数据）。
实际业务只使用 `.local/runtimes/dsh/`（生产宿主），不在源码缓存内保留第二套代理状态。
项目插件随 `.local/app/`（当前程序）更新；运行组件升级先构建候选，经覆盖发布换入固定位置，成功后回收事务旧组件。
升级不制作源码、模型或环境备份；重要用户数据只保留一份滚动备份，规则见[本地目录与覆盖发布](../../docs/guides/local-layout.md)。

正式 `shadow-agent`（影子执行代理）由 `integrations/dsh/voice-terminal/preset/shadow-agent/` 维护。
插件构建将该配置合入统一 Bundle（功能包）的组合补丁，安装到 Web
profile（网页配置），并通过 `agent-preset-registry`（Agent 预设注册器）的 `default`（部署默认预设字段）
设为默认项。DSH 0.2.0-rc.2 不再读取 DSH_HOME（数据目录）中旧版 `.agent-presets/` 目录；该目录即使为回退保留，也不会注册预设。

项目 DSH Web Host（DSH 网页服务）仅监听 `127.0.0.1:3081`，由 Voice Server（语音服务）管理。
从桌面程序打开 Voice Terminal，再使用工作台“后台协作”面板底部的“代理控制台”入口取得一次性访问地址；
直接访问 3081 不带 launch token（临时访问令牌）时不能完成网页授权。不要收藏、记录或分享带
`?token=` 的完整地址。

`voice-terminal-shadow-bridge`（保留的桥接加载行与设置命名空间）使用 DSH v0.2.0-rc.2 的公开插件接口。
设置页面继续使用 `configForms`（设置表单服务）；旧的 `settingsScope` 接口已移除。页面注册受服务
生命周期控制，避免服务尚未就绪时插件启动失败。

## 目标版本配套升级说明

DSH v0.2.0-rc.2 将原生 Schedule（会话定时任务服务）移入可选 Automation Bundle（自动化功能包）
`@deepseek-ai/dsh-experimental-schedule-bundle`。该 Bundle 随官方 CLI（命令行程序）安装但默认关闭；
用户可通过 Web（网页）侧栏的 Plugins（插件）页面启用。启用后，原生 `schedule_create/list/update/delete`
（创建、列出、更新、删除会话任务工具）按 DSH Session（会话）存储并投递到所属会话，原生任务页也随之启用。

项目 Wake Tasks（唤醒任务插件）与原生 Schedule（会话定时任务）职责不同，可同时使用。Wake Tasks 的
`wake_task_create/list/update/delete`（创建、列出、更新、删除语音链路任务工具）及简易入口
`schedule_wake`（创建简易唤醒任务）使用项目自己的任务目录，触发后投递到当前 Voice（语音）主线关联的
DSH 会话。两套工具名、存储和投递目标分开；启用官方 Bundle（功能包）不会将原生 DSH 任务导入 Voice
任务，也不会关闭项目唤醒任务。

升级 profile（配置档案）时，旧的根级 `schedule`（原生调度服务）或 `ui-schedule`（原生任务页）禁用补丁
可能指向未挂载的条目并产生 `patch: entry <id> not found`（补丁目标不存在）警告；若出现，应移除这类过时
补丁，通过 Automation Bundle（自动化功能包）控制官方功能。安装与构建会保留用户已经启用的官方 Bundle。
Wake Tasks 仅迁移旧 Voice `wake-tasks.json`（旧语音任务文件）；不读取或导入 DSH 原生 Schedule（调度）
任务，已存在的项目任务继续留在 `storages/voice-terminal-wake-tasks.json`（项目任务文件）中。

完整的安装、设置、运行和故障说明见 [`docs/guides/dsh-console.md`](../../docs/guides/dsh-console.md)。许可证快照位于
`licenses/`。
