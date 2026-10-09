# 当前正式程序与分发安装器交付

产品版本沿用 `0.9.0`，以 `build`（构建编号）区分产物。开发和用户日常使用同一套正式程序；分发入口改为只负责展开及重装的独立安装器。架构与恢复规则见[统一正式程序与分发安装器](unified-product-installer.md)，本轮实测见[统一程序验收记录](unified-product-acceptance.md)。

## 测试入口

| 入口或命令 | 中文说明 |
| --- | --- |
| `.local/VoiceTerminal.exe` | 本地正式入口，无参数读取已发布本体 |
| `npm run desktop:start` | 与双击同一正式启动流程 |
| `npm run dev` | 正式入口显式传入 `--dev`（源码调试开关）及 `--source-root`（源码目录），使用当前业务源码和界面热更新 |
| `.local/VoiceTerminalUIDemo.exe` | 前端演示入口，使用隔离缓存，不启动业务服务 |
| `npm run dev:ui` | 重新构建并运行前端演示 |
| `.local/releases/installer/VoiceTerminalSetup.exe` | 正常分发安装器，内嵌统一正式程序包；只负责安装及重装 |
| `.local/build/program-package/program.zip` | 本地发布、安装器及后续正式更新共用的程序包 |

同一工作区只允许一个业务实例。测试普通启动与源码调试时，先从应用内退出当前业务实例，再切换模式。另一工作区的正式程序、界面演示和安装器可以同时打开。

命令行正式启动只等待根启动器完成交接；正式桌面进程随后独立运行，不会让 PowerShell 一直等待产品窗口退出。

安装器退出后，用户日常启动所选工作区根目录的 `VoiceTerminal.exe`（正式入口），桌面快捷方式也直接指向该入口。安装器不配置或下载组件；缺少环境或模型的正式程序进入组件管理页。再次选择已有工作区执行保留数据、模型、运行环境和登记的重装。

旧 `.local/releases/distribution-single-exe/VoiceTerminal.exe`（历史分发入口）不属于本轮测试及交付。旧系统工作区定位记录和旧私有网页引擎文件保留，但当前入口不再使用。所有正式实例统一使用系统 WebView2（网页运行引擎）。

## 重建与核对

通过应用内退出业务实例，关闭演示窗口，再执行：

```powershell
npm run desktop:publish # 构建、组装并事务发布正式本体，保留数据与组件
./scripts/distribute-desktop.ps1 -SkipProductBuild # 将刚发布的同一包嵌入正常安装器
./.local/Verify-Package.ps1 # 原生验证正式入口、宿主、业务、界面与支持文件
./.local/runtimes/python/python.exe -B -X utf8 scripts/test-backend.py -q # 后端测试
npm test # 前端与受管开发服务测试
npm run typecheck # 前端类型检查
npm --prefix integrations/dsh/voice-terminal test # 代理插件测试与类型检查
./.local/runtimes/python/python.exe -B -X utf8 scripts/check-doc-links.py # 文档链接检查
git diff --check # 差异格式检查
```

`npm run desktop:dist`（完整分发构建）可以独立重建正式包和安装器。若需要核验本地正式包与安装器逐文件一致，先发布，再使用 `-SkipProductBuild`（复用已发布程序包开关），避免比较不同构建。

安装器交付目录包含 `delivery.json`（交付记录）与 `build-stamp.json`（构建记录）。核对 `role=installer`（入口职责为安装器）、`qualification=false`（未开放自动验收参数）以及两份记录的 `program_package_sha256`（内嵌统一程序包摘要）与当前包一致。安装器的 `sha256`（可执行文件摘要）应与交付记录一致。

隔离自动验收另行构建，不能作为正常交付：

```powershell
./scripts/distribute-desktop.ps1 -SkipProductBuild -Qualification # 构建隔离验收安装器，正式程序包不变
./.local/runtimes/python/python.exe -B -X utf8 scripts/qualify-unified-product.py --setup .local/releases/installer-qualification/VoiceTerminalSetup.exe # 验收安装、重装、切换、迁移及恢复
```

隔离目录位于仓库外的同级 `测试`（现场验收目录）中，包含中文及空格。逐文件记录与启动报告保留在相应验收目录。自动验收只清理自己创建且目标匹配的受管快捷方式，不处理其他桌面文件。

## 用户测试顺序

1. 在本地正式程序检查原有设置与历史、组件管理、服务状态和正常退出；退出后用 `npm run dev`（源码热更新）检查同一工作区和组件位置。
2. 在演示入口核对界面交互。演示使用独立缓存，不操作正式用户数据。
3. 在正常安装器选择空目录，检查原有动画、进度与按钮；安装完成后确认安装器退出、正式组件管理页可以独立使用及再次启动。
4. 退出目标正式实例，再次选择该工作区重装，核对原有用户内容、模型、运行环境和组件登记；运行中的目标应提示退出后重试。
5. 在正式程序中验证已有工作区切换、空工作区初始化、整体迁移及失败恢复。完成搬迁后直接启动目标根目录的正式入口。

“应用更新”页面继续保留；在线更新按后续 GitHub（代码与发布托管平台）分发方案接入。本轮交付的安装器用于首次安装和保留用户数据的重装。
