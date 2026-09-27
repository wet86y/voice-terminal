# DSH 插件代码结构与雷区审计（2026-09-27）

对照 DSH `dsh-v0.1.7-rc.2`（commit `477b4f420553e8a52c2fbccc464d7561b239c443`）的官方插件规范，
审计项目自有插件 `integrations/dsh/voice-terminal-shadow-bridge`（以下简称 Bridge）与
`integrations/dsh/voice-terminal-wake-tasks`（以下简称 Wake Tasks），并修复其中一个会导致
DSH 对话页全局空白的缺陷。本次只改代码与逻辑，不改功能：工具集合、工具描述、提示词、
Voice 协议语义、设置页行为均未变化。

判定依据主要来自仓库内规范文档：`docs/cookbook/adding-a-tool.md`（工具契约）、
`packages/AGENTS.md`（包与插件约定）、`packages/client/AGENTS.md`（Web 客户端约定）、
`docs/architecture.md`（组合与生命周期）。

## 一、事故缺陷（已修复）

### 1.1 症状

影子 agent 会话运行后，DSH 控制台的「对话」页变空，且**所有**会话一起变空；刷新后普通会话
恢复显示，但只要订阅中包含影子会话就再次全空。「轨迹」页始终正常。

### 1.2 根因链

1. **读取端（崩溃点，位于 DSH 官方源码）。** `ui-chat` 在正常渲染路径上读
   `turn/end.data.reason.kind`。`reason` 为 `null` 时抛
   `TypeError: Cannot read properties of null (reading 'kind')`。该异常发生在共享的
   session-controller event feed 订阅里，订阅一旦抛出即失效，于是对话页失去全部事件来源。
2. **写入端（触发点）。** `agent-loop` 的 `step()` 在「本步执行了工具、但没有任何工具结果
   声明终结」时返回 `null`；`turnEnds` 因此保持 `null`，最终以非空断言写成
   `turn/end.reason = null`，绕过 `reason: TurnEndReason` 的非空类型契约。
3. **为什么影子会话必中。** 影子 agent 每个 activation 都以终端工具收尾
   （`finish_shadow_no_action` / `request_user_input` / `return_shadow_result`）。
   终端工具只置了插件内部的 `binding.terminal`，从未调用 DSH 的回合终结入口
   `exec.concludeTurn()`，因此条件 2 必然成立。
4. **为什么普通会话也会被波及。** 崩溃发生在 feed 订阅层而非单会话视图，一条坏记录即可
   影响全局。

### 1.3 数据证据

对 `.runtime/dsh/home/sessions` 下全部 `turn/end` 记录做统计：

| 回合形态 | 记录数 | `reason` |
| --- | --- | --- |
| 有工具调用、未被 abort | 22 | 全部 `null` |
| 有工具调用、被 abort | 4 | 正常（走 `agent.ts:353` 的 abort 赋值） |
| 无工具调用 | 5 | 正常（走 `agent.ts:516`） |

即「回合的最后一步是工具调用」是唯一的相关变量，与「是否影子会话」无直接关系；影子会话
只是**总是**以工具收尾，所以必然命中。

### 1.4 修复

`Bridge` 的 `post()` 在 Voice Server 确认收下事件后调用 `exec.concludeTurn()`：

- 成功判定放宽为 `accepted` 或 `duplicate`（`voice_terminal/server/websocket.py` 对两者都返回
  HTTP 200，都表示事件已被收下；此前只认 `accepted`，会额外多跑一轮 protocol recovery）。
- 只在成功时声明终结；失败路径保持原行为，交给既有 recovery 逻辑。
- 工具名、描述、参数 schema、render、工具集合不变。

### 1.5 验证

1. 真实触发一次影子 activation（`shadow.debug.wake`，`config/local.yaml` 已启用调试入口）。
2. 新会话 `voice-shadow-6c2f992f-…` 写入 `turn/end` → `{"kind":"completed"}`（修复前为 `null`）。
3. DSH 控制台启动报错数 **0**；普通会话与影子会话均正常渲染对话内容。
4. 全库复查：33 条 `turn/end` 中 `reason` 为 `null` 的为 **0**。

### 1.6 残留风险

