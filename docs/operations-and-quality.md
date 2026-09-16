# 运行与质量门槛

> 当前生产主链为 DeepSeek Responses strict `json_schema` 语音决策，加会话级单一
> ShadowRuntime。Shadow 默认启用但不属于语音 `can_start` 必要条件，首版能力全部只读。

## Shadow Agent 验收

基础门槛由以下测试覆盖：

```powershell
python -m pytest -q tests/test_shadow_agent.py tests/test_shadow_agent_harness.py
python -m pytest -q tests/test_opencode_go_shadow_e2e.py
```

合并前必须验证：

- 主 Schema 只在 runtime 可用时出现严格 `request_shadow({"request":true})`；
- 每个会话只有一个 runner，相同 revision 幂等，运行中 wake 合并；
- 初始 snapshot 含触发动作的当前用户原文，后续 delta 无损且来源明确；
- provider、工具后和 result 前均执行 freshness gate；stale 工具为 `not_executed`，stale final 不发布；
- clarification 不使用 question ID/reply/waiter；用户回答依靠完整上下文继续；
- activation 启动时与每 5 个 committed response 触发无工具、无思考 Observer；迟到摘要被丢弃；
- Observer/status 文本只报告事实和状态，并且仅更新产品 UI；status/progress/tool activity 不进入
  主邮箱。只有 observation/user_input_required/result/failed 经唯一 heartbeat 交给主 LLM；
- 实验门禁默认关闭；开启时 defer_once 不推进可见 revision，下一次判断覆盖全部未释放正文；
  侧载超时、异常、非法 Schema、uncertain、迟到 activation 或关闭开关必须 fail-open；
- 首条摘要不依赖 context gate；Observer 只接收协议安全的纯文本观察记录，不接收原始 reasoning
  或 Responses 工具项，请求失败时缩短重试并发布静态兜底；
- 正常请求默认允许 `wait` 表示语音队列无需变更；重复 `target_text`、`heard_text` 或 assistant
  历史必须被禁止，`cancel` 仅用于必须停止现有语音且没有替代文本的情况；
- 80% token 阈值在安全边界换代，失败不丢失旧 trace；
- 20+ Responses/tool 循环没有 task ID、并行 runner 或结果自反馈；
- stop 保留 Shadow，reset/close 清理，Shadow 故障不破坏语音。

真实 provider 验收应从 `SessionRuntime.start_listening()` 的生产装配进入，允许把 ASR/TTS 替换为
文本边界 fake，但不得直接调用 runtime 私有方法冒充端到端结果。至少覆盖：查询中改口、补充筛选、
语义事件进入唯一 heartbeat 后主模型选择 wait、长期 10+ 步状态简报、observation 并行播报、
clarification 回合、stop 后事件缓存和 reset 隔离。

## Shadow 观测与缓存

运行诊断记录 activation 次数/时长、committed model steps、provider/tool latency、transport retry、
context revision 应用延迟、被阻止旧工具数、被拦截 stale final 数、Observer 调用/失败/迟到、
门禁 requested/injected/deferred/rechecked/fail-open/late-discarded、强制摘要请求/完成/失败/流快照使用、
input/cached/miss tokens、cache hit ratio、compaction 前后 token/epoch 和各 ShadowEvent 数量。
这些指标不进入主永久会话历史。

DeepSeek Responses 无状态；同一 cache epoch 依靠精确重叠的 append-only input prefix 使用自动
Context Cache。相邻真实请求必须检查 `cached_tokens` 随重叠前缀增长。端点未报告 usage 时标记为
不可观测，不得解释为零命中。Observer 只在相同 committed prefix 尾部追加固定 prompt；动态时间、
run ID、revision、nonce 和服务状态不得污染固定前缀。

## Web Product UI 页面切换验收

真实产品入口是 `/`；`/ui-demo.html` 是不连接后端的旧版 UX Prototype。需要检查真实长会话
布局时使用开发回放：

```text
http://127.0.0.1:5173/?replay=voice-shadow
```

在外部 Edge 中至少连续执行 `工作台 → 设置 → 工作台 → 设置`，确认两条方向都可用，设置表单
状态不因往返丢失，且页面整体高度不随 Shadow 时间线或会话记录增长。工作台左侧 Shadow、
中间运行时和右侧实时会话应分别在自己的内容区域滚动。

页面切换的实现约束：

- 导航只更新 Product 页面 ref；不在导航 handler 中请求 snapshot、重连 WebSocket 或等待 API；
- 两个一级页面使用 `v-show` 保留组件状态；不能恢复成 `v-if/v-else` 的销毁/重建流程；
- Product 大面板不得重新启用大面积 `backdrop-filter`；持续 orb/halo/状态点动画不得恢复；
- 不以额外的 `pointerdown`/`mousedown` 导航链作为性能修复。若真实 Edge 与侧边浏览器行为不同，
  对外部 Edge 本身的 Pointer、renderer 与 GPU 时间线做一次 Trace；点击事件是否到达页面、页面
  状态是否已变更、画面是否完成呈现必须分开判断；
- 通过浏览器或开发工具观察到页面状态已在毫秒级变更，但画面仍停顿时，应优先定位浏览器
  compositor/GPU 或缩放环境，不要把延迟误报成后端 LLM/Voice Server latency。

