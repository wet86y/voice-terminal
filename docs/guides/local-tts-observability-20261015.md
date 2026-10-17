# 本地 TTS 可观测性与日志容量

本指南定义本地语音合成的日志关联、诊断开关、文件轮转预算和隐私边界；日志仅用于诊断，不改变合成策略、模型、音色、推理步数、播放调度或用户设置。

## 日志关联与隐私边界

本地合成事件用 `turn=`（用户轮次）、`speech=`（播报事务）和 `session=`（原生上下文）关联；失败、预热、分段回执、缓存溢出、模型切换与服务生命周期均有对应事件。`controller.turn_latency`（本轮时延）、`controller.tts_status`（合成状态）及播放器事件可与合成日志按轮次对齐。

普通日志默认不含会话文本，只记录字符数与文本摘要。只有显式设置 `VOICE_TTS_LOG_TEXT=1`（允许记录有限文本预览）才追加预览；`VOICE_TTS_NATIVE_TRACE=1`（启用原生分阶段跟踪）将原生追踪写入独立日志。原生输出由父进程逐行添加时间戳，便于和语音服务时间线对照。

## 本地合成日志字段

一次本地合成从会话建立到分段完成由以下事件串起，全部带 `turn=`／`speech=`／`session=`：

```text
audio_cpp_tts.session_policy      模型、音色版本、续接、预热/试听、推理步数、采样率
audio_cpp_tts.request_start       分段序号、字符数、续接、步数、文本摘要
audio_cpp_tts.request_complete    首包、锁等待、网络首音频、总耗时、音频时长、实时因子、PCM 字节
audio_cpp_tts.request_failed      阶段、异常类型、错误文本、是否重试、是否已有音频、耗时
audio_cpp_tts.request_rejected    前置校验拒绝（音色缺失、参考音频缺失、会话状态）
audio_cpp_tts.segment_wait_failed 分段回执超时或缺失
audio_cpp_tts.buffer_overflow     提前合成暂存超过 32 MiB
audio_cpp_tts.prewarm_ready       预热返回音频字节数
audio_cpp_tts.prewarm_failed      预热失败原因
audio_cpp_tts.continuation_fallback 声学续接失败后回退独立合成
audio_cpp_tts.continuation_reanchored       设计音色在完整句末成功重锚定
audio_cpp_tts.continuation_reanchor_skipped 上下文释放未确认，重锚定跳过
audio_cpp_tts.model_loaded/model_unloaded/model_load_failed  模型装载与卸载（含 `elapsed_ms`）
audio_cpp_tts.server_started/server_ready/model_switched/server_stopped  原生服务生命周期
gpu_performance.applied/restored  本地合成期间的显卡性能模式
```

轮次侧补充 `controller.turn_latency`（该轮全部时延口径）、`controller.tts_status`（结算态降级原因）、`controller.tts_prefetch_miss`（句间欠载）、`audio.player_first_pcm_submitted`／`audio.player_enqueue`／`audio.player_drained`（播放位置与字节数）、`tts.router_switch`／`tts.local_model_switch_*`（后端与模型选择）。

排查入口：先用 `turn=` 串起 `session_policy → request_start → request_complete`，再用同一行的 `rtf`／`ttfb_ms` 与 `controller.tts_prefetch_miss` 对齐句间停顿；失败时直接看 `request_failed` 的 `stage`／`error_type`。

### 诊断开关

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `VOICE_LOG_LEVEL` | `INFO` | 业务模块日志级别；`DEBUG` 额外给出 `audio.player_enqueue` 等逐块明细 |
| `VOICE_TTS_NATIVE_TRACE` | 关 | 传入原生 `--log --log-file`，写入独立 `audiocpp-trace.log`（扰动耗时，仅用于阶段划分） |
| `VOICE_TTS_LOG_TEXT` | 关 | 在本地合成日志中追加 120 字文本预览；关闭时只记字符数与 `text_digest` |
| `VOICE_TTS_NATIVE_LOG_PUMP` | 开 | `0` 回退为原生 stdout 直接重定向（不加时间戳） |
| `VOICE_LLM_CONTEXT_AUDIT` | 开 | 完整模型请求正文审计；这是唯一会写入真实会话文本的日志 |

## 原生日志

原生 `audiocpp_server.exe` 的 stdout 由父进程管道转发，每行加本地时间戳后写入 `audio-cpp-tts.log`；该文件在每次启动前超过 4 MiB 时轮转为 `.1`。转发线程只读取不解析，任何异常都降级为"继续排空管道"，不会让原生进程因管道写满阻塞。`_service_ready()` 仍按 HTTP `/health` 判定，不读取日志文件。

