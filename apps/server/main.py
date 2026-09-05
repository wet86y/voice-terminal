from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # type: ignore

from voice_terminal.config import load_config
from voice_terminal.env import load_env_file
from voice_terminal.logging_setup import configure_logging
from voice_terminal.server.websocket import VoiceRuntime, create_app


def build_application(config_path: Path | None = None) -> tuple[object, VoiceRuntime]:
    root = Path(__file__).resolve().parents[2]
    default_path = root / "config" / "default.yaml"
    local_path = config_path or root / "config" / "local.yaml"
    config = load_config(default_path, local_path if local_path.is_file() else None)
    runtime = VoiceRuntime(config)

    @asynccontextmanager
    async def lifespan(_app):
        await runtime.initialize()
        try:
            yield
        finally:
            await runtime.close()

    app = create_app(runtime, lifespan=lifespan)

    return app, runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Terminal v0.1 Voice Server")
    parser.add_argument("--config", type=Path, default=None, help="本地 YAML 配置路径")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    load_env_file(root / ".env.local")
    configure_logging(root / "logs" / "voice-server.log")
    app, runtime = build_application(args.config)
    host = args.host or runtime.config.server.host
    port = args.port or runtime.config.server.port
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
