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
