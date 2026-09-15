from dataclasses import dataclass


@dataclass(frozen=True)
class AgentOptions:
    enabled: bool = False
    max_context_tokens: int = 524288
    trace_limit: int = 200
    output_limit: int = 32
    scheduler_tick_ms: int = 400
    # Final silence is the long background-check boundary.  It is distinct
    # from Smart Turn's short phrase-end silence in InputOptions.
    final_silence_ms: int = 30000
    voice_activation_ms: int = 200
    text_debounce_ms: int = 180
    replace_grace_ms: int = 1000

    @classmethod
    def from_mapping(cls, data):
        if not isinstance(data, dict):
            raise ValueError("agent must be an object")
        result = cls(**data)
        if type(result.enabled) is not bool:
            raise ValueError("agent.enabled must be boolean")
        for name in cls.__dataclass_fields__:
            if name in {"enabled"}:
                continue
            if type(getattr(result, name)) is not int or getattr(result, name) <= 0:
                raise ValueError(f"agent.{name} must be a positive integer")
        return result
