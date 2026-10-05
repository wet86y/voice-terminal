# 独立桌面应用、开发与便携分发

本项目采用 Tauri 2（桌面应用框架）、Vue 3（页面框架）、TypeScript（类型化脚本语言）和 Vite（前端构建工具），保留 Python（后端运行环境）。正式页面与视觉预览共享 `apps/frontend/src/interface/` 的组件及设计变量；正式页面的业务状态由 `useProductAdapter.ts`（业务适配器）维护，控件与表单生命周期集中在 `useProductControls.ts`（控件组合逻辑）。

2026 年 10 月 5 日，主产品、样例预览和界面验收统一为桌面入口，旧浏览器启动器已撤销。当前范围与验证状态见[第一阶段记录](desktop-frontend-consolidation.md)，早期便携候选的历史结果见[实施验收记录](desktop-acceptance.md)。在线更新与发布签名留在后续专题。

当前交付收敛为两个入口：`.runtime/delivery/VoiceTerminal/VoiceTerminal.exe`（完整数据正式程序）和 `.runtime/delivery/VoiceTerminalUIDemo.exe`（纯前端演示程序）。正式程序在原目录更新并沿用完整数据；演示程序从当前源码加载共享界面，用于下一阶段的界面修订。演示入口需要本机开发工具，不作为客户机便携包内容。

## 启动与退出

便携目录通过根目录 `VoiceTerminal.exe`（精简启动程序）启动。首次启动创建独立用户目录，补齐缺失的默认模板，然后进入待机。在线语言模型与语音合成仍需用户配置网络服务和凭据。

- 再次启动唤回现有窗口。
- 关闭窗口隐藏到托盘；托盘无法建立时保留窗口。
- 应用菜单、托盘和“设置 → 系统运行”的“退出应用”协调结束整套所属服务。
- 正常退出预算约 15 秒。Python（后端运行环境）服务管理器先正常关闭语音及其委托进程，再关闭识别与分析；超时由桌面持有的 Job Object（Windows 作业对象）回收。外壳异常结束也会关闭其拥有的 Python（后端运行环境）进程子树。
- 应用使用明确的可执行文件和参数列表，支持中文、空格路径及从其他工作目录启动。固定网页运行时支持本地磁盘；不支持 UNC（网络共享）路径。

正式程序的职责只有一份：根入口定位版本组合后启动桌面并退出；桌面负责窗口、托盘、单实例及服务管理器；服务管理器负责本地网页、识别、分析及语音服务；语音服务负责声纹与 DSH（代理执行宿主）。前端发送原有业务命令，不另建服务重启策略。

桌面与管理器使用私有标准输入／输出管道。`protocol`（协议格式）为 1；`kind`（消息类型）包含 `ready`（界面入口就绪，含单次入口凭据）、`start`（外壳已接管）、`shutdown`（退出请求）、`stopped`（退出完成）、`exit_requested`（业务请求退出）、`startup_result`（启动检查结果）和 `desktop_process`（桌面进程身份）。业务进程只有在外壳接管后才启动。

桌面权限限定到主窗口和本实例实际的本机网页来源。`DesktopPort`（桌面交互接口）只提供唤回窗口、打开日志目录、打开受限代理控制台及退出。代理控制台通过系统默认浏览器打开 DSH（代理执行宿主）网页；桌面入口校验固定本机地址、端口和访问令牌，不再创建独立桌面窗口。控制台网页不具有主窗口原生命令权限。

## 开发入口

在源码根目录安装前端依赖。真实业务开发还需要已准备好的 Python（后端运行环境）3.12 x64；样例只需 Node.js（脚本运行时）、Rust（原生构建工具）、MSVC（微软原生编译工具）及 WebView2（渲染引擎）。

```powershell
npm run dev       # 真实业务桌面热更新；别名 desktop:dev（桌面开发）。
npm run dev:ui    # 独立桌面样例热更新；别名 desktop:ui（桌面样例）。
npm run desktop:start # 静态界面桌面启动。
npm run build     # 正式界面、原生主程序和根启动程序；别名 desktop:build（桌面构建）。
npm run desktop:publish # 构建并更新原正式程序，同时同步可双击的演示启动程序。
npm run frontend:build # 内部静态界面构建，不提供网页预览。
.\scripts\start-desktop.ps1 -Dev -Python 'D:\运行环境\python.exe'
.\scripts\start-desktop.ps1 -Diagnostic -Python 'D:\运行环境\python.exe'
.\scripts\start.ps1 -Python 'D:\运行环境\python.exe' # 无界面后端调试。
```

