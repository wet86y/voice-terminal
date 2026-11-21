"""DshConsoleProxy（原生网页转发）：固定宿主、内存凭据和可撤销的设备会话。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from ipaddress import IPv4Address
import secrets
import time
import logging
from typing import Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import h11


CONSOLE_PROTOCOL_VERSION = 1  # CONSOLE_PROTOCOL_VERSION（网页隧道协议版本）。
CONSOLE_GATEWAY_PATH = "/v1/dsh-console"  # CONSOLE_GATEWAY_PATH（固定控制台会话与隧道路由）。
CONSOLE_CHUNK_BYTES = 65_536  # CONSOLE_CHUNK_BYTES（外层二进制消息和读缓冲上限）。
CONSOLE_HEADER_BYTES = 32_768  # CONSOLE_HEADER_BYTES（网页请求头上限）。
CONSOLE_MAX_BODY_BYTES = 4 * 1024**3  # CONSOLE_MAX_BODY_BYTES（单向网页正文传输上限）。
CONSOLE_SESSION_SECONDS = 8 * 60 * 60  # CONSOLE_SESSION_SECONDS（控制台授权最长存活秒数）。
CONSOLE_LEASE_SECONDS = 30  # CONSOLE_LEASE_SECONDS（客户端心跳中断后的授权回收秒数）。
_HOP_HEADERS = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"te", b"trailer", b"upgrade"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DshConsoleTarget:
    """DshConsoleTarget（受管目标快照）：只能由当前宿主管理器提供。"""

    origin: str  # origin（当前 DSH 实际回环来源）。
    generation: str  # generation（当前 DSH 实例代次，不以端口替代）。
    launch_url: str = field(repr=False)  # launch_url（仅服务端可见的启动令牌地址）。

    @property
    def port(self) -> int:
        address = urlsplit(self.origin)
        if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost"}
                or address.port is None or address.username or address.password
                or address.path not in {"", "/"} or address.query or address.fragment):
            raise ValueError("DSH 转发目标不是当前受管回环地址。")
        launch = urlsplit(self.launch_url)
        if (launch.scheme, launch.netloc) != (address.scheme, address.netloc) or launch.path != "/":
            raise ValueError("DSH 登录入口与受管目标不一致。")
        return address.port


@dataclass(eq=False)
class DshConsoleSession:
    """DshConsoleSession（设备网页授权）：不保存网页正文，不向客户端返回上游凭据。"""

    session_id: str  # session_id（随机控制台会话编号，仍需设备签名）。
    device_id: str  # device_id（已通过设备签名认证的客户端编号）。
    core_session_id: str  # core_session_id（签发授权的核心运行编号）。
    browser_origin: str  # browser_origin（客户端原生程序绑定的回环网页来源）。
    target: DshConsoleTarget  # target（授权时固定的受管 DSH 实例快照）。
    expires_at: float  # expires_at（单调时钟到期点）。
    lease_deadline: float  # lease_deadline（最近一次已签名访问或心跳续期的到期点）。
    cookie: str = field(repr=False)  # cookie（只在服务端内存持有的 DSH 凭据）。
    authorized: Callable[[], bool] = field(repr=False)  # authorized（当前设备信任及广播授权检查）。
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)  # stop（撤销及到期时关闭所有活动通道）。
    active: int = 0  # active（该会话当前活动的网页请求或升级连接数）。
    expiry_timer: asyncio.TimerHandle | None = field(default=None, repr=False)  # expiry_timer（到期时主动清除凭据）。
    guard_task: asyncio.Task | None = field(default=None, repr=False)  # guard_task（即使没有网页连接也检查撤销和心跳）。

    @property
    def authority(self) -> str:
        return urlsplit(self.browser_origin).netloc


def browser_origin(value: object) -> str:
    """browser_origin（客户端来源校验）：只接受规范的 IPv4 本机动态端口。"""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("客户端控制台来源无效。")
    address = urlsplit(value)
    host = IPv4Address(address.hostname or "")
    if (address.scheme != "http" or not host.is_loopback or address.port is None
            or value != f"http://{host}:{address.port}" or not 0 < address.port <= 65535):
        raise ValueError("客户端控制台只允许原生程序的本机回环来源。")
    return value


class DshConsoleProxy:
    """DshConsoleProxy（固定目标网页代理）：每个隧道处理一个 HTTP 交换及可选升级。"""

    def __init__(self, target: Callable[[], DshConsoleTarget | None]) -> None:
        self._target = target
        self._sessions: dict[str, DshConsoleSession] = {}
        self._open_lock = asyncio.Lock()

    def valid(self, session: DshConsoleSession) -> bool:
        target = self._target()
        return (not session.stop.is_set() and min(session.expires_at, session.lease_deadline) > time.monotonic() and session.authorized()
                and target is not None and target == session.target
                and self._sessions.get(session.session_id) is session)

    def close(self, session: DshConsoleSession) -> None:
        self._sessions.pop(session.session_id, None)
        if session.expiry_timer is not None:
            session.expiry_timer.cancel()
            session.expiry_timer = None
        if session.guard_task is not None:
            if session.guard_task is not asyncio.current_task():
                session.guard_task.cancel()
            session.guard_task = None
        session.cookie = ""
        session.stop.set()

    def close_all(self) -> None:
        for session in tuple(self._sessions.values()):
            self.close(session)

    def get(self, session_id: str, device_id: str, core_session_id: str) -> DshConsoleSession:
        session = self._sessions.get(session_id)
        if (session is None or session.device_id != device_id or session.core_session_id != core_session_id
                or not self.valid(session)):
            if session is not None and session.device_id == device_id:
                self.close(session)
            raise ValueError("远程控制台会话已失效，请重新打开。")
        session.lease_deadline = time.monotonic() + CONSOLE_LEASE_SECONDS
        return session

    async def open(self, device_id: str, core_session_id: str, origin: object,
                   authorized: Callable[[], bool]) -> DshConsoleSession:
        origin = browser_origin(origin)
        async with self._open_lock:
            for item in tuple(self._sessions.values()):
                if not self.valid(item):
                    self.close(item)
            if len(self._sessions) >= 32:
                raise ValueError("远程控制台会话数量达到上限。")
            target = self._target()
            if target is None or not authorized():
                raise ValueError("服务端 DSH 尚未就绪。")
            target.port  # 校验固定目标及启动地址；不接受客户端提交的上游地址。
            # 用现有协议状态机交换凭据，避免通用 HTTP（网页请求）客户端日志输出含令牌的地址。
            headers = await _exchange_credentials(target, urlsplit(origin).netloc)
            cookies: list[str] = []
            for name, value in headers:
                if name != b"set-cookie":
                    continue
                parsed = SimpleCookie()
                parsed.load(value.decode("latin1"))
                cookies.extend(f"{name}={morsel.value}" for name, morsel in parsed.items() if name.startswith("dsh-auth-"))
            if len(cookies) != 1 or self._target() != target or not authorized():
                raise ValueError("DSH 网页登录凭据或运行代次无效。")
            for item in tuple(self._sessions.values()):
                if item.device_id == device_id:
                    self.close(item)
            session = DshConsoleSession(secrets.token_hex(32), device_id, core_session_id, origin,
                target, time.monotonic() + CONSOLE_SESSION_SECONDS, time.monotonic() + CONSOLE_LEASE_SECONDS, cookies[0], authorized)
            self._sessions[session.session_id] = session
            session.expiry_timer = asyncio.get_running_loop().call_later(CONSOLE_SESSION_SECONDS, self.close, session)
            session.guard_task = asyncio.create_task(self._guard(session))
        return session

    async def _guard(self, session: DshConsoleSession) -> None:
        try:
            while self.valid(session):
                await asyncio.sleep(.5)
        finally:
            self.close(session)

    async def tunnel(self, websocket, session: DshConsoleSession) -> None:
        if not self.valid(session) or session.active >= 16:
            await websocket.close(code=1008)
            return
        session.active += 1
        tasks: list[asyncio.Task] = []
        try:
            await websocket.accept()

            async def revoked() -> None:
                await session.stop.wait()

            tasks = [asyncio.create_task(self._proxy(websocket, session)), asyncio.create_task(revoked())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 不记录可能包含启动令牌、凭据、网页或附件的异常正文；也不重放请求。
            logger.debug("dsh.console_tunnel_closed error_type=%s", type(exc).__name__)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            session.active -= 1
            with suppress(Exception):
                await websocket.close(code=1000)

    async def _proxy(self, websocket, session: DshConsoleSession) -> None:
        downstream = h11.Connection(h11.SERVER, max_incomplete_event_size=CONSOLE_HEADER_BYTES)
        upstream = None
        reader = writer = None
        upload = None
        try:
            while True:
                # 一条已签名隧道只属于一条浏览器连接；每个 HTTP（网页请求）周期重新检查授权与来源。
                request = await asyncio.wait_for(_next_browser_event(websocket, downstream), 30)
                if isinstance(request, h11.ConnectionClosed):
                    return
                if not isinstance(request, h11.Request):
                    raise ValueError("网页隧道缺少 HTTP 请求。")
                headers, upgrading = _request_headers(request, session)
                if not self.valid(session):
                    raise ValueError("DSH 网页会话已失效。")
                # 复用远程授权字节流；每次仍新建固定 DSH 本机连接，避免宿主空闲断开与写入重放竞态。
                reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", session.target.port), 5)
                upstream = h11.Connection(h11.CLIENT, max_incomplete_event_size=CONSOLE_HEADER_BYTES)
                writer.write(upstream.send(h11.Request(method=request.method, target=request.target, headers=headers)))
                await writer.drain()
                upload = asyncio.create_task(_send_browser_body(websocket, downstream, upstream, writer))
                received = 0
                while True:
                    event = upstream.next_event()
                    if event is h11.NEED_DATA:
                        upstream.receive_data(await reader.read(CONSOLE_CHUNK_BYTES))
                        continue
                    if isinstance(event, (h11.Response, h11.InformationalResponse)):
                        if event.status_code == 101 and not upgrading:
                            raise ValueError("DSH 返回了未申请的协议升级。")
                        response_headers = _response_headers(event.headers, session, event.status_code == 101)
                        response_type = h11.InformationalResponse if isinstance(event, h11.InformationalResponse) else h11.Response
                        await _send(websocket, downstream.send(response_type(status_code=event.status_code, headers=response_headers, reason=event.reason)))
                        if event.status_code == 101:
                            await asyncio.wait_for(upload, 5)
                            await self._upgraded(websocket, reader, writer, downstream.trailing_data[0], upstream.trailing_data[0])
                            return
                    elif isinstance(event, h11.Data):
                        received += len(event.data)
                        if received > CONSOLE_MAX_BODY_BYTES:
                            raise ValueError("网页下载超过传输上限。")
                        await _send(websocket, downstream.send(event))
                    elif isinstance(event, h11.EndOfMessage):
                        await _send(websocket, downstream.send(event))
                        break
                    else:
                        raise ValueError("DSH 网页响应未完整结束。")
                # 早返回的响应不能把尚未读完的上传当成下一请求；结束本隧道并由调用者处理结果。
                await asyncio.sleep(0)
                if downstream.their_state is not h11.DONE:
                    return
                await asyncio.wait_for(upload, 5)
                upload = None
                if downstream.our_state is not h11.DONE:
                    return
                downstream.start_next_cycle()
                await _close_writer(writer)
                reader = writer = upstream = None
        finally:
            if upload is not None:
                upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)
            if writer is not None:
                await _close_writer(writer)

    async def _upgraded(self, websocket, reader, writer, client_tail: bytes, upstream_tail: bytes) -> None:
        if client_tail:
            writer.write(client_tail)
            await writer.drain()
        if upstream_tail:
            await _send(websocket, upstream_tail)

        async def incoming() -> None:
            while True:
                writer.write(await _receive(websocket))
                await writer.drain()

        async def outgoing() -> None:
            while data := await reader.read(CONSOLE_CHUNK_BYTES):
                await _send(websocket, data)

        tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _receive(websocket) -> bytes:
    data = await websocket.receive_bytes()
    if not 0 < len(data) <= CONSOLE_CHUNK_BYTES:
        raise ValueError("控制台隧道二进制消息长度无效。")
    return data


async def _exchange_credentials(target: DshConsoleTarget, authority: str):
    writer = None
    try:
        async with asyncio.timeout(10):
            connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=CONSOLE_HEADER_BYTES)
            address = urlsplit(target.launch_url)
            path = address.path + ("?" + address.query if address.query else "")
            reader, writer = await asyncio.open_connection("127.0.0.1", target.port)
            writer.write(connection.send(h11.Request(method=b"GET", target=path.encode("ascii"),
                headers=[(b"host", authority.encode("ascii")), (b"connection", b"close")])))
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    connection.receive_data(await reader.read(CONSOLE_HEADER_BYTES))
                elif isinstance(event, h11.InformationalResponse):
                    continue
                elif isinstance(event, h11.Response):
                    headers = list(event.headers)
                    if (event.status_code != 303 or dict(headers).get(b"location") != b"./"
                            or sum(len(name) + len(value) + 4 for name, value in headers) > CONSOLE_HEADER_BYTES):
                        raise ValueError("DSH 网页登录交接失败。")
                    return headers
                else:
                    raise ValueError("DSH 网页登录未返回有效响应。")
    except (OSError, TimeoutError, h11.ProtocolError, UnicodeError):
        raise ValueError("DSH 网页登录交接失败。") from None
    finally:
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)


async def _send(websocket, data: bytes) -> None:
    for offset in range(0, len(data), CONSOLE_CHUNK_BYTES):
        await asyncio.wait_for(websocket.send_bytes(data[offset:offset + CONSOLE_CHUNK_BYTES]), 30)


async def _close_writer(writer) -> None:
    writer.close()
    with suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), 2)


async def _next_browser_event(websocket, connection):
    while True:
        event = connection.next_event()
        if event is not h11.NEED_DATA:
            return event
        connection.receive_data(await _receive(websocket))


def _connection_headers(headers) -> set[bytes]:
    return _HOP_HEADERS | {part.strip().lower() for name, value in headers if name == b"connection" for part in value.split(b",")}


async def _send_browser_body(websocket, downstream, upstream, writer) -> None:
    received = 0
    try:
        while True:
            event = await asyncio.wait_for(_next_browser_event(websocket, downstream), 60)
            if not isinstance(event, (h11.Data, h11.EndOfMessage)):
                raise ValueError("网页请求正文无效。")
            if isinstance(event, h11.Data):
                received += len(event.data)
                if received > CONSOLE_MAX_BODY_BYTES:
                    raise ValueError("网页上传超过传输上限。")
            writer.write(upstream.send(event))
            await writer.drain()
            if isinstance(event, h11.EndOfMessage):
                return
    except BaseException:
        writer.close()
        raise


def _request_headers(request: h11.Request, session: DshConsoleSession) -> tuple[list[tuple[bytes, bytes]], bool]:
    if (not request.target.startswith(b"/") or request.target.startswith(b"//")
            or b"#" in request.target or b"\\" in request.target or request.method in {b"CONNECT", b"TRACE"}):
        raise ValueError("控制台仅转发当前 DSH 的原始站内路径。")
    target = urlsplit(request.target.decode("ascii"))
    if any(name == "token" for name, _ in parse_qsl(target.query, keep_blank_values=True)):
        raise ValueError("浏览器不得提交 DSH 启动令牌。")
    values = {name: value for name, value in request.headers}
    if len(request.target) + sum(len(name) + len(value) + 4 for name, value in request.headers) > CONSOLE_HEADER_BYTES:
        raise ValueError("网页请求头超过长度上限。")
    if int(values.get(b"content-length", b"0")) > CONSOLE_MAX_BODY_BYTES:
        raise ValueError("网页上传超过传输上限。")
    if (values.get(b"host") != session.authority.encode("ascii")
            or (b"origin" in values and values[b"origin"] != session.browser_origin.encode("ascii"))
            or values.get(b"sec-fetch-site") == b"cross-site"):
        raise ValueError("控制台网页来源不匹配。")
    upgrading = values.get(b"upgrade", b"").lower() == b"websocket"
    if b"upgrade" in values and not upgrading:
        raise ValueError("控制台只允许网页 WebSocket 升级。")
    dropped = _connection_headers(request.headers) | {b"cookie", b"authorization", b"expect"}
    headers = [(name, value) for name, value in request.headers if name not in dropped
        and not name.startswith((b"x-forwarded-", b"x-voice-terminal-")) and name != b"forwarded"]
    # 插件自有 Cookie（网页状态）可继续使用；DSH 和项目代理凭据始终独立。
    extra_cookies = []
    for part in values.get(b"cookie", b"").split(b";"):
        name, separator, _ = part.strip().partition(b"=")
        if separator and not name.startswith((b"dsh-auth-", b"voice-dsh-console-")):
            extra_cookies.append(part.strip())
    headers.append((b"cookie", b"; ".join([session.cookie.encode("ascii"), *extra_cookies])))
    if upgrading:
        headers.extend([(b"connection", b"Upgrade"), (b"upgrade", b"websocket")])
    return headers, upgrading


def _response_headers(headers, session: DshConsoleSession, upgrading: bool) -> list[tuple[bytes, bytes]]:
    if sum(len(name) + len(value) + 4 for name, value in headers) > CONSOLE_HEADER_BYTES:
        raise ValueError("DSH 网页响应头超过长度上限。")
    dropped = _connection_headers(headers)
    result = []
    for name, value in headers:
        if name in dropped:
            continue
        if name == b"set-cookie" and value.lstrip().lower().startswith((b"dsh-auth-", b"voice-dsh-console-")):
            continue
        if name == b"location":
            location = urlsplit(value.decode("latin1"))
            if location.netloc == urlsplit(session.target.origin).netloc:
                local = urlsplit(session.browser_origin)
                value = urlunsplit((local.scheme, local.netloc, location.path, location.query, location.fragment)).encode("latin1")
            if any(key == "token" for key, _ in parse_qsl(location.query, keep_blank_values=True)):
                raise ValueError("DSH 登录跳转不得传到客户端。")
        result.append((name, value))
    if upgrading:
        result.extend([(b"connection", b"Upgrade"), (b"upgrade", b"websocket")])
    return result
