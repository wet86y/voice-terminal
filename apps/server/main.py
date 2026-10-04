from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from voice_terminal.resources import bootstrap_resources
if __name__ == "__main__":
    bootstrap_resources("voice")

import uvicorn  # type: ignore

from voice_terminal.config import PersonaConfig, load_config
from voice_terminal.paths import AppPaths
from voice_terminal.application.managed import run_uvicorn, report as application_report
from voice_terminal.env import load_env_file
from voice_terminal.logging_setup import configure_logging
from voice_terminal.model_registry import ModelRegistryService, WindowsCredentialVault
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app


def _runtime_build_info(root: Path, effective_config: Path) -> dict[str, str | int]:
    """Small, non-secret identity payload for logs and ``/health``.

    A desktop workflow often edits source while an old Uvicorn process still
    owns the port.  Without a process/build fingerprint the Web UI can look
    current through Vite HMR while talking to an obsolete backend.
    """

    revision = "unknown"
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=2, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" and os.environ.get("VOICE_APP_ORIGIN") else 0,
        )
        revision = completed.stdout.strip() or revision
    except (OSError, subprocess.SubprocessError):
        pass
    sources = {
        "episode_source_mtime_ns": root / "voice_terminal" / "agent" / "episode.py",
        "episode_controller_source_mtime_ns": root / "voice_terminal" / "controller" / "episode_controller.py",
        "context_source_mtime_ns": root / "voice_terminal" / "session" / "context.py",
    }
    info: dict[str, str | int] = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_revision": revision,
        "config_path": str(effective_config.resolve()),
    }
    info.update({key: path.stat().st_mtime_ns if path.is_file() else 0
                 for key, path in sources.items()})
    return info


def build_application(config_path: Path | None = None) -> tuple[object, SessionRuntime]:
    root = Path(__file__).resolve().parents[2]
    paths = AppPaths.resolve(root)
    default_path = root / "config" / "default.yaml"
    local_path = config_path or paths.config / "local.yaml"
    effective_config = local_path if local_path.is_file() else default_path
    yaml_config = load_config(default_path, local_path if local_path.is_file() else None)
    # Product Persona is a fixed, allowlisted sidecar surface.  Keep the
    # runtime loader aligned with the API's paths even if legacy YAML carries
    # a custom Persona directory or filename.
    yaml_config = replace(
        yaml_config,
        persona=PersonaConfig(directory=paths.persona),
    )
    product_settings = ProductSettingsStore(
        path=paths.config / "product-settings.json",
        config=yaml_config,
    )
    config = product_settings.effective_config()
    runtime = SessionRuntime(
        config,
        product_settings=product_settings,
        speaker_worker_config_path=effective_config,
    )
    runtime.build_info = _runtime_build_info(root, effective_config)
    model_registry = ModelRegistryService(
        config=config,
        registry_path=paths.config / "model-registry.json",
        vault=WindowsCredentialVault(),
        apply_callback=runtime.restart_model_services,
    )

    @asynccontextmanager
    async def lifespan(_app):
        # Restore the last explicitly committed product assignment before any
        # audio, KWS, LLM, Shadow or TTS component is constructed.  A stale or
        # unavailable assignment is non-fatal: YAML remains the safe fallback
        # and the warning is visible through /health and Product snapshot.
        try:
            assignment = await model_registry.runtime_assignment_payload()
            if assignment is not None:
                restored = await runtime.restore_model_services(assignment)
                if not restored.get("ok"):
                    runtime.startup_errors.append(
                        "model_assignment_restore: "
                        + str(restored.get("message") or "恢复失败")[:300]
                    )
                else:
                    model_registry.config = runtime.config
        except Exception as exc:
            runtime.startup_errors.append(
                f"model_assignment_restore: {type(exc).__name__}: {str(exc)[:260]}"
            )
        # Populate the product snapshot with the current runtime projection
        # and local model inventory before the first Web UI request.  This is
        # filesystem/config metadata only; it never performs provider
        # discovery or touches credentials.
        try:
            await model_registry.scan()
        except Exception as exc:
            runtime.startup_errors.append(
                f"model_registry_scan: {type(exc).__name__}: {str(exc)[:260]}"
            )
        try:
            await runtime.initialize()
        except BaseException:
            await runtime.close_background_services()
            await runtime.close()
            await runtime.dsh.close()
            raise
        try:
            compute_status = await runtime.compute_status()
            restored_devices = await asyncio.to_thread(
                product_settings.revert_failed_compute_devices,
                compute_status.get("models", {}),
            )
            if restored_devices:
                runtime.startup_errors.append(
                    "compute_device_restore: CUDA 加载失败后已恢复为上次成功设备（"
                    + ", ".join(f"{name}={device}" for name, device in sorted(restored_devices.items()))
                    + ")."
                )
        except Exception as exc:
            runtime.startup_errors.append(
                f"compute_status_startup: {type(exc).__name__}: {str(exc)[:240]}"
            )
        if not os.environ.get("VOICE_APP_ORIGIN"):
            runtime.dsh.begin_start()
        application_report("voice", "初始化完成，等待服务监听")
        try:
            yield
        finally:
            try:
                await runtime.close_background_services()
            finally:
                try:
                    await runtime.close()
                finally:
                    await runtime.dsh.close()

    app = create_app(
        runtime,
        lifespan=lifespan,
        model_registry=model_registry,
        product_settings=product_settings,
    )

    return app, runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Terminal Session Server")
    parser.add_argument("--config", type=Path, default=None, help="本地 YAML 配置路径")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    paths = AppPaths.resolve(root)
    load_env_file(paths.env_file)
    configure_logging(paths.logs / "voice-server.log")
    app, runtime = build_application(args.config)
    logging.getLogger(__name__).info("server.runtime_build %s", runtime.build_info)
    host = args.host or runtime.config.server.host
    port = args.port or runtime.config.server.port
    run_uvicorn(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=10,
        on_shutdown=runtime.begin_application_shutdown)


if __name__ == "__main__":
    main()