源码开发命令启动前始终执行增量原生构建，静态开发命令还构建当前界面。双击入口直接运行已构建的主程序，不承担构建职责。默认读取 `.runtime/delivery/VoiceTerminal/portable.json`（正式便携配置）及 `data/state/active.json`（正式启用组合），复用完整用户数据、Python（后端运行环境）、模型、DSH（代理执行宿主）和固定渲染引擎；只把程序源码切换为当前工程。`InstallRoot`（正式安装目录参数）可覆盖该安装位置；`DataDir`（业务数据目录参数）仍可显式覆盖数据位置，缺少正式安装且未指定数据时明确报错，不创建隔离产品。正式与源码业务调试使用独立渲染缓存，但共用业务数据和端口，启动调试前应退出正式程序。`WebView2`（固定渲染引擎目录参数）可明确覆盖渲染引擎。

真实开发固定 5173，桌面样例固定 5174；冲突时失败，不连接未知服务。只有便携静态界面服务在冲突时选择空闲端口。样例使用 `--ui-preview`（仅调试构建的样例模式），在业务启动上下文之前分支，不解析业务解释器、模型或真实配置；缓存独立，关闭窗口回收所属服务，可与正式程序并行。页面、状态和浮层通过样例工具切换，语音事件回放也在此查看。

双击 `VoiceTerminalUIDemo.exe`（演示启动程序）直接启动 `apps/desktop/target/debug/VoiceTerminalDesktop.exe`（已构建的调试宿主），参数固定为源码根目录及 `--ui-preview`（演示模式）。根入口立即退出，不启动脚本或构建任务，也不建立独立过渡窗口。原生宿主先打开最终主窗口，待其本地页面加载完成，再从后台线程启动 Node.js（前端脚本运行时）服务；服务准备好后在同一窗口进入共享界面。正式程序使用相同的窗口优先顺序加载业务服务。主页面刷新不会重复启动服务，退出期间不建立新的服务。重复双击由原生宿主唤回当前窗口。

`npm run dev:ui`（演示开发命令）仍先增量构建原生宿主，再进入同一演示模式，用于修改原生源码后的开发。演示主程序未构建时，双击入口明确提示先执行开发或发布命令。演示入口输出记录到 `.runtime/desktop-ui-preview/launcher.log`（演示启动日志），前端服务输出记录到 `frontend-dev.log`（前端开发服务日志）；源码开发命令使用 UTF-8（统一字符编码）输出。

在 `apps/frontend/src/interface/`（共享界面组件）和 `design.css`（共享设计样式）中修订界面，演示通过 HMR（热更新）展示；正式程序继续使用上一次构建的静态界面。确认修改后退出正式程序并关闭演示窗口，执行 `npm run desktop:publish`（更新正式程序），重新启动正式入口验证实际业务。正式组装只收集产品页面，不带演示启动程序、场景或开发服务。

### 桌面会话与界面权限

宿主通过私有管道发送 `launch_token`（60 秒一次性入口凭据），桌面导航到 `/desktop-entry`（受控入口）建立按实例区分的 Cookie（会话凭据），然后替换地址进入产品或样例。普通根路径只显示说明，不签发会话。界面资源、运行配置和宿主管理接口均要求有效会话；开发服务在外层同时验证普通请求和 HMR（热更新）升级连接。

入口完成原生 `desktop_status`（桌面状态）验证后才动态导入界面。`frontend_mode`（前端模式）只允许 `product`（产品）和 `ui-preview`（样例）；`development`（开发标识）表示调试构建。产品握手后加载宿主配置，业务接口与实时通信地址来自宿主当前状态，没有浏览器默认地址回退。样例仅获状态和退出能力，不连接真实配置、业务服务、存储或麦克风。

正式构建生成 `desktop-manifest.json`（界面产物清单），组装时验证产品入口及其动态依赖闭包，拒绝样例、额外入口和陈旧资源。开发辅助服务不进入正式程序组件。