读取端空值解引用位于 DSH 官方源码内，插件无法覆盖：`ConversationNodeDefinition` 按 `kind`
唯一注册，无法替换内置 definition；`config/dsh/README.md` 又禁止修改 `.runtime/dsh/source`。
因此若将来仍有回合既未调用终端工具、也未以文本收尾，`reason: null` 仍会被写入并触发崩溃。
建议向上游反馈两处：`agent-loop` 不应把「未声明终结」退化成 `reason: null`；客户端读取
`turn/end.reason` 应做空值保护。

## 二、顺手发现并修复的缺陷

| 位置 | 问题 | 处理 |
| --- | --- | --- |
| `scripts/setup-dsh.ps1` 的 `Get-TreeHash` | 用 `Sort-Object FullName` 做文化排序（`.` 在 `-` 之前），与 DSH 运行时 `voice_terminal/dsh/runtime.py::_tree_hash` 的小写码点排序不一致，导致 `bridge_build_inputs_sha256` 永远校验失败，DSH 拒绝启动 | 新增 `Compare-PathOrdinal`（小写后逐码点比较）并用于排序，两侧哈希现已逐键一致 |
| `scripts/setup-dsh.ps1` 校验分支 | 读 `cordis.patch.yml` / `shadow preset` 清单时未指定编码，PowerShell 5.1 按 ANSI 读取会把中文注释与下一行 YAML 键拼在一起，既误报漂移、也曾把损坏的补丁写进产物 | 显式 `-Encoding UTF8` |
| `integrations/dsh/voice-terminal-wake-tasks/src/index.ts` | `attached` 用 `Map<object, …>` 以 agent 对象做强引用键；agent 未走 `agent/disposed` 时条目永不释放 | 改为 `WeakMap`，拆卸闭包单独记录释放顺序（注册/卸载时机不变） |
| `integrations/dsh/voice-terminal-wake-tasks/package.json` | `files` 未包含构建已产出的 `lib/index.js.map`、`lib/plugin.js.map` | 补全发布清单 |

## 三、审计清单：符合规范的部分

- **入口形态正确。** 两个插件都提供 `name` / `inject` / `Config` / `apply`（`plugin.ts` 转出），
  包入口指向 `lib/plugin.js`，无默认导出与命名导出混用。
- **注册皆走 effects。** HTTP 路由、工具注册、消息来源标记都通过
  `ctx.effect` / `ctx.on` / `ctx.tools.register` 建立，随 fiber 释放。
- **工具定义使用官方 `defineTool`。** 参数与输出 schema 经 `ParameterSchemaSpec` /
  `ValueSchemaSpec` 编译，注册借用只读定义，未在注册后改写 schema 或回调。
- **模型可见 ⟺ 已落盘。** `user/message` 的 `source.kind` 固定为 `` `plugin:${name}` ``，
  客户端 `voice-terminal-context.ts` 只呈现来源属于本插件的消息。
- **客户端席位用法正确。** `ctx.slots.inject` + `ctx.slots.register`、
  `ctx.configForms.whileServed(...)`、`ctx.uiConversation.events.register(...)` 均按
  `packages/client/AGENTS.md` 的契约使用，并在 effect 中注册。

## 四、审计清单：已记录、未修改的问题

以下问题属实但修复会改变行为或越界，按「只关注代码和逻辑问题、别动功能」的要求保留记录。

### 4.1 工具契约（`docs/cookbook/adding-a-tool.md`）

- **未遵守 `exec.signal`。** Bridge 的 `post()`、`get_latest_context`、`read_ambient_context`
  以及 Wake Tasks 的 `voiceApi()` 都 `await fetch` 且不接 `signal`；DSH 仍会在
  `tools/execute` 层中止调用，但已发出的本机 HTTP 请求不会被取消。
- **输出 schema 过宽。** 终端工具统一用 `{ type: 'object', additionalProperties: true }`，
  等于放弃对 Voice Server 返回结构的校验；规范鼓励把 `output.schema` 当作有用的程序化 API。
- **无 `presentCall` / `presentResult`。** Web 端一律落到通用卡片（规范允许，属可选）。
- **自实现 `defineTool` 包装绕过类型推导。** `index.ts` 里的 `defineTool(options: any)` 让
  `args` / `exec` 退化为 `any`，失去 `InferArgs` 的编译期校验。
- **工具名与描述为中文。** 规范未禁止；是否要有 i18n 约定需产品侧确认。
- **Wake Tasks 未声明回合终结。** 其三个工具属普通工具，不受本次缺陷影响，但同样没有
  `concludeTurn()`；若将来需要以工具收尾，需一并补上。

