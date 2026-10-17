# 本地 TTS 可观测性与日志容量

本指南定义本地语音合成的日志关联、诊断开关、文件轮转预算和隐私边界；日志仅用于诊断，不改变合成策略、模型、音色、推理步数、播放调度或用户设置。

## 日志关联与隐私边界

正式本地播报事件用 `turn=`（用户轮次）、`speech=`（播报事务）和 `session=`（原生上下文）关联，事务预建会话也携带这些标识。预热会话没有正式播报事务，`speech=None`（无正式播报事务）属于预期，按已有轮次与会话号关联。服务生命周期按 `pid`（原生进程编号）关联。用户输入定稿事件使用当前输入轮次，避免沿用上一输出所有者。

普通日志默认不含会话文本，只记录字符数与文本摘要。只有显式设置 `VOICE_TTS_LOG_TEXT=1`（允许记录有限文本预览）才追加预览；`VOICE_TTS_NATIVE_TRACE=1`（启用原生分阶段跟踪）将原生追踪写入独立日志。原生输出由父进程逐行添加时间戳，便于和语音服务时间线对照。

## 本地合成日志字段

一次本地合成从会话建立到分段完成由以下事件串起，全部带 `turn=`／`speech=`／`session=`：

```text
audio_cpp_tts.session_policy      模型、音色版本、续接、预热/试听、推理步数、采样率
audio_cpp_tts.request_start       分段序号、字符数、续接、步数、文本摘要
audio_cpp_tts.first_audio_ready   每段第一个非空音频块抵达、等待时长和首块音频长度
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
audio_cpp_tts.runtime_identity    程序构建、实际原生程序路径与摘要、安装版本、收据匹配状态
audio_cpp_tts.runtime_config      原生计算后端、设备编号、线程数与计算图/缓存参数
gpu_performance.applied/restored  本地合成期间的显卡性能模式
```

轮次侧补充 `controller.turn_latency`（该轮全部时延口径）、`controller.tts_status`（结算态降级原因）、`controller.tts_prefetch_miss`（句间欠载）、`audio.player_first_pcm_submitted`／`audio.player_enqueue`／`audio.player_drained`（播放位置与字节数）、`tts.router_switch`／`tts.local_model_switch_*`（后端与模型选择）。

排查入口：先用 `turn=` 串起 `session_policy → request_start → request_complete`，再用同一行的 `rtf`／`ttfb_ms` 与 `controller.tts_prefetch_miss` 对齐句间停顿；失败时直接看 `request_failed` 的 `stage`／`error_type`。

### 默认首音时间线与阶段口径

新增记录默认随普通 INFO（信息级）日志输出，每段只增加一次首块事件，首音入队与提交每个播报事务各记录一次。

| 事件或字段 | 中文说明 |
| --- | --- |
| `audio_cpp_tts.first_audio_ready` | 每段首个原生音频块已抵达并完成解码；包含轮次、事务、会话和分段号 |
| `controller.tts_first_pcm_enqueued` | 本轮首块音频已进入播放器，`key`（播放键）连接设备提交事件 |
| `ready_to_enqueue_ms` | 合成首音就绪至首次入队的毫秒数，包含播放许可等上游门槛等待 |
| `audio.player_first_pcm_submitted` | 设备回调首次消费该播放键的音频，尚需考虑硬件实际出声延迟 |
| `queue_to_submit_ms` | 首块入队至设备回调首次提交的毫秒数 |
| `http_headers_ms` | 提交网络请求至响应头抵达的毫秒数，不等同于纯原生计算耗时 |
| `chunk_count`、`max_chunk_gap_ms` | 本段非空音频块数量、相邻块在应用侧抵达时间的最大间隔毫秒数；它表示音频供应节奏，不表示播放间隙 |
| `full_decode` | 该请求是否使用完整解码试听路径 |
| `engine_ttft_ms` | 原生流式完成事件报告的首音等待毫秒数；未提供时为空 |
| `engine_wall_ms`、`engine_rtf`、`engine_audio_duration_ms` | 原生完整任务报告的耗时、实时因子和音频时长；未提供时为空 |
| `controller.tts_sentence_window_wait` | 句窗实际等待超过50毫秒时，记录等待时长与前后剩余音频量 |
| `audio_cpp_tts.prewarm_cancel_wait` | 正式请求取消尚未完成的预热时，记录取消等待毫秒数 |
| `app_build`、`binary_path`、`binary_sha256` | 当前程序构建、实际启动的原生程序路径与完整摘要 |
| `installed_version`、`receipt_path`、`binary_matches_receipt` | 对应目录收据的安装版本、路径、实际程序与收据摘要是否一致 |
| `target_version`、`binary_matches_target` | 当前随包清单要求的原生版本、实际运行程序是否与该目标摘要一致；用于发现新版应用仍使用旧组件 |

先比较原生首包、首音至入队、入队至设备提交三个阶段，再对照整段实时因子和最大块抵达间隔。`max_chunk_gap_ms`（最大音频块抵达间隔）按音频块到达应用的时间计算；85 毫秒只说明两块到达时刻相差最多 85 毫秒，不能据此判断播放是否停顿。判断播放器因队列耗尽而断流，应看 `underrun_ms`（播放器欠载静音毫秒数）：它累计设备回调因队列为空而输出静音的时长。`controller.tts_prefetch_miss.miss_ms`（预取事件欠载毫秒数）是整轮累计欠载，不能逐条相加。复用已有原生服务时，若本次没有创建进程，日志明确记录 `runtime_identity_unavailable`（无法证明被复用进程身份），不会把候选文件当成运行映像。

显卡进程查询按接口版本使用真实结构布局和驱动返回的实际数量；查询失败通过 `process_query_available`（计算/图形进程表可用状态）与 `reason`（不可用原因）标明。`gpu_sample_ms`（显卡取证耗时毫秒）记录后台线程的采样耗时。进程表只能证明观察到哪些显卡进程，不能单凭数量推断算力抢占或根因。

原生日志转发读取当前可用数据，不再等待凑满4096字节；前缀时间仍是父进程收到输出的时刻，不能替代原生内部事件时钟。

上述轻量汇总不包含预填充、解码器计算图容量等内部阶段。更细原生追踪继续由下面的诊断开关显式启用，单独分析其对耗时的扰动。

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
