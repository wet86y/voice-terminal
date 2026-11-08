<p align="center">
  <img src="apps/desktop/src-tauri/icons/icon.png" alt="Voice Terminal 图标" width="88" height="88">
</p>

<h1 align="center">Voice Terminal</h1>

<p align="center"><strong>Windows 上的个人语音 Agent —— 说一句，它就动手。</strong></p>

<p align="center">
  <a href="https://github.com/wet86y/voice-terminal/releases/latest"><img alt="最新版本" src="https://img.shields.io/github/v/release/wet86y/voice-terminal?label=%E6%9C%80%E6%96%B0%E7%89%88%E6%9C%AC&color=%2350c878"></a>
  <a href="LICENSE"><img alt="许可证" src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
  <img alt="平台" src="https://img.shields.io/badge/platform-Windows%2010%20%2F%2011%20x64-0078d4?logo=windows&logoColor=white">
  <img alt="状态" src="https://img.shields.io/badge/status-%E5%BC%80%E5%8F%91%E4%B8%AD-orange">
  <img alt="语言" src="https://img.shields.io/badge/speech-ASR%20%E2%86%92%20LLM%20%E2%86%92%20TTS-8b5cf6">
</p>

<p align="center">开发中的个人语音 Agent。语音识别、决策与合成的整条链路跑在你自己电脑上，界面是一个窗口，配置是几次点击。</p>

---

## 快速开始

**1. 下载安装器**（Windows 10 / 11 x64，~115 MB）

```powershell
# 打开下载页选择最新版本
Start-Process "https://github.com/wet86y/voice-terminal/releases/latest"
```

或直接取安装器：

```powershell
Invoke-WebRequest -Uri "https://github.com/wet86y/voice-terminal/releases/latest/download/VoiceTerminalSetup.exe" -OutFile "$env:USERPROFILE\Downloads\VoiceTerminalSetup.exe"
```

**2. 安装并启动** —— 安装器只负责选择工作区、展开程序、创建桌面快捷方式，不做额外配置。

**3. 在图形界面里一键下载组件** —— 打开程序进入组件管理页，点“一键下载”，程序自动补齐语音环境、识别模型与后台执行宿主；全程有统一进度条、可取消、可重试。组件就绪后点“启动程序”即可开始对话。

**4. 说话** —— 可直接按住说话，也可在设置里开启唤醒词，进入随时待命的语音状态。

> 需要模型与运行环境的组件会在首次启动时由程序自行下载与校验；下载和转换过程较长属正常现象，本地语音引擎的转换尤其依赖显卡与内存资源。

---

## 为什么是它

| | |
|---|---|
| **低延迟交互** | 语音合成分块流式返回，边合成边播放，首块音频不必等整段生成；唤醒词与静默窗口让对话连续，不必反复唤醒或重新点按。 |
| **低性能占用** | 模型按需加载，空闲时只保留必要组件；识别、分析与本地语音最多三路并发，资源不足时自动降并发。本地语音跑在显卡上时，CPU 也能承担基础识别与活动检测。 |
| **GUI 一键下载** | 所有组件、模型与运行环境都在图形界面里下载、更新、导入和重置，带进度、可取消、可重试；不需要用户碰命令行、包管理器或虚拟环境。 |
| **配置简单** | 人格、指令、唤醒词、静默、声纹、定时任务都在设置页完成；凭据交给系统凭据管理器保存，配置即时生效，无需重启。 |

---

## 功能介绍

### 本地语音合成（可选）

