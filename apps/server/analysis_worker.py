import argparse
import logging
from pathlib import Path
import sys
from urllib.parse import urlparse

if __package__ in (None, ""):
    program = next(parent for parent in Path(__file__).resolve().parents
                   if (parent / "pyproject.toml").is_file() and (parent / "voice_terminal").is_dir())
    sys.path.insert(0, str(program))

from voice_terminal.local_layout import project_root

from voice_terminal.startup_logging import initialize_worker_logging, worker_imports_ready, startup_stage
if __name__ == "__main__":
    initialize_worker_logging("analysis", "analysis-worker.log")

from voice_terminal.config import load_config
from voice_terminal.paths import AppPaths
from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app
from voice_terminal.analysis.smart_turn import SmartTurn
from voice_terminal.model_process import ModelProcess
from voice_terminal.logging_setup import configure_logging
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.application.managed import report, report_endpoint, run_uvicorn
from voice_terminal.application.ports import bind_loopback, endpoint_url

if __name__ == "__main__":
    worker_imports_ready("analysis")


def main():
    import uvicorn
    root = project_root(Path(__file__))
    paths = AppPaths.resolve(root)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=paths.config / "local.yaml")
    args = parser.parse_args()
    with startup_stage("读取语音分析有效配置"):
        config = load_config(root / "config/default.yaml", args.config)
        config = ProductSettingsStore(
            path=paths.config / "product-settings.json",
            config=config,
        ).effective_config()
    address = urlparse(config.smart_turn.analysis_url)
    listener, actual_port = bind_loopback(address.port or 8767, host=address.hostname or "127.0.0.1")
    report_endpoint("analysis", endpoint_url("analysis", actual_port))
    if __name__ != "__main__":
        configure_logging(paths.logs / "analysis-worker.log")
    lanes = {}
    def emotion_factory(device=None, *, fallback_device=None, allow_cuda_fallback=False):  # fallback_device（最近成功实际设备）；allow_cuda_fallback（回滚允许回退）。
        selected = device or config.compute.devices.get("emotion", "cpu")
        return ModelProcess("emotion", config.model_dir / "analysis/emotion2vec-plus-base",
            device=selected, fallback_device=(fallback_device or config.compute.fallback_devices.get("emotion", "cpu")),
            require_cuda=device == "cuda" and not allow_cuda_fallback,
            ignore_host_fallback=device == "cuda")

    factories = {
        "emotion": emotion_factory,
        "acoustic": lambda: ModelProcess("acoustic", config.model_dir / "analysis/yamnet"),
    }
    report("analysis", "加载语句结束判断与语音精修模型")
    for name, enabled, model_name, factory in (
        ("endpoint", config.smart_turn.smart_turn, "SmartTurn v3.2（语音结束判断）",
         lambda: SmartTurn(
             config.model_dir / "analysis/smart-turn",
             device=config.compute.devices.get("smart_turn", "cpu"),
             fallback_device=config.compute.fallback_devices.get("smart_turn", "cpu"),
         )),
        ("emotion", config.audio_enhancements.emotion, "emotion2vec+ base（情绪背景）", factories["emotion"]),
        ("acoustic", config.audio_enhancements.acoustic_events, "YAMNet（声学事件）", factories["acoustic"]),
    ):
        lane = AnalysisLane(error=None if enabled else "disabled", model_name=model_name)
        lanes[name] = lane
        try:
            if enabled:
                with startup_stage("加载分析模型：" + model_name):
                    lane.prepare(factory)
                logging.info("analysis.ready operation=%s health=%s", name, lane.health_payload())
        except Exception as exc:
            logging.exception("analysis.load_failed operation=%s", name)
            lane.error = str(exc)
    try:
        run_uvicorn(create_analysis_app(lanes, factories), sock=listener,
            host="127.0.0.1", port=actual_port,
            ws_max_size=3 * 1024 * 1024, timeout_graceful_shutdown=3)
    finally:
        listener.close()


if __name__ == "__main__":
    main()
