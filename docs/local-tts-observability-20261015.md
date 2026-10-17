# 本地 TTS 可观测性与日志容量（2026-10-15）

本轮补齐本地语音合成链路的日志覆盖，并给运行日志目录加 100 MiB 总预算。修改只涉及日志与诊断，不改变合成策略、模型、音色、推理步数、播放调度或任何用户设置。

## 审计结论与本次闭环

此前逐环节审计确认：日志覆盖本地 TTS 的"两端和边界"，缺少"中间过程"，且本地记录与轮次没有关联。本轮按缺口逐项闭环。

| 缺口 | 现象 | 本次处理 |
| --- | --- | --- |
| A 无轮次关联 | `AudioCppTtsClient.session()` 直接丢弃 `turn_id`，`audio_cpp_tts.*` 全部不带轮次标识 | 会话保存 `turn_id`／`speech_id`，并派生 `session`（原生上下文标识前八位）；所有相关日志行前置三个字段 |
| B 失败无痕 | 十余种 `AudioCppTtsError` 只抛异常并塞入内部队列，provider 全文件仅两条 WARNING | 新增 `request_failed`／`request_rejected`／`segment_wait_failed`／`buffer_overflow`／`prewarm_failed`，失败原因、阶段与耗时全部落盘 |
| C 文本不可见 | 本地只记字符数，在线记前 120 字预览 | 始终记 `chars` 与 `text_digest`（SHA-256 前 12 位）；`VOICE_TTS_LOG_TEXT=1` 时追加 120 字预览 |
| D 原生无分阶段计时 | `--log --log-file` 通道存在但生产从不启用 | 新增 `VOICE_TTS_NATIVE_TRACE=1` 开关，写入独立 `audiocpp-trace.log` |
| E 原生日志无时间戳 | `audio-cpp-tts.log` 只有裸 stdout，无法与 Python 时间线对齐 | 父进程用管道 pump 逐行加 `YYYY-MM-DD HH:MM:SS.mmm` 前缀；`VOICE_TTS_NATIVE_LOG_PUMP=0` 可回退 |
| F 轮次时延只在界面 | `latency.snapshot` 约 22 项指标只发 WebSocket，文件日志只记事件类型 | 新增 `controller.turn_latency`（INFO，按轮一行） |
| G 结算态失败只在界面 | `tts.status=DEGRADED` 只发界面 | 新增 `controller.tts_status`（WARNING，含 `turn_id`、provider、`error_type`） |
| H 路由零日志 | `TtsBackendRouter` 无任何日志 | 记录后端切换、本地排空停止、模型热切换起止与失败 |
| I 事件声明却落不了盘 | `TTS_SEGMENT`／`TTS_PLAYBACK_STARTED`／`TTS_PLAYBACK_ENDED` 不在 `episode.log` 落盘白名单 | 加入白名单 |

保留的隐私口径：普通日志默认不含任何会话文本，文本预览必须显式开启；完整请求正文仍只写入 `voice-server-llm-context.jsonl`。

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

Windows（操作系统）上删除被打开的文件会失败，本机实测确认：

| 持有方式 | `os.unlink` 结果 |
| --- | --- |
| 本进程打开（追加写） | `PermissionError`，`winerror=32`（共享冲突） |
| 另一进程打开（追加写） | `PermissionError`，`winerror=32` |
| 句柄关闭后 | 删除成功 |

对应处理分两层：

1. **本进程自己持有的文件**：`configure_logging` 的日志与审计 `jsonl`、宿主 `application.log`、运行中的原生 `audio-cpp-tts.log`／`audiocpp-trace.log` 都在打开时通过 `protect_log_files` 登记，句柄关闭时用 `unprotect_log_files` 撤销（避免文件被永久排除在剔除范围之外）。登记过的文件在任何剔除路径（启动、轮转后、巡检）下都不会被尝试删除，也不会被记成"跳过"。
2. **其它进程持有的文件**（组件 stdout 镜像 `application-<组件>.log`、Rust 侧 `desktop-service.log`／`frontend-dev.log`、声纹子进程 `speaker-worker.out/err.log`）：删除必然失败，实现捕获 `OSError` 后记入 `skipped` 并**继续**处理其余文件，不会中断、不会抛出，也不会因此漏删别的类别。追加式日志的轮转（`rotate_oversized_log`）同样在占用时返回"未轮转"而不报错。

代价是诚实的：被占用的文件在持有期内无法收缩，此时总量可能暂时高于预算，日志会落一条 `log_budget.over_budget ... reason=files_held_open_or_too_recent` 说明原因，待持有方退出后由下一次剔除回收。这就是单文件上限同时下调的原因——把"存活进程打开的文件之和"压到约 65 MiB，留出余量。

### 实测演练

把真实日志目录整体复制到临时区（不触碰原目录），用 30 MiB 预算演练一次（原目录 36.43 MiB，20 个候选文件）：

```text
removed: asr-worker-llm-context.jsonl      （识别进程审计）
removed: analysis-worker-llm-context.jsonl （分析进程审计）
removed: speaker-worker-llm-context.jsonl  （声纹进程审计）
removed: application-dev.log               （组件 stdout 镜像）
removed: application-voice.log.1           （主服务 stdout 镜像，轮转备份）
removed: application-asr.log.1             （识别 stdout 镜像，轮转备份）
total: 36.43 MiB → 25.68 MiB，skipped 为空
```

