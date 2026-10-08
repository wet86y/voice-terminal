"""Loopback listener binding（回环监听绑定）and endpoint validation（端点校验）."""
from __future__ import annotations

import errno
import json
import math
import os
import socket
import time
from urllib.parse import urlsplit, urlunsplit


ENDPOINT_PATHS = {
    "host": "/",
    "web": "/",
    "voice": "/",
    "asr": "/asr",
    "analysis": "/analysis",
    "speaker": "/speaker",
    "dsh": "/",
}
ENDPOINT_SCHEMES = {
    "host": {"http"},
    "web": {"http"},
    "voice": {"http"},
    "asr": {"ws"},
    "analysis": {"ws"},
    "speaker": {"ws"},
    "dsh": {"http"},
}
ENDPOINT_ENV = "VOICE_APP_ENDPOINTS"  # VOICE_APP_ENDPOINTS（受管端点表）。


def is_address_in_use(exc: OSError) -> bool:
    """Recognize EADDRINUSE on POSIX and Winsock（Windows 套接字错误码）."""
    return exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048


def bind_loopback(preferred_port: int, *, host: str = "127.0.0.1") -> tuple[socket.socket, int]:
    """Bind the preferred loopback port, falling back to an OS-assigned port.

    Binding is the authority for availability; callers must keep this socket and
    pass it to the HTTP/WebSocket server instead of closing and rebinding it.
    """
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("受管服务只允许绑定本机回环地址。")
    if type(preferred_port) is not int or not 0 <= preferred_port <= 65535:
        raise ValueError("首选端口必须为 0 到 65535 之间的整数。")

    bind_host = "127.0.0.1"

    def create(port: int) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((bind_host, port))
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            return listener
        except BaseException:
            listener.close()
            raise

    try:
        listener = create(preferred_port)
    except OSError as exc:
        if preferred_port == 0 or not is_address_in_use(exc):
            raise
        listener = create(0)
    return listener, int(listener.getsockname()[1])


def endpoint_url(component: str, port: int, *, host: str = "127.0.0.1") -> str:
    """Build the canonical loopback URL（回环地址）for a component listener."""
    if component not in ENDPOINT_PATHS:
        raise ValueError("未知端点组件。")
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("端点主机必须是本机回环地址。")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("已绑定端口必须为 1 到 65535 之间的整数。")
    scheme = "http" if component in {"host", "web", "voice", "dsh"} else "ws"
    return urlunsplit((scheme, f"{host}:{port}", ENDPOINT_PATHS[component], "", "")).rstrip("/")


def validate_endpoint_url(component: str, value: object) -> dict[str, object]:
    """Validate and describe an actual endpoint URL（实际端点地址）."""
    if component not in ENDPOINT_PATHS or not isinstance(value, str):
        raise ValueError("端点组件或地址无效。")
    try:
        address = urlsplit(value)
        port = address.port
    except ValueError as exc:
        raise ValueError("端点地址格式无效。") from exc
    expected_path = ENDPOINT_PATHS[component]
    if (address.scheme not in ENDPOINT_SCHEMES[component]
            or address.hostname not in {"127.0.0.1", "localhost"}
            or port is None or not 1 <= port <= 65535
            or address.username or address.password or address.query or address.fragment
            or address.path not in ({"", "/"} if expected_path == "/" else {expected_path})):
        raise ValueError("端点地址不符合本机协议约束。")
    return {
        "url": urlunsplit((address.scheme, address.netloc, expected_path, "", "")).rstrip("/"),
        "scheme": address.scheme,  # scheme（传输协议）。
        "host": address.hostname,  # host（回环主机）。
        "port": port,  # port（实际监听端口）。
        "path": expected_path,  # path（组件协议路径）。
    }


