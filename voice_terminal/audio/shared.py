"""SharedAudio（共享音频）：Windows（操作系统）映射、签名范围与原生完成租约。"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
from multiprocessing.shared_memory import SharedMemory
import uuid

from ..resources import ResourcePolicy, registry


def capabilities() -> dict | None:
    if not ResourcePolicy.load().shared_audio or os.name != "nt":
        return None
    if not os.environ.get("VOICE_APP_INSTANCE_ID") or not os.environ.get("VOICE_APP_INTERNAL_TOKEN"):
        return None
    import psutil
    return {"version": 1, "instance_id": os.environ["VOICE_APP_INSTANCE_ID"],  # 版本与宿主实例标识。
            "generation": os.environ.get("VOICE_APP_SERVICE_GENERATION", ""),  # 接收进程代次。
            "pid": os.getpid(), "created_at": psutil.Process().create_time()}  # 接收进程身份。


def compatible(peer) -> bool:
    local = capabilities()
    return bool(local and isinstance(peer, dict) and peer.get("version") == 1
                and peer.get("instance_id") == local["instance_id"]
                and isinstance(peer.get("generation"), str) and peer["generation"]
                and type(peer.get("pid")) is int and peer["pid"] > 0
                and type(peer.get("created_at")) in (int, float))


def signature(value: dict) -> str:
    token = os.environ.get("VOICE_APP_INTERNAL_TOKEN", "")
    if not token:
        raise ValueError("共享音频缺少宿主凭据")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(token.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class SharedAudio:
    def __init__(self, pcm: bytes, peer: dict, request_id: str):
        if not compatible(peer) or not pcm or len(pcm) % 2:
            raise ValueError("共享音频能力或样本格式无效")
        import psutil
        self.memory = SharedMemory(create=True, size=len(pcm), name="vt_audio_" + uuid.uuid4().hex)
        self.closed = False
        self.lease = None
        try:
            self.memory.buf[:] = pcm
            self.reference = {"version": 1, "name": self.memory.name, "offset": 0, "size": len(pcm),
            "sample_rate": 16000, "channels": 1, "format": "s16le",  # 采样率、声道及16位小端格式。
            "instance_id": os.environ["VOICE_APP_INSTANCE_ID"], "receiver_generation": peer["generation"],
            "receiver_pid": peer["pid"], "receiver_created_at": peer["created_at"],
            "owner_pid": os.getpid(), "owner_created_at": psutil.Process().create_time(),
            "request_id": request_id}  # 宿主、接收代次、双方身份及请求标识。
            self.reference["signature"] = signature(self.reference)  # signature（签名校验值）。
            self.lease = registry.lease("shared_audio_owner", self.memory, len(pcm), release_on="request_end")
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.memory.close()
            self.memory.unlink()
            if self.lease is not None:
                self.lease.close()
            self.closed = True


class SharedAudioReader:
    def __init__(self, value: dict, request_id: str, max_bytes: int):
        local = capabilities()
        if not local or not isinstance(value, dict):
            raise ValueError("当前进程未启用共享音频")
        value = dict(value)
        signed = value.pop("signature", None)
        if not isinstance(signed, str) or not hmac.compare_digest(signed, signature(value)):
            raise ValueError("共享音频签名无效")
        if (value.get("version") != 1 or value.get("instance_id") != local["instance_id"]
                or value.get("receiver_generation") != local["generation"]
                or value.get("receiver_pid") != local["pid"]
                or value.get("receiver_created_at") != local["created_at"]
                or value.get("request_id") != request_id
                or (value.get("sample_rate"), value.get("channels"), value.get("format")) != (16000, 1, "s16le")):
            raise ValueError("共享音频实例、代次、请求或格式不匹配")
        name, offset, size = value.get("name"), value.get("offset"), value.get("size")
        if (not isinstance(name, str) or not name.startswith("vt_audio_")
                or type(offset) is not int or type(size) is not int or offset < 0 or offset % 2
                or not 0 < size <= max_bytes or size % 2):
            raise ValueError("共享音频范围无效")
        import psutil
        if psutil.Process(value["owner_pid"]).create_time() != value.get("owner_created_at"):
            raise ValueError("共享音频所有者已过期")
        self.memory = SharedMemory(name=name)
        if offset + size > self.memory.size:
            self.memory.close()
            raise ValueError("共享音频超出映射范围")
        self.view = self.memory.buf[offset:offset + size].toreadonly()
        self.lease = registry.lease("shared_audio_reader", self.view, release_on="native_complete")
        self.in_use = False
        self.closed = False

    def close(self):
        if not self.closed:
            self.lease.close()
            self.view.release()
            self.memory.close()
            self.closed = True
            self.in_use = False


async def native_audio_call(function, pcm, reader=None):
    """原生调用被 shield（取消屏蔽）保护，读映射只在完成回调中关闭。"""
    lease = registry.lease("native_audio", pcm, release_on="native_complete")
    if reader:
        reader.in_use = True
    try:
        future = asyncio.get_running_loop().run_in_executor(None, function, pcm)
    except BaseException:
        lease.close()
        if reader:
            reader.in_use = False
        raise
    def finished(result):
        if not result.cancelled():
            result.exception()
        lease.close()
        if reader:
            reader.close()
    future.add_done_callback(finished)
    return await asyncio.shield(future)
