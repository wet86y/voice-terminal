# 项目隔离的 DSH 基线

本目录定义 Voice Terminal 使用的 DeepSeek Harness（DSH）可审计基线。当前锁定候选版
`0.1.5-rc.2`（tag `dsh-v0.1.5-rc.2`，commit
`fb2c4b9e698e30edb738bca4cf0618587db7d203`）。DSH 仍是 developer preview；升级必须显式
修改锁文件、复核许可证与预设，并重新运行测试，绝不追踪浮动版本。

`scripts/setup-dsh.ps1` 将官方源码和项目专属 DSH_HOME 安装到 Git 忽略的
`.runtime/dsh/`。它不会读取或写入用户的 `~/.dsh`。受管 Web 进程只监听
`127.0.0.1:3081`，由 Voice Server 启停；`3080` 保留给已有的全局 DSH。不要把项目端口暴露到局域网、公网或反向代理。

当前 `shadow-agent` 是隔离验证预设，不是正式 Python Shadow 会话的实时镜像。它只装载
官方内置的文件搜索、Web 搜索/抓取、UI 提问、原生工具展示和基础压缩组件；没有文件写入、
Shell、代码执行、任务、子代理、Skills、MCP、Hooks 或第三方/自定义插件。

首次进入控制台后选择本仓库根目录作为 workspace，创建专用会话，并发送：

> 初始化 Voice Terminal Shadow 验证会话。只检查当前预设、可见工具和只读边界，不执行项目修改。

源码：<https://github.com/deepseek-ai/deepseek-harness>
许可证快照：`licenses/LICENSE` 与 `licenses/THIRD_PARTY_NOTICES.md`。