def validate_endpoint_report(component: str, payload: dict) -> dict[str, object]:
    """Validate endpoint_url（端点地址）and endpoint（端点元数据）from a child report.

    endpoint fields: host（回环主机）, port（实际端口）, base_url（服务来源）, pid（进程编号）,
    created_at（进程创建时间）. The host table adds scheme（协议）, path（协议路径） and url（完整地址）.
    """
    endpoint = payload.get("endpoint")
    endpoint_url_value = payload.get("endpoint_url")
    if isinstance(endpoint, dict):
        host = endpoint.get("host")
        port = endpoint.get("port")
        base_url = endpoint.get("base_url")
        if ((host is not None and (not isinstance(host, str) or host not in {"127.0.0.1", "localhost"}))
                or (port is not None and (isinstance(port, bool) or not isinstance(port, int)
                    or not 1 <= port <= 65535))):
            raise ValueError("端点报告的主机或端口无效。")
        scheme = "http" if component in {"host", "web", "voice", "dsh"} else "ws"
        if base_url is not None:
            if not isinstance(base_url, str):
                raise ValueError("端点报告的 base_url（基础地址）无效。")
            try:
                base = urlsplit(base_url)
                base_port = base.port
            except ValueError as exc:
                raise ValueError("端点报告的 base_url（基础地址）无效。") from exc
            if (base.scheme != scheme or base.hostname not in {"127.0.0.1", "localhost"}
                    or (host is not None and base.hostname != host)
                    or (port is not None and base_port != port)
                    or base.path not in ("", "/") or base.username or base.password
                    or base.query or base.fragment):
                raise ValueError("端点报告的 base_url（基础地址）与主机端口不一致。")
            if endpoint_url_value is None:
                if base_port is None:
                    raise ValueError("端点报告的 base_url（基础地址）缺少端口。")
                endpoint_url_value = endpoint_url(component, base_port, host=base.hostname)
        elif endpoint_url_value is None and host is not None and port is not None:
            endpoint_url_value = endpoint_url(component, port, host=host)
    detail = validate_endpoint_url(component, endpoint_url_value)
    if isinstance(endpoint, dict):
        endpoint_host, endpoint_port = endpoint.get("host"), endpoint.get("port")
        if endpoint_host is not None and endpoint_host != detail["host"]:
            raise ValueError("端点报告主机与 URL 不一致。")
        if endpoint_port is not None and endpoint_port != detail["port"]:
            raise ValueError("端点报告端口与 URL 不一致。")
    pid = endpoint.get("pid") if isinstance(endpoint, dict) else None
    if pid is None:
        pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("端点报告的 pid（进程编号）无效。")
    created_at = endpoint.get("created_at") if isinstance(endpoint, dict) else None
    if created_at is not None:
        if (isinstance(created_at, bool) or not isinstance(created_at, (int, float))
                or not math.isfinite(created_at) or created_at <= 0
                or created_at > time.time() + 60):
            raise ValueError("端点报告的 created_at（创建时间）无效。")
    detail["pid"] = pid
    detail["created_at"] = float(created_at) if created_at is not None else None
    return detail


def read_endpoint_table() -> dict[str, dict[str, object]]:
    """Read the managed endpoint table（受管端点表）from the process environment."""
    raw = os.environ.get(ENDPOINT_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("VOICE_APP_ENDPOINTS（受管端点表）不是有效 JSON。") from exc
    if not isinstance(parsed, dict):
        raise ValueError("VOICE_APP_ENDPOINTS（受管端点表）必须是对象。")
    result: dict[str, dict[str, object]] = {}
    for component, row in parsed.items():
        if component not in ENDPOINT_PATHS or not isinstance(row, dict):
            continue
        actual = row.get("url")
        preferred = row.get("preferred_url")
        entry: dict[str, object] = {}
        if actual:
            entry.update(validate_endpoint_url(component, actual))
        if preferred:
            entry["preferred_url"] = validate_endpoint_url(component, preferred)["url"]
        if entry:
            result[component] = entry
    return result


def endpoint_from_environment(component: str, *, preferred: bool = False) -> str | None:
    """Return one validated URL from VOICE_APP_ENDPOINTS（受管端点表）."""
    row = read_endpoint_table().get(component)
    if not row:
        return None
    value = row.get("preferred_url") if preferred else row.get("url") or row.get("preferred_url")
    return str(value) if isinstance(value, str) else None
