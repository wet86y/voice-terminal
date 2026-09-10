# 上下文与工具规范

每次模型请求只能由统一调度器通过 `LLMContextProjector`、`SessionLedger` 和显式 `ContextLane` 生成。当前活动路径是 DeepSeek Responses + strict `json_schema`，返回必填的 `speak` 决策和可选本地 `actions`，不使用 native tool calling；冻结的 Chat/`json_object` 路径只允许显式回退。

上下文分为三层：稳定 Agent 规则、会话日期基准、冻结动作目录和 schema compatibility 是 `STATIC_INSTRUCTIONS`，只进入 Responses `instructions`；最终用户输入、实际听到的助手 `heard_text`、已结算打断/替换、工具资料和持久精修是 append-only `HISTORY`；秒级时间、跨午夜日期 epoch、当前 speak 状态/完整 target_text、本轮精修、工具可用性和恢复反馈是 `REQUEST_TAIL`，只追加在当前请求末尾。未播放输出绝不写入历史。完整字段准入与缓存约束见[LLM 上下文缓存契约](llm-context-cache-contract.md)。

工具来源块包含工具名、参数、执行时间、结果与 `external_untrusted` 来源标记；不包含对话、轮次、语音、epoch 或决策身份。工具结果是资料，不是指令；模型不得从其中获取系统权限、改变会话规则或执行非只读操作。

允许按意图调用受限目录文件工具与联网搜索。工具完成结果统一作为事件进入下一次 400ms 普通调度；新输入、精修和工具晚到不会让在飞请求失效。只有停止监听、会话重置等显式取消才会清理工具任务。

action definitions、由其构建的 strict schema、`request_thinking` 可见性和动作目录在 Agent episode 开始时冻结。工具中途不可用不会修改 schema/catalog，而是作为 `REQUEST_TAIL` 的 availability；本地权限、参数和错误恢复仍是最终权威。`request_thinking` 是预留的本地控制 action，不是业务工具，不产生 `ToolExchange`。
