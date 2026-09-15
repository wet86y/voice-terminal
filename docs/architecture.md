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

## 单一 ShadowRuntime

每个 session 只有一个 `ShadowRuntime`、一个活动 runner 和一份私有 committed trace。
activation 结束后 runtime 回到 idle，但工作摘要和 cache epoch 保留到 reset/close。
不存在任务字典、task ID、任务队列、modify/reply/cancel、checkpoint 或业务状态机。

`wake()` 只读取当前 `shadow_input_revision`：空闲时启动，运行中合并到最新 revision，
相同已处理 revision 返回 `already_current`。这些结果仅用于日志，不进入主 ledger。

Shadow 使用普通 Responses function-calling 循环。只提交 `response.completed.output`；
SSE partial reasoning/text 不进入 committed trace。每个 provider 请求有 timeout 和有限 transport
retry，但 activation 不设总墙钟或总步骤上限。

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

`progress` 和 `status` 覆盖可见状态；`observation`、`user_input_required`、`result`、`failed`
进入主语义 ledger。除 progress 外，事件进入现有 400ms mailbox；主 LLM决定 speak 或 wait。

## StatusObserver

每 5 个成功提交的 Shadow provider response 触发一次无状态只读观察。Observer 使用 committed
prefix 的不可变快照，`reasoning_effort=none`、`tools=[]`、`max_output_tokens=512`、10 秒 timeout，
只返回 80～160 字工作简报。它不计入 Shadow model step、不写回 trace、不阻塞主循环；同时只有
一个请求，重复触发合并。activation 已结束、reset/close 或已有更新状态时丢弃迟到摘要。

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
- `session.stop`：只停止语音 episode，Shadow 继续运行；
- barge-in：只影响语音输出；
- `session.reset`：终止 Shadow runner/Observer/压缩，清空 trace/status，轮换 ledger epoch 与
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
| 日志回放 fixture（开发只读） | `apps/web/src/product/replay-data.ts` |

Product UI 的一级导航只有 `home` 和 `settings`。两个页面组件可以保留各自的表单/滚动状态，
但隐藏页面必须通过 `v-show` 进入 `display: none`，不能用 `v-if/v-else` 在导航时反复销毁和
重建。导航只更新前端页面 ref，不发 Voice Server 请求，也不等待后端响应。

Product 页使用固定视口和面板内部滚动；长 Shadow 时间线、系统状态和实时会话不得把整个
文档高度撑大。Product 大面板不使用大面积 `backdrop-filter`，运行态的 orb、halo 和状态点
不使用持续的 transform/box-shadow 动画，以避免外部 Edge 在切换页面时重复合成大层。旧版
`/ui-demo.html` 不继承这些 Product 专用性能规则。
