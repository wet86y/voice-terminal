# DSH（代理执行宿主）诊断脚本

这些脚本用于排查“项目 DSH 控制台”的安装一致性、会话日志完整性与渲染故障。摘要、日志扫描和会话检查以读取为主；真实唤醒会创建后台激活及会话记录，会话清理会备份和删除数据，隔离冒烟检查会创建并回收测试夹具。具体影响见下表，不能把整个目录视为只读工具。背景与结论见
[`docs/guides/dsh-plugin-maintenance.md`](../../docs/guides/dsh-plugin-maintenance.md)。

脚本默认从自身位置推导仓库根目录，可在任意工作目录下用 `node`（脚本运行环境）或项目私有 `python`（后端解释器）运行；切换命令工作目录不会改变脚本默认的项目目标。
`verify-console-state.mjs` 与 `capture-boot-errors.mjs` 需要 DSH 控制台处于打开状态，并通过
ego-browser（指定浏览器）的 CDP（浏览器调试协议）端点 `%LOCALAPPDATA%\ego-lite-linux\browser.json` 读取页面控制台。该专用浏览器不可用时，优先使用[项目代理调试入口](../../docs/guides/agent-debug.md)进行正式界面和业务检查，不把这两个脚本当作通用电脑控制入口。

| 脚本 | 用途 | 何时用 |
| --- | --- | --- |
| `compare-build-state-hashes.mjs` | 校验唯一便携配置和当前程序、运行组件的完整摘要 | `setup-dsh.ps1 -ValidateOnly` 通过但 DSH 仍拒启动时 |
| `find-null-kind-rows.mjs` | 扫描全部会话日志，列出会让客户端 `reason.kind` 抛错的 `turn/end` 记录 | 对话页空白时第一步 |
| `inspect-session.mjs` | 回放单个会话：逐帧解码、打印事件与 surface（交互面）元数据、统计回合结局 | 需要看某会话到底写了什么 |
| `verify-console-state.mjs` | 重新加载控制台，报告启动报错数、侧栏会话与对话页渲染字符数 | 修完或改动前端后验收 |
| `capture-boot-errors.mjs` | 抓取控制台报错的完整堆栈，并记录客户端实际订阅了哪些会话 | 需要把崩溃定位到具体会话时 |
| `trigger-shadow-wake.py` | 有状态操作：经 `shadow.debug.wake`（后台调试唤醒）创建真实 activation（后台激活），可能写入会话并请求已配置的模型 | 已确认当前目标并允许创建测试会话时才执行，不属于日志查阅 |
| `purge-poisoned-sessions.mjs` | 会写盘：备份并删除含 `reason: null`（结局缺失）的会话 | 仅在确认要清理污染会话时，默认 dry run（仅预览） |
| `upgrade-smoke.mjs` | 运行插件真实加载器、任务共存与资源释放回归，创建隔离夹具 | 验证代理升级，不替代正式程序真机验收 |
| `plugin-install-smoke.py` | 使用私有解释器和受管后端测试入口验证隔离安装、备份及正式组包 | 修改安装或迁移后；成功回收本轮夹具 |

## 典型流程

```powershell
# 1. 安装一致性：当前便携配置与组件清单必须闭合
scripts\setup-dsh.ps1 -ValidateOnly
node scripts\dsh-diagnose\compare-build-state-hashes.mjs

# 2. 全库是否还有 reason: null
node scripts\dsh-diagnose\find-null-kind-rows.mjs

# 3. 有状态端到端验证：先确认当前目标允许创建真实测试会话
.local\runtimes\python\python.exe -B -X utf8 scripts\dsh-diagnose\trigger-shadow-wake.py 240
node scripts\dsh-diagnose\inspect-session.mjs "<会话目录>\session.v4.jsonl.zstd"

# 4. 控制台验收（需先打开控制台页面）
node scripts\dsh-diagnose\verify-console-state.mjs
```

## 清理污染会话

```powershell
# 先看会删什么（不写盘）
node scripts\dsh-diagnose\purge-poisoned-sessions.mjs
# 确认后先退出业务：验证并替换唯一滚动用户数据备份，再删除
node scripts\dsh-diagnose\purge-poisoned-sessions.mjs apply
```

删除范围包括会话目录、`session_projcache` 条目、`workspace.json` 中的成员与归档引用，以及
`session-mappings.json` 中对应映射的 retired 标记。唯一数据备份包含会话及存储文件，可先在隔离目录校验后恢复。
