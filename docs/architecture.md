# Voice Terminal 架构与组件契约

## 总体结构

系统由两个并行但权限隔离的平面组成：

```text
用户 → ASR → 主 LLM → Speech transaction → TTS / Player
                    └→ request_shadow({"request": true})
                                  │
                                  ▼
权威会话上下文 → 单一 ShadowRuntime → Responses / READ_ONLY tools
                                  ├→ progress / status
                                  ├→ observation
                                  ├→ user_input_required
                                  └→ result / failed
                                             │
                                             ▼
                                 主 LLM 400ms mailbox
```

主语音平面继续由 `SessionRuntime → EpisodeController → AgentDecisionScheduler +
EpisodeSpeechOutput` 构成。Shadow 是同一会话持有的认知执行平面，不拥有语音、TTS、
播放器或主调度器的控制权。

## 主语音平面不变量

- 所有主 LLM 请求仍由一个 400ms 单飞调度循环管理。
- `speak` transaction、传达感知 ledger、barge-in、TTS generation 与播放器语义不变。
- 主动作目录只有可选 `request_thinking` 和可用时的 `request_shadow`。
- `request_shadow` 的参数严格且只能是 `{"request": true}`，可和非空 `speak`、
  `request_thinking` 同时出现。
- system-check 与 final-silence 禁止任何 action。
- 完整 decision 通过本地校验后才后台 wake；主决策和语音输出不等待 Shadow。

### 语音事务的发送前沿（admission）

一个播报事务中的文本有两条线：**已接收**（`SpeechHandle.text`，模型本次输出的全部文本）和
**已送出**（`sent_chars`，真正交给 TTS provider 的字符数）。只有已送出的部分不可撤回。

`speak_snapshot` 因此同时给出 `text`、`sent_chars`、`rewritable`、`played_ms` 和
`synthesized_ms`。判定一个到达的 decision 是否与正在播放的内容冲突时，基准是**发送前沿**
而不是整段已接收文本：

- 新文本仍在发送前沿之内 → 等待，尚不能判定；
- 新文本以发送前沿开头 → 未送出的尾巴可以就地替换，已播出的前缀照常播放；
- 分歧落在发送前沿之内 → 已生效内容确实要改，走原有的替换路径。

`AppConfig.tts.scheduling: playback_window` 为 Bidi 启用发送闸门：只有当 provider 已返回但
尚未播放的音频低于 `tts.admission_lead_ms` 时，才放出下一帧文本。这个窗口只控制**文本发送
时机**，与旧的“等待单段合成完成”语义无关——Bidi 服务端自行断句，一个发送帧不等于一个
服务端句子，所以后者在 Bidi 上始终关闭。发送暂存区不阻塞生产者：把播放时钟反压到 LLM 读取
循环会让长回答停滞到 provider 超时。暂存区超过字符预算时放弃窗口并直接发送（记
`tts.admission_overrun`）。`scheduling: direct` 完全关闭该机制。

窗口的测量对象是 **provider 已返回的音频**，因此每次发送后必须先等 provider 为这一帧返回
PCM，再重新测量。一段回答刚开始时尚未返回任何音频，`remaining_ms` 读数为零，窗口会一直
敞开：实测中整段 20 帧文本在 48 毫秒内被一次性送出，provider 随后又跑到播放前面 28 秒，
闸门形同不存在。这个等待有上限（`ADMISSION_CATCH_UP_SECONDS`，0.5 秒）且不致命，provider
较慢时退回原来的直接发送行为，不会把事务降级为失败。

用户 barge-in、AEC 失败回退、`speak.state=cancel` 与 `session.stop` 仍然是立即中止，不经过
这条柔性路径。

主语音状态的决策先回答“是否需要变更当前语音队列”：`wait` 是正常调用中的通用队列不变更
决策，适用于没有新内容、现有语音仍正确、内容已经传达、Shadow 只是状态或事件已经过时等情况；
`speak` 只用于仍相关且尚未传达的新内容；`cancel` 仅用于必须停止现有待播/播放内容且没有
可立即替换文本的情况。`continuation_allowed` 不表示需要重播 `target_text`，没有新增后缀时
必须 `wait`。KWS 等明确要求确认的系统场景是显式例外。

## 单一 ShadowRuntime

每个 session 只有一个 `ShadowRuntime`、一个活动 runner 和一份私有 committed trace。
activation 结束后 runtime 回到 idle，但工作摘要和 cache epoch 保留到 reset/close。
不存在任务字典、task ID、任务队列、modify/reply/cancel、checkpoint 或业务状态机。

`wake()` 只读取当前 `shadow_input_revision`：空闲时启动，运行中合并到最新 revision，
相同已处理 revision 返回 `already_current`。这些结果仅用于日志，不进入主 ledger。
动作校验后 wake 立即作为后台任务提交，不等待 TTS、播放、思考租约或下一次心跳。
Shadow runner、provider 与只读工具完全由 session runtime 驱动；语音 episode stop 或心跳忙碌
都不会暂停它们。

