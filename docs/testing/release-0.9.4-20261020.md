# 0.9.4 构建、本机安装与公开发行验收

验证日期：2026-10-20（香港时间）。GitHub（代码托管平台）发行标签为 [`v0.9.4`](https://github.com/wet86y/voice-terminal/releases/tag/v0.9.4)，发行说明见[v0.9.4](../releases/v0.9.4.md)。

## 正式程序与分发资产

本机正式程序与分发安装器使用同一产品构建 `product-169ee34cc4324866`。程序包包含 347 个固定文件，大小 103,525,271 字节，SHA-256（完整文件摘要）为 `0fc848ff6c3f5f42415e4377433402e9d9234e052b740e05ce9cf2a5adcea80e`。本机安装版本和构建编号与程序包一致，正式入口大小为 9,774,080 字节，SHA-256 为 `267b681966a11383564ec342fff336a8c203093a512b3f157f5811b51479aefb`。

| GitHub 发行资产 | 大小 | SHA-256（完整文件摘要） |
| --- | ---: | --- |
| `VoiceTerminalSetup.exe`（普通分发安装器） | 112,895,488 字节 | `9da4b3f9cad4f1a9d9b5921ca4d46a45fce1d7b4f48acc628e11b3207b2ca397` |
| `delivery.json`（交付记录） | 724 字节 | `a17cc5eb1c4947018d2911baf7bfe28135118aa445d070452528c0dc5c05f590` |
| `update.json`（更新清单） | 1,891 字节 | `3d3f4e75962563618bae4cb11412f0d6d1ae6bb9faa82ecab93b4d8ff84911e1` |
| `SHA256SUMS.txt`（摘要列表） | 247 字节 | `bba22a8ea4e4b2ba0e36ae2c0ac0f9b26946f6c77614697a75a9dc76b9816634` |

发行资产由本地普通优化构建生成，`qualification=false`（隔离验收开关关闭）。公开资产逐项回下载后与本机完整 SHA-256 一致；latest（最新发行）入口和更新清单均指向 `0.9.4`。

## 构建和安装

- `pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/publish-desktop.ps1`（本机程序发布）通过；正式入口更新到 0.9.4，与 347 个固定本体文件的程序包构建编号及摘要一致。
- `pwsh -NoProfile -ExecutionPolicy Bypass -File .local/Verify-Package.ps1`（本机固定本体校验）通过。
- `pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/distribute-desktop.ps1 -SkipProductBuild`（复用本轮程序包构建分发安装器）通过；安装器构建戳、内嵌程序包摘要和 `delivery.json`（交付记录）一致。
- 桌面发布流程执行前端 TypeScript（类型脚本）检查和生产构建，并完成桌面宿主、启动入口及安装器的优化构建。Rust（桌面宿主语言）编译有 5 条既有未使用代码警告，无编译错误。
- `pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/publish-github-release.ps1 -SkipBuild`（复用本轮资产发布 GitHub 发行版）完成本地版本、构建戳、安装器大小与摘要、发行资产存在性核验；公开发行资产回下载摘要及 latest 入口另行核验。

本轮未重复运行自动回归测试；本记录覆盖版本一致性、正式构建、本机固定本体和分发资产核验，不代表完成了全量测试或不同设备上的现场语音验收。
