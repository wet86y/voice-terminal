# DSH 会话任务与语音链路任务共存修订

修订日期：2026-10-05。本文记录本次 DSH（代理执行宿主）升级后的定时任务冲突、修复、部署、兼容边界和验证结果。项目使用说明见[DSH 控制台指南](dsh-console.md)，本机部署快照和日志见 Git 忽略的[共存修复验收记录](../.runtime/dsh-coexist-20261005/acceptance.md)。

## 修订结论

DSH Session Schedule（会话定时任务）和 Voice Terminal Wake Tasks（语音链路定时任务）用途不同，应当共存。用户启用官方“自动化任务”后，DSH 报 `schedule_create is already registered in this scope`（同一工具作用域重复注册 `schedule_create`）。根因是项目插件也将自己的链路任务 CRUD（增删改查）注册为官方 `schedule_create/list/update/delete` 工具名；冲突发生在同一个根 Agent（根智能体）的工具注册表。

本次本地对照 DSH `0.1.7-rc.2` 和 `0.2.0-rc.2` 的 Schedule（会话定时）核心注册实现相同。没有证据表明 DSH 版本升级移除了两类任务共存能力；本次修复让两个插件分别使用自己的工具名、存储和投递目标。

| 任务类别 | 工具名 | 保存位置 | 触发后的目标 |
| --- | --- | --- | --- |
| 官方会话任务 | `schedule_create/list/update/delete`（创建、列出、更新、删除会话任务） | DSH home（数据目录）下官方 `storages/schedule.json`（会话任务文件） | 创建任务的 DSH 会话，由官方调度与“自动化任务”页面管理 |
| 项目语音链路任务 | `wake_task_create/list/update/delete`（创建、列出、更新、取消链路任务） | DSH home 下 `storages/voice-terminal-wake-tasks.json`（项目任务文件） | 到期时投递到当前 Voice（语音）主线关联的 DSH 会话 |

项目仍提供 `schedule_wake`（创建简易链路唤醒）、`cancel_wake`（取消简易链路唤醒）、`read_wake_reference`（读取任务参考）和 `publish_voice_event`（投递语音事件）。这些工具继续使用项目链路，不与官方会话任务工具重名。任意 DSH 根会话均可创建项目链路任务。

## 实施修订

- [项目唤醒任务插件注册入口](../integrations/dsh/voice-terminal-wake-tasks/src/index.ts)：将项目 CRUD 工具更名为 `wake_task_*`，保留其他语音事件工具及全部根会话挂载行为。未保留旧项目 CRUD 别名，因为它们会再次占用官方工具名。
- [项目任务服务](../integrations/dsh/voice-terminal-wake-tasks/src/task-service.ts)：删除官方 `schedule.json` 到项目任务表的导入逻辑。项目服务继续读取和迁移旧 Voice `wake-tasks.json`（旧语音链路任务文件），并原样恢复已有项目任务及历史。
- [DSH profile 生成与运行时](../voice_terminal/dsh/profile.py) 和 [运行管理器](../voice_terminal/dsh/runtime.py)：保留 profile package（配置清单）中用户选择的官方及其他可选 Bundle（功能包），同时更新受管插件版本；根据官方 Bundle 选择维护补丁，不再因官方功能启用而拒绝启动。
- [安装、构建及迁移脚本](../scripts/setup-dsh.ps1)、[发行 profile 初始化](../scripts/check-dsh-wake-migration.mjs)：移除原先把两类任务合并的迁移检查；仅保留旧 Voice 项目任务迁移。官方任务存储不作为项目导入来源。
- [便携桌面组装脚本](../scripts/assemble-desktop.py)：保留停机刷新、组件校验和提交失败恢复行为，部署新的程序目录，同时复用已验证的 DSH 组件。
- [影子智能体规则](../config/dsh/shadow-agent.rules.md)、README（项目说明）和 DSH 操作文档：解释如何按投递目标选择工具，避免同一提醒被重复创建。
- 新增同根工具注册和任务存储隔离回归，覆盖真实官方调度服务、官方工具注册函数、工具运行时及编译后的项目插件。

