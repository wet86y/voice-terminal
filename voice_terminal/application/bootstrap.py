"""Bootstrap manifest（启动清单）：在业务 Python 中解析既有有效配置。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from ..startup_logging import startup_stage, startup_event
from ..resources import bootstrap_resources
from ..paths import AppPaths
from .resource_readiness import inspect_startup_resources, required_model_resources
from ..local_layout import read_portable

if __name__ == "__main__":
    with startup_stage("启动检查资源策略"):
        bootstrap_resources("bootstrap")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/local.yaml")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("业务运行环境要求 Python 3.12")
    root = Path.cwd()
    paths = AppPaths.resolve(root)
    with startup_stage("读取启动配置"):
        from voice_terminal.config import load_config
        from voice_terminal.env import load_env_file
        load_env_file(paths.env_file)
        local = Path(args.config)
        config = load_config(root / "config/default.yaml", local if local.is_file() else None)
    with startup_stage("读取产品有效设置"):
        from voice_terminal.server.product import ProductSettingsStore
        config = ProductSettingsStore(path=paths.config / "product-settings.json", config=config).effective_config()
    resource_requirements = required_model_resources(config)
    resource_issues = inspect_startup_resources(
        root,
        config.model_dir,
        resource_requirements,
        dsh_enabled=config.dsh.enabled and config.dsh.auto_start,
        dsh_runtime=paths.install_root / read_portable(paths.install_root)["dsh"],
        dsh_version=config.dsh.expected_version,
        dsh_commit=config.dsh.expected_commit,
        dsh_bridge=config.dsh.bridge_protocol_version,
    )
    from importlib.util import find_spec
    if paths.separated and find_spec("sherpa_onnx") and (config.kws_dir / "tokens.txt").is_file() and not config.kws_keywords_file.is_file():
        with startup_stage("生成唤醒词文件"):
            from ..kws.keywords import generate_keywords_for
            keywords = config.wake.keywords or (config.wake.keyword,)
            generate_keywords_for(model_dir=config.kws_dir, raw_path=paths.cache / "kws/keywords.raw.txt",
                                  output_path=config.kws_keywords_file, entries=list(zip(keywords, config.wake.keyword_labels)))
    with startup_stage("检查运行依赖版本"):
        from ..dependencies import diagnose
        dependencies = diagnose(check_dependencies=False)
    startup_event("启动检查清单已生成", event="completed")
    print(json.dumps({
        "asr_url": config.asr.worker_url,
        "compute_devices": config.compute.devices,  # compute_devices（逐模型运行设备）：宿主按登记环境选择解释器。
        "model_dir": str(config.model_dir),  # model_dir（统一模型与下载缓存根目录）。
        "dependency_status": dependencies,  # dependency_status（同一版本清单的检查结果，兼容现用环境）。
        "analysis_url": config.smart_turn.analysis_url,
        "speaker_url": config.speaker_worker_url,
        # port=0（系统分配端口）在 DSH（代理执行宿主）完成绑定前没有可发布的首选地址。
        "dsh_url": f"http://{config.dsh.host}:{config.dsh.port}" if config.dsh.port > 0 else "",
        "analysis_enabled": True,
        "analysis_lanes": [name for name, enabled in (("endpoint", config.smart_turn.smart_turn),
            ("emotion", False), ("acoustic", False)) if enabled],
        "startup_gpu_roles": [role for role, enabled, models in (
            ("asr", True, ("paraformer",)),
            ("analysis", config.smart_turn.smart_turn or config.audio_enhancements.emotion,
             tuple(name for name, selected in (("smart_turn", config.smart_turn.smart_turn),
                                               ("emotion", config.audio_enhancements.emotion)) if selected)),
            ("voice", True, ("voice_vad", "kws") +
             (("campplus",) if config.speaker.mode != "off" or config.speaker.owner_only_barge_in else ())),
        ) if enabled and any(config.compute.devices.get(name, "auto") != "cpu" for name in models)],
        # startup_gpu_roles（启动时可能使用显卡的组件）：仅用于并行资源预留，不更改模型设备选择。
        "worker_timeout_seconds": 240 if paths.separated else 180,  # worker_timeout_seconds（模型进程启动预算）：首次解压的导入与缓存初始化较慢。
        "voice_host": config.server.host, "voice_port": config.server.port,
        "speaker_enabled": config.speaker.mode != "off" or config.speaker.owner_only_barge_in,
        "dsh_enabled": config.dsh.enabled and config.dsh.auto_start,
        "dsh_timeout_seconds": config.dsh.startup_timeout_ms / 1000,
        "resource_requirements": resource_requirements,  # resource_requirements（本次配置会加载的固定模型资源编号）。
        "resource_issues": resource_issues,  # resource_issues（本地缺失或大小不符的资源与组件说明）。
        "dsh_expected_version": config.dsh.expected_version,  # dsh_expected_version（锁定的代理版本）。
        "dsh_expected_commit": config.dsh.expected_commit,  # dsh_expected_commit（锁定的代理兼容提交）。
        "dsh_expected_bridge": config.dsh.bridge_protocol_version,  # dsh_expected_bridge（锁定的代理桥接协议）。
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
