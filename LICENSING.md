# 许可证范围与第三方声明

本文件说明仓库根目录 [LICENSE](LICENSE)（许可证文本）和正式程序包中许可证声明的适用范围。根目录许可证没有把所有第三方文件统一改成 Apache-2.0（Apache 2.0 开源许可证）。第三方源码、依赖、模型、录音及单独发布的组件继续适用各自的许可条件。

| 范围（代码或资源类别） | 仓库位置（主要文件） | 适用许可证（条款来源） |
| --- | --- | --- |
| 本项目原创源码、桌面宿主、界面、独立适配器、构建与补丁编排逻辑 | `voice_terminal/`、`apps/`、`scripts/` 及本项目文档；个别目录/文件另有声明时以其声明为准 | Apache-2.0；根目录 `LICENSE`（许可证文本）是这些原创内容的许可依据。当前是否随正式程序包交付见文末说明。 |
| 上游源码片段及按补丁生成的修改文件 | 见下方 AnifLive-TTS（本地语音引擎）补丁目标表 | 依源文件来源分别适用 PolyForm Noncommercial 1.0.0（非商业用途许可证）、MIT（宽松式许可证）或 Apache-2.0；本项目的根许可证不替换上游许可。 |
| Vendored source（复制进仓库的第三方源码）及独立依赖 | `voice_terminal/vendor/asr_hotword/`、`config/dsh/`、`package-lock.json`、`apps/desktop/Cargo.lock` 及其他组件清单 | 保留各自许可证、版权与 NOTICE（第三方声明）；清单见本文件下方“第三方组件许可清单”。 |
| Model weights（模型权重）、reference audio（参考录音）、生成的推理引擎和其他模型资产 | `config/resources/catalog.json`、`config/aniflive-presets.json` 及本机组件目录 | 不由源码许可证决定；逐项确认模型卡、来源许可、声音权利及分发条件。现有参考录音的分发授权尚未确认，见 [AnifLive 资源说明](config/resources/assets/aniflive/README.md)。 |

## 第三方组件许可清单

