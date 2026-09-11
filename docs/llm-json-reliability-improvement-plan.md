# LLM JSON 可靠性候选改进计划单

状态：**长期维护中的候选方向清单，不是已批准的修复 backlog**  
适用范围：DeepSeek Responses `json_schema` 决策信封、流式解析、协议恢复、动作 Schema 与相关观测  
关联问题：[`known-issues/llm-invalid-json-responses.md`](known-issues/llm-invalid-json-responses.md)  
关联迁移门槛：[`llm-json-schema-migration.md`](llm-json-schema-migration.md)

## 使用规则与授权门禁

本文吸收 2026-09-11 深度研究报告及随后对当前工作区的逐项核对结果，用于长期保存“可能值得修复或验证的方向”。清单中的条目可能只是诊断假设、实验候选、运行策略选择或架构研究，**不表示问题已经证实，也不表示用户已经授权实施**。

任何人或 Agent 在修改代码、配置、Prompt、Schema、模型参数、重试预算或运行协议前，必须先向用户说明并获得该次变更的明确许可。说明至少包含：

1. 当前本地实现和可复现证据；
2. 本次准备修改的准确文件、配置和行为边界；
3. 为什么选择该方向，以及哪些结论仍只是推断；
4. 对回答语义、工具动作、TTS 首播、取消、请求数、延迟、成本、隐私和 provider 可移植性的影响；
5. 如何隔离变量、验证结果和判断成功；
6. 回滚方式及停止条件；
7. 明确询问用户是否批准执行本次具体修改。

没有明确许可时，只允许进行不改变外部状态的本地检查、已有报告分析、测试阅读和方案编制。不得因为条目标记为 P0/P1、建议采纳或已有证据，就自动修改产品。一次许可只覆盖当次说明的范围；不得顺带实施同表中的其他方向。

本文状态含义：

| 状态 | 含义 |
| --- | --- |
| `RECORDED` | 仅记录方向，尚未批准实验或实现 |
| `EXPERIMENT_REQUIRED` | 只能在获得许可后先做隔离实验，不能直接作为生产修复 |
| `IMPLEMENTED_VERIFY` | 本地已有实现，只记录其边界并持续验证；不代表可自行扩大范围 |
| `DEFERRED` | 当前不应进入主线，需满足前置条件后重新说明和获批 |
| `DO_NOT_ADOPT` | 当前证据下不应采用；若未来重议，视为新的架构/产品决策 |

## 不得破坏的现有产品边界

除非用户另行明确批准架构升级，本计划中的可靠性工作必须保留：

- 400 ms 普通事件合并与单路在飞 LLM；
- 单一 `{"speak":...,"actions":...}` 原子决策信封；
- 合法 `speak` 前缀的两阶段提交和低延迟文本/TTS 路径；
- `SpeechDecisionEnvelopeStream` 作为本地最终权威；
- 本地只读 actions、工具结果回填和会话账本；
- 用户 barge-in、stop、reset 的立即取消边界；
- 当前 speech transaction 的同轮前缀追加、跨轮隔离和 generation 防迟到机制；
- 无效完整正文不得直接进入持久 assistant 历史；
- 普通自然语言不得被无条件包装成 `speak`，以免掩盖丢失的工具动作。

## 已确认基线

当前本地事实用于判断方向，不等同于未来 provider 的永久行为：

- 活动配置使用 DeepSeek Responses、`stream=true`、`text.format.type=json_schema`、`strict=true`，普通请求使用 `reasoning.effort=none`；
- 请求未显式发送 `temperature`，`max_output_tokens=512`；
- Prompt 已统一要求 `state/emotion/text/actions` 全部存在，并用空字符串表示 `wait/cancel`；本地 validator 为兼容历史输出继续保留较宽的接受边界；
- 本地 validator 为兼容历史输出仍允许缺失 `emotion`，Responses 边界允许 `wait/cancel.text=""`；
- provider-facing action Schema 合并异构动作参数并要求中性 filler，本地在执行前去掉无关字段；
- SSE parser 已记录 malformed event、sequence 缺失/重复/倒退/jump，并继续以 authoritative final 对照为正确性边界；
- HTTP、连接和超时错误已类型化，transport retry 根据 `retryable` 属性判断，不再解析错误字符串；
- protocol recovery 额度按整个 `turn_id` 共享，speculative/finalized/refined 没有独立额度；
- 有界 JSON repair 已接入 Responses 主路径，并在 repair 后重新执行完整本地校验；普通正文、混合正文、截断容器和歧义结构仍被拒绝。

