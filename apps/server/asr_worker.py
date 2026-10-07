from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):
    program = next(parent for parent in Path(__file__).resolve().parents
                   if (parent / "pyproject.toml").is_file() and (parent / "voice_terminal").is_dir())
    sys.path.insert(0, str(program))

from voice_terminal.local_layout import project_root

from voice_terminal.resources import bootstrap_resources
if __name__ == "__main__":
    bootstrap_resources("asr")

from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection
from voice_terminal.config import load_config
from voice_terminal.paths import AppPaths
from voice_terminal.env import load_env_file
from voice_terminal.logging_setup import configure_logging
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.application.managed import report, wait_for_shutdown


async def run(config_path: Path | None = None) -> None:
    try:
        import websockets  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 websockets，请先运行 scripts\\setup.ps1。") from exc

    root = project_root(Path(__file__))
    paths = AppPaths.resolve(root)
    default_path = root / "config" / "default.yaml"
    local_path = config_path or paths.config / "local.yaml"
    config = load_config(default_path, local_path if local_path.is_file() else None)
    config = ProductSettingsStore(
        path=paths.config / "product-settings.json",
        config=config,
    ).effective_config()
    engine = AsrWorkerEngine(
        model_dir=config.asr_model_dir,
        device=config.compute.devices.get("paraformer", "auto"),
        fallback_device=config.compute.fallback_devices.get("paraformer", "cpu"),
        sample_rate=config.audio.sample_rate,
        chunk_size=config.asr.chunk_size,
        encoder_chunk_look_back=config.asr.encoder_chunk_look_back,
        decoder_chunk_look_back=config.asr.decoder_chunk_look_back,
    )
    # Do not open the health port until the real model is loaded.  This makes
    # start.ps1 and Voice Server readiness reflect actual ASR availability.
    report("asr", "加载语音识别模型")
    async def handler(websocket, path=None):
        if path not in (None, "/asr"):
            await websocket.close(code=1008, reason="unknown path")
            return
        await handle_asr_connection(websocket, engine)

    try:
        await engine.prepare()
        async with websockets.serve(handler, config.asr_worker.host, config.asr_worker.port):
            logging.getLogger(__name__).info(
                "ASR Worker listening on ws://%s:%s/asr (device=%s)",
                config.asr_worker.host,
                config.asr_worker.port,
                engine.device,
            )
            report("asr", "模型已加载并预热，语音识别接口已监听")
            await wait_for_shutdown()
    finally:
        engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Terminal ASR Worker")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    root = project_root(Path(__file__))
    paths = AppPaths.resolve(root)
    load_env_file(paths.env_file)
    configure_logging(paths.logs / "asr-worker.log")
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
