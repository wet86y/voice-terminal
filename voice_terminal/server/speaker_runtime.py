"""Speaker worker, enrollment, and scoring operations for SessionRuntime."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
import base64
import ipaddress
import logging
import sys
import time
import os
import subprocess
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..compute import device_candidates
from ..log_budget import enforce_log_budget, rotate_oversized_log
from ..speaker.client import SpeakerWorkerClient, SpeakerWorkerError
from ..speaker.probe import probe as probe_speaker_worker
from ..speaker.contracts import SpeakerObservation
from ..speaker.enrollment import ENROLLMENT_REQUIRED_SEGMENTS, EnrollmentRejectedError, centroid_of, check_segments, enrollment_prompts
from ..speaker.enrollment_capture import MAX_SEGMENT_SECONDS, MIN_SEGMENT_SECONDS, CaptureControl, EnrollmentCaptureError, capture_segment, speech_ms_for_pcm, voiced_ranges_for_windows
from ..speaker.quality import base_window_quality
from ..speaker.registry import ENROLLMENT_MAX_CLIPPED_RATIO
from ..speaker.engine import WeSpeakerOnnxEngine
from ..speaker.factory import create_speaker_engine
from ..speaker.recording import decode_audio_bytes
from ..speaker.windows import speaker_window_ranges

logger = logging.getLogger(__name__)


class SpeakerRuntimeMixin:
    async def _on_speaker_observation(self, observation) -> None:
        controller = self.controller
        if controller is not None:
            await controller.on_speaker_observation(observation)

    async def _initialize_speaker(self) -> None:
        if (self.config.speaker.mode == "off"
                and not self.config.speaker.owner_only_barge_in):
            self.state.set_service("speaker", "DISABLED")
            return
        if not self.speaker_profile.available:
            logger.info("speaker sidecar enabled without a registered profile")
        await self.ensure_speaker_worker()

    async def _sync_speaker_fingerprint(self, client: SpeakerWorkerClient) -> None:
        """Verify that stored templates belong to the running frontend.

        The sidecar already holds the ONNX session, so ask it for the probe
        embedding instead of loading a second session here. A mismatch marks
        the profile stale and makes ``available`` false; filter（拦截模式）
        closes input admission while observe（仅标记模式）keeps input available.
        """
        self._speaker_warmed = False
        try:
            response = await client.fingerprint(timeout_s=5.0)
        except Exception as exc:
            logger.info("speaker.fingerprint_unavailable error=%s", str(exc)[:200])
            return
        probe = response.get("probe")
        self.speaker_profile.set_runtime_fingerprint(
            {
                "frontend_version": str(response.get("frontend_version") or ""),
                "model_id": str(self.config.speaker.model_id),
            },
            probe if isinstance(probe, list) else None,
        )
        self._speaker_warmed = isinstance(probe, list) and bool(probe)
        if self.speaker_profile.stale:
            logger.warning("speaker.profile_stale reason=%s", self.speaker_profile.stale_reason)

    async def ensure_speaker_worker(self) -> bool:
        """Connect to or launch the local sidecar before enabling speaker modes."""
        async with self._speaker_worker_lock:
            if getattr(self, "_application_stopping", False):
                return False
            if (self.speaker is not None and self.speaker.connected
                    and not self.speaker.degraded
                    and self.state.services.get("speaker") == "READY"):
                # 就绪缓存不能代替本次恢复的健康检查。
                try:
                    await self.speaker.health(timeout_s=1.5)
                    if not self.speaker.degraded:
                        await asyncio.to_thread(self._release_speaker_engine)
                        return True
                except SpeakerWorkerError:
                    pass

            stale = self.speaker
            self.speaker = None
            if stale is not None:
                try:
                    await stale.close()
                except Exception:
                    logger.debug("speaker.stale_client_cleanup_failed", exc_info=True)

            self.state.set_service("speaker", "STARTING")
            managed = bool(os.environ.get("VOICE_APP_ORIGIN") or
                           getattr(self, "_speaker_compute_restarting", False))
            process = self._speaker_worker_process
            # 受管模式只连接本次 Voice 进程实际启动的 sidecar（附属进程），不附着到占用首选端口的未知服务。
            # 受管子进程统一先通过应用层就绪探测，再创建有降级状态的业务客户端。
            connected, last_error = (False, None) if managed else await self._connect_speaker_worker()
            if connected:
                await asyncio.to_thread(self._release_speaker_engine)
                return True

            if not self.config.speaker_model_path.is_file():
                return self._speaker_start_failed(
                    f"声纹模型文件不存在：{self.config.speaker_model_path}"
                )
            if not self._speaker_worker_host_is_local():
                return self._speaker_start_failed(
                    f"远程 Speaker Worker 不可连接：{last_error or self.config.speaker_worker_url}"
                )

            process = self._speaker_worker_process
            if process is None or process.returncode is not None:
                self._speaker_worker_process = None
                try:
                    process = await self._start_managed_speaker_worker()
                except Exception as exc:
                    return self._speaker_start_failed(
                        f"启动 Speaker Worker 失败：{type(exc).__name__}: {exc}"
                    )

            started = time.monotonic()
            startup_timeout = 120.0 if getattr(getattr(self, "paths", None), "separated", False) else 60.0
            deadline = started + startup_timeout
            next_progress = started
            logger.info("声纹服务等待就绪：进程编号=%s，预算秒数=%s", process.pid, startup_timeout)
            while time.monotonic() < deadline:
                if getattr(self, "_application_stopping", False):
                    await self._stop_managed_speaker_worker()
                    return False
                if process.returncode is not None:
                    break
                worker_url = await self._speaker_worker_url_for_process(process)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if worker_url:
                    try:
                        ready, health = await asyncio.wait_for(
                            probe_speaker_worker(worker_url, timeout_s=min(3.0, remaining)), timeout=remaining)
                        if ready:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            connected, current_error = await asyncio.wait_for(
                                self._connect_speaker_worker(worker_url=worker_url), timeout=remaining)
                            if connected:
                                await asyncio.to_thread(self._release_speaker_engine)
                                logger.info("声纹服务启动完成：进程编号=%s，耗时毫秒=%.1f", process.pid,
                                            (time.monotonic() - started) * 1000)
                                return True
                            last_error = current_error or last_error
                        else:
                            last_error = health.get("error") or "声纹服务尚未通过应用层健康检查"
                    except asyncio.TimeoutError:
                        last_error = "声纹服务就绪检查超过剩余启动预算"
                if time.monotonic() >= next_progress:
                    logger.info("声纹服务仍在加载：进程编号=%s，耗时秒数=%.1f，阶段=%s",
                                process.pid, time.monotonic() - started,
                                last_error or "等待工作进程发布监听端点")
                    next_progress = time.monotonic() + 5
                await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))

            if process.returncode is None:
                await self._stop_managed_speaker_worker()
                reason = f"Speaker Worker（声纹工作进程）启动等待超时（{startup_timeout:g} 秒）：{last_error or '无健康检查响应'}"
            else:
                reason = f"Speaker Worker 已退出（exit code {process.returncode}）：{last_error or '无 health 响应'}"
            return self._speaker_start_failed(reason)

    async def switch_speaker_compute_device(self, device: str, *, allow_cuda_fallback: bool = False) -> dict[str, Any]:  # allow_cuda_fallback（回滚旧 GPU 偏好时允许实际设备回退）。
        """Replace the owned CAM++ worker and verify its device with a probe inference."""
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError("device（目标设备模式）必须是 auto、cpu 或 cuda")
        if not hasattr(self, "_speaker_compute_switch_lock"):
            self._speaker_compute_switch_lock = asyncio.Lock()
        async with self._speaker_compute_switch_lock:
            process = self._speaker_worker_process
            if not getattr(self, "_speaker_compute_switch_owned", False):
                if process is None or process.returncode is not None:
                    raise RuntimeError("CAM++ 声纹工作进程由外部管理，拒绝热切换设备。")
                self._speaker_compute_switch_owned = True
            previous_pid = process.pid if process is not None else None
            # 等待及关闭完成前保留客户端所有权，取消后的会话收尾仍能找到它。
            client = self.speaker
            controller = getattr(self, "controller", None)
            if controller is not None:
                active_analysis = getattr(controller, "_speaker_analysis_task", None)
                if active_analysis is not None and not active_analysis.done():
                    await asyncio.wait_for(asyncio.shield(active_analysis), timeout=30)
            if client is not None:
                try:
                    await asyncio.wait_for(client.queue.join(), timeout=10)
                    pending = [*client.pending_embed.values(), *client.pending_finish.values()]
                    if pending:
                        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(item) for item in pending),
                                                              return_exceptions=True), timeout=30)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("等待 CAM++ 原生推理结束超时，未退出旧工作进程。") from exc
            if controller is not None:
                controller.speaker_client = None
            if client is not None:
                from ..asr.worker import AsrWorkerEngine
                # 客户端关闭会先解绑连接；必须等连接真实关闭后再传递取消。
                await AsrWorkerEngine._await_native(asyncio.create_task(client.close()))
            if self.speaker is client:
                self.speaker = None
            await self._stop_managed_speaker_worker()
            self._speaker_warmed = False
            self._speaker_compute_restarting = True
            self._speaker_compute_device_override = device
            try:
                if not await self.ensure_speaker_worker() or self.speaker is None:
                    reason = next((str(item).split(": ", 1)[-1] for item in reversed(self.startup_errors)
                                   if str(item).startswith("speaker:")), "CAM++ 声纹工作进程未就绪。")
                    raise RuntimeError(reason)
                current = await self.speaker.health(timeout_s=5.0)
                actual = str(current.get("device") or "").lower()
                actual = "cuda" if actual.startswith("cuda") else "cpu" if actual.startswith("cpu") else actual
                if device == "cuda" and actual != "cuda" and not allow_cuda_fallback:
                    raise RuntimeError("显式请求 cuda（图形处理器），但 CAM++ 实际运行于 cpu（中央处理器）。")
                if current.get("model_loaded") is not True:
                    raise RuntimeError("CAM++ 模型未完成加载。")
                if not self._speaker_warmed:
                    raise RuntimeError("CAM++ 真实推理预热未通过。")
                new_process = self._speaker_worker_process
                if previous_pid is not None and new_process is not None and new_process.pid == previous_pid:
                    raise RuntimeError("CAM++ 切换后仍连接到旧工作进程。")
                return {**current, "model_warmed": True}
            finally:
                self._speaker_compute_device_override = None
                self._speaker_compute_restarting = False

    def _speaker_worker_host_is_local(self) -> bool:
        host = urlsplit(self.config.speaker_worker_url).hostname or ""
        if host.lower() == "localhost":
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return address.is_loopback or address.is_unspecified

    async def _speaker_worker_url_for_process(self, process: asyncio.subprocess.Process) -> str | None:
        """Resolve the port reported for this child, with PID-owned socket fallback."""
        if os.environ.get("VOICE_APP_ORIGIN"):
            try:
                from ..application.managed import current_endpoint_table
                table = await asyncio.to_thread(current_endpoint_table)
                row = table.get("speaker", {}) if isinstance(table, dict) else {}
                if isinstance(row, dict) and row.get("pid") == process.pid and isinstance(row.get("url"), str):
                    return str(row["url"])
            except Exception:
                pass
        try:
            import psutil
            owner = psutil.Process(process.pid)
            expected_created = getattr(self, "_speaker_worker_created_at", None)
            actual_created = float(owner.create_time())
            if expected_created is not None and abs(actual_created - expected_created) > 2:
                return None
            listeners = [connection for connection in owner.net_connections(kind="tcp")
                         if connection.status == psutil.CONN_LISTEN and connection.laddr]
            addresses = []
            for connection in listeners:
                local = connection.laddr
                host, port = (local.ip, local.port) if hasattr(local, "ip") else (local[0], local[1])
                if host != "127.0.0.1":
                    return None
                addresses.append(port)
            ports = set(addresses)
            if len(ports) == 1:
                return f"ws://127.0.0.1:{next(iter(ports))}/speaker"
        except Exception:
            pass
        return None

    async def _connect_speaker_worker(self, *, worker_url: str | None = None) -> tuple[bool, str | None]:
        client = SpeakerWorkerClient(
            worker_url or self.config.speaker_worker_url,
            on_observation=self._on_speaker_observation,
        )
        try:
            await client.connect()
            health = await client.health(timeout_s=5.0)
            await self._sync_speaker_fingerprint(client)
        except BaseException as exc:
            try:
                await client.close()
            except Exception:
                logger.debug("speaker.client_cleanup_failed", exc_info=True)
            if not isinstance(exc, Exception):
                raise
            return False, f"{type(exc).__name__}: {str(exc)[:200]}"

        self.speaker = client
        self._speaker_worker_health = health
        self._speaker_worker_active_url = worker_url or self.config.speaker_worker_url
        self.state.set_service("speaker", "READY")
        controller = getattr(self, "controller", None)
        if controller is not None:
            # 组件重试同样必须更新实时声纹采样器，避免继续使用已经关闭的旧连接。
            controller.speaker_client = client
            controller.configure_speaker_runtime()
        self.startup_errors = [
            item for item in self.startup_errors if not str(item).startswith("speaker:")
        ]
        logger.info("speaker.worker_ready url=%s", self._speaker_worker_active_url)
        return True, None

    async def _start_managed_speaker_worker(self) -> asyncio.subprocess.Process:
        if getattr(self, "_application_stopping", False):
            raise RuntimeError("应用正在退出，不能启动声纹进程。")
        from ..application.managed import report
        report("speaker", "加载声纹模型")
        log_dir = self.paths.logs
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "speaker-worker.out.log"
        stderr_path = log_dir / "speaker-worker.err.log"
        for stream_path in (stdout_path, stderr_path):
            rotate_oversized_log(stream_path, max_bytes=2 * 1024 * 1024, backups=1)
        enforce_log_budget(log_dir)
        from ..paths import python_command
        selected_device = (getattr(self, "_speaker_compute_device_override", None) or
                           self.config.compute.devices.get("campplus", "cpu"))
        cpu_python = os.environ.get("VOICE_CPU_PYTHON", sys.executable)
        from ..gpu_environment import worker_python
        interpreter = worker_python(self.paths.install_root,
            self.paths.data_root or self.paths.install_root / "data", "voice", cpu_python,
            {"voice_vad": "cpu", "kws": "cpu", "campplus": selected_device})
        command = python_command(interpreter, "voice_terminal.speaker.worker",
                                 "--config", str(self._speaker_worker_config_path), root=self._project_root)
        from ..resources import ResourcePolicy
        child_env = ResourcePolicy.load().environment("speaker", model_dir=self.config.model_dir)
        child_env["VOICE_TENSOR_DEVICE"] = selected_device
        child_env["VOICE_MODEL_DEVICE"] = selected_device
        child_env["VOICE_CPU_PYTHON"] = cpu_python
        if selected_device == "cuda":
            child_env.pop("VOICE_GPU_FALLBACK", None)
        child_env["VOICE_PROCESS_STARTED_AT"] = str(time.time())  # 子进程入口日志的统一时间起点。
        child_env["PYTHONUNBUFFERED"] = "1"
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=child_env,
                cwd=str(self._project_root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" and os.environ.get("VOICE_APP_ORIGIN") else 0,
            )
        self._speaker_worker_process = process
        try:
            import psutil
            self._speaker_worker_created_at = float(psutil.Process(process.pid).create_time())
        except Exception:
            self._speaker_worker_created_at = None
        logger.info(
            "speaker.worker_started pid=%s config=%s",
            process.pid, self._speaker_worker_config_path,
        )
        return process

    def _speaker_start_failed(self, reason: str) -> bool:
        self.speaker = None
        self.state.set_service("speaker", "DEGRADED")
        message = str(reason)[:240]
        self.startup_errors = [
            item for item in self.startup_errors if not str(item).startswith("speaker:")
        ]
        self.startup_errors.append(f"speaker: {message}")
        logger.warning("optional speaker sidecar unavailable: %s", message)
        return False

    async def _stop_managed_speaker_worker(self) -> None:
        process = self._speaker_worker_process
        if process is not None and process.returncode is not None:
            self._speaker_worker_process = None
            self._speaker_worker_created_at = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        # 取消等待时仍保留进程引用，后续关闭可以继续等待或强制回收。
        if self._speaker_worker_process is process:
            self._speaker_worker_process = None
            self._speaker_worker_created_at = None
        logger.info("speaker.worker_stopped pid=%s", process.pid)

    def speaker_status(self) -> dict[str, Any]:
        from ..speaker.audit import parameter_snapshot, parameter_effects
        profile = self.speaker_profile.snapshot()
        if not profile.get("available") and getattr(self.speaker_profile, "root", None) is not None and (self.speaker_profile.root.parent / "registry.json").is_file():
            profile.update(needs_reenrollment=True, stale_reason="speaker_model_changed", warning="旧声纹资料与当前 CAM++（声纹模型）不兼容，已停止使用；请重新录制主人声纹")
        service = self.state.services.get("speaker", "DISABLED")
        # 通信或推理故障必须同步至状态接口；仅模型仍加载不能证明声纹功能可用。
        if ((self.config.speaker.mode != "off" or self.config.speaker.owner_only_barge_in)
                and service == "READY" and (self.speaker is None or not self.speaker.connected
                                           or getattr(self.speaker, "degraded", False))):
            service = "DEGRADED"
            self.state.set_service("speaker", service)
        startup_error = next((item for item in reversed(self.startup_errors)
                              if str(item).startswith("speaker:")), None)
        last_error = self.speaker.last_error if self.speaker is not None else None
        if not last_error:
            last_error = startup_error or ("声纹工作进程不可用，请查看日志并重试组件。" if service == "DEGRADED" else profile.get("warning"))
        owner_selected = bool(self.config.speaker.owner_only_barge_in)
        processor = getattr(self, "audio_processor", None)
        controller = getattr(self, "controller", None)
        protected = getattr(controller, "_barge_in_protected_available", None)
        owner_reason = ""
        owner_ready = False  # 主人打断已完成配置与组件准备；播放验证单独报告。
        if owner_selected:
            if not profile.get("available"):
                owner_reason = "主人声纹资料不可用"
            elif service != "READY" or self.speaker is None or not self.speaker.connected:
                owner_reason = "声纹工作进程不可用"
            elif processor is None or not processor.enabled:
                owner_reason = "回声消除未启用"
            elif not self.config.audio.echo_cancellation or not getattr(processor, "available", True):
                owner_reason = "回声消除组件不可用"
            elif controller is None or getattr(controller, "speaker_sensor", None) is None:
                owner_reason = "声纹采样链未就绪"
            else:
                owner_ready = True
                if not getattr(getattr(controller, "output", None), "active", False):
                    # 待机期间没有反向播放参考音频，不能将正常等待当成组件故障。
                    owner_reason = "已启用，等待播放期间验证回声保护"
                elif not callable(protected) or not protected():
                    owner_ready = False
                    owner_reason = "播放期间回声保护不可用"
        owner_effective = owner_selected and not owner_reason
        rolling = getattr(controller, "_speaker_snapshot", None)
        return {
            "mode": self.config.speaker.mode,
            "effective_settings": parameter_snapshot(self.config.speaker),
            "parameter_effects": parameter_effects(),
            "effective_mode": getattr(controller, "speaker_mode", self.config.speaker.mode),
            "session": controller.speaker_policy.snapshot() if controller is not None and hasattr(controller, "speaker_policy") else {},
            "service": service,
            "worker_connected": bool(self.speaker is not None and self.speaker.connected),
            "model_id": self.config.speaker.model_id,
            "runtime": getattr(self,"_speaker_worker_health",{}).get("runtime", "FunASR / PyTorch（原生语音模型运行时）"),
            "device": getattr(self,"_speaker_worker_health",{}).get("device"),
            "needs_registration": not bool(profile.get("available")),
            "model_path": str(self.config.speaker_model_path),
            "model_present": self.config.speaker_model_path.is_file(),
            "registered": bool(profile.get("available")),
            "filter_allowed": bool(profile.get("available")) and service == "READY"
            and bool(self.speaker is not None and self.speaker.connected),
            "profile": profile,
            "last_error": last_error,
            "test": dict(self._speaker_test_result or {}),
            "anti_spoofing": "unavailable",
            "owner_only_barge_in_selected": owner_selected,
            "owner_only_barge_in_ready": owner_ready,  # 主人打断配置及采样链已就绪。
            "owner_only_barge_in_effective": owner_effective,
            "owner_only_barge_in_reason": owner_reason,
            "rolling_state": (
                {"identity": rolling.identity.value, "quality": rolling.quality,
                 "reason": rolling.reason, "score": rolling.score,
                 "analysis_start_sample": rolling.analysis_start_sample,
                 "analysis_end_sample": rolling.analysis_end_sample,
                 "candidate_start_sample": rolling.candidate_start_sample,
                 "candidate_end_sample": rolling.candidate_end_sample,
                 "age_ms": round(max(0.0, (time.monotonic() - rolling.completed_at) * 1000), 1)}
                if self.config.speaker.mode != "off" and rolling is not None and rolling.is_fresh()
                else {"identity": "unknown", "reason": "disabled_or_stale"}
            ),
        }

    def speaker_profile_snapshot(self) -> dict[str, Any]:
        return self.speaker_profile.snapshot()

    def _speaker_speech_ms(self, pcm: bytes) -> tuple[int, bool]:
        """Voiced duration measured by the same detector the live chain runs.

        Returns ``(speech_ms, measured)``.  ``measured`` is false only when the
        WebRTC VAD is unavailable, in which case a gain-dependent energy
        estimate is used as a clearly-labelled fallback.
        """
        sample_rate = self.config.audio.sample_rate
        processor = self.audio_processor
        if processor is not None and getattr(processor, "available", False):
            try:
                processor.reset()
                try:
                    value, measured = speech_ms_for_pcm(
                        pcm, sample_rate=sample_rate, processor=processor,
                    )
                finally:
                    processor.reset()
                if measured:
                    return value, True
            except Exception:
                logger.debug("speaker.vad_measure_failed", exc_info=True)
        try:
            import numpy as np  # type: ignore
            values = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            if not values.size:
                return 0, False
            return int(np.count_nonzero(np.abs(values) >= 327.0) * 1000 / sample_rate), False
        except Exception:
            return 0, False

    @staticmethod
    def _segment_quality(pcm: bytes, speech_ms: int, *, source: str, measured: bool) -> dict[str, Any]:
        import math
        try:
            import numpy as np  # type: ignore
            values = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean((values / 32768.0) ** 2))) if values.size else 0.0
            clipped = float(np.mean(np.abs(values) >= 32760.0)) if values.size else 0.0
        except Exception:
            rms, clipped = 0.0, 0.0
        return {
            "source": source,
            "duration_ms": int(len(pcm) // 2 * 1000 // 16000),
            "speech_ms": int(speech_ms),
            "speech_measured_by_vad": bool(measured),
            "rms_dbfs": round(20.0 * math.log10(rms), 2) if rms > 1e-9 else -120.0,
            "clipped_ratio": round(clipped, 4),
        }

    @staticmethod
    def _prompt_label(index: int) -> str:
        for item in enrollment_prompts():
            if int(item.get("index", -1)) == int(index):
                return str(item.get("label") or "自定义")
        return "自定义"

    def start_speaker_enrollment(self) -> dict[str, Any]:
        enrollment_id = uuid4().hex
        while len(self._speaker_enrollment) >= 4:
            oldest_id = next(iter(self._speaker_enrollment))
            self.cancel_speaker_enrollment(oldest_id)
        # Enrollment IDs are intentionally short-lived and single-use; raw
        # PCM exists only in this in-memory session until commit/failure.
        self._speaker_enrollment[enrollment_id] = {
            "segments": {}, "created_at": time.monotonic(),
            "config": self.config.speaker,  # 不可变配置快照，登记参数在下一次登记生效。
        }
        from ..speaker.audit import audit, parameter_snapshot
        audit("enrollment_started（登记参数快照）", enrollment_id=enrollment_id,
              parameters=parameter_snapshot(self.config.speaker))
        return {
            "enrollment_id": enrollment_id,
            "required_segments": ENROLLMENT_REQUIRED_SEGMENTS,
            "min_segment_ms": int(MIN_SEGMENT_SECONDS * 1000),
            "max_segment_ms": int(MAX_SEGMENT_SECONDS * 1000),
            # Three *different* continuous sentences.  Same-text enrollment
            # inflates same-text scores without modelling the speaker better,
            # and the live path only ever sees spontaneous speech.
            "prompts": enrollment_prompts(),
            "capture_endpoint": f"/api/v1/speaker/enrollment/{enrollment_id}/capture",
            "guidance": (
                "请用日常说话的速度与音量，在注册时使用的同一设备、同一距离下连续朗读；"
                "三段请朗读不同的句子，不要反复念同一句，也不要用拖长音的持续元音。"
            ),
        }

    def cancel_speaker_enrollment(self, enrollment_id: str) -> bool:
        """Discard one in-progress enrollment and every raw PCM reference."""
        session = self._speaker_enrollment.pop(str(enrollment_id or ""), None)
        for owner, control in getattr(self, "_speaker_captures", {}).values():
            if owner == enrollment_id:
                control.cancel.set()
        if not isinstance(session, dict):
            return False
        segments = session.get("segments")
        if isinstance(segments, dict):
            segments.clear()
        return True

    @asynccontextmanager
    async def _capture_speaker_pcm(self, *, owner: str, seconds: float, capture_id: str | None):
        from .runtime_operations import runtime_operation
        async with runtime_operation(self, "speaker_capture", "声纹现场测试" if owner == "test" else "声纹录制",
                                     phase="recording", pause=True, wait_output=True):
            async with self._capture_speaker_pcm_paused(owner=owner, seconds=seconds, capture_id=capture_id) as result:
                yield result

    @asynccontextmanager
    async def _capture_speaker_pcm_paused(self, *, owner: str, seconds: float, capture_id: str | None):
        """capture_id（采集请求标识）只绑定一个有界流，取消不能复活已删除的会话。"""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 5 <= seconds <= 10:
            raise ValueError("录制时长必须为 5–10 秒")
        if capture_id is not None and (not isinstance(capture_id, str) or not capture_id or len(capture_id) > 80):
            raise ValueError("capture_id（采集请求标识）必须为 1–80 个字符")
        captures = getattr(self, "_speaker_captures", None)
        if captures is None:
            self._speaker_captures = captures = {}
        if captures or self.state.listening:
            raise EnrollmentCaptureError("请停止语音会话或当前录音后重试。", code="SPEAKER_INPUT_BUSY")
        session = self._speaker_enrollment.get(owner) if owner != "test" else None
        if session and (session.get("committing") or time.monotonic() - session.get("created_at", time.monotonic()) > 15 * 60):
            raise ValueError("注册正在保存或已过期，请结束后重试")
        key = capture_id or uuid4().hex
        control = CaptureControl()
        captures[key] = (owner, control)
        capture_task = asyncio.create_task(asyncio.to_thread(
            capture_segment, sample_rate=self.config.audio.sample_rate,
            channels=self.config.audio.channels, input_device=self.config.audio.input_device,
            seconds=seconds, processor=self.audio_processor, control=control,
        ))
        try:
            captured = await asyncio.shield(capture_task)
            if control.cancel.is_set() or (owner != "test" and owner not in self._speaker_enrollment):
                raise EnrollmentCaptureError("本次采集已取消", code="SPEAKER_CAPTURE_CANCELLED")
            if not 5000 <= captured.duration_ms <= 10000:
                raise ValueError("每段录音长度必须在 5 至 10 秒之间")
            yield captured, control
        finally:
            control.cancel.set()
            try:
                # 原生录音线程退出后再归还麦克风；取消协程不会取消线程本身。
                await asyncio.shield(asyncio.gather(capture_task, return_exceptions=True))
            finally:
                captures.pop(key, None)

    async def stop_speaker_capture(self, owner: str, capture_id: str, *, cancel: bool = False) -> dict[str, Any]:
        """cancel（取消本段）丢弃采样；普通停止只接受至少五秒的真实采样。"""
        item = getattr(self, "_speaker_captures", {}).get(capture_id)
        if item is None or item[0] != owner:
            return {"stopped": False}
        control = item[1]
        if not cancel and control.duration_ms < 5000:
            raise ValueError("录满 5 秒后才可提前停止")
        (control.cancel if cancel else control.stop).set()
        closed = await asyncio.to_thread(control.closed.wait, 2)
        if not closed:
            raise ValueError("采集尚未释放，请稍候再试")
        return {"stopped": True, "cancelled": cancel}

    async def close_speaker_captures(self) -> None:
        controls = [item[1] for item in getattr(self, "_speaker_captures", {}).values()]
        for control in controls:
            control.cancel.set()
        for control in controls:
            await asyncio.to_thread(control.closed.wait, 2)

    def add_speaker_enrollment_segment(self, enrollment_id: str, *, index: int,
                                       label: str, encoded: str) -> dict[str, Any]:
        session = self._speaker_enrollment.get(enrollment_id)
        if session is None:
            raise ValueError("录音会话不存在或已过期")
        if time.monotonic() - float(session.get("created_at") or 0) > 15 * 60:
            expired = self._speaker_enrollment.pop(enrollment_id, None)
            if isinstance(expired, dict) and isinstance(expired.get("segments"), dict):
                expired["segments"].clear()
            raise ValueError("录音会话已过期，请重新开始")
        if isinstance(index, bool) or not isinstance(index, int) or index not in {0, 1, 2}:
            raise ValueError("录音段编号必须是 0、1 或 2")
        if not isinstance(encoded, str) or len(encoded) > 4_000_000:
            raise ValueError("录音数据过大")
        try:
            audio = decode_audio_bytes(base64.b64decode(encoded, validate=True))
        except Exception as exc:
            raise ValueError(f"录音数据无效：{type(exc).__name__}") from exc
        duration_ms = len(audio) // 2 * 1000 // self.config.audio.sample_rate
        if duration_ms < 5000 or duration_ms > 10000:
            raise ValueError("每段录音长度必须在 5 至 10 秒之间")
        speech_ms, measured = self._speaker_speech_ms(audio)
        quality = self._segment_quality(audio, speech_ms, source="enrollment_upload",
                                        measured=measured)
        if quality["rms_dbfs"] < -50.0 or speech_ms < 1000:
            raise ValueError("录音音量过低或有效人声不足，请在安静环境中重录")
        if quality["clipped_ratio"] > self.config.speaker.enrollment_max_clipped_ratio:
            raise ValueError("录音存在明显削波，请降低麦克风音量后重录")
        session["segments"][index] = {
            "label": str(label or self._prompt_label(index))[:80],
            "pcm": audio,
            "duration_ms": duration_ms,
            "quality": quality,
        }
        return {"index": index, "duration_ms": duration_ms, "quality": quality,
                "received": len(session["segments"])}

    async def capture_speaker_enrollment_segment(self, enrollment_id: str, *, index: int,
                                                 label: str, seconds: float, capture_id: str | None = None) -> dict[str, Any]:
        """Record one segment from the runtime input device.

        Enrollment used to run through the browser, which selected a different
        device and applied WebRTC AEC/NS/AGC on top of a hand-written resampler.
        Recording here instead means the enrolled templates and the live audio
        travel the same chain.
        """
        session = self._speaker_enrollment.get(enrollment_id)
        if session is None:
            raise ValueError("录音会话不存在或已过期")
        if isinstance(index, bool) or not isinstance(index, int) or index not in {0, 1, 2}:
            raise ValueError("录音段编号必须是 0、1 或 2")
        async with self._capture_speaker_pcm(owner=enrollment_id, seconds=seconds, capture_id=capture_id) as (captured, control):
            speech_ms = captured.speech_ms
            measured = speech_ms > 0
            if not measured:
                speech_ms, measured = self._speaker_speech_ms(captured.pcm)
            if captured.rms_dbfs < -50.0:
                raise ValueError("录音音量过低，请靠近麦克风或在安静环境中重录")
            if captured.clipped_ratio > self.config.speaker.enrollment_max_clipped_ratio:
                raise ValueError("录音存在明显削波，请降低麦克风音量后重录")
            quality = self._segment_quality(captured.pcm, speech_ms, source="enrollment_capture",
                                            measured=measured)
            quality["processor_applied"] = bool(captured.processed)
            session["segments"][index] = {
                "label": str(label or self._prompt_label(index))[:80],
                "pcm": captured.pcm,
                "duration_ms": captured.duration_ms,
                "quality": quality,
            }
            return {"index": index, "duration_ms": captured.duration_ms, "quality": quality,
                    "received": len(session["segments"])}

    def _get_speaker_engine(self) -> WeSpeakerOnnxEngine:
        with getattr(self, "_speaker_engine_lock", nullcontext()):
            engine = self._speaker_engine
            if engine is None or engine.model_path != self.config.speaker_model_path:
                engine = create_speaker_engine(
                    self.config.speaker.model_id, self.config.speaker_model_path,
                    sample_rate=self.config.audio.sample_rate,
                    device=self.config.compute.devices.get("campplus", "cpu"),
                    fallback_device=self.config.compute.fallback_devices.get("campplus", "cpu"),
                )
                self._speaker_engine = engine
            if not engine.loaded:
                engine.load()
            return engine

    def _release_speaker_engine(self) -> None:
        """释放本地缓存引用；正在执行的原生调用仍由线程局部引用持有实例。"""
        with getattr(self, "_speaker_engine_lock", nullcontext()):
            # 不强制清空 session（推理会话），避免取消或边车恢复提前释放原生输入。
            self._speaker_engine = None

    async def _embed_speaker_audio(self, pcm: bytes) -> tuple[float, ...]:
        """Use the sidecar for embeddings; keep a local fallback for setup/offline enrollment."""
        if self.speaker is not None and self.speaker.connected:
            try:
                return await self.speaker.embed_pcm(pcm)
            except SpeakerWorkerError as exc:
                if exc.code == "SPEAKER_AUDIO_QUALITY":
                    raise
                logger.warning("speaker worker embedding failed; trying local fallback", exc_info=True)
        engine = await asyncio.to_thread(self._get_speaker_engine)
        return await asyncio.to_thread(engine.embed_pcm, pcm)

    def _speaker_runtime_fingerprint(self) -> dict[str, Any]:
        probe = self._speaker_runtime_probe()
        return {
            # frontend_version（特征前端版本）必须来自实际引擎，避免跨模型沿用默认指纹。
            "frontend_version": self._speaker_engine.frontend_version if self._speaker_engine else "",
            "model_id": str(self.config.speaker.model_id),
            "embedding_dim": len(probe) if probe else 0,
        }

    def _speaker_runtime_probe(self) -> tuple[float, ...] | None:
        engine = self._speaker_engine
        if engine is None or not engine.loaded:
            return None
        try:
            return engine.probe_embedding()
        except Exception:
            logger.debug("speaker.probe_failed", exc_info=True)
            return None

    async def _speaker_fingerprint(self) -> tuple[dict[str, Any], tuple[float, ...] | None]:
        """Fingerprint to store alongside a new enrollment.

        Prefer the sidecar (it already holds the session) so enrollment does
        not force a second ONNX load; fall back to the local engine that
        produced the embeddings.
        """
        client = self.speaker
        if client is not None and client.connected:
            try:
                response = await client.fingerprint(timeout_s=5.0)
                probe = response.get("probe")
                if isinstance(probe, list) and probe:
                    values = tuple(float(x) for x in probe)
                    return ({
                        "frontend_version": str(response.get("frontend_version") or ""),
                        "model_id": str(self.config.speaker.model_id),
                        "embedding_dim": len(values),
                    }, values)
            except Exception:
                logger.info("speaker.fingerprint_unavailable", exc_info=True)
        if self._speaker_engine is None or not self._speaker_engine.loaded:
            await asyncio.to_thread(self._get_speaker_engine)
        return self._speaker_runtime_fingerprint(), self._speaker_runtime_probe()

    async def commit_speaker_enrollment(self, enrollment_id: str) -> dict[str, Any]:
        session = self._speaker_enrollment.get(enrollment_id)
        if session is None:
            raise ValueError("录音会话不存在或已过期")
        if session.get("committing") or any(owner == enrollment_id for owner, _ in getattr(self, "_speaker_captures", {}).values()):
            raise ValueError("请结束正在进行的录制或保存后重试")
        segments = session.get("segments") if isinstance(session, dict) else {}
        if not isinstance(segments, dict) or len(segments) != ENROLLMENT_REQUIRED_SEGMENTS:
            raise ValueError("请完成三段引导录音")
        session["committing"] = True
        try:
            samples: list[tuple[str, Any, Mapping[str, Any]]] = []
            quality: list[dict[str, Any]] = []
            embeddings: list[tuple[float, ...]] = []
            for index in range(ENROLLMENT_REQUIRED_SEGMENTS):
                item = segments.get(index)
                if not isinstance(item, dict):
                    raise ValueError("录音段不完整")
                pcm = item.get("pcm")
                embedding = await self._embed_speaker_audio(pcm)
                if self._speaker_enrollment.get(enrollment_id) is not session:
                    raise ValueError("本次注册已取消")
                embeddings.append(embedding)
                quality.append(dict(item.get("quality") or {}))
                samples.append((str(item.get("label") or self._prompt_label(index)), embedding, quality[-1]))
            # Refuse an enrollment whose own segments disagree about the
            # speaker.  Storing a template that already fails this check is
            # how a profile ends up silently useless.
            enrollment_config = session.get("config", self.config.speaker)
            detail = check_segments(quality, embeddings, config=enrollment_config)
            for index, item in enumerate(quality):
                item["cosine_to_centroid"] = round(
                    WeSpeakerOnnxEngine.similarity(embeddings[index], centroid_of(embeddings)), 4
                )
            fingerprint, probe = await self._speaker_fingerprint()
            if self._speaker_enrollment.get(enrollment_id) is not session:
                raise ValueError("本次注册已取消")
            # 嵌入提取已在工作进程完成；此处小文件事务与取消在同一循环串行化。
            result = self.speaker_profile.replace_templates(samples, fingerprint=fingerprint, probe=probe)
            self._speaker_enrollment.pop(enrollment_id, None)
            segments.clear()
            if self.speaker_router is not None:
                self.speaker_router.profile.load()
            # 保存新模板后失效旧身份结论；已选择的声纹功能立即恢复采样。
            if self.controller is not None:
                await self.controller._stop_speaker_sensor()
                self.controller.configure_speaker_runtime()
            if self.config.speaker.mode != "off" or self.config.speaker.owner_only_barge_in:
                await self.set_speaker_settings({
                    "mode": self.config.speaker.mode,
                    "owner_only_barge_in": self.config.speaker.owner_only_barge_in,
                })
            await self.bus.publish(self.snapshot())
            merged = dict(result)
            merged["enrollment"] = {
                "min_similarity": detail.get("min_similarity"),
                "min_pair": detail.get("min_pair"),
                "pairwise": detail.get("pairwise"),
                "segments": quality,
            }
            return merged
        except EnrollmentRejectedError:
            raise
        finally:
            session["committing"] = False
            await asyncio.to_thread(self._release_speaker_engine)
            # 失败时允许原会话重录；取消或成功后立即释放全部采样。
            if self._speaker_enrollment.get(enrollment_id) is not session:
                segments.clear() if isinstance(segments, dict) else None

    async def test_speaker_audio(self, encoded: str) -> dict[str, Any]:
        if not self.speaker_profile.available:
            raise ValueError("尚未注册声纹")
        if not isinstance(encoded, str) or len(encoded) > 4_000_000:
            raise ValueError("录音数据过大")
        try:
            audio = decode_audio_bytes(base64.b64decode(encoded, validate=True))
        except Exception as exc:
            raise ValueError(f"录音数据无效：{type(exc).__name__}") from exc
        duration_ms = len(audio) // 2 * 1000 // self.config.audio.sample_rate
        if duration_ms < 500 or duration_ms > 20000:
            raise ValueError("现场测试录音长度必须在 0.5 至 20 秒之间")
        speech_ms, _measured = self._speaker_speech_ms(audio)
        scored = await asyncio.to_thread(self._score_speaker_audio, audio, speech_ms)
        self._speaker_test_result = dict(scored)
        return scored

    def _score_speaker_audio(self, pcm: bytes, speech_ms: int) -> dict[str, Any]:
        """设置页测试结束即释放临时声纹缓存，异常路径也不会保留第二份模型。"""
        try:
            return self._score_speaker_audio_owned(pcm, speech_ms)
        finally:
            self._release_speaker_engine()

    def _score_speaker_audio_owned(self, pcm: bytes, speech_ms: int) -> dict[str, Any]:
        """Aggregate-slide a test clip exactly like the live turn pipeline.

        The previous implementation reported ``max`` over the centroid and
        every labelled template, which is a different (and systematically more
        optimistic) formula than the runtime uses.  A settings-page number that
        cannot be compared with the live decision is worse than no number.
        """
        from ..speaker.state import SpeakerStateMachine

        sample_rate = self.config.audio.sample_rate
        window = max(1, self.config.speaker.window_ms * sample_rate // 1000)
        step = max(1, self.config.speaker.step_ms * sample_rate // 1000)
        total_samples = len(pcm) // 2
        sampled = [
            (start, pcm[start * 2:end * 2])
            for start, end in speaker_window_ranges(total_samples, window, step)
        ]
        engine = self._get_speaker_engine()
        # The same upstream VAD（语音活动检测） frames are intersected with
        # each window, exactly as the live Worker does. The whole-clip duration
        # remains separate so overlapping windows cannot inflate the turn gate.
        window_ranges = [
            (start, start + len(chunk) // 2)
            for start, chunk in sampled
        ]
        try:
            window_voiced_ranges, window_speech_measured = voiced_ranges_for_windows(
                pcm,
                windows=window_ranges,
                sample_rate=sample_rate,
                processor=self.audio_processor,
            )
        except Exception:
            logger.debug("speaker.test_vad_window_measure_failed", exc_info=True)
            window_voiced_ranges, window_speech_measured = [], False
        if not window_speech_measured:
            window_speech_values = [self._speaker_speech_ms(chunk)[0] for _, chunk in sampled]
            window_speech_sample_values: list[int | None] = [None for _ in sampled]
            window_voiced_ranges = [None for _ in sampled]
        else:
            window_speech_sample_values = [
                int(sum(end - start for start, end in ranges))
                for ranges in window_voiced_ranges
            ]
            window_speech_values = [
                int(samples * 1000 // sample_rate)
                for samples in window_speech_sample_values
            ]

        minimum_window_speech_ms = self.speaker_router.state_config.min_speech_ms
        minimum_window_speech_ratio = float(self.config.speaker.min_window_speech_ratio)
        configured_window_ms = max(1, int(self.config.speaker.window_ms))
        window_diagnostics: list[dict[str, Any]] = []
        embeddings: list[tuple[int, int, int, float | None, tuple[float, ...]]] = []
        failed_window_count = 0
        gate_rejected_window_count = 0
        eligible_window_count = 0
        quality_counts: dict[str, int] = {}
        first_embedding_error: str | None = None
        for index, ((start, chunk), window_speech_ms, window_speech_samples) in enumerate(
                zip(sampled, window_speech_values, window_speech_sample_values), start=1):
            quality, fallback_speech_ms = base_window_quality(
                chunk,
                sample_rate=sample_rate,
                min_speech_ms=minimum_window_speech_ms,
                vad_measured=window_speech_measured,
            )
            # When VAD is available its speech duration is authoritative; the
            # amplitude estimate is only a fallback. Low level and clipping
            # checks remain active in either measurement mode.
            if not window_speech_measured:
                window_speech_ms = fallback_speech_ms
            speech_ratio = (
                round(min(1.0, int(window_speech_samples or 0) / max(1, len(chunk) // 2)), 4)
                if window_speech_measured else None
            )
            if quality == "ready":
                if (window_speech_measured
                        and speech_ratio is not None
                        and speech_ratio < minimum_window_speech_ratio):
                    quality = "not_speech"
                elif window_speech_ms < minimum_window_speech_ms:
                    quality = "insufficient_audio"
            quality_counts[quality] = quality_counts.get(quality, 0) + 1
            window_diagnostics.append({
                "index": index,
                "speech_ms": int(window_speech_ms),
                "speech_ratio": speech_ratio,
                "quality": quality,
            })
            if quality != "ready":
                gate_rejected_window_count += 1
                continue
            eligible_window_count += 1
            try:
                embedding = tuple(float(x) for x in engine.embed_pcm(chunk))
                embeddings.append((
                    start, start + len(chunk) // 2, int(window_speech_ms),
                    speech_ratio, embedding,
                ))
            except Exception as exc:
                failed_window_count += 1
                first_embedding_error = first_embedding_error or f"{type(exc).__name__}: {exc}"
                continue
        if not embeddings:
            if failed_window_count:
                logger.warning(
                    "speaker.test_no_usable_embedding candidate_windows=%d failed_windows=%d first_error=%s",
                    len(sampled), failed_window_count, (first_embedding_error or "")[:240],
                )
            reason = (
                "speaker_test_insufficient_window_speech"
                if eligible_window_count == 0
                else "speaker_test_no_usable_window"
            )
            return {
                "state": "non_owner_confirmed",
                "similarity": None,
                "threshold_used": None,
                "speech_ms": 0,
                "measured_speech_ms": int(speech_ms),
                "window_count": 0,
                "candidate_window_count": len(sampled),
                "eligible_window_count": eligible_window_count,
                "window_gate_rejected_count": gate_rejected_window_count,
                "failed_window_count": failed_window_count,
                "window_quality_counts": quality_counts,
                "window_diagnostics": window_diagnostics,
                "window_ms": configured_window_ms,
                "minimum_window_speech_ms": minimum_window_speech_ms,
                "minimum_window_speech_ratio": minimum_window_speech_ratio,
                "minimum_turn_speech_ms": self.speaker_router.state_config.min_turn_speech_ms,
                "frontend_version": engine.frontend_version,
                "reason": reason,
                "anti_spoofing": "unavailable",
            }
        if failed_window_count:
            logger.warning(
                "speaker.test_partial_embedding_failure candidate_windows=%d failed_windows=%d first_error=%s",
                len(sampled), failed_window_count, (first_embedding_error or "")[:240],
            )
        machine = SpeakerStateMachine(config=self.speaker_router.state_config,
                                     scorer=self.speaker_router.score_aggregate)
        machine.reset(turn_id="speaker-test", generation=0)
        for start, end, window_speech_ms, speech_ratio, embedding in embeddings:
            machine.observe(SpeakerObservation(
                turn_id="speaker-test", generation=0,
                window_started_sample=start, window_ended_sample=end,
                similarity=self.speaker_router.score_embedding(embedding),
                quality="ready", speech_ms=window_speech_ms,
                embedding=embedding,
                speech_ratio=speech_ratio,
                turn_speech_ms=int(speech_ms),
            ))
        decision = machine.decision()
        reason = None
        if decision.aggregated_windows == 0:
            reason = "speaker_test_insufficient_window_speech"
        elif decision.speech_ms < self.speaker_router.state_config.min_turn_speech_ms:
            reason = "speaker_test_insufficient_turn_speech"
        elif decision.state.value == "unknown":
            reason = "speaker_score_ambiguous"
        return {
            "state": decision.state.value,
            "similarity": decision.similarity,
            "threshold_used": decision.threshold_used,
            "speech_ms": decision.speech_ms,
            "measured_speech_ms": int(speech_ms),
            "window_count": decision.aggregated_windows,
            "candidate_window_count": len(sampled),
            "eligible_window_count": eligible_window_count,
            "window_gate_rejected_count": gate_rejected_window_count,
            "failed_window_count": failed_window_count,
            "window_quality_counts": quality_counts,
            "window_diagnostics": window_diagnostics,
            "window_ms": configured_window_ms,
            "minimum_window_speech_ms": minimum_window_speech_ms,
            "minimum_window_speech_ratio": minimum_window_speech_ratio,
            "minimum_turn_speech_ms": self.speaker_router.state_config.min_turn_speech_ms,
            "frontend_version": engine.frontend_version,
            "reason": reason,
            "anti_spoofing": "unavailable",
        }

    async def capture_speaker_test(self, *, seconds: float, capture_id: str | None = None) -> dict[str, Any]:
        """Record and score a live test clip through the runtime chain.

        Using the same device and processing as a real turn is what makes the
        reported score comparable with the routing decision; a browser
        recording would answer a different question.
        """
        if not self.speaker_profile.available:
            raise ValueError("尚未注册声纹")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("录制时长必须是数字")
        async with self._capture_speaker_pcm(owner="test", seconds=seconds, capture_id=capture_id) as (captured, control):
            speech_ms = captured.speech_ms
            if speech_ms <= 0:
                speech_ms, _measured = self._speaker_speech_ms(captured.pcm)
            result = await asyncio.to_thread(self._score_speaker_audio, captured.pcm, speech_ms)
            result["duration_ms"] = captured.duration_ms
            result["rms_dbfs"] = captured.rms_dbfs
            if control.cancel.is_set():
                raise EnrollmentCaptureError("本次采集已取消", code="SPEAKER_CAPTURE_CANCELLED")
            self._speaker_test_result = dict(result)
            return result
