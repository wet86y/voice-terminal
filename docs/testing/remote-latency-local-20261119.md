# 远程模式首音延迟本地计算（2026-11-19）

## 范围

本轮只修改前端首音延迟的取数口径：远程（局域网配对）模式不再读取服务端产品快照中的 `runtime.latency.values`，改用项目已有的浏览器本地首音计时；不新增设备配置字段、不改协议、不改服务端接口、不改媒体时钟同步。

## 原因

旧实现中 `useProductAdapter()` 的 `latencyMs` 只在“已连接且非远程模式”时使用本地计时；远程模式回落到服务端快照的 `speech_end_to_playback_ms`、`agent_request_to_speak_ms` 或 `turn_total_ms`。该快照是服务端自己的测量，包含的边界与客户端实际听到首音的时刻不同，也无法反映客户端到服务端的网络与转发延迟。

同时，本地首音计时在远程模式会被服务端事件时间戳干扰：`useVoiceTerminal()` 在事件带 `timestamp` 时用服务端墙钟校正起点和终点。两机时钟不同域，这个校正会把两台机器的时钟差算进延迟。

## 修订

- `useVoiceTerminal()` 增加 `latencyTimestamps` 选项（默认沿用原行为：注入时钟的测试只走本机时钟；生产本机模式仍用服务端事件时间戳）。
- `useProductAdapter()` 传入 `latencyTimestamps: () => application.snapshot.value?.remote_mode !== true`。远程模式的事件时间戳全部忽略，只用 `performance.now()` 的单调时钟；起点是本地收到 `FINALIZING`（输入结束判定）的事件到达时刻，终点是本地收到 `speech.started`、`wait` 或纯停止 `cancel` 的事件到达时刻，替换式 `cancel` 继续计时到后继音频起播。
- 远程模式的 `latencyMs` 只用 `voice.perceptualLatencyMs`，且要求实时语音通道已连接；语音通道断开时显示“—”，不回退服务端快照。
- 远程模式的 `latencyRunning` 跟随同一套计时状态，工作台“延迟计时”动画与“首音延迟”结算保持原有交互。
- 本机模式行为不变：仍优先用同域服务端事件时间戳校正浏览器回调延迟，避免后台标签页计时漂移。

## 验证

- 新增 `useVoiceTerminal` 两项：远程/跨机时钟域忽略服务端时间戳（服务端时间分别偏快 8 秒和 12 秒时，本地仍得到真实的 320/813 毫秒）；同机时钟域仍按服务端事件间隔结算（700 毫秒，即使本地计时器已走 1000 毫秒）。
- 新增 `ProductApp` 一项：远程模式收到 `FINALIZING` 后进入计时，收到 `speech.started` 后停止；即使把产品快照的 `speech_end_to_playback_ms` 设为 9999，显示值也不采用该快照。
- `npm run test --workspace @voice-terminal/frontend -- src/composables/useVoiceTerminal.test.ts src/product/ProductApp.test.ts`：136 项通过。
- 前端全量：43 个文件、539 项通过；`vue-tsc --noEmit` 类型检查通过。

## 现场边界

- 本轮验证使用注入时钟和产品测试替身，没有真实双机麦克风/扬声器、网络抖动或长时间运行验收。
- 远程模式的起点是客户端收到输入结束事件的时刻，不是客户端声学判定的停止说话时刻；它包含服务端识别结束事件到达客户端的下行延迟，不包含上行最后一段音频的传输与识别时间。这是“本地可观测的首音延迟”，与 README 中本机模式的历史口径不同，不能直接混用两组数字。
- 计时跟随客户端订阅的语音事件流。当前交互设备是另一台设备时，该值反映该会话在本客户端的事件到达时序，不等同于本设备麦克风到扬声器的端到端延迟；单设备远程交互时二者一致。本轮没有增加“仅当前交互设备”的显示门禁。
- 服务端 `latency` 快照仍保留在会话记录和诊断数据中，只是不再作为远程工作台首音延迟的显示来源。
- 与后台协作控制台中继的可行性调研分开记录，见[远程 DSH 控制台中继](../architecture/remote-dsh-console.md)。

## 交付

- 已随 product-593d771db3cd30ae 同包更新到本机和分发安装器，安装后正式页面重新挂载；构建与校验细节见[当前交付](../releases/current-release.md)。
