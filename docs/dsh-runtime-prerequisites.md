# DSH（代理执行宿主）分发依赖核对

核对对象为本项目锁定的官方 Windows x64（64 位操作系统）桌面附件 `0.2.0-rc.2`（版本号），兼容源码标签为 `dsh-v0.2.0-rc.2`（版本标签），兼容提交为 `639ed015397290b3745d163aafe02ffee4aa3f84`（源码版本）。以下结论适用于官方桌面组件；从源码构建和 npm（包管理器）安装方式的依赖要求不同。

| 工具或环境 | 当前项目使用方式 | 分发检查范围 |
| --- | --- | --- |
| WebView2（微软网页引擎） | 系统自动更新版，由原生入口检测并提供官方安装引导 | 必需的系统前置环境 |
| Node.js（脚本运行环境） | 官方桌面组件自带，网页宿主使用随包 Electron（桌面框架）的脚本执行模式 | 检查组件完整性，无需检测全局安装 |
| pnpm（包管理器） | 官方桌面命令入口把随包工具传入插件管理器 | 检查组件完整性，无需检测全局安装 |
| npm（包管理器） | 当前基础启动与官方桌面插件管理不直接调用全局工具 | 不作为程序启动前置条件 |
| Python（运行环境） | Voice Server（语音服务）下载维护；代理子进程搜索路径优先指向项目私有解释器 | 由语音组件校验，无需检测全局安装 |
| PowerShell（命令环境） | 优先第 7 版，可回退 Windows（操作系统）自带第 5.1 版 | 第 7 版不是必需项，不设为额外全局安装前置 |
| Git（版本控制工具） | 部分 GitHub（代码托管平台）插件来源及代码操作需要 | 按功能检查；不是基础启动的必需项 |

原适配直接运行普通 DSH（代理执行宿主）命令入口，没有传入随包包管理器，插件安装可能落到全局 `pnpm`（包管理器命令）。本次改用官方桌面命令入口，它同时接入包管理器与 Office（文档处理）组件解析。原适配还没有为代理子进程指定 Python（解释器）搜索路径；本次从当前工作区的 `portable.json`（本地组件路径记录）读取解释器位置并加入搜索路径。

因此，WebView2（网页引擎）校验后没有发现还需列为基础启动拦截项的全局依赖：Node.js（脚本运行环境）与 pnpm（包管理器）随 DSH（代理执行宿主）组件提供，Python（解释器）由项目维护，PowerShell（命令环境）可回退至 Windows（操作系统）自带第 5.1 版。Git（版本控制工具）只影响部分外部插件及代码操作，应在用户使用相关功能时按需检查并提供引导。本轮完成依赖核对与内部工具接入，尚未实现 Git（版本控制工具）的按功能报错及下载跳转流程。

官方依据：[桌面运行包说明](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/apps/desktop/README.md)、[随包包管理器接入入口](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/apps/desktop-host/src/cli.ts)、[命令环境选择及回退](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/packages/shell/pwsh-local/src/resolve.ts)、[外部插件代码来源检查](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/packages/boot/plugin-manager/src/github-connection.ts)。
