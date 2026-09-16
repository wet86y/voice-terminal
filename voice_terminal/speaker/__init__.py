"""Optional speaker-verification sidecar primitives."""

from .contracts import SpeakerDecision, SpeakerObservation, SpeakerMode, SpeakerState
from .client import SpeakerWorkerClient, SpeakerWorkerError
from .engine import SpeakerModelError, WeSpeakerOnnxEngine
from .registry import SpeakerProfileStore
from .state import SpeakerStateMachine
from .worker import SpeakerWorkerEngine

__all__ = [
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
]
