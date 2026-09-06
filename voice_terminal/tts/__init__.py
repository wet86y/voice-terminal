"""Reusable text-to-speech scheduling policies.

The provider protocol and the policy that decides when text may be sent are
kept separate.  This lets a streaming provider such as MiniMax Bidi send
text immediately while older providers can still use the playback-window
policy.
"""

from .scheduling import (
    DirectStreamingPolicy,
    PlaybackProgress,
    PlaybackWindowPolicy,
    TtsSchedulingError,
)

__all__ = [
    "DirectStreamingPolicy",
    "PlaybackProgress",
    "PlaybackWindowPolicy",
    "TtsSchedulingError",
]
from .alignment import DeliveryResolver, SpeechAlignment
from .scheduling import (
    DirectStreamingPolicy,
    PlaybackProgress,
    PlaybackWindowPolicy,
    TtsGenerationScheduler,
    TtsSchedulingError,
    TtsSchedulingState,
)
from .text_merger import BidiTextMerger

__all__ = [
    "BidiTextMerger",
    "DeliveryResolver",
    "DirectStreamingPolicy",
    "PlaybackProgress",
    "PlaybackWindowPolicy",
    "SpeechAlignment",
    "TtsGenerationScheduler",
    "TtsSchedulingError",
    "TtsSchedulingState",
]
