本轮完成了模型加载内存优化、按需导入、临时声纹实例释放和处理器轻量检测。Paraformer（流式语音识别）、SenseVoice（整句精修）继续使用原有权重和推理精度；mmap（内存映射）加载与前述代码优化已设为新版源码默认。FSMN-VAD（语音活动检测）的未量化 ONNX（开放神经网络交换格式）导出资产已纳入新版程序及必需组件清单，处理器模式按新版策略自动选择。研究和首轮组件验收只写独立缓存；后续按用户授权同步本机正式本体并重建分发包，运行组件由管理页按当前清单完成更新。INT8（8位整数）精修候选继续只保留在隔离缓存。

首轮研究实施前保存了 196 个源码/配置文件的基线，包含当时已有的源码改动。当时原有 7 个未提交文件为 95 行新增、43 行删除；其中 5 个业务源码与冻结副本逐字节摘要相同，本轮未编辑另外 2 个既有测试文件。基线位于 [baseline-source](<D:/项目开发/语音前端/.local/cache/memory-optimization-20261010/baseline-source/source-manifest.json>)。

**已采用的实现**

- [model_loader.py](<D:/项目开发/语音前端/voice_terminal/asr/model_loader.py>)：对固定 FunASR（语音模型框架）1.4.14 和明确源码 SHA-256（文件摘要）进行校验，在内存中适配上游加载器，文件权重使用 `torch.load(..., mmap=True)`，移除整份 checkpoint（检查点）深拷贝。键名映射、排除、形状检查、严格加载、加载回调保持上游逻辑；文件格式或旧参数不支持映射时回退，真实映射/文件损坏错误继续抛出。没有修改安装依赖目录。
- [sensevoice.py](<D:/项目开发/语音前端/voice_terminal/analysis/sensevoice.py>) 与 [detector.py](<D:/项目开发/语音前端/voice_terminal/vad/detector.py>)：扩展已存在的按需注册方案，精修同时注册自身和内部检测器。独立精修进程的 FunASR 导入模块数由 399 降至 55，检测由 399 降至 28。流式识别原本已按需导入，本轮保持其 78 个模块，收益来自权重加载变化。
- [speaker_runtime.py](<D:/项目开发/语音前端/voice_terminal/server/speaker_runtime.py>)：声纹附属进程恢复健康、注册提交结束、设置页评分结束以及功能关闭时，释放本地缓存实例引用。锁保护构造与引用释放；原生线程正在推理时由其局部引用持有实例，不强制销毁正在使用的 session（推理会话）。
- [engine.py](<D:/项目开发/语音前端/voice_terminal/speaker/engine.py>)：增加可选的有限线程对照，默认仍采用原策略。实测内存收益小且 CPU 推理变慢，所以未将有限线程策略设为默认。

可以用 `VOICE_MODEL_LOAD_MODE=original`（原版权重加载）回退 mmap（内存映射）适配，用 `VOICE_ASR_IMPORT_MODE=full`（完整组件导入）回退按需注册。声纹对照开关为 `VOICE_SPEAKER_SESSION_MODE=original|bounded`（原策略/有限线程），默认 `original`。版本或摘要未知时采用完整注册与原加载器。

**测量条件与解释**

使用项目正式 Python（解释器）3.12.10、PyTorch（深度学习框架）2.11.0+cu128、FunASR 1.4.14、ONNX Runtime（模型推理运行时）1.24.4 和 funasr-onnx（轻量推理包）0.4.3，在约 16 GiB（吉二进制字节）系统内存、RTX 4060 Laptop / 8 GiB 显存上测试。输入固定为本地 16 kHz（千赫兹）单声道录音，记录文件和 PCM（脉冲编码调制音频）摘要。没有访问麦克风或云端。

每侧使用独立新进程，两侧串行，记录导入、加载、预热、推理和释放。大型模型各跑两轮，第二轮反转两侧顺序；每侧预热 2 次、推理 5 次，通常每 50 ms（毫秒）采样。GPU 检测的延迟复测另用预热 3 次、推理 30 次、250 ms 采样，避免低频单块延迟的抽样波动。明确请求 CUDA（显卡计算平台）但实际回退 CPU 的侧结果会失败；本轮所报 GPU 用例均确认实际运行于 CUDA。

