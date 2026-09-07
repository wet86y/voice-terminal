from dataclasses import dataclass


@dataclass(frozen=True)
class AgentOptions:
    enabled: bool = False
    decision_mode: str = "balanced"
    balanced_interval_ms: int = 800
    aggressive_interval_ms: int = 400
    silence_ms: int = 5500
    max_requests_per_step: int = 5
    max_context_tokens: int = 524288
    trace_limit: int = 200
    output_limit: int = 32

    @classmethod
    def from_mapping(cls, data):
        if not isinstance(data, dict):
            raise ValueError("agent must be an object")
        result = cls(**data)
        if type(result.enabled) is not bool:
            raise ValueError("agent.enabled must be boolean")
        if result.decision_mode not in {"conservative", "balanced", "aggressive"}:
            raise ValueError("invalid agent.decision_mode")
        for name in cls.__dataclass_fields__:
            if name in {"enabled", "decision_mode"}:
                continue
            if type(getattr(result, name)) is not int or getattr(result, name) <= 0:
                raise ValueError(f"agent.{name} must be a positive integer")
        return result