现有复放基线：

| 数据 | 结果 | 解读边界 |
| --- | --- | --- |
| 1× 真实节奏复放 | 147/147 completed，120 strict valid，27 invalid | 请求级严格成功率 81.63%，不能等同 finalized turn 最终成功率 |
| 27 个失败的结构 | 22 个完整普通正文、4 个合法 JSON 但信封不合约、1 个容器异常 | 主要问题不是单纯 JSON 截断，也不是安全 repair 能覆盖的类型 |
| 分阶段 | speculative 48/66、finalized 21/28、recovery 25/26、refined 26/27 通过 | 只显示相关性；不能据此直接证明 dynamic tail、Prompt 或 temperature 的因果关系 |
| bounded-repair 复放 | 95 次中 13 个严格失败，安全 repair 命中 0 | 现有 repair 处理低频低歧义错误，不会解决占主导的 prose 脱壳 |

## 候选方向总表

| ID | 候选方向 | 当前状态 | 建议顺序 | 产品逻辑影响 | 实施前门禁 |
| --- | --- | --- | --- | --- | --- |
| JR-01 | 统一 Prompt、provider Schema 与 wire contract | `IMPLEMENTED_VERIFY` | 持续验证 | 不改变业务语义，但会改变模型输出格式分布 | 扩大或收紧 validator 前重新获批 |
| JR-02 | 增加短 canonical JSON 示例 | `IMPLEMENTED_VERIFY` | 持续验证 | 可能影响措辞、wait 倾向和字段顺序 | 改动示例或追加示例前重新获批 |
| JR-03 | `temperature=默认/0.2/0.0` 对照 | `EXPERIMENT_REQUIRED` | 第一批实验 | 会影响措辞、动作选择和随机性 | 模型固定、按 effort 分层并获批 |
| JR-04 | transport/JSON/schema/local/action 五层分类 | `IMPLEMENTED_VERIFY` | 持续验证 | 仅诊断；注意日志隐私和性能 | 扩大持久内容或改变拒绝规则前重新获批 |
| JR-05 | 有界低歧义 JSON repair | `IMPLEMENTED_VERIFY` | 持续验证 | 已扩大少量可接受格式；禁止扩为语义修复 | 扩大任何规则前重新获批 |
| JR-06 | SSE malformed 与 sequence 观测 | `IMPLEMENTED_VERIFY` | 持续验证 | 只观测，不因 gap 自动取消或失败 | 将指标升级为失败条件前重新获批 |
| JR-07 | 类型化 provider/transport errors | `IMPLEMENTED_VERIFY` | 持续验证 | 会改变重试覆盖、延迟和请求数 | 扩大重试类型或次数前重新获批 |
| JR-08 | stage + semantic version 恢复预算 | `EXPERIMENT_REQUIRED` | 第二批 | 会增加部分 LLM 请求并改变失败处理 | turn 总硬上限、取消规则获批 |
| JR-09 | finalized response watchdog | `DEFERRED` | provider 主因缓解后 | 新增自动唤醒，属于产品运行逻辑变化 | 独立方案与明确许可 |
| JR-10 | action Schema subtraction / 简化 | `EXPERIMENT_REQUIRED` | 第二/三批 | 可能改变工具选择和参数准确率 | action correctness 评审获批 |
| JR-11 | provider Schema 删除 `maxItems` A/B | `EXPERIMENT_REQUIRED` | 第二批实验 | 保留本地 8 条上限时不改最终契约 | 不引用 Tool Call 限制作直接结论 |
| JR-12 | `strict:true` 与不发送 `strict` A/B | `EXPERIMENT_REQUIRED` | 第一批实验 | provider 约束行为可能变化 | 只做交错对照，不能盲改生产 |
| JR-13 | 按 reasoning effort 分配输出 token | `EXPERIMENT_REQUIRED` | 数据触发后 | 改变成本和最坏延迟 | 先证明 low effort 存在截断 |
| JR-14 | provider/model capability 固定与漂移监控 | `RECORDED` | 所有 A/B 之前 | 切换模型会改变回答行为，不得顺带迁移 | 先做只读 probe；迁移另行获批 |
| JR-15 | provider native Tool Call shadow | `DEFERRED` | 中长期研究 | 会改变原子协议、工具历史和可移植性 | shadow 不执行工具；架构许可 |
| JR-16 | 普通正文自动包装成 `speak` | `DO_NOT_ADOPT` | 不执行 | 可能掩盖丢失 actions，实质改变业务语义 | 未来重议视为新产品决策 |
| JR-17 | 简单提高全 turn recovery limit | `DO_NOT_ADOPT` | 不执行 | 增加恢复风暴、成本与尾延迟风险 | 只能由有总硬上限的新版策略替代 |
| JR-18 | 改为 NDJSON/多对象协议 | `DO_NOT_ADOPT` | 不执行 | 破坏 speak/actions 原子提交和现有流式事务 | 未来重议视为协议大版本升级 |