下表大型模型取两轮均值，其余为单轮。峰值工作集使用 Windows（操作系统）的历史高水位；私有提交量峰值为周期与阶段边界采样最大值。工作集含共享映射页，私有提交量还包括可分页的进程内存，两者都不能直接当作整套应用独占物理内存。其他应用运行导致部分基线期间系统可用内存低至约 232 MiB（兆二进制字节），因此延迟数据仅作这次测试的观测，不能宣称一般环境下必然同幅加速。

| 组件与设备 | 峰值工作集：原版 → 优化（GiB） | 私有提交量峰值：原版 → 优化（GiB） | 推理输出 |
| --- | --- | --- | --- |
| Paraformer / CPU | 3.07 → 2.23 | 4.11 → 2.89 | 两轮文本一致 |
| Paraformer / GPU（图形处理器） | 3.36 → 2.53 | 5.34 → 3.33 | 两轮文本一致 |
| SenseVoice / CPU | 3.41 → 2.29 | 4.51 → 2.93 | 两轮文本及标签一致 |
| SenseVoice / GPU | 3.79 → 2.58 | 6.04 → 3.36 | 两轮文本及标签一致 |
| FSMN-VAD / CPU，保留原推理引擎 | 0.77 → 0.54 | 1.31 → 1.01 | 事件一致 |
| FSMN-VAD / CPU，轻量 ONNX（开放神经网络交换格式）首轮候选 | 0.77 → 0.088 | 1.32 → 0.154 | 事件一致，现用工作区保持原样，新版组件更新采用该导出 |

Paraformer GPU 的专用显存峰值约 1015 MiB，SenseVoice GPU 约 1119 MiB，两轮的原版与候选基本相同。收益主要来自删除主机中的重复权重和无关导入，原有模型张量本就已运行在显存。GPU 检测的首次专用显存计数存在约 14 MiB 波动，30 次推理复测时两侧均约 133 MiB，张量分配量与保留量也相近。

声纹有限线程的 CPU 私有提交峰值仅由 234.2 降至 231.6 MiB，推理中位耗时由 22.0 增至 39.8 ms；GPU 提交量由约 2029.8 降至 2013.7 MiB、耗时由 3.8 增至 4.2 ms。声纹缓存释放的生命周期测试通过，但释放 ONNX 会话不代表已导入的 CUDA 库与进程上下文都会卸载，不能按整个声纹进程体积承诺回收量。

原始报告都在 [测试缓存](<D:/项目开发/语音前端/.local/cache/memory-optimization-20261010/asr-cpu-r1.json>) 所在目录，文件分别为 `asr-cpu-r1/r2.json`、`asr-cuda-r1/r2.json`、`refine-cpu-r1/r2.json`、`refine-cuda-r1/r2.json`、`vad-cpu-r1.json`、`vad-cuda-r1/r2.json`、`vad-onnx-cpu-r1.json`、`speaker-cpu-r1.json` 和 `speaker-cuda-r1.json`。`asr-default-mode.json`（默认加载实测）确认没有指定加载环境变量时实际采用 mmap；`vad-onnx-constructor.json`（正式检测器构造实测）确认通过资源策略选择轻量后端，导入 FunASR 模块数为零，工作集约 90 MiB。独立角色数据不能直接相加宣称为整套多进程应用的总节省。

**候选资产与验收**

[prepare_memory_optimization_models.py](<D:/项目开发/语音前端/scripts/prepare_memory_optimization_models.py>) 下载官方固定提交的 SenseVoice 模型，并复制检测源文件到独立测试目录，每项核验文件大小和摘要。原始 `.local/models`（生产模型资产）没有覆盖。

FSMN-VAD 未量化导出位于 `.local/cache/memory-optimization-20261010/models/fsmn-vad/onnx`。模型大小 1,730,427 字节，SHA-256 为 `443ac237b4a33ea22b5ae5a6527888bc237d460fa7a20de7b632727a2c7db56f`。源权重保持 `b3be75be477f0780277f3bae0fe489f48718f585f3a6e45d7dd1fbb1a4255fc5`。1/5/20/30/65 帧的分数和缓存输出通过原数值阈值：相对误差 `rtol=1e-4`、绝对误差 `atol=1e-5`；最大逐项绝对差为约 `1.41e-5`，仍满足相对误差条件。

