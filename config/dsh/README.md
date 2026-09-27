# 项目 DSH 基线

Voice Terminal 使用的 DeepSeek Harness（DSH，DeepSeek 开发框架）固定为预发布版
`dsh-v0.1.7-rc.2`，提交号为
`477b4f420553e8a52c2fbccc464d7561b239c443`。升级必须显式更新锁定文件、许可证快照和回归检查，
不追踪浮动版本。

使用 PowerShell 7 的 `pwsh -NoProfile -File scripts/setup-dsh.ps1` 安装或修复项目运行时，使用
`pwsh -NoProfile -File scripts/setup-dsh.ps1 -ValidateOnly` 检查安装状态。官方源码和项目专属 DSH_HOME（DSH 数据目录）
位于 Git 忽略的 `.runtime/dsh/`；该数据目录与用户级 `~/.dsh` 隔离。升级时保留项目设置和会话数据，
并按安装脚本流程备份后再启动新版本。

正式 `shadow-agent`（Shadow 执行 Agent）由 `config/dsh/agent-presets/shadow-agent/` 维护。
安装脚本会把该配置包装成项目本地 `voice-terminal-shadow-preset` Bundle（预设功能包），安装到 Web
profile（网页配置），并通过 `agent-preset-registry`（Agent 预设注册器）的 `default`（部署默认预设字段）
设为默认项。DSH 0.1.7-rc.2 不再读取 DSH_HOME 中旧版 `.agent-presets/` 目录；该目录即使为回退保留，也不会注册预设。

项目 DSH Web Host（DSH 网页服务）仅监听 `127.0.0.1:3081`，由 Voice Server（语音服务）管理。
打开 Voice Terminal 页面 `http://127.0.0.1:5173/`，再使用顶栏的 DSH 控制台入口取得一次性访问地址；
直接访问 3081 不带 launch token（临时访问令牌）时不能完成网页授权。不要收藏、记录或分享带
`?token=` 的完整地址。

`voice-terminal-shadow-bridge`（语音终端插件桥接层）按 DSH v0.1.7 的公开插件接口构建。该版本使用
`configForms`（设置表单服务）提供插件设置页面，不再提供旧的 `settingsScope` 接口。页面注册受服务
生命周期控制，避免服务尚未就绪时插件启动失败。

完整的安装、设置、运行和故障说明见 [`docs/dsh-console.md`](../../docs/dsh-console.md)。许可证快照位于
`licenses/`。
