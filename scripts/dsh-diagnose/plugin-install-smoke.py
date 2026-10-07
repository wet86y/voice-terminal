"""隔离执行真实插件安装：旧三包迁移、重复安装、数据保留及三种摘要算法一致性。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]  # ROOT（项目根目录）。
sys.path.insert(0, str(ROOT))
from voice_terminal.config import DshConfig
from voice_terminal.dsh.profile import LEGACY_PACKAGES, NATIVE_SCHEDULE_BUNDLE, PLUGIN_PACKAGE
from voice_terminal.dsh.runtime import DshRuntimeManager


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    if os.name != "nt" or not shutil.which("pwsh"):
        raise RuntimeError("隔离安装检查需要 Windows（操作系统）及 PowerShell 7（命令环境）。")
    fixture = Path(tempfile.mkdtemp(prefix="plugin-install-check-", dir=ROOT / ".runtime"))
    # 独占目录保留安装日志与数据核验结果；运行目录从不指向现有项目数据。
    for relative in ("integrations/dsh/voice-terminal", "config/dsh", "config/shadow_agent"):
        shutil.copytree(ROOT / relative, fixture / relative)
    (fixture / "scripts/dsh-diagnose/lib").mkdir(parents=True)
    for relative in ("scripts/setup-dsh.ps1", "scripts/build-dsh-client.mjs", "scripts/check-dsh-wake-migration.mjs",
                     "scripts/dsh-diagnose/compare-build-state-hashes.mjs", "scripts/dsh-diagnose/lib/dsh-paths.mjs"):
        shutil.copyfile(ROOT / relative, fixture / relative)
    runtime = fixture / ".runtime/dsh"
    runtime.mkdir(parents=True)
    environment = dict(os.environ)
    for key in ("DSH_HOME", "VOICE_DSH_RUNTIME", "VOICE_DATA_DIR", "VOICE_PROGRAM_ROOT"):
        environment.pop(key, None)
    environment.update({"VOICE_TEST_SOURCE": str(ROOT / ".runtime/dsh/source"),
                        "VOICE_TEST_LINK": str(runtime / "source"), "DSH_TELEMETRY_DISABLED": "1"})
    subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                    "New-Item -ItemType Junction -Path $env:VOICE_TEST_LINK -Target $env:VOICE_TEST_SOURCE -ErrorAction Stop | Out-Null"],
                   check=True, env=environment, timeout=30)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        # 这是未启动服务的独立测试项目，安装脚本仍执行真实端口检查。
        (fixture / "config/default.yaml").write_text(f"server:\n  port: {reserved.getsockname()[1]}\n", encoding="utf-8")
    profile = runtime / "home/profiles/web"
    old_artifacts = {"@voice-terminal/dsh-shadow-bridge": "voice-terminal-shadow-bridge",
                     "@voice-terminal/dsh-wake-tasks": "voice-terminal-wake-tasks",
                     "@voice-terminal/dsh-shadow-agent-preset": "voice-terminal-shadow-preset"}
    dependencies = {}
    for name, directory in old_artifacts.items():
        artifact = runtime / "bundles" / directory
        # 旧登记只需无行为的最小功能包样本，避免依赖旧产物目录仍然存在。
        write_json(artifact / "package.json", {"name": name, "version": "0.1.1", "private": True,
                    "type": "module", "main": "./plugin.js", "dsh": {"bundle": {"patch": "./cordis.patch.yml"}}})
        (artifact / "plugin.js").write_text("export function apply() {}\n", encoding="utf-8")
        (artifact / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
        dependencies[name] = "link:" + artifact.as_posix()
    user = runtime / "bundles/retained-user-plugin"
    write_json(user / "package.json", {"name": "@voice-test/retained-plugin", "version": "1.0.0", "private": True,
                                      "dsh": {"bundle": {"patch": "./cordis.patch.yml"}}})
    (user / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    dependencies["@voice-test/retained-plugin"] = "link:" + user.as_posix()
    write_json(profile / "package.json", {"name": "test-profile-web", "private": True, "dependencies": dependencies,
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", *old_artifacts,
                                        NATIVE_SCHEDULE_BUNDLE, "@voice-test/retained-plugin"], "patchReload": "live"}}})
    (profile / "cordis.patch.yml").write_text((fixture / "config/dsh/web-profile.patch.yml").read_text(encoding="utf-8")
        + '- id: voice-terminal-shadow-bridge\n  name: "@voice-terminal/dsh-shadow-bridge"\n  config:\n'
          '    workspaceId: saved-workspace\n    provider: saved-provider\n'
          "- id: voice-terminal-wake-tasks\n  name: '@voice-terminal/dsh-wake-tasks'\n"
          '- id: schedule\n  config:\n    maxHistory: 77\n', encoding="utf-8")
    write_json(runtime / "session-mappings.json", {"schema_version": 2, "sessions": {"saved-voice": {"dsh_session_id": "saved-dsh"}}})
    write_json(runtime / "home/storages/voice-terminal-wake-tasks.json", {"schema_version": 1, "tasks": [{"task_id": "saved-task", "history": ["已保留"]}], "deleted_task_ids": ["deleted-task"]})
    write_json(runtime / "home/storages/schedule.json", {"native": "官方任务样本"})
    preserved = {path: path.read_bytes() for path in (runtime / "session-mappings.json", runtime / "home/storages/voice-terminal-wake-tasks.json", runtime / "home/storages/schedule.json")}
    for index, args in enumerate((["-Offline", "-SkipInstall"], ["-Offline", "-SkipInstall"], ["-Offline", "-ValidateOnly"])):
        with (fixture / f"install-{index}.log").open("w", encoding="utf-8") as log:
            subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(fixture / "scripts/setup-dsh.ps1"), *args],
                           cwd=fixture, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
        assert all(path.read_bytes() == value for path, value in preserved.items()), "安装修改了既有任务或映射"
    manifest = json.loads((profile / "package.json").read_text(encoding="utf-8-sig"))
    selected = manifest["dsh"]["profile"]["bundles"]
    assert selected.count(PLUGIN_PACKAGE) == 1 and not LEGACY_PACKAGES.intersection(selected)
    assert not LEGACY_PACKAGES.intersection(manifest["dependencies"])
    assert NATIVE_SCHEDULE_BUNDLE in selected and "@voice-test/retained-plugin" in selected
    patch = (profile / "cordis.patch.yml").read_text(encoding="utf-8-sig")
    assert "workspaceId: saved-workspace" in patch and "provider: saved-provider" in patch and "maxHistory: 77" in patch
    assert 'name: "@voice-terminal/dsh-plugin"' in patch and "name: '@voice-terminal/dsh-plugin/wake-tasks'" in patch
    config = DshConfig(source_dir=runtime / "source", home_dir=runtime / "home", profile_patch=fixture / "config/dsh/web-profile.patch.yml")
    asyncio.run(DshRuntimeManager(config)._validate_installation())
    subprocess.run(["node", str(fixture / "scripts/dsh-diagnose/compare-build-state-hashes.mjs")], cwd=fixture, env=environment, check=True, timeout=30)
    print(f"隔离安装、重复安装、三包迁移、原生及用户插件保留、数据与三端摘要检查通过：{fixture}")


if __name__ == "__main__":
    main()