后续实施已把同一导出复制到 `config/resources/assets/fsmn-vad/onnx`（程序随包检测资产），通过必需组件版本 `v2.0.4-onnx-443ac237`（轻量检测组件版本）交付。新策略采用 `policy_schema: 2`（策略格式版本）、`vad_backend: auto`（自动检测后端选择）；运行环境锁加入 `onnx-vad`（轻量检测依赖组），所有六组依赖完成当前版本校验后才允许整体语音服务启动。完整升级规则见 [组件更新说明](model-resource-updates.md)。

正式处理器运行栈报告（一次性本机诊断产物，原位于 `.local/cache/model-resource-updates-20261010/cpu-smoke/cpu-smoke-report.json`；该目录属于可回收缓存，已连同其中的临时 CPython 3.12.10 环境一并清退，不再保留文件本体，此处保留结论）在独立 CPython（Python 解释器）3.12.10 中下载、摘要核验并组装了锁定的 37 个依赖包，实际 ONNX Runtime（模型推理运行库）是处理器版 1.24.3。普通检测器构造选择轻量后端，实际会话只有 `CPUExecutionProvider`（处理器执行提供程序）；没有导入 PyTorch（张量框架）或 FunASR（语音模型框架）。42/42 组流式输入通过，与此前轻量输出 42/42 完全一致，与两侧均通过的黄金对照 36/36 组事件一致。该进程检测器构造后的当前工作集约 84.2 MiB，历史峰值约 145.1 MiB，完成所有用例后约 87.7 MiB；运行库版本和采样流程与上表首轮候选不同，这组数据单独记录，不能混用为同轮降幅。

[真实模型升级报告](<D:/项目开发/语音前端/.local/cache/model-resource-updates-20261010/real-upgrade/report.json>) 使用只读的正式源模型，在独立工作区安装旧 `v2.0.4`（原检测组件版本）后模拟程序清单升级。旧组件被启动门禁拦截，损坏候选被拒绝且旧权重、安装收据、根模型清单和发行登记摘要不变；成功更新后八项文件摘要校验通过、旧目录遗留文件与备份删除、用户设置保持不变。未覆盖现用正式模型目录。

[流式对照](<D:/项目开发/语音前端/.local/cache/memory-optimization-20261010/vad-streaming-comparison.json>)：ONNX 42/42 通过，FunASR 36/42 通过。两侧都成功的 36 个用例，语音开始/结束事件逐项相同。原版的 6 个错误为 20 ms 静音和 10 ms 音频直接作为最终数据块、各测试三种块大小，异常为张量列表不能为空；轻量候选对这些输入通过。本轮没有修饰或隐藏原版缺陷。检测验收脚本已修正空最终刷新，并在错误时非零退出。