用户启用官方 Automation Bundle（自动化功能包）时，程序升级、profile 重建和第二次启动都保留此选项。该 Bundle 在干净默认 profile 中仍默认关闭；项目链路任务无需关闭或接管它。

## 部署组合与兼容边界

本次延续已选定的 DSH `dsh-v0.2.0-rc.2`（候选版），提交号 `639ed015397290b3745d163aafe02ffee4aa3f84`。桥接、唤醒任务和影子预设功能包仍为 `0.1.1`，Bridge（桥接协议）仍为 `2.0`。

便携应用为 `.runtime/delivery/VoiceTerminal/apps/20261005-dsh-task-coexist/VoiceTerminalDesktop.exe`（本机桌面程序）；DSH 组件仍为 `runtimes/dsh/639ed015-e7dd1315c977`。官方 DSH 组件与依赖闭包没有改版重建。本次重建项目包后刷新桌面程序及组件选择清单，正常退出并重启；第二次启动中桌面、DSH 宿主、官方功能选择、插件状态和桥接健康检查均通过。Node.js（脚本运行环境）实际版本为 `24.19.0`。

应用升级保留以下格式和设置：

- 官方会话任务格式及其存储文件原样保留；项目任务的任务标识、任务记录及投递历史不改写。
- 会话映射仍为 schema version（数据格式版本）`2`；历史映射不变，当前宿主绑定由正常启动重建。
- 原工作区、模型、推理等级、自定义影子预设、凭据、便携 `.env.local`（环境变量文件）、语音配置及历史保留。
- 源码快照保存于 `.runtime/dsh/upgrade.backup-0.2.0-rc.2-20261005-130637-257`；便携数据快照和旧程序、首次升级前的回退组合见本机[验收记录](../.runtime/dsh-coexist-20261005/acceptance.md)。

本机修复前用户选择了官方功能，但旧项目插件与之冲突。修复前的便携快照是数据和故障状态的取证材料，不能单独作为已经修复的可运行组合。若需从回退材料恢复服务，应使用首次升级记录中的旧 DSH、旧程序及对应数据组成完整一致的组合，不应混搭不同版本的宿主、程序与 profile。

## 验收结果

| 验收范围 | 结果 |
| --- | --- |
| 完整后端回归 | 671 项通过，17 项跳过；该轮包括新增 profile、运行时和 PowerShell 安装修订回归 |
| 安装与 profile 针对性回归 | 32 项通过；该数字与完整回归重叠，不额外累加 |
| 桥接插件 | 22 项通过；类型检查和测试产物构建通过 |
| 唤醒任务插件 | 4 项通过；包括工具名共存、相同根工具作用域增删改查、项目/官方存储隔离、重启恢复、失败重试与去重 |
| 官方任务界面 | DSH“自动化任务”页正常打开并读取已有官方任务 |
| 项目任务界面 | Voice Terminal“唤醒任务”页正常打开并读取已有项目任务 |
| 隔离源码宿主 | 同时启用官方和项目功能包；完整影子预设挂载成功，测试未发起外部模型推理 |
| 桌面生命周期 | 新程序正常退出，所属进程结束；第二次启动就绪且保留官方功能选择 |
| 启动日志 | 当前宿主无 `already registered`（重复注册）或 `did not activate`（插件未激活）错误 |
| 前端与官方 DSH | 沿用首次升级已通过的 64 项前端测试、类型检查、构建及 102 项官方测试；本次代码未改动前端或官方宿主组件 |
| 差异格式 | `git diff --check`（差异格式检查）通过 |

验证未创建生产测试任务，也未执行真实模型推理或语音播报。隔离回归使用临时任务存储和本机模拟回调；生产环境中的原有任务、历史和配置只读核对。
