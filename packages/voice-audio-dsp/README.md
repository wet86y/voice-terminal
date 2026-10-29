# voice-audio-dsp（内部保音高音频处理包）

仅依赖 Python 标准库，通过 `ctypes`（外部函数接口）加载同目录 SoundTouch 2.4.0（音频处理库）动态库。`TempoProcessor`（节奏处理器）接受16位单声道 PCM（脉冲编码音频），保持音高调整节奏，跨输入块保留内部缓冲；`finish()`（结束并收尾）排空实际输出，`close()`（释放）丢弃未输出内容。

主会话源码位于此目录。使用仓库根目录 `scripts/build-speech-dsp.py`（受管构建脚本）生成 Windows x64（64位平台）内部包；禁止将生成的动态库和构建目录加入源码历史。构建输入、锁定摘要与正式组件交付见[语音调节架构说明](../../docs/architecture/speech-controls.md)。

包内附官方对应源码归档、LGPL 2.1 或更新版本（较宽松公共许可证）、构建来源及 Windows SDK（开发套件）资源头替换补丁。动态库使用浮点内部采样，外部输入输出保持16位格式；无需 MFC（微软界面类库）或新增 Python 原生扩展环境。
