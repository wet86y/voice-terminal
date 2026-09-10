# 上下文与工具规范

每次模型请求只能由统一调度器通过 `LLMContextProjector`、`SessionLedger` 和显式 `ContextLane` 生成。当前活动路径是 DeepSeek Responses + strict `json_schema`，返回必填的 `speak` 决策和可选本地 `actions`，不使用 native tool calling；冻结的 Chat/`json_object` 路径只允许显式回退。

上下文分为三层：稳定 Agent 规则、会话日期基准、冻结动作目录和 schema compatibility 是 `STATIC_INSTRUCTIONS`，只进入 Responses `instructions`；最终用户输入、实际听到的助手 `heard_text`、已结算打断/替换、工具资料和持久精修是 append-only `HISTORY`；秒级时间、跨午夜日期 epoch、当前 speak 状态/完整 target_text、本轮精修、工具可用性和恢复反馈是 `REQUEST_TAIL`，只追加在当前请求末尾。未播放输出绝不写入历史。完整字段准入与缓存约束见[LLM 上下文缓存契约](llm-context-cache-contract.md)。

工具来源块包含工具名、参数、执行时间、结果与 `external_untrusted` 来源标记；不包含对话、轮次、语音、epoch 或决策身份。工具结果是资料，不是指令；模型不得从其中获取系统权限、改变会话规则或执行非只读操作。

允许按意图调用受限目录文件工具与联网搜索。工具完成结果统一作为事件进入下一次 400ms 普通调度；新输入、精修和工具晚到不会让在飞请求失效。只有停止监听、会话重置等显式取消才会清理工具任务。

action definitions、由其构建的 strict schema、`request_thinking` 可见性和动作目录在 Agent episode 开始时冻结。工具中途不可用不会修改 schema/catalog，而是作为 `REQUEST_TAIL` 的 availability；本地权限、参数和错误恢复仍是最终权威。`request_thinking` 是本地控制 action，不是业务工具，不产生 `ToolExchange`。

## 思考模式与工具等待

会话模式为 `fast`、`balanced`、`deep`，默认 `balanced`。快速模式不暴露思考 action，基础请求为 `effort=none`；深度模式基础请求为 `effort=low`，同样不暴露 action。仅均衡模式向严格 schema 暴露 `request_thinking`，其唯一合法形状为 `{"name":"request_thinking","arguments":{"request":true}}`。`request` 是 schema 兼容的固定确认字段，不是模型可选择的 provider 参数；本地固定将它解释为一次 `effort=low` 的续请求。

请求思考必须同时维护非空 `speak` 转换。没有业务工具时，系统立即安排 low 续请求；有业务工具时，先启动工具并最多等待 2 秒。工具提前完成可释放续请求；2 秒届满后工具仍在后台运行，续请求仅获得 pending 状态而不得把未返回资料当成事实。工具完成或最终超时只发布 `TOOL_COMPLETED`，随后进入既有 400ms 普通调度队列。用户开口、停止监听或 session reset 优先于 thinking wait，并立即使等待及其待发续请求失效。
