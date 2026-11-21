//! DshConsoleForwarder（原生网页转发）：客户端回环入口、一次性引导和完整上游协议。
use crate::ssh_device::{DshConsoleLease, SshDeviceConnection, SshDeviceManager};
use futures_util::{SinkExt, StreamExt};
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full};
use hyper::{
    body::{Bytes, Incoming},
    header,
    service::service_fn,
    Request, Response, StatusCode,
};
use hyper_util::rt::TokioIo;
use std::{
    collections::HashMap,
    convert::Infallible,
    error::Error,
    io,
    pin::Pin,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, DuplexStream, ReadBuf},
    net::TcpListener,
    sync::{mpsc, watch, OwnedSemaphorePermit, Semaphore},
    task::{JoinHandle, JoinSet},
};
use tokio_tungstenite::{
    tungstenite::{
        protocol::{Role, WebSocketConfig},
        Message,
    },
    WebSocketStream,
};

const CHUNK_BYTES: usize = 65_536; // CHUNK_BYTES（外层网页隧道二进制块上限）。
const BOOTSTRAP_PATH: &str = "/_voice_terminal_console/"; // BOOTSTRAP_PATH（仅原生程序签发的一次性引导路径）。
const SESSION_SECONDS: u64 = 8 * 60 * 60; // SESSION_SECONDS（本机网页会话最长存活秒数）。
const HTTP_IDLE_SECONDS: u64 = 8; // HTTP_IDLE_SECONDS（空闲网页隧道回收秒数，早于服务端三十秒空闲上限）。
type BoxError = Box<dyn Error + Send + Sync>;
type Body = UnsyncBoxBody<Bytes, BoxError>;

struct HttpConnection {
    sender: hyper::client::conn::http1::SendRequest<Incoming>, // sender（仅属于当前浏览器 TCP 的请求发送器）。
    driver: JoinHandle<()>, // driver（底层有界隧道驱动，随浏览器连接回收）。
    last_used: Instant,     // last_used（最近一次完整响应结束时间）。
}
impl Drop for HttpConnection {
    fn drop(&mut self) {
        self.driver.abort();
    }
}
type HttpConnectionSlot = Option<HttpConnection>; // HttpConnectionSlot（当前浏览器连接的专用上游）。

struct ForwardedBody {
    incoming: Incoming, // incoming（保持流式背压的原站正文）。
    guard: Option<tokio::sync::OwnedMutexGuard<HttpConnectionSlot>>, // guard（正文完成前独占请求发送器）。
}
impl ForwardedBody {
    fn new(incoming: Incoming, guard: tokio::sync::OwnedMutexGuard<HttpConnectionSlot>) -> Self {
        let mut body = Self {
            incoming,
            guard: Some(guard),
        };
        if hyper::body::Body::is_end_stream(&body.incoming) {
            body.finish(false);
        }
        body
    }
    fn finish(&mut self, failed: bool) {
        if let Some(mut slot) = self.guard.take() {
            if failed {
                slot.take();
            } else if let Some(connection) = slot.as_mut() {
                connection.last_used = Instant::now();
            }
        }
    }
}
impl Drop for ForwardedBody {
    fn drop(&mut self) {
        self.finish(true);
    }
}
impl hyper::body::Body for ForwardedBody {
    type Data = Bytes;
    type Error = hyper::Error;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<hyper::body::Frame<Bytes>, hyper::Error>>> {
        let result = hyper::body::Body::poll_frame(Pin::new(&mut self.incoming), cx);
        match &result {
            Poll::Ready(None) => self.finish(false),
            Poll::Ready(Some(Err(_))) => self.finish(true),
            Poll::Ready(Some(Ok(_))) if hyper::body::Body::is_end_stream(&self.incoming) => {
                self.finish(false)
            }
            _ => {}
        }
        result
    }
    fn is_end_stream(&self) -> bool {
        hyper::body::Body::is_end_stream(&self.incoming)
    }
    fn size_hint(&self) -> hyper::body::SizeHint {
        hyper::body::Body::size_hint(&self.incoming)
    }
}

/// ConsoleTunnel（有界网页字节流）：销毁时通知专用 SSH（安全外壳协议）会话退出。
pub struct ConsoleTunnel {
    io: DuplexStream,          // io（交给成熟 HTTP（网页请求）实现的有界双向流）。
    stop: watch::Sender<bool>, // stop（通知外层网页隧道停止并回收连接）。
}

