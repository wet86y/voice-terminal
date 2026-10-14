# ASR-Hotword（识别后热词纠错库）来源

上游：https://github.com/HaujetZhao/asr-hotword

许可证：MIT（宽松开源许可），原文保留在本目录 LICENSE（许可证文件）。

源码采用 2026-10-14 性能评估时下载的版本，与本项目基准使用的文件一致。
原始文件 SHA-256（文件摘要）记录于本目录 `SOURCE.json`（来源记录），
同时与 `.local/cache/hotword-benchmark-20261014/results.json` 中的基准文件一致。
产品适配、标点边界、超时和设置校验位于 `voice_terminal/asr/hotwords.py`，
不改变上游纠错算法。发布包携带本目录源码，不依赖临时基准目录或运行时下载。