### 4.2 Cordis 与生命周期（`packages/AGENTS.md`）

- **`inject` 全为硬依赖。** Bridge 声明 9 个服务；任一缺失会使插件永久 PENDING，表现为
  `Failed to load plugins` / `pending`。可考虑改 `ctx.get` 可选读取，但会改变启动时序。
- **类型逃逸普遍。** `apply(ctx: any)`、`defineTool(options: any)`、多处 `as unknown as`
  与结构化 `any` 依赖，与仓库 `strict` 及「显式 `any` 需说明理由」的约定不符。
- **单文件过大。** `index.ts` 约 800 行，混合 HTTP 协议、会话绑定、观察者快照、工具注册与
  recovery 逻辑；按包规范应拆分为协议层、绑定层与工具层。

### 4.3 Web 客户端（`packages/client/AGENTS.md`）

- **响应式读取未走框架席位。** `VoiceTerminalSettingsTab.tsx` 在业务组件里直接
  `useSyncExternalStore` + `settings.subscribe`，并把 `workspaces.list` 快照镜像进本地
  `useState`。规范要求这些事实走 `inject` 的 `hooks` 舱位（`use<Name>`），组件不承载订阅。

### 4.4 会话与数据

- **`binding.requests` 无上限。** 每会话每请求一条（含响应快照），长会话持续增长，仅在
  binding 销毁时释放；加界会改重放语义，故保留。
- **健康检查版本号硬编码。** `GET health` 返回 `bundle_version: '0.1.0'`，与包版本可能漂移。
- **`session-mappings.json` 与 DSH 会话无自动对账。** Voice 侧映射记录 `dsh_session_id`，
  DSH 侧会话被删除时映射不会自动标记失效（本次事件中已手工标记 retired）。
- **旧目录残留。** `.runtime/dsh/home/.agent-presets/` 仍保存一份旧版 Shadow preset 副本
  （内容与 `config/dsh/agent-presets/shadow-agent/` 存在 `workflow-ptc` 与
  `workflow-worker-thread` 的差异）。DSH 0.1.7-rc.2 不再读取该目录，仅作回退保留。

## 五、本次删除的历史数据

`.runtime/dsh/home/sessions` 中 4 个影子会话写有 `reason: null`（共 22 条），在客户端读取端
未加固的前提下打开即导致全局空白，故按决定删除：

| 会话 | 记录数 |
| --- | --- |
| `voice-shadow-139b6b4e-0e2b-4f81-ba7c-cd6eb003e0c3` | 11 |
| `voice-shadow-fd0cabeb-c09a-4ae5-9604-66ff727616be` | 9 |
| `voice-shadow-7672cbb3-7aea-482e-b52d-b4920c022527` | 2 |
| `voice-shadow-fc1e5797-2220-4fcd-9f85-b93445d8da15` | 0（同批处理，无 `null` 记录） |

删除范围：会话目录与 `session_projcache` 条目、`workspace.json` 中的成员与归档引用、
`session-mappings.json` 中对应映射的 `retired_at` 标记。全部内容已备份到
`logs/incident-20260926-null-turn-end/<时间戳>/`（含 `workspace.json` 与
`session-mappings.json` 快照），可整体还原。普通会话与其他影子会话未受影响。

## 六、复核命令

以下脚本都随本文件一起提交，位于 `scripts/dsh-diagnose/`（用法与前提见该目录 README）。

```powershell
# 安装与哈希一致性（两侧配方必须一致，否则 DSH 拒绝启动）
scripts\setup-dsh.ps1 -ValidateOnly
node scripts\dsh-diagnose\compare-build-state-hashes.mjs

# 全库是否还有 reason: null / 缺失
node scripts\dsh-diagnose\find-null-kind-rows.mjs

# 单个会话到底写了什么
node scripts\dsh-diagnose\inspect-session.mjs ".runtime\dsh\home\sessions\<项目目录>\<会话目录>"

# 插件自测
node --test integrations\dsh\voice-terminal-shadow-bridge\test\*.test.mjs

# 真实触发一次影子 activation，再检查新写入的 turn/end
python scripts\dsh-diagnose\trigger-shadow-wake.py 240
node scripts\dsh-diagnose\find-null-kind-rows.mjs

# DSH 控制台启动报错与渲染（需先打开控制台页面）
node scripts\dsh-diagnose\verify-console-state.mjs
node scripts\dsh-diagnose\capture-boot-errors.mjs
```
