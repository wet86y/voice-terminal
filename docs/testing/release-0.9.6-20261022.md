# 0.9.6 构建、本机更新与 GitHub 发布验收（2026-10-22）

## 对照范围

- 上一次已发布版本为 `0.9.5`，标签 [`v0.9.5`](https://github.com/wet86y/voice-terminal/releases/tag/v0.9.5) 对应源码提交 `9e36aa2d4391f9cfa52437d715c60b00c8e7c394`。
- 本机更新前的正式程序为版本 `0.9.6`、构建 `product-356ca02c8c4c2cd6`，源码提交 `f510e6cc1e136c2ea3b5b1b8f8d7803f3cc6b545`；本轮完整发行说明覆盖 v0.9.5 到最终 0.9.6 源码的全部用户可感知变化。
- 最终构建使用源码提交 `d6bb9cdadb3f82e80229c71636bf8bd475f994ae`，产品版本保持 `0.9.6`。
- 公开更新前，GitHub latest（最新发行）为 `v0.9.5`，没有 `v0.9.6` 标签；本报告在公开发布完成后补录标签、资产及回下载核验结果。

## 正式程序包和安装器

| 交付项目 | 本轮值 |
| --- | --- |
| `version`（产品版本） | `0.9.6` |
| `build`（构建编号） | `product-00553ed3cf2ff675` |
| `program_package`（正式程序包） | 104,344,677 字节；363 个固定文件 |
| `program_package_sha256`（程序包 SHA-256 文件摘要） | `7f033c4357d629e1be198af2d5bea059ac1751f871d1d89c89bad07028981cb7` |
| `installed_app_files`（本机固定本体文件数） | 359 |
| `installer`（普通分发安装器） | 115,339,776 字节 |
| `installer_sha256`（安装器 SHA-256 文件摘要） | `dbbceb40063378057c683177fd463b4e176f36f936aef74bbb3904a9feaca36f` |
| `delivery_record`（安装器交付记录） | 892 字节；SHA-256 为 `1a163d6840072a527b15e095ac321a6165b69de2fb839ead6f3f4ca184940bee` |
| `installer_package_sha256`（安装器内嵌程序包摘要） | 与正式程序包一致：`7f033c4357d629e1be198af2d5bea059ac1751f871d1d89c89bad07028981cb7` |
| `installer_build`（安装器构建类型） | 优化构建，`qualification=false`（隔离验收模式关闭） |

## 构建和本机更新

- `npm run desktop:publish`（构建并事务覆盖本机正式程序）成功；前端 TypeScript（类型脚本）类型检查、Vite（前端构建工具）正式构建、桌面宿主与启动引导程序的 Rust（桌面原生代码）优化构建均完成。
- `npm run desktop:dist`（构建分发安装器）成功。完成正式程序包构建后，以该程序包重装本机正式目录；因安装脚本重新组装程序包，随后从最终程序包重新构建普通安装器，最终安装器戳中的程序包摘要与最终程序包一致。
- Cargo（Rust 包管理器）的镜像参数未通过 TOML（配置格式）解析，现有构建脚本自动回退原始软件源并成功完成构建。
- `.local/Verify-Package.ps1`（本机完整性校验脚本）退出码为 0；本机 `app/release.json`（程序版本清单）、最终程序包和最终安装器构建戳的版本、构建编号及摘要关系一致。
- 强制兼容记录为 `status=compatible`（兼容）、`stable=true`（结果稳定），18 项任务完成；有效运行组件已复用。
- 桌面宿主输出 5 条未使用代码警告，没有构建错误；本轮未运行独立回归测试套件。本记录不代表完成所有设备上的现场语音验收。
- `npm run assets:check`（暂存资产检查）通过 3 个文档路径；源码和版本文档随发行提交推送，公开资产核验结果见下节。

## GitHub 公开发行

正式发行标签 [`v0.9.6`](https://github.com/wet86y/voice-terminal/releases/tag/v0.9.6) 指向提交 `8778fa6885398c890382c1601ca13b084117efcf`，其中产品程序构建基线为 `d6bb9cdadb3f82e80229c71636bf8bd475f994ae`。GitHub 发行版为公开正式发行，非草稿、非预发布版；公开 latest（最新发行）入口已返回 v0.9.6。发行正文与本地[v0.9.6 发行说明](../releases/v0.9.6.md)一致，`update.json`（更新清单）的 `releaseNotes`（中文发行说明）也与该文档转换后的正文一致。

| GitHub 发行资产 | 大小 | SHA-256（完整文件摘要） |
| --- | ---: | --- |
| `VoiceTerminalSetup.exe`（普通分发安装器） | 115,339,776 字节 | `dbbceb40063378057c683177fd463b4e176f36f936aef74bbb3904a9feaca36f` |
| `delivery.json`（交付记录） | 892 字节 | `1a163d6840072a527b15e095ac321a6165b69de2fb839ead6f3f4ca184940bee` |
| `update.json`（更新清单） | 2,220 字节 | `0fa999184bda9fc2f1f844f23e9649dddecbf6ab480d1bb8f1592b939cbfb1a1` |
| `SHA256SUMS.txt`（摘要列表） | 247 字节 | `6124de6536caa2b96082c4763cd723fe096b5864f0d578d89746baec6e545882` |

发行脚本 `scripts/publish-github-release.ps1 -SkipBuild`（复用已构建资产发布发行版）完成发布；四项资产均通过 `gh release download`（GitHub 发行下载）回下载，与本机构建输出的大小及 SHA-256 完整一致。发行前的源码与版本说明提交已推送至 `main`（源码主分支）提交 `8778fa6885398c890382c1601ca13b084117efcf`；本次公开资产记录的 `assets:check`（暂存资产检查）通过 2 个文档路径，发行前也曾通过 3 个文档路径。
