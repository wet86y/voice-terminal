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