`VOICE_TTS_NATIVE_TRACE=1` 时额外传入原生自身的 `--log --log-file`（写入 `audiocpp-trace.log`）。该模式逐行刷新并开启张量跟踪，会扰动绝对耗时，**只用于定位阶段划分，不能作为无刷新扰动的性能口径**。

## 日志容量预算

日志目录（工作区 `cache/logs`，开发渠道为 `.local/cache/logs`）保留 **100 MiB** 总预算，覆盖 `*.log`、`*.jsonl` 及其数字轮转备份；同目录的 `startup-handoff-error.json` 等非日志文件不参与。

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `VOICE_LOG_BUDGET_MB` | `100` | 目录总预算；`0` 完全关闭剔除 |
| `VOICE_LOG_SWEEP_SECONDS` | `300` | 常驻巡检周期（每个进程一个守护线程） |
| `VOICE_LOG_MIN_AGE_SECONDS` | `60` | 只剔除安静超过该秒数的文件，避免与并发轮转竞态 |
| `VOICE_LOG_BUDGET_WATCHDOG` | 开 | `0` 关闭巡检线程，仅保留启动与轮转时剔除 |

触发点：`configure_logging`（各工作进程启动）、宿主 `application.log` 初始化、监督器组件日志轮转（4 MiB）、声纹子进程 stdout/err 打开前（2 MiB）、原生服务启动前（4 MiB），以及巡检线程。

### 剔除范围与顺序

剔除面对**全部类别**的日志文件，没有按类别配额：候选是所有 `*.log`、`*.log.<数字>`、`*.jsonl`、`*.jsonl.<数字>`，把它们放在一起按 `(修改时间, 文件名)` 升序排列后逐个删除，直到总量回到预算内。因此先走的总是在场最旧的轮转备份，可能是主服务日志、子进程日志、组件 stdout 镜像、原生 stdout 或审计 `jsonl` 中的任意一类，不会只盯着某一类反复删。修改时间相同的文件按文件名稳定排序，结果可复现。

被排除在候选之外的只有两类：安静时间不足 `VOICE_LOG_MIN_AGE_SECONDS` 的文件（避免与并发轮转竞态），以及本进程当前打开的文件。

### 运行中文件的锁定占用

Windows（操作系统）上打开的文件无法删除：

| 持有方式 | `os.unlink` 结果 |
| --- | --- |
| 本进程打开（追加写） | `PermissionError`，`winerror=32`（共享冲突） |
| 另一进程打开（追加写） | `PermissionError`，`winerror=32` |
| 句柄关闭后 | 删除成功 |

对应处理分两层：

1. **本进程自己持有的文件**：`configure_logging` 的日志与审计 `jsonl`、宿主 `application.log`、运行中的原生 `audio-cpp-tts.log`／`audiocpp-trace.log` 都在打开时通过 `protect_log_files` 登记，句柄关闭时用 `unprotect_log_files` 撤销（避免文件被永久排除在剔除范围之外）。登记过的文件在任何剔除路径（启动、轮转后、巡检）下都不会被尝试删除，也不会被记成"跳过"。
2. **其它进程持有的文件**（组件 stdout 镜像 `application-<组件>.log`、Rust 侧 `desktop-service.log`／`frontend-dev.log`、声纹子进程 `speaker-worker.out/err.log`）：删除必然失败，实现捕获 `OSError` 后记入 `skipped` 并**继续**处理其余文件，不会中断、不会抛出，也不会因此漏删别的类别。追加式日志的轮转（`rotate_oversized_log`）同样在占用时返回"未轮转"而不报错。

代价是诚实的：被占用的文件在持有期内无法收缩，此时总量可能暂时高于预算，日志会落一条 `log_budget.over_budget ... reason=files_held_open_or_too_recent` 说明原因，待持有方退出后由下一次剔除回收。这就是单文件上限同时下调的原因——把"存活进程打开的文件之和"压到约 65 MiB，留出余量。

## 已知边界

- 被存活进程打开的文件在 Windows 上无法收缩，预算保证的是"可删除文件集合"内的总量；Rust（桌面宿主）持有的 `desktop-service.log` 属于这一类。
- 原生分阶段计时依赖原生程序已有的 `--log` 通道。
- 日志不保存 PCM（脉冲编码音频）波形；主观音质问题仍需隔离试听或重放工具，不能仅凭日志判定。
- 在线（MiniMax）后端保持原有日志口径，只接受统一的 `speech_id` 形参以便调用方统一。
