# Voice Terminal

Voice Terminal 是 Windows 本机实时语音会话终端。浏览器负责产品界面和控制，Voice Server 负责音频采集、KWS、VAD、ASR、主 Agent、Shadow、TTS、播放和运行诊断。

当前代码包版本为 `0.7.0`。默认 LLM 路径是 DeepSeek Responses + strict `json_schema`；`voice_decision_v06` 只是冻结的协议兼容标识，不是发布版本号。

> 安全边界：默认服务只绑定 `127.0.0.1`。当前管理 API、WebSocket 和模型探测接口不适合直接暴露到局域网或公网；需要远程部署时，先完成 [当前已知问题](docs/known-issues.md) 中的认证和 SSRF 防护工作。

## 运行结构

```text
用户 → ASR → 主 Agent/LLM → Speech transaction → TTS / Player
                         └→ request_shadow({"request": true})
                                      │
                                      ▼
                         单一 ShadowRuntime → READ_ONLY tools
                                      │
                                      ▼
                              主 LLM 400ms mailbox
```

主语音链只有一个调度循环和一个语音事务 owner。未播放的 assistant 草稿不会写入历史；只有用户输入、实际传达的 assistant 文本、有效 refinement 和有语义影响的打断会进入会话账本。Shadow 是同一 session 的只读认知旁路，不控制语音、TTS、播放器或主调度器。

## 快速开始

环境要求：Windows、Python 3.12、Node.js 20 或更高版本。

```powershell
.\scripts\setup.ps1 -Python D:\DevTools\Python312\python.exe -Analysis
.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe
```

启动前将真实凭据放在 `.env.local` 或进程环境中，不要提交到 Git：

```text
DEEPSEEK_API_KEY=
MINIMAX_API_KEY=
```

默认入口：

- Product UI：`http://127.0.0.1:5173/`
- Voice Server：`http://127.0.0.1:8765/`
- WebSocket：`ws://127.0.0.1:8765/ws`
- 健康检查：`http://127.0.0.1:8765/health`
- 后端调试：`.\scripts\start.ps1 -Python D:\DevTools\Python312\python.exe -NoWeb`

完整启动需要同时确认 Web 页面、Voice Server、ASR worker 和 analysis worker 可用；只看到 `/health` 返回成功不代表浏览器端已经准备好。

## 产品页面

- `/`：当前真实 Product UI，连接 Product API 和 Voice Server。
- `/?replay=voice-shadow`：开发环境只读回放，用于检查长会话布局、Shadow 时间线和上下文指标；不会向后端提交操作。
- `/ui-demo.html`：legacy 视觉原型，只使用固定模拟数据，不建立 WebSocket、不申请麦克风权限，也不读写真实设置。

Product UI 只有工作台和设置两个一级页面。页面切换应保留组件状态，真实协议和会话排序以服务端事件为准，详见 [WebSocket 协议](docs/websocket-protocol.md)。

## 主要配置

- `config/default.yaml`：默认运行参数、端口、模型、KWS、音频和 Shadow 配置。
- `config/local.yaml`：本机非密钥覆盖，不提交真实凭据。
- `config/persona/`：`instructions.md`、`personality.md`、`user.md`；在新 Agent episode 创建时读取并冻结。
- `config/shadow_agent/`：Shadow 指令和工具策略。
- `config/product-settings.json`：本机产品设置，包含 KWS、静默、Shadow 开关和声纹设置等非密钥内容。
- `config/speaker/`：声纹资料、模板和特征指纹；详见 [声纹增强](docs/speaker-enhancement.md)。

### 运行模式

- `fast`：主 LLM 使用 `reasoning.effort=none`。
- `balanced`：基础请求不思考，必要时通过本地 `request_thinking` 触发一次低思考续请求。
- `deep`：主请求直接使用低思考。
- KWS：`wake.enabled=true` 时进入唤醒词待机；关闭后 `session.start` 直接进入 ASR。
- Shadow：默认开启，但只读、异步，不是主语音链路能够启动的必要条件。

KWS 和声纹是独立能力。KWS 负责输入所有权和唤醒待机，声纹负责 ASR final 后的可选路由增强；任一旁路发生故障，都不能默默改变另一条链路的语义。

## 验证命令

在仓库根目录执行：

```powershell
python -m pytest -q
npm test
npm run typecheck
npm run build
git diff --check
```

需要真实设备或 provider 的验收时，按照 [运行与质量门槛](docs/operations-and-quality.md) 执行；本地 `logs/` 和 `reports/` 只作为诊断产物，不属于项目文档。

## 文档入口

- [架构与生命周期](docs/architecture.md)
- [上下文与工具规范](docs/context-and-tools.md)
- [WebSocket 协议](docs/websocket-protocol.md)
- [LLM 上下文缓存契约](docs/llm-context-cache-contract.md)
- [LLM JSON Schema 迁移与回滚 Runbook](docs/llm-json-schema-migration.md)
- [可选声纹增强](docs/speaker-enhancement.md)
- [运行与质量门槛](docs/operations-and-quality.md)
- [当前已知问题](docs/known-issues.md)

`docs/llm-protocol-baseline.json` 是历史 Chat Completions 回滚基线，供显式兼容验证使用，不代表当前默认 transport。`docs/` 不再保存版本发布审计、产品草案或一次性故障调查；这些内容应在修复后直接进入 Git 历史或测试记录。
