# V0.7 发布审计

审计日期：2026-09-13（Asia/Hong_Kong）
审计范围：受版本控制的项目文档、发布元数据、默认配置、主语音链、会话级 Shadow Agent、
WebSocket 公共协议与自动化验证入口。

## 发布基线

V0.7 的发布版本统一为 `0.7.0`：Python 包元数据与 `voice_terminal.__version__`、FastAPI
服务元数据、根 npm workspace、Web workspace 和 `package-lock.json` 均使用这一版本。公开
WebSocket 命令和默认端口保持不变，因此本次不是网络协议破坏性升级。

`voice_decision_v06`、`test_v06_contracts.py` 及 `v06-reliability-audit.md` 中的 `v06` 指向
冻结的决策信封兼容基线，而不是当前产品发布号。它们被保留以避免不必要的 provider Schema
变更；其中历史审计文档已明确标注为基线资料。

## 已对齐的运行事实

| 主题 | V0.7 事实基线 | 权威位置 |
| --- | --- | --- |
| 主模型路径 | DeepSeek Responses + strict `json_schema`；Chat Completions 仅作为显式回滚 | `config/default.yaml`、`docs/llm-json-schema-migration.md` |
| 语音与会话 | 一个 400 ms 单飞调度循环；仅实际传达的 assistant 文本进入历史；ASR late refinement 不回写已发送历史 | `docs/v06-reliability-audit.md`、`voice_terminal/agent/episode.py`、`voice_terminal/session/ledger.py` |
| Shadow | 每个 session 一个长期 `ShadowRuntime`；只读工具、最新权威上下文、异步事件 relay；不控制语音 | `docs/architecture.md`、`docs/context-and-tools.md` |
| Thinking | `reasoning_mode` 是 `fast` / `balanced` / `deep` 的主开关；`request_thinking` 是本地控制动作 | `docs/llm-json-schema-migration.md` |
| TTS 播放看板 | `speech.queued/delta/started/progress/completed` 构成只读实时投影；首次实际播放由 `speech.started` 标记，`playback_finished` 立即结束 UI 活动态，`speech.completed` 仍负责最终结算；重连只读实时 Episode 快照 | `docs/websocket-protocol.md`、`voice_terminal/controller/episode_output.py`、`apps/web/src/composables/useVoiceTerminal.ts` |
| 启动 | `scripts/start.ps1` 启动 Analysis、ASR、Web 与 Voice Server；`-NoWeb` 仅用于后端调试 | `docs/operations-and-quality.md` |
| 运行产物 | 根目录 `logs/` 存放日志，`reports/` 存放诊断报告；二者都是本地、已忽略追踪的固定目录 | `.gitignore`、`README.md` |

## 文档审计结论

- README 的前端验证命令已改为在仓库根目录执行，由 workspace 脚本转发到 `apps/web`；旧的
  `cd web` 路径不存在。
- JSON Schema 迁移文档不再把默认启用的 Shadow 写成“非生产诊断设施”，并明确它与主模型
  thinking 路径相互独立。
- 文档中对本地 `logs/`、`reports/` 的引用均视为运行产物，而非应随仓库分发的证据文件。
- Persona 与 Shadow 指令均符合权限边界：前者不覆盖本地协议，后者不接收主模型任务摘要、
  不拥有副作用工具或语音控制权。
- Speak Queue 已收敛为事件驱动的只读观测组件。它不显示或改变 1 秒追加窗口、内部 hold、
  provider session、replacement、delivery、SessionLedger 或 LLM `speak_snapshot` 的状态机；实时
  Episode 快照读取失败时仍返回缓存的 `RuntimeState.agent`，不会中断 WebSocket 订阅。

## 仍然开放的风险

当前没有发现需要阻止 V0.7 文档和版本基线发布的 P0 实现冲突。仍有一项开放 P1：官方
DeepSeek Responses 偶尔以 completed 状态返回无法通过本地信封校验的非空内容。运行时已使用
一次有界协议恢复并拒绝不安全的纯文本降级，但 provider 主因尚未定位。发布质量门槛、复现方法
和回滚条件以 [已知问题记录](known-issues/llm-invalid-json-responses.md) 与
[迁移 Runbook](llm-json-schema-migration.md) 为准；在达到 300 次、跨三日的 99.5% 有效率门槛前，
该问题保持开放。

## 验证要求

在仓库根目录执行：

```powershell
python -m pytest -q
npm test
npm run typecheck
npm run build
```

完整设备验收仍需要真实麦克风、AEC、ASR、TTS 与 provider 凭据；上述自动化验证不替代该项验收。

## 本次审计结果

- `python -m pytest -q`：254 passed、2 skipped；出现两条第三方 SWIG 弃用告警，不影响结果。
- `npm test`：Web Vitest 14/14 通过。
- `npm run typecheck`：通过。
- `npm run build`：通过，Vite 已完成生产构建。

npm 在当前环境中额外提示遗留 `python` 环境配置将在未来主版本不再支持；该提示不来自项目
package 配置，未影响安装、测试、类型检查或构建，作为环境维护项单独保留。
