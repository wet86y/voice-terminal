from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from dataclasses import replace
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import load_config
from .kws.keywords import validate_keywords_file
from .model_setup import validate_models
from .server.product import ProductSettingsStore


@dataclass
class Check:
    name: str
    status: str
    message: str


def _check(name: str, ok: bool, message: str, *, warning: bool = False) -> Check:
    return Check(name, "PASS" if ok else ("WARN" if warning else "FAIL"), message)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def run_checks(config_path: Path, *, online: bool = False, model_dir: Path | None = None) -> list[Check]:
    root = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
    default_path = root / "config" / "default.yaml"
    config = load_config(default_path, config_path if config_path.is_file() else None)
    if model_dir is not None:
        config = replace(config, model_dir=model_dir.resolve())
    # The product UI keeps wake settings in a sidecar so that they can be
    # edited without rewriting YAML.  Diagnostics must inspect the same
    # effective runtime config as Voice Server, otherwise a custom (including
    # Chinese) keyword is reported against the stale YAML/default label.
    product_settings = ProductSettingsStore(
        path=config.paths.config / "product-settings.json",
        config=config,
    )
    config = product_settings.effective_config()
    checks: list[Check] = []
    checks.append(_check("python", sys.version_info[:2] == (3, 12), sys.version.split()[0]))
    from .dependencies import diagnose
    dependencies = diagnose()
    checks.append(_check("dependencies:lock", not dependencies["mismatches"],
        json.dumps(dependencies["mismatches"], ensure_ascii=False) or "运行依赖与统一版本锁一致", warning=True))
    checks.append(_check("dependencies:consistency", dependencies["dependency_check_ok"],
        dependencies["dependency_check"], warning=True))

    for module in ("numpy", "yaml", "websockets", "fastapi", "uvicorn", "httpx"):
        try:
            __import__(module)
            checks.append(_check(f"python:{module}", True, "installed"))
        except ImportError:
            checks.append(_check(f"python:{module}", False, "missing"))

    try:
        import torch  # type: ignore

        cuda = bool(torch.cuda.is_available())
        gpu = torch.cuda.get_device_name(0) if cuda else "CPU fallback"
        checks.append(_check("torch", True, f"{torch.__version__}; cuda={cuda}; device={gpu}"))
    except ImportError:
        checks.append(_check("torch", False, "missing"))

    try:
        import sounddevice as sd  # type: ignore

        devices = list(sd.query_devices())
        inputs = [str(item.get("name")) for item in devices if item.get("max_input_channels", 0) > 0]
        outputs = [str(item.get("name")) for item in devices if item.get("max_output_channels", 0) > 0]
        checks.append(_check("audio:input", bool(inputs), ", ".join(inputs[:3]) or "no input device"))
        checks.append(_check("audio:output", bool(outputs), ", ".join(outputs[:3]) or "no output device"))
    except Exception as exc:
        checks.append(_check("audio", False, str(exc)))

    # WeSpeaker is an optional sidecar.  Core audio/ASR readiness must not be
    # reported as failed merely because its model has not been installed.
    model_errors = validate_models(config.model_dir, require_speaker=False)
    checks.append(_check("models", not model_errors, "ready" if not model_errors else "; ".join(model_errors)))
    from .model_setup import _is_wespeaker_model_complete
    speaker_model = config.model_dir / "speaker" / config.speaker.model_id
    speaker_ready = _is_wespeaker_model_complete(speaker_model)
    if config.speaker.mode == "off":
        checks.append(_check("models:speaker", True, "optional sidecar disabled"))
    else:
        checks.append(_check(
            "models:speaker", speaker_ready,
            str(speaker_model) if speaker_ready else f"optional model unavailable: {speaker_model}",
            warning=True,
        ))
        try:
            import onnxruntime  # type: ignore
            checks.append(_check("python:onnxruntime", True, "installed"))
        except ImportError:
            checks.append(_check("python:onnxruntime", False, "missing; speaker sidecar will degrade", warning=True))
    if config.smart_turn.enabled:
        from .analysis.model_setup import validate
        analysis_errors = validate(config.model_dir)
        checks.append(_check("models:analysis", not analysis_errors, "; ".join(analysis_errors) or "pinned assets verified", warning=True))
        for module in ("onnxruntime", "transformers"):
            try:
                __import__(module)
                checks.append(_check(f"python:{module}", True, "installed"))
            except ImportError:
                checks.append(_check(f"python:{module}", False, "missing; analysis will degrade", warning=True))
    wake_name = "/".join(config.wake.keywords) or config.wake.keyword
    if config.wake.enabled:
        try:
            validate_keywords_file(config.kws_keywords_file, config.wake.keyword_labels)
            checks.append(_check(f"kws:{wake_name}", True, str(config.kws_keywords_file)))
        except Exception as exc:
            checks.append(_check(f"kws:{wake_name}", False, str(exc)))
    else:
        checks.append(_check(f"kws:{wake_name}", True, "disabled by wake.enabled=false"))

    voice_port_busy = _port_in_use(config.server.host, config.server.port)
    checks.append(
        _check(
            "port:voice-server",
            not voice_port_busy,
            f"{config.server.host}:{config.server.port} is available"
            if not voice_port_busy
            else f"{config.server.host}:{config.server.port} is already listening",
            warning=True,
        )
    )
    worker_port_busy = _port_in_use(config.asr_worker.host, config.asr_worker.port)
    checks.append(
        _check(
            "port:asr-worker",
            True,
            f"{config.asr_worker.host}:{config.asr_worker.port} is listening"
            if worker_port_busy
            else f"{config.asr_worker.host}:{config.asr_worker.port} is available; start.ps1 will launch the worker",
        )
    )

    for env_name in (config.llm.api_key_env, config.tts.api_key_env):
        configured = bool(os.environ.get(env_name, "").strip())
        checks.append(_check(f"secret:{env_name}", configured, "configured" if configured else "missing"))

    if online:
        checks.extend(asyncio.run(_online_checks(config)))
    return checks


