# 能力就绪与能力级恢复

本文定义语音系统在**运行期**如何区分“还在加载”“已经失败”和“必须人工介入”，以及如何只恢复一个能力而不重启整个语音服务。启动阶段的全屏加载画面与统一退出仍按[应用生命周期](../guides/application-lifecycle.md)执行。

## 状态口径

能力（capability）标识：`audio`、`asr`、`llm`、`tts`、`kws`、`speaker`、`dsh`、`shadow_agent`、`web_search`。门禁能力为 `audio`、`asr`、`llm`、`tts`（外加启用中的 `kws`）。

| 分类 | 取值 | 含义 |
| --- | --- | --- |
| 就绪 | `READY` `RUNNING` `STREAMING` `CONNECTED` | 可用；监听中的 `RUNNING` 与播报中的 `STREAMING` 都属于正常 |
| 加载中 | `STARTING` `LOADING` `WAITING` `CONNECTING` `RETRYING` | 瞬态；组件显示“启动中”，暂时拦截主监听，不判失败 |
| 失败 | `ERROR` `OFFLINE`（非加载语义时） | 终态；需要人工或自动恢复 |

不变量：`services` 键集合、`SERVICE_READY_STATES` 语义、`retry_allowed` 计算规则与 WebSocket 公共事件名保持不变；新增字段一律只增不改。

## 宿主侧规则

- **探测分档**：`/health` 返回的 `services` 拆成 `loading` 与 `unavailable` 两组，只有 `unavailable` 会写 `core_phase="failed"`；`loading` 写 `core_phase="starting"` 并把组件显示为“启动中”。
- **去抖与宽限**：探测抛异常时按连续次数与首次失败时刻计数，默认 `PROBE_FAILURE_THRESHOLD=3`、`PROBE_GRACE_SECONDS=6.0` 才判故障；未达阈值时组件保持原状态并显示“健康检查重试中（第 N 次）”。探测成功立即清零。
- **次要调用不升级为主故障**：`/internal/application/status` 查询失败只记录日志并保留上一次能力行，不再把 `core_available` 置假。
- **整体状态**：`core_phase` 为 `ready`/`starting`/`failed`，`state` 对应 `ready`/`degraded`/`starting`/`failed`。`startup_complete` 之后出现加载中不再写成启动失败。
- **停止原因可追溯**：`ManagedProcess.request_stop(reason)` 在 `application.log` 记录 `请求停止工作进程：组件=…，原因=…，进程编号=…`；调用方使用 `app-exit` 或 `component-retry:<组件>`。

## 能力级恢复

语音服务内部把 `initialize_session()` 拆成幂等的能力初始化：`_initialize_asr`、`_initialize_llm`、`_initialize_shadow`、`_initialize_tts`、`_initialize_controller`，`initialize_local()` 拆出 `_initialize_audio`、`_initialize_kws`、`_initialize_vad`。每个能力记录自己的最近一次错误（`_record_error` 写入 `_service_errors`），恢复成功后按能力清除。

| 能力 | 恢复动作 | 说明 |
| --- | --- | --- |
| `audio` | `_initialize_audio` | 重建采集与输入处理 |
| `asr` | `_initialize_asr` | 重连识别工作进程；VAD 缺失时显式失败而不是停留在加载中 |
| `llm` | `_initialize_llm` | 重建主模型客户端 |
| `tts` | `_retry_tts` | 重建合成路由并重放当前选择；播放器与设备时钟保持同一实例 |
| `kws` | `_initialize_kws` | 重建关键词唤醒；停用时保持停用 |
| `speaker` | `_initialize_speaker` | 重连声纹工作进程并校验特征 |

调用链：`POST /api/v1/application/capabilities/{能力}/retry` → `ApplicationHost.request_capability_retry` → `retry_capability` → `POST /internal/application/control {action:"capability-retry", capability}` → `SessionRuntime.retry_capability`。

回落规则：语音进程已经退出时直接走整进程重建；组件页对 `voice` 组件发起重试时，只要进程存活且存在待恢复能力，就先做能力级恢复。并发由宿主 `operation_lock` 与语音 `_session_lock` 串行化，同一能力恢复期间拒绝重复请求。

## 主监听门禁

`RuntimeState.can_start_reason` 给出不能开始监听的原因与分类：

| `code` | 含义 | 界面行为 |
| --- | --- | --- |
| `CAPABILITY_LOADING` | 缺口能力全部处于加载中 | 主页按钮显示“组件启动中”并禁用；`session.start` 被拦截并记住意图，能力就绪后自动开始监听 |
| `CAPABILITY_FAILED` | 存在非加载中的缺口 | 沿用会话控制器的错误提示；从本地组件页单独恢复 |
| `SESSION_ACTIVE` / `RUNTIME_ERROR` | 会话进行中 / 运行时错误态 | 沿用既有语义 |

用户主动 `session.stop` 会撤销挂起的开始意图；`_listen_resume_requested` 与既有的 `_tts_resume_requested` 同构。

## 界面呈现

- 全屏启动画面只在 `startup_complete` 为假或应用正在退出时出现；运行期故障改用页面横幅。
- 横幅在 `degraded` 或运行期核心失败时显示，并列出不可用能力名称，入口跳转“本地组件”页。
- 本地组件页在模型行之外增加能力行（音频、主模型、合成等），`启动中` 与 `不可用` 分开呈现，恢复按钮只重建该能力。

## 边界

- 不引入新的常驻看门狗；沿用 `monitor()` 的 2 秒轮询。
- 能力级恢复不替换播放器实例，避免会话控制器持有的引用与播放回调脱离。
- `analysis`（SmartTurn / SenseVoice）仍是独立宿主组件，不并入能力级恢复。
- 资源缺失、配置缺失与启动检查失败仍属于需要人工处理的终态，保持原有入口。
