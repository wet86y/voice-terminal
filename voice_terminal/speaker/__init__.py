"""Optional speaker-verification sidecar primitives."""

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
