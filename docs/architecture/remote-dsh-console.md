# 远程 DSH 控制台中继（可行性调研与设计）

本文记录局域网客户端打开服务端 DSH Web UI 的调研结论、验证证据和建议设计。状态：**调研完成，尚未实现**。文中的“本机探针”是本机可行性验证，不代表已完成双机验收。

## 结论

可以通过现有 SSH 桥接协议实现，不需要启用 OpenSSH 的 TCP 转发，也不需要修改 `sshd` 配置或防火墙。标记方案的基本结构是：

1. 客户端经已认证的 SSH 固定桥接打开控制台中继通道；
2. 服务端中继在首个文档请求中注入 DSH 启动令牌，令牌不离开服务端；
3. 客户端在本机回环随机端口监听，把浏览器连接逐条转成 SSH 桥接通道，浏览器只访问 `http://127.0.0.1:<随机端口>/`。

另有一条更轻的路径——复用现有已认证控制通道、用 WebSocket 承载原始字节（效果等同端口映射，且不需要修改 SSH 公钥或重新配对），见下文“更轻的路径”，建议优先评估。

本机探针已证明：DSH Web UI 经原始 TCP 转发后，令牌登录、持久 Cookie、静态资源和 `/api` 的 Host/Origin 校验都成立；SSE 与 `/api/remote.mux` WebSocket 长连接走同一原始 TCP 通道，尚需真机验证。登录令牌注入需要“仅在没有目标 Cookie 时注入”，否则会形成 303 重定向循环（探针 2b 已验证修正规则）。

## 现状

- 客户端远程模式下，工作台“后台协作”面板的后台轨迹和待决审批跟随服务端语音实时事件流（`runtime.snapshot`、`shadow.product`，经 `/v1/voice` 到达）；只有实时通道未连接或尚未收到权威投影时，才回退到打开页面时读取的产品快照。授权按钮通过既有远程固定操作 `dsh.approval.decide` 提交。底部“代理控制台”按钮仍被禁用并显示“仅服务端本机可打开”（`apps/frontend/src/product/ProductApp.vue`、`apps/frontend/src/product/useProductAdapter.ts`）。
- `openDshConsole()` 对远程模式直接报错，不请求本机回环地址（`apps/frontend/src/product/useProductAdapter.ts`）。
- 服务端 DSH 由 Voice Server（语音服务）管理，Web Host 端口动态分配；本机实测 `ui_origin` 为 `http://127.0.0.1:50966`，启动令牌由 DSH 子进程标准输出报告，Voice Server 保存 `launch_url`，只通过带 Origin 校验的本机产品接口返回，不进入状态快照或日志（`voice_terminal/dsh/runtime.py`、`voice_terminal/server/http_api.py`）。
- 服务端强制命令桥接当前只登记四个固定标记：控制、媒体、管理、文件；目标分别是 18765、18765、18768、18769（`apps/desktop/common/src/device_bridge.rs`、`apps/desktop/common/src/ssh_device.rs`）。任何其它命令、shell 和子系统都被拒绝。

## 现有协议能做到什么

现有桥接不是通用端口转发，而是“固定标记 + 固定回环目标 + 项目密钥/设备信任/构建/工作区/SID 校验”的强制命令通道：

- `bridge_target()` 只把四个标记映射到固定本机地址；`validate_ssh_connection_scope()` 拒绝非私有局域网/回环来源和非白名单标记。
- 客户端 `open_fixed_bridge()` 只允许这四个标记打开 SSH exec 通道；通道内是原始字节流，可承载 HTTP、WebSocket 或二进制。
- 管理、文件通道额外先写一行 `DeviceBridgePrelude`（工作区、SID、设备编号、构建）给服务端回环服务，服务端逐次核验（`apps/desktop/src-tauri/src/remote_management.rs`）。
- 微软 Win32 OpenSSH 可能在同一 SSH 连接上沿用首个 `SSH_ORIGINAL_COMMAND`，因此同一连接只用于一种标记；语音转发就是每条本机连接新建一条专用 SSH 连接（`SshDeviceManager::relay_voice_websocket`）。

