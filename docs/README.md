# 文档索引

现行文档与历史记录按职责组织。版本、实际构建和发行结果统一查阅[当前交付](releases/current-release.md)；历史报告中的“当前”“待验”指记录当时的范围，不能替代最新状态。

## 按任务查阅

| 任务 | 入口及用途 |
| --- | --- |
| 首次安装、组件配置 | [统一正式程序与安装器](guides/unified-product-installer.md)、[组件设置](guides/component-settings.md)、[模型运行环境](guides/模型运行环境配置指南.md) |
| 设备连接、配对及远程业务 | [设备操作指南](guides/device-connection.md)、[分布式设备架构](architecture/distributed-device-runtime.md)、[远程代理网页](architecture/remote-dsh-console.md)、[局域网版本同步](architecture/lan-version-sync.md) |
| 开发、界面资产及调试 | [代理调试入口](guides/agent-debug.md)、[UI（用户界面）规范](development/ui-standards.md)、[本机目录](guides/local-layout.md) |
| 构建、本机更新和公开发行 | [同包交付流程](development/desktop-pipeline.md)、[更新日志规范](development/release-notes.md)、[GitHub（代码托管平台）发行](development/github-release.md) |
| 故障、验证与未完成事项 | [测试入口](testing/operations-and-quality.md)、[当前已知问题](testing/known-issues.md)、[现场验收清单](testing/desktop-acceptance.md) |

现行操作指南说明当前实现；架构文档维护跨模块契约；带日期的验收、迁移和实施快照保留原时间与证据边界。配置模板、组件锁和正式注册入口是对应字段、版本与操作的实现依据，不从历史测试数字推断当前行为。英文专业名词和字段说明使用中英文对照。

## 架构

- [系统架构与生命周期](architecture/architecture.md)
- [本地语音独立参考登记与默认选择](architecture/aniflive-reference-import.md)
- [Bridge 2.0（第二版桥接协议）](architecture/bridge-2.0-protocol.md)
- [能力就绪与能力级恢复](architecture/capability-readiness-and-recovery.md)
- [兼容性组件与更新交接](architecture/compatibility-component.md)
- [上下文与工具契约](architecture/context-and-tools.md)
- [连续性底座：上下文衔接与定时唤醒](architecture/continuity-foundation.md)
- [分布式设备运行架构与开发进度](architecture/distributed-device-runtime.md)
- [局域网正式版本同步](architecture/lan-version-sync.md)
- [LLM（大语言模型）上下文缓存契约](architecture/llm-context-cache-contract.md)
- [项目进程管理与系统显示身份](architecture/process-management.md)
- [远程 DSH（代理执行宿主）原生网页转发](architecture/remote-dsh-console.md)
- [TTS（语音合成）能力驱动的语音调节](architecture/speech-controls.md)
- [WebSocket（实时双向通信）协议](architecture/websocket-protocol.md)
- [语音队列决策与后台问答策略](architecture/语音队列决策与后台问答策略.md)

## 说明

### 操作与维护指南

- [设备连接、远程配对与断开移除](guides/device-connection.md)
- [组件设置与分类下载](guides/component-settings.md)
- [TTS（语音合成）运行说明](guides/tts-runtime.md)
- [统一正式程序与分发安装器](guides/unified-product-installer.md)
- [本地目录与覆盖发布](guides/local-layout.md)
- [代理调试模式与调用入口](guides/agent-debug.md)
- [应用生命周期](guides/application-lifecycle.md)
- [ASR（自动语音识别）热词](guides/asr-hotwords.md)
- [ASR 延迟与 CUDA（显卡计算平台）配置](guides/asr-latency-and-cuda.md)
- [音频增强：双路精修、情绪背景与声学信箱](guides/audio-enhancements.md)
- [CPU/GPU（中央处理器／图形处理器）设备热切换生命周期](guides/compute-device-hot-switch.md)
- [桌面应用开发、更新日志与分发](guides/desktop-application.md)
- [国内镜像与原站回退](guides/download-sources.md)
- [DSH（代理执行宿主）控制台](guides/dsh-console.md)
- [DSH npm（包管理器）分发与安装](guides/dsh-npm-distribution.md)
- [DSH（代理执行宿主）插件维护](guides/dsh-plugin-maintenance.md)
- [LLM JSON Schema（结构化数据约束）迁移](guides/llm-json-schema-migration.md)
- [本地模型运行设备](guides/local-model-compute.md)
- [模型资源更新](guides/model-resource-updates.md)
- [语音服务运行依赖卡片](guides/runtime-dependency-components.md)
- [运行资源管理](guides/runtime-resource-management.md)
- [在线切换、自动停听与状态提示](guides/runtime-switching.md)
- [屏幕双图输入](guides/screen-input.md)
- [可选声纹增强](guides/speaker-enhancement.md)
- [启动与退出图标动画](guides/startup-logo-animation.md)
- [流式标点补全](guides/streaming-punctuation.md)
- [模型运行环境配置指南](guides/模型运行环境配置指南.md)

