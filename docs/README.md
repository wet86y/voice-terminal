# 文档索引

文档按职责分为四类：

- **架构**：系统结构、组件边界、协议和长期行为契约。
- **说明**：安装、配置、操作和开发维护指南。
- **测试**：测试入口、回归与基准报告、现场验收清单及当前问题。
- **交付**：当前正式构建与交付记录。

测试结果与操作规则分别放入测试和说明目录；协议、架构与产品操作说明各自归类，避免同一索引混排。

## 架构

- [系统架构与生命周期](architecture/architecture.md)
- [兼容性组件与更新交接](architecture/compatibility-component.md)
- [Bridge 2.0（第二版桥接协议）](architecture/bridge-2.0-protocol.md)
- [能力就绪与能力级恢复](architecture/capability-readiness-and-recovery.md)
- [上下文与工具契约](architecture/context-and-tools.md)
- [连续性底座：上下文衔接与定时唤醒](architecture/continuity-foundation.md)
- [LLM（大语言模型）上下文缓存契约](architecture/llm-context-cache-contract.md)
- [WebSocket（实时双向通信）协议](architecture/websocket-protocol.md)
- [语音队列决策与后台问答策略](architecture/语音队列决策与后台问答策略.md)

## 说明

- [更新日志填写规范](development/release-notes.md)
- [GitHub（代码托管平台）源码与发行发布](development/github-release.md)
- [组件设置与分类下载](guides/component-settings.md)
- [CPU/GPU 设备热切换生命周期](guides/compute-device-hot-switch.md)
- [在线切换、自动停听与状态提示](guides/runtime-switching.md)
- [国内镜像与原站回退](guides/download-sources.md)
- [模型运行环境配置指南](guides/模型运行环境配置指南.md)
- [应用生命周期](guides/application-lifecycle.md)
- [ASR（自动语音识别）热词](guides/asr-hotwords.md)
- [ASR 延迟与 CUDA（显卡计算平台）配置](guides/asr-latency-and-cuda.md)
- [桌面应用开发与分发](guides/desktop-application.md)
- [DSH（代理执行宿主）控制台](guides/dsh-console.md)
- [DSH npm（包管理器）分发与安装](guides/dsh-npm-distribution.md)
- [DSH 插件维护](guides/dsh-plugin-maintenance.md)
- [本地目录与覆盖发布](guides/local-layout.md)
- [本地模型运行设备](guides/local-model-compute.md)
- [音频增强：双路精修、情绪背景与声学信箱](guides/audio-enhancements.md)
- [本地 TTS（语音合成）接入](guides/local-tts-integration-20261014.md)
- [本地 TTS 日志与可观测性](guides/local-tts-observability-20261015.md)
- [LLM JSON Schema（结构化数据约束）迁移](guides/llm-json-schema-migration.md)
- [模型资源更新](guides/model-resource-updates.md)
- [运行资源管理](guides/runtime-resource-management.md)
- [语音服务运行依赖卡片](guides/runtime-dependency-components.md)
- [屏幕双图输入](guides/screen-input.md)
- [可选声纹增强](guides/speaker-enhancement.md)
- [启动与退出图标动画](guides/startup-logo-animation.md)
- [流式标点补全](guides/streaming-punctuation.md)
- [TTS 组件分发](guides/tts-component-distribution-20261015.md)
- [TTS 运行说明](guides/tts-runtime.md)
- [统一正式程序与分发安装器](guides/unified-product-installer.md)

## 测试

- [在线切换与自动停听回归（2026-10-20）](testing/runtime-switching-20261020.md)

- [测试、启动与现场验收入口](testing/operations-and-quality.md)
- [启动与维护界面连续性验收](testing/startup-ui-continuity.md)
- [当前已知问题](testing/known-issues.md)
- [桌面现场验收](testing/desktop-acceptance.md)
- [CAM++（声纹模型）流式验收](testing/campplus-streaming-acceptance-20261012.md)
- [下载与首次启动修复验收](testing/download-startup-flow-20261010.md)
- [国内镜像与原站回退验证](testing/download-sources-20261018.md)
- [GitHub 节点测速择优与四路下载接入验证](testing/github-node-selection-20261019.md)
- [关于页、工作区迁移与更新加速验证](testing/about-workspace-acceleration-20261019.md)
- [0.9.1 构建、升级与发布验证](testing/release-0.9.1-20261018.md)
- [0.9.1 应用更新交接与重新发行验收](testing/app-update-handoff-20261018.md)
- [0.9.2 构建、本机安装与公开发行验收](testing/release-0.9.2-20261018.md)
- [0.9.3 构建、本机安装与公开发行验收](testing/release-0.9.3-20261019.md)
- [0.9.5 构建与公开发行验收](testing/release-0.9.5-20261021.md)
- [0.9.4 构建、本机安装与公开发行验收](testing/release-0.9.4-20261020.md)
- [GitHub 节点测速择优与四路下载接入验证](testing/github-node-selection-20261019.md)
- [关于页、工作区迁移与更新加速验证](testing/about-workspace-acceleration-20261019.md)
- [0.9.2 一代 TTS 播放失败调查与修复验证](testing/tts-v1-playback-investigation-20261018.md)
- [DSH 0.2.1-rc.2 升级与分发验收](testing/dsh-rc2-upgrade-20261017.md)
- [本地模型计算基准](testing/local-model-compute-benchmark.md)
- [音频增强替换验收记录](testing/audio-enhancements-20261020.md)
- [开发工作区独立恢复验收](testing/workspace-development-recovery-20261021.md)
- [前端十项失败归因与产品逻辑修订验收](testing/frontend-ten-failures-audit-20261021.md)
- [试听后合成变慢与原生组件覆盖升级](testing/tts-preview-upgrade-20261017.md)
- 本地 TTS 诊断记录仅保存在维护者本机，不纳入源码仓库。
- [内存优化验收](testing/memory-optimization-20261010.md)
- [最近代码审阅与开放风险](testing/recent-code-review-20261016.md)
- [运行环境验证](testing/runtime-validation-20261010.md)
- [并行启动验收](testing/startup-parallel-acceptance.md)
- [TTS 运行时修复验收](testing/tts-runtime-fixes-20261014.md)
- [旧程序升级兼容性验收](testing/upgrade-compatibility-20261016.md)
- [语音可靠性修复验收](testing/voice-reliability-fixes-20261010.md)
- [Voice Server GPU（语音服务显卡）运行依赖实现与验证](testing/voice-gpu-runtime-20261017.md)
- [VoxCPM2（第二代本地语音模型）解码图生命周期回归](testing/voxcpm2-decoder-graph-lifecycle-20261016.md)
- [LLM 协议基线 JSON（结构化数据文件）](testing/llm-protocol-baseline.json)

## 交付

- [当前正式程序与分发安装器](releases/current-release.md)
- [v0.9.6 强制兼容处理与发布构建](releases/v0.9.6.md)
- [v0.9.5 CPU/GPU 设备热切换与进程回收](releases/v0.9.5.md)
- [v0.9.4 语音音色试听、对话与影子执行修复](releases/v0.9.4.md)
- [v0.9.1 下载改进与应用更新修订](releases/v0.9.1.md)
- [v0.9.2 日志导出、感知延迟与语音合成修复](releases/v0.9.2.md)
- [v0.9.3 关于页、工作区迁移与下载加速](releases/v0.9.3.md)
- [CAM++（声纹模型）分发与升级](releases/campplus-distribution-upgrade-20261012.md)
