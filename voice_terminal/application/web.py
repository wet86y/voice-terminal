"""Host HTTP surface（宿主网页接口）：会话受限的桌面入口和启动诊断。"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

from .startup_page import loading_page, render_startup_page


PUBLIC_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Voice Terminal · 桌面应用</title><style>body{background:#101217;color:#edf0f6;font:16px system-ui;max-width:760px;margin:16vh auto;padding:24px}main{border:1px solid #30384b;background:#181c27;padding:32px;border-radius:18px}p{color:#aeb7c9;line-height:1.8}</style>
<main><h1>Voice Terminal · 语音终端</h1><p>请通过桌面程序打开。此地址不提供网页业务入口。</p></main></html>"""

FALLBACK_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice Terminal · 语音终端</title><style>html,body{margin:0;background:#0e1118}__STARTUP_STYLE__ .startup-components{max-height:45vh;overflow:auto;margin:20px 0;padding:0;list-style:none;text-align:left;font-size:12px;line-height:1.65}.startup-components li{padding:10px 0;border-bottom:1px solid #ffffff12;white-space:pre-wrap}.startup-components button{display:block;margin-top:6px}</style></head><body>
<main class="startup-screen" role="status"><section class="startup-content"><div class="startup-icon" role="img" aria-label="语音终端">__STARTUP_ICON__</div>
<h1 class="startup-title" id="title">应用启动中</h1><p class="startup-status" id="phase">正在加载应用界面</p>
<div class="startup-progress" id="progress" data-indeterminate="true" role="progressbar" aria-label="应用启动进度"><span class="startup-progress-fill" id="fill"></span></div>
<ul class="startup-components" id="components" hidden></ul><div class="startup-actions"><button class="startup-button" id="skip">跳过</button><button class="startup-button" id="retry" hidden>重试</button><button class="startup-button" id="exit" hidden>退出应用</button></div></section></main>
<script>
const labels={waiting:'等待启动',starting:'启动中',ready:'运行正常',disabled:'已停用',failed:'需要检查',blocked:'依赖不可用',external:'外部进程',stopping:'退出中',stopped:'已退出'};
const bridge=window.__TAURI_INTERNALS__;
const phase=document.getElementById('phase'),list=document.getElementById('components'),retry=document.getElementById('retry'),exit=document.getElementById('exit');
const heading=document.getElementById('title'),progress=document.getElementById('progress'),fill=document.getElementById('fill'),skip=document.getElementById('skip');
let busy=false,monitoring=false,inspecting=false;
function statusError(status){
  if(!status||typeof status!=='object'||Array.isArray(status))return '桌面启动程序返回了无效状态。';
  if(typeof status.error==='string'&&status.error.trim())return status.error;
  if(status.error!==null&&status.error!=='')return '桌面启动程序的错误状态无效。';
  if(status.ready!==true)return '桌面启动程序尚未就绪。';
  if(status.frontend_mode!=='product')return '当前窗口不是产品界面入口。';
  if(typeof status.development!=='boolean')return '桌面启动程序的开发标识无效。';
  return '';
}
async function verifyDesktop(){
  if(!bridge||typeof bridge.invoke!=='function')throw new Error('没有检测到桌面原生命令桥。');
  const status=await bridge.invoke('desktop_status'),problem=statusError(status);
  if(problem)throw new Error(problem);
  if(status.startup_skipped===true)inspecting=true;
}
function restartPrompt(reason){
  heading.textContent='启动遇到问题';
  phase.textContent='请退出并重新启动桌面程序。'+(reason?' '+reason:'');
  list.replaceChildren();
  progress.hidden=true;skip.hidden=true;
  retry.hidden=!bridge||typeof bridge.invoke!=='function';
  exit.hidden=!bridge||typeof bridge.invoke!=='function';
}
async function refresh(){
  if(busy)return;
  busy=true;
  try{
    await verifyDesktop();
    const response=await fetch('/api/v1/application/snapshot',{cache:'no-store'});
    if(!response.ok)throw new Error('应用状态连接不可用。');
    const snapshot=await response.json();
    const active=snapshot.components.filter(component=>component.state==='starting');
    heading.textContent=inspecting?'本地组件':snapshot.startup_complete?'界面加载失败':'应用启动中';
    phase.textContent=snapshot.startup_complete?'应用界面资源不可用，请退出并重新启动。':active.length?'正在启动 '+active.map(component=>component.name).join('、'):'正在加载应用界面';
    progress.hidden=inspecting||snapshot.startup_complete===true;
    list.hidden=!inspecting;skip.hidden=inspecting;
    const total=Number(snapshot.total_components)||0,complete=Number(snapshot.completed_components)||0;
    progress.dataset.indeterminate=String(total<=0);
    fill.style.width=total>0?Math.min(100,Math.max(0,complete/total*100))+'%':'';
    list.replaceChildren();
    for(const component of snapshot.components){
      const item=document.createElement('li');
      item.textContent=component.name+' · '+(labels[component.state]||'不可用')+' · '+component.phase+(component.error?'\\n'+component.error:'');
      if(component.retry_allowed){
        const button=document.createElement('button');
        button.className='startup-button';
        button.textContent='重试';
        button.addEventListener('click',async()=>{
          try{
            await verifyDesktop();
            await fetch('/api/v1/application/components/'+component.id+'/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
            await refresh();
          }catch(error){restartPrompt(error instanceof Error?error.message:'桌面操作失败。');}
        });
        item.append(button);
      }
      list.append(item);
    }
    retry.hidden=true;
    exit.hidden=false;
    if(!monitoring){monitoring=true;window.setInterval(()=>void refresh(),2000);}
  }catch(error){restartPrompt(error instanceof Error?error.message:'桌面启动程序不可用。');}
  finally{busy=false;}
}
skip.addEventListener('click',async()=>{inspecting=true;progress.hidden=true;list.hidden=false;skip.hidden=true;await refresh();});
retry.addEventListener('click',()=>void refresh());
exit.addEventListener('click',async()=>{
  if(!bridge||typeof bridge.invoke!=='function')return;
  exit.disabled=true;
  try{await bridge.invoke('exit_application');}
  catch(error){exit.disabled=false;restartPrompt(error instanceof Error?error.message:'无法退出桌面应用。');}
});
if(!bridge||typeof bridge.invoke!=='function')restartPrompt('请从桌面应用重新打开。');
else void refresh();
</script></body></html>"""


class HostWeb:
    """HostWeb（宿主网页）：私网 UI 会话只由一次性桌面令牌建立。"""

    launch_token_lifetime_seconds = 60  # launch_token_lifetime_seconds（入口令牌有效秒数）。

    def __init__(self, application, directory: Path, *, port: int = 5173) -> None:
        self.application = application
        self.directory = directory.resolve()
        self.instance_id = str(application.state.value["instance_id"])  # instance_id（应用实例标识）。
        self.session = secrets.token_urlsafe(32)  # session（随机浏览器会话凭据）。
        # cookie_name（浏览器会话 Cookie 名称）：每个应用实例有各自的名字，避免旧实例 Cookie 串用。
        self.cookie_name = "voice_application_" + "".join(c for c in self.instance_id if c.isalnum())
        self._token_lock = threading.Lock()  # _token_lock（入口令牌原子消费锁）。
        self._launch_tokens: dict[str, float] = {}  # _launch_tokens（入口令牌到单调时钟过期点的映射）。
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                # 入口 URL 含短期启动令牌，HTTP 访问日志一律关闭。
                pass

            def send(self, data: bytes, content_type: str, status: int = 200, *, set_cookie: bool = False,
                     referrer_policy: str | None = None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if referrer_policy:
                    self.send_header("Referrer-Policy", referrer_policy)
                if set_cookie:
                    # session（浏览器会话凭据）不持久化，且脚本不能读取。
                    self.send_header("Set-Cookie", f"{owner.cookie_name}={owner.session}; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
                if not getattr(self, "_suppress_body", False):
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def data(self, value, status: int = 200):
                self.send(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

            def host_matches(self) -> bool:
                return self.headers.get("Host") == urlsplit(owner.origin).netloc

            def has_session(self) -> bool:
                matches = []
                for part in self.headers.get("Cookie", "").split(";"):
                    item = part.strip()
                    name, separator, value = item.partition("=")
                    if name == owner.cookie_name:
                        matches.append((bool(separator), value))
                if len(matches) != 1 or not matches[0][0]:
                    return False
                try:
                    return secrets.compare_digest(matches[0][1], owner.session)
                except TypeError:
                    return False

            def allowed(self, *, mutation=False, internal=False) -> bool:
                if not self.host_matches():
                    return False
                if internal:
                    try:
                        return secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.application.internal_token)
                    except TypeError:
                        return False
                valid = self.has_session()
                expected_origin = getattr(owner.application, "frontend_origin", "") or owner.origin
                if mutation:
                    # origin（请求来源）既接受直连 Host，也接受受管代理改写后的 Host 来源或实际界面来源。
                    valid = valid and self.headers.get("Origin") in {owner.origin, expected_origin}
                return valid

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                if not self.host_matches():
                    self.data({"error": "网页来源不匹配"}, 403)
                    return
                if path == "/desktop-entry":
                    self.desktop_entry(parsed.query)
                    return
                if path in ("/", "/index.html") and not self.has_session():
                    self.send(PUBLIC_HTML.encode(), "text/html; charset=utf-8", referrer_policy="no-referrer")
                    return
                if path == "/api/v1/application" or path.startswith("/api/v1/application/"):
                    if not self.allowed():
                        self.data({"error": "应用会话无效，请通过桌面程序重新打开"}, 403)
                        return
                    if path == "/api/v1/application/snapshot":
                        self.data(owner.application.state.snapshot())
                    elif path == "/api/v1/application/events":
                        self.events()
                    else:
                        self.data({"error": "接口不存在"}, 404)
                    return
                if path == "/application-config.js":
                    if not self.allowed():
                        self.data({"error": "应用会话无效，请通过桌面程序重新打开"}, 403)
                        return
                    snapshot = owner.application.state.snapshot()
                    # config（前端连接配置）字段：managed（受管标识）、origin（界面来源）、voice_api_url（语音接口）、voice_ws_url（实时语音接口）。
                    config = {"managed": True, "origin": getattr(owner.application, "frontend_origin", "") or owner.origin,
                        "voice_api_url": snapshot["voice_api_url"], "voice_ws_url": snapshot["voice_ws_url"]}
                    self.send(("window.__VOICE_APPLICATION__=" + json.dumps(config) + ";").encode(), "text/javascript; charset=utf-8")
                    return
                # 业务资产只有在一次性桌面握手建立的会话下才可读取。
                if not self.allowed():
                    self.data({"error": "请通过桌面程序打开"}, 403)
                    return
                target = (owner.directory / unquote(path).lstrip("/")).resolve()
                if not target.is_relative_to(owner.directory):
                    self.data({"error": "路径无效"}, 404)
                    return
                if path in ("/", "/index.html") or not Path(path).suffix:
                    target = owner.directory / "index.html"
                if target.is_file():
                    self.send(target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                elif path in ("/", "/index.html") or not Path(path).suffix:
                    self.send(render_startup_page(owner.application.root, FALLBACK_HTML).encode(), "text/html; charset=utf-8")
                else:
                    self.data({"error": "文件不存在"}, 404)

            def do_HEAD(self):
                if not self.host_matches():
                    self.data({"error": "网页来源不匹配"}, 403)
                    return
                path = urlsplit(self.path).path
                if path == "/desktop-entry" or path == "/api/v1/application/events":
                    self.data({"error": "不支持此接口方法"}, 405)
                    return
                self._suppress_body = True
                self.do_GET()

            def unsupported_method(self):
                path = urlsplit(self.path).path
                if not self.host_matches() or not self.allowed(mutation=True):
                    self.data({"error": "不允许的应用操作"}, 403)
                    return
                self.data({"error": "不支持此接口方法"}, 405)

            def do_OPTIONS(self):
                self.unsupported_method()

            def do_PUT(self):
                self.unsupported_method()

            def do_PATCH(self):
                self.unsupported_method()

            def do_DELETE(self):
                self.unsupported_method()

            def desktop_entry(self, query: str) -> None:
                parameters = parse_qs(query, keep_blank_values=True)
                values = parameters.get("token", [])
                if len(parameters) != 1 or len(values) != 1 or not values[0] or not owner.consume_launch_token(values[0]):
                    self.send("入口令牌无效或已使用，请通过桌面程序重新打开。".encode(), "text/plain; charset=utf-8", 403,
                              referrer_policy="no-referrer")
                    return
                # 页面脚本只负责同源 replace，不携带令牌，也不会触发跨站 303 重定向。
                body = loading_page(owner.application.root)
                self.send(body.encode(), "text/html; charset=utf-8", set_cookie=True, referrer_policy="no-referrer")

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
                elif path == "/api/v1/application/wake-tasks":
                    try:
                        self.data(owner.application.create_wake_task(payload), 201)
                    except HTTPError as exc:
                        try:
                            detail = json.loads(exc.read(16384)).get("detail", "任务创建失败。")
                        except (ValueError, AttributeError):
                            detail = "任务创建失败。"
                        self.data({"detail": detail}, exc.code if exc.code in {400, 404, 409, 422, 503} else 503)
                    except Exception:
                        self.data({"detail": "任务创建未确认，请刷新列表后检查；DSH（代理执行宿主）可能暂不可用。"}, 503)
                elif path.startswith("/api/v1/application/components/") and path.endswith("/retry"):
                    key = path.split("/")[-2]
                    if owner.application.request_retry(key):
                        self.data({"ok": True}, 202)
                    else:
                        self.data({"error": "组件当前不能重试"}, 409)
                else:
                    self.data({"error": "接口不存在"}, 404)

        class OwnedServer(ThreadingHTTPServer):
            # Windows（操作系统）的地址复用可能允许多个监听器占据同一端口。
            allow_reuse_address = False

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        try:
            self.server = OwnedServer(("127.0.0.1", port), Handler)
        except OSError:
            self.server = OwnedServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_port}"  # origin（宿主内部监听地址）。
        self.closed = False  # closed（宿主服务是否已关闭）。

    def issue_launch_token(self) -> str:
        """issue_launch_token（签发入口令牌）：生成只能兑换一次的 60 秒桌面凭据。"""
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with self._token_lock:
            self._launch_tokens = {value: expiry for value, expiry in self._launch_tokens.items() if expiry > now}
            self._launch_tokens[token] = now + self.launch_token_lifetime_seconds
        return token

    def consume_launch_token(self, token: str) -> bool:
        """consume_launch_token（消费入口令牌）：锁内删除，避免并发重复兑换。"""
        now = time.monotonic()
        with self._token_lock:
            expiry = self._launch_tokens.pop(token, None)
            return expiry is not None and expiry > now

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True, name="application-web").start()

    def close(self):
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
