"""Check that every application version field matches the root package version."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def read_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8-sig"))


def python_version() -> str:
    tree = ast.parse((ROOT / "voice_terminal/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return value.value
    raise ValueError("voice_terminal.__version__（Python 程序版本）未找到。")


def main() -> int:
    root_manifest = read_json("package.json")
    version = root_manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print("失败：package.json 的 version（项目统一版本）必须使用三段式版本号。", file=sys.stderr)
        return 1

    checks: list[tuple[str, object]] = [
        ("pyproject.toml [project].version（Python 项目元数据版本）",
         tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]),
        ("voice_terminal.__version__（Python 程序版本）", python_version()),
        ("apps/frontend/package.json version（前端项目版本）",
         read_json("apps/frontend/package.json").get("version")),
        ("apps/desktop/Cargo.toml [workspace.package].version（桌面工作区版本）",
         tomllib.loads((ROOT / "apps/desktop/Cargo.toml").read_text(encoding="utf-8"))
         ["workspace"]["package"]["version"]),
        ("config/distribution.json version（分发配置版本）",
         read_json("config/distribution.json").get("version")),
    ]

    package_lock = read_json("package-lock.json")
    checks.extend([
        ("package-lock.json version（依赖锁项目版本）", package_lock.get("version")),
        ("package-lock.json packages[''].version（根包锁定版本）",
         package_lock.get("packages", {}).get("", {}).get("version")),
        ("package-lock.json packages['apps/frontend'].version（前端包锁定版本）",
         package_lock.get("packages", {}).get("apps/frontend", {}).get("version")),
    ])

    for relative in (
        "apps/desktop/src-tauri/tauri.conf.json",
        "apps/desktop/bootstrap/tauri.conf.json",
        "apps/desktop/installer/tauri.conf.json",
    ):
        checks.append((f"{relative} version（桌面应用版本）", read_json(relative).get("version")))

    cargo_lock = tomllib.loads((ROOT / "apps/desktop/Cargo.lock").read_text(encoding="utf-8"))
    local_crates = {
        "voice-desktop-common",
        "voice-terminal-bootstrap",
        "voice-terminal-desktop",
        "voice-terminal-installer",
    }
    locked_crates = {row["name"]: row["version"] for row in cargo_lock["package"]
                     if row["name"] in local_crates}
    for crate in sorted(local_crates):
        checks.append((f"Cargo.lock {crate} version（本地 Rust 套件版本）", locked_crates.get(crate)))

    mismatches = [(label, value) for label, value in checks if value != version]
    if mismatches:
        for label, actual in mismatches:
            print(f"不一致：{label} 为 {actual!r}，应为 {version}。", file=sys.stderr)
        return 1

    print(f"版本一致：所有 {len(checks)} 个程序版本字段均为 {version}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
