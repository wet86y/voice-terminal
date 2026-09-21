# 项目隔离 DSH Shadow 执行端

Voice Terminal 内置第一方 DSH Bundle `voice-terminal-shadow-bridge`。当
`shadow_agent.backend: dsh` 时，项目 DSH Web Host 中的正式根 Agent承担 Shadow 任务；当前默认值
已经切换为 `dsh`，`native` 仅作为显式人工回滚后端保留。后端在新 Voice Session 创建时固定，同一 Session不热切换，也不在 DSH
故障时自动转交 native。

固定基线为 `dsh-v0.1.5-rc.2` / commit
`fb2c4b9e698e30edb738bca4cf0618587db7d203`。Bundle 只使用该版本的公开 Cordis、Web Host、Agent、
Session、Tool 和事件扩展点；不得修改 `.runtime/dsh/source`、解析私有 Session 文件、依赖私有
Web RPC，或启动第二个长期持有同一 Session 的 DSH 进程。

## 安装与漂移校验

```powershell
.\scripts\setup-dsh.ps1
.\scripts\setup-dsh.ps1 -ValidateOnly
```

setup 会验证固定 source/tag/commit、Node、pnpm 和 CLI，构建 TypeScript Bridge Bundle，用官方
`dsh plugin --profile web add` 安装到项目 profile，同步正式 preset，运行 `dump-config`，并记录
Bundle 版本、协议版本、Bundle hash、公共提示词 hash 和受管配置 hash。`-ValidateOnly` 对缺失或
漂移 fail-closed。

项目私有运行数据位于 `.runtime/dsh`：

- `source`：固定上游源码和构建物；
- `home`：项目 DSH_HOME、正式/retired Session、Harness 设置和凭据；
- `session-mappings.json`：不含正文、凭据或 token 的 Voice↔DSH 映射；
- `build-state.json`：不含秘密的构建与 hash 审计；
- `web.dump-config.yml`：最后一次受管配置展开结果。

## 通信与安全边界

DSH 固定监听 `127.0.0.1:3081`。Voice Server 每次启动生成独立 Bridge Bearer token，仅通过受管
子进程环境交给 Bundle；它与浏览器 launch token、DSH Cookie 和模型 API key完全分离。Bridge
路由和 Voice 内部回调都拒绝非 loopback、错误 token、错误 Session/epoch/revision/activation、
跨 Session发布、冲突重放和 retired Session。

Voice 模型仍只发送无正文的 `request_shadow({"request": true})`。Voice Terminal 是唯一权威上下文
源；Bundle 先强制注入带来源、epoch、revision 的完整 snapshot/连续 update，再唤醒根 Agent。
普通 DSH assistant 正文永远不进入 Voice 邮箱。正常回传只允许六个根 Agent控制工具：

- `get_latest_context`（仅同步补救）；
- `read_ambient_context`（旁路、不可信、Session 内）；
- `publish_main_event`；
- `request_user_input`；
- `return_shadow_result`；
- `finish_shadow_no_action`。

子代理可承担明确并行任务，但执行入口会拒绝子代理调用 Voice 控制工具。根 Agent统一决定澄清、
确认、观察和唯一终态。语音 confirmation 不会批准或修改 DSH Harness 权限。

## 模型、工具与可见性

正式 preset 继承固定版 `standard` 的完整默认工具组，包括文件、Shell、Web、任务、Skills、目标、
workflow 和子代理；Bridge 不调用 `tools.restrict()`。受管进程默认
`DSH_PERMISSION_MODE=read-only`，实际允许/拒绝由官方 Harness 权限面板人工控制。

DSH 根模型、provider、reasoning 和凭据只由 Harness UI 管理；Voice 设置只管理 Voice 主模型、
status observer 和 TTS。status observer 仍由 Voice 托管，但只能通过 Bundle 的有界
ObserverSnapshot 读取已提交事件，不接收 reasoning、流式草稿、完整敏感参数、环境变量、凭据或
子代理私有轨迹，也不回注 DSH 根 Agent。status observer 的真实摘要会在 Voice 侧生成
`ShadowEvent(kind=status, wake_policy=normal)`：它更新可替换的 `ShadowVisibleStatus`，触发一次
普通 Voice LLM 调度，并在下一次请求的 `runtime.shadow.status` 中可见；它不追加为长期语义历史，
也不会伪装成 `result` 或其他终态事件。

正式 Session 在官方 DSH UI 中显示完整 Voice 权威上下文，并标记 `Source: Voice Terminal`、epoch、
revision 和 snapshot/delta。reset 会取消旧 Turn、把旧 Session 标记 retired 并保留在 UI中，再为
新 epoch 创建全新 Session；旧 activation 的迟到事件永久拒绝。

## 故障边界

3081 被现有进程占用时 Runtime Manager拒绝附着。DSH 异常退出只做有限自动重启，达到上限后标记
unavailable；Voice 主链继续运行，并且不会为同一 Voice Session自动切到 native。Bridge、模型、
恢复或协议未结算故障由系统产生真实 `failed`；业务上的“没有结果”由根 Agent选择澄清、result
或 no-action。

产品顶栏通过 Voice Server取得一次性 launch URL；不要收藏、记录或分享带 `?token=` 的地址。
Session upload、DeepSeek Session Log 和 OTel 保持禁用。固定上游许可证快照位于
`config/dsh/licenses/`。
