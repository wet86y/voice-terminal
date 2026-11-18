"""远程产品操作在网页、原生白名单及服务端固定路由间保持一致。"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index + len(start))
    return source[start_index:end_index]


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
        "const ALLOWED_OPERATIONS: &[&str] = &[",
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