### 开发流程与实施跟踪

- [AnifLive（本地语音）接入与运行维护](development/aniflive-integration.md)
- [开发资产治理与覆盖更新](development/asset-governance.md)
- [开发、构建与同包交付流程](development/desktop-pipeline.md)
- [分布式运行实施跟踪](development/distributed-device-implementation-plan.md)
- [GitHub（代码托管平台）源码与发行发布](development/github-release.md)
- [主语音双后端与共享凭据](development/inference-backends.md)
- [更新日志填写规范](development/release-notes.md)
- [UI（用户界面）资产与开发规范](development/ui-standards.md)

### 模块维护与来源说明

- [DSH（代理执行宿主）项目基线](../config/dsh/README.md)
- [DSH 统一插件与测试](../integrations/dsh/voice-terminal/README.md)
- [DSH 诊断工具](../scripts/dsh-diagnose/README.md)
- [保音高音频处理包](../packages/voice-audio-dsp/README.md)
- [WebRTC AEC（回声消除）适配层](../apps/desktop/common/native/webrtc_aec/README.md)
- [WebRTC VAD（语音活动检测）源码](../apps/desktop/common/native/webrtcvad/README.md)
- [原生音频第三方声明](../licenses/native-audio/README.md)
- [参考录音种子及权利边界](../config/resources/assets/aniflive/README.md)
- [离线分词词表](../config/resources/assets/tokenizers/README.md)
- [项目许可与第三方范围](../LICENSING.md)

## 测试

### 当前入口与开放问题

- [测试、启动与现场验收入口](testing/operations-and-quality.md)
- [桌面现场验收](testing/desktop-acceptance.md)
- [当前已知问题](testing/known-issues.md)

### 专项验收与历史记录

