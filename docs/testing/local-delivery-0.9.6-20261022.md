# 0.9.6 本机重建与分发安装器验收（2026-10-22）

## 对照范围

- 产品版本保持 `0.9.6`，没有修改版本字段。
- 上一次成功的本机程序包为 `product-32de7738c48f743a`，大小 104,230,026 字节，SHA-256（内容摘要）为 `a7e419dc6ef2b4d27ac01e2228c3bfad3d4a5ad824611a81ba3871bce33dd8f1`。
- 本次使用仓库最新源码提交 `f510e6cc1e136c2ea3b5b1b8f8d7803f3cc6b545` 重建本机正式程序包，并从同一程序包生成普通分发安装器。
- 本次用户可感知变化包括：按需完整性维护、组件未安装时保留下载与导入入口、设备热切换和启动交接稳定性修复，以及运行期组件更新保留主页面并持续显示恢复状态。

## 构建与核验

- `npm run desktop:publish`（构建并覆盖本机正式程序）成功；前端类型检查、生产构建、优化版桌面宿主和正式入口均成功。
- `scripts/distribute-desktop.ps1 -SkipProductBuild`（复用正式程序包构建安装器）成功；安装器采用优化构建，`qualification=false`（隔离验收模式关闭）。
- Cargo（原生包管理器）的镜像参数首次解析失败，构建脚本自动回退到原站；后续正式程序与安装器构建均成功。
- 本机入口 `--verify-package`（正式本体完整性校验）退出码为 0。
- 本机兼容记录为 `status=compatible`（状态：兼容）、`stable=true`（结果稳定），`tasks`（处理任务）共 18 项并已完成。
- 安装器 `program_package_sha256`（内嵌正式程序包摘要）与本机程序包 SHA-256 完全一致。
- 发布过程报告有效组件已复用；没有运行 GitHub（代码托管平台）发行或上传操作。
- 本轮未重跑独立回归测试；构建包含前端类型检查、前端生产构建及 Rust（桌面原生代码）优化编译，本机完整性校验与兼容检查均通过。

## 产物

| 项目 | 结果 |
| --- | --- |
| `version`（产品版本） | `0.9.6` |
| `build`（构建编号） | `product-356ca02c8c4c2cd6` |
| `program_package`（正式程序包） | `.local/build/program-package/program.zip`；104,340,769 字节；363 个固定文件 |
| `program_package_sha256`（正式程序包摘要） | `5b9edb2a45999caaefb223a217117a9ec5eef535c5a538d80297b1c1db95ce3f` |
| `installed_app_files`（本机固定本体文件数） | 359 |
| `installer`（普通分发安装器） | `.local/releases/installer/VoiceTerminalSetup.exe`；115,297,792 字节 |
| `installer_sha256`（安装器摘要） | `ce3c8d6f8b24755bbc40119a29c5c4420e2353df6eb8c616ae15d1b98e9a215f` |
| `installer_package_sha256`（安装器内嵌程序包摘要） | `5b9edb2a45999caaefb223a217117a9ec5eef535c5a538d80297b1c1db95ce3f` |
| `delivery_record`（安装器交付记录） | `.local/releases/installer/delivery.json`；724 字节；SHA-256 为 `cc9fdf2d0300e81358098f0e962079e0829bed1ffd2151b30328a71dff623478` |
| `package_info`（本机程序包记录） | `.local/build/program-package/package-info.json`；432 字节；SHA-256 为 `0e1845810bfb81fd12eb87c32739213d804c1f430ce1c5d4c7cecbbf5b29e727` |
| `public_release`（公开发行版本） | 仍为 `0.9.5`；本次未上传 |