由此可推出的最小改动是：**新增第五个标记 `voice-terminal-v1-console`，指向一个新的固定回环端口（建议 18770），由服务端控制台中继监听**。这样既保留“只能到固定回环服务”的边界，又能由中继在每次连接时解析 DSH 当前动态端口。

不采用的方向：

- SSH 本地端口转发（`direct-tcpip` / `ssh -L`）**不作为默认**：项目公钥行带 `restrict`（[system_feature.rs](../../apps/desktop/common/src/system_feature.rs)），它禁用 TCP 转发；去掉后 OpenSSH 的 per-key `port-forwarding` 会同时开启本地和远程转发，`permitopen` 只限制 `-L` 目标，不能只开 `-L`；只开本地转发只能改 sshd 的 `AllowTcpForwarding local`（全局或 `Match`），会影响该账户的其他 SSH 用途。若接受这个安全边界变化，`-L` 仍是最短实现路径，见下文“更轻的路径”。
- 把控制台标记直接映射到 DSH 端口：DSH 端口动态，固定常量无法覆盖；且重启后端口变化。
- 在语音网关（18765）加 `/dsh/*` 反向代理：DSH 前端资源使用根路径和相对 `<base href="./">`，路径前缀会破坏资源与后续请求；用 HTML/JS 重写不可靠。注意这与下文“控制通道 WebSocket 隧道”不同：后者不在 HTTP 层加路径前缀，而是把原始字节封进 WebSocket 帧。

## DSH Web Host 的鉴权与校验（已核对 0.2.1-rc.5 安装目录）

- 访问 `/` 需要 `?token=<进程启动令牌>`：有效令牌返回 `303 Location: ./`，并写入 `dsh-auth-<authority 摘要>` Cookie；Cookie 载荷绑定 `authority`（HTTP 下为 `Host`，含端口）。
- 已有有效 Cookie 时直接返回首页；没有令牌也没有 Cookie 时返回 401 文本“dsh web authentication required…”。
- 静态资源公开；`/api` 要求 `Host` 是回环地址（端口不限），`Origin` 若存在必须与 `Host` 完全同源，并且 `Sec-Fetch-Site` 不能是 `cross-site`。
- 前端使用 `/api` 的 JSON 请求、SSE（`text/event-stream`）长连接，以及 `/api/remote.mux` WebSocket 流；原始 TCP 隧道对这些都透明。

这解释了为什么原始隧道能直接工作：浏览器访问 `http://127.0.0.1:<客户端随机端口>/` 时，`Host` 和 `Origin` 都是这个客户端 authority（回环地址），DSH 生成的 Cookie 也绑定这个 authority，校验自然通过；反过来，中继把 Host 或 Origin 改写成服务端 authority 反而会破坏同源判断。

## 本机探针证据（2026-11-19）

探针脚本保存在本机 `.local/investigations/dsh-console-probe-20261119/`，不进入源码历史。测试对象是本机正在运行的服务端：`GET /api/v1/dsh/status` 返回 `ready`、`ui_origin=http://127.0.0.1:50966`。

| 场景 | 结果 |
| --- | --- |
| 本机原始 TCP 转发 `127.0.0.1:50967 → 50966`，带 `?token=` 首次访问 | `303 See Other`，`Location: ./`，写入绑定 `127.0.0.1:50967` 的 Cookie |
| 用上述 Cookie 访问 `/` | `200`，`text/html`，38,882 字节 |
| 访问 `/assets/index-*.js` | `200`，941,436 字节 |
| 带正确同源 `Origin` 请求 `/api`（空 RPC） | 路由 404，说明已通过 Host/Origin 栅栏 |
| 带错误 `Origin` 请求 `/api` | `403 forbidden` |
| 无 Cookie 访问 `/` | 401 鉴权文本 |
| 服务端中继注入令牌：`GET /` 无 Cookie | `303` + 新 Cookie；随后 `/` 返回 `200` |
| 注入后再次访问 `/` | 仍 `200`，不会循环 |
| 带旧 Cookie 的 `relogin` 导航 | `303` + 刷新 Cookie，可恢复 |
| 注入后同源 `/api` | 404（栅栏通过）；错误 Origin 仍 `403` |