正式设置包含唤醒与静默、人格与指令、声纹、唤醒任务、模型管理、本地计算和系统运行七个区域。三个静默／唤醒保存动作保持独立；历史静默入口映射到合并页。控件状态由页面根层持有，切换设置页保留草稿。对话按 `seq`（服务端顺序号）排序，继续使用会话、轮次与播报标识及真实播放交付状态；最新 200 条已结束条目之外仍保留活动条目。

## 发行目录与本地记录

```text
VoiceTerminal/
├─ VoiceTerminal.exe                 # 精简启动入口
├─ portable.json                     # 便携配置与初始组合
├─ Verify-Package.ps1                # 离线摘要校验
├─ apps/<构建标识>/                  # 原生程序、前端、后端及默认模板
│  └─ release.json                   # 发布方清单
├─ runtimes/python/<环境标识>/       # 官方嵌入解释器与闭合依赖
├─ runtimes/webview2/<环境标识>/     # 固定网页渲染引擎
├─ runtimes/dsh/<产物标识>/          # 宿主、物理依赖目录及其 Node（脚本运行环境）
├─ models/<资源标识>/                # 只读模型资源
└─ data/                            # 用户数据
   ├─ config/                       # 设置、注册表及配置覆盖
   ├─ persona/                      # 人格及用户资料
   ├─ speaker/                      # 声纹资料
   ├─ dsh/                          # 代理配置、任务和持久状态
   ├─ history/                      # 会话身份与已有归档
   ├─ logs/                         # 日志
   ├─ cache/                        # 模型缓存、关键词和字节码
   ├─ imports/                      # 用户导入文件
   ├─ .env.local                    # 私密配置；不得提交或进入发布包
   └─ state/                        # 本地启用、实例与迁移记录
```

本轮 Node.js（JavaScript 运行环境）随 DSH（代理执行宿主）组件一同封装；清单保留单独运行环境路径字段，后续可独立拆分，不要求多仓库。

`portable.json`（便携配置）的 `data_dir`（用户数据位置）可以是相对解压根目录的路径或绝对路径。其他常用字段如下：

| 字段 | 含义 |
| --- | --- |
| `schema`（记录格式） | 当前为 1 |
| `app`（程序路径） | 当前程序目录，相对应用根目录 |
| `python`（解释器路径） | 当前独立解释器，相对应用根目录 |
| `models`（模型路径） | 当前模型目录，相对应用根目录 |
| `webview2`（固定渲染引擎） | 固定引擎目录；为空时需系统运行时 |
| `dsh`（代理宿主） | 闭合的代理运行目录 |
| `node`（独立宿主解释器） | 可选；本轮由代理组件携带 |
| `external_python`（外部解释器） | 仅环境辅助退路使用存在的绝对路径 |
| `external_models`（外部模型） | 仅环境辅助退路使用存在的绝对目录 |

首次成功完成入口检查后，从初始组合生成 `data/state/active.json`（本地启用记录）。它记录实际组合，**不是发布清单**。已有记录优先于初始组合；手动切换程序时停机修改此记录，并执行离线校验。快速开发期使用唯一构建标识和简短显示版本；协议或数据格式只在兼容性发生变化时提升格式号。

程序、运行环境和模型目录属于只读发布文件。默认模板位于程序包，已有用户文件不会被首次初始化覆盖。唤醒词及临时词典写到 `data/cache/kws`，模型加载缓存与字节码写到 `data/cache`。DSH（代理执行宿主）自研插件属于程序包，启动时同步到数据区受管配置档；第三方依赖仅由数据区目录联接指向运行环境。应用搬迁后会重建联接目标，不能将构建机的绝对联接放入发布包。

宿主有两万多份依赖文件。启动时检查发行版本、桥接协议、解释器、入口及调度模块的摘要，再以实际启动健康检查验证依赖加载；构建与 `Verify-Package.ps1`（整包离线验证）执行完整文件大小、摘要和文件集合验证。正常启动不会扫描全部依赖文件，也不声称已经再次完整验证了整个发行目录。手动更换组件后应先执行整包校验。

要改变数据位置，先退出应用，将整个数据目录复制并校验到新位置，再修改 `portable.json`（便携配置）的 `data_dir`（数据位置）。运行中不切换目录。跨 Windows（操作系统）账户迁移后，Credential Manager（系统凭据管理器）里的密钥需要重新录入。

## 旧数据导入

