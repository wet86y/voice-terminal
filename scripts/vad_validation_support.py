"""VAD test/benchmark backend control（语音活动检测测试/基准后端控制）。"""

from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable


def build_vad_detector(
    *,
    model_dir: Path,
    expected_backend: str,
    device: str = "cpu",
    end_silence_ms: int | None = None,
    native_threads: dict[str, int] | None = None,
    detector_factory: Callable[..., Any] | None = None,
    resource_policy_class: Any | None = None,
):
    """Build a detector with a temporary requested policy and verify the result.

    Arguments（参数）：model_dir（模型目录）、expected_backend（预期后端）、
    device（设备）、end_silence_ms（结束静音阈值）、native_threads（原生线程预算）、
    detector_factory（检测器构造入口）、resource_policy_class（资源策略类型）。
    The ResourcePolicy.load override（资源策略临时覆写）exists only for this
    validation/benchmark call; it never changes production configuration files.
    """
    if expected_backend not in {"funasr", "onnx"}:
        raise ValueError(
            f"unsupported expected VAD backend（预期语音检测后端不受支持）: {expected_backend}"
        )

    from unittest.mock import patch

    if detector_factory is None or resource_policy_class is None:
        from voice_terminal.resources import ResourcePolicy
        from voice_terminal.vad.detector import FsmnVadDetector
        detector_factory = detector_factory or FsmnVadDetector
        resource_policy_class = resource_policy_class or ResourcePolicy

    policy = resource_policy_class(
        native_threads=dict(native_threads or {}),
        vad_backend=expected_backend,
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(resource_policy_class, "load", return_value=policy))
        if expected_backend == "onnx":
            # Do not let a failed ONNX candidate load the heavyweight FunASR fallback.
            stack.enter_context(
                patch(
                    "voice_terminal.asr.model_loader.auto_model_class",
                    side_effect=RuntimeError(
                        "FunASR fallback disabled（ONNX 验证/基准不加载原后端）"
                    ),
                )
            )
        detector = detector_factory(
            model_dir=model_dir,
            device=device,
            end_silence_ms=end_silence_ms,
        )

    actual_backend = getattr(detector, "backend", None)
    if actual_backend != expected_backend:
        load_error = getattr(detector, "load_error", None)
        raise RuntimeError(
            "VAD backend mismatch（语音检测后端不符）："
            f"requested={expected_backend!r}, actual={actual_backend!r}, "
            f"load_error={load_error!r}"
        )
    return detector
