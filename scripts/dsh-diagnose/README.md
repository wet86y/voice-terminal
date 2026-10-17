# DSH 诊断脚本

这些脚本用于排查「项目 DSH 控制台」的安装一致性、会话日志完整性与渲染故障。它们都是只读
检查，除 `purge-poisoned-sessions.mjs` 外都不写盘。背景与结论见
[`docs/guides/dsh-plugin-maintenance.md`](../../docs/guides/dsh-plugin-maintenance.md)。

脚本默认从自身位置推导仓库根目录，可在任意工作目录下用 `node` / `python` 直接运行。
`verify-console-state.mjs` 与 `capture-boot-errors.mjs` 需要 DSH 控制台处于打开状态，并通过
ego-browser 的 CDP 端点（`%LOCALAPPDATA%\ego-lite-linux\browser.json`）读取页面控制台。

| 脚本 | 用途 | 何时用 |
| --- | --- | --- |
| `compare-build-state-hashes.mjs` | 校验唯一便携配置和当前程序、运行组件的完整摘要 | `setup-dsh.ps1 -ValidateOnly` 通过但 DSH 仍拒启动时 |
| `find-null-kind-rows.mjs` | 扫描全部会话日志，列出会让客户端 `reason.kind` 抛错的 `turn/end` 记录 | 对话页空白时第一步 |
| `inspect-session.mjs` | 回放单个会话：逐帧解码、打印事件与 surface 元数据、统计回合结局 | 需要看某会话到底写了什么 |
| `verify-console-state.mjs` | 重新加载控制台，报告启动报错数、侧栏会话与对话页渲染字符数 | 修完或改动前端后验收 |
| `capture-boot-errors.mjs` | 抓取控制台报错的完整堆栈，并记录客户端实际订阅了哪些会话 | 需要把崩溃定位到具体会话时 |
| `trigger-shadow-wake.py` | 经产品自身的 `shadow.debug.wake` 触发一次真实影子 activation | 需要端到端验证影子链路时 |
| `purge-poisoned-sessions.mjs` | **会写盘**：备份并删除含 `reason: null` 的会话 | 仅在确认要清理污染会话时，默认 dry run |

## 典型流程

```powershell
# 1. 安装一致性：当前便携配置与组件清单必须闭合
scripts\setup-dsh.ps1 -ValidateOnly
node scripts\dsh-diagnose\compare-build-state-hashes.mjs

# 2. 全库是否还有 reason: null
node scripts\dsh-diagnose\find-null-kind-rows.mjs

# 3. 需要端到端
python scripts\dsh-diagnose\trigger-shadow-wake.py 240
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