impl ConsoleTunnel {
    pub(crate) async fn new<S>(stream: S, connection: SshDeviceConnection) -> Self
    where
        S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        let config = WebSocketConfig::default()
            .max_message_size(Some(CHUNK_BYTES))
            .max_frame_size(Some(CHUNK_BYTES));
        let websocket = WebSocketStream::from_raw_socket(stream, Role::Client, Some(config)).await;
        let (local, pipe) = tokio::io::duplex(CHUNK_BYTES);
        let (stop, mut stopped) = watch::channel(false);
        tokio::spawn(async move {
            let (mut sink, mut source) = websocket.split();
            let (mut input, mut output) = tokio::io::split(pipe);
            let (pong, mut pongs) = mpsc::channel(4);
            let upload = async {
                let mut buffer = vec![0u8; CHUNK_BYTES];
                loop {
                    let message = tokio::select! {
                        read = input.read(&mut buffer) => {
                            let count = read?;
                            if count == 0 { return Ok::<(),io::Error>(()); }
                            Message::Binary(Bytes::copy_from_slice(&buffer[..count]))
                        }
                        pong = pongs.recv() => {
                            let Some(pong) = pong else { return Ok(()); };
                            Message::Pong(pong)
                        }
                    };
                    tokio::time::timeout(Duration::from_secs(30), sink.send(message))
                        .await
                        .map_err(|_| io::Error::other("控制台隧道写入超时。"))?
                        .map_err(|_| io::Error::other("控制台隧道写入失败。"))?;
                }
            };
            let download = async {
                while let Some(message) = source.next().await {
                    match message.map_err(|_| io::Error::other("控制台隧道读取失败。"))? {
                        Message::Binary(data) if !data.is_empty() && data.len() <= CHUNK_BYTES => {
                            output.write_all(&data).await?;
                        }
                        Message::Ping(value) => {
                            pong.send(value)
                                .await
                                .map_err(|_| io::Error::other("控制台保活写端已关闭。"))?;
                        }
                        Message::Pong(_) => {}
                        Message::Close(_) => return Ok::<(), io::Error>(()),
                        _ => return Err(io::Error::other("控制台隧道只接受有界二进制消息。")),
                    }
                }
                Ok(())
            };
            tokio::select! { _ = stopped.changed() => {}, _ = upload => {}, _ = download => {} }
            let _ = connection.close().await;
        });
        Self { io: local, stop }
    }
}

impl Drop for ConsoleTunnel {
    fn drop(&mut self) {
        let _ = self.stop.send(true);
    }
}
impl AsyncRead for ConsoleTunnel {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_read(context, buffer)
    }
}
impl AsyncWrite for ConsoleTunnel {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.io).poll_write(context, buffer)
    }
    fn poll_flush(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_flush(context)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_shutdown(context)
    }
}

struct BrowserSession {
    origin: String,                           // origin（当前原生监听器的实际网页来源）。
    authority: String,                        // authority（必须与每个浏览器请求匹配的主机端口）。
    cookie_name: String,                      // cookie_name（当前监听器独有的本机会话凭据名称）。
    cookie: String,                           // cookie（不返回产品网页或写入日志的本机随机凭据）。
    tickets: Mutex<HashMap<String, Instant>>, // tickets（最长一分钟、只消费一次的浏览器引导票据）。
    expires_at: Instant,                      // expires_at（本机授权到期点）。
    running: AtomicBool,                      // running（当前监听和授权是否仍有效）。
    stop: watch::Sender<bool>,                // stop（统一终止监听、HTTP 连接和升级连接）。
    remote: Arc<SshDeviceManager>,            // remote（持有当前已配对目标的原生连接管理器）。
    lease: DshConsoleLease,                   // lease（当前核心及设备的独立网页授权）。
}

/// DshConsoleForwarder（客户端网页入口）：同一合法目标复用监听，允许多个浏览器标签页。
pub struct DshConsoleForwarder {
    session: Arc<BrowserSession>, // session（所有网页标签共用的本机授权和停止信号）。
    task: Option<JoinHandle<()>>, // task（监听及其所有普通网页连接的回收任务）。
}

