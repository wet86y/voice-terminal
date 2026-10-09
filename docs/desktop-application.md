# 桌面开发与分发

开发和用户日常运行同一正式程序；分发入口是独立安装器。架构、目录、重装与迁移规则见[统一正式程序与分发安装器](unified-product-installer.md)，成品与验收记录见[当前交付](current-release.md)。

本地正式入口为 `.local/VoiceTerminal.exe`（正式程序），安装器为 `.local/releases/installer/VoiceTerminalSetup.exe`（分发安装程序）。组件路径读取工作区 `portable.json`（第二版布局配置），均使用系统 WebView2（网页运行引擎）。`.local/VoiceTerminalUIDemo.exe`（前端演示入口）继续使用独立缓存。在项目根目录运行：

```powershell
npm run desktop:start        # 与双击同一正式入口，读取已发布静态本体
npm run dev                  # 显式源码调试，沿用正式环境与用户数据
npm run dev:ui               # 构建演示宿主，只启动界面服务
npm run desktop:build        # 构建到缓存，不换入正式本体
npm run desktop:publish      # 组装并事务换入统一正式程序包
npm run desktop:dist         # 构建内嵌同一正式包的独立安装器
.\.local\Verify-Package.ps1  # 原生固定本体摘要校验，无后端环境也可执行
```

发布前退出工作区业务实例。本体发布与安装器重装均保留用户数据、编号、模型、运行环境及其登记。组件维护在正式程序内执行；本轮不接入在线更新。备份白名单和完整环境导出工具见[本地目录与覆盖发布](local-layout.md)。

桌面窗口负责单实例、托盘、加载状态、受管子进程及退出预算。关闭正式产品窗口隐藏到托盘；产品菜单或托盘中的退出操作会停止全部所属组件。演示只启动前端，不能启动语音服务、代理业务或麦克风。真实开发与正式程序共用业务端口和数据，启动前先退出另一个业务实例。

正式程序通过已构建界面运行；源码开发由受管前端服务提供热更新。网页资源、启动凭据和管理操作绑定本实例；普通浏览器访问服务地址只显示桌面入口说明。服务默认使用回环地址。

组件协议、模型选择和业务功能保持现有边界。组件启动结论与历史性能记录见[桌面验收](desktop-acceptance.md)、[启动调度](startup-optimization-plan.md)和[应用生命周期](application-lifecycle.md)。最新目录、更新和数据验收见[本地整理验收](local-layout-acceptance-20261008.md)。