Shadow 使用普通 Responses function-calling 循环。只提交 `response.completed.output`；
SSE partial reasoning/text 不进入 committed trace。Shadow 的 activation、工具调用轮次、调用次数、
provider 请求时长和 transport retry 均不设本地上限；文件/搜索工具运行 8 秒后软返回，
底层只读工作继续运行并在原 activation 活跃时回注完整结果；
reset、close、取消、provider 协议终止
以及外部服务本身的限制仍然有效。activation 不设总墙钟或总步骤上限。
activation 从启动起按 20 秒周期累计运行时间；跨过周期后只在模型响应、工具输出已配对且
上下文可安全追加的边界合并注入一条咨询式收敛提醒。提醒不会取消正在进行的 provider 请求；
已有合法终态时直接发布，不为补发提醒额外增加模型轮次。

DeepSeek thinking+tools 的正常响应按官方协议原样进入 committed trace，跨 activation 继续完整
回传 `reasoning`、`function_call`、`function_call_output` 和 `message`，以保持合法续接和缓存前缀。
若服务端成功返回的单次响应缺少非空 `reasoning_text`，该响应可用于当前结果/工具执行，但不能
在下一次 thinking+tools 请求中原样重放；provider 只把这一批输出转换为协议无关的数据记录，
对应工具结果也使用相同表示。此前所有合法前缀保持不变，不在 activation 边界批量改写历史。

## 权威 Shadow 上下文

`ShadowContextProjector` 从 `SessionLedger` 投影专用上下文，不复用语音模型的 prompt 或
REQUEST_TAIL。它包含：用户输入与 refinement、尚未结算但触发 wake 的当前 ASR 原文、实际
传达的 assistant 文本、具有语义影响的 delivery interruption、确认的工具/外部事件、冻结的
日期和时区。

它排除主 LLM reasoning、未播放 speak 草稿、TTS/播放器机械状态、主 action schema、Shadow
进度文本以及已经存在于 Shadow 私有 trace 的 Shadow 输出。Shadow 事件会进入主 LLM 上下文，
但不会增加 `shadow_input_revision`，因此不能形成反射唤醒循环。

ledger reset 增加 source epoch 并从 revision 0 重新开始。Shadow 的 cache epoch 与 source epoch
分离：换代压缩不会伪装成用户事件。

实验性门禁在 projector 前维护连续可见水位。`source_revision` 是 ledger 权威尾部，
`visible_revision` 是 Shadow 当前可读的最大 revision；可见输入永远是 `(0, visible_revision]`
的连续前缀，不复制、不过滤、不重写 frame。门禁默认关闭，此时可见水位自动追平来源。
运行中开启时，侧载模型只可返回 `inject_now` 或 `defer_once`：只有明确的 irrelevant/duplicate
可以暂缓一次，补充、变更、偏移、引导、不确定和任何错误都立即注入。下一次 wake 必须从旧
可见水位重新判断完整未释放区间；放行较新 revision 时，此前暂缓正文自然按原顺序一起进入。
空闲 wake 永远绕过门禁并读取完整前缀。判断文本、reason 和摘要都不写入 ledger、working trace
或 Shadow 输入。

产品设置 API 使用独立 schema 版本。`PUT /api/v1/product/settings/shadow` 严格接受
`{"experimental_context_gate_enabled": true|false}`，写入成功后立即更新当前 Session 的
门禁状态；切换不会停止语音、取消 Shadow 或创建新的会话。旧 schema 设置文件缺少 `shadow`
段时自动补 `false`，并保留 wake、silence 和 Persona 配置。

## 引导式上下文更新

运行中 wake 只更新 `pending_context_revision`，不打断在飞 provider 或已经开始的只读工具。
安全边界位于 provider 前、工具批次后、尚未开始的调用前和 result 发布前。

若旧 response 刚产生工具调用而新上下文已到达，调用不会执行，所有 call 都写入：

```json
{"status":"not_executed","reason":"conversation_context_updated"}
```

随后追加无损 context delta 和固定意图重判提示。工具执行期间到达更新时，工具自然完成并
保留真实结果，但下一 provider 步必须先看到更新。任何 stale final 都只能留在私有 trace，
不能越过 pending revision 发布。

## 工具与事件

首版业务工具必须声明 `READ_ONLY`；executor 在 runtime 构造时拒绝其他 effect。预留
`REVERSIBLE_WRITE`、`EXTERNAL_COMMIT`、`DESTRUCTIVE`，但不加载真实实现。

除文件/搜索工具外，固定暴露三个本地端口：

- `get_latest_context(since_revision)`：返回 unchanged、无损 delta 或 epoch resync snapshot；
- `publish_main_event(kind, text)`：发布 progress 或 observation；
- `request_user_input(kind, question, options, operation_summary)`：发布 clarification/未来 confirmation，
  并结束本次 activation。