可选接入本地 TTS（语音合成），离线也能出声。推理框架使用 **[AnifLive-TTS](https://github.com/Hiruynk/AnifLive-TTS)**，由 [Hiruynk](https://github.com/Hiruynk) 开发，为个人消费级设备提供稳定可靠的低延迟、高质量 TTS 模型方案。在此向 Hiruynk 致谢。

- 支持固定预设与配对权重导入：确认模型、参考录音与转写后，后台自动完成下载、转换、验证与音色登记
- 按模型管理情绪与场景参考音色，支持上传、试听、启用与默认参考恢复
- 48 / 96 语义标记分块流式输出，提供显卡高性能、首音等待等选项
- 引擎在本机生成并真实验证，不同显卡与依赖身份的引擎不互相套用
- 也可选择 MiniMax 在线语音作为播放目标，两者可在设置中随时切换

### 静默与唤醒词

- 静默窗口（默认 90 秒）内可直接接话，无需重新喊唤醒词
- 唤醒词可配置，唤醒命中或模型主动发声后麦克风交给识别
- 窗口到期时由最终静默检查交回唤醒待机；主动结束录音或进入唤醒待机会立即失效

### 自定义指令设置与人格设定

- 人格风格与用户资料独立配置，首次激活时读取，设置页保存后立即对后续对话生效
- 自定义指令可约束助手的行为边界与任务处理方式
- 默认大模型路径为 DeepSeek Responses 接口与严格结构化输出

### 声纹识别

- 主人 / 非主人二分，证据不足时按非主人处理
- 拦截模式在身份结算前暂缓输入派发，放行本轮主人及有效连续主人
- 阈值与连续放宽量可在页面编辑后保存，主人打断采用独立开关

### 定时任务

- 支持 1 分钟至 24 小时间隔，可选择持续循环或设定成功触发次数
- 页面与后台执行工具均可创建、修改和查询
- 到期提醒通过语音事件回到主对话链路

### 后台协作

- 后台执行与语音交互构成同一个助手：实际操作、外部查询与定时请求交给后台，语音侧只汇报进展
- 待决权限请求可单次同意、连续同意或拒绝；开启语音确认后，模型会先说明待授权操作再按你的明确答复处理

---

## 开发路线图

1. **长记忆模块** —— 让助手跨会话记住偏好与上下文，而不是每次从零开始。
2. **局域网互联** —— 多设备场景下，由一台设备承担算力消耗，为其他设备提供更低占用的语音交互能力。

---

## 文档

完整文档按架构、说明、测试与交付四类整理，目录说明与分类索引见 **[文档索引](docs/README.md)**。

| 类别 | 入口 |
| --- | --- |
| 架构 | [架构与生命周期](docs/architecture/architecture.md) · [Bridge 2.0 桥接协议](docs/architecture/bridge-2.0-protocol.md) · [WebSocket 协议](docs/architecture/websocket-protocol.md) · [上下文与工具](docs/architecture/context-and-tools.md) · [连续性底座](docs/architecture/continuity-foundation.md) |
| 说明 | [本地目录与覆盖发布](docs/guides/local-layout.md) · [统一正式程序与分发安装器](docs/guides/unified-product-installer.md) · [组件设置](docs/guides/component-settings.md) · [声纹增强](docs/guides/speaker-enhancement.md) · [热词替换](docs/guides/asr-hotwords.md) · [本地 TTS 运行](docs/guides/tts-runtime.md) · [本地模型运行设备](docs/guides/local-model-compute.md) |
| 测试 | [测试、启动与现场验收入口](docs/testing/operations-and-quality.md) · [当前已知问题](docs/testing/known-issues.md) |
| 交付 | [当前交付](docs/releases/current-release.md) · [v0.9.12 发行说明](docs/releases/v0.9.12.md) · [GitHub 发布流程](docs/development/github-release.md) |

**运行环境要求**：Windows 10 / 11 x64；系统 WebView2（网页运行引擎）；本地语音合成需兼容的 NVIDIA（英伟达）显卡，并预留足够的显存、内存与磁盘空间。

**安全边界**：默认服务只绑定 `127.0.0.1`。当前管理 API、WebSocket 和模型探测接口不适合直接暴露到局域网或公网；需要远程部署时，先完成[当前已知问题](docs/testing/known-issues.md)中的认证和 SSRF 防护工作。

---

## 开发者速查

以下内容面向在源码上工作的开发者。日常开发复用现有环境，常规更新采用固定位置覆盖。

### 本地开发

本地开发和用户使用同一套正式程序。本机正式入口是 `.local/VoiceTerminal.exe`；对外分发 `.local/releases/installer/VoiceTerminalSetup.exe`，只负责选择工作区、展开或重装同一正式程序包、创建桌面快捷方式与启动交接。组件下载、导入、配置及维护全部由正式程序承担。详见[统一正式程序与分发安装器](docs/guides/unified-product-installer.md)及[当前交付](docs/releases/current-release.md)。

源码开发复用项目 `.local/portable.json` 指定的开发业务环境：Python 位于 `.local/runtimes/python/`，Node.js 从 `.local/runtimes/dsh/runtime.json` 解析。开发依赖由 npm 安装在根目录 `node_modules/`；原生构建工具保存在 `.local/tools/`，构建输出统一进入 `.local/build/`。

```powershell
.\scripts\setup.ps1 -SkipInstall -SkipDsh
.\scripts\install-frontend.ps1
npm run desktop:start
```

组件与构建依赖默认采用国内镜像优先、失败回退原站，版本及内容校验保持不变。来源映射、离线安装及验证边界见[国内镜像与原站回退](docs/guides/download-sources.md)。

```powershell
npm run dev          # 真实业务桌面热更新；别名 npm run desktop:dev
npm run dev:ui       # 桌面样例热更新；别名 npm run desktop:ui
npm run build        # 统一正式界面、原生宿主及根入口；别名 npm run desktop:build
npm run desktop:prepare  # 按内容复用或准备正式程序包，不安装
npm run desktop:publish  # 准备程序包并更新本机正式入口
npm run desktop:dist    # 准备程序包并构建独立 EXE（安装器）
npm run desktop:deliver # 更新本机并构建安装器，共用同一程序包
npm run github:release:local # 同包更新本机并发布 GitHub 发行
```

开发过程的资产归属、覆盖更新、失败现场保留与提交大小约束见[开发资产治理](docs/development/asset-governance.md)。需要自动提交检查时，可执行 `npm run assets:hook`；容量维护按需使用 `npm run assets:audit`（只读预算审计）。这些命令不作为启动或日常调试的前置条件。

凭据继续使用 Windows Credential Manager。不要将开发凭据提交到 Git：

```text
DEEPSEEK_API_KEY=
MINIMAX_API_KEY=
```

### 运行结构

```text
用户 → ASR → 主 Agent/LLM → Speech transaction → TTS / Player
                         └→ request_shadow({"request": true})
                                      │
                                      ▼
                         单一 ShadowRuntime → 当前后端已授予的工具
```

主语音链只有一个调度循环和一个语音事务 owner。未播放的助手草稿不会写入历史；只有用户输入、实际传达的助手文本、有效转写精修和有语义影响的打断会进入会话账本。后台执行是同一会话的认知旁路，详见 [Bridge 2.0 桥接协议](docs/architecture/bridge-2.0-protocol.md)。

### 默认入口

- 产品界面：通过桌面程序打开；普通浏览器访问本机地址只显示启动说明。
- 语音服务：使用宿主端点表中的实际地址，首选端口 8765 被占用时由系统分配。
- WebSocket：使用实际语音服务地址的 `/ws`。
- 健康检查：使用实际语音服务地址的 `/health`；可应答不代表全部模型推理已就绪。
- 后台执行控制台：从工作台“后台协作”面板底部的“代理控制台”进入。

### 组件与资源

语音服务、模型和后台执行宿主按随包清单区分必需与可选组件。更新由用户点击卡片中的更新或导入入口触发；待更新的必需组件会阻止整体服务启动。模型版本、文件摘要校验、事务恢复和安装验证见[程序升级与必需组件更新](docs/guides/model-resource-updates.md)。

本地语音引擎的接入、导出、引擎编译与登记过程见 [AnifLive 本地语音接入记录](docs/development/aniflive-integration.md)。

从原生首屏开始统一显示应用图标、当前启动组件、共享进度条和“跳过”按钮；模型与后台执行宿主的启动复核通过后，识别、分析进程和本地语音模型按实时资源预算最多同时加载三路。组件和服务启动门槛见[组件更新说明](docs/guides/model-resource-updates.md)，校验缓存及显卡环境迁移见[启动与显卡环境排查](docs/guides/local-model-compute.md)，调度细节见[启动调度梳理](docs/guides/runtime-resource-management.md)。

### 主要配置

下列 `.local/data` 路径描述开发渠道；分发渠道的对应位置为所选工作区的 `data` 目录。

- `config/default.yaml`：默认运行参数、首选端口、模型、唤醒词、音频和后台执行配置；分发程序使用工作区 `app/config/default.yaml`。
- `.local/data/config/local.yaml`：保存当前非密钥运行参数，不提交真实凭据。
- `.local/data/persona/`：`personality.md`（人格风格）和 `user.md`（用户资料）；通过设置页保存后立即对后续请求生效。源码 `config/templates/persona/` 只用于补齐缺失资料。
- `config/shadow_agent/`：后台执行指令和工具策略。
- `config/dsh/`：后台执行宿主基线；新分发程序锁定上游预发布版 DSH `0.2.1-rc.5`，项目插件 `@voice-terminal/dsh-plugin` 更新为 0.2.2。维护要求见[DSH 插件维护手册](docs/guides/dsh-plugin-maintenance.md)。
- `.local/data/config/product-settings.json`：唤醒词、静默、后台执行、自定义指令和声纹设置等非密钥内容。
- `.local/data/speaker/`：声纹注册资料、模板和特征指纹。

诊断日志集中在工作区 `cache/logs`，属于可回收缓存，保留 **100 MiB** 总预算（`VOICE_LOG_BUDGET_MB`，设为 `0` 关闭）。

### 运行模式

- `fast`（快速，默认）：主 LLM 使用 `reasoning.effort=none`。
- `deep`（深度）：主请求直接使用 `reasoning.effort=low`。

`llm.reasoning_mode` 与 `session.reasoning_mode` 仅允许 `fast` 与 `deep`。参数及恢复说明见[思考模式说明](docs/guides/llm-json-schema-migration.md)。

### 测试与验收

自动化测试、专项回归、文档链接检查、正式包核对和真实设备现场验收的入口与边界见[测试、启动与现场验收入口](docs/testing/operations-and-quality.md)。本地 `.local/cache/logs/` 和 `.local/cache/reports/` 属于本机产物，不属于项目文档。

---

## 许可与致谢

本项目使用 [Apache License 2.0](LICENSE) 开源，第三方组件许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

- **[AnifLive-TTS](https://github.com/Hiruynk/AnifLive-TTS)** —— 由 [Hiruynk](https://github.com/Hiruynk) 开发的本地 TTS 推理框架，本项目的本地语音合成能力建立在其上。
- **[DSH](https://github.com/deepseek-ai/dsh)** —— 后台执行宿主与插件体系。
- **[Tauri](https://tauri.app)** —— 桌面窗口、托盘、单实例与服务管理宿主。