迁移必须停机。命令会复制、逐文件校验、记录启用结果，并保留旧来源；目标存在同名文件时拒绝覆盖。旧配置、私密配置文件、人格、声纹、模型注册表、唤醒任务、连续性与邮箱状态、代理用户状态和已存在的会话归档均在迁移范围内。源码依赖目录、受管插件和临时缓存不迁移。

```powershell
# 源码环境执行；目标应为空。
& 'D:\已准备环境\python.exe' -m voice_terminal.distribution migrate `
  --source 'D:\旧项目' --data 'D:\VoiceTerminal\data'

# 发行环境执行；程序路径与解释器路径从便携配置／启用记录取得。
& '.\runtimes\python\<环境标识>\python.exe' -B -X utf8 `
  '.\apps\<构建标识>\scripts\backend-entry.py' voice_terminal.distribution migrate `
  --source 'D:\旧项目' --data '.\data'
```

迁移结果保存为 `state/migration.json`（迁移记录）。当前未持久化的会话不能从文件恢复。程序组合回退和用户数据恢复分别处理：切回旧程序不会自动恢复数据；后续修改数据格式必须先备份涉及文件并记录迁移。

## 构建运行环境与完整包

构建机准备 Node.js（前端及宿主构建环境）、锁定的 pnpm（宿主依赖构建工具）、Rust（原生构建工具）、MSVC（微软原生编译工具）及完整的 Python（构建解释器）3.12 x64。客户机不运行依赖安装或源码编译。

根启动入口单独使用 `crt-static`（静态链接原生运行库）构建，避免在选择随包运行环境之前依赖系统安装的 C++（原生运行库）。构建后应检查入口二进制的动态依赖；Python（后端运行环境）与文档转换引擎仍各自携带原样收集的微软可再分发库。[Rust 官方运行库链接说明](https://doc.rust-lang.org/reference/linkage.html#static-and-dynamic-c-runtimes)

Python（后端运行环境）组件除官方可嵌入解释器和锁定依赖外，还从已授权构建机的微软可再分发目录原样收集 C++（原生运行库）。不能从系统目录复制来代替合法、可追溯的构建输入。依赖包自带许可文件保留，运行环境附原生库来源、摘要和许可链接；发行方按对应构建工具许可进行分发。

```powershell
$BuildPython = 'D:\构建环境\python.exe'
$VcRedist = 'D:\已授权构建工具\VC\Redist\MSVC\<版本>\x64'
.\scripts\build-desktop.ps1

# 优先尝试当前统一组合。显卡张量轮子较大，包体或兼容性不合适时收敛到 cpu（中央处理器）。
& $BuildPython -X utf8 scripts/build-runtime.py --profile cu128 --vc-redist $VcRedist --output '.runtime/package-build/python-unified'
& $BuildPython -X utf8 scripts/build-runtime.py --profile cpu --vc-redist $VcRedist --output '.runtime/package-build/python-cpu'

# 收集宿主生产依赖闭包；输出物理目录，终端不需要 Git（源码工具）或 pnpm（依赖构建工具）。
& $BuildPython -X utf8 scripts/build-dsh-runtime.py `
  --node 'C:\Program Files\nodejs\node.exe' --vc-redist $VcRedist --output '.runtime/package-build/dsh-runtime'

# 从微软下载 x64 固定版本 CAB（运行时压缩包）后准备目录及读取／执行权限。
.\scripts\prepare-webview2.ps1 -Cab 'D:\构建输入\webview2-x64.cab' -RuntimeDir '.runtime/package-build/webview2'

# WebView2 参数指向实际包含 msedgewebview2.exe 的目录。
& $BuildPython -X utf8 scripts/assemble-desktop.py --output '.runtime/delivery/VoiceTerminal' `
  --python-runtime '.runtime/package-build/python-cpu' `
  --dsh-runtime '.runtime/package-build/dsh-runtime' `
  --webview2 '.runtime/package-build/webview2/<实际运行时目录>' `
  --models 'D:\模型\voice-terminal' --zip
```

