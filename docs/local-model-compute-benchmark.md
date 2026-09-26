# SenseVoiceSmall CPU/GPU 实测

## 测试条件

- 设备：Windows，Intel Core i7-13650HX、NVIDIA GeForce RTX 4060 Laptop GPU（8 GiB 显存）、16 GiB 系统内存。
- 软件：PyTorch 2.11.0+cu128（CUDA 12.8）、FunASR 1.4.14。
- 输入：模型目录中的 Paraformer 示例音频 asr_example.wav，16 kHz、单声道、5.55 秒。
- 每次测试都在独立 Python 进程中依次加载 SenseVoiceSmall 与其 FSMN-VAD（语音活动检测）模型，再识别同一段音频。现有应用保持运行。
- 内存使用 GiB（2³⁰ 字节）；推理时间只测一次，包含首次推理开销，不代表所有音频长度。

## 结果

| 指标 | CPU（中央处理器） | GPU（图形处理器） |
| --- | ---: | ---: |
| SenseVoiceSmall 实际设备 | CPU | CUDA |
| 内部 FSMN-VAD 实际设备 | CPU | CUDA |
| 模型加载时间 | 10.92 秒 | 12.12 秒 |
| 5.55 秒音频推理时间 | 0.473 秒 | 0.566 秒 |
| 实时率（推理时间 ÷ 音频时长） | 0.085 | 0.102 |
| 工作进程 RSS（常驻内存），加载后 | 3.40 GiB | 1.21 GiB |
| 工作进程 RSS，推理后 | 3.51 GiB | 1.72 GiB |
| 系统可用 RAM，加载后 | 1.00 GiB | 3.59 GiB |
| 系统可用 RAM，推理后 | 1.33 GiB | 3.10 GiB |
| 系统可用显存，加载前 | — | 6.94 GiB |
| 系统可用显存，加载后 | — | 6.00 GiB |
| 系统可用显存，推理后 | — | 5.95 GiB |
| PyTorch 进程峰值 GPU 分配 | 0 | 0.91 GiB |

CPU 与 GPU 两次识别得到完全相同的中文文本：“欢迎大家来体验达摩院推出的语音识别模型。” SenseVoiceSmall 和内部 FSMN-VAD 都成功加载到所选设备。

这个 5.55 秒短样本上，GPU 推理比 CPU 慢约 20%，加载也慢约 1.20 秒；GPU 的主要收益是把加载后进程 RSS 降低约 2.19 GiB，并将系统可用 RAM 提高约 2.60 GiB。GPU 路径使用了约 0.91 GiB 峰值 PyTorch 显存。结果支持“在 RAM 紧张、显存有余量时切到 GPU”的用途，但不能据此承诺每段音频都会更快；不同长度、并发与设备下需要重新测量。

FunASR（语音识别工具包）的接口允许通过 device（运行设备）指定 cpu 或 cuda；项目使用相同路径完成了实际切换。[FunASR Paraformer 示例与设备参数](https://github.com/modelscope/FunASR/blob/main/examples/industrial_data_pretraining/paraformer/README.md)。FunASR 公布的 SenseVoice H100 基准中，GPU 与 CPU 的实时率分别为 169.6x 和 17.2x；它使用 H100 和不同测试负载，不能直接预测本机 RTX 4060 上的时延。[FunASR SenseVoice 基准](https://www.funasr.com/en/blog/funasr-vs-whisper-benchmark.html)

## 应用级设备切换验证

2026-09-26 保存 `sensevoice=cuda`（SenseVoiceSmall 使用 GPU）并完整重启应用后，`GET /api/v1/product/compute-status`（运行状态 API）报告 SenseVoiceSmall 的 `requested_device=cuda`（请求设备）、`device=cuda`（实际设备）、`service=READY`（服务就绪），内部 FSMN-VAD 也为 `cuda`；分析工作进程健康接口报告两者均就绪且 `load_error=null`（无加载错误）。ASR Worker（自动语音识别工作进程）探针返回 `READY`。这确认应用后端已使用 GPU 设置，不只是独立 Python（编程语言）基准进程使用 GPU。

同一状态快照中 Paraformer 也已配置为 `cuda` 并报告就绪。Voice Server FSMN-VAD 当前选择 CPU，但本机 PyTorch CUDA 后端可用；KWS 保持 CPU 是因为尚未安装 Sherpa-ONNX CUDA，SmartTurn 和 WeSpeaker 保持 CPU 是因为 ONNX Runtime 未提供 CUDA 执行提供程序。此应用级检查只验证设备选择和模型加载，不是 CPU/GPU 速度对照；速度对照仍限于上方注明的独立进程与单条音频。

本机当前安装的是 CPU 版 Sherpa-ONNX 1.13.7 和仅提供 Azure/CPU 执行提供程序的 ONNX Runtime 1.24.3。因此本次实测只覆盖 FunASR/PyTorch CUDA 路径；KWS（关键字唤醒）、SmartTurn（语音结束判断）与 WeSpeaker（声纹识别）的 GPU 后端需先安装可选 GPU 依赖后再验证。