- [系统脚本执行与编码边界修复（2026-11-22）](testing/system-shell-hardening-20261122.md)
- [SSH（安全远程连接）默认解释器桥接修复与本机验证（2026-11-22）](testing/ssh-bridge-shell-20261122.md)
- [项目文档梳理与发行对照（2026-11-22）](testing/documentation-review-20261122.md)
- [公共弹窗嵌套布局回归核验（2026-11-22）](testing/ui-dialog-layout-regression-20261122.md)
- [SSH（安全外壳协议）自动配置审计与本机验收（2026-11-21）](testing/ssh-auto-configuration-audit-20261121.md)
- [局域网正式版本同步验证记录（2026-11-21）](testing/lan-version-sync-20261121.md)
- [服务端设置权威修订验证（2026-11-20）](testing/settings-authority-20261120.md)
- [2026-11-20 局域网双机全生命周期验收](testing/lan-lifecycle-20261120.md)
- [代理调试连接与调用工具验收（2026-11-20）](testing/agent-debug-20261120.md)
- [远程后台轨迹与授权按钮映射（2026-11-19）](testing/remote-shadow-approval-20261119.md)
- [远端屏幕捕获放开与批量授权策略修复（2026-11-19）](testing/remote-screen-and-approval-20261119.md)
- [远程模式首音延迟本地计算（2026-11-19）](testing/remote-latency-local-20261119.md)
- [远程媒体恢复与日志修订验证（2026-11-19）](testing/remote-device-recovery-20261119.md)
- [SSH（安全远程连接）广播生命周期与项目身份审计（2026-11-18）](testing/ssh-broadcast-lifecycle-20261118.md)
- [SSH（安全远程连接）广播与配对兼容性修订记录（2026-11-18）](testing/ssh-broadcast-compatibility-20261118.md)
- [SSH（安全远程连接）桥接收尾与重复检查修订（2026-11-18）](testing/ssh-bridge-lifecycle-20261118.md)
- [SSH（安全远程连接）管理员账户识别修订（2026-11-18）](testing/ssh-account-membership-20261118.md)
- [远程客户端连接修订审计（2026-11-18）](testing/remote-client-connection-20261118.md)
- [本机覆盖、分发器与 OpenSSH 验收（2026-11-18）](testing/local-delivery-1.0.0-20261118.md)
- [配对工作区编号修订与验收（2026-11-18）](testing/device-pairing-workspace-id-20261118.md)
- [管理与文件通道就绪等待修订（2026-11-18）](testing/device-channel-wait-20261118.md)
- [分布式运行本机覆盖与 SSH 模拟验收（2026-11-17）](testing/local-delivery-1.0.0-20261117.md)
- [v1.0.0 修订重发验收（2026-11-08）](testing/release-1.0.0-reissue-20261108.md)
- [v1.0.0 首次构建与公开发行验收（已撤回）（2026-11-08）](testing/release-1.0.0-20261108.md)
- [AnifLive 无人值守交付验收（2026-11-08）](testing/aniflive-unattended-delivery-20261108.md)
- [本地语音导入与情绪参考接手续验（2026-11-08）](testing/aniflive-reference-takeover-20261108.md)
- [AnifLive 模型导入匹配审计（2026-11-08）](testing/aniflive-import-audit-20261108.md)
- [AnifLive 无人值守构建验收（2026-11-07）](testing/aniflive-unattended-build-20261107.md)
- [AnifLive 本机程序更新验收（2026-11-07）](testing/aniflive-local-update-20261107.md)
- [AnifLive 本地主线整合与覆盖验证（2026-11-07）](testing/aniflive-local-integration-20261107.md)
- [DSH 0.2.1-rc.5 本机升级实施记录（2026-11-02）](testing/dsh-rc5-upgrade-20261102.md)
- [v0.9.12 本机更新与 GitHub（代码托管平台）发行验收（2026-11-01）](testing/release-0.9.12-20261101.md)
- [Voice Terminal（语音终端）v0.9.11 同版本重发与本机更新验收（2026-11-01）](testing/release-0.9.11-reissue-20261101.md)
- [Voice Terminal v0.9.11 当前源码同版本重发记录（2026-11-01）](testing/release-0.9.11-current-source-20261101.md)
- [0.9.11 GitHub（代码托管平台）公开发行验收（2026-11-01）](testing/release-0.9.11-20261101.md)
- [0.9.10 双后端与协议修复本机覆盖更新（2026-11-01）](testing/local-delivery-0.9.10-20261101.md)
- [0.9.10 本机更新、分发与 GitHub（代码托管平台）发行验收（2026-10-30）](testing/release-0.9.10-20261030.md)
- [2026-10-30 重复校验链路审计后续验收](testing/redundant-validation-followup-20261030.md)
- [0.9.9 最新源码本机覆盖更新（2026-10-29 21:32）](testing/local-delivery-0.9.9-20261029.md)
- [共用组件错误恢复验证（2026-10-29）](testing/component-recovery-20261029.md)
- [智能语音调节实施验证（2026-10-28）](testing/speech-controls-20261028.md)
- [运行依赖增量更新修复验证（2026-10-28）](testing/runtime-incremental-update-20261028.md)
- [0.9.9 最新源码本机覆盖更新（2026-10-28）](testing/local-delivery-0.9.9-20261028.md)
- [0.9.9 本机更新、分发与 GitHub 发行验收（2026-10-26）](testing/release-0.9.9-20261026.md)
- [0.9.8 同版本本机覆盖更新验收（2026-10-25）](testing/local-delivery-0.9.8-20261025.md)
- [持续倾听与二等信箱验收（2026-10-24）](testing/secondary-listening-20261024.md)
- [0.9.8 本机更新与公开发行验收（2026-10-24）](testing/release-0.9.8-20261024.md)
- [项目进程管理验收（2026-10-24）](testing/process-management-20261024.md)
- [0.9.7 本机覆盖更新验收（2026-10-24）](testing/local-delivery-0.9.7-20261024.md)
- [0.9.7 本机更新与公开发行验收（2026-10-23）](testing/release-0.9.7-20261023.md)
- [统一桌面构建流程验收（2026-10-23）](testing/desktop-pipeline-20261023.md)
- [热词增强精修、主路隔离与本机更新验收（2026-10-23）](testing/contextual-dual-pass-20261023.md)
- [0.9.6 构建、本机更新与 GitHub 发布验收（2026-10-22）](testing/release-0.9.6-20261022.md)
- [0.9.6 本机交付验收（2026-10-22）](testing/local-delivery-0.9.6-20261022.md)
- [组件运行期恢复验收（2026-10-22）](testing/component-runtime-recovery-20261022.md)
- [工作区容量审计（2026-10-21）](testing/workspace-footprint-audit-20261021.md)
- [开发工作区独立恢复验收（2026-10-21）](testing/workspace-development-recovery-20261021.md)
- [0.9.5 构建与公开发行验收（2026-10-21）](testing/release-0.9.5-20261021.md)
- [前端十项失败归因与产品逻辑修订验收（2026-10-21）](testing/frontend-ten-failures-audit-20261021.md)
- [在线切换与自动停听回归（2026-10-20）](testing/runtime-switching-20261020.md)
- [0.9.4 构建、本机安装与公开发行验收（2026-10-20）](testing/release-0.9.4-20261020.md)
- [音频增强替换验收记录（2026-10-20）](testing/audio-enhancements-20261020.md)
- [0.9.3 构建、本机安装与公开发行验收（2026-10-19）](testing/release-0.9.3-20261019.md)
- [GitHub 节点测速择优与四路下载接入验证（2026-10-19）](testing/github-node-selection-20261019.md)
- [下载与更新生命周期验证（2026-10-19）](testing/download-update-lifecycle-20261019.md)
- [关于页、工作区迁移与更新加速验证（2026-10-19）](testing/about-workspace-acceleration-20261019.md)
- [0.9.2 一代 TTS 播放失败调查与修复验证（2026-10-18）](testing/tts-v1-playback-investigation-20261018.md)
- [0.9.2 构建、本机安装与公开发行验收（2026-10-18）](testing/release-0.9.2-20261018.md)
- [0.9.1 构建、升级与发布验证（2026-10-18）](testing/release-0.9.1-20261018.md)
- [国内镜像与原站回退验证（2026-10-18）](testing/download-sources-20261018.md)
- [0.9.1 应用更新交接与重新发行验收（2026-10-18）](testing/app-update-handoff-20261018.md)
- [Voice Server GPU（语音服务显卡）运行依赖实现与验证（2026-10-17）](testing/voice-gpu-runtime-20261017.md)
- [试听后合成变慢与原生组件覆盖升级（2026-10-17）](testing/tts-preview-upgrade-20261017.md)
- [DSH 0.2.1-rc.2 升级与分发验收（2026-10-17）](testing/dsh-rc2-upgrade-20261017.md)
- [VoxCPM2（第二代本地语音模型）解码图生命周期回归（2026-10-16）](testing/voxcpm2-decoder-graph-lifecycle-20261016.md)
- [旧程序升级兼容性验收（2026-10-16）](testing/upgrade-compatibility-20261016.md)
- [最近代码审阅与开放风险（2026-10-16）](testing/recent-code-review-20261016.md)
- [TTS 运行时修复验收（2026-10-14）](testing/tts-runtime-fixes-20261014.md)
- [CAM++（声纹模型）流式验收（2026-10-12）](testing/campplus-streaming-acceptance-20261012.md)
- [语音可靠性修复验收（2026-10-10）](testing/voice-reliability-fixes-20261010.md)
- [运行环境验证（2026-10-10）](testing/runtime-validation-20261010.md)
- [内存优化验收（2026-10-10）](testing/memory-optimization-20261010.md)
- [下载与首次启动修复验收（2026-10-10）](testing/download-startup-flow-20261010.md)
- [启动与维护界面连续性验收](testing/startup-ui-continuity.md)
- [并行启动验收](testing/startup-parallel-acceptance.md)
- [本地模型计算基准](testing/local-model-compute-benchmark.md)

