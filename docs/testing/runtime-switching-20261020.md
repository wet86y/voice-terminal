# 在线切换与自动停听回归（2026-10-20）

本轮实现统一的右下角运行操作提示，以及音频增强、语音合成、试听播放和声纹采集共用的暂停恢复机制。操作开始前只记录监听状态，连续操作结束后统一恢复；暂停和恢复期间的监听控件禁用，不增加单独的恢复入口。完整入口、实际生效边界和字段见[在线切换说明](../guides/runtime-switching.md)。

## 验证结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 运行操作、增强切换、语音合成接口、音色任务、即时设置、组件宿主和声纹专项 | 147 通过、2 跳过 | `.local/reports/runtime-operations-delivery-focused.log` |
| 推理模式回归 | 22 通过 | `tests/test_reasoning_modes.py`；验证有效模式成功终态、非法模式无更新 |
| 全量后端（首次运行） | 1663 通过、24 失败、3 跳过，130.92 秒 | `.local/reports/runtime-operations-backend-full.log` |
| 24 项失败所属 7 个测试文件（清理后） | 141 通过，17.07 秒 | `tests/test_audio_cpp_tts.py`、`tests/test_local_tts_optimization.py`、`tests/test_tts_revision_guards.py`、`tests/test_kws_acceleration.py`、`tests/test_kws_wake_window.py`、`tests/test_v05_reliability.py`、`tests/test_product_api.py` |
| 全量后端（清理后） | 1687 通过、0 失败、3 跳过、7 条弃用警告，187.53 秒 | `python -X utf8 -m pytest tests -q --tb=short` |
| 前端全量 | 355 通过、34 个测试文件通过 | `.local/reports/runtime-operations-frontend.log` |
| 最终文案相关组件复核 | 弹窗与语音设置 10 通过 | `OperationNotice.test.ts`（操作提示测试）、`TtsSettings.test.ts`（语音设置测试） |
| 前端类型检查及本地构建 | 通过 | `.local/reports/runtime-operations-build.log` |
| 桌面原生调试构建 | 通过，保留既有 5 项未使用代码警告 | `.local/reports/runtime-operations-native-build.log` |

首次全量后端回归中的 24 个失败节点均在清理后消除。17 项本地 TTS（文本转语音）测试原先仍要求已关闭的声学续接字段或回退流程，现改为检查当前参考音频配方和不创建续接上下文；3 项启动测试修正了在线密钥、模型资源和采样率组合；3 个 VAD（语音活动检测）替身补齐语音结束字段；人格设置断言更新为新会话生效。清理没有重新启用声学续接。清理后的全量后端回归为 1687 通过、0 失败、3 跳过。

项目本机程序已通过正式发布流程换入 `.local/app`（日常运行本体），启动入口为 `.local/VoiceTerminal.exe`（正式启动器）。`app/release.json`（本体清单）和 `build/program-package/package-info.json`（程序包记录）的构建编号一致，均为 `product-b6dea0d93ee2c021`；程序包 SHA-256（文件摘要）为 `6f5521606bae46c088044bf90c0a2322fbf3f2817d6380c7b95af02858e7efc4`。桌面宿主大小 16,145,920 字节，构建时间为 2026-10-20 22:07（香港时间）。`.local/Verify-Package.ps1`（本机本体校验脚本）验证通过；模型、Python 运行环境、DSH（代理宿主）和 WebView2（网页运行引擎）目录均保留。前端构建输出位于 `.local/build/frontend`（本地界面构建目录）。

## 新增边界覆盖

- 原先监听／未监听分别恢复，未来组件名称可通过公共契约直接显示。
- 连续预约按顺序执行，操作之间不主动重开麦克风；后台音色任务在返回已接纳之前确定顺序。
- 已有语言模型请求尚未产生音频时，试听仍等待回答完成；空闲录音不被无关的启动状态卡住。
- 排队取消、后台任务首次调度前取消、恢复阶段取消均释放所属租约并发布终态。
- 原模型恢复失败、明确停止和应用关闭不会自动重开监听；监听恢复失败会报告错误。
- 原生录音取消后，先等待录音线程退出，再恢复主监听。
- 宿主组件恢复使用同一状态契约，能力级恢复有独立目标及名称；处理器回退警告不会被误判为恢复失败。
- 进行中显示转圈与阶段；全部完成后停留 300 毫秒并以 160 毫秒淡出；失败保留，旧修订号不覆盖新结果，重连不重放历史成功。

既有模式测试原先把发布快照数量固定为两次，本轮增加进度快照后已改为核对两次模式生效终态，以及非法模式不创建操作或发布更新。

验证使用模拟的原生录音、播放器和合成会话核对并发与释放边界。真实麦克风、扬声器及持续运行中的整机试听仍需现场验收。