impl DshConsoleForwarder {
    pub async fn start(remote: Arc<SshDeviceManager>) -> Result<Self, String> {
        // 使用独立的 127/8（本机回环网段）主机地址，隔离其他本机端口的浏览器 Cookie（会话凭据）。
        let random = rand::random::<[u8; 3]>();
        let host =
            std::net::Ipv4Addr::new(127, 64 + (random[0] & 63), random[1], 1 + random[2] % 254);
        let listener = TcpListener::bind((host, 0))
            .await
            .map_err(|_| "无法建立客户端 DSH 网页回环入口。".to_string())?;
        let address = listener
            .local_addr()
            .map_err(|_| "无法读取客户端 DSH 网页地址。".to_string())?;
        let origin = format!("http://{address}");
        let lease = remote.open_dsh_console(&origin).await?;
        let (stop, mut stopped) = watch::channel(false);
        let session = Arc::new(BrowserSession {
            origin,
            authority: address.to_string(),
            cookie_name: format!("voice-dsh-console-{}", nonce()),
            cookie: nonce(),
            tickets: Mutex::new(HashMap::new()),
            expires_at: Instant::now() + Duration::from_secs(SESSION_SECONDS),
            running: AtomicBool::new(true),
            stop,
            remote,
            lease,
        });
        let state = session.clone();
        let task = tokio::spawn(async move {
            let health_state = state.clone();
            let mut health_stop = state.stop.subscribe();
            let health = tokio::spawn(async move {
                let mut checks = 0;
                loop {
                    tokio::select! {
                        _ = health_stop.changed() => break,
                        _ = tokio::time::sleep(Duration::from_secs(1)) => {}
                    }
                    checks += 1;
                    if !health_state.running.load(Ordering::Acquire)
                        || Instant::now() >= health_state.expires_at
                        || !health_state
                            .remote
                            .dsh_console_current(&health_state.lease)
                            .await
                    {
                        let _ = health_state.stop.send(true);
                        break;
                    }
                    if checks % 10 == 0 {
                        let result = tokio::time::timeout(
                            Duration::from_secs(5),
                            health_state.remote.dsh_console_status(&health_state.lease),
                        )
                        .await;
                        if !matches!(result, Ok(Ok(()))) {
                            let _ = health_state.stop.send(true);
                            break;
                        }
                    }
                }
            });
            let slots = Arc::new(Semaphore::new(32));
            let mut connections = JoinSet::new();
            loop {
                tokio::select! {
                    _ = stopped.changed() => break,
                    Some(_) = connections.join_next(), if !connections.is_empty() => {},
                    accepted = listener.accept() => {
                        let Ok((stream, peer)) = accepted else { break; };
                        if !peer.ip().is_loopback() { continue; }
                        let Ok(permit) = slots.clone().try_acquire_owned() else { continue; };
                        let session = state.clone();
                        let permit = Arc::new(permit);
                        let mut connection_stop = state.stop.subscribe();
                        connections.spawn(async move {
                            let upstream = Arc::new(tokio::sync::Mutex::new(None::<HttpConnection>));
                            let idle_upstream = upstream.clone();
                            let service = service_fn(move |request| handle(request, session.clone(), permit.clone(), upstream.clone()));
                            let connection = hyper::server::conn::http1::Builder::new().max_buf_size(32_768)
                                .timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_secs(15))
                                .serve_connection(TokioIo::new(stream), service).with_upgrades();
                            let idle = async {
                                loop {
                                    tokio::time::sleep(Duration::from_secs(1)).await;
                                    if let Ok(mut slot) = idle_upstream.try_lock() {
                                        if slot.as_ref().is_some_and(|value| value.last_used.elapsed() >= Duration::from_secs(HTTP_IDLE_SECONDS)) {
                                            slot.take();
                                        }
                                    }
                                }
                            };
                            tokio::select! { _ = connection_stop.changed() => {}, _ = connection => {}, _ = idle => {} }
                        });
                    }
                }
            }
            state.running.store(false, Ordering::Release);
            let _ = state.stop.send(true);
            drop(listener);
            health.abort();
            let _ = health.await;
            connections.shutdown().await;
            state.remote.close_dsh_console(&state.lease).await;
        });
        Ok(Self {
            session,
            task: Some(task),
        })
    }

    pub fn running(&self) -> bool {
        self.session.running.load(Ordering::Acquire) && Instant::now() < self.session.expires_at
    }
    pub fn origin(&self) -> &str {
        &self.session.origin
    }

    /// bootstrap_url（一次性浏览器入口）：仅交给原生系统打开操作，不返回产品网页。
    pub fn bootstrap_url(&self) -> Result<String, String> {
        if !self.running() {
            return Err("远程控制台已停止，请重新打开。".into());
        }
        let mut tickets = self
            .session
            .tickets
            .lock()
            .map_err(|_| "控制台登录交接状态不可用。")?;
        tickets.retain(|_, expiry| *expiry > Instant::now());
        if tickets.len() >= 16 {
            return Err("控制台登录交接次数过多，请稍后重试。".into());
        }
        let ticket = nonce();
        tickets.insert(ticket.clone(), Instant::now() + Duration::from_secs(60));
        Ok(format!("{}{BOOTSTRAP_PATH}{ticket}", self.session.origin))
    }

    pub async fn check(&self) -> Result<(), String> {
        self.session
            .remote
            .dsh_console_status(&self.session.lease)
            .await
    }

    pub async fn stop(mut self) {
        let _ = self.session.stop.send(true);
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}

impl Drop for DshConsoleForwarder {
    fn drop(&mut self) {
        let _ = self.session.stop.send(true);
    }
}

