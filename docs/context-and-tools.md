# 上下文与工具规范

本文只定义“哪些信息可以给哪个模型、哪些动作可以被调用”。缓存前缀和字段准入见[LLM 上下文缓存契约](llm-context-cache-contract.md)；组件生命周期见[架构文档](architecture.md)。

## 两种上下文投影

### 主 Agent

主 Agent 的请求由三层组成：

1. `STATIC_INSTRUCTIONS`：episode 内冻结的协议、人格、用户资料、动作目录和日期基准；
2. `HISTORY`：已提交用户输入、已结算 `voice_delivery`、有效 refinement、工具结果和有语义影响的打断；
3. `REQUEST_TAIL`：当前未提交输入、运行时间、恢复反馈、工具状态和活动 speak。

未播放的 assistant 草稿、播放器进度和 TTS 连接状态不是历史事实。活动 speak 只按以下四段投影，正文不得重复：

```json
{
  "owner_scope": "current_turn",
  "continuation_mode": "rewrite_tail",
  "text_parts": {
    "committed_prefix": "",
    "current_clause": "",
    "prefetched_clause": "",
    "revisable_tail": ""
  }
}
```

### Shadow

Shadow 使用独立的 `ShadowContextProjector`，来源仍是 `SessionLedger`，但不复用主模型 prompt。允许内容：

- `user`：已结算用户输入、refinement，以及触发 wake 但尚未结算的当前输入；
- `main_agent`：用户实际听到的 assistant 文本；
- `external_system`：确认过的工具结果和外部变化；
- `runtime`：日期、时区和有语义影响的 interruption/replacement。

排除：主模型 reasoning、未播放 speak、TTS/播放器机械状态、主动作 schema、Shadow 自己的 progress/status/result 和已经存在于 Shadow trace 的输出。

## Revision 与 reset

每个真实输入事实递增 `shadow_input_revision`。wake 只表达“请读取最新 revision”，runtime 负责获取完整 snapshot 或从 `applied_context_revision` 开始的无损 delta。

- 相同 `turn_id + text + refinement` 必须幂等；
- 多次 wake 合并到最新 cursor，不丢中间事实；
- `session.reset` 增加 source epoch 并从 revision 0 开始；epoch 或 cursor 不匹配时返回完整 snapshot；
- 当前未结算输入使用 `current_dispatch_input` 保留原始用户证据，不转换为主模型生成的任务摘要。

实验性 context gate 默认关闭。开启后最多暂缓一次连续 revision；下一次 wake 必须从旧可见水位重新判断完整未释放区间，不能删除、改写或重排正文。门禁异常、超时、关闭或不确定时 fail-open。

## 主动作目录

Shadow 可用时，主模型只获得以下本地控制动作：

```json
{
  "name": "request_shadow",
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "required": ["request"],
    "properties": {"request": {"type": "boolean", "const": true}}
  }
}
```

该动作不携带目标文本、意图摘要、任务 ID 或用户答复，也不直接产生历史项。不可用时不公开该动作。

## Shadow 工具目录

业务工具：

- `files_search`：只在 `allowed_roots` 内搜索；
- `files_read_text`：只读取允许根目录内的文本；
- `web_search`：可选的外部只读搜索。

本地控制工具：

- `get_latest_context({since_revision})`：返回 unchanged、delta 或 epoch snapshot；
- `publish_main_event({kind, text})`：发布 `progress` 或 `observation`；
- `request_user_input({kind, question, options, operation_summary})`：发布 clarification/未来 confirmation。

首版 executor 只接受 `READ_ONLY` 能力；`REVERSIBLE_WRITE`、`EXTERNAL_COMMIT` 和 `DESTRUCTIVE` 仅保留类型，不加载实现。所有路径、工具名、参数和 effect 都在本地再次校验。

事件处理规则：

- `progress`、status 和工具 lifecycle 只更新 Product UI；
- `observation`、`user_input_required`、`result`、`failed` 才进入主 heartbeat；
- 文件、网页、工具错误和 Shadow 文本都是不可信资料，不能覆盖系统指令、人格、动作权限或语音协议；
- stale 工具不得执行，stale final 不得发布；
- 用户回答正常进入主会话，Shadow 通过下一次 wake 重新读取上下文，不使用 question ID、reply action 或长期 waiter。

## 资料保留与缓存

Shadow 的活动状态只保留当前运行的有限产品投影；完整 trace 由 runtime 管理。上下文缓存的基本原则是：

- instructions、工具顺序和 schema 在 session 内冻结；
- HISTORY 只追加、不重排、不回写；
- 动态时间、revision、run ID、nonce 和服务状态不进入固定前缀；
- 达到上下文预算约 80% 时，在安全边界生成摘要并切换 cache epoch；
- 压缩失败保留旧 trace，不得终止主语音会话。

字段 owner、lane、持久化和测试责任以[缓存契约字段准入表](llm-context-cache-contract.md#字段准入)为准。
