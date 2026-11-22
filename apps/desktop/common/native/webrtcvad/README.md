# 原生 WebRTC VAD 源码

此目录保留与当前 Python 组件 `webrtcvad-wheels 2.0.14` 匹配的 WebRTC 语音活动检测（VAD）C 源码。源码来自上游 [daanzu/py-webrtcvad-wheels](https://github.com/daanzu/py-webrtcvad-wheels) 的官方 PyPI `2.0.14` 源码归档；归档完整 SHA-256（安全哈希算法）及缓存位置登记在[原生音频源码锁](../../../../../config/native-audio-sources.lock.json)。

从归档 `cbits/webrtc/` 原样复制到本目录 `cbits/webrtc/`，只保留 VAD 算法和其信号处理依赖。Python 绑定 `pywebrtcvad.c`、Python 包装器和打包脚本没有导入。C 源码文件未修改；Windows 构建通过 `build.rs`（Rust 构建脚本）静态编译这些 C 源码，`native_vad.rs`（原生 Rust VAD 接口）直接链接 C ABI。已覆盖模式范围、标准采样率/帧长和静音输入；与现用 Python 实现的样本行为对照仍待完成。

`LICENSE`（许可证）保留上游汇总许可文本，其中项目 Python 包装器采用 MIT（宽松开源许可证），`cbits/webrtc/` 内 WebRTC VAD 代码采用 BSD（宽松开源许可证）风格许可。VAD 源文件还指向 WebRTC `PATENTS`（专利声明）与 `AUTHORS`（贡献者列表）；PyPI（Python 包索引）原归档未单列这两个文件。当前来源锁和[原生音频声明目录](../../../../../licenses/native-audio/README.md)已集中登记、保存固定上游基线的许可证、作者及专利文件，程序组装器将该声明目录一并交付；具体来源和许可适用范围以各文件原文为准。

| 字段 | 中文说明 |
| --- | --- |
| `webrtcvad-wheels 2.0.14` | 与当前运行环境相同的独立 WebRTC VAD Python 包版本 |
| `cbits/webrtc/` | 原样保留并静态编译的 WebRTC VAD 与信号处理 C 源码 |
| `LICENSE` | 上游包 MIT 与 WebRTC BSD 风格许可证汇总文本 |
| `.local/build/audio/webrtcvad-wheels-2.0.14/` | 经摘要验证的完整上游源码包，仅作基线对照 |
