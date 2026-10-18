# Third-Party Notices

Voice Terminal is licensed under Apache-2.0. Third-party files and bundled dependencies remain under their own licenses. This notice identifies the source components that are included or used by the desktop release build; it does not replace their original license texts.

| Component | Use in this project | License and retained notice |
| --- | --- | --- |
| tiktoken（分词器）词表 | 固定摘要的 o200k_base（词表名称）随程序交付，避免首次运行下载。 | MIT；保留 [词表许可证](config/resources/assets/tokenizers/LICENSE) 和原始文件；来源及摘要见 `config/download-sources.json`（下载来源配置）。 |
| [DesktopUpdateKit](https://github.com/wet86y/DesktopUpdateKit) | Pinned native WinHTTP（Windows 网络传输）transport（传输层）used for resource downloads, cancellation, resume, and digest verification; the application updater still uses Voice Terminal's workspace installation transaction. | MIT; see [licenses/DesktopUpdateKit-MIT.txt](licenses/DesktopUpdateKit-MIT.txt). Source revision is recorded in [config/update-kit.lock.json](config/update-kit.lock.json). |
| [audio.cpp](https://github.com/0xShug0/audio.cpp/tree/v0.9.1) | Pinned Windows voice synthesis server and the reproducible VoxCPM（语音合成模型）compatibility patch in `config/resources/assets/audiocpp/voxcpm-patch.zip`. | Apache-2.0; see [licenses/audio.cpp-Apache-2.0.txt](licenses/audio.cpp-Apache-2.0.txt). Exact upstream release and source revision are recorded in `config/resources/catalog.json` and `scripts/prepare-audiocpp-patch.py`. |
| [ASR-Hotword](https://github.com/HaujetZhao/asr-hotword) | Vendored recognition post-processing source. | MIT; see [voice_terminal/vendor/asr_hotword/LICENSE](voice_terminal/vendor/asr_hotword/LICENSE) and [UPSTREAM.md](voice_terminal/vendor/asr_hotword/UPSTREAM.md). |
| DSH（代理执行宿主）npm、Python（后端解释器）和捆绑工具依赖 | DSH standalone console（独立控制台）and runtime payload. | Full package-level listing and licensing notes: [config/dsh/licenses/THIRD_PARTY_NOTICES.md](config/dsh/licenses/THIRD_PARTY_NOTICES.md). The distribution payload also keeps upstream package license and notice files. |
| Cargo（Rust 包管理器）and npm（JavaScript 包管理器）dependencies | Desktop host, installer, and frontend build. | Exact package versions are recorded in [apps/desktop/Cargo.lock](apps/desktop/Cargo.lock) and [package-lock.json](package-lock.json); each dependency retains its upstream license. |

The installer package embeds the application payload and separately downloaded resources. Those resources retain their upstream licenses and notices in their respective manifests or package directories.
