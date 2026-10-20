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

from voice_terminal.startup_logging import initialize_worker_logging, worker_imports_ready, startup_stage
if __name__ == "__main__":
    initialize_worker_logging("asr", "asr-worker.log")

from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection
from voice_terminal.config import load_config
from voice_terminal.paths import AppPaths
from voice_terminal.env import load_env_file
from voice_terminal.logging_setup import configure_logging
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.application.managed import report, report_endpoint, wait_for_shutdown
from voice_terminal.application.ports import bind_loopback, endpoint_url

if __name__ == "__main__":
    worker_imports_ready("asr")


async def run(config_path: Path | None = None) -> None:
    try:
        import websockets  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 websockets，请先运行 scripts\\setup.ps1。") from exc

    root = project_root(Path(__file__))
    paths = AppPaths.resolve(root)
    default_path = root / "config" / "default.yaml"
    local_path = config_path or paths.config / "local.yaml"
    with startup_stage("读取语音识别有效配置"):
        config = load_config(default_path, local_path if local_path.is_file() else None)
        config = ProductSettingsStore(
            path=paths.config / "product-settings.json",
            config=config,
        ).effective_config()
    listener, actual_port = bind_loopback(config.asr_worker.port, host=config.asr_worker.host)
    report_endpoint("asr", endpoint_url("asr", actual_port))
    engine = AsrWorkerEngine(
        model_dir=config.asr_model_dir,
        device=config.compute.devices.get("paraformer", "auto"),
        fallback_device=config.compute.fallback_devices.get("paraformer", "cpu"),
        sample_rate=config.audio.sample_rate,
        chunk_size=config.asr.chunk_size,
        encoder_chunk_look_back=config.asr.encoder_chunk_look_back,
        decoder_chunk_look_back=config.asr.decoder_chunk_look_back,
        punctuation_model_dir=config.punctuation_model_dir if config.asr.punctuation_enabled else None,
        punctuation_device=config.asr.punctuation_device,
        offline_model_dir=config.offline_asr_model_dir,
        dual_pass_enabled=config.audio_enhancements.dual_pass,
    )
    # Bind the listener before loading so the host can publish the selected port;
    # health readiness still waits for the fully loaded model below.
    report("asr", "加载语音识别模型")
    async def handler(websocket, path=None):
        if path not in (None, "/asr"):
            await websocket.close(code=1008, reason="unknown path")
            return
        await handle_asr_connection(websocket, engine)

    try:
        with startup_stage("加载并预热语音识别及实时标点模型"):
            await engine.prepare()
        async with websockets.serve(handler, sock=listener):
            logging.getLogger(__name__).info(
                "ASR Worker listening on ws://%s:%s/asr (device=%s)",
                "127.0.0.1",
                actual_port,
                engine.device,
            )
            report("asr", "模型已加载并预热，语音识别接口已监听")
            await wait_for_shutdown()
    finally:
        engine.close()
        listener.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Terminal ASR Worker")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    root = project_root(Path(__file__))
    paths = AppPaths.resolve(root)
    load_env_file(paths.env_file)
    if __name__ != "__main__":
        configure_logging(paths.logs / "asr-worker.log")
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
