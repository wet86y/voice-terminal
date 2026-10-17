# Voice Server GPU（语音服务显卡）运行依赖实现与验证

本轮为 Voice Server（语音服务）增加 CPU（中央处理器）必需依赖、audio.cpp（语音合成引擎）可选依赖和 GPU（图形处理器）可选依赖三张卡片。GPU 卡片在现用 `runtimes/python`（Python 运行环境）中安装固定 CUDA（显卡计算接口）构建的 PyTorch（深度学习框架）与 torchaudio（音频扩展）；同一解释器可继续使用 CPU 推理。实现、接口和使用条件见[语音服务运行依赖卡片](../guides/runtime-dependency-components.md)。

## 安装与更新行为

- 基础 CPU 依赖仍使用 `config/runtime/voice-runtime.lock.json`（基础依赖锁），可选 GPU 依赖使用 `config/runtime/voice-gpu.lock.json`（显卡依赖锁）。GPU 锁只替换 `torch` 与 `torchaudio`，单独记录版本、官方来源、文件大小和 SHA-256（安全散列摘要）。PyTorch 包约 2.75 GB（十进制），torchaudio 包约 1.67 MB；缺失的固定包才会下载，符合摘要的缓存和已安装依赖会复用。
- CPU 更新会保留已安装 GPU 组合；纯 CPU 环境导入不能覆盖当前 GPU 环境。候选环境内只清除旧版 PyTorch 与 torchaudio 登记文件后再安装新轮子，沿用当前下载、校验、启用与失败回滚事务。
- Paraformer（流式识别）、SenseVoice（语音精修）和 CAM++（声纹识别）复用原模型目录与文件。依赖安装不复制权重，也不自动更改模型设备偏好。
- ONNX Runtime（模型推理运行时）和 Sherpa-ONNX（关键词语音推理运行时）继续使用 CPU 依赖；已经验证的旧 GPU 推理环境按原兼容逻辑保留。

## 代码与模型验证

代码验证覆盖 Python（运行环境）工作器、GPU 环境登记和导入保护、Rust（原生后端）桌面命令，以及前端依赖卡片交互。完成时记录的结果为：Python 定向测试 47 项通过，Rust 公共模块 99 项通过且 11 项按既有配置忽略，桌面命令 22 项通过；前端 Vitest（前端测试框架）325 项通过，正式前端构建通过。验证确认可选卡片缺失不会阻止基础 CPU 环境就绪，GPU 更新保留在同一个解释器和目录，旧 CPU 轮子先从候选中移除，CPU 环境导入不能覆盖已安装的 GPU 环境。

用同一批已安装模型文件分别启动 CPU 和 CUDA 进程进行真实推理。Paraformer 与 SenseVoice 的输出摘要逐字节相同，两个 CUDA 进程也确认实际设备为 `cuda`；CAM++ 在两种设备上均生成 192 维归一化向量，余弦相似度为 `0.9999987025`，最大分量差为 `0.0004454050`。测试前后模型权重及示例音频的 SHA-256 和修改时间保持一致。

单次未预热推理耗时仅记录为观测值：Paraformer CPU/CUDA 为 1510.75/837.90 毫秒，SenseVoice CPU/CUDA 为 375.21/560.84 毫秒。这些结果来自一张 RTX 4060 Laptop GPU（笔记本显卡）和一个示例音频，不能当作不同机器、音频或并发场景的性能保证。

## 交付状态

本轮正式程序和普通分发安装器均已更新为 `product-b5518b4b2b25d924`（正式构建编号）。固定程序包为 101,332,861 字节，SHA-256（安全散列摘要）为 `b7eec0e6e7aa5375e3d8e4c4b3a894a8258f079d4940604254590f91d60a5fc2`；安装器为 110,672,896 字节，摘要为 `b1cbc95838dccec4446d0abb8101a2ce60f044e841e9d29841fb4aff0fc9f0ae`。本机固定本体校验通过，安装器内嵌包摘要与本机程序包一致，且包内含可选 GPU 依赖锁。发布只更新程序本体与安装器，没有触发约 2.75 GB 的 GPU 依赖下载或切换现用模型设备偏好。当前交付清单见[正式程序与分发安装器交付](../releases/current-release.md)。
