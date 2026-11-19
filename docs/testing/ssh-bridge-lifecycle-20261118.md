# SSH（安全远程连接）桥接收尾与重复检查修订（2026-11-18）

## 现场证据

本轮起点为实际本机及分发构建 product-8d564ca0714236f4（构建编号）。保留此前所有未提交修订。两端当前安装构建一致；客户端 D:\AI\example-test（工作区）公钥和主机指纹验证约 150 毫秒完成，原生管理请求实际返回 core_state（语音核心状态）为 ready（就绪）。

客户端报错后，用户为排查手动关闭过服务端广播；当前端口探测超时由关闭后的专用规则解释，不能据此解释此前已认证但任务未就绪的故障。随后通过正式入口恢复广播继续诊断。

真实双机独立诊断使用客户端既有身份，私钥在原设备内只读解密，不输出或导出。原始 SSH（安全远程连接）消息、中文及转义文本、官方流式接口、项目原生管理器均取得有效管理回执。随后同一原生管理器读取核心控制接口，10 秒预算实际 10,006 毫秒失败，60 秒预算实际 60,012 毫秒仍失败。证据在 .local/cache/reports（本机诊断报告目录）的 remote-wire-comparison.json（编码对照）、remote-wire-stream.json（官方流式接口）、remote-wire-manager.json（项目管理器）和 remote-wire-control-before.json（核心控制失败复现）。

## 官方契约与根因

