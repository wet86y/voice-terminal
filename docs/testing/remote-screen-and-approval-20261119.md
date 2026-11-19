# 远端屏幕捕获放开与批量授权策略修复

## 对照范围

本轮起点为上一次实际安装的 1.0.0 / product-e06f5e48ba58f078，源码提交 18371c5 加本会话修订。前半段为只读审计（双端日志、设备能力、DSH 运行记录），后半段按用户要求实施两项修复：放开远端屏幕捕获、修复“批量授权”落成自动拒绝的问题。未修改设备配置格式、依赖锁、系统服务、防火墙规则或组件安装状态；未重建分发安装器。

## 远端屏幕捕获

### 原因

- `voice_terminal/devices/remote_session.py` 的 `REMOTE_CAPABILITIES`（首版远端受管能力清单）不含 `screen_capture`，远端即使声明也会被拒绝。
- `voice_terminal/devices/screen.py` 的 `ScreenCaptureRouter` 只认本机：`available_for()` 仅对 `local` 返回真，`grab()` 对非本机直接抛“当前交互设备的屏幕捕获尚不可用”。
- `voice_terminal/server/http_api.py` 的能力快照只在 `device_id == "local"` 时加入 `screen_capture`。
- 底层能力实际存在：远端 `device-helper.ps1` 的 Operations 含 `shot`，设备任务通道白名单（`ssh_device.rs`、`device_helper.rs`、`desktop.py`）均放行 `shot`，但产品层从未调用它。

### 修订

- `ScreenCaptureRouter` 增加可选远端通道：远端设备先 `tree` 取前台窗口快照，再 `shot(wholeScreen=true, maxSide=global_max_edge)` 取整屏，按本机同一套双图规则裁剪编码（global JPEG + local PNG），复用既有未消费租约、失效代次与并发保护；返回 `execution_device_id`，继续保持“绝不回退本机”。
- `REMOTE_CAPABILITIES` 加入 `screen_capture`。
- `websocket.py` 构造路由时注入 `device_router`。
- 能力快照改为按设备调用 `available_for(item.device_id)`，远端也声明 `screen_capture`。
- 远端截图走 B 的常驻辅助进程，不要求客户端安装 DSH 运行时；`capture_screen` 不传 `device_id` 时跟随当前语音交互设备。

## 批量授权策略

### 原因

DSH 自带的 `danger-full-access` 权限预设同时设置 `sandbox: danger-full-access` 与 `approval: never`；而 DSH 对 `never` 的处理是直接返回 `rejected`（`dsh-user-approval` 中 `effectivePolicy === "never"` 即拒绝）。因此界面上点“批量授权”后，设备工具（`device_terminal`、`device_file`）显式请求的一次性审批被自动拒绝，表现为“授权后反而全部被拒”，随后后台任务无法收尾、激活被协议恢复清理，桥接工具报“不是活跃绑定的根代理”。

### 修订

- 插件新增 `approval.policy` 入口：校验激活后通过 `ApprovalService.setPolicy` 把会话审批策略设为 `ask` 或 `never`。
- 语音侧在“连续同意”（`allowed-until-silence`）成功结算后调用该入口，把 `never` 恢复为 `ask`；后续设备审批回到语音卡片，由既有自动批准逻辑逐条同意，不再静默拒绝。策略纠正失败不撤销已确认的本地连续授权。

## 验证

- `tests/test_device_screen.py`、`tests/test_screen_capture.py`、`tests/test_device_remote_session.py`、`tests/test_device_remote_capture.py` 共 43 项通过；新增远端用例覆盖 `tree + shot` 两次设备操作、双图角色、未消费复用与来源独立。
- `pytest tests -k "device or screen or remote"` 为 324 通过、2 失败；失败为 `tests/test_device_contracts.py` 的 PCM 流连续性用例，属既有基线问题，与本次改动文件无关。
- 正式安装覆盖后重新启动：服务 `IDLE`，audio/kws/asr/llm/tts/shadow_agent/analysis 均 `READY`，`device_gateway.ready=true`，DSH `ready`（0.2.1-rc.5）；`GET /api/v1/devices` 返回远端能力 `["audio_stream","desktop_control","file_transfer","screen_capture","terminal_control"]`，远端 `connected=true`、`media_ready=true`。
- 重启后日志无 `entry did not activate`、无插件 pending、无桥接 401；DSH Web 正常就绪。

## 未验收与回滚

- 尚未用真实语音会话触发一次远端截图，也未现场复现“连续同意后设备工具恢复可用”；这两项需在下一轮真机测试确认。
- 改动前的 Python 与插件备份分别在 `.local/tmp/screen-remote-patch-backup/` 与 `.local/tmp/approval-policy-fix-backup/`。
- 若用户在 DSH 自身界面手动选择 `danger-full-access` 预设，语音侧不会自动纠正本次策略；该边界待后续决定是否强制拦截。
