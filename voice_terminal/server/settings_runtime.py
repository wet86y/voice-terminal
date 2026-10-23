"""Product settings（产品设置）：保存后更新运行时，启动期间的修改自动补应用。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
import logging
from typing import Any, Mapping

from .product import apply_settings_to_config
from .runtime_operations import runtime_operation

logger = logging.getLogger(__name__)


async def _finish_compute_write(future):
    """等待设备设置写回真正结束；重复取消不能遗留后台写盘线程。"""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


class ProductSettingsRuntimeMixin:
    async def switch_compute_device(self, payload: Mapping[str, Any], *, ticket=None) -> dict[str, Any]:
        """事务式替换一个受支持模型的计算设备，核验后才提交偏好。"""
        from ..config import COMPUTE_DEVICE_MODES

        if not isinstance(payload, Mapping) or set(payload) != {"model_id", "device"}:
            raise ValueError("计算设备请求必须只包含 model_id（目标模型编号）和 device（目标设备模式）。")
        model_id, device = payload.get("model_id"), payload.get("device")
        if not isinstance(model_id, str) or model_id not in {"paraformer", "emotion", "campplus"}:
            raise ValueError("model_id（目标模型编号）只支持 paraformer、emotion 和 campplus。")
        if not isinstance(device, str) or device not in COMPUTE_DEVICE_MODES:
            raise ValueError("device（目标设备模式）必须是 auto、cpu 或 cuda。")

        label = {"paraformer": "Paraformer", "emotion": "emotion2vec", "campplus": "CAM++"}[model_id]
        target = f"compute_device:{model_id}"
        async with runtime_operation(self, target, f"{label}运行设备切换", pause=True, ticket=ticket) as operation:
            try:
                return await self._switch_compute_device_transaction(model_id, device, operation)
            except BaseException as failure:
                if not isinstance(failure, asyncio.CancelledError):
                    operation.fail(str(failure))
                raise

    async def _switch_compute_device_transaction(self, model_id: str, device: str, operation) -> dict[str, Any]:
        """在一个已登记的运行操作中执行检查、替换、核验、保存和回滚。"""
        import os
        from ..analysis.client import AnalysisClient
        from ..application.resource_readiness import inspect_model_resources
        from ..config import ComputeConfig
        from ..compute import model_hardware_status

        if self.product_settings is None:
            raise ValueError("产品设置存储不可用，未切换计算设备。")
        if not hasattr(self, "_compute_switch_lock"):
            self._compute_switch_lock = asyncio.Lock()

        async with self._compute_switch_lock:
            await operation.update("checking")
            old_compute = self.config.compute
            old_device = old_compute.devices.get(model_id, "cpu")
            requirements = {
                "paraformer": ("Paraformer", ["paraformer-streaming"]),
                "emotion": ("emotion2vec", ["emotion2vec-plus-base"]),
                "campplus": ("CAM++", ["campplus-zh-cn-common"]),
            }
            label, resources = requirements[model_id]
            issues = await asyncio.to_thread(
                inspect_model_resources, self._project_root, self.config.model_dir,
                {label: resources},
            )
            if issues:
                raise ValueError("；".join(issues.values()))

            if device == "cuda":
                from . import websocket as session_module
                hardware = await asyncio.to_thread(session_module.hardware_status, refresh=True)
                hardware = await asyncio.to_thread(model_hardware_status, hardware, self.config, verify=True)
                capability = hardware.get("models", {}).get(model_id, {})
                if not capability.get("gpu_available"):
                    reason = capability.get("reason") or "GPU 后端不可用。"
                    raise ValueError(f"{model_id} 暂不能使用 GPU：{reason}")

            if model_id == "campplus":
                process = getattr(self, "_speaker_worker_process", None)
                if process is None or process.returncode is not None:
                    raise ValueError("CAM++ 声纹工作进程由外部管理或未启动，拒绝热切换。")
            if os.environ.get("VOICE_APP_ORIGIN"):
                owner_flag = "VOICE_APP_ASR_MANAGED" if model_id == "paraformer" else "VOICE_APP_ANALYSIS_MANAGED" if model_id == "emotion" else None
                if owner_flag and os.environ.get(owner_flag) != "1":
                    raise ValueError(f"{model_id} 工作进程由外部管理，拒绝热切换。")

            model_status = await self.compute_status()
            current = model_status.get("models", {}).get(model_id, {})
            if current.get("service") != "READY" or current.get("model_loaded") is not True:
                raise ValueError(f"{model_id} 模型当前未由本应用加载，拒绝热切换。")
            if model_id in {"paraformer", "emotion"} and current.get("model_warmed") is not True:
                raise ValueError(f"{model_id} 模型尚未完成真实推理预热，拒绝热切换。")
            if model_id == "campplus" and current.get("model_warmed") is not True:
                raise ValueError("CAM++ 尚未完成真实推理预热，拒绝热切换。")
            if model_id == "paraformer" and self.config.audio_enhancements.dual_pass:
                dual = model_status.get("auxiliary", {}).get("dual_pass", {})
                if dual.get("service") != "READY" or dual.get("model_warmed") is not True:
                    raise ValueError("已启用的 Paraformer 离线精修模型未就绪，拒绝设备切换。")
                current_device = str(current.get("device") or "").lower()
                dual_device = str(dual.get("device") or "").lower()
                current_device = "cuda" if current_device.startswith("cuda") else "cpu" if current_device.startswith("cpu") else current_device
                dual_device = "cuda" if dual_device.startswith("cuda") else "cpu" if dual_device.startswith("cpu") else dual_device
                if current_device not in {"cpu", "cuda"} or dual_device != current_device:
                    raise ValueError("当前 Paraformer 主路与离线精修设备不一致，拒绝设备切换。")

            current_actual = str(current.get("device") or "").lower()
            current_actual = "cuda" if current_actual.startswith("cuda") else "cpu" if current_actual.startswith("cpu") else current_actual
            if current_actual not in {"cpu", "cuda"}:
                raise ValueError(f"{model_id} 未报告有效的当前运行设备，拒绝热切换。")
            current_request = current.get("requested_device", old_device)
            already_applied = (old_device == device and current.get("service") == "READY"
                               and current.get("model_loaded") is True and current.get("model_warmed") is True
                               and current_request == device
                               and (device == "auto" or current_actual == device))
            if already_applied:
                return self.product_settings.snapshot()["compute"]

            previous_saved = (await asyncio.to_thread(self.product_settings.settings_only))["compute"]
            previous_actual = current_device if model_id == "paraformer" and self.config.audio_enhancements.dual_pass else current.get("device")
            previous_actual = "cuda" if str(previous_actual).startswith("cuda") else "cpu"

            client = AnalysisClient(self.config.smart_turn.analysis_url) if model_id == "emotion" else None

            async def apply(mode: str, *, restoring: bool = False) -> dict[str, Any]:  # restoring（恢复旧模型时放宽旧 GPU 偏好限制）。
                if model_id == "paraformer":
                    if self.asr is None:
                        raise RuntimeError("ASR Worker（语音识别工作进程）不可用。")
                    return await self.asr.configure_compute(mode, allow_cuda_fallback=restoring)
                if model_id == "emotion":
                    result = await client.configure({}, devices={"emotion": mode},
                                                    allow_cuda_fallback={"emotion": restoring})
                    return result.get("emotion", {})
                return await self.switch_speaker_compute_device(mode, allow_cuda_fallback=restoring)

            def verify(status: Mapping[str, Any], requested: str, *, expected_actual: str | None = None,
                       allow_cuda_fallback: bool = False) -> str:  # allow_cuda_fallback（仅允许回滚恢复旧 GPU 偏好）。
                if status.get("status", "READY") != "READY" or status.get("model_loaded") is not True:
                    raise RuntimeError(f"{model_id} 目标模型未加载就绪。")
                if status.get("model_warmed") is not True:
                    raise RuntimeError(f"{model_id} 未通过真实推理预热核验。")
                actual = str(status.get("device") or "").lower()
                actual = "cuda" if actual.startswith("cuda") else "cpu" if actual.startswith("cpu") else actual
                if actual not in {"cpu", "cuda"}:
                    raise RuntimeError(f"{model_id} 未报告有效的实际运行设备。")
                if requested in {"cpu", "cuda"} and actual != requested \
                        and not (allow_cuda_fallback and requested == "cuda" and actual == "cpu"):
                    raise RuntimeError(f"请求 {requested}，但 {model_id} 实际运行于 {actual}。")
                if expected_actual in {"cpu", "cuda"} and actual != expected_actual:
                    raise RuntimeError(f"回滚要求恢复到 {expected_actual}，但 {model_id} 实际运行于 {actual}。")
                if model_id == "paraformer":
                    offline = status.get("offline", {})
                    if self.config.audio_enhancements.dual_pass:
                        offline_device = str(offline.get("device") or "").lower()
                        offline_device = ("cuda" if offline_device.startswith("cuda") else
                                          "cpu" if offline_device.startswith("cpu") else None)
                        if (offline.get("status") != "READY" or offline.get("model_loaded") is not True
                                or offline.get("model_warmed") is not True or offline_device != actual):
                            raise RuntimeError("双路离线精修未就绪，或实际设备与 Paraformer 主路不一致。")
                    elif offline.get("status") != "DISABLED" or offline.get("model_loaded") is True:
                        raise RuntimeError("双路离线精修未启用，但检测到其进程被启动。")
                return actual

            self._compute_switching = True
            try:
                await operation.update("loading")
                target_status = await apply(device)
                await operation.update("verifying")
                actual = verify(target_status, device)
                loop = asyncio.get_running_loop()
                save_future = loop.run_in_executor(None, partial(
                    self.product_settings.save_compute_model, model_id, device, active_device=actual))
                try:
                    saved = await asyncio.shield(save_future)
                except asyncio.CancelledError:
                    try:
                        await _finish_compute_write(save_future)
                    except Exception:
                        # 保存失败由存储层恢复旧值；仍按取消处理模型回滚。
                        logger.debug("计算设备保存取消后写盘失败", exc_info=True)
                    else:
                        restore_future = loop.run_in_executor(None, partial(
                            self.product_settings.save_compute_model, model_id,
                            previous_saved["devices"][model_id],
                            active_device=previous_saved["fallback_devices"][model_id]))
                        try:
                            await _finish_compute_write(restore_future)
                        except Exception as restore_error:
                            operation.prevent_resume()
                            raise RuntimeError(f"设备切换取消后恢复原设置失败，已停止监听：{restore_error}") from restore_error
                    raise
                # 保存期间其他设置可正常应用；只在最新配置上替换本次模型的计算字段。
                current_compute = self.config.compute
                devices = {**current_compute.devices, model_id: device}
                fallbacks = {**current_compute.fallback_devices, model_id: actual}
                self.config = replace(self.config, compute=ComputeConfig(devices, fallbacks))
                return saved
            except BaseException as failure:
                if getattr(self, "_closed", False) or getattr(self, "_application_stopping", False):
                    # 退出流程负责最终回收；写盘收尾失败也不能再启动旧模型。
                    operation.prevent_resume()
                    raise
                await operation.update("rolling_back")
                try:
                    restored = await apply(old_device, restoring=True)
                    verify(restored, old_device, expected_actual=previous_actual, allow_cuda_fallback=True)
                except BaseException as rollback_error:
                    logger.exception("计算设备切换失败且模型恢复失败，保持停止监听")
                    operation.prevent_resume()
                    if isinstance(failure, asyncio.CancelledError):
                        raise RuntimeError(f"计算设备切换取消且恢复原模型失败，已停止监听：{rollback_error}") from failure
                    raise RuntimeError(f"{label}设备切换失败且恢复原模型失败，已停止监听：{rollback_error}") from failure
                raise
            finally:
                self._compute_switching = False

    async def switch_audio_enhancements(self, payload: Mapping[str, Any]) -> dict:
        """双路独立切换不暂停主路；其他增强沿用现有暂停事务。"""
        from ..audio_enhancements import AudioEnhancements
        from ..analysis.client import AnalysisClient
        from ..application.resource_readiness import inspect_model_resources
        if not hasattr(self, "_enhancement_switch_lock"):
            self._enhancement_switch_lock = asyncio.Lock()
        if not hasattr(self, "_compute_switch_lock"):
            self._compute_switch_lock = asyncio.Lock()
        async with self._enhancement_switch_lock, self._compute_switch_lock:
            old = self.config.audio_enhancements
            new = AudioEnhancements.from_mapping({**old.snapshot(), **dict(payload)})
            if old == new:
                return new.snapshot()
            requirements = [resource for field, resource in (
                ("dual_pass", "paraformer-offline"), ("emotion", "emotion2vec-plus-base"),
                ("acoustic_events", "yamnet")) if getattr(new, field) and not getattr(old, field)]
            issues = await asyncio.to_thread(inspect_model_resources, self._project_root,
                                            self.config.model_dir, {"音频增强": requirements} if requirements else {})
            if issues:
                raise ValueError("；".join(issues.values()))
            controller = self.controller
            analysis = AnalysisClient(self.config.smart_turn.analysis_url)
            changed = {"emotion": new.emotion, "acoustic": new.acoustic_events}
            changed = {key: value for key, value in changed.items()
                       if value != (old.emotion if key == "emotion" else old.acoustic_events)}
            dual_changed = new.dual_pass != old.dual_pass
            async def apply(settings):
                if new.dual_pass != old.dual_pass:
                    status = await self.asr.configure_enhancements(settings.dual_pass)
                    offline = status.get("offline", {})
                    if (offline.get("status") != ("READY" if settings.dual_pass else "DISABLED")
                            or settings.dual_pass and offline.get("device") != status.get("device")):
                        raise RuntimeError("双路精修未就绪或实际设备与主识别不一致")
                if changed:
                    selected = {key: settings.emotion if key == "emotion" else settings.acoustic_events for key in changed}
                    status = await analysis.configure(selected)
                    for key, enabled in selected.items():
                        if status.get(key, {}).get("status") != ("READY" if enabled else "DISABLED"):
                            raise RuntimeError(f"音频增强通道 {key} 未达到预期状态")
            async def persist(settings):
                task = asyncio.create_task(asyncio.to_thread(self.product_settings.save_audio_enhancements, settings.snapshot()))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await _finish_compute_write(task)
                    raise
            self._enhancement_resume_requested = True
            labels = {"dual_pass": "双路精修", "emotion": "情绪背景", "acoustic_events": "声学事件"}
            label = "、".join(labels[key] for key in labels if getattr(new, key) != getattr(old, key))
            async with runtime_operation(self, "audio_enhancements", label, pause=bool(changed)) as operation:
                # 只有需要暂停输入的背景通道切换才挂起新的监听请求。
                self._enhancement_switching = bool(changed)
                saving_started = False
                try:
                    await operation.update("loading")
                    if controller is not None and dual_changed:
                        controller.audio_enhancements = new
                        await controller._invalidate_dual_refinements()
                    await apply(new)
                    await operation.update("verifying")
                    if self.product_settings is not None:
                        saving_started = True
                        await persist(new)
                    self.config = replace(self.config, audio_enhancements=new)
                    if controller is not None:
                        controller.audio_enhancements = new
                        if not new.acoustic_events:
                            controller.perception["acoustic"].clear()
                except BaseException as failure:
                    await operation.update("rolling_back")
                    if controller is not None and dual_changed:
                        controller.audio_enhancements = old
                        await controller._invalidate_dual_refinements()
                    try:
                        await apply(old)
                        if saving_started:
                            await persist(old)
                    except Exception as rollback_error:
                        logger.exception("音频增强恢复失败")
                        if changed:
                            operation.prevent_resume()
                        if not isinstance(failure, asyncio.CancelledError):
                            raise RuntimeError(f"音频增强切换失败且恢复原模型失败：{rollback_error}") from failure
                    raise
                finally:
                    self._enhancement_switching = False
                    if not self._enhancement_resume_requested:
                        operation.prevent_resume()
            return new.snapshot()

    async def apply_product_settings(self, settings: Mapping[str, Any], *, label: str = "运行设置") -> bool:
        """apply_product_settings（即时应用）：保留当前输入、播放与会话历史。"""
        async with runtime_operation(self, "product_settings", label) as operation:
            applied = await self._apply_product_settings(settings)
            if not applied:
                operation.fail("运行设置应用失败")
            return applied

    async def _apply_product_settings(self, settings: Mapping[str, Any]) -> bool:
        async with self._session_lock:
            async with self._product_settings_lock:
                self.stage_product_settings(settings)
                if self.application_initializing:
                    return False
                applied = await self._apply_pending_product_settings()
                await self.bus.publish(self.snapshot())
                return applied

    async def _apply_pending_product_settings(self) -> bool:
        """启动汇合时或保存后应用；不再要求停止监听或开启一轮对话。"""
        pending = self._pending_product_settings
        if pending is None:
            return True
        previous = self.config
        # compute（模型运行设备）有独立的应用重启契约，不能被其他设置顺带加载。
        live_sections = {key: pending[key] for key in ("wake", "silence", "quiet_hours", "speaker", "screen_capture") if key in pending}
        next_config = replace(apply_settings_to_config(previous, live_sections), compute=previous.compute)
        wake_changed = next_config.wake != previous.wake
        old_worker = self.kws
        candidate = old_worker
        old_compute = dict(self._compute_models.get("kws", {}))
        try:
            # 旧工作线程在新模型成功加载前继续服务；原生构造在后台执行。
            if wake_changed:
                candidate = (await self._load_measured_model("kws", self._build_kws_worker, next_config)
                             if next_config.wake.enabled else None)
            if (wake_changed and not next_config.wake.enabled and self.controller is not None
                    and self.state.listening and self.state.input_owner == "kws"):
                if not await self.controller._ensure_asr_ready(preserve_listening=True):
                    raise ValueError("语音识别尚未就绪，无法从关键词待机切换为直接监听。")
            if next_config.speaker != previous.speaker:
                await self.set_speaker_settings(vars(next_config.speaker))
            self.history.screen_capture.configure(next_config.screen_capture)
            if not next_config.screen_capture.enabled and self.controller is not None:
                episode = getattr(self.controller, "episode", None)
                if episode is not None:
                    episode.invalidate_screen_capture()
            self.config = next_config
            self.quiet_policy.config = next_config.quiet_hours
            self.state.wake_always_on = bool(next_config.wake.enabled)
            self.kws = candidate
            if self.controller is not None:
                self.controller.set_agent_options(next_config.agent)
                if wake_changed:
                    await self.controller.set_wake_settings(candidate, next_config.wake)
            if wake_changed:
                self.state.set_service("kws", ("RUNNING" if self.state.listening and self.state.input_owner == "kws"
                                               else "READY") if candidate is not None else "DISABLED")
                if candidate is None:
                    self._compute_models["kws"] = {"requested_device": previous.compute.devices.get("kws", "cpu"),
                                                   "device": None, "service": "DISABLED", "load_error": None}
            if wake_changed and old_worker is not None:
                try:
                    await asyncio.to_thread(old_worker.close)
                except Exception:
                    logger.warning("product.old_kws_cleanup_failed", exc_info=True)
            if self._pending_product_settings is pending:
                self._pending_product_settings = None
            self.startup_errors = [item for item in self.startup_errors if not str(item).startswith("product_settings:")]
            return True
        except Exception as exc:
            logger.exception("product.settings_apply_failed")
            self.history.screen_capture.configure(previous.screen_capture)
            if candidate is not None and candidate is not old_worker:
                await asyncio.to_thread(candidate.close)
            self.config = previous
            self.quiet_policy.config = previous.quiet_hours
            self.kws = old_worker
            self._compute_models["kws"] = old_compute
            self.state.wake_always_on = bool(previous.wake.enabled)
            if self.controller is not None:
                self.controller.set_agent_options(previous.agent)
                if wake_changed:
                    await self.controller.set_wake_settings(old_worker, previous.wake)
            self.startup_errors = [item for item in self.startup_errors if not str(item).startswith("product_settings:")]
            self.startup_errors.append(f"product_settings: {str(exc)[:300]}")
            return False

    async def refresh_custom_instruction_settings(self) -> None:
        """refresh_custom_instruction_settings（刷新自定义指令）：资料文档仅在新会话载入。"""
        store = self.product_settings
        if store is None:
            return
        instructions = store.settings_only().get("custom_instructions", [])
        async with self._session_lock:
            self._instruction_snapshot = [dict(item) for item in instructions]
            # 保留当前会话已冻结的用户资料；人格文档由新会话从磁盘读取。
            self.shadow_context.set_product_context(self._shadow_user_profile, self._instruction_snapshot)
            episode = getattr(self.controller, "episode", None)
            if episode is not None:
                episode.refresh_custom_instructions(self._instruction_snapshot)
            self._persist_voice_identity()
            await self.bus.publish(self.snapshot())
