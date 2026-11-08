<p align="center">
  <img src="apps/desktop/src-tauri/icons/icon.png" alt="Voice Terminal 图标" width="88" height="88">
</p>

<h1 align="center">Voice Terminal</h1>

<p align="center"><strong>低延迟、低成本、易配置的个人语音 Agent</strong></p>

<p align="center">
  <a href="https://github.com/wet86y/voice-terminal/releases/latest"><img alt="最新版本" src="https://img.shields.io/github/v/release/wet86y/voice-terminal?label=%E6%9C%80%E6%96%B0%E7%89%88%E6%9C%AC&color=%2350c878"></a>
  <a href="LICENSE"><img alt="许可证" src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
  <img alt="平台" src="https://img.shields.io/badge/platform-Windows%2010%20%2F%2011%20x64-0078d4?logo=windows&logoColor=white">
  <img alt="状态" src="https://img.shields.io/badge/status-%E5%BC%80%E5%8F%91%E4%B8%AD-orange">
  <img alt="语言" src="https://img.shields.io/badge/speech-ASR%20%E2%86%92%20LLM%20%E2%86%92%20TTS-8b5cf6">
</p>

## 关于项目

Voice Terminal 最开始是准备作为 [OpenClaw](https://github.com/openclaw) 的语音交互插件做的——毕竟谁不幻想一个随叫随到的语音 agent 管家呢？

后来个人小巧思越攒越多，项目架构越来越复杂，插件已经承载不了我需要的功能。索性直接把它转为独立产品。

项目目标很简单：**低延迟、低成本、易配置**的个人语音 agent。

---

## 核心能力

### 1. 低延迟、可打断、自主决策的语音对话

流式 ASR 识别驱动 LLM，模型维护一条**可修订的 TTS 播放队列**：后一段语音在播之前就已经开始准备，播到中途支持修订。
TTS 的播放权限由本地 VAD 和主语音大模型**共同控制**：模型可以按语义选择说话，或者选择沉默。

本机真实对话采样：

| 推理挡位 | 首音延迟（输入提交至首音，中位数） | 样本 |
| --- | --- | --- |
| `fast`（快速，默认 · 无思考） | **1266 ms** | 43 次首播 |
| `deep`（深度 · 低强度思考） | **1703 ms** | 39 次首播 |

> 口径：输入提交至首音，取中位数。上表为历史编辑样本收敛值（样本 2026-10-28 / 11-01），不代表所有交互。

提前准备语音的实际收益：输入较长时（跨度 ≥6 秒），回答与首段发声会在你还在讲话时就已经启动；从末次有效发声到播放首音只需 299–333 毫秒。

### 2. 影子代理：对话链路与执行链路拆分

后台 DSH（DeepSeek Harness）是项目主语音的执行机构。**主语音模型和后台执行模型分开设定**——主语音用低思考挡位保证延迟，后台用高思考强度保证任务执行准确性。

主语音会话不传递指令，只发起需求；系统把对话上下文交给后台高思考模型，由后台确认意图、完成执行，随后回传唤醒主语音会话。

### 3. 自定义指令：高度自由的指令拓展

预设指令辅以关键词捕获，保证在**关闭思考模式**下，自定义指令依然准确遵循。

举个例子：设置一条静默功能，指定执行目标为影子 agent、执行目标为「使用静默提前工具」。之后在语音交互中说出对应的命令词，就能驱动 LLM 完成任务执行。

### 4. 声纹识别：拦截非主人语音

开启前需要先登记你的语音完成主人认证，之后非用户的声音事件会被直接拦截。

考虑到连续对话体验和不同时期的音调偏差，系统在每次开启交互（唤醒词唤醒或手动开启监听）时都有一个宽容期：期间捕获你的声纹并配置临时声纹，与原生登记共同作用。

### 5. 定时任务

语音交互中随口就能设定，也可以在页面手动设。支持 1 分钟到 24 小时间隔，可循环可设次数，到点提醒直接回到语音链路里。

### 6. 屏幕捕获：让 agent 亲眼看一下

你可以让 agent「看一眼你的屏幕」。系统会捕获一张全局截图，以及一张鼠标位置附近的局部截图，把重点凸显出来。

### 7. 可选的双路精修与情绪识别

- **双路精修**：流式识别主路之外，额外挂一条离线路做转写精修，双路结果结合后再交给模型。
- **情绪识别**：可选的情绪识别模型在后台听你的语气，把情绪标签作为背景信息递给模型。

两个开关位于组件设置。

### 8. 热词替换

可配置热词，显著提高专有名词、术语的识别准确性。

### 9. 组件下载

纯 GUI 下载配置界面，一站式处理，**告别终端和命令行**。下载、更新、导入、重置全在页面里完成，带进度、可取消、可重试。

### 10. 在线更新

通过 GitHub 分发完成系统在线更新，带内容摘要校验与回滚。

---

## 环境配置

- **Windows 10 / 11 x64** —— 桌面程序仅支持 Windows。
- **Node.js `>= 20`** —— 源码开发需要；后台执行宿主锁定 `^22.19.0 || >=24.0.0`。正式程序由组件管理自动安装，不用手动准备。
- **WebView2** —— 使用系统自带版本，Windows 10 / 11 已预装。
- **DeepSeek API 密钥** —— 主语音模型必需。在组件页填写，凭据交给 Windows 凭据管理器保存，不写进配置文件。
- **MiniMax API 密钥** —— 仅使用在线 TTS 时需要；本地语音模型则不需要。
- **NVIDIA 显卡** —— 本地TTS模型推理框架仅支持**NVIDIA 显卡**

## 运行模式

- `fast`（快速，默认）：主 LLM 使用 `reasoning.effort=none`。
- `deep`（深度）：主请求直接使用 `reasoning.effort=low`。

---

## 项目架构

1. **语音主 agent 链路** —— 400 ms 心跳的事件驱动轮询调度策略；流式 ASR 识别并驱动 LLM 维护可替换的 `speak` 队列；经由 `speak` 队列驱动流式 TTS 完成语音播放。

2. **语音合成** —— 基于 MiniMax 的低延迟在线 TTS 模型，以及 [Hiruynk](https://github.com/Hiruynk) 开发的 [AnifLive-TTS](https://github.com/Hiruynk/AnifLive-TTS)（极低延迟、低显存占用的双流式 TTS）。本地引擎在本机生成并真实验证，不同显卡与依赖身份的引擎不互相套用。

3. **后端执行** —— 项目封装 DeepSeek Harness，通过插件桥接赋予语音交互完整的 agent 执行能力，也保证了后续项目的自由拓展能力。

4. **语音识别** —— 基于 FunASR 框架搭建的识别框架，采用流式 ASR 模型作为主识别链路，配合 CAM++ 模型完成声纹识别与拦截功能。

---

## 后续开发路线图

1. **长记忆模块** —— 服务于低延迟的记忆处理模块。
2. **局域网互联** —— 多设备场景下，由一台设备承担算力消耗，为其他设备提供更低占用的语音交互能力。

---

## 快速开始

**1. 下载安装器**（Windows 10 / 11 x64，~115 MB）

```powershell
Start-Process "https://github.com/wet86y/voice-terminal/releases/latest"
```

**2. 安装并启动** —— 启动安装器，选择工作区、展开程序、创建桌面快捷方式，启动主程序。

**3.组件下载** —— 打开程序进组件管理页，点「一键下载」或者单独下载，程序检查并补齐语音环境开启组件下载任务，本地TTS以及可选组件按需配置。

**4. 密钥配置** —— 组件下载完毕后配置deepseek api密钥，按需配置minmax api密钥。

**5. 启动程序** —— 组件下载完毕后点击启动程序完成程序启动。

---

## 文档

完整文档按架构、说明、测试与交付四类整理，目录说明与分类索引见 **[文档索引](docs/README.md)**。

- **架构** — [架构与生命周期](docs/architecture/architecture.md) · [Bridge 2.0 桥接协议](docs/architecture/bridge-2.0-protocol.md) · [WebSocket 协议](docs/architecture/websocket-protocol.md) · [上下文与工具](docs/architecture/context-and-tools.md) · [连续性底座](docs/architecture/continuity-foundation.md)
- **说明** — [本地目录与覆盖发布](docs/guides/local-layout.md) · [统一正式程序与分发安装器](docs/guides/unified-product-installer.md) · [组件设置](docs/guides/component-settings.md) · [声纹增强](docs/guides/speaker-enhancement.md) · [热词替换](docs/guides/asr-hotwords.md) · [本地 TTS 运行](docs/guides/tts-runtime.md) · [本地模型运行设备](docs/guides/local-model-compute.md)
- **测试** — [测试、启动与现场验收入口](docs/testing/operations-and-quality.md) · [当前已知问题](docs/testing/known-issues.md)
- **交付** — [当前交付](docs/releases/current-release.md) · [v0.9.12 发行说明](docs/releases/v0.9.12.md) · [GitHub 发布流程](docs/development/github-release.md)

**运行环境要求**：Windows 10 / 11 x64；系统 WebView2；本地语音合成需兼容的 NVIDIA（英伟达）显卡，并预留足够的显存、内存与磁盘空间。

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
- 后台执行控制台：从工作台「后台协作」面板底部的「代理控制台」进入。

### 组件与资源

语音服务、模型和后台执行宿主按随包清单区分必需与可选组件。更新由用户点击卡片中的更新或导入入口触发；待更新的必需组件会阻止整体服务启动。模型版本、文件摘要校验、事务恢复和安装验证见[程序升级与必需组件更新](docs/guides/model-resource-updates.md)。

本地语音引擎的接入、导出、引擎编译与登记过程见 [AnifLive 本地语音接入记录](docs/development/aniflive-integration.md)。

从原生首屏开始统一显示应用图标、当前启动组件、共享进度条和「跳过」按钮；模型与后台执行宿主的启动复核通过后，识别、分析进程和本地语音模型按实时资源预算最多同时加载三路。组件和服务启动门槛见[组件更新说明](docs/guides/model-resource-updates.md)，校验缓存及显卡环境迁移见[启动与显卡环境排查](docs/guides/local-model-compute.md)，调度细节见[启动调度梳理](docs/guides/runtime-resource-management.md)。

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

### 测试与验收

自动化测试、专项回归、文档链接检查、正式包核对和真实设备现场验收的入口与边界见[测试、启动与现场验收入口](docs/testing/operations-and-quality.md)。本地 `.local/cache/logs/` 和 `.local/cache/reports/` 属于本机产物，不属于项目文档。

---

## 许可与致谢

本项目使用 [Apache License 2.0](LICENSE) 开源，第三方组件许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

- **[AnifLive-TTS](https://github.com/Hiruynk/AnifLive-TTS)** —— 由 [Hiruynk](https://github.com/Hiruynk) 开发的极低延迟、低显存占用的双流式 TTS 推理框架，本项目的本地语音合成能力建立在其上。
- **[DeepSeek Harness](https://github.com/deepseek-ai/dsh)** —— 后台执行宿主与插件体系。
- **[FunASR](https://github.com/modelscope/FunASR)** —— 语音识别框架。
- **[MiniMax](https://www.minimaxi.com/)** —— 在线低延迟 TTS 服务。
- **[Tauri](https://tauri.app)** —— 桌面窗口、托盘、单实例与服务管理宿主。
