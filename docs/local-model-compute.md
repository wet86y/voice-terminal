# 本地模型运行设备

设置页中的“本地模型设备”可以分别配置 Paraformer（流式语音识别）、SenseVoiceSmall（语音识别与分析）、Voice Server FSMN-VAD（语音活动检测）、KWS（关键字唤醒）、SmartTurn（语音结束判断）和 WeSpeaker（声纹识别）。TTS（语音合成）不受这些选项影响。

每个模型可选择：

- **自动**：可用且显存充足时先尝试 NVIDIA CUDA（GPU 计算平台）；模型加载失败或显存不足时回退 CPU（中央处理器）。
- **仅 CPU**：强制在 CPU 上运行。
- **GPU（CUDA）**：请求在 GPU 上运行。启动失败时回到保存设备选择时记录的上次成功设备，并在设置页显示原因。

更改保存到 config/product-settings.json，需要重新启动整个应用后生效。显存和系统内存是机器级实时数据；当前后端不报告每个模型的精确显存占用。每个模型显示其工作进程的常驻内存。

## 后端接入与状态读取

设备设置已接入后端：`PUT /api/v1/product/settings/compute`（保存本地模型设备偏好）写入设备选择并返回 `restart_required`（需要重启）；`GET /api/v1/product/compute-status`（读取硬件与运行状态）读取 GPU 能力、工作进程健康状态、实际设备和进程常驻内存。ASR（自动语音识别）、分析、Voice Server（语音服务）及声纹工作进程从同一份产品设置加载设备偏好。

设备偏好保存后，当前存活的模型不会热迁移；必须完整重启应用，所有工作进程才会按新偏好重新加载。重启前，设置中的“保存后的运行偏好”和“当前实际设备”可能不同。以健康状态返回的 `device`（实际运行设备）与 `service`（服务状态）判断是否已切换，不能只看保存的偏好。

本机 RTX 4060 于 2026-09-26 完整重启后的检查结果：SenseVoiceSmall（语音识别与分析）请求设备和实际设备均为 `cuda`，服务为 `READY`（就绪），内部 FSMN-VAD（语音活动检测）也报告 `cuda`；Paraformer（流式语音识别）同样报告 `cuda` 和 `READY`。因此后端设置接口、应用启动配置及分析工作进程的 GPU 加载路径已实际连通。KWS（关键字唤醒）、SmartTurn（语音结束判断）和 WeSpeaker（声纹识别）仍因相应 GPU 执行后端未安装而保持 CPU；可用能力以状态 API 的逐模型结果为准。

## 安装 GPU 推理后端

默认依赖安装 CPU 版 PyTorch（深度学习框架）、ONNX Runtime（ONNX 推理运行时）和 Sherpa-ONNX（语音模型运行时）。在 Windows、Python 3.12 环境中，使用可选 GPU 安装配置：

~~~powershell
.\scripts\setup.ps1 -Python "D:\DevTools\Python312\python.exe" -Gpu
~~~

-Gpu 会安装 PyTorch CUDA 12.8、ONNX Runtime CUDA（CUDA 执行提供程序）和 Sherpa-ONNX CUDA 12/cuDNN 9（CUDA 深度学习库）版本。Paraformer、SenseVoiceSmall 和 FSMN-VAD 使用 PyTorch CUDA。设备选择页只有在对应的运行后端已安装并检测到 GPU 后，才会开放该模型的 GPU 选项。

若要回到默认 CPU 推理依赖，再运行不带 -Gpu 的安装命令：

~~~powershell
.\scripts\setup.ps1 -Python "D:\DevTools\Python312\python.exe"
~~~

安装或替换推理后端时请关闭应用。CUDA 依赖版本必须与本机 NVIDIA 驱动和 Python 运行环境兼容；可参考 [ONNX Runtime CUDA 执行提供程序要求](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html) 和 [Sherpa-ONNX Windows CUDA 安装指南](https://k2-fsa.github.io/sherpa/onnx/install/windows/build-cuda.html)。

SenseVoiceSmall 及其内部 FSMN-VAD（语音活动检测）始终使用同一设备。FunASR（语音识别工具包）通过 device="cuda" 或 device="cpu" 选择运行位置，具体以加载结果和设置页健康状态为准。
