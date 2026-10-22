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
- `npm run assets:check`（暂存资产检查）通过 3 个文档路径；GitHub 发布结果待源码提交推送后补录。

## GitHub 公开发行

待完成：源码提交推送、`v0.9.6` 正式发行、四项资产回下载摘要核对及 latest（最新发行）入口复核。
