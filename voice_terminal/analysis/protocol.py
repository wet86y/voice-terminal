import base64

MAX_PCM_BYTES = 16000 * 2 * 62


def open_request_audio(value: dict):
    """open_request_audio（打开请求音频）：旧正文或持有到原生完成的共享读映射。"""
    if "audio_ref" not in value:
        return decode_request(value), None
    if "pcm_base64" in value:
        raise ValueError("音频正文与引用不能同时提交")
    if (value.get("operation") not in {"endpoint", "refine", "emotion", "acoustic"}
            or not isinstance(value.get("request_id"), str) or not isinstance(value.get("turn_id"), str)
            or type(value.get("generation_id")) is not int):
        raise ValueError("分析请求身份无效")
    from ..audio.shared import SharedAudioReader
    reader = SharedAudioReader(value["audio_ref"], value["request_id"], MAX_PCM_BYTES)
    return reader.view, reader


def decode_request(value: dict) -> bytes:
    if value.get("operation") not in {"endpoint", "refine", "emotion", "acoustic"}:
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
