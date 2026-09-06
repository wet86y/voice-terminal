from dataclasses import dataclass


@dataclass(frozen=True)
class V03Options:
    enabled: bool = False
    decision_mode: str = "balanced"
    balanced_interval_ms: int = 800
    aggressive_interval_ms: int = 400
    silence_ms: int = 5500
    max_requests_per_step: int = 5
    max_context_chars: int = 64000
    trace_limit: int = 200
    output_limit: int = 32

    @classmethod
    def from_mapping(cls, data):
        if not isinstance(data, dict):
            raise ValueError("v03 must be an object")
        result = cls(**data)
        if type(result.enabled) is not bool:
            raise ValueError("v03.enabled must be boolean")
        if result.decision_mode not in {"conservative", "balanced", "aggressive"}:
            raise ValueError("invalid v03.decision_mode")
        for name in cls.__dataclass_fields__:
            if name in {"enabled", "decision_mode"}:
                continue
            if type(getattr(result, name)) is not int or getattr(result, name) <= 0:
                raise ValueError(f"v03.{name} must be a positive integer")
        return result
