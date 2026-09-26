from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection
from voice_terminal.config import load_config
from voice_terminal.env import load_env_file
from voice_terminal.logging_setup import configure_logging
from voice_terminal.server.product import ProductSettingsStore


async def run(config_path: Path | None = None) -> None:
    try:
        import websockets  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 websockets，请先运行 scripts\\setup.ps1。") from exc

    root = Path(__file__).resolve().parents[2]
    default_path = root / "config" / "default.yaml"
    local_path = config_path or root / "config" / "local.yaml"
    config = load_config(default_path, local_path if local_path.is_file() else None)
    config = ProductSettingsStore(
        path=root / "config" / "product-settings.json",
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
    await asyncio.to_thread(engine.load)

    async def handler(websocket, path=None):
        if path not in (None, "/asr"):
            await websocket.close(code=1008, reason="unknown path")
            return
        await handle_asr_connection(websocket, engine)

    async with websockets.serve(handler, config.asr_worker.host, config.asr_worker.port):
        logging.getLogger(__name__).info(
            "ASR Worker listening on ws://%s:%s/asr (device=%s)",
            config.asr_worker.host,
            config.asr_worker.port,
            engine.device,
        )
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Terminal ASR Worker")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    load_env_file(root / ".env.local")
    configure_logging(root / "logs" / "asr-worker.log")
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