关键修正：中继不能对每个 `GET /` 无条件注入令牌，否则 DSH 每次都返回 `303`，浏览器在 `/` 与 `./` 之间循环。正确规则是**仅当请求没有该 authority 对应的有效 Cookie 时才注入**；需要强制刷新时由客户端重新打开一个新的本机端口（authority 改变，Cookie 名随之改变），或使用一次性 `relogin` 标记。

## 社区与官方现成方案（2026-11-19 核对）

官方与社区都把“SSH 本地端口转发到回环地址”当作标准做法：

- DSH Web 文档说明 SSH 会话下不自动打开浏览器，由 SSH 客户端或编辑器负责本地转发地址；社区讨论给出的操作就是 `ssh -L 3080:127.0.0.1:3080`（[Discussion #76](https://github.com/deepseek-ai/deepseek-harness/discussions/76)）。
- DSH 也支持 `--host` 绑定一个具体局域网地址、`--public-url` 和 `--trusted-host`，但要求前置反向代理提供 TLS 和 authority，并明确警告不要直接暴露端口；不支持 `0.0.0.0`。在非回环的纯 HTTP 地址下，浏览器安全上下文缺失（例如 `crypto.randomUUID` 不可用）会影响 DSH 前端 RPC，设置持久化也依赖 hostname 判断。因此“直接绑局域网地址”不适合作为本项目路径。

可参考的社区实现：

| 方案 | 做法 | 与本项目的差距 |
| --- | --- | --- |
| [`dsh-remote-panel-ssh`](https://www.npmjs.com/package/dsh-remote-panel-ssh)（[源码](https://github.com/1583477320/remote-dsh)） | 本地 DSH 桌面壳侧栏 iframe；本机网关完成 `?token=` → Cookie 交换并代持 Cookie，转发 HTTP（重写 Host/Origin/Referer）和 `/api/remote.mux` WebSocket；隧道用 `ssh2.forwardOut()`；令牌不下发浏览器，401 时重读令牌自愈 | 本地必须是 DSH 桌面应用，远端必须是 Linux 且可安装/启动独立 `dsh web`，使用标准 SSH 直连；本项目客户端没有 DSH 运行时，服务端是 Windows 且 DSH 由 Voice Server 管理，SSH 是受限强制命令（`restrict` 禁用端口转发）。网关/token 处理思路可直接借鉴 |
| [`dsh-remote-connector`](https://www.npmjs.com/package/dsh-remote-connector)（[源码](https://github.com/WhitesPantry/dsh-remote-connector)） | 侧栏管理远端服务器；SSH 部署/启动 `dsh --profile web`，本机 `ssh -L` 隧道，返回带 token 的地址 | 依赖系统 `ssh` 和 Linux 远端；token 进入浏览器地址栏；没有项目设备配对与受限桥接 |
| [`dsh-remote-window`](https://www.npmjs.com/package/dsh-remote-window) | 客户端通过 SSH 在远端启动 DSH，并把官方 Web UI 全屏嵌入客户端；服务端插件只验证 DSH `0.2.1-rc.3` | DSH 桌面壳 + Linux 远端 + 版本锁定；服务端需安装配套插件，不适用于本项目的零组件客户端 |
| [`dsh-gateway`](https://www.npmjs.com/package/dsh-gateway)（[源码](https://github.com/clarknu/dsh-gateway)） | 在 DSH web profile 内起 HTTPS + 账号登录网关，供局域网或公网访问 | 不复用本项目 SSH/设备信任；会在服务端新增局域网监听和独立账号体系，改变现有回环安全边界；还需把社区插件纳入受管 DSH 清单和版本校验 |
| `dsh-pocket` 等移动端方案 | 手机扫码访问 DSH Web，局域网/公网隧道 + 访问密码 | 面向手机，不是本项目客户端；同样绕开设备配对 |

结论：**没有能直接安装复用的方案**。可复用的是它们的交互与网关设计：本机网关完成令牌交换、令牌不进浏览器、按域校验 Cookie、401 自愈、端口隧道保持回环。不能复用的根本原因是本项目客户端没有本地 DSH 运行时，服务端 SSH 公钥带 `restrict`（禁用 TCP 转发）和固定强制命令，且 DSH 版本、profile 与组件清单都由项目锁定。

如果愿意改变 SSH 边界，标准 `ssh -L` 会显著减少实现量（社区方案都走这条路），但需要新增独立 SSH 凭据或放开 `PermitOpen`/`AllowTcpForwarding`，并把项目的受限桥接替换为通用转发；这会让已配对客户端可以访问服务端任意回环目标，不建议作为产品默认。

## 更轻的路径：控制通道 WebSocket 隧道（推荐优先评估）

现有语音转发已经证明：**不必新增 SSH 标记或端口转发**，可以把一条 WebSocket 塞进现有的 `voice-terminal-v1-control` 强制命令通道。语音转发在 `SshDeviceManager::relay_voice_websocket` 中打开 control 通道、向 18765 发 `Upgrade: websocket`，然后把浏览器与 `/v1/voice` 双向转发；媒体通道还已有 `SshWebSocketStream` 的帧编解码。控制台可以复用同一套结构：

1. **服务端**：在 Voice Server 的 18765 网关增加一个固定 WebSocket 路由 `/v1/dsh-tunnel`。
   - 加入 `_GATEWAY_WEBSOCKET_PATHS` 和 `LoopbackDeviceGatewayGuard.allowed_paths`，沿用 `_authorize_device_gateway_request` 的已签名设备挑战（GET 路径，签名载荷固定为 `{"protocol_version":1}`），不需要新的桥接标记。
   - 握手通过后打开 `asyncio.open_connection(127.0.0.1, runtime.dsh 当前实际端口)`；把客户端发来的 WebSocket 二进制帧写入 DSH，把 DSH 读到的字节按 ≤64 KiB 封成二进制帧发回。
   - 在第一条 HTTP 请求头上做令牌注入；目标固定为当前 DSH，不接收任意目标、路径或命令。
   - DSH 未就绪时以 1013/1008 关闭；设备未受信、来源不是 18765、签名过期都拒绝。
2. **客户端**：新增一个本机回环监听（随机端口），每条浏览器 TCP 连接：
   - 用专用 SSH 连接开 control 通道，按现有 `authorize_gateway` 取挑战并签名，向 `/v1/dsh-tunnel` 发 WebSocket 升级；
   - 复用 `SshWebSocketStream` 的帧编解码，把浏览器字节分块封成二进制帧，把服务端帧写回 TCP；
   - 浏览器只访问 `http://127.0.0.1:<随机端口>/`，地址中没有令牌。
3. **令牌注入的精确规则**：Cookie 名是 `dsh-auth-<base64url(sha256(authority))>`，而浏览器 Cookie 不区分端口。新的本机端口会同时带上旧端口的 `dsh-auth-*` Cookie；中继必须计算**当前 Host authority 对应的确切 Cookie 名**，只有该 Cookie 不存在时才注入令牌，并丢弃旧 `dsh-auth-*`。只用“存在任意 dsh-auth Cookie”判断会把新端口误判为已登录，得到 401。
4. **生命周期**：控制台转发器随远程模式启动/停止；SSH 断开、目标切换、模式切换、应用退出时回收；`open_console` 只接受当前转发器实际端口。

与新增标记方案相比：

| | 控制通道 WebSocket 隧道 | 新标记 + 回环中继 | 直接 `ssh -L` |
| --- | --- | --- | --- |
| SSH 公钥/sshd 改动 | 无 | 无 | 需去掉 `restrict`，并接受 `-R` 或改 sshd |
| 服务端改动 | 18765 新增一个受限 WS 路由 | 新增 18770 监听器 + 新标记 + 前导帧 | 固定 relay/端口 |
| 令牌是否进客户端 | 否（服务端注入） | 否（服务端注入） | 是（地址栏带 token） |
| DSH 动态端口 | 支持 | 支持 | 需固定端口或 relay |
| 主要成本 | WS 帧封装/解封装 | 新标记、前导帧、监听器 | UAC 重写公钥、重新配对、`-R` 风险 |
| 估算 | 约 2–3 人日 | 约 3–5 人日 | 约 1–2 人日（不含安全评估） |

**推荐顺序**：先评估控制通道 WebSocket 隧道；它不破坏现有 `restrict` 加固，也不需要 UAC/重新配对，仍然保持“令牌不离开服务端”。如果确认可以修改 SSH 公钥策略，再考虑直接 `-L` 作为最短实现。

## 建议设计

### 服务端

1. **控制台中继**：在 Voice Server（Python）中新增 `DshConsoleRelay`，只监听 `127.0.0.1:18770`，随共享主机设备网关（`DeviceGateway`，18765）一起启停，不单独对外暴露。
   - 每条连接先读一行 `DeviceBridgePrelude`（上限 4096 字节），校验类型、协议版本、工作区、构建、设备编号和受信设备状态；不匹配立即关闭。
   - 校验当前设备存在有效的控制台授权（见下）；授权由已认证的管理通道按设备登记，绑定任务通道 `connection_generation`，设备断线即失效。
   - 再读首个 HTTP 请求头（上限 32 KiB）。仅当方法是 `GET`、路径是 `/`、且请求没有当前 `Host` authority 对应的 `dsh-auth-` Cookie 时，把请求行改写为 `/?token=<launch_url 中的令牌>`，并丢弃旧的 `dsh-auth-` Cookie；其它请求原样转发。
   - 目标由 `runtime.dsh` 当前实际端点解析；DSH 未就绪时关闭并记录有界原因。之后双向复制字节，不解析 body、不缓存内容、不记录令牌。
   - 连接数、并发、头部大小、首包等待时间都需要上限；半关闭和异常退出沿用 `device_bridge` 的处理方式。
2. **新增固定标记**：`voice-terminal-v1-console` → `127.0.0.1:18770`，带前导帧；在 `device_bridge.rs` 的 `bridge_target()`、`validate_ssh_connection_scope()` 和 `ssh_device.rs` 的 `open_fixed_bridge()` 白名单中登记。
3. **授权操作**：在 `/v1/management` 的固定操作表中新增 `console.open` 和 `console.close`。`console.open` 由已签名设备在当前任务通道代次下登记短时授权（建议有效期与设备连接绑定，另设 12 小时上限）；`console.close` 撤销。只读/写入账本沿用现有管理通道，不新增任意 URL 或参数。
4. **版本兼容**：旧服务端不认识新标记或新操作时，客户端明确提示“服务端版本不支持远程控制台”，不回退到本机或任意地址。

### 客户端原生

1. 新增 `device_ssh_console` 命令（`start`/`status`/`stop`），参照现有 `device_ssh_voice_forwarder`：
   - 仅远程配对模式、SSH 已认证、任务通道就绪时启动；
   - 在本机 `127.0.0.1` 随机端口监听，只接受回环对端；
   - 用同一条专用 SSH 连接打开 `voice-terminal-v1-console` 通道（不能与控制/管理/媒体/文件通道共用连接）；每条本机连接对应一条桥接通道；
   - 通道建立后先读取并校验服务端前导帧，再原样双向复制；浏览器不直接接触 SSH 或令牌；
   - 提供 `stop`，并在 SSH 断开、目标切换、模式切换、应用退出时回收；`start` 每次分配新端口，便于刷新 authority 和 Cookie。
2. `open_console` 增加远程分支：只接受 `http://127.0.0.1:<当前控制台转发端口>/`（路径 `/`、无查询、无片段），比对当前转发的实际端口后交给系统默认浏览器；本机 DSH 的 `?token=` 入口校验保持不变。

### 前端

1. `openDshConsole()` 在远程模式改为：确认管理连接与服务端业务就绪 → 调用 `console.open` → 启动 `device_ssh_console` → `open_console(本地地址)`；失败时清理授权并显示可重试原因。
2. 工作台按钮在远程模式按“服务端管理已连接 + 服务端业务就绪 + DSH 状态 ready”启用，文案改为“代理控制台”/“服务端代理未就绪”，不再固定显示“仅服务端本机可打开”。
3. 关闭控制台、切换运行模式或断开连接时调用 `console.close` 并停止本机转发；不把授权或地址写入日志、历史或持久配置。

## 安全边界

- 控制台中继只绑定回环，只能连到当前 DSH 端点；不接收任意目标、路径或命令。
- 令牌只在服务端内存与出站请求行中出现，不进入客户端、浏览器地址栏、历史或日志。
- 授权只能由已签名、已配对、任务通道代次匹配的设备经管理通道申请；设备断线、撤销、模式切换或超时后失效。
- 前导帧逐连接校验工作区/构建/设备信任；即使本机其它进程连到 18770，没有有效授权也无法建立转发。
- DSH 自身的 Host/Origin、Cookie、只读权限策略保持不变；本设计不绕过 DSH 鉴权，只是替浏览器完成令牌交换。
- 限制单设备并发通道数、单连接头大小和空闲时间，避免把服务端变成通用的回环代理或放大资源消耗。

## 需要验证的风险

- **每条浏览器连接一条 SSH 通道 = 服务端一个强制命令进程**。强制命令会在 Tauri/Python 启动前分流，但仍是一个完整的 `VoiceTerminalDesktop.exe` 进程；一条页面通常占用 2–6 条连接。需要实测进程数、内存、首字节延迟和页面加载时间，并设置并发上限。
- 浏览器连接复用、SSE 长连接、`/api/remote.mux` WebSocket 流、页面刷新和 DSH 重启后的行为需要真机验证；DSH 重启会更换令牌，中继每次连接重新解析 `launch_url` 可以跟随，但已打开页面可能短暂失败。
- DSH 端口、Cookie authority 与客户端随机端口的关系需要双机验证，包括客户端应用重启后旧 Cookie 的处理。
- 现有 `bridge_target`、管理通道和强制命令测试夹具需要扩展；旧服务端/旧客户端组合必须有明确失败路径。
- `open_console` 现有“只能打开当前受管 DSH”的断言需要同时覆盖本机与远程两种来源，避免放宽成任意回环地址。

## 实施顺序（建议）

1. 服务端 `DshConsoleRelay` + 前导帧校验 + 令牌注入规则 + 单元/集成本机测试（用本机假 DSH 或真实 DSH）。
2. 新桥接标记与 `ssh_device` 通道、客户端转发器命令、URL 校验；本机回环 SSH 夹具测试。
3. `console.open`/`console.close` 管理操作与前端调用、按钮状态、错误文案。
4. 双机现场验收：真实配对、打开控制台、加载会话/历史、SSE 长连接、刷新、DSH 重启、断线重连、撤销设备、并发上限。
5. 文档、README 与已知问题同步；未完成前保持“仅服务端本机可打开”。

## 相关落点

- 强制命令与标记：[device_bridge.rs](../../apps/desktop/common/src/device_bridge.rs)、[ssh_device.rs](../../apps/desktop/common/src/ssh_device.rs)
- 客户端原生命令与控制台校验：[main.rs](../../apps/desktop/src-tauri/src/main.rs)
- 前端控制台入口：[ProductApp.vue](../../apps/frontend/src/product/ProductApp.vue)、[useProductAdapter.ts](../../apps/frontend/src/product/useProductAdapter.ts)
- 设备网关与管理操作：[device_gateway.py](../../voice_terminal/server/device_gateway.py)、[http_api.py](../../voice_terminal/server/http_api.py)
- DSH 启动地址与端口：[runtime.py](../../voice_terminal/dsh/runtime.py)
- 现有控制台维护说明：[dsh-console.md](../guides/dsh-console.md)
