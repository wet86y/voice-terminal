# 本机候选包与 OpenSSH 模拟验收（2026-11-18）

## 本轮边界

- 本报告记录一个 build-only（仅构建）测试候选。没有覆盖 `.local`（本机正式安装）、构建分发安装器或公开发行。
- 没有触发或绕过 UAC（用户账户控制），没有安装、启动或重启 OpenSSH（安全外壳协议）服务，也没有修改 Windows（操作系统）防火墙规则。
- 微软 OpenSSH 准备流程使用 PowerShell（Windows 命令行脚本）模拟命令回归；当前服务状态只读查询。模拟测试不等于真实安装。

## 候选程序包

- `npm run desktop:prepare`（准备统一程序包）完成，版本为 `1.0.0`，最新构建编号 `product-54be91eeaeeed684`。
- `.local/build/program-package/program.zip`（统一完整程序包）大小为 109,034,030 字节；压缩包含 462 项，其中 461 个固定本体文件和 1 份包清单；package-info.json（程序包信息）的构建编号、大小和摘要一致。SHA-256（文件摘要）为 `25de318e447fb3b3b006b2c00a1be3f2f31dd75554ecf64b99706efb92cfb2cf`。
- 包内 `Verify-Package.ps1`（正式包完整性校验）通过。本候选保留完整客户端与服务端本体，不是裁剪的客户端包。
- 现有 `.local` 正式安装仍为 `product-4ae83f2a70ecf810`；本轮没有本机覆盖发布。

## SSH（安全外壳协议）准备流程

- `system_feature::tests`（系统功能测试模块）共 29 项，其中 28 项通过、1 项只读实机检查按设计忽略。忽略用例随后单独运行并通过 1/1，只读取当前系统状态。
- 生成的 OpenSSH 安装脚本在模拟 PowerShell 环境执行。测试以同名模拟命令替代 `Add-WindowsCapability`（安装微软 Windows 可选功能）、服务和防火墙命令，覆盖已就绪、全新安装、已有默认规则、待重启、功能源失败、部分安装、规则查询失败及禁用结果无法复核等分支；没有执行真实系统命令。本轮另从 Rust 源码提取同一脚本，以进程内 PowerShell 模拟命令验证全新安装、已运行服务不重启、服务启动失败退出码为 1。安装当前用户 Rust 1.100.0 stable 工具链后，SSH 系统功能定向测试 28 项通过、1 项只读检查忽略；公共原生模块完整测试 362 项通过、14 项忽略；Tauri 原生集成 96/96；OPAQUE TCP 本机回环配对 1/1。Rust 格式检查通过。
- 当前 Windows OpenSSH 只读状态检查通过 1/1。实测状态为微软签名 `sshd`（OpenSSH 服务）9.5.6.4、运行中并监听 TCP 22；此检查未安装或更改服务。
- “检查 SSH 服务”界面新增长任务反馈：超过五分钟仍运行时显示继续跟踪；晚到成功会清除超时提示，晚到失败/取消会显示实际任务结果。两个 fake-timer（虚拟计时器）界面测试通过，不调用 UAC。

## 自动化验证

- 前端 `npm test`（前端与开发入口测试）：43 个测试文件、516 项通过；Node（JavaScript 运行环境）开发入口 3 项通过；修正语音设置到 AnifLive（本地语音引擎）组件卡的 prop（组件属性）名称，ProductApp（正式产品页）回归验证跳转后卡片展开。
- `npm run typecheck`（前端类型检查）通过；`cargo fmt --all -- --check`（Rust 格式检查）通过；`git diff --check`（差异空白检查）没有空白错误，只有仓库 LF/CRLF（换行符）配置提示。
- Tauri（桌面框架）原生测试：96/96 通过；资源任务测试 17/17、远程操作跨层白名单契约测试 1/1 通过。原生测试包含远程静态根路径预检、六位配对启动路径、远程零组件启动、SSH 授权取消与重试状态，以及 TTS（语音合成）上传回执恢复回归。
- `voice-desktop-common`（桌面公共原生模块）SSH（安全外壳协议）及原生音频特性下的完整测试套件 362 项通过、14 项按设计忽略；系统功能定向测试 28 项通过、1 项只读实机检查按设计忽略，后者另行只读执行并通过 1/1。
- Python（后端运行环境）远程网关、启动阶段、设备信任、文件传输、TTS 音频导入和产品接口组合回归 98/98 通过。

