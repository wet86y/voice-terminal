"""Host HTTP surface（宿主网页接口）：模型启动前即可使用的反馈入口。"""
from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit

FALLBACK_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Voice Terminal · 启动诊断</title><style>body{background:#101217;color:#edf0f6;font:16px system-ui;max-width:920px;margin:60px auto;padding:24px}button{padding:10px;margin:8px;background:#30384b;color:white;border:0;border-radius:8px}li{padding:12px;white-space:pre-wrap}small{color:#aaa}</style>
<h1>Voice Terminal · 系统运行</h1><p>网页构建产物尚未准备好。请在项目内执行 npm run build（构建网页）。</p><p id="phase">连接启动程序…</p><ul id="components"></ul><button onclick="location.reload()">重新打开工作台</button><button id="exit">退出应用</button>
<script>const labels={waiting:'等待启动',starting:'启动中',ready:'运行正常',disabled:'已停用',failed:'需要检查',blocked:'依赖不可用',external:'外部进程',stopping:'退出中',stopped:'已退出'};async function refresh(){try{const s=await(await fetch('/api/v1/application/snapshot')).json();document.getElementById('phase').textContent=s.phase;const list=document.getElementById('components');list.replaceChildren();for(const c of s.components){const li=document.createElement('li');li.textContent=c.name+' · '+(labels[c.state]||'不可用')+' · '+c.phase+(c.error?'\\n'+c.error:'');if(c.retry_allowed){const b=document.createElement('button');b.textContent='重试';b.onclick=()=>fetch('/api/v1/application/components/'+c.id+'/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});li.append(b)}list.append(li)}}catch{document.getElementById('phase').textContent='应用已退出或连接中断'}}refresh();setInterval(refresh,2000);document.getElementById('exit').onclick=()=>fetch('/api/v1/application/exit',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});</script></html>"""


class HostWeb:
    def __init__(self, application, directory: Path, *, port: int = 5173) -> None:
        self.application = application
        self.directory = directory.resolve()
        self.session = secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def send(self, data: bytes, content_type: str, status: int = 200, *, cookie: bool = False):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if cookie:
                    self.send_header("Set-Cookie", f"voice_application={owner.session}; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def data(self, value, status: int = 200):
                self.send(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

            def allowed(self, *, mutation=False, internal=False) -> bool:
                if self.headers.get("Host") != urlsplit(owner.origin).netloc:
                    return False
                if internal:
                    return secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.application.internal_token)
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                    valid = secrets.compare_digest(cookie["voice_application"].value, owner.session)
                except (KeyError, ValueError):
                    valid = False
                if mutation:
                    valid = valid and self.headers.get("Origin") == owner.origin
                return valid

            def do_GET(self):
                path = urlsplit(self.path).path
                if self.headers.get("Host") != urlsplit(owner.origin).netloc:
                    self.data({"error": "网页来源不匹配"}, 403)
                    return
                if path.startswith("/api/v1/application/"):
                    if not self.allowed():
                        self.data({"error": "应用会话无效，请重新打开页面"}, 403)
                        return
                    if path == "/api/v1/application/snapshot":
                        self.data(owner.application.state.snapshot())
                    elif path == "/api/v1/application/events":
                        self.events()
                    else:
                        self.data({"error": "接口不存在"}, 404)
                    return
                if path == "/application-config.js":
                    snapshot = owner.application.state.snapshot()
                    config = {"managed": True, "origin": owner.origin,
                        "voice_api_url": snapshot["voice_api_url"], "voice_ws_url": snapshot["voice_ws_url"]}
                    self.send(("window.__VOICE_APPLICATION__=" + json.dumps(config) + ";").encode(), "text/javascript", cookie=True)
                    return
                target = (owner.directory / unquote(path).lstrip("/")).resolve()
                if not target.is_relative_to(owner.directory):
                    self.data({"error": "路径无效"}, 404)
                    return
                if path == "/" or not Path(path).suffix:
                    target = owner.directory / "index.html"
                if target.is_file():
                    content = target.read_bytes()
                    if target.name == "index.html":
                        content = content.replace(b"<head>", b'<head><script src="/application-config.js"></script>', 1)
                    self.send(content, mimetypes.guess_type(target.name)[0] or "application/octet-stream", cookie=target.name == "index.html")
                elif path == "/" or not Path(path).suffix:
                    self.send(FALLBACK_HTML.encode(), "text/html; charset=utf-8", cookie=True)
                else:
                    self.data({"error": "文件不存在"}, 404)

            def events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                revision = -1
                try:
                    while not owner.closed:
                        snapshot = owner.application.state.snapshot()
                        if snapshot["revision"] != revision:
                            self.wfile.write(("data: " + json.dumps(snapshot, ensure_ascii=False) + "\n\n").encode())
                            self.wfile.flush()
                            revision = snapshot["revision"]
                            if snapshot["state"] == "stopped":
                                break
                        with owner.application.state.condition:
                            owner.application.state.condition.wait(timeout=1)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            def do_POST(self):
                path = urlsplit(self.path).path
                internal = path == "/internal/application/report"
                if not self.allowed(mutation=not internal, internal=internal):
                    self.data({"error": "不允许的应用操作"}, 403)
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    self.data({"error": "需要 JSON（结构化数据）请求"}, 415)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 16384:
                        raise ValueError()
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError()
                except (ValueError, json.JSONDecodeError):
                    self.data({"error": "请求内容无效"}, 400)
                    return
                if internal:
                    owner.application.report(payload)
                    self.data({"ok": True})
                elif path == "/api/v1/application/exit":
                    owner.application.request_exit()
                    self.data({"ok": True}, 202)
                elif path.startswith("/api/v1/application/components/") and path.endswith("/retry"):
                    key = path.split("/")[-2]
                    if owner.application.request_retry(key):
                        self.data({"ok": True}, 202)
                    else:
                        self.data({"error": "组件当前不能重试"}, 409)
                else:
                    self.data({"error": "接口不存在"}, 404)

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.closed = False

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True, name="application-web").start()

    def close(self):
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
