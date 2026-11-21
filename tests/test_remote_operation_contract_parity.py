"""远程产品操作在网页、原生白名单及服务端固定路由间保持一致。"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index + len(start))
    return source[start_index:end_index]


def test_registered_desktop_commands_share_permissions_dispatch_and_debug_catalog() -> None:
    desktop = ROOT / "apps/desktop/src-tauri"
    registrations = _between(
        (desktop / "src/commands.rs").read_text(encoding="utf-8"),
        "$apply![", "\n        ]",
    ).split("[", 1)[1]
    handlers = [item.strip().rsplit("::", 1)[-1] for item in registrations.split(",") if item.strip()]
    assert handlers and len(handlers) == len(set(handlers)), "正式命令名不能为空或重复。"
    declared = set()
    for source in (desktop / "src").rglob("*.rs"):
        declared.update(re.findall(
            r"#\[tauri::command(?:\([^\]]*\))?\]\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
            source.read_text(encoding="utf-8"),
        ))
    assert set(handlers) == declared, "每个正式命令须登记一次，不能遗漏处理器或登记不存在的函数。"
    main = (desktop / "src/main.rs").read_text(encoding="utf-8")
    build = (desktop / "build.rs").read_text(encoding="utf-8")
    assert 'include!("commands.rs")' in main
    assert 'desktop_command_registry!(desktop_handler)' in main
    assert 'include!("src/commands.rs")' in build
    assert 'desktop_command_registry!(command_manifest_from_registry)' in build
    assert 'commands.join("\\n")' in build and 'desktop-commands.txt' in build
    assert 'include_str!(concat!(env!("OUT_DIR"), "/desktop-commands.txt"))' in (
        desktop / "src/agent_debug/ui.rs"
    ).read_text(encoding="utf-8")


def test_frontend_product_operations_reach_native_and_server_allowlists() -> None:
    frontend_source = (ROOT / "apps/frontend/src/product/api.ts").read_text(encoding="utf-8")
    frontend_map = _between(
        frontend_source,
        "const REMOTE_PRODUCT_OPERATIONS:",
        "\n}\n\nasync function remoteModeSelected",
    )
    frontend_dispatch = _between(
        frontend_source,
        "async function remoteProductResponse",
        "async function readError",
    )
    frontend_operations = set(
        re.findall(r":\s*'([a-z][a-z0-9_.]+)'", frontend_map)
    )
    frontend_operations.update(
        re.findall(r"\boperation\s*=\s*['\"]([a-z][a-z0-9_.]+)['\"]", frontend_dispatch)
    )

    native_source = (ROOT / "apps/desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    native_allowlist = _between(
        native_source,
        "const REMOTE_BUSINESS_OPERATIONS: &[&str] = &[",
        "\n    ];",
    )
    native_operations = set(re.findall(r'"([a-z][a-z0-9_.]+)"', native_allowlist))

    server_source = (ROOT / "voice_terminal/server/http_api.py").read_text(encoding="utf-8")
    server_map = _between(
        server_source,
        "DEVICE_MANAGEMENT_OPERATIONS: dict[str, tuple[str, str, bool]] = {",
        "\n}\n\n\ndef _remote_management_request_spec",
    )
    server_dispatch = _between(
        server_source,
        "def _remote_management_request_spec(",
        "\ntry:\n    from fastapi",
    )
    server_operations = set(re.findall(r'"([a-z][a-z0-9_.]+)"\s*:', server_map))
    server_operations.update(
        re.findall(
            r'"((?:product|settings|models|hotwords|tts|speaker|wake_task|model|provider|dsh|voice)\.[a-z0-9_.]+)"',
            server_dispatch,
        )
    )

    assert frontend_operations, "frontend remote operation map must not be empty"
    assert not (frontend_operations - native_operations), (
        "frontend operations missing from Rust native allowlist: "
        f"{sorted(frontend_operations - native_operations)}"
    )
    assert not (frontend_operations - server_operations), (
        "frontend operations missing from Python fixed server dispatch: "
        f"{sorted(frontend_operations - server_operations)}"
    )
