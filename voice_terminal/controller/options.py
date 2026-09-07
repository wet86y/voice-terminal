from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class InputOptions:
    enabled: bool = False
    smart_turn: bool = False
    sensevoice: bool = False
    tools: bool = False
    web_search: bool = False
    analysis_url: str = "ws://127.0.0.1:8767/analysis"
    threshold: float = 0.5
    candidate_ms: int = 700
    recheck_ms: int = 400
    smart_timeout_ms: int = 500
    hard_silence_ms: int = 1800
    refine_timeout_ms: int = 3000
    min_segment_chars: int = 6
    max_segment_chars: int = 20
    max_turn_ms: int = 60000
    # Keep only a short amount of already returned speech ahead of the
    # Windows player. The default keeps immediate-send behavior.
    tts_pacing: bool = False
    tts_lead_ms: int = 2000
    # This is the native PCM queue cap, distinct from the legacy text pacing
    # window.  Bidi uses direct text streaming but still needs a bounded player.
    tts_local_buffer_ms: int = 2000
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: (Path(__file__).resolve().parents[2] / "docs",))

    @classmethod
    def from_mapping(cls, data: dict, root: Path) -> "InputOptions":
        if not isinstance(data, dict):
            raise ValueError("smart_turn must be an object")
        values = dict(data)
        if "allowed_roots" in values:
            roots = values["allowed_roots"]
            if not isinstance(roots, list) or not all(isinstance(p, str) for p in roots):
                raise ValueError("tools.allowed_roots must be a list of paths")
            values["allowed_roots"] = tuple((root / p).resolve() for p in roots)
        for key in ("enabled", "smart_turn", "sensevoice", "tools", "web_search", "tts_pacing"):
            if key in values and not isinstance(values[key], bool):
                raise ValueError(f"smart_turn.{key} must be boolean")
        result = cls(**values)
        for key in ("candidate_ms", "recheck_ms", "smart_timeout_ms", "hard_silence_ms", "refine_timeout_ms",
                    "min_segment_chars", "max_segment_chars", "max_turn_ms", "tts_lead_ms",
                    "tts_local_buffer_ms"):
            value = getattr(result, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"smart_turn.{key} must be a positive integer")
        if not 0 <= result.threshold <= 1 or result.hard_silence_ms < result.candidate_ms:
            raise ValueError("invalid Smart Turn thresholds")
        if result.max_turn_ms > 60000 or result.max_segment_chars < result.min_segment_chars:
            raise ValueError("invalid smart_turn bounds")
        return result