## 候选方向明细

### JR-01：统一 Prompt、Schema 与 wire contract

问题已在本地确认：自然语言 Prompt 与 provider-facing Schema 对 `emotion` 和控制状态 `text` 的要求互相矛盾。候选 canonical wire form 为：

```json
{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}
```

建议只统一模型侧说明和 wire contract；本地 validator 是否继续接受历史宽松形态应作为兼容性决定单独说明。此方向能直接消除确定的指令冲突，但不能被描述为已经证明可以解决占主导的 prose 输出。

实施前必须向用户确认：是否只改 Prompt，是否同步生成 Schema/示例，是否收紧本地 validator，以及旧 provider/rollback adapter 的兼容要求。

### JR-02：短 canonical 示例

候选是在静态前缀中加入一条 `speak` 和一条 `wait` 的完整合法示例。示例应短且中性，避免模型复制具体回答或过度选择 `wait`。JSON Schema 不保证 key order；示例只能软引导 `state → emotion → text → actions`，主要可能改善格式遵循和首个安全文本延迟。

必须单独 A/B，不与 temperature 或 Schema 改动同时归因。

### JR-03：temperature 对照

当前请求不发送 temperature。候选对照为 provider 默认值、0.2 和 0.0；不同时调整 `top_p`。测试必须：

- 固定 provider、明确模型 ID、Schema、Prompt、token budget 和输入语料；
- 按 `reasoning.effort=none` 与 thinking 请求分层，避免把 thinking 下不生效的参数计入结论；
- 同一冻结请求重复并随机交错各实验组；
- 同时看严格格式、回答质量、口语自然度、action 正确率、首播延迟和 tokens/successful turn。

不得以“低 temperature 更确定”为理由直接改生产默认值。

### JR-04：五层结果分类

建议观测层次为：

```text
provider transport/terminal
→ JSON syntax
→ provider-facing Schema
→ local decision contract
→ action adapter / tool contract
```

当前已有 terminal error、结构指纹和 `strict_valid/compat_recovered/invalid_envelope`，但 provider Schema 与本地业务 contract 尚未完全独立统计。新增观测不应持久记录模型正文；如果需要精确上下文审计，继续使用已隔离、明确包含会话内容且有轮转限制的专用审计日志。

### JR-05：现有 bounded repair 的维护边界

已实现规则：BOM、单层 JSON fence、尾逗号、字符串控制字符、仅有完整 `speak` 时补空 actions、空 emotion 映射到 calm。全部候选在修复后仍必须通过完整本地 validator。