- [LLM（大语言模型）协议基线数据](testing/llm-protocol-baseline.json)

### 已退役指南与阶段快照

以下文档保留历史背景；当前运行及交付使用上方指南。

- [本地语音、独立音色库与提前合成（2026-10-14）](guides/local-tts-integration-20261014.md)
- [本地语音日志容量与旧后端诊断记录（2026-10-15）](guides/local-tts-observability-20261015.md)
- [TTS（语音合成）组件与分发下载（2026-10-15）](guides/tts-component-distribution-20261015.md)
- [分布式设备实施与验收交接（2026-11-18 历史快照）](development/distributed-device-parallel-handoff.md)

## 交付

- [当前正式程序与分发安装器](releases/current-release.md)
- [v1.1.0 发行说明](releases/v1.1.0.md)
- [v1.0.0 发行说明](releases/v1.0.0.md)
- [v0.9.12 发行说明](releases/v0.9.12.md)
- [v0.9.11 发行说明](releases/v0.9.11.md)
- [v0.9.10 发行说明](releases/v0.9.10.md)
- [v0.9.9 定时任务间隔、声纹拦截与进程管理](releases/v0.9.9.md)
- [v0.9.8 持续倾听、声纹筛选与播报修订](releases/v0.9.8.md)
- [v0.9.7 发行说明](releases/v0.9.7.md)
- [v0.9.6 强制兼容处理与发布构建](releases/v0.9.6.md)
- [v0.9.5 CPU/GPU 设备热切换与进程回收](releases/v0.9.5.md)
- [v0.9.4 语音音色试听、对话与影子执行修复](releases/v0.9.4.md)
- [v0.9.3 关于页、工作区迁移与下载加速](releases/v0.9.3.md)
- [v0.9.2 日志导出、感知延迟与语音合成修复](releases/v0.9.2.md)
- [v0.9.1 下载改进与应用更新修订](releases/v0.9.1.md)
- [v0.9.0 发行说明](releases/v0.9.0.md)
- [CAM++（声纹模型）分发与升级（2026-10-12）](releases/campplus-distribution-upgrade-20261012.md)