async fn handle(
    mut request: Request<Incoming>,
    session: Arc<BrowserSession>,
    permit: Arc<OwnedSemaphorePermit>,
    upstream: Arc<tokio::sync::Mutex<HttpConnectionSlot>>,
) -> Result<Response<Body>, Infallible> {
    if !session.running.load(Ordering::Acquire) || Instant::now() >= session.expires_at {
        return Ok(error(
            StatusCode::GONE,
            "远程控制台已停止，请在语音终端中重新打开。",
        ));
    }
    if request.uri().scheme().is_some()
        || request.uri().authority().is_some()
        || !trusted_origin(&request, &session)
    {
        return Ok(error(StatusCode::FORBIDDEN, "控制台网页来源不匹配。"));
    }
    if let Some(ticket) = request.uri().path().strip_prefix(BOOTSTRAP_PATH) {
        let consumed = request.method() == hyper::Method::GET
            && request.uri().query().is_none()
            && session
                .tickets
                .lock()
                .ok()
                .and_then(|mut tickets| tickets.remove(ticket))
                .is_some_and(|expiry| expiry > Instant::now());
        if !consumed {
            return Ok(error(
                StatusCode::UNAUTHORIZED,
                "控制台登录入口已过期或已经使用。",
            ));
        }
        let mut response = error(StatusCode::SEE_OTHER, "正在打开 DSH 控制台。");
        response
            .headers_mut()
            .insert(header::LOCATION, header::HeaderValue::from_static("/"));
        response.headers_mut().insert(
            header::SET_COOKIE,
            format!(
                "{}={}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}",
                session.cookie_name, session.cookie
            )
            .parse()
            .unwrap(),
        );
        return Ok(response);
    }
    if !has_cookie(&request, &session) {
        return Ok(error(
            StatusCode::UNAUTHORIZED,
            "请从语音终端的代理控制台按钮打开此网页。",
        ));
    }
    let upgrade = request
        .headers()
        .get(header::UPGRADE)
        .is_some_and(|value| value.as_bytes().eq_ignore_ascii_case(b"websocket"));
    if request.method() == hyper::Method::CONNECT
        || (request.headers().contains_key(header::UPGRADE) && !upgrade)
    {
        return Ok(error(
            StatusCode::BAD_REQUEST,
            "控制台仅支持网页请求及 WebSocket（双向实时连接）。",
        ));
    }
    let browser_upgrade = upgrade.then(|| hyper::upgrade::on(&mut request));
    // 客户端凭据不进入服务端 DSH；插件自有 Cookie（网页状态）继续透传。
    let cookies = request
        .headers()
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(';'))
        .map(str::trim)
        .filter(|part| {
            !part.split('=').next().is_some_and(|name| {
                name.starts_with("voice-dsh-console-") || name.starts_with("dsh-auth-")
            })
        })
        .collect::<Vec<_>>()
        .join("; ");
    request.headers_mut().remove(header::COOKIE);
    if !cookies.is_empty() {
        if let Ok(value) = cookies.parse() {
            request.headers_mut().insert(header::COOKIE, value);
        }
    }
    request.headers_mut().remove(header::EXPECT);
    if !session.remote.dsh_console_current(&session.lease).await {
        return Ok(error(
            StatusCode::GONE,
            "共享主机连接已变化，请重新打开控制台。",
        ));
    }
    let mut slot = upstream.lock_owned().await;
    if !session.remote.dsh_console_current(&session.lease).await {
        slot.take();
        return Ok(error(
            StatusCode::GONE,
            "共享主机连接已变化，请重新打开控制台。",
        ));
    }
    if slot.as_ref().is_some_and(|value| {
        value.sender.is_closed()
            || value.driver.is_finished()
            || value.last_used.elapsed() >= Duration::from_secs(HTTP_IDLE_SECONDS)
    }) {
        slot.take();
    }
    if slot.is_none() {
        let tunnel = match session.remote.dsh_console_tunnel(&session.lease).await {
            Ok(tunnel) => tunnel,
            Err(_) => {
                return Ok(error(
                    StatusCode::BAD_GATEWAY,
                    "远程 DSH 连接已中断，请重新打开控制台。",
                ))
            }
        };
        let (sender, connection) = match hyper::client::conn::http1::Builder::new()
            .max_buf_size(32_768)
            .handshake(TokioIo::new(tunnel))
            .await
        {
            Ok(value) => value,
            Err(_) => return Ok(error(StatusCode::BAD_GATEWAY, "无法建立 DSH 网页连接。")),
        };
        let mut connection_stop = session.stop.subscribe();
        let driver = tokio::spawn(async move {
            tokio::select! { _ = connection_stop.changed() => {}, _ = connection.with_upgrades() => {} }
        });
        *slot = Some(HttpConnection {
            sender,
            driver,
            last_used: Instant::now(),
        });
    }
    let connection = slot.as_mut().expect("已建立当前浏览器的上游连接");
    if connection.sender.ready().await.is_err() {
        slot.take();
        return Ok(error(
            StatusCode::BAD_GATEWAY,
            "DSH 网页连接已结束，请重新操作；业务请求没有重发。",
        ));
    }
    let mut response = match connection.sender.send_request(request).await {
        Ok(response) => response,
        Err(_) => {
            slot.take();
            return Ok(error(
                StatusCode::BAD_GATEWAY,
                "DSH 网页请求未完整完成；写入操作不会自动重试。",
            ));
        }
    };
    if response.status() == StatusCode::SWITCHING_PROTOCOLS {
        let Some(browser_upgrade) = browser_upgrade else {
            slot.take();
            return Ok(error(
                StatusCode::BAD_GATEWAY,
                "DSH 返回了未申请的协议升级。",
            ));
        };
        let upstream_upgrade = hyper::upgrade::on(&mut response);
        let connection = slot.take(); // connection（升级后由字节流中继持有，不再当作普通 HTTP 复用）。
        drop(slot);
        let mut stopped = session.stop.subscribe();
        tokio::spawn(async move {
            let _permit = permit;
            let _connection = connection;
            let relay = async {
                let (browser, upstream) = tokio::try_join!(browser_upgrade, upstream_upgrade)?;
                let _ = tokio::io::copy_bidirectional(
                    &mut TokioIo::new(browser),
                    &mut TokioIo::new(upstream),
                )
                .await;
                Ok::<(), hyper::Error>(())
            };
            tokio::select! { _ = stopped.changed() => {}, _ = relay => {} }
        });
        return Ok(response.map(|body| {
            body.map_err(|error| -> BoxError { Box::new(error) })
                .boxed_unsync()
        }));
    }
    Ok(response.map(|body| {
        ForwardedBody::new(body, slot)
            .map_err(|error| -> BoxError { Box::new(error) })
            .boxed_unsync()
    }))
}