| Component（组件） | Use（用途） | License and retained notice（适用许可及保留声明） |
| --- | --- | --- |
| tiktoken（分词器）词表 | 固定摘要的 o200k_base（分词词表）随程序交付，避免首次运行下载。 | MIT；保留 [词表许可证](config/resources/assets/tokenizers/LICENSE) 和原始文件；来源及摘要见 `config/download-sources.json`（下载来源配置）。 |
| [DesktopUpdateKit](https://github.com/wet86y/DesktopUpdateKit) | 固定版本的 Windows 本机 WinHTTP（Windows 网络通信接口）传输层，用于资源下载、取消、续传和摘要校验。应用更新仍由 Voice Terminal（语音终端）的工作区安装事务处理。 | MIT；见 [许可证文本](licenses/DesktopUpdateKit-MIT.txt)。上游修订记录见 [config/update-kit.lock.json](config/update-kit.lock.json)（更新组件锁）。 |
| [audio.cpp](https://github.com/0xShug0/audio.cpp/tree/v0.9.1)（退役的原生语音组件） | 旧兼容源码补丁保留在 `config/resources/patches/audiocpp-quality.patch`（源码补丁文件）；当前桌面程序不再使用 audio.cpp（旧语音框架）作为运行后端，`config/resources/assets/audiocpp/`（旧组件资源目录）不进入正式程序包。 | Apache-2.0；见 [许可证文本](licenses/audio.cpp-Apache-2.0.txt)。固定来源及修订见 `config/resources/catalog.json`（资源目录）和 `scripts/prepare-audiocpp-patch.py`（补丁准备脚本）；补丁生成的上游文件仍保留其来源许可。 |
| [ASR-Hotword](https://github.com/HaujetZhao/asr-hotword)（语音识别热词处理） | 复制进仓库的识别后处理源码。 | MIT；保留 [许可证文本](voice_terminal/vendor/asr_hotword/LICENSE) 和 [来源说明](voice_terminal/vendor/asr_hotword/UPSTREAM.md)。 |
| [AnifLive-TTS](https://github.com/Hiruynk/AnifLive-TTS)（可选本地语音引擎） | 本项目提供独立适配器和兼容补丁器；用户安装该可选组件时，正式程序按固定提交获取上游源码，在本机打补丁并构建模型包。桌面程序包不包含完整上游源码归档、已生成模型包或推理引擎，但补丁器含少量上游代码锚点。 | 分项许可、补丁目标、固定提交及 `Required Notice`（必须保留的版权声明）见本文件“AnifLive-TTS（本地语音引擎）”一节；上游 PolyForm Noncommercial 1.0.0（非商业用途许可证）文本及本地副本见 [许可证文本](licenses/AnifLive-TTS-PolyForm-Noncommercial-1.0.0.txt)。该路径不表示此许可证文本已被组装进正式程序包；依赖和模型仍各循其许可证。 |
| DSH（代理执行宿主）的 npm（JavaScript 包管理器）、Python（后端解释器）及捆绑工具依赖 | DSH 独立控制台和运行时载荷。 | 完整包级清单与许可说明见 [config/dsh/licenses/THIRD_PARTY_NOTICES.md](config/dsh/licenses/THIRD_PARTY_NOTICES.md)；程序包按现有 DSH 配置保留其上游许可证与声明文件。 |
| Cargo（Rust 包管理器）和 npm（JavaScript 包管理器）依赖 | 桌面宿主、安装器和前端构建。 | 固定版本记录于 [apps/desktop/Cargo.lock](apps/desktop/Cargo.lock)（Rust 依赖锁）和 [package-lock.json](package-lock.json)（npm 依赖锁）；每项依赖继续适用其上游许可证。 |

上表列明主要源码组件和分发依赖；各依赖的完整版本仍以相应锁文件和组件清单为准。第三方组件目录内的原始许可证、版权和 NOTICE（第三方声明）优先于本表概括。

## AnifLive-TTS（本地语音引擎）

本项目固定使用的上游源码版本为 v1.5.0-beta，提交号 `9bc349d3a71362b3bfdccbc630983809800954fd`，源码归档摘要记录在 `voice_terminal/tts/aniflive_native.py`（源码准备模块）及 `docs/development/aniflive-integration.md`（集成记录）中。正式程序包组装器不包含完整上游源码归档、已生成 AnifLive-TTS 源码树、模型包或 TensorRT（推理引擎）计划文件；用户安装可选组件时，程序按固定版本获取源码，在本机应用兼容补丁并准备模型。

补丁准备器与适配器属于本项目的独立代码，适用 Apache-2.0。`voice_terminal/tts/aniflive_native.py`（源码准备与补丁应用）和 `voice_terminal/tts/aniflive_export.py`（分阶段导出适配）中也含少量复制自上游文件的源码锚点/校验代码片段；这些片段仍保留其原来源许可。补丁生成的修改文件继续适用对应上游文件许可，不会因补丁器或根目录许可证而整体改成 Apache-2.0。

| Upstream patch target（上游补丁目标） | Upstream origin（上游来源） | Applicable license（适用许可证） |
| --- | --- | --- |
| `src/aniflive_tts/service.py`（显卡显存兼容判断）、`src/aniflive_tts/backend/trt_builder.py`（引擎图解析路径）、`scripts/setup_shared_assets.py`（共享资源目录清单排序） | AnifLive-TTS 原创代码 | PolyForm Noncommercial 1.0.0（非商业用途许可证）；保留 [上游许可证](licenses/AnifLive-TTS-PolyForm-Noncommercial-1.0.0.txt)及 `Required Notice: Copyright 2026 Hiruynk.`（必须保留的版权声明）。 |
| `minimal_inference/export_onnx.py`（ONNX 模型导出脚本） | GPT-SoVITS Minimal Inference（最小推理实现） | Apache-2.0；根目录 [LICENSE](LICENSE) 提供许可证文本，上游修改记录见固定提交的 [来源记录](https://github.com/Hiruynk/AnifLive-TTS/blob/9bc349d3a71362b3bfdccbc630983809800954fd/minimal_inference/ANIFLIVE_TTS_PROVENANCE.json)。 |
| `minimal_inference/GPT_SoVITS/text/korean.py`（韩语文本前端） | GPT-SoVITS（语音模型实现） | MIT；保留 RVC-Boss（上游版权方）的版权与许可，文本见 [上游 MIT 许可证](licenses/AnifLive-GPT-SoVITS-MIT.txt)。 |
| AnifLive-TTS 的其他源文件及其运行依赖 | 固定上游归档、`config/runtime/aniflive-runtime.lock.json`（运行依赖锁） | 文件级来源及依赖各自的许可证；不能仅依据根目录 Apache-2.0 判断。 |

`config/runtime/aniflive-runtime.lock.json`（运行依赖锁）固定依赖版本、下载文件和摘要，但目前没有逐包许可证字段；因此该锁不是完整的逐依赖许可证清单。分发其中的 wheel（Python 二进制包）时，应以每个包自己的许可证/NOTICE（第三方声明）文件及上游依赖声明为准，不能仅以 AnifLive-TTS 的主许可证或本项目的根许可证替代。

上游 [LICENSING.md](https://github.com/Hiruynk/AnifLive-TTS/blob/9bc349d3a71362b3bfdccbc630983809800954fd/LICENSING.md)（许可范围说明）将 AnifLive-TTS 原创代码列为 PolyForm Noncommercial 1.0.0，并将 GPT-SoVITS 派生代码列为 MIT、Minimal Inference 派生代码及适用的 C++（编译语言）部分列为 Apache-2.0；其 [THIRD_PARTY_NOTICES.md](https://github.com/Hiruynk/AnifLive-TTS/blob/9bc349d3a71362b3bfdccbc630983809800954fd/THIRD_PARTY_NOTICES.md)（第三方声明）另列依赖、模型及数据来源。上游文件级声明和许可证文本优先于本项目的概括性分类。

PolyForm Noncommercial 1.0.0 仅许可符合该许可证定义的非商业用途；该许可证不授予 AnifLive-TTS 原创代码的商业使用权。上游说明商业使用须另行取得 Hiruynk 的书面商业许可。本项目的 Apache-2.0 许可不授予这些第三方权利；涉及商业使用、再分发或将补丁生成的源码纳入商业交付前，应先取得适用的书面授权或移除受限部分并重新审查。许可原文见 [PolyForm 官方许可证](https://polyformproject.org/licenses/noncommercial/1.0.0)（非商业许可证正文）。

## 正式程序包与本地文档

本次许可证整理仅修改源码仓库内的文档和许可证文本副本，没有修改正式程序包。当前桌面程序组装器不会把根目录 `LICENSE`（项目许可证）、本文件或根目录 `licenses/`（第三方许可证文件目录）复制到应用目录；因此这些说明可在源码仓库查阅，不应被描述为已随正式程序包交付。组件管理页另行安装的第三方组件仍按各自清单和组件目录保留许可证与声明。

这也表示本次文档更新没有解决正式程序包的许可证交付问题。正式程序包内含项目的补丁器代码及其中少量上游代码片段，但当前组装规则没有把这里列出的上游许可证与范围说明带入应用目录；发布时不能把“源码仓库已有声明”视作接收程序包者已取得相同声明。应按最终交付方式另行确认相关许可证文本或允许的许可证链接如何提供给接收者。