- [OpenSSH（安全外壳）官方手册](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT)规定 from（来源范围）和强制命令用于限制授权公钥的连接来源与命令；[微软官方配置说明](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)说明 Windows（操作系统）的命令由配置的系统命令环境执行。
- [russh（项目 SSH 库）官方接口](https://docs.rs/russh/latest/russh/struct.Channel.html)说明流式接口接收标准输出消息；项目不能据此认为命令错误输出或退出状态已被处理。
- [Rust（原生语言）标准输出说明](https://doc.rust-lang.org/std/io/struct.Stdout.html)要求即时发送时显式刷新缓冲；[线程等待说明](https://doc.rust-lang.org/std/thread/struct.JoinHandle.html#method.join)明确等待会持续到线程结束。
- 原桥接在服务端结束回执后等待标准输入线程退出；客户端同时等待 SSH EOF（输出结束），形成互等。原输出复制没有逐块刷新，无换行的控制正文及二进制数据可能滞留。旧实现新增两项针对性回归均失败，原始失败保存在 .local/cache/logs/ssh-bridge-lifecycle-before.log（修复前回归日志）。
- 每条桥接另做完整系统查询，本机实测约 14,362 毫秒；缩减状态查询仍约 11,141 毫秒，超过任务通道原有 10 秒预算。按用户要求移除该连接热路径的系统查询，不通过延长业务超时掩盖问题。

## 修订边界

桥接按字节块刷新输出；客户端结束输入时仅半关闭上行，保留回执方向。服务端结束输出后直接结束专用桥接进程，避免等待仍开放的客户端输入。新回归覆盖输入仍开放时结束、无换行二进制即时刷新、半关闭后完整回执。

每条桥接保留 SSH_CONNECTION（官方服务提供的已接受端点）格式、有效端口、私有地址或成对回环、固定通道标记校验；不再调用 PowerShell（系统脚本进程）扫描系统、组件、签名、服务或防火墙。精确子网限制继续由已登记公钥 from（来源范围）及仅私有/域网络开放的专用防火墙执行；工作区、账户、构建、设备信任、主机指纹、请求签名及撤销检查保留。主动检查及配置服务仍可执行完整系统检查。系统功能模块与本轮起点摘要完全一致，未删除此前管理员账户修订。

管理握手失败诊断补充错误类别、位置、字节数及文本编码有效性，继续限制日志长度，不记录报文正文或凭据。

## 最终交付与复测

完成后在此补录最终构建、源码差异及真实双机控制通道验证，不能用本机回环或管理状态成功替代任务、语音链路验收。

## 同包交付恢复（安装「拒绝访问」）

`npm run desktop:deliver` 在安装阶段以系统错误 5 结束。定位依据：事务记录 `state=committed` 说明本体换入已完成；`.local/tmp/publish/new` 已清除而 `old` 树一条未删（NTFS 名称序 new 在 old 之前），说明失败发生在提交后的 `recover_install → remove_owned(tmp/publish)`；安装日志中错误行夹在两条进度行之间是采集顺序假象（[scripts/desktop-pipeline.ps1](../../scripts/desktop-pipeline.ps1) 用两个异步读取轮询标准输出与错误输出）。

现场复核：对残留 `old` 树 10 个代表路径做 `CreateFileW(DELETE)` 加独占共享探测、ACL、只读属性、受控文件夹访问与 21:46–21:50 事件日志检查，均无阻碍。随后用正式入口 `publish-desktop.ps1 -SkipBuild` 补跑安装与维护副本两步，一次通过（安装 6.0 秒、保留 0.7 秒），未再出现错误 5，确认为一次性瞬时占用，不是权限结构缺陷。

核对结果：程序包 `program.zip` 与 `package-info.json` 同为 `product-6f3d53f6ffe34ab0`（`5bf87786…bf7a`）；`releases/installer/delivery.json`、`build-stamp.json`、`VoiceTerminalSetup.exe` 与 `maintenance/installer.json` 及维护副本实算摘要一致（`f3608442…b5f8`，119430656 字节）；维护副本构建由旧的 `product-8d564ca0714236f4` 更新为当前构建；`tmp/update-transaction.json` 与 `tmp/publish` 事务收尾后消失；`compatibility.json` 由 `enforcement.phase=pending` 转为 `complete`、`status=compatible`；`--verify-package` 补跑前后各通过一次。

## 管理通道与控制通道的 SSH 连接边界

现场补充证据：服务端 `device-channels.log` 每隔约 25 至 30 秒记录一次「远程管理会话申请 JSON 格式无效。」并夹杂「远程管理连接读取失败。」，与客户端核心控制请求 10 秒、60 秒预算连续超时的现象同源。管理服务解析的是 JSON 会话行，但实际到达的是控制通道的 HTTP（超文本传输协议）请求，说明控制请求被分派到了管理桥接。

原实现让管理标记与控制、媒体、文件标记共用同一条已认证 SSH 连接（媒体与文件各自新建连接，管理没有）。微软 Win32 OpenSSH 已报告同一连接上的后续命令可能沿用首个 `SSH_ORIGINAL_COMMAND`（原始命令标记）；一旦首个标记是管理标记，后续控制通道就会被送进管理 JSON 解析器，这与上面的服务端日志完全一致。

修订：管理标记改由独立 SSH 连接承载（`ConnectedDeviceSession` 的 `management` 连接，按需建立、传输层关闭后重建），管理租约、通道复用与逐次信任核验仍保留在管理通道内；控制、媒体与文件保持原有连接与复用方式，隔离只改承载连接，不改协议、租约或授权判定。

回归：新增 `control_requests_keep_their_lane_when_the_server_replays_the_first_command`，用「按连接沿用首个 exec 命令」的测试 SSH 服务模拟该缺陷，顺序与现场一致（先管理、后控制），断言控制请求仍到达控制网关且管理服务只被连接一次。修复前该测试失败，报「SSH 业务回执缺少完整 HTTP 响应头（已收到 0 字节）。」；修复后通过。`voice-desktop-common` 全量单元测试 381 项通过、15 项忽略、0 项失败（含 `ssh_device` 29 项）。

尚未完成：真实双机复测需要两端同包更新并重新登记当前构建绑定的受限公钥，未取得该结果前不宣称连接修复完成。

## 媒体通道时钟同步（远程语音）

现场现象：远程语音连上后立即被服务端关闭，客户端显示「主机在完成时钟同步前关闭媒体通道」；服务端 `voice-server.log` 记录 `device.media_stream_closed`，原因为「设备处理时间超过主机测得的往返时间」与「设备时钟测量的时间或顺序无效」交替出现。

根因：服务端五轮往返测量使用 `time.monotonic()`。本机 Windows（操作系统）的该时钟由 `GetTickCount64` 提供，`time.get_clock_info('monotonic')` 报告分辨率 0.015625 秒，实测步进 16 毫秒；局域网往返只有 1 至 3 毫秒，于是同一刻度内的往返被读成 0（判超往返），相邻两轮读到相等值（判次序无效）。此前的确定性测试用可重放的细粒度假时钟注入，因此没有暴露该差异。

修订：

- `voice_terminal/devices/clock_sync.py` 新增 `media_clock()`，改用 QPC（高性能计数器）并把起点锚定回 `time.monotonic()` 时间轴；锚点常量只平移绝对值，成对差值保持亚微秒精度，其他主机时间比较不受影响。
- `voice_terminal/server/http_api.py` 的媒体时钟路径（设备时间映射、五轮往返、估计与两次输出回执映射）全部改用 `media_clock()`。
- `DeviceClockMapper.observe` 去掉跨轮严格递增判定与「设备处理时间不得超过往返」的零容忍判定，改为 10 毫秒抖动容忍；仍保留单次测量自身成立（主机收晚于发、设备发不早于收、整数范围）。

回归：`tests/test_device_clock.py` 新增三项（媒体时钟精度与时间轴、抖动容忍内接受而明显超出仍拒绝、五轮测量完成），`tests/test_device_media_endpoint.py` 显式注入媒体时钟；两文件共 13 项通过。

真机结果（构建 product-5064f26d5e17d8e7，客户端 device-9525dfbd-5a17-4b2b-ba2c-3744d87d9014）：

- 管理状态 200，`core_state=ready`
- `/v1/status` 预算 10 秒实际 327 毫秒、预算 60 秒实际 379 毫秒，均 200
- 任务租约及续租 274 毫秒
- 媒体握手及五轮时钟同步 26 毫秒，会话正常关闭

报告：`.local/cache/reports/remote-wire-media-after.json`。音频实际收发尚未在真实设备上验收，不作为已完成的语音链路结论。

### 远程语音本机转发的页面权限

媒体链路通过后，客户端语音页仍显示「等待服务端语音实时通道」，状态栏报 `Command device_ssh_voice_forwarder not allowed by ACL`。原因：静态能力 [boot.json](../../apps/desktop/src-tauri/capabilities/boot.json) 逐个列出了 `allow-device-*` 命令，包含管理与任务/媒体通道，但漏了 `allow-device-ssh-voice-forwarder`；页面来源落在该静态能力上时，本机回环转发指令被 ACL 拒绝，转发 listener 因此从未启动。

修订：把 `allow-device-ssh-voice-forwarder` 加入 `boot` 能力（与已有的 `allow-device-ssh-management-request`、`allow-device-ssh-media-channel` 同级）。重装两端后，真实双机整条链路复测通过：管理 200、`/v1/status` 两次 200、任务租约及续租 549 毫秒、媒体握手及五轮时钟同步 84 毫秒。
