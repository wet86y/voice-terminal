# LLM JSON Schema 迁移与回滚 Runbook

本文是当前 LLM transport、输出校验、恢复和回滚的唯一操作说明。历史 provider 调查不再单独维护；当前未关闭的问题集中在[当前已知问题](known-issues.md#开放问题)。

## 当前生产配置

默认配置位于 `config/default.yaml`：

```yaml
llm:
  provider: deepseek
  base_url: https://api.deepseek.com
  model: deepseek-flash
  transport: responses
```

当前生产请求使用 Responses + strict `json_schema`。兼容 schema 名称 `voice_decision_v06` 只用于本地决策 envelope 和冻结测试，不表示版本号。

可选的 OpenCode Go 调试 profile：

```powershell
.\scripts\start.ps1 -LlmProvider opencode_go
```

需要 `OPENCODE_GO_API_KEY`。调试 profile 的 upstream session 绑定当前 `conversation_id`；`session.reset` 必须生成新的 upstream session identity。provider 元数据不得进入公共 WebSocket 事件。

`VOICE_LLM_PROVIDER` 可以覆盖进程级 provider。切换必须显式完成，不能让同一个用户轮次自动同时发送到两个 provider。

## 思考模式

本节描述当前源码的双模式契约；2026-10-07 已完成回归和独立构建，正式目录更新待与其他修订合并处理，见[源码修订与验收记录](desktop-acceptance.md)。

| 模式 | 基础请求 | 额外行为 |
| --- | --- | --- |
| `fast`（快速，默认） | `reasoning.effort=none`（关闭思考） | 不追加思考请求 |
| `deep`（深度） | `reasoning.effort=low`（低强度思考） | 基础请求直接使用低强度思考 |

均衡模式及 `request_thinking`（请求追加思考）已移至 Git（版本控制）中的均衡模式历史。当前配置、会话命令与主代理仅支持快速和深度；历史本机配置若显式设置 `llm.reasoning_mode: balanced`（均衡），应改为 `fast`（快速）或 `deep`（深度）。历史 `thinking_action_enabled`（追加思考开关）不再读取。

前端初值、缺省配置与运行快照均使用快速。模式在代理轮次创建时确定：尚未创建轮次时切换立即影响新轮次，已有轮次时暂存到下一轮次，不取消当前请求。关键词唤醒确认请求继续使用 `reasoning.effort=none`（关闭思考）。主代理目录只保留现有 `request_shadow`（唤醒影子代理）能力，不再注入或执行均衡的追加思考动作；注册已归档动作会被拒绝。

`session.reasoning_mode`（设置运行模式命令）的 `mode`（运行模式）若为均衡或其他非法值，返回 `INVALID_REASONING_MODE`（运行模式无效），保持现有选择和连接。客户端收到旧快照中的均衡值时不会覆盖本地快速或深度选择。

## 输出校验与恢复

每次 provider 响应都必须经过：

1. 流式边界和 terminal event 校验；
2. JSON envelope 解析；
3. `speak` 状态、情绪和 action 参数校验；
4. 本地 action registry 和权限校验；
5. 只有完整合法结果才允许进入 Agent/TTS。

运行时保留一次有界协议恢复，用于空输出、非法 envelope/action 或因 `max_output_tokens` 导致的 `response.incomplete`。恢复请求必须保留原始 request kind（请求类型：普通事件、`SHADOW_INFORMATION`（影子信息事件）、`final_silence`（最终静默检查）、`kws_wake`（唤醒确认）），不能把系统检查改写成普通用户输入重处理；影子信息恢复只重用当前逻辑请求已认领的本地快照。

以下情况不消耗协议修复预算，也不请求模型“自我修复”：

- `response.failed`；
- `response.incomplete` / content filter；
- provider HTTP/网络错误；
- 缺少 terminal event；
- 明确的 transport failure。

流式响应在允许的空白、BOM 或单层 JSON code fence 后出现 10 个非空白字符，且正文仍未以 `{` 开始时，可以提前关闭流并进入同一条有界恢复路径。不得把普通 prose 当作语音内容提交，也不得把失败正文写入 assistant 历史。

## Schema 兼容规则

- provider schema 使用 strict `json_schema`；本地 parser/validator 始终是最终权威。
- 每个 object property 必须满足 provider 的 `required` 约束。
- 不使用 provider native tools；主 Agent 的业务动作经本地 `actions` 和受控的上下文来源块进入下一请求。
- `wait`、`cancel` 等异构 speech 状态使用 provider 可接受的中性字段，provider adapter 返回后再由本地 validator 解释；`cancel` 的替换文本与 `revision` 的新尾巴同样只由本地 validator 赋予语义，provider schema 只保证字段存在。
- action catalog、schema 和 action 可见性在一个 episode 内冻结。

## 回滚

回滚只通过显式配置切换：

```yaml
llm:
  transport: chat_completions
```

`docs/llm-protocol-baseline.json` 是 2026-09-10 保存的历史回滚基线，使用 Chat Completions + `json_object`，不代表当前默认运行路径。回滚前必须确认该文件对应的测试和 provider 能力仍可用；不能在一次用户轮次中自动双发或静默切换。

回滚后仍必须保留本地 envelope 校验、action registry、错误处理和安全边界。

## 发布门槛

在宣称当前 transport 稳定前，至少完成：

- 300 次真实或等价的 Responses 决策，跨至少 3 个使用日；
- 本地 envelope 有效率 ≥ 99.5%；
- 最近 100 次无 schema/local disagreement；
- 无空白 completed response；
- 无超过一次的协议恢复链；
- `fast`（快速）、`deep`（深度）和标注 action case（动作用例）全部符合预期；
- p95 首段安全语音文本延迟不劣化超过 200ms；
- 至少 50 条代表性决策通过人工语义复核。

同时执行：

```powershell
python -m pytest -q
npm test
npm run typecheck
npm run build
```

真实 provider 验收必须从 `SessionRuntime.start_listening()` 的生产装配进入；可以用 fake ASR/TTS 控制输入输出边界，但不能只调用 runtime 私有方法冒充端到端结果。

## 排障记录要求

LLM context audit 默认写入 `logs/voice-server-llm-context.jsonl`，通过 `conversation_id + request_id + turn_id + wire_sha256` 关联请求和终态。日志可以包含真实会话文本，不能提交到仓库；API key 和 Authorization header 永远不得写入。

非法 JSON、恢复耗尽、缺少 terminal event 或 provider capability 变化，统一先更新[当前已知问题](known-issues.md)，再决定是否修改本 runbook。不要重新建立一份按日期命名的长期调查文档。
