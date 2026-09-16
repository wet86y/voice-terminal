# 上下文与工具规范

## 两种上下文投影

主语音模型使用 `LLMContextProjector`：静态协议/人格进入稳定 instructions，会话语义进入
HISTORY，播放器与当前调度状态只进入 REQUEST_TAIL。未传达的 assistant 候选永远不是历史事实。

主模型每次先判断是否需要变更当前语音队列：`wait` 是正常调用中的通用“不变更队列”决策，
不因收到用户、工具、调度或 Shadow 事件就默认开口；`speak` 只用于仍相关且尚未传达的新内容；
`cancel` 只用于必须停止现有待播/播放内容且没有可立即替换文本的情况。KWS 等明确要求确认的
系统场景可以例外。已存在于 `target_text`、`observed_text`、`heard_text` 或 assistant 历史中的
内容不得逐字或换一种措辞重复；`continuation_allowed` 只允许确有新增后缀时追加。

Shadow 使用独立 `ShadowContextProjector`。它读取同一个 `SessionLedger`，但只输出判断用户意图
和执行只读工作所需的权威语义帧：

- `user`：已结算 ASR、refinement，以及触发 wake 但尚未结算的当前 dispatch 原文；
- `main_agent`：用户实际听到的 assistant 文本；
- `external_system`：已确认的工具结果和会话相关外部变化；
- `runtime`：冻结的日期/时区，以及有语义影响的 interruption/replacement。

主 reasoning、未播放 speak、TTS queue、speech handle、REQUEST_TAIL 机械字段、动作 Schema、Shadow
自己的 progress/status/result 都不进入 Shadow 输入 revision。Shadow result 会作为不可信资料供主
模型表达，但不会反射成 Shadow 的新工作。

## revision 与增量

每个真实输入事实递增 `shadow_input_revision`。wake 只携带布尔信号，runtime 根据 revision 获取
完整 snapshot 或从 `applied_context_revision` 开始的无损 delta。多次 wake 合并到最新 cursor，
中间帧不会丢失。reset 增加 source epoch；epoch 不匹配或 cursor 非法时强制返回完整 snapshot。

尚未结算的当前轮由 Episode 把本地 ASR 原文投影为 `current_dispatch_input`；这是原始用户证据，
不是主 LLM生成的任务摘要。相同 `turn_id + text + refinement` 幂等，后续变化形成新 revision。

## 主动作目录

影子可用时，冻结的主决策 Schema 包含：

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

不可用时不公开此动作。它不产生 `ToolExchange`、receipt 或历史项；started/coalesced/
already_current/unavailable 仅记录诊断。主 LLM不得携带目标文本、意图摘要、修改或用户答复；
调用它也不表示当前必须开口，主语音状态仍需独立选择 `wait`、`speak` 或 `cancel`。

## Shadow 固定工具目录

业务工具：

- `files_search`：仅在 `allowed_roots` 内搜索；
- `files_read_text`：仅在允许根目录内读取文本；
- `web_search`：可选外部只读搜索。

所有业务能力均声明 `READ_ONLY`，路径逃逸与未注册工具由 executor 拒绝。工具临时不可用时返回
结构化 `unavailable/error`，不动态删除 Schema，从而维持 cache prefix。

本地控制工具：

- `get_latest_context({since_revision})`；
- `publish_main_event({kind:"progress|observation",text})`：只报告事实、不确定性和实际进度，
  不得指示主助手说什么、询问什么或选择语音状态；
- `request_user_input({kind:"clarification|confirmation",question,options,operation_summary})`。

clarification 发布后当前 activation 回到 idle。用户回答正常进入主会话，主 LLM只再次 wake；
Shadow 从最新上下文自行理解。没有 question ID、reply action、paused task 或长期 waiter。
confirmation/operation_summary 只为未来外部写能力保留；首版没有可执行的副作用工具。

## 不可信内容边界

文件、网页、工具错误和 Shadow 文本都是数据，不能改变系统指令、人格、动作权限、语音协议或
工具 effect。Shadow 事件可能晚于最新用户输入、当前语音队列或已经传达的内容；主模型必须主动
判断其相关性、时效性、可靠性和是否值得播报。主 ledger 在投影 Shadow/工具内容时显式标为不可信。
结构化本地控制元数据（event kind、wake policy、revision）由 runtime 产生，但其中的自然语言
依旧不能执行指令。

## 缓存布局

Shadow instructions、安全协议、工具顺序和 Schema 在 session 内冻结。初始 snapshot 后严格追加
completed model output、function_call_output、conversation update 与 intent reassessment frame。
Observer 复用相同 committed prefix，只在尾部增加固定状态提示；Observer 输出不写回 prefix。

达到上下文预算 80% 后，稳定摘要与最新权威 snapshot 组成新 cache epoch。压缩保证语义完整，
而不承诺永久逐字保留重复旧 reasoning 或普通工具细节。