fn trusted_origin<B>(request: &Request<B>, session: &BrowserSession) -> bool {
    request
        .headers()
        .get(header::HOST)
        .is_some_and(|value| value.as_bytes() == session.authority.as_bytes())
        && request
            .headers()
            .get(header::ORIGIN)
            .is_none_or(|value| value.as_bytes() == session.origin.as_bytes())
        && !request
            .headers()
            .get("sec-fetch-site")
            .is_some_and(|value| value.as_bytes() == b"cross-site")
}

fn has_cookie<B>(request: &Request<B>, session: &BrowserSession) -> bool {
    let values = request
        .headers()
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(';'))
        .filter_map(|part| part.trim().split_once('='))
        .filter(|(name, _)| *name == session.cookie_name)
        .map(|(_, value)| value)
        .collect::<Vec<_>>();
    values.len() == 1 && values[0] == session.cookie
}

fn error(status: StatusCode, message: &str) -> Response<Body> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .header(header::CACHE_CONTROL, "no-store")
        .header("referrer-policy", "no-referrer")
        .header("x-content-type-options", "nosniff")
        .body(
            Full::new(Bytes::copy_from_slice(message.as_bytes()))
                .map_err(|never| match never {})
                .boxed_unsync(),
        )
        .unwrap()
}

fn nonce() -> String {
    rand::random::<[u8; 32]>()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ssh_device::SshDeviceTarget;
    use russh::{
        keys::{ssh_key::LineEnding, Algorithm, HashAlg, PrivateKey},
        server,
    };
    use std::{
        io::{BufRead, BufReader},
        process::{Child, Command, Stdio},
    };
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;

    struct PythonFixture(Child);
    impl Drop for PythonFixture {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    struct FixedSshServer {
        gateway: std::net::SocketAddr,
        channels: HashMap<russh::ChannelId, russh::Channel<server::Msg>>,
        connections: Arc<std::sync::atomic::AtomicUsize>, // connections（用于确认持久 HTTP 不重复认证的连接计数）。
    }
    impl server::Server for FixedSshServer {
        type Handler = Self;
        fn new_client(&mut self, _: Option<std::net::SocketAddr>) -> Self {
            self.connections.fetch_add(1, Ordering::SeqCst);
            Self {
                gateway: self.gateway,
                channels: HashMap::new(),
                connections: self.connections.clone(),
            }
        }
    }
    impl server::Handler for FixedSshServer {
        type Error = russh::Error;
        async fn auth_publickey(
            &mut self,
            _: &str,
            _: server::VerifiedPublicKey<'_>,
        ) -> Result<server::Auth, Self::Error> {
            Ok(server::Auth::Accept)
        }
        async fn channel_open_session(
            &mut self,
            channel: russh::Channel<server::Msg>,
            reply: server::ChannelOpenHandle,
            _: &mut server::Session,
        ) -> Result<(), Self::Error> {
            self.channels.insert(channel.id(), channel);
            reply.accept().await;
            Ok(())
        }
        async fn exec_request(
            &mut self,
            id: russh::ChannelId,
            data: &[u8],
            session: &mut server::Session,
        ) -> Result<(), Self::Error> {
            if data != b"voice-terminal-v1-control" {
                session.channel_failure(id)?;
                session.close(id)?;
                return Ok(());
            }
            session.channel_success(id)?;
            if let Some(channel) = self.channels.remove(&id) {
                let gateway = self.gateway;
                tokio::spawn(async move {
                    if let Ok(mut target) = tokio::net::TcpStream::connect(gateway).await {
                        let _ =
                            tokio::io::copy_bidirectional(&mut channel.into_stream(), &mut target)
                                .await;
                    }
                });
            }
            Ok(())
        }
    }

    async fn fixture(public_key: &str) -> (PythonFixture, serde_json::Value) {
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
            .unwrap();
        let reports = root.join(".local/cache/reports");
        std::fs::create_dir_all(&reports).unwrap();
        let log = std::fs::File::create(reports.join("dsh-console-e2e-stderr.log")).unwrap();
        let child = Command::new(root.join(".local/runtimes/python/python.exe"))
            .args(["-B", "-X", "utf8"])
            .arg(root.join("tests/fixtures/dsh-console-e2e-server.py"))
            .current_dir(&root)
            .env("VOICE_DEVICE_E2E_ID", "test-device")
            .env("VOICE_DEVICE_E2E_PUBLIC_KEY", public_key)
            .stdout(Stdio::piped())
            .stderr(Stdio::from(log))
            .spawn()
            .unwrap();
        let mut process = PythonFixture(child);
        let stdout = process.0.stdout.take().unwrap();
        let line = tokio::time::timeout(
            Duration::from_secs(60),
            tokio::task::spawn_blocking(move || {
                let mut line = String::new();
                BufReader::new(stdout).read_line(&mut line).unwrap();
                line
            }),
        )
        .await
        .unwrap()
        .unwrap();
        let value = serde_json::from_str(&line).expect("隔离 Python 网关应报告动态端口，错误见 .local/cache/reports/dsh-console-e2e-stderr.log");
        (process, value)
    }

    async fn browser_client(origin: &str) -> hyper::client::conn::http1::SendRequest<Full<Bytes>> {
        let stream = tokio::net::TcpStream::connect(origin.strip_prefix("http://").unwrap())
            .await
            .unwrap();
        let (sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
            .await
            .unwrap();
        tokio::spawn(async move {
            let _ = connection.await;
        });
        sender
    }

    async fn request(
        sender: &mut hyper::client::conn::http1::SendRequest<Full<Bytes>>,
        origin: &str,
        method: &str,
        path: &str,
        cookie: Option<&str>,
        body: Bytes,
    ) -> (StatusCode, hyper::HeaderMap, Bytes) {
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header(header::HOST, origin.strip_prefix("http://").unwrap());
        if let Some(cookie) = cookie {
            request = request.header(header::COOKIE, cookie);
        }
        let response = tokio::time::timeout(
            Duration::from_secs(60),
            sender.send_request(request.body(Full::new(body)).unwrap()),
        )
        .await
        .expect("网页请求应在限定时间内返回")
        .unwrap();
        let (parts, body) = response.into_parts();
        let body = body.collect().await.unwrap().to_bytes();
        (parts.status, parts.headers, body)
    }

    async fn control(port: u16, path: &str) -> Bytes {
        let origin = format!("http://127.0.0.1:{port}");
        let mut client = browser_client(&origin).await;
        request(&mut client, &origin, "POST", path, None, Bytes::new())
            .await
            .2
    }

    #[tokio::test]
    #[ignore = "需要显式选择真实已配对客户端，仅采样网页读取，不执行模型或设置写入"]
    async fn paired_client_console_http_refresh_timing_uses_the_saved_identity() {
        let root = std::env::var_os("VOICE_TEST_PAIRED_CLIENT_ROOT") // VOICE_TEST_PAIRED_CLIENT_ROOT（真实客户端安装目录）。
            .map(std::path::PathBuf::from)
            .expect("必须显式选择真实客户端安装目录");
        let data = root.join("data");
        let config = match crate::device_config::load_from_root(&data) {
            crate::device_config::LoadResult::Valid(config) => config,
            _ => panic!("客户端设备配置无效"),
        };
        assert_eq!(config.mode, crate::device_config::RunMode::Remote);
        let host = config.host.expect("客户端尚未配对");
        let manager = Arc::new(SshDeviceManager::default());
        manager.set_key_root(&data).unwrap();
        manager
            .connect(
                &host.credential_ref,
                host.transport_credential_ref.as_deref(),
                &config.device_id,
                SshDeviceTarget {
                    address: host.address,
                    port: host.port,
                    username: host.username,
                    host_key_sha256: host.host_key_sha256,
                    gateway_port: crate::ssh_device::DEFAULT_GATEWAY_PORT,
                },
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        let forwarder = DshConsoleForwarder::start(manager.clone()).await.unwrap();
        let origin = forwarder.origin().to_owned();
        let bootstrap = forwarder.bootstrap_url().unwrap();
        let mut client = browser_client(&origin).await;
        let (status, headers, _) = request(
            &mut client,
            &origin,
            "GET",
            bootstrap.strip_prefix(&origin).unwrap(),
            None,
            Bytes::new(),
        )
        .await;
        assert_eq!(status, StatusCode::SEE_OTHER);
        let cookie = headers[header::SET_COOKIE]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap()
            .to_owned();
        let mut durations = Vec::new();
        let mut sizes = Vec::new();
        for _ in 0..5 {
            let started = Instant::now();
            let (status, _, body) = request(
                &mut client,
                &origin,
                "GET",
                "/",
                Some(&cookie),
                Bytes::new(),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
            assert!(String::from_utf8_lossy(&body).contains("__DSH_BOOT__"));
            durations.push(started.elapsed().as_millis() as u64);
            sizes.push(body.len());
        }
        forwarder.stop().await;
        manager.disconnect().await.unwrap();
        println!(
            "真实局域网页读取采样：{}",
            serde_json::json!({"http_ms":durations,"body_bytes":sizes,
            "fields":{"http_ms":"同一客户端网页连接上五次原站读取的完整耗时毫秒","body_bytes":"各次原站文档字节数"}})
        );
    }

    #[tokio::test]
    async fn original_web_ui_over_pinned_ssh_supports_streams_tabs_revocation_and_restart() {
        let product = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let transport = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let (mut process, ports) = fixture(&product.public_key().to_openssh().unwrap()).await;
        let gateway_port = ports["gateway_port"].as_u64().unwrap() as u16;
        let control_port = ports["control_port"].as_u64().unwrap() as u16;
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let config = Arc::new(server::Config {
            keys: vec![host_key],
            auth_rejection_time: Duration::ZERO,
            auth_rejection_time_initial: Some(Duration::ZERO),
            ..Default::default()
        });
        let connections = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let mut server = FixedSshServer {
            gateway: ([127, 0, 0, 1], gateway_port).into(),
            channels: HashMap::new(),
            connections: connections.clone(),
        };
        let (handle_tx, handle_rx) = tokio::sync::oneshot::channel();
        let ssh = tokio::spawn(async move {
            let running = server::Server::run_on_socket(&mut server, config, &listener);
            let _ = handle_tx.send(running.handle());
            running.await
        });
        let shutdown = handle_rx.await.unwrap();
        let manager = Arc::new(SshDeviceManager::default());
        manager
            .connect_with_test_credentials(
                "test-device",
                SshDeviceTarget {
                    address: "127.0.0.1".into(),
                    port: address.port(),
                    username: "test-device".into(),
                    host_key_sha256: fingerprint,
                    gateway_port,
                },
                &product.to_openssh(LineEnding::LF).unwrap(),
                &transport.to_openssh(LineEnding::LF).unwrap(),
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        let forwarder = DshConsoleForwarder::start(manager.clone()).await.unwrap();
        let origin = forwarder.origin().to_owned();
        let mut client = browser_client(&origin).await;
        assert_eq!(
            request(&mut client, &origin, "GET", "/", None, Bytes::new())
                .await
                .0,
            StatusCode::UNAUTHORIZED
        );
        let bootstrap = forwarder.bootstrap_url().unwrap();
        let path = bootstrap.strip_prefix(&origin).unwrap();
        let (status, headers, _) =
            request(&mut client, &origin, "GET", path, None, Bytes::new()).await;
        assert_eq!(status, StatusCode::SEE_OTHER);
        assert_eq!(headers[header::LOCATION], "/");
        let cookie = headers[header::SET_COOKIE]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap()
            .to_owned();
        assert!(!cookie.contains("dsh-auth"));
        assert_eq!(
            request(&mut client, &origin, "GET", path, None, Bytes::new())
                .await
                .0,
            StatusCode::UNAUTHORIZED
        );
        let (status, _, html) = request(
            &mut client,
            &origin,
            "GET",
            "/",
            Some(&cookie),
            Bytes::new(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert!(String::from_utf8_lossy(&html).contains("plugins/fixture/client.js"));
        let after_first = connections.load(Ordering::SeqCst);
        let (status, headers, script) = request(
            &mut client,
            &origin,
            "GET",
            "/plugins/??fixture/client.js,extra/client.js&rev=a%2Fb&x=1&x=2",
            Some(&cookie),
            Bytes::new(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert!(!headers.contains_key(header::SET_COOKIE));
        assert_eq!(script, "globalThis.fixturePluginLoaded=true;");
        assert_eq!(
            connections.load(Ordering::SeqCst),
            after_first,
            "同一浏览器连接的下一 HTTP 请求应复用已授权字节流"
        );
        let payload = Bytes::from((0u8..=255).cycle().take(2_097_408).collect::<Vec<_>>());
        let (_, _, download) = request(
            &mut client,
            &origin,
            "GET",
            "/api/download",
            Some(&cookie),
            Bytes::new(),
        )
        .await;
        assert_eq!(download, payload);
        let (_, _, upload) = request(
            &mut client,
            &origin,
            "POST",
            "/api/upload",
            Some(&cookie),
            payload.clone(),
        )
        .await;
        assert_eq!(upload, payload);
        assert_eq!(
            connections.load(Ordering::SeqCst),
            after_first,
            "完整二进制上传和下载结束后仍可复用同一字节流"
        );
        tokio::time::sleep(Duration::from_secs(HTTP_IDLE_SECONDS + 1)).await;
        let before_reopen = connections.load(Ordering::SeqCst);
        assert_eq!(
            request(
                &mut client,
                &origin,
                "GET",
                "/",
                Some(&cookie),
                Bytes::new()
            )
            .await
            .0,
            StatusCode::OK
        );
        assert!(
            connections.load(Ordering::SeqCst) > before_reopen,
            "空闲回收后，应在下一新请求写出前建立新隧道"
        );
        // 连续放弃正在下载的浏览器连接，不得耗尽服务端十六条隧道名额。
        for _ in 0..18 {
            let mut abandoned =
                tokio::net::TcpStream::connect(origin.strip_prefix("http://").unwrap())
                    .await
                    .unwrap();
            abandoned
                .write_all(
                    format!(
                        "GET /api/download HTTP/1.1\r\nHost: {}\r\nCookie: {cookie}\r\n\r\n",
                        origin.strip_prefix("http://").unwrap()
                    )
                    .as_bytes(),
                )
                .await
                .unwrap();
            let mut status = [0u8; 12];
            abandoned.read_exact(&mut status).await.unwrap();
            assert_eq!(&status, b"HTTP/1.1 200");
            drop(abandoned);
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let mut after_cancellation = browser_client(&origin).await;
        assert_eq!(
            request(
                &mut after_cancellation,
                &origin,
                "GET",
                "/",
                Some(&cookie),
                Bytes::new()
            )
            .await
            .0,
            StatusCode::OK
        );
        drop(after_cancellation);

        let mut first = format!("{}/api/remote.mux", origin.replace("http:", "ws:"))
            .into_client_request()
            .unwrap();
        first
            .headers_mut()
            .insert(header::COOKIE, cookie.parse().unwrap());
        first
            .headers_mut()
            .insert(header::ORIGIN, origin.parse().unwrap());
        let mut second = first.clone();
        second.headers_mut().insert(
            "sec-websocket-key",
            "dGhlIHNhbXBsZSBub25jZQ==".parse().unwrap(),
        );
        let (mut tab_one, _) = tokio_tungstenite::connect_async(first).await.unwrap();
        let (mut tab_two, _) = tokio_tungstenite::connect_async(second).await.unwrap();
        tab_one
            .send(Message::Binary(payload.clone()))
            .await
            .unwrap();
        assert_eq!(
            tab_one.next().await.unwrap().unwrap(),
            Message::Binary(payload.clone())
        );
        tab_two
            .send(Message::Binary(Bytes::from_static(b"independent-tab")))
            .await
            .unwrap();
        assert_eq!(
            tab_two.next().await.unwrap().unwrap(),
            Message::Binary(Bytes::from_static(b"independent-tab"))
        );
        control(control_port, "/restart").await;
        let old = tokio::time::timeout(Duration::from_secs(5), tab_one.next())
            .await
            .unwrap();
        assert!(!matches!(old, Some(Ok(Message::Binary(_)))));
        assert!(request(
            &mut client,
            &origin,
            "GET",
            "/",
            Some(&cookie),
            Bytes::new()
        )
        .await
        .0
        .is_server_error());
        forwarder.stop().await;
        assert!(
            tokio::net::TcpStream::connect(origin.strip_prefix("http://").unwrap())
                .await
                .is_err()
        );

        let reopened = DshConsoleForwarder::start(manager.clone()).await.unwrap();
        let bootstrap = reopened.bootstrap_url().unwrap();
        let mut fresh = browser_client(reopened.origin()).await;
        let (_, headers, _) = request(
            &mut fresh,
            reopened.origin(),
            "GET",
            bootstrap.strip_prefix(reopened.origin()).unwrap(),
            None,
            Bytes::new(),
        )
        .await;
        let new_cookie = headers[header::SET_COOKIE]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap();
        control(control_port, "/revoke").await;
        assert!(request(
            &mut fresh,
            reopened.origin(),
            "GET",
            "/",
            Some(new_cookie),
            Bytes::new()
        )
        .await
        .0
        .is_server_error());
        reopened.stop().await;
        if std::env::var_os("VOICE_TEST_REAL_DSH_BROWSER").is_some() {
            let result: serde_json::Value =
                serde_json::from_slice(&control(control_port, "/real-host").await).unwrap();
            assert_eq!(result["ready"], true, "隔离真实 DSH 应正常加载正式插件");
            let real = DshConsoleForwarder::start(manager.clone()).await.unwrap();
            let url = real.bootstrap_url().unwrap();
            let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../..")
                .canonicalize()
                .unwrap();
            let plain_root =
                std::path::PathBuf::from(root.to_string_lossy().trim_start_matches(r"\\?\"));
            let browser_temp = result["browser_temp"]
                .as_str()
                .expect("浏览器临时目录由隔离夹具分配");
            let output = tokio::process::Command::new("node")
                .arg(plain_root.join("tests/fixtures/dsh-console-browser-check.mjs"))
                .env("VOICE_TEST_BROWSER_URL", &url)
                .env("TEMP", browser_temp)
                .env("TMP", browser_temp)
                .current_dir(plain_root)
                .output()
                .await
                .unwrap();
            assert!(
                output.status.success(),
                "真实 DSH 网页验证失败：{}",
                String::from_utf8_lossy(&output.stderr).replace(&url, "[一次性网页入口]")
            );
            println!(
                "真实 DSH 浏览器验证：{}",
                String::from_utf8_lossy(&output.stdout)
            );
            real.stop().await;
        }
        manager.disconnect().await.unwrap();
        control(control_port, "/stop").await;
        shutdown.shutdown("原生网页转发集成测试结束".into());
        let _ = tokio::time::timeout(Duration::from_secs(5), ssh)
            .await
            .unwrap();
        for _ in 0..600 {
            if process.0.try_wait().unwrap().is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert!(
            process.0.try_wait().unwrap().is_some(),
            "隔离测试服务应完成资源回收后正常退出"
        );
    }
}
