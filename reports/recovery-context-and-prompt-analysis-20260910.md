# 空白恢复风暴：上下文与提示词实验

日期：2026-09-10（Asia/Hong_Kong）

## 结论

生产风暴由 DeepSeek Chat Completions `json_object` 在 thinking disabled 时返回最终空白-only content 触发，本地无上限同态恢复将单次上游异常放大为风暴。失败响应不进入 SessionLedger；恢复请求仅增加一条泛化系统反馈，因此不会污染持久语义历史，但也没有告诉模型上次响应是空白-only，更没有改变协议约束。

完整启动日志内可关联的 202 次请求中有 173 次 `SPEAK_ENVELOPE_INVALID`；这 173 次全部只有空格 delta，没有任何非空白 content。空格数为 3--147。一个代表性连续链在同一 turn、相同 42 条 message 下连续四次产生 48 个空格并失败，第五次仍先产生 48 个空格，随后输出合法 JSON 并成功。前导空格本身不是失败条件，只有终止时仍为空白-only 才是失败。

本次新增 105 次真实 API 独立请求的提示词矩阵没有复现最终空白-only，因此不能证明提示词消除了该上游现象。不过，它复现了另一类 envelope 不合规，并显示明确结构恢复提示比当前泛化恢复提示更可靠。

## 恢复上下文的实际差异

普通请求由生产 `PROMPT`、SessionLedger 投影、当前输入事实、动态调度快照和可选动作目录组成。失败后：

1. 空白响应不会作为 assistant message 写入 SessionLedger；这是正确的，避免污染共享语义历史。
2. 新请求追加一条 `即时恢复反馈` system message。
3. 反馈 code 是通用的 `SPEAK_ENVELOPE_INVALID`，message 是 `incomplete or invalid decision JSON`。
4. 反馈没有 `content_chars`、`non_whitespace_chars`、`finish_reason`、失败类型或恢复次数。
5. instruction 使用“本轮仍必须调用且只调用一次 speak”，但 `speak` 是 JSON 字段而非工具，且这句话没有重申顶层必须同时包含 `speak` 与 `actions`。
6. 每次恢复更换 error/request ID 和时钟值，但其余语义上下文基本相同；恢复失败可以再次创建新的恢复信号，没有 turn 级预算。

## 提示词矩阵

测试使用生产 V0.6 system prompt、受影响会话的真实 ASR-final 用户侧时序、thinking disabled、`response_format=json_object` 和 `max_tokens=512`。生产日志按设计不持久化 `llm.context` 与 `DeliveryReport.heard_text`，因此 assistant 侧持久上下文无法事后逐字恢复；实验是保守重建，不是原请求的字节级重放。每个样本是一条独立请求，没有自动重试，也没有 TTS/PCM。

两轮实验合计结果：

| 变体 | 样本 | 合法 envelope | 非法 | 最终空白-only |
| --- | ---: | ---: | ---: | ---: |
| baseline，无恢复反馈 | 25 | 19 | 6 | 0 |
| current_generic，当前泛化恢复 | 25 | 19 | 6 | 0 |
| blank_specific，明确指出上轮空白-only | 25 | 23 | 2 | 0 |
| open_brace，规定首尾字符、精确双顶层字段与例子 | 15 | 15 | 0 | 0 |
| short_reset，短上下文重新声明协议 | 15 | 15 | 0 | 0 |

补充结构采样中：

- baseline 的两次失败都是合法 JSON，但顶层只有 `speak`，缺失 `actions`。
- current_generic 的三次失败中，两次顶层只有 `speak`，一次 JSON 在末尾附近语法不完整。
- blank_specific 的补充 10 次均合法。

由于各组样本小且没有重现目标空白-only 事件，组间差异只能作为提示词方向证据，不能估算生产空白率或宣称因果修复。

## 推荐的恢复反馈

恢复消息应使用字段语言而非工具调用语言，并明确分类：

> 上一次请求已以 finish_reason=stop 结束，但最终 content 仅包含空白字符，非合法 JSON。该失败内容未加入会话历史。本次是此用户轮次唯一一次自动恢复。最终输出必须恰好包含两个顶层字段 `speak` 和 `actions`；第一个非空白字符必须是 `{`，最后一个非空白字符必须是 `}`。不要输出 JSON 外文本，不要用空白代替 wait。需要等待时也必须输出完整 wait 对象。请重新处理最后一条用户输入。

可附一份与目标状态相符的完整对象例子。不要把失败的空格正文作为 assistant message 回放；只传递终止原因和结构统计。

## 安全边界

提示词改进不能替代恢复预算。建议同一个逻辑 turn 最多一次自动恢复；再次空白或协议失败后熔断并等待新输入。应新增独立的 `LLM_EMPTY_OUTPUT` 分类，并记录 `finish_reason`、content 字符数、非空白字符数、attempt 和恢复预算。短上下文 reset 虽然本次 15/15 合法，但会丢失 assistant/delivery 语义，只适合作为经过摘要的异构降级方案，不能直接替代完整生产上下文。

## 产物

- `reports/deepseek-recovery-prompt-matrix-20260910.json`：75 次五组交错实验。
- `reports/deepseek-recovery-prompt-structure-followup-20260910.json`：30 次结构补充实验。
- `scripts/probe_recovery_prompt_matrix.py`：无自动重试、无 TTS、正文不落盘的实验脚本。
