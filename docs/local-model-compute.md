# 本地模型运行设备

设置页中的“本地模型设备”可以分别配置 Paraformer（流式语音识别）、SenseVoiceSmall（语音识别与分析）、Voice Server FSMN-VAD（语音活动检测）、KWS（关键字唤醒）、SmartTurn（语音结束判断）和 WeSpeaker（声纹识别）。

每个模型可选择：

- **自动**：可用且显存充足时先尝试 NVIDIA CUDA（GPU 计算平台）；模型加载失败或显存不足时回退 CPU（中央处理器）。
- **仅 CPU**：强制在 CPU 上运行。
- **GPU（CUDA）**：请求在 GPU 上运行。启动失败时回到保存设备选择时记录的上次成功设备，并在设置页显示原因。

更改保存到 `.local/data/config/product-settings.json`（设备偏好文件），需要重新启动整个应用后生效。页面资源摘要和组件行使用宿主同一次采样的 `resource_processes`（去重进程资源明细）。流式识别、语音分析、声纹与代理工作进程各自计量；桌面窗口、渲染进程、应用宿主、界面、环境检查和主语音服务合并为 Voice Server（语音服务）。主语音检测、唤醒模型共享语音服务进程；结束判断共享语音分析进程，只注明资源计入位置，避免重复展示占用。

列表分组的原始字节数之和与顶部总量严格一致，显示为 MB（兆字节）／GB（吉字节）后可能有四舍五入差异。常驻内存是进程当前在物理内存中的工作集；提交内存是进程私有提交量，两者不能混作同一种物理占用。未取得有效测量时直接隐藏该指标；合法的零占用保留。模型行的资源包含所属进程及其子进程，不能解读为模型权重独占大小。

## 后端接入与状态读取

设备设置已接入后端：`PUT /api/v1/product/settings/compute`（保存本地模型设备偏好）写入设备选择并返回 `restart_required`（需要重启）；`GET /api/v1/product/compute-status`（读取硬件与运行状态）读取 GPU 能力、工作进程健康状态、实际设备和进程常驻内存。ASR（自动语音识别）、分析、Voice Server（语音服务）及声纹工作进程从同一份产品设置加载设备偏好。

设备偏好保存后，当前存活的模型不会热迁移；必须完整重启应用，所有工作进程才会按新偏好重新加载。重启前，设置中的“保存后的运行偏好”和“当前实际设备”可能不同。以健康状态返回的 `device`（实际运行设备）与 `service`（服务状态）判断是否已切换，不能只看保存的偏好。

本地组件页的刷新会通过 `GET /api/v1/product/compute-status?refresh=true`（重新检测设备环境）重新运行隔离探测。PyTorch（张量框架）执行实际显卡矩阵运算；ONNX Runtime（模型推理运行时）还核对节点执行记录，拒绝静默回退。关键词唤醒检查专用显卡运行库与物理显卡。逐模型再检查所需模型资源和配置；WeSpeaker（声纹识别）与 SmartTurn（语音结束判断）还通过独立解释器加载实际模型、执行特征推理，并核对显卡卷积或矩阵节点。检查结果随模型文件或能力采样更新失效，单纯提供程序声明或通用矩阵检查不能放行实际模型。独立声纹进程也预加载随包显卡运行库。

未通过时不提供可选显卡项，接口也拒绝保存显卡偏好。组件未就绪或正在刷新环境时，设备选择和保存暂时锁定；保存包含显卡偏好的配置前重新验证环境。历史已保存但现已不可用的显卡偏好显示为禁用项，允许改回自动或中央处理器。

`gpu_dedicated_bytes`（进程专用显存字节）从 Windows（操作系统）GPU Process Memory（显卡进程内存）性能计数器读取，不初始化推理框架；多适配器按进程合并，共享进程与子进程按进程标识、创建时间去重。读取失败保留未知，不能用整张显卡已用量代替应用占用。接口实现依据 [PdhAddEnglishCounterW（加入性能计数器）](https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhaddenglishcounterw) 与 [PdhGetFormattedCounterArrayW（读取实例计数数组）](https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhgetformattedcounterarrayw)。

本机 RTX 4060 于 2026-09-26 完整重启后的检查结果：SenseVoiceSmall（语音识别与分析）请求设备和实际设备均为 `cuda`，服务为 `READY`（就绪），内部 FSMN-VAD（语音活动检测）也报告 `cuda`；Paraformer（流式语音识别）同样报告 `cuda` 和 `READY`。因此后端设置接口、应用启动配置及分析工作进程的 GPU 加载路径已实际连通。Voice Server FSMN-VAD 的 PyTorch CUDA（深度学习框架的 GPU 后端）可用，但当前保存偏好为 CPU；KWS（关键字唤醒）、SmartTurn（语音结束判断）和 WeSpeaker（声纹识别）因相应 GPU 执行后端未安装而保持 CPU。可用能力以状态 API 的逐模型结果为准。

## 安装 GPU 推理后端

当前后端唯一环境位于 `.local/runtimes/python/`（正式运行组件），由 `.local/portable.json`（便携配置）引用。已验证的本机组合包含 PyTorch CUDA 12.8（张量框架的显卡计算运行库）及 ONNX Runtime CUDA（模型推理运行库的显卡执行后端）；每个模型能否使用显卡仍以设备页的实际检测为准。校验现有依赖使用：

```powershell
.\scripts\setup.ps1 -SkipInstall -SkipDsh
```

替换依赖时，先使用 `scripts/build-runtime.py`（后端组件构建工具）在 `.local/tmp/python-candidate/`（组件候选目录）构建并验证完整环境。`--profile cu128`（显卡依赖组合）与 `--profile cpu`（中央处理器依赖组合）选择张量框架版本，`--onnx-profile`（模型格式运行库计算组合）选择对应推理后端，`--verify-cuda`（真实显卡验证）用于有显卡的构建机。构建必须提供 `--vc-redist`（已授权的微软原生运行库来源）；具体依赖锁与构建要求见[流式识别延迟与图形处理器支持](asr-latency-and-cuda.md)。

候选通过校验后，退出所有业务实例并关闭演示窗口，在项目根目录执行：

```powershell
.\scripts\publish-desktop.ps1 -PythonRuntime .\.local\tmp\python-candidate
```

`PythonRuntime`（后端组件候选参数）经同一发布事务换入固定位置，失败恢复原组合，成功后回收旧环境。切换中央处理器组合也采用同一流程。CUDA（显卡计算运行库）依赖必须与本机 NVIDIA（显卡厂商）驱动和 Python（后端解释器）版本兼容；运行组件及模型都只保留当前必要组合，完整规则见[本地目录与覆盖发布](local-layout.md)。

SenseVoiceSmall 及其内部 FSMN-VAD（语音活动检测）始终使用同一设备。FunASR（语音识别工具包）通过 device="cuda" 或 device="cpu" 选择运行位置，具体以加载结果和设置页健康状态为准。

正式程序和源码业务共用上述后端组件与 `.local/models/`（当前模型集合）。SmartTurn（语音结束判断）显卡模式还需要同修订的单精度显卡模型；模型变化先在项目临时区构建完整候选，再通过 `ModelsRoot`（模型组件候选参数）与相应环境一起发布。运行库版本、真实显卡验证、程序与模型组件统一切换及分段计时见[流式识别延迟与图形处理器支持](asr-latency-and-cuda.md)。
