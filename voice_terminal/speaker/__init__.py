"""Optional speaker-verification sidecar primitives."""

import sys

# -m（模块入口）会先执行父包；线程预算必须在父包导入数值库前设置。
# 主服务导入声纹工具时，不改变主服务的资源角色。
_arguments = getattr(sys, "orig_argv", ())
if any(value == "-m" and index + 1 < len(_arguments)
       and _arguments[index + 1] == "voice_terminal.speaker.worker"
       for index, value in enumerate(_arguments)):
    from ..resources import bootstrap_resources
    bootstrap_resources("speaker")

from .contracts import SpeakerDecision, SpeakerObservation, SpeakerMode, SpeakerState
from .client import SpeakerWorkerClient, SpeakerWorkerError
from .engine import SpeakerModelError, WeSpeakerOnnxEngine
from .enrollment import ENROLLMENT_PROMPTS, EnrollmentRejectedError, check_segments
from .features import FRONTEND_VERSION, InsufficientAudioError, pcm_to_fbank
from .registry import SpeakerProfileStore
from .state import SpeakerStateMachine
from .worker import SpeakerWorkerEngine
from .rolling import RollingSpeakerIdentity, RollingSpeakerSensor, RollingSpeakerSnapshot

__all__ = [
    "ENROLLMENT_PROMPTS",
    "FRONTEND_VERSION",
    "InsufficientAudioError",
    "EnrollmentRejectedError",
    "SpeakerDecision",
    "SpeakerObservation",
    "SpeakerMode",
    "SpeakerState",
    "SpeakerProfileStore",
    "SpeakerStateMachine",
    "SpeakerWorkerClient",
    "SpeakerWorkerError",
    "SpeakerModelError",
    "WeSpeakerOnnxEngine",
    "SpeakerWorkerEngine",
    "RollingSpeakerIdentity",
    "RollingSpeakerSensor",
    "RollingSpeakerSnapshot",
    "check_segments",
    "pcm_to_fbank",
]