Python（后端运行环境）构建器使用官方 3.12.10 可嵌入发行和锁定版本约束收集 wheel（预编译依赖包），生成带 SHA-256（文件摘要）的依赖锁。构建机轮子生产器独立锁定 pip（依赖安装工具）、setuptools（依赖构建工具）及 wheel（依赖归档工具），避免旧依赖被更新的隔离构建后端错误打包。用户环境由这些轮子安装到独立目录，验证其自身依赖闭合、实际原生库导入、中央处理器张量操作、音频设备查询及中文／空格目录搬迁后的再次导入。已收集轮子可用 `--offline`（离线构建）重建；构建工具虚拟环境不会进入发行包。

若这一方案在其他系统出现实际兼容问题，使用 `--external-python`（外部解释器）明确指定已准备的解释器，按检测脚本报告依赖；模型也可通过 `--external-models`（外部模型）明确引用外部目录。这类产物是环境辅助包，不能声明免配置便携验收完成。不复制开发机 venv（虚拟环境）冒充可移动运行时。

离线检测与校验：

```powershell
& $BuildPython -X utf8 scripts/check-desktop-environment.py `
  --python 'D:\待检测环境\python.exe' --models 'D:\模型' --webview2 'D:\固定网页运行时'

# 在完整包内运行，校验程序、环境、模型及当前组合；用户数据不参与发布文件验证。
powershell -NoProfile -ExecutionPolicy Bypass -File .\Verify-Package.ps1
```

构建器生成完整压缩包及组件压缩包，包含大小、逐文件 SHA-256（文件摘要）、构建标识和所需组件清单。完整包排除用户数据、个人凭据和开发环境目录。清单摘要用于离线完整性检查；本轮尚无发布签名，不能作为在线更新信任根。

若先组装目录并完成运行验收，之后再封装，可使用独立命令；它支持自定义的数据位置，并在进入目录前就排除用户数据及其中的受管联接：

```powershell
& $BuildPython -X utf8 scripts/package-desktop.py --root '.runtime/delivery/VoiceTerminal' `
  --output '.runtime/delivery/VoiceTerminal.zip' --components '.runtime/delivery/VoiceTerminal-components'
```

本机正式程序通过 `npm run desktop:publish`（构建并更新正式目录）更新，默认覆盖原 `.runtime/delivery/VoiceTerminal`（正式安装目录）的启动入口并启用新程序，复用已校验的运行环境和模型，保留完整用户数据。该命令不会组装另一份空白数据产品。刷新使用新的构建标识，先暂存和校验，再启用；可捕获的提交错误会恢复旧入口和记录。它不承担断电恢复或终端在线更新事务。旧程序组件目录保留以便构建检查；最终完整包只收入当前启用程序和所需组件。下面是仅组装更新的底层命令：

```powershell
& $BuildPython -X utf8 scripts/assemble-desktop.py --output '.runtime/delivery/VoiceTerminal' `
  --build '20261004-desktop' --refresh-program
```

升级 DSH（代理执行宿主）时，在同一停机刷新命令中传入 `--dsh-runtime`（新宿主运行产物路径）。
构建器先校验新组件的完整清单、版本及提交号与新程序锁版一致，再安装到新的受管目录，
同步新程序的 `release.components`（组件清单）、`active.json`（当前启用记录）及 `portable.json`（便携默认配置）。
组件校验失败或选择记录提交失败时恢复旧组合；旧宿主组件保留。首次启动可能迁移宿主用户配置，
因此应在停机后另外备份 `data/dsh`（宿主数据目录）及相关语音设置，回退涉及数据时一并恢复该快照。

```powershell
& $BuildPython -X utf8 scripts/assemble-desktop.py --output '.runtime/delivery/VoiceTerminal' `
  --build '20261005-dsh020rc2' --refresh-program `
  --dsh-runtime '.runtime/package-build/dsh-020rc2-20261005-v3'
