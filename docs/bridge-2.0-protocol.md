# Bridge 2.0 可替换信息协议

`protocol_version`（协议版本）固定为 `"2.0"`。运行时不解析 1.0，不提供旧工具别名，不双写新旧事件。

## 设计背景与决策

长任务需要让 DSH（语音终端委托的任务代理）在执行期间间歇反馈，同时继续当前工作。旧协议把一次回传与 DSH 回合结束绑定，无法表达“反馈后继续”。Bridge 2.0 将回传分成不结束回合的界面进度、触发一次 Voice 主模型推理的信息，以及唯一终态。

信息回传会引起一次 LLM（大语言模型）推理请求，但不要求直接播报。Voice 主模型读取当时最新的一条信息和当前对话上下文，自行决定表达、静默或以其他方式响应。该信息是本地可替换状态：在请求构造时被认领，供该逻辑请求消费后即从共享槽移除；请求结束后丢弃，不保存为会话历史或后续请求的长期记忆。这样避免多条中间更新积压、重放或破坏语音对话连贯性。

一项激活可以发布零到多条进度和信息，但只允许接受一个终态。终态必须自包含，包含用户继续处理所需的事实；在途模型请求不被后续事件抢占，终态等待后续调度边界处理。

## 资格边界

Bridge（桥接层）控制工具只允许当前 Voice Session（语音会话）绑定的 DSH 根 Agent（根代理）调用。
`voice_session_id`（语音会话标识）、`dsh_session_id`（DSH 会话标识）、`epoch`（会话代次）、
`context_revision`（上下文修订号）和 `activation_id`（激活标识）必须同时匹配。子代理、普通 DSH 会话、
过期激活与旧修订全部拒绝。

## `shadow.event`（影子事件端点）

信封只允许以下字段：

- `protocol_version`（协议版本）、`request_id`（请求幂等标识）、`event_id`（事件幂等标识）；
- `voice_session_id`（语音会话标识）、`dsh_session_id`（DSH 会话标识）、`epoch`（会话代次）；
- `context_revision`（上下文修订号）、`activation_id`（激活标识）、`event`（判别联合事件）。

`event`（事件）必须精确匹配一种结构：

| `event.type`（事件类型） | 正文字段 | 终态 | 投递位置 |
|---|---|---:|---|
| `progress`（界面进度） | `text`（进度文本，1–512 字符） | 否 | Product UI（产品界面） |
| `information`（Voice 信息） | `text`（信息文本，1–2000 字符） | 否 | 易失单槽 |
| `input_required`（需要用户输入） | `question`（问题，1–1000）、`options`（选项，最多 8 项，每项 1–200） | 是 | HISTORY（历史上下文） |
| `result`（最终结果） | `text`（自包含结果，1–8000 字符） | 是 | HISTORY |
| `no_action`（无需动作） | 可选 `reason`（内部原因，1–500 字符） | 是 | 不写语义历史 |
| `failed`（基础设施失败） | `message`（失败说明）、`code`（失败代码）、`infrastructure`（是否基础设施故障） | 是 | HISTORY |

所有文本先去除首尾空白，再校验长度。空文本、超长文本、未知字段、不适用于当前 `event.type`
的字段一律返回 `conflict`（协议冲突），不截断、不宽容解析。

## 信息槽与推理边界

`latest_shadow_information`（最新影子信息）是进程内单槽，保存 `version`（本地版本）、`text`（信息文本）、
`source`（来源，`agent` 或 `observer`）、`activation_id`（激活标识）和 `context_revision`（上下文修订号）。
根 Agent 与 status observer（状态观察器）没有来源优先级，后到者覆盖先到者。

每次 `information` 都排入 `SHADOW_INFORMATION`（影子信息事件）；调度安全点前的多次到达会合并成一次推理。
请求构建时快照最新版本，放入 `REQUEST_TAIL.shadow.information`（请求尾部的影子信息）；上下文冻结并完成审计后，
仅当共享槽仍是同一 `version` 时清除。新版本在下一次推理中消费。已认领快照仅随同一 logical request（逻辑请求）的
transport retry（传输重试）或 protocol recovery（协议恢复）复用，逻辑请求结束后立即丢弃。

`information` 不进入 HISTORY、continuity document（会话衔接文档）、Shadow 权威输入或 durable mailbox（持久邮箱）。
Voice 主模型可以输出 `wait`（静默），且旧信息不会再次触发推理。

`progress`（界面进度）只更新 Product UI（产品界面），不会唤醒 Voice 推理。根 Agent 与 status observer（状态观察器）发布的信息按到达顺序共享同一槽，来源没有优先级；观察器简报同时更新产品活动视图并触发 Voice 推理。一个逻辑请求认领信息后，后续到达的新版本保留到下一次推理；传输重试和协议恢复沿用该逻辑请求的本地快照，不重新认领共享槽。终态清除该激活尚未认领的信息，已经认领的信息不被反向撤销。

DSH（代理运行时）工具面包含 `publish_progress`（发布界面进度）、`publish_voice_information`（发布 Voice 信息）、`request_user_input`（请求用户输入）、`return_shadow_result`（返回最终结果）、`finish_shadow_no_action`（无动作结束）和 `request_early_silence`（请求提前进入静默）六个控制工具，以及上下文读取工具。移除 `publish_main_event`（旧主线事件入口）、通用 `payload`（附加数据）和旧工具别名。非终态调用不会执行 `exec.concludeTurn()`（结束 DSH 回合）；终态仅在 Voice 接受 `accepted`（已接受）或 `duplicate`（重复确认）后结束该回合。

## 提前静默请求