`missing_actions → []` 更准确地说是低歧义保守修复，而不是绝对无语义损失。任何新增规则，尤其是花括号片段抽取、截断补全、普通正文包装、推断 action 或删除额外字段，都属于新的语义决定，必须重新解释并获批。

### JR-06：SSE 完整性观测

候选指标包括：

- malformed SSE event；
- missing sequence field；
- duplicate / non-increasing sequence；
- sequence jump；
- streamed chars 与 authoritative chars；
- stream/final match。

官方“单调递增”不必然等同序号逐一连续，因此 jump 只能先作为诊断信号，不能自动断言丢包。继续以 authoritative final 对照作为最终边界；不得仅因 sequence jump 取消 TTS。

### JR-07：类型化错误与重试矩阵

候选类型至少区分 HTTP status、rate limit、connect、timeout、收到部分正文后的断流、Responses terminal failure、用户取消和 supersede。retryability 应由类型和“是否已收到正文”共同决定，不再匹配异常字符串。

该方向会改变 P95/P99 延迟和请求数。实施前必须提供覆盖 400/401/403、429、500/502/503/504、连接失败、部分流断开、用户取消、新输入 supersede、content filter 和 `max_output_tokens` 的测试矩阵。

### JR-08：恢复额度的 stage/version 归属

候选不是简单增加次数，而是为 authoritative finalized 保留有限恢复机会。不能只把 key 改为原始 `input_revision`，因为重复 finalized 事件也可能产生新 revision。建议设计时同时考虑：

```text
turn_id + stage + semantic_digest
```

并设置每 turn 总请求硬上限。必须定义 speculative recovery 被取消后是否归还额度、finalized 成功后的清理、refined 的实质语义变化判定，以及用户继续讲话时的取消行为。

### JR-09：finalized watchdog

只有在 provider/Prompt 主因已显著缓解后才重新评估。它会在没有新外部事件时主动唤醒 LLM，属于产品运行逻辑改变。候选触发必须同时满足 finalized、当前权威 turn、用户未讲话、无活动 LLM/TTS/工具、该语义版本没有合法控制决定且未超过全局预算；任一新输入、合法决定、TTS 开始或 session stop 都必须取消 watchdog。

### JR-10/JR-11/JR-12：Schema 与 strict 的能力矩阵

采用交错实验，而不是顺序批量运行：

| 组 | 变量 | 目的 |
| --- | --- | --- |
| A | 当前生产 Schema + `strict:true` | 基线 |
| B | 当前生产 Schema，不发送 `strict` | 验证未文档化字段是否影响兼容实现 |
| C | 最小 Schema，不发送 `strict` | 区分基础 structured output 与生产 Schema 复杂度 |
| D | 当前 Schema + canonical 示例 | 检查生成引导作用 |
| E | 当前 Schema 删除 provider `maxItems`，本地仍限制 8 | 检查关键字兼容性 |
| F | action Schema subtraction | 定位合并参数、neutral filler 或 thinking action 的影响 |

DeepSeek strict Tool Call 对 `maxItems` 的限制不能直接证明 Responses `text.format.json_schema` 也不支持它。只有 A/B 数据可以决定是否移除。Schema 简化必须同时评价工具选择和参数正确性，不能只优化信封通过率。

### JR-13：按 effort 分配 token budget

普通 `effort=none` 暂时保持 512；现有复放没有 `response.incomplete=max_output_tokens`，因此没有扩大普通预算的依据。只有监控确认 low reasoning 的 visible output 被 reasoning token 挤压时，才实验 768/1024，并同时衡量成本与最坏延迟。

### JR-14：provider/model capability 固定

任何 A/B 前先用只读 capability probe 记录 `/models` 可用 ID、请求 model 和响应 model。配置中的别名可能随 provider 演进而漂移；模型迁移会改变回答和工具行为，不能与 Prompt、temperature 或 Schema 实验合并，也不能根据文档名称变化自动修改配置。

### JR-15：native Tool Call shadow

