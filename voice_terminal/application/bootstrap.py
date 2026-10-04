"""Bootstrap manifest（启动清单）：在业务 Python 中解析既有有效配置。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from ..resources import bootstrap_resources
from ..paths import AppPaths

if __name__ == "__main__":
    bootstrap_resources("bootstrap")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/local.yaml")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("业务运行环境要求 Python 3.12")
    root = Path.cwd()
    paths = AppPaths.resolve(root)
    from voice_terminal.config import load_config
    from voice_terminal.env import load_env_file
    from voice_terminal.server.product import ProductSettingsStore
    load_env_file(paths.env_file)
    local = Path(args.config)
    config = load_config(root / "config/default.yaml", local if local.is_file() else None)
    config = ProductSettingsStore(path=paths.config / "product-settings.json", config=config).effective_config()
    if paths.separated and not config.kws_keywords_file.is_file():
        from ..kws.keywords import generate_keywords_for
        keywords = config.wake.keywords or (config.wake.keyword,)
        generate_keywords_for(model_dir=config.kws_dir, raw_path=paths.cache / "kws/keywords.raw.txt",
                              output_path=config.kws_keywords_file, entries=list(zip(keywords, config.wake.keyword_labels)))
    from ..dependencies import diagnose
    dependencies = diagnose(check_dependencies=False)
    print(json.dumps({
        "asr_url": config.asr.worker_url,
        "model_dir": str(config.model_dir),  # model_dir（统一模型与下载缓存根目录）。
        "dependency_status": dependencies,  # dependency_status（同一版本清单的检查结果，兼容现用环境）。
        "analysis_url": config.smart_turn.analysis_url,
        "analysis_enabled": config.smart_turn.smart_turn or config.smart_turn.sensevoice,
        "analysis_lanes": [name for name, enabled in (("endpoint", config.smart_turn.smart_turn),
            ("refine", config.smart_turn.sensevoice)) if enabled],
        "worker_timeout_seconds": 240 if paths.separated else 180,  # worker_timeout_seconds（模型进程启动预算）：首次解压的导入与缓存初始化较慢。
        "voice_host": config.server.host, "voice_port": config.server.port,
        "speaker_enabled": config.speaker.mode != "off" or config.speaker.owner_only_barge_in,
        "dsh_enabled": config.dsh.enabled and config.dsh.auto_start,
        "dsh_timeout_seconds": config.dsh.startup_timeout_ms / 1000,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
