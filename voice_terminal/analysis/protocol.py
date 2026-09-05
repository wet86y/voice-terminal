import base64

MAX_PCM_BYTES = 16000 * 2 * 62


def decode_request(value: dict) -> bytes:
    if value.get("operation") not in {"endpoint", "refine"}:
        raise ValueError("unknown analysis operation")
    if not isinstance(value.get("request_id"), str) or not isinstance(value.get("turn_id"), str):
        raise ValueError("missing request identity")
    if type(value.get("generation_id")) is not int:
        raise ValueError("missing generation")
    encoded = value.get("pcm_base64", "")
    if not isinstance(encoded, str) or len(encoded) > MAX_PCM_BYTES * 4 // 3 + 4:
        raise ValueError("audio too large")
    pcm = base64.b64decode(encoded, validate=True)
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_PCM_BYTES:
        raise ValueError("invalid 16 kHz mono int16 PCM")
    return pcm
