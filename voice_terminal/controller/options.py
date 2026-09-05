from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class V02Options:
    enabled: bool = False
    smart_turn: bool = False
    sensevoice: bool = False
    predictions: bool = False
    tools: bool = False
    analysis_url: str = "ws://127.0.0.1:8767/analysis"
    threshold: float = 0.5
    candidate_ms: int = 700
    recheck_ms: int = 400
    smart_timeout_ms: int = 500
    hard_silence_ms: int = 1800
    refine_timeout_ms: int = 3000
    prediction_interval_ms: int = 800
    min_segment_chars: int = 6
    max_segment_chars: int = 20
    max_turn_ms: int = 60000
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: (Path(__file__).resolve().parents[2] / "docs",))

    @classmethod
    def from_mapping(cls, data: dict, root: Path) -> "V02Options":
        if not isinstance(data, dict):
            raise ValueError("v02 must be an object")
        values = dict(data)
        if "allowed_roots" in values:
            roots = values["allowed_roots"]
            if not isinstance(roots, list) or not all(isinstance(p, str) for p in roots):
                raise ValueError("v02.allowed_roots must be a list of paths")
            values["allowed_roots"] = tuple((root / p).resolve() for p in roots)
        for key in ("enabled", "smart_turn", "sensevoice", "predictions", "tools"):
            if key in values and not isinstance(values[key], bool):
                raise ValueError(f"v02.{key} must be boolean")
        result = cls(**values)
        for key in ("candidate_ms", "recheck_ms", "smart_timeout_ms", "hard_silence_ms", "refine_timeout_ms",
                    "prediction_interval_ms", "min_segment_chars", "max_segment_chars", "max_turn_ms"):
            value = getattr(result, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"v02.{key} must be a positive integer")
        if not 0 <= result.threshold <= 1 or result.hard_silence_ms < result.candidate_ms:
            raise ValueError("invalid Smart Turn thresholds")
        if result.max_turn_ms > 60000 or result.max_segment_chars < result.min_segment_chars:
            raise ValueError("invalid v02 bounds")
        return result