async def _online_checks(config) -> list[Check]:
    checks: list[Check] = []
    try:
        from .providers.deepseek import DeepSeekClient

        client = DeepSeekClient(
            base_url=config.llm.base_url,
            model=config.llm.model,
            api_key=config.api_key(config.llm.api_key_env),
            provider=config.llm.provider,
            api_key_env=config.llm.api_key_env,
            session_id="doctor-check" if config.llm.provider == "opencode_go" else None,
            timeout_ms=config.llm.timeout_ms,
            thinking=config.llm.thinking,
            reasoning_effort=config.llm.reasoning_effort,
        )
        await client.check()
        checks.append(_check(f"online:{config.llm.provider}", True, f"{config.llm.base_url}; model={config.llm.model}"))
    except Exception as exc:
        checks.append(_check(f"online:{config.llm.provider}", False, str(exc)))

    try:
        from .providers.minimax_tts import MiniMaxTtsClient

        client = MiniMaxTtsClient(
            base_url=config.tts.base_url,
            websocket_path=config.tts.websocket_path,
            model=config.tts.model,
            api_key=config.api_key(config.tts.api_key_env),
            output_format=config.tts.output_format,
            sample_rate=config.tts.sample_rate,
            voice_id=config.tts.voice_id,
            timeout_ms=config.tts.timeout_ms,
            transport=config.tts.transport,
            scheduling=config.tts.scheduling,
            subtitle_enable=config.tts.subtitle_enable,
            subtitle_type=config.tts.subtitle_type,
            audio_cache_max_bytes=config.tts.audio_cache_max_bytes,
            cancel_timeout_ms=config.tts.cancel_timeout_ms,
            keepalive_seconds=config.tts.keepalive_seconds,
        )
        await client.check()
        checks.append(_check("online:minimax", True, f"{client.ws_url}; model={config.tts.model}"))
    except Exception as exc:
        checks.append(_check("online:minimax", False, str(exc)))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice Terminal environment doctor")
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    # Windows PowerShell may expose a legacy GBK stdout codec.  Audio device
    # names can contain symbols such as ``®``; replace unsupported console
    # characters instead of making an otherwise useful diagnostic crash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    checks = run_checks(args.config, online=args.online, model_dir=args.model_dir)
    if args.as_json:
        print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"{item.status:>4}  {item.name}: {item.message}")
    return 0 if not any(item.status == "FAIL" for item in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
