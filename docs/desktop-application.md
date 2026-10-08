# 桌面开发与分发

当前正式启动、源码开发、演示、无界面后端、构建及覆盖发布使用统一的本机布局，详见[本地目录与覆盖发布](local-layout.md)。

本机正式入口为 `.local/VoiceTerminal.exe`（正式启动程序），演示入口为 `.local/VoiceTerminalUIDemo.exe`（前端演示程序）。两入口由同一次发布同步更新；组件路径只读取 `.local/portable.json`（便携配置）。在项目根目录执行常用命令：

```powershell
npm run dev                  # 源码业务开发，复用正式环境与用户数据
npm run dev:ui               # 演示开发，只启动界面服务
npm run desktop:build        # 构建到固定缓存，不换入正式程序
npm run desktop:publish      # 构建并事务换入当前正式程序及项目插件
.\.local\Verify-Package.ps1  # 完整安装摘要校验
```

发布前退出业务实例并关闭演示窗口。普通代码发布复用现有模型、后端环境、代理宿主与网页引擎；成功后回收事务旧组件，不生成大型备份或交付压缩包。运行组件变更也经同一事务换入固定位置，失败恢复原组合。

重要用户数据只通过 `scripts/local-admin.py backup`（滚动数据备份命令）保留一份；明确交付时通过 `scripts/package-desktop.py`（交付导出工具）覆盖最新包，排除真实用户数据和开发产物。两命令均使用 `.local/runtimes/python/python.exe`（正式解释器）；完整调用方式、备份白名单和恢复规则见[本地目录与覆盖发布](local-layout.md)。

桌面窗口负责单实例、托盘、加载状态、受管子进程及退出预算。关闭正式产品窗口隐藏到托盘；产品菜单或托盘中的退出操作会停止全部所属组件。演示只启动前端，不能启动语音服务、代理业务或麦克风。真实开发与正式程序共用业务端口和数据，启动前先退出另一个业务实例。

正式程序通过已构建界面运行；源码开发由受管前端服务提供热更新。网页资源、启动凭据和管理操作绑定本实例；普通浏览器访问服务地址只显示桌面入口说明。服务默认使用回环地址。

组件协议、模型选择和业务功能保持现有边界。组件启动结论与历史性能记录见[桌面验收](desktop-acceptance.md)、[启动调度](startup-optimization-plan.md)和[应用生命周期](application-lifecycle.md)。最新目录、更新和数据验收见[本地整理验收](local-layout-acceptance-20261008.md)。
