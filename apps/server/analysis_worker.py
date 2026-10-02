import argparse
import logging
from pathlib import Path
from urllib.parse import urlparse

from voice_terminal.config import load_config
from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app
from voice_terminal.analysis.smart_turn import SmartTurn
from voice_terminal.analysis.sensevoice import SenseVoice
from voice_terminal.logging_setup import configure_logging
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.application.managed import report, run_uvicorn


def main():
    import uvicorn
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=root / "config/local.yaml")
    args = parser.parse_args()
    config = load_config(root / "config/default.yaml", args.config)
    config = ProductSettingsStore(
        path=root / "config" / "product-settings.json",
        config=config,
    ).effective_config()
    configure_logging(root / "logs/analysis-worker.log")
    lanes = {}
    report("analysis", "加载语句结束判断与语音精修模型")
    for name, enabled, model_name, factory in (
        ("endpoint", config.smart_turn.smart_turn, "smart-turn-v3.2-cpu.onnx",
         lambda: SmartTurn(
             config.model_dir / "analysis/smart-turn",
             device=config.compute.devices.get("smart_turn", "cpu"),
             fallback_device=config.compute.fallback_devices.get("smart_turn", "cpu"),
         )),
        ("refine", config.smart_turn.sensevoice, "SenseVoiceSmall",
         lambda: SenseVoice(
             config.model_dir / "analysis/sensevoice",
             config.vad_model_dir,
             device=config.compute.devices.get("sensevoice", "cpu"),
             fallback_device=config.compute.fallback_devices.get("sensevoice", "cpu"),
         )),
    ):
        try:
            lanes[name] = AnalysisLane(factory() if enabled else None, None if enabled else "disabled",
                                        model_name=model_name)
        except Exception as exc:
            logging.exception("analysis.load_failed operation=%s", name)
            lanes[name] = AnalysisLane(error=str(exc), model_name=model_name)
    address = urlparse(config.smart_turn.analysis_url)
    run_uvicorn(create_analysis_app(lanes), host=address.hostname or "127.0.0.1", port=address.port or 8767,
                ws_max_size=3 * 1024 * 1024, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
