# GitHub source and release publishing

The public repository is `wet86y/voice-terminal`. Source uses Apache-2.0. Windows release assets are built on the maintainer's local Windows machine because the package step consumes local runtime resources.

## Publish a release

1. Commit and push the intended source to `main`.
2. Sign in to GitHub CLI（GitHub 命令行工具）with permission to publish releases.
3. Run `npm run github:release` from the repository root on Windows.

The script runs `npm run desktop:dist`, then validates `package.json`, the product build stamp, installer build stamp, `package-info.json`, `delivery.json`, the installer file size, and its SHA-256 digest. It creates `update.json` with Chinese field descriptions and `SHA256SUMS.txt` for the release assets. Any mismatch stops publication before `gh release create` or `gh release upload`.

The first release is `v0.9.0`. Later releases use the version from the root package manifest and a matching `v<version>` tag. The script refuses a dirty worktree or a local `main` that has not been pushed to `origin/main`.

## Release assets

- `VoiceTerminalSetup.exe` — the self-contained distribution installer.
- `delivery.json` — the validated build and installer record.
- `update.json` — the app updater manifest.
- `SHA256SUMS.txt` — SHA-256 digests for the installer and JSON records.

The app reads `update.json` from the latest public GitHub Release. It downloads the installer only after the user starts the update and verifies the exact size and SHA-256 before handing it to the current workspace's installer transaction.

## 旧版在线更新兼容验收

分发器必须接受已下发程序的 `nonce`（单次编号）格式：十六进制纳秒时间戳、一个连字符和十六进制 `PID`（进程编号）。0.9.0 在启动分发器后正常退出；分发器应在进程存活时核对创建时间和路径，明确已退出时继续检查工作区空闲状态并取得独占锁。查询权限不足不能当作进程已退出。

修订分发器后，使用 `scripts/check-app-update-handoff.py`（应用更新交接验收脚本），传入 `--previous-package`（旧版正式程序包）及 `--previous-sha256`（公开交付记录中的程序包摘要）。脚本使用新的隔离工作区，分别通过正式 `--app-update-config`（应用更新配置入口）验证仍在运行和已退出的旧程序，核对一次性配置消费、目标版本、固定文件摘要、工作区身份及数据保留。成功后回收测试工作区，仅保留报告；不使用真实用户数据。

已下发的旧程序无需修改。在线发行修订时必须同步替换 `VoiceTerminalSetup.exe`（分发器）、`delivery.json`（交付记录）、`update.json`（更新清单）和 `SHA256SUMS.txt`（文件摘要列表），让旧程序下载到的安装器大小和摘要与清单一致。仅替换安装器会使旧程序在下载校验阶段拒绝更新。

## 同版本撤回与重新发行

需要撤回缺陷发行版并保留版本号时，按以下流程执行：

1. 保存原发行记录，再撤回指定版本的公开发行，检查公开最新入口已不再指向该缺陷发行版。
2. 将修复提交并推送到 `main`（源码主分支），保持构建工作区干净；核对目标标签实际指向的提交。`--target main`（创建标签时的目标分支参数）不会自动移动已经存在的标签，同版本重新发行必须将标签绑定到已核对的修订提交。
3. 从该源码重新运行 `scripts/distribute-desktop.ps1`（完整分发构建脚本）。等待正式程序、程序包、安装器和交付记录全部生成；构建过程中目录可能暂时包含不同批次的产物，不能直接发布。
4. 用本轮普通安装器执行真实旧版交接验收，再运行 `scripts/publish-github-release.ps1 -SkipBuild`（复用已构建并验收的资产发布）。该参数不跳过程序包摘要、构建戳、交付记录及安装器大小和摘要校验。
5. 从公开最新更新入口及发行直链回下载四项资产，逐项与本机资产比较完整摘要；同时核对更新清单、摘要列表、安装器大小与发行标签。发布脚本自身只核对资产存在及安装器大小，公开正文和摘要的回下载核验需要另行完成。

源码、发行标签和本轮资产应保持可追溯关系。发行后的纯文档补录可以另行提交，已验证的发行标签继续指向实际用于构建的提交。本次 0.9.1 的执行结果见[重新发行验收](../testing/app-update-handoff-20261018.md#线上重建与重新发行验收)。