六次删除跨越四类文件，全部是各自类别中最旧的轮转备份；`voice-server.log`、`asr-worker.log`、`analysis-worker.log`、`application-*.log`、`audio-cpp-tts.log`、`desktop-service.log`、`speaker-worker*` 的当前文件全部保留。演练目录已删除，真实目录 36.43 MiB 保持原样、未删除任何文件。

单文件轮转上限同步下调，使"存活进程打开的文件之和"最坏约 65 MiB，留出足够余量：

| 文件 | 原上限 | 现上限 |
| --- | --- | --- |
| `voice-server.log` | 10 MiB × 5 | 8 MiB × 3 |
| `asr-worker.log` / `analysis-worker.log` | 10 MiB × 5 | 4 MiB × 2 |
| `speaker-worker.log` | 10 MiB × 5 | 4 MiB × 2 |
| `*-llm-context.jsonl` | 50 MiB × 9 | 16 MiB × 3 |
| `application-<组件>.log` | 5 MiB × (1+3) | 4 MiB × (1+3) |
| `speaker-worker.out/err.log` | 无上限 | 2 MiB × 1 |
| `audio-cpp-tts.log`、`audiocpp-trace.log` | 无上限 | 4 MiB × 1 |

## 验证

```powershell
.local/runtimes/python/python.exe -B -X utf8 scripts/test-backend.py -q tests/test_log_budget.py tests/test_tts_log_correlation.py tests/test_tts_native_log.py
.local/runtimes/python/python.exe -B -X utf8 scripts/test-backend.py -q tests/test_audio_cpp_tts.py tests/test_tts_features.py tests/test_tts_caption.py tests/test_tts_voices.py tests/test_local_tts_optimization.py
.local/runtimes/python/python.exe -B -X utf8 scripts/test-backend.py -q
.local/runtimes/python/python.exe -B -X utf8 scripts/check-doc-links.py
.local/runtimes/python/python.exe -B -X utf8 scripts/verify-local-tts-memory.py --models voxcpm2 voxcpm1 --steps 15
```

结果：三个新增套件 37 项通过（`test_log_budget.py` 21、`test_tts_log_correlation.py` 9、`test_tts_native_log.py` 7）；TTS 相关回归 108 项通过；全量后端 **1525 项通过、3 项跳过**；工程内文档链接检查 383 项、无失效目标。

`tests/test_log_budget.py` 覆盖最旧优先剔除、受保护文件、被占用文件跳过且不中断、非日志文件不动、安静期保护、环境变量解析、追加式日志轮转、处理器轮转后剔除，以及**跨类别全局剔除**（最旧的审计 `jsonl` 先走、同龄文件不偏向任何一类、登记过的打开文件在所有剔除路径下都受保护）。`tests/test_tts_log_correlation.py` 覆盖轮次/事务标识贯通、分段序号递增、失败与拒绝落盘、分段等待失败，以及默认不落文本、显式开启后才出现预览。`tests/test_tts_native_log.py` 覆盖逐行时间戳、转发线程故障后降级排空，以及 `VOICE_TTS_NATIVE_TRACE`／`VOICE_TTS_NATIVE_LOG_PUMP` 两个开关对启动命令和 stdout 装配的实际影响。

配置装配的集成核对（隔离临时目录、3 MiB 预算）：两个最旧文件被剔除，安静期内的新文件、当前活动文件、新建的审计 `jsonl` 与同目录非日志 `json` 全部保留，剔除后总量回到预算内。

实际日志目录（`.local/cache/logs`，36.43 MiB）低于预算，首次剔除为空操作，未删除任何既有日志。

`scripts/verify-local-tts-memory.py` 的会话现在带 `turn_id=selftest-<label>`，隔离真实合成（18884 端口、不开音频设备）后可直接在 `audio_cpp_tts.*` 行上核对三个关联字段非空。实测（`--models voxcpm2 --steps 10`，两轮基线与有界容量）得到 6 条 `request_start`／`request_complete`，同一会话的三段共享 `session=` 且 `segment=0/1/2` 递增，`text_digest` 与字符数齐全，`model_loaded ... elapsed_ms` 与 `server_started ... local_model/log/trace` 均按新格式输出；两轮完整音频逐字节相同。应用侧原始日志保存在 `.local/cache/reports/tts-observability/acceptance-local-tts.log`；原生 stdout 侧同一轮的 `.local/cache/reports/tts-runtime-fixes/native-memory/*.log` 尾部可见新写入的逐行时间戳（前部无时间戳行为改动前的历史内容，按追加模式保留）。

## 已知边界

- 被存活进程打开的文件在 Windows 上无法收缩，预算保证的是"可删除文件集合"内的总量；Rust（桌面宿主）持有的 `desktop-service.log` 属于这一类。
- 原生分阶段计时只能依赖原生程序已有的 `--log` 通道；本轮未改原生 C++ 源码，也未重建原生运行包。
- 本轮不落 PCM 波形，主观音质问题仍需隔离试听或重放工具，不能仅凭日志判定。
- 在线（MiniMax）后端保持原有日志口径，只接受统一的 `speech_id` 形参以便调用方统一。
