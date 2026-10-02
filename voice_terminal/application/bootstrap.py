"""Bootstrap manifest（启动清单）：在业务 Python 中解析既有有效配置。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/local.yaml")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("业务运行环境要求 Python 3.12")
    root = Path.cwd()
    from voice_terminal.config import load_config
    from voice_terminal.env import load_env_file
    from voice_terminal.server.product import ProductSettingsStore
    load_env_file(root / ".env.local")
    local = Path(args.config)
    config = load_config(root / "config/default.yaml", local if local.is_file() else None)
    config = ProductSettingsStore(path=root / "config/product-settings.json", config=config).effective_config()
    print(json.dumps({
        "asr_url": config.asr.worker_url,
        "analysis_url": config.smart_turn.analysis_url,
        "analysis_enabled": config.smart_turn.smart_turn or config.smart_turn.sensevoice,
        "analysis_lanes": [name for name, enabled in (("endpoint", config.smart_turn.smart_turn),
            ("refine", config.smart_turn.sensevoice)) if enabled],
        "voice_host": config.server.host, "voice_port": config.server.port,
        "speaker_enabled": config.speaker.mode != "off" or config.speaker.owner_only_barge_in,
        "dsh_enabled": config.dsh.enabled and config.dsh.auto_start,
        "dsh_timeout_seconds": config.dsh.startup_timeout_ms / 1000,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