`progress`、`status`、工具 lifecycle 和所有 activity 只覆盖产品投影/UI，不写主邮箱、不触发
heartbeat。只有 `observation`、`user_input_required`、`result`、`failed` 进入主语义 ledger 和
现有 400ms mailbox，并继续由唯一心跳串行消费。事件文本是异步参考资料而不是播报指令，可能晚于最新会话状态；
主 LLM 必须结合最新用户输入、assistant 历史和 `runtime.speak` 自行决定 `speak`、`wait` 或
`cancel`，不得重复已经传达或当前队列已经覆盖的内容。

## StatusObserver

每次 activation 启动时以及每 5 个成功提交的 Shadow provider response 触发一次无状态只读观察。Observer 使用
协议无关的会话、已提交模型阶段和完整工具观察记录，`reasoning_effort=none`、`tools=[]`、
`max_output_tokens=512`，不设本地请求 timeout；
只返回 80～160 字工作简报，内容只陈述已确认事实、正在核实事项和实际阻塞，不得建议主助手
说什么、询问什么、调用什么动作或选择何种语音状态。它不计入 Shadow model step、不写回 trace、
不阻塞主循环；同时只有一个请求，重复触发合并。`status` 只进入产品摘要槽位，不进入主 LLM。
首条 Observer 在权威快照应用后立即启动，与实验性 context gate 无关。Observer 不读取原始
reasoning tail 或 Responses 工具协议项；首次失败时用更短的安全记录重试，再失败则发布本地静态运行状态。
activation 已结束、reset/close 或已有更新状态时丢弃迟到摘要。

## 缓存与换代压缩

同一 cache epoch 内 input item 只追加、不重排。工具名/顺序/Schema 在 session 内冻结；动态
revision、时间、run ID、nonce 不进入固定前缀。相邻请求依靠完全相同的重叠前缀使用 DeepSeek
自动 Context Cache，并记录 input/cached/miss token 与 hit ratio。

默认在 524288 tokens 的 80% 处于安全边界压缩。压缩器低思考、无工具、结构化输出，优先折叠
私有 reasoning、重复查询和普通工具细节，保留当前意图、授权、关键证据、已完成行动、待询问
信息、真实外部回执和近期会话。成功后创建新 cache epoch；失败保留旧 trace 并在后续边界重试，
不会终止主语音会话。

## 生命周期

- `session.start`：启动语音 episode，并消费 stop 期间缓存的 Shadow 重要事件；
- `session.stop`：只停止语音输入（关闭输入作用域），保留已经派发的 LLM 请求与 Agent/LLM/TTS 输出队列继续结算，
  释放被挂起的播放 hold，且不再为已关闭的输入作用域派发新轮次；Shadow 继续运行；
- barge-in：只影响语音输出；
- `session.reset`：终止 Shadow runner/Observer/门禁/压缩，清空临时 reasoning、trace/status，轮换 ledger epoch 与
  OpenCode session header；
- runtime close：取消 provider/observer 请求，关闭组件和共享 HTTP client；
- Shadow result/no_action/failed：只结束 activation，session runtime 仍可在新 revision 上再次唤醒。

## 主要代码归属

| 能力 | 文件 |
|---|---|
| Shadow contracts/action/events | `voice_terminal/shadow_agent/contracts.py` |
| 专用上下文投影 | `voice_terminal/shadow_agent/context.py` |
| 单一 agent loop/observer/compaction | `voice_terminal/shadow_agent/manager.py` |
| session ownership/event relay | `voice_terminal/shadow_agent/component.py` |
| Responses transport/cache usage | `voice_terminal/shadow_agent/provider.py` |
| READ_ONLY capabilities | `voice_terminal/shadow_agent/tools.py` |
| revision authority/main projection | `voice_terminal/session/ledger.py` |
| main action dispatch/heartbeat accept | `voice_terminal/agent/episode.py` |
| Product UI 页面与会话投影 | `apps/web/src/product/ProductApp.vue` |
| Product API、Voice Server 适配与设置状态 | `apps/web/src/product/useProductAdapter.ts` |
| Shadow 实验开关 API 与持久化 | `voice_terminal/server/product.py`、`voice_terminal/server/websocket.py` |
| 日志回放 fixture（开发只读） | `apps/web/src/product/replay-data.ts` |

Product UI 的一级导航只有 `home` 和 `settings`。两个页面组件可以保留各自的表单/滚动状态，
但隐藏页面必须通过 `v-show` 进入 `display: none`，不能用 `v-if/v-else` 在导航时反复销毁和
重建。导航只更新前端页面 ref，不发 Voice Server 请求，也不等待后端响应。

Product 页使用固定视口和面板内部滚动；长 Shadow 时间线、系统状态和实时会话不得把整个
文档高度撑大。Product 大面板不使用大面积 `backdrop-filter`，运行态的 orb、halo 和状态点
不使用持续的 transform/box-shadow 动画，以避免外部 Edge 在切换页面时重复合成大层。旧版
`/ui-demo.html` 不继承这些 Product 专用性能规则。