SenseVoice 候选来自 [官方 ONNX 模型仓库](https://modelscope.cn/models/iic/SenseVoiceSmall-onnx) 的提交 `4e95991ddb34c70e9b94f026d62ac82a9d941ae1`，存于 `.local/cache/memory-optimization-20261010/models/sensevoice-onnx`。`model_quant.onnx`（量化权重）大小 241,216,270 字节，SHA-256 为 `21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822`。BPE（子词分词器）文件单独取自项目固定的原模型提交 `3847d57b6bdf2dd8875cb1508d2af43d80a16bf7`。官方 ONNX 仓库未声明底层浮点权重提交，所以不能断言这些差异只由量化造成。

首轮整段 ONNX 精修候选峰值工作集约 388 MiB，私有提交峰值约 447 MiB；原包装器则包含内部 VAD 切片，而该首次 ONNX 测量直接输入整段，两者流程不同。这次候选把“达摩院”识别为“打摩院”；统一切片后，该用例全文一致，所以初次差异不能作为量化精度下降的证据。

相同 PCM 和相同 VAD 切片的严格验收由 [validate_sensevoice_candidates.py](<D:/项目开发/语音前端/scripts/validate_sensevoice_candidates.py>) 完成，结果见 [refine-corpus-quality.json](<D:/项目开发/语音前端/.local/cache/memory-optimization-20261010/refine-corpus-quality.json>)。12 例包括 2 个模型示例、9 个中文/英语唤醒词示例和 1 秒静音。两侧均成功执行，音频与切片摘要一致，实际使用 CPU，参考参数确认 FP32（32位浮点），候选实际执行提供程序只有 CPUExecutionProvider（中央处理器执行提供程序）。

严格逐字与标签对照 10/12 通过、退出码 2；没有执行异常。两例全文不同，共 3 个切片文本有差异。中文示例含参考“全国”与候选“全部”、部分词语和标点变化；英语示例差异为句首“The”与“the”大小写。语言、情绪、事件、原始标签、段数和无语音判断全部一致。没有人工校订真值，因此该结果衡量输出兼容性，不表示已证明候选的实际准确率更低，也不能把变化全部归因于量化。当前不替换默认精修模型。

静音用例由共享的参考 VAD 判无切片，候选没有实际调用量化模型，报告明确标记 `model_inference_performed=false`（未执行模型推理）。这只验证无语音流程；没有把它算作直接检验量化模型的静音事件标签。

**测试和边界**

加载器 16 项轻量及真实小检查点测试通过，覆盖嵌套权重、前缀映射、排除、形状不匹配保留值、严格加载、回调、源对象/文件不变，以及未知版本/配置/导入失败回退。声纹实例生命周期和相关识别/检测/取消/共享请求的针对性测试共 62 项通过。

首轮优化的完整后端命令 `.local/runtimes/python/python.exe -B -X utf8 scripts/test-backend.py -q`：1034 项通过、1 项跳过、1 项失败，7 个既有弃用警告。完成必需组件更新功能后的最终同一命令结果为 1058 项通过、1 项跳过、1 项失败。唯一失败为 [test_scheduler_staircase.py:756](<D:/项目开发/语音前端/tests/test_scheduler_staircase.py:756>) 的 `test_secondary_and_final_prompts_are_mutually_exclusive`（次级与最终提示互斥用例），期望末条上下文为 `secondary_mailbox`（次级信箱），实际为 `speech_queue_policy`（语音队列策略）。使用冻结源码、同一测试精确复现该失败，且相关源码摘要与冻结基线相同；本轮未修改这部分既有工作。

最终前端 248 项测试、原生公共模块 45 项测试、桌面宿主 18 项测试及 51 项后端资源针对性检查通过。新版正式本体编译、界面类型检查和界面构建通过；独立工作区中的 297 项固定文件安装校验、原生模型下载和保留数据重装通过。组件流程及分发验收记录见 [组件更新说明](model-resource-updates.md)。

未启动带真实麦克风和云端连接的整套产品，没有把独立模型测量当作实际整机峰值。模型研究阶段未创建提交或覆盖发布；后续用户已授权文档更新、本机程序发布、分发重建及源码提交，最终本体状态以[当前交付](current-release.md)和实际交付清单为准。SmartTurn（发言结束判断）及唤醒词原本已有量化资产，本轮未改其权重。分析进程的 SmartTurn 特征提取仍可能导入 PyTorch，即使后续整合轻量精修，也不能据此宣称整个分析进程已摆脱该框架。

可复核基准示例：

```powershell
.local/runtimes/python/python.exe -B -X utf8 scripts/benchmark_model_memory.py --role asr --model-root .local/models --device cuda --threads 4 --warmup-runs 2 --iterations 5 --sample-interval 0.05 --order candidate-first --output .local/cache/memory-optimization-20261010/asr-cuda-recheck.json
```

其中 `role`（组件角色）、`device`（请求设备）、`threads`（线程预算）、`warmup-runs`（预热次数）、`iterations`（推理次数）、`sample-interval`（采样间隔）、`order`（两侧顺序）和 `output`（报告位置）均在命令行帮助中有中文说明。基准报告的 `success`（成功）表示模型运行完成，输出是否一致由 `comparison.semantic_outputs_equal`（语义输出一致）另行记录；量化质量验收则在任何输出差异时非零退出。

设计依据包括 [PyTorch 官方权重加载说明](https://docs.pytorch.org/tutorials/recipes/recipes/module_load_state_dict_tips.html) 的文件映射方案，以及 [ONNX Runtime 官方线程配置说明](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)。本轮未采用需要额外构造兼容性验证的 meta（无实体参数）初始化或 assign（直接指派权重），也未通过降低现用浮点模型精度获取默认收益。