## 独立 B（远程客户端）启动

- 使用统一程序包正式入口，把完整程序安装到受管临时工作区 `.local/tmp/distributed-client-b-installed-product-54be91eeaeeed684`；安装退出码为 0，没有 UAC 对话框。较早失败的暂存目录 .local/tmp/distributed-client-b-installed-product-0ff23ea32f104fc4 已保留供诊断。B 的 `Verify-Package.ps1`（正式包完整性校验）通过。
- B 使用 `--mode remote`（远程配对模式）启动；新工作区配置为 schema 3（配置格式第 3 版）、`remote`（远程配对）及 `remote_client`（远程客户端），拥有独立设备编号，未配置服务端身份。
- B 没有 Python（后端解释器）、模型文件、DSH（后台执行宿主）运行时或组件安装收据；启动进程树有桌面宿主及 WebView2（网页运行引擎），没有 Python 或 `--worker-host`（工作进程宿主）进程。
- `data/state/workspace-ready.json`（产品页真实挂载记录）在启动期间曾确认 `ready=true`、`role=product`（正式产品页）且 `management_ready=false`（并非组件管理页）。这验证远程静态产品页通过桌面预检并实际挂载；Tauri 根路径 `/` 与 `/index.html` 均作为受控远程入口处理；B 正式退出后该启动记录当前不存在。
- 通过正式 `--exit`（程序退出参数）退出后，B 工作区下剩余桌面和 WebView2 进程为 0。
- 可视验收受 Windows PIN（个人识别码）锁屏影响。截图显示锁屏，没有输入或绕过 PIN；因此没有验证实际页面布局、文字可读性或按钮状态。此前候选曾出现“桌面启动程序尚未就绪”的无障碍树文本；本候选通过原生 `workspace-ready.json` 证明前端真实挂载，但仍不能替代解锁后的视觉检查。

## 尚未完成

- 本候选没有覆盖安装到本机 A（服务端），没有开启广播或更改网络规则。
- 使用当前已安装 A（构建 `product-4ae83f2a70ecf810`）做模式切换回归：原配置是 `local/standalone/broadcast=false`（本地运行/独立设备/关闭广播）。第一次 `local → remote → local`（本地→远程→本地）正式参数切换完整通过；切回本地后健康状态从 `STARTING`（启动中）约 35 秒后转为 `IDLE`（空闲）、`can_start=true`（可启动）、无启动错误，音频、唤醒、识别、语言模型、TTS、影子代理和分析服务就绪，`input_owner=none`（未开始监听）。随后为记录远程模式无本地 worker，再把 A 切到 `remote/remote_client/broadcast=false`；该次 `--mode remote` 成功停止本地 Python 和 `--worker-host`（工作进程宿主）进程。再执行恢复 `--mode local --broadcast off` 时，命令在启动前被执行环境安全审核以“rejected by policy”（策略拒绝）拒绝；没有触发 UAC，也未尝试其他方式绕过。A 通过正式 `--exit`（程序退出参数）正常退出，**当前 `.local` 持久模式仍为 `remote/remote_client`、广播关闭、没有活动桌面或本地语音 worker；第二次回切未验收。**
- 没有进行局域网搜索、六位码配对、SSH 桥接、客户端接管/回退、远程业务设置落盘、音频文件上传与服务端音色登记、供应商 TTS 请求或 Windows 扬声器播放。
- A/B/C（多实例）真实连接和双机语音回环均未验收。A 的 `local→remote→local` 参数切换曾完整通过一次；第二次远程记录后的本机恢复命令被执行环境拒绝，当前 A 仍处于远程模式。最新候选证明完整包构建、隔离安装、远程配置、产品页挂载和无组件启动，不代表配对或远程业务可用。
