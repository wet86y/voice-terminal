# Native WebRTC audio processing（原生 WebRTC 音频前处理）

`aec_ffi.cpp` and `aec_ffi.h` are the project adapter around the fixed upstream Audio Processing Module (APM，音频处理模块); they are not copied upstream implementation files. They expose a narrow C ABI (原生函数边界) for the Rust device worker and keep C++ exceptions inside the adapter.

The implementation baseline is `aec-audio-processing 1.0.1` (AEC 上游包版本), which bundles WebRTC Audio Processing `2.1` and Abseil C++ `20240722.0`. The archive URL, SHA-256 (安全哈希算法摘要), source paths, and license paths are recorded in [`config/native-audio-sources.lock.json`](../../../../../config/native-audio-sources.lock.json). The original, digest-verified source remains in `.local/build/audio/`; product changes belong in this project adapter, not in that cached upstream tree.

Run [`scripts/prepare-native-audio.ps1`](../../../../../scripts/prepare-native-audio.ps1) to verify or fetch the locked archives, build the WebRTC static library into `.local/build/audio/aec-native-static-install/`, and write its build receipt (构建回执). The script uses an existing local Python (构建解释器), Meson (C/C++ 构建系统), Visual C++ toolchain (微软 C++ 工具链), and Ninja (构建执行器). It does not install them or change system-wide settings. The desktop build pipeline invokes this preparation before a Windows build that includes the desktop host.

The device worker supplies 16 kHz mono capture frames to the adapter as two 10 ms blocks. Playback references from 16/24/32/48 kHz are low-pass filtered and resampled to the existing 16 kHz preprocessing contract. The worker reports processing health and near-end VAD (近端语音活动检测) with each capture frame; stale references or excessive buffered delay revoke protected-interruption eligibility.

Upstream license and patent notices are copied verbatim into [`licenses/native-audio/`](../../../../../licenses/native-audio/README.md) and included in the assembled product package. The PyPI source archives do not report artifact attestations (发布产物来源证明); the project lock pins their full SHA-256 digests.
