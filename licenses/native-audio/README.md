# Native audio third-party notices（原生音频第三方声明）

This directory contains verbatim license, author, and patent notice files from the fixed AEC, WebRTC VAD, and libopus source baselines. The archive paths, versions, hashes, and upstream license paths are listed in [`config/native-audio-sources.lock.json`](../../config/native-audio-sources.lock.json). Keep the individual notice files together with the desktop program and the Opus shared library.

| Notice file（声明文件） | Source component（来源组件） | Notice type（声明类型） |
| --- | --- | --- |
| `aec-audio-processing-LICENSE.txt` | `aec-audio-processing 1.0.1` | Upstream package license（上游包许可证） |
| `webrtc-audio-processing-COPYING.txt`, `webrtc-LICENSE.txt` | WebRTC Audio Processing 2.1 | BSD license notices（BSD 许可证声明） |
| `webrtc-PATENTS.txt` | WebRTC | Patent grant（专利授权声明） |
| `webrtc-audio-processing-AUTHORS.txt` | WebRTC Audio Processing | Authors（作者列表） |
| `abseil-LICENSE.txt` | Abseil C++ 20240722.0 | Apache License 2.0（Apache 2.0 许可证） |
| `meson-build-LICENSE.txt` | Bundled Abseil build helper | MIT license notice（MIT 许可证声明） |
| `pffft-LICENSE.txt`, `rnnoise-COPYING.txt`, `ooura-LICENSE.txt`, `spl_sqrt_floor-LICENSE.txt`, `webrtc_fft-LICENSE.txt` | WebRTC embedded signal-processing dependencies | Individual upstream notices（各自上游声明） |
| `webrtcvad-LICENSE.txt` | Vendored `webrtcvad-wheels 2.0.14` VAD source | MIT wrapper and BSD WebRTC notices（MIT 封装与 BSD WebRTC 声明） |
| `libopus-COPYING.txt`, `libopus-AUTHORS.txt` | Xiph.Org `libopus 1.6.1` | BSD-3-Clause license, patent notices, and authors（BSD-3-Clause 许可证、专利声明与作者列表） |

The project adapter itself remains covered by the repository root [LICENSE](../../LICENSE). This notice bundle does not change any upstream license terms.