```

独立封包工具只收录发布清单声明的程序、组件、根入口及使用说明。归档内的 `portable.json`（便携配置）采用经过校验的实际组合，数据位置默认为相对 `data`（数据目录）；工作目录里的外置数据位置和未知根目录文件不会成为发布内容。

发布与运行记录的主要字段：

| 字段 | 含义 |
| --- | --- |
| `kind`（组件类别）、`build`（唯一构建标识） | 区分程序、解释器、宿主、模型和网页运行时 |
| `files`（文件清单） | 逐文件记录 `path`（相对路径）、`size`（字节大小）、`sha256`（摘要） |
| `components`（所需组件） | 程序要求的组件目录、构建标识及 `manifest_sha256`（组件清单摘要） |
| `root_files`（根目录发布文件） | 根入口与离线验证脚本的完整性记录 |
| `compatibility`（兼容要求） | 桌面控制协议、数据格式与最低系统要求 |
| `external_requirements`（外部环境要求） | 仅用于明确标记辅助环境退路 |
| `portable_candidate`（便携候选） | 已收集独立环境和模型；不表示所有目标机器验收已完成 |
| `version`（宿主版本）、`commit`（源码提交） | 代理宿主构建输入身份 |
| `bridge_protocol`（业务桥接协议） | 宿主与程序的业务协议，当前为字符串 `2.0` |
| `node`（宿主解释器）、`entry`（宿主入口）、`schedule`（调度模块） | 宿主组件内的受管相对路径 |
| `vc_runtime`（原生运行库）、`office_vc_runtime`（文档转换原生运行库） | 原样收集的微软运行库来源及文件摘要 |
| `signed`（已有发布签名） | 本轮为否，后续更新专题落实 |

固定 WebView2（网页渲染引擎）120 及以上在 Windows 10（操作系统）的未打包桌面程序中需要对引擎目录授予应用容器读取／执行权限。桌面在首次启动及引擎位置改变后调用系统权限工具准备，并在数据区记录结果；构建准备脚本也能独立执行该步骤。普通用户的新解压位置仍需验证。权限准备失败时可停机将便携配置及本地启用记录的 `webview2`（固定引擎）设为空，明确选择系统 WebView2（网页渲染引擎）退路，再执行桌面启动检查。系统基线为 Windows 10 22H2／Windows 11 的 x64 版本。[微软固定版本分发说明](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)

## 验收与后续接口

```powershell
& $BuildPython -m pytest -q tests/test_application_host.py tests/test_portable_paths.py `
  tests/test_product_api.py tests/test_product_sidecar.py tests/test_session_storage.py `
  tests/test_dsh_runtime.py tests/test_speaker_production.py tests/test_keywords.py
npm test
npm run typecheck
npm run build
git diff --check
```

真实目录的进程验收和一小时待机采样可以使用以下脚本。它读取无凭据的实例登记与启动结果，依据程序路径、父子关系、实例标识和进程创建时间确认归属；异常退出测试只结束该实例的桌面外壳。报告不含凭据或会话正文。正常退出测试先等待用户或界面自动化点击真实桌面退出按钮，然后核验所属进程回收，窗口关闭和托盘恢复仍需实际窗口交互验收。

```powershell
& $BuildPython -X utf8 scripts/check-desktop-lifecycle.py --root 'D:\VoiceTerminal' `
  --action launch --cwd 'C:\Windows\Temp' --restricted-path --report '.runtime/launch-check.json'
& $BuildPython -X utf8 scripts/check-desktop-lifecycle.py --root 'D:\VoiceTerminal' `
  --action soak --seconds 3600 --report '.runtime/one-hour-memory.json'
& $BuildPython -X utf8 scripts/check-desktop-lifecycle.py --root 'D:\VoiceTerminal' `
  --action exit --report '.runtime/exit-check.json'
# 重新启动后再执行：crash（模拟外壳异常结束）。
& $BuildPython -X utf8 scripts/check-desktop-lifecycle.py --root 'D:\VoiceTerminal' `
  --action crash --report '.runtime/crash-check.json'
```

最终验收还需在解压后的真实产物执行：从不同工作目录启动；单实例唤回；关闭隐藏；托盘恢复；正常退出与外壳异常退出无所属进程残留；所有发布文件校验；停机搬迁后启动；数据位置调整；原有语音、打断、声纹注册、故障重试与一小时运行统计。干净的 Windows 10／Windows 11、无系统 Python（后端运行环境）／Node.js（脚本运行环境）／Git（源码工具）及无显卡设备的检查，需要对应物理机或干净虚拟机，不能由开发机导入测试替代。

本轮提供启用组合读取、完整组件校验、统一退出管道及 `state/startup-result.json`（启动结果）接口。在线更新、发布签名和原生事务助手另立专题；拟复用 DesktopUpdateKit（更新下载校验库）的原生下载核心。后续事务为下载暂存、清单与组件验证、安全解包、协调退出、切换组合、启动健康检查、确认或回退。跨重启续传、中断恢复、文件占用、清理和数据恢复需单独验收。