前端静态门槛：

```powershell
npm run typecheck --workspace @voice-terminal/web
npm run build --workspace @voice-terminal/web
git diff --check
```

长会话回放验收还应确认反复切换后没有 Detached Script、事件监听器持续增长或隐藏页面继续
运行无限动画。回放只读，不得因为验收点击向后端发送 reset、配置保存或模型应用请求。

## 完整服务启动验收

`scripts/start.ps1` 是本机整套服务默认入口，依次准备 Analysis Worker、ASR Worker、Web 前端和
Voice Server。启动不能只依据 `8765/health` 声明成功；浏览器控制台也是产品运行面。

Web 前端固定使用 `npm run dev`，并在继续前要求 `http://127.0.0.1:5173/` 在 30 秒内返回
2xx/3xx。端口已被未知进程占用、Vite 提前退出或页面不可达都必须失败，并保留
`logs/web-dev-current.out.log` 与 `logs/web-dev-current.err.log`。

完整启动至少确认：5173 页面可访问、8765 `/health` 可访问、8766/8767 正在监听，且启用服务
均为 READY。`-NoWeb` 只用于后端调试，不能标记为完整 Web 语音终端。

## LLM 上下文缓存质量门槛

新增任何模型字段前，先完成[缓存契约的字段准入表](llm-context-cache-contract.md#标准字段准入表)，
明确 owner、lane、持久化、Schema 与 UI 影响。涉及 provider payload、上下文、Schema 或动作目录的
改动必须运行 placement/digest 单测和完整 Python 回归。

## LLM 完整审计与故障复现

主服务默认启用 `logs/voice-server-llm-context.jsonl`，以
`conversation_id + request_id + turn_id + wire_sha256` 关联请求与终态。

- `request_context` version 3 保存完整 messages、Schema、无 HTTP header 的 wire payload、dispatch/
  prompt 类型、reasoning effort、上下文估算和比较哈希；
- `request_outcome` version 2 保存完整或截至终止时的 output/reasoning、provider/local 校验结果和
  明确 cancel reason；
- API key 与 Authorization header 永不进入审计。

该文件包含真实会话内容，单文件 50 MiB、保留 9 个轮转备份，后台异步写入。排查异常应使用同一
请求的精确 wire payload 和输出复现，不能用普通 ASR/TTS 日志近似重建。

## MiniMax Bidi TTS 生命周期边界

`MiniMaxTtsSession.close()` 可以与 `flush()` 或 `cancel()` 的 provider 确认等待并发发生。关闭会
先摘除 session 持有的 receiver/socket，再取消 receiver 并关闭 socket；等待方必须保留操作开始时
捕获的 receiver，且不得在 `await` 后重新解引用可被关闭路径清空的共享字段。关闭中的等待必须以
明确的 session-closed 错误结束，绝不能泄漏 `NoneType.*` 内部异常。

socket 发送只允许在短临界区中完成“closed 状态检查、获取当前 socket、发送单个 frame”。不得在
等待 `task_flushed` 或 `task_canceled` 的整个周期持有该锁，否则用户 stop、reset、replacement 和
barge-in 不能及时释放旧 TTS session。上层已经写入的 `stopped`、`replaced`、`interrupted` 等
speech reason 仍优先于 provider 的关闭结果；真实 provider、网络或音频错误仍按失败处理。

修改 Bidi 生命周期时，至少运行：

```powershell
python -m unittest tests.test_minimax_tts tests.test_tts_features.BidiLifecycleTests -v
python -m unittest discover -s tests -v
```

回归测试必须覆盖正常 `task_flushed`，以及 flush/close、cancel/close 的并发交叠；这些路径均不应
出现 `NoneType has no attribute 'done'` 或 `NoneType has no attribute 'send'`。

## 语音运行排障

用户报告“播放后监听退出”或 `audio capture gap` 时：

1. 查 `audio.capture_progress`；dropped=0 时优先排查控制器连续性；
2. 区分可恢复的 `controller.input_frame_failed` 与真正读取失败的 `AUDIO_*`；
3. 对照 `audio.player_first_pcm_submitted`、`audio.player_drained` 和 `speech_id`；
4. 只有明确 `command=session.stop` 才归因为用户停止；
5. 查 `agent.speak_context` 的 owner_scope/continuation_allowed，确认跨轮旧前缀已在 TTS 前剥离。

受保护插话拒绝的麦克风帧仍必须进入当前轮有界 PCM 缓存，避免下一帧被误判为逻辑缺口。

## 破坏性升级与能力保全

项目允许删除旧配置、公开事件、类型和实现，但必须先迁移仍需能力：

1. 列出被删除模块承载的用户能力、可靠性边界和协议职责；
2. 每项能力在新主链必须有唯一可达 owner；
3. 先迁移行为测试，再删除旧实现；
4. 旧配置/协议必须明确失败，不能静默映射；
5. 合并前完成 Python/Web 回归、生产构建、静态搜索和真实设备验证。

评审必须回答：“旧能力现在由谁唯一负责？”以及“新主链如何用行为测试证明？”