`request_early_silence`（请求提前进入静默）无参数，仅当前用户明确要求或本会话已启用的影子自定义指令明确规定当前场景时调用。
它通过 `/api/internal/dsh-bridge/input.early-silence`（提前静默端点）发送
`protocol_version`（协议版本）、`request_id`（请求幂等标识）、`voice_session_id`（语音会话标识）、
`dsh_session_id`（DSH 会话标识）、`epoch`（会话代次）、`context_revision`（上下文修订号）和
`activation_id`（当前激活标识）。插件限制根代理调用，服务端校验当前活动绑定并合并重复请求。

该请求不属于 `shadow.event`（影子事件），不结束激活，不伪造用户输入或已经过的静默时长。
Voice（语音服务）等待输入及播报安静后，将当前静默窗口提前标记为最终检查待执行，沿用
`final_silence`（静默前最终检查）及其播报结算与 KWS（关键词唤醒）交接。请求保留到交接完成；
停止、重置或输入所有权代次变化时清除，不持久化到重启后的会话。

## 原子性与恢复

每个 Voice 会话使用 activation event lock（激活事件锁）串行化幂等校验、绑定匹配、事件投递、唯一终态登记、
Session mapping（会话映射）保存与激活清理。同一 `event_id`（事件标识）和同一正文重放返回 `duplicate`，
同标识不同正文返回 `conflict`。首个终态成功后，其他终态和迟到非终态返回 `stale`（已过期）。

映射文档的 `schema_version`（结构版本）为 2，终态回执持久保存 `last_terminal_activation_id`（最后终态激活标识）、
`last_terminal_event_id`（最后终态事件标识）和 `last_terminal_event_digest`（最后终态正文摘要）。
正式安装前必须停止 Voice Server（语音服务端）；安装脚本会检查配置的监听端口，发现仍在监听就拒绝部署。
安装脚本只会归档非版本 2 的旧映射并创建空版本 2 映射；`-ValidateOnly`（仅校验模式）不要求停服也不迁移数据；
运行时没有版本 1 迁移分支。

`publish_voice_event`（投递语音事件）使用独立 `DurableVoiceEvent`（持久语音事件）和 `DurableVoiceMailbox`（持久语音邮箱），
不借用 Shadow information（影子信息），也不参与全局最新覆盖。

## 影响范围

本协议修订影响 Bridge（桥接层）工具与事件、DSH 会话和映射、Voice 调度与请求上下文、状态观察器、Product UI（产品界面）事件呈现、提示词、默认配置、协议文档和 DSH 构建校验。DSH 会话映射结构升级到 `schema_version`（结构版本）2；部署时必须停止 Voice Server（语音服务端），安装脚本会归档旧映射并创建空的版本 2 映射，运行时不恢复版本 1 会话。

麦克风、ASR（自动语音识别）、TTS（语音合成）、播放器、语音事务、用户打断规则及定时任务存储格式不属于本协议的变更范围。定时任务 `publish_voice_event`（投递语音事件）仍使用独立持久邮箱。

## 本次实施与验证记录

2026-10-02 增加 `request_early_silence`（请求提前进入静默）。完整 Python（后端）回归为
**571 passed（通过）、17 skipped（跳过）**，Bridge（桥接插件）测试为 **22 passed（通过）**，
TypeScript（类型检查）、DSH（影子代理宿主）重建和 `-ValidateOnly`（产物一致性校验）通过。
Product UI（产品界面）测试、类型检查与生产构建通过。
新增自动化测试覆盖等待安静、窗口被新输入替换后继续请求、最终检查结算后交还 KWS（关键词唤醒）以及
当前激活校验和重复请求确认。真实麦克风现场验收尚未执行。

2026-09-30 完成本协议实施与回归。全量 Python 自动化测试为 **498 passed（通过）、17 skipped（跳过）**；Bridge 插件 Node（插件运行时）测试为 **17 passed**，TypeScript（类型检查）通过；Product UI（产品界面）测试为 **61 passed**，类型检查通过。DSH Bundle（功能包）已重新构建，`setup-dsh.ps1 -ValidateOnly`（仅校验模式）通过，检查到 `protocol_version`（协议版本）2.0 和映射结构版本 2。`git diff --check`（提交差异空白检查）通过。

Luna 子代理另运行两组定向 Python 测试，分别为 **112 passed / 2 skipped** 和 **85 passed / 15 skipped**；两组存在重叠，不应相加。15 项跳过测试是 Bridge 2.0 删除的被动进度次级邮箱旧用例，其余跳过项属于既有诊断用例。

另用临时 Voice 实例通过 ASGITransport（应用内 HTTP 测试传输）向真实 `shadow.event`（影子事件端点）发送协议请求，并经过 DshShadowAgentSession（DSH 语音会话）、AgentEpisodeRuntime（语音回合运行时）和 SessionLedger（会话账本）构造实际推理消息：

- `progress`（界面进度）返回 `200 accepted`（已接受），没有生成待处理推理事件；
- A、B、C 三条 `information`（Voice 信息）均被接受，合并成一个 `SHADOW_INFORMATION`（影子信息事件），实际请求消息中只有 C；
- 认领 C 后到达的 D 保留给下一次请求消息；
- `result`（最终结果）被接受后，迟到信息返回 `409 stale`（已过期），共享槽为空。

隔离 HTTP（超文本传输协议）试验使用临时目录并自动清理映射，没有改动工作区文件。正式 Voice Server（语音服务端）未运行，因此本轮没有通过正式 Controller（控制器）装配、没有派发模型 Provider（模型服务提供方）请求，也没有调用外部模型。该试验验证接口、会话校验、事件投递、信息认领和真实请求消息构造；正式服务装配及模型收到请求后的行为仍需在现场验收时验证。
