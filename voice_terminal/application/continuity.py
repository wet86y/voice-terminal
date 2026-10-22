"""Desktop continuity（桌面连续会话）：仅在同一原生窗口的私有管道中交接，不落盘。"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit


def loopback_port(origin: str) -> int:
    """loopback_port（固定回环端口）：恢复时禁止静默换端口或接受其它来源。"""
    value = urlsplit(origin)
    if (value.scheme != "http" or value.hostname != "127.0.0.1" or not value.port
            or value.username or value.password or value.path or value.query or value.fragment
            or origin != f"http://127.0.0.1:{value.port}"):
        raise ValueError("桌面恢复来源无效。")
    return value.port


def read_start(stream) -> dict | None:
    """read_start（接管启动）：在建立网页入口前读取桌面所有权及可选连续会话。"""
    line = stream.readline(16385)
    if not line or len(line) > 16384:
        raise RuntimeError("桌面启动控制管道已关闭或消息无效。")
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("protocol") != 1 or value.get("kind") != "start":
        raise RuntimeError("桌面尚未接管本次服务。")
    continuity = value.get("continuity")  # continuity（内存中的网页来源与会话凭据）。
    if continuity is None:
        return None
    if not isinstance(continuity, dict):
        raise ValueError("桌面连续会话无效。")
    for field in ("host_origin", "frontend_origin"):
        loopback_port(continuity.get(field, ""))
    if (not re.fullmatch(r"voice_application_[A-Za-z0-9]+", continuity.get("cookie_name", ""))
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", continuity.get("session", ""))):
        raise ValueError("桌面连续会话凭据无效。")
    return continuity