只作为中长期研究。shadow 分支不得执行工具、写 ledger、提交 TTS 或影响用户状态。若未来考虑生产迁移，必须作为新的协议版本评审 speak/actions 原子性、增量前缀、tool result 历史、recovery、provider portability 和回滚，不得归类为本轮 JSON 小修。

## 实施记录

### 2026-09-11：首批低风险方向获批实施

用户明确批准 JR-01、JR-02、JR-04、JR-06、JR-07，并要求继续保持单 JSON envelope、现有 TTS/speak queue/barge-in、普通 512 token、拒绝 prose 自动包装且不迁移 native Tool Call。

本次实现：

- 模型 Prompt 与 provider Schema 对齐，要求 `state/emotion/text/actions` 始终存在，`wait/cancel.text=""`；
- 加入一条短 speak 示例和一条短 wait 示例；
- 新增无第三方依赖的 provider-facing Schema 子集诊断，并区分 transport、JSON syntax、provider Schema、local contract 和 action contract；
- SSE 记录 malformed、sequence missing/duplicate/non-increasing/jump 及首末序号；jump 只观测，不改变 TTS 或完成判定；
- 新增类型化 HTTP、连接和超时错误；仅 429/5xx 或尚未收到正文的瞬时传输错误可进入既有有界 transport retry；
- `max_output_tokens` 仍为 512；没有新增 temperature、恢复额度、watchdog、native tool 或 prose salvage。

验证与回滚：相关协议、SSE、类型错误和运行时回归测试通过；若真实会话出现动作准确率、wait 倾向、首播延迟或重试尾延迟回归，应整体回滚本次 Prompt/示例或错误策略的对应独立变更，而不是放宽本地信封校验。

## 实验与验收规则

每轮只改变一个主要因素；如必须做多因素矩阵，应能独立估计各因素效果。优先使用真实脱敏 replay corpus，同一请求重复若干次，各组随机交错，避免时段性 provider 波动造成误判。

最低指标：

- request-level strict envelope success，按 speculative/finalized/refined/recovery 分层；
- finalized turn 在总预算内得到合法控制决定的比例；
- JSON syntax、provider Schema、local contract 和 action adapter 各层失败率；
- action selection/arguments 的规则或人工正确率；
- request-to-safe-speak、user-end-to-audio、terminal latency 的 P50/P95/P99；
- recovery、transport retry 和总 requests/turn；
- input/output/reasoning/cache tokens 与 tokens/successful turn；
- stream/final mismatch、malformed SSE 和 sequence 异常。

至少 300 次直接 Responses 决策可作为开发阶段的最低回归 gate，但不能单独证明真实失败率已经低于 0.5%，也不能作为“已保证 99.5%”的统计结论。发布判断还需跨时段/跨使用日、更大样本、最近窗口无连续失败链，以及代表性决策的语义和 action 人工检查。

## 每次申请实施时的说明模板

在执行本文任一 `RECORDED`、`EXPERIMENT_REQUIRED` 或 `DEFERRED` 条目前，向用户提交以下简要说明并等待明确许可：

```text
计划单 ID：
本地现状与证据：
本次只准备修改：
明确不修改：
它可能改善什么：
仍未证明什么：
对产品逻辑/延迟/成本/工具/TTS/隐私的影响：
验证样本、指标和成功门槛：
停止/回滚条件：
是否批准按以上范围执行：
```

实施完成后，应回写本文件的状态、日期、实际证据和回滚位置；实验不成立时也要记录否定结果，避免以后因遗忘而重复进行同一片面修复。

## 当前建议执行顺序（不构成授权）

1. 先申请 JR-14 的只读模型能力确认，以及 JR-01/JR-02/JR-03/JR-12 的正交离线实验许可；
2. 数据显示协议遵循改善后，再申请 JR-04/JR-06 的诊断增强；
3. provider/Prompt 主因稳定后，再评审 JR-07/JR-08；
4. 只有仍存在 finalized 无回应时才重新提出 JR-09；
5. JR-10/JR-11/JR-13/JR-15 保持实验或研究性质；
6. JR-16/JR-17/JR-18 保持禁止采用，除非用户明确启动新的产品/协议决策。
