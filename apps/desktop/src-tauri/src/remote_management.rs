//! RemoteManagementService（远程管理服务）：独立于 Python 核心的固定原生资源 RPC（远程过程调用）。
use crate::remote_import::RemoteImportCoordinator;
use crate::resource_commands::{valid_resource_id, ResourceManager};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Write},
    net::{Shutdown, SocketAddr, TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Condvar, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use voice_desktop_common::{
    device_management_contract::{
        valid_host_id, valid_request_id, DeviceBridgePrelude, RemoteManagementOpenRequest,
        RemoteManagementOpenResponse, RemoteManagementRequest, RemoteManagementResponse,
        DEVICE_MANAGEMENT_PROTOCOL_VERSION,
    },
    device_trust::DeviceTrustStore,
};

const DEFAULT_REMOTE_MANAGEMENT_ADDRESS: &str = "127.0.0.1:18768"; // DEFAULT_REMOTE_MANAGEMENT_ADDRESS（仅回环监听地址）。
const MAX_OPEN_LINE_BYTES: usize = 16 * 1024; // MAX_OPEN_LINE_BYTES（握手行最大字节数）。
const MAX_REQUEST_LINE_BYTES: usize = 528 * 1024; // MAX_REQUEST_LINE_BYTES（512 KiB 参数上限外加管理信封空间）。
const MAX_RESPONSE_LINE_BYTES: usize = 2 * 1024 * 1024; // MAX_RESPONSE_LINE_BYTES（管理响应行最大字节数）。
const SESSION_TTL: Duration = Duration::from_secs(15 * 60); // SESSION_TTL（管理会话存活时间）。
const REQUEST_RESULT_TTL: Duration = Duration::from_secs(24 * 60 * 60); // REQUEST_RESULT_TTL（请求幂等结果保留时间）。
const MAX_SESSIONS: usize = 256; // MAX_SESSIONS（服务进程内管理会话容量）。
const MAX_REQUEST_RESULTS: usize = 512; // MAX_REQUEST_RESULTS（服务进程内操作结果容量）。
const MAX_CONNECTIONS: usize = 64; // MAX_CONNECTIONS（同时打开的本机桥接连接上限）。

#[derive(Clone)]
struct ManagedSession {
    client_device_id: String, // client_device_id（经 SSH forced-command（强制命令）绑定的客户端设备编号）。
    expires_at: Instant,      // expires_at（管理会话到期时刻）。
}

struct OpenResult {
    request: RemoteManagementOpenRequest, // request（原始会话申请，用于同编号参数冲突检查）。
    response: RemoteManagementOpenResponse, // response（原始服务端签发结果）。
    expires_at: Instant,                  // expires_at（该申请幂等结果保留时刻）。
}

struct RequestResult {
    request: RemoteManagementRequest,   // request（原始管理操作请求）。
    response: RemoteManagementResponse, // response（原始管理操作结果）。
    client_device_id: String,           // client_device_id（操作发起方经 SSH 验证的设备编号）。
    expires_at: Instant,                // expires_at（请求幂等结果保留时刻）。
}

struct InFlightRequest {
    request: RemoteManagementRequest, // request（正在执行的原始操作请求）。
    client_device_id: String,         // client_device_id（正在执行操作的已认证客户端）。
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SshEnableDecision {
    StatusOnly,      // StatusOnly（仅状态回执）：服务已就绪，不提交系统管理任务。
    StartManagedJob, // StartManagedJob（提交受管任务）：仅在用户明确确认且状态未就绪时执行。
}

struct ServiceState {
    next_generation: u64, // next_generation（下一个管理会话代次）。
    sessions: HashMap<u64, ManagedSession>, // sessions（当前有效管理会话）。
    opens: HashMap<String, OpenResult>, // opens（会话申请幂等结果）。
    inflight: HashMap<String, InFlightRequest>, // inflight（执行中的操作编号及参数）。
    requests: HashMap<String, RequestResult>, // requests（资源操作幂等结果）。
}

/// RemoteManagementService（远程管理服务）：使用原生 ResourceManager（资源管理器），不调用 Python 网关。
pub struct RemoteManagementService {
    manager: Arc<ResourceManager>, // manager（现有原生资源事务管理器）。
    target_host_id: String,        // target_host_id（本机服务端稳定编号）。
    trust_store: Arc<DeviceTrustStore>, // trust_store（主会话提供的原生设备信任快照）。
    import_coordinator: Arc<RemoteImportCoordinator>, // import_coordinator（校验 FILE 通道设备绑定和摘要的导入协调器）。
    state: Mutex<ServiceState>,                       // state（管理会话和幂等记录）。
    requests_changed: Condvar, // requests_changed（唤醒等待同一操作首次分派结果的重试连接）。
}

/// RemoteManagementHandle（远程管理服务句柄）：负责停止回环 listener（监听器）及其连接。
pub struct RemoteManagementHandle {
    address: SocketAddr,   // address（服务实际绑定的回环地址）。
    stop: Arc<AtomicBool>, // stop（listener 停止信号）。
    connections: Arc<Mutex<HashMap<u64, TcpStream>>>, // connections（可由停止流程关闭的连接）。
    workers: Arc<Mutex<Vec<JoinHandle<()>>>>, // workers（已接受连接的工作线程）。
    accept_worker: Option<JoinHandle<()>>, // accept_worker（listener 接受线程）。
}

impl RemoteManagementService {
    /// new（创建服务）：接收原生资源管理器、服务端编号、已打开的设备信任存储和远程文件导入协调器。
    pub fn new(
        manager: Arc<ResourceManager>,
        target_host_id: String,
        trust_store: Arc<DeviceTrustStore>,
        import_coordinator: Arc<RemoteImportCoordinator>,
    ) -> Result<Arc<Self>, String> {
        if !valid_host_id(&target_host_id) {
            return Err("远程管理服务端编号无效。".into());
        }
        if trust_store.snapshot().state == "invalid" {
            return Err("设备信任登记无效；远程管理服务拒绝启动。".into());
        }
        let generation = generation_seed();
        Ok(Arc::new(Self {
            manager,
            target_host_id,
            trust_store,
            import_coordinator,
            state: Mutex::new(ServiceState {
                next_generation: generation,
                sessions: HashMap::new(),
                opens: HashMap::new(),
                inflight: HashMap::new(),
                requests: HashMap::new(),
            }),
            requests_changed: Condvar::new(),
        }))
    }

    /// start（启动服务）：只允许绑定 loopback（回环）地址；默认地址为 127.0.0.1:18768。
    pub fn start(
        self: &Arc<Self>,
        bind_addr: SocketAddr,
    ) -> Result<RemoteManagementHandle, String> {
        if !bind_addr.ip().is_loopback() {
            return Err("远程管理服务仅允许绑定本机回环地址。".into());
        }
        let listener = TcpListener::bind(bind_addr)
            .map_err(|error| format!("无法启动远程管理回环服务：{error}"))?;
        listener
            .set_nonblocking(true)
            .map_err(|_| "无法配置远程管理回环 listener。".to_owned())?;
        let address = listener
            .local_addr()
            .map_err(|_| "无法读取远程管理服务监听地址。")?;
        if !address.ip().is_loopback() {
            return Err("远程管理服务意外绑定到了非回环地址。".into());
        }

        let stop = Arc::new(AtomicBool::new(false));
        let connections = Arc::new(Mutex::new(HashMap::<u64, TcpStream>::new()));
        let workers = Arc::new(Mutex::new(Vec::<JoinHandle<()>>::new()));
        let service = Arc::clone(self);
        let thread_stop = Arc::clone(&stop);
        let thread_connections = Arc::clone(&connections);
        let thread_workers = Arc::clone(&workers);
        let accept_worker = thread::Builder::new()
            .name("remote-management-listener".into())
            .spawn(move || {
                static NEXT_CONNECTION: AtomicU64 = AtomicU64::new(1);
                let mut last_cleanup = Instant::now();
                while !thread_stop.load(Ordering::Acquire) {
                    if last_cleanup.elapsed() >= Duration::from_secs(15) {
                        let _ = service.manager.cleanup_remote_imports();
                        service.prune_expired_state();
                        last_cleanup = Instant::now();
                    }
                    match listener.accept() {
                        Ok((stream, peer)) => {
                            if !peer.ip().is_loopback() {
                                continue;
                            }
                            let connection_id = NEXT_CONNECTION.fetch_add(1, Ordering::Relaxed);
                            let Ok(shutdown_stream) = stream.try_clone() else {
                                continue;
                            };
                            let registered = if let Ok(mut active) = thread_connections.lock() {
                                if active.len() >= MAX_CONNECTIONS {
                                    false
                                } else {
                                    active.insert(connection_id, shutdown_stream);
                                    true
                                }
                            } else {
                                false
                            };
                            if !registered {
                                let _ = stream.shutdown(Shutdown::Both);
                                continue;
                            }
                            if let Ok(mut active_workers) = thread_workers.lock() {
                                let mut pending = Vec::new();
                                for worker in active_workers.drain(..) {
                                    if worker.is_finished() {
                                        let _ = worker.join();
                                    } else {
                                        pending.push(worker);
                                    }
                                }
                                *active_workers = pending;
                                let connection_service = Arc::clone(&service);
                                let connection_map = Arc::clone(&thread_connections);
                                if let Ok(worker) = thread::Builder::new()
                                    .name("remote-management-connection".into())
                                    .spawn(move || {
                                        let _ = connection_service.serve_connection(stream, None);
                                        if let Ok(mut active) = connection_map.lock() {
                                            active.remove(&connection_id);
                                        }
                                    })
                                {
                                    active_workers.push(worker);
                                } else if let Ok(mut active) = thread_connections.lock() {
                                    active.remove(&connection_id);
                                }
                            }
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(50));
                        }
                        Err(_) => thread::sleep(Duration::from_millis(100)),
                    }
                }
            })
            .map_err(|error| format!("无法建立远程管理 listener 线程：{error}"))?;

        Ok(RemoteManagementHandle {
            address,
            stop,
            connections,
            workers,
            accept_worker: Some(accept_worker),
        })
    }

    /// start_default（启动默认服务）：只绑定 127.0.0.1:18768。
    pub fn start_default(self: &Arc<Self>) -> Result<RemoteManagementHandle, String> {
        let address = DEFAULT_REMOTE_MANAGEMENT_ADDRESS
            .parse::<SocketAddr>()
            .map_err(|_| "远程管理默认回环地址无效。")?;
        self.start(address)
    }

    /// serve_connection（处理单个 SSH 桥接连接）：先验证 bridge.prelude（桥接前导帧），再处理管理 JSON 行。
    pub fn serve_connection(
        &self,
        mut stream: TcpStream,
        authenticated_client_device_id: Option<String>,
    ) -> Result<(), String> {
        let peer = stream
            .peer_addr()
            .map_err(|_| "远程管理连接缺少本机对端地址。")?;
        if !peer.ip().is_loopback() {
            return Err("远程管理连接不是来自本机 SSH 桥接。".into());
        }
        stream
            .set_nodelay(true)
            .map_err(|_| "无法配置远程管理连接。")?;
        stream
            .set_read_timeout(Some(Duration::from_secs(60)))
            .map_err(|_| "无法配置远程管理读取超时。")?;
        stream
            .set_write_timeout(Some(Duration::from_secs(30)))
            .map_err(|_| "无法配置远程管理写入超时。")?;
        let writer = stream
            .try_clone()
            .map_err(|_| "无法建立远程管理回写通道。")?;
        let mut reader = BufReader::new(stream);

        let prelude_bytes = read_line_bounded(&mut reader, MAX_OPEN_LINE_BYTES)?
            .ok_or("SSH 桥接未提供身份前导帧。")?;
        let prelude: DeviceBridgePrelude =
            serde_json::from_slice(&prelude_bytes).map_err(|_| "SSH 桥接前导帧 JSON 格式无效。")?;
        prelude.validate()?;
        if authenticated_client_device_id
            .as_deref()
            .is_some_and(|id| id != prelude.client_device_id)
        {
            return Err("SSH 桥接前导帧设备编号与强制命令绑定身份不一致。".into());
        }
        self.validate_bridge_identity(&prelude)?;

        let open_bytes = read_line_bounded(&mut reader, MAX_OPEN_LINE_BYTES)?
            .ok_or("远程管理连接未发送会话申请。")?;
        let open_request: RemoteManagementOpenRequest =
            serde_json::from_slice(&open_bytes).map_err(|_| "远程管理会话申请 JSON 格式无效。")?;
        validate_open_request(
            &open_request,
            &self.target_host_id,
            &prelude.client_device_id,
        )?;
        let open_response = self.open_session(open_request)?;
        write_json_line(
            &mut writer
                .try_clone()
                .map_err(|_| "无法写入远程管理会话回执。")?,
            &open_response,
            MAX_RESPONSE_LINE_BYTES,
        )?;

        loop {
            let Some(bytes) = read_line_bounded(&mut reader, MAX_REQUEST_LINE_BYTES)? else {
                return Ok(());
            };
            let request: RemoteManagementRequest = serde_json::from_slice(&bytes)
                .map_err(|_| "远程管理操作 JSON 格式无效；连接已关闭。")?;
            if request.session_generation != open_response.session_generation {
                return Err("管理操作代次不属于当前已认证连接，已拒绝请求。".into());
            }
            // 持续管理通道每次操作重新确认当前构建与设备信任，撤销后不能继续使用旧连接。
            self.validate_bridge_identity(&prelude)?;
            let response = self.dispatch_request(request);
            write_json_line(
                &mut writer
                    .try_clone()
                    .map_err(|_| "无法写入远程管理操作回执。")?,
                &response,
                MAX_RESPONSE_LINE_BYTES,
            )?;
        }
    }

    /// dispatch_request（分派固定资源操作）：不转发到 Python 网关或任意 URL。
    pub fn dispatch_request(&self, request: RemoteManagementRequest) -> RemoteManagementResponse {
        dispatch_with_ledger(
            &self.target_host_id,
            &self.state,
            &self.requests_changed,
            request,
            |request, client| self.dispatch_resource_operation(request, client),
        )
    }

    fn validate_bridge_identity(&self, prelude: &DeviceBridgePrelude) -> Result<(), String> {
        let context = self.manager.context();
        if prelude.workspace_id != context.workspace_id {
            return Err("SSH 桥接工作区编号与当前正式程序不匹配。".into());
        }
        if voice_desktop_common::system_feature::current_user_sid()? != prelude.owner_sid {
            return Err("SSH 桥接账户 SID 与当前 Windows 登录账户不匹配。".into());
        }
        let release = voice_desktop_common::resources::load(&context.program.join("release.json"))?;
        let build = release["build"]
            .as_str()
            .ok_or("当前正式程序缺少有效构建编号。")?;
        if prelude.build != build {
            return Err("SSH 桥接程序构建与当前服务端不匹配。".into());
        }
        if self.trust_store.snapshot().state == "invalid" {
            return Err("原生设备信任存储无效；拒绝 SSH 管理会话。".into());
        }
        let fresh_store = DeviceTrustStore::open(&context.data)?;
        let trust = fresh_store.snapshot();
        if trust.state != "valid"
            || fresh_store
                .trusted_public_key(&prelude.client_device_id)
                .is_none()
        {
            return Err("SSH 桥接设备当前未在原生信任登记中。".into());
        }
        Ok(())
    }

    fn open_session(
        &self,
        request: RemoteManagementOpenRequest,
    ) -> Result<RemoteManagementOpenResponse, String> {
        let now = Instant::now();
        let mut state = self.state.lock().map_err(|_| "远程管理会话状态不可用。")?;
        prune_state(&mut state);
        if let Some(previous) = state.opens.get(&request.request_id) {
            if previous.request != request {
                return Err("同一会话申请编号已用于不同身份或目标。".into());
            }
            if previous.response.expires_at_ms <= unix_millis() {
                return Err("原管理会话已过期，请使用新的会话申请编号。".into());
            }
            return Ok(previous.response.clone());
        }
        if state.sessions.len() >= MAX_SESSIONS {
            return Err("远程管理会话容量已满。".into());
        }
        let generation = state.next_generation;
        state.next_generation = state
            .next_generation
            .checked_add(1)
            .ok_or("管理会话代次耗尽；请重启本机服务。")?;
        let expires_at = now + SESSION_TTL;
        let expires_at_ms = unix_millis().saturating_add(SESSION_TTL.as_millis() as u64);
        let response = RemoteManagementOpenResponse {
            protocol_version: DEVICE_MANAGEMENT_PROTOCOL_VERSION,
            request_id: request.request_id.clone(),
            target_host_id: self.target_host_id.clone(),
            client_device_id: request.client_device_id.clone(),
            session_generation: generation,
            expires_at_ms,
            fields: open_response_fields(),
        };
        state.sessions.insert(
            generation,
            ManagedSession {
                client_device_id: request.client_device_id.clone(),
                expires_at,
            },
        );
        state.opens.insert(
            request.request_id.clone(),
            OpenResult {
                request,
                response: response.clone(),
                expires_at,
            },
        );
        Ok(response)
    }

    fn dispatch_resource_operation(
        &self,
        request: &RemoteManagementRequest,
        authenticated_client_device_id: &str,
    ) -> (u16, Value) {
        let args = &request.arguments;
        match request.operation.as_str() {
            "management.status" if exact_keys(args, &[], &[]) => {
                (200, self.manager.remote_management_status())
            }
            "system.ssh.status" if exact_keys(args, &[], &[]) => {
                match self.manager.open_ssh_server_status(false) {
                    Ok(status) => (
                        200,
                        ssh_status_payload(json!(status), ssh_server_job(&self.manager)),
                    ),
                    Err(error) => (503, error_body(error)),
                }
            }
            "system.ssh.enable" => {
                if !exact_keys(args, &["confirmed"], &[])
                    || args["confirmed"].as_bool() != Some(true)
                {
                    return (
                        400,
                        error_body("启用 OpenSSH Server 必须提供 confirmed: true 的明确用户确认。"),
                    );
                }
                let status = match self.manager.open_ssh_server_status(true) {
                    Ok(status) => status,
                    Err(error) => return (503, error_body(error)),
                };
                let decision = match ssh_enable_decision(args, status.ready) {
                    Ok(decision) => decision,
                    Err(error) => return (400, error_body(error)),
                };
                let job = ssh_server_job(&self.manager);
                if decision == SshEnableDecision::StatusOnly {
                    return (200, ssh_status_payload(json!(status), job));
                }
                if job.as_ref().is_some_and(ssh_job_is_active) {
                    return (202, ssh_status_payload(json!(status), job));
                }
                match self.manager.enable_open_ssh_server() {
                    Ok(_) => (
                        202,
                        ssh_status_payload(json!(status), ssh_server_job(&self.manager)),
                    ),
                    Err(error) => {
                        let active_job = ssh_server_job(&self.manager);
                        if active_job.as_ref().is_some_and(ssh_job_is_active) {
                            (202, ssh_status_payload(json!(status), active_job))
                        } else {
                            (409, error_body(error))
                        }
                    }
                }
            }
            "resource.inventory" if exact_keys(args, &[], &[]) => match self.manager.inventory() {
                Ok(rows) => (200, json!(rows)),
                Err(error) => (503, error_body(error)),
            },
            "resource.jobs" if exact_keys(args, &[], &[]) => (200, json!(self.manager.snapshots())),
            "resource.job.read" if exact_keys(args, &["job_id"], &[]) => {
                let Some(job_id) = args["job_id"]
                    .as_str()
                    .filter(|value| !value.is_empty() && value.len() <= 128)
                else {
                    return (400, error_body("资源任务编号无效。"));
                };
                match self
                    .manager
                    .snapshots()
                    .into_iter()
                    .find(|row| row["job_id"] == job_id)
                {
                    Some(job) => (200, job),
                    None => (404, error_body("找不到指定资源任务。")),
                }
            }
            "resource.job.cancel" if exact_keys(args, &["job_id"], &[]) => {
                let Some(job_id) = args["job_id"]
                    .as_str()
                    .filter(|value| !value.is_empty() && value.len() <= 128)
                else {
                    return (400, error_body("资源任务编号无效。"));
                };
                match self.manager.cancel(job_id) {
                    Ok(()) => (
                        202,
                        json!({"job_id":job_id,"cancel_requested":true,"fields":{"job_id":"已请求取消的服务端任务编号","cancel_requested":"服务端已接受取消请求"}}),
                    ),
                    Err(error) if error.contains("找不到") => (404, error_body(error)),
                    Err(error) => (409, error_body(error)),
                }
            }
            "resource.install.plan" if exact_keys(args, &["resource_id"], &[]) => {
                let Some(resource_id) = args["resource_id"].as_str() else {
                    return (400, error_body("resource_id 必须是字符串。"));
                };
                match self.manager.remote_install_plan(resource_id) {
                    Ok(plan) => (200, plan),
                    Err(error) if error.contains("未知") || error.contains("找不到") => {
                        (404, error_body(error))
                    }
                    Err(error) => (400, error_body(error)),
                }
            }
            "resource.install.start" if exact_keys(args, &["resource_id"], &[]) => {
                let Some(resource_id) = args["resource_id"].as_str() else {
                    return (400, error_body("resource_id 必须是字符串。"));
                };
                match self.manager.download(resource_id.to_owned()) {
                    Ok(job) => (202, job),
                    Err(error) => (409, error_body(error)),
                }
            }
            "resource.install.required" if exact_keys(args, &[], &[]) => {
                match self.manager.download_required() {
                    Ok(job) => (202, job),
                    Err(error) => (409, error_body(error)),
                }
            }
            "resource.job.start" => self.start_resource_job(args),
            "resource.import.source.reserve" if exact_keys(args, &[], &["resource_id"]) => {
                let resource_id = match args.get("resource_id") {
                    None | Some(Value::Null) => None,
                    Some(Value::String(value)) => Some(value.clone()),
                    _ => return (400, error_body("resource_id 必须是字符串或空值。")),
                };
                match self.import_coordinator.reserve_source(
                    &request.request_id,
                    resource_id,
                    authenticated_client_device_id,
                ) {
                    Ok(reservation) => (
                        201,
                        serde_json::to_value(reservation)
                            .unwrap_or_else(|_| error_body("远程导入预留回执编码失败。")),
                    ),
                    Err(error) if error.contains("冲突") || error.contains("同一") => {
                        (409, error_body(error))
                    }
                    Err(error) if error.contains("容量") => (429, error_body(error)),
                    Err(error) => (400, error_body(error)),
                }
            }
            "resource.import.source.commit"
                if exact_keys(args, &["source_id", "manifest"], &[]) =>
            {
                (
                    409,
                    error_body("远程导入清单与完整文件摘要只能由 SSH FILE finish 通道核验并提交。"),
                )
            }
            "resource.import.source.abort" if exact_keys(args, &["source_id"], &[]) => {
                let Some(source_id) = args["source_id"].as_str() else {
                    return (400, error_body("远程导入来源编号必须是字符串。"));
                };
                match self
                    .import_coordinator
                    .abort_reserved_source(source_id, authenticated_client_device_id)
                {
                    Ok(()) => (
                        200,
                        json!({"source_id":source_id,"aborted":true,"fields":{"source_id":"服务端签发的暂存来源编号","aborted":"未提交暂存来源已取消"}}),
                    ),
                    Err(error) if error.contains("不存在") || error.contains("过期") => {
                        (410, error_body(error))
                    }
                    Err(error) => (409, error_body(error)),
                }
            }
            "resource.import.confirm" if exact_keys(args, &["job_id", "accepted"], &[]) => {
                let Some(job_id) = args["job_id"]
                    .as_str()
                    .filter(|value| !value.is_empty() && value.len() <= 128)
                else {
                    return (400, error_body("资源任务编号无效。"));
                };
                let Some(accepted) = args["accepted"].as_bool() else {
                    return (400, error_body("accepted 必须是布尔确认值。"));
                };
                match self.manager.confirm_import(job_id, accepted) {
                    Ok(()) => (
                        202,
                        json!({"job_id":job_id,"accepted":accepted,"fields":{"job_id":"待确认的服务端资源任务编号","accepted":"用户对原生导入识别结果的确认"}}),
                    ),
                    Err(error) => (409, error_body(error)),
                }
            }
            "resource.reset.plan" if exact_keys(args, &["resource_id"], &[]) => {
                let Some(resource_id) = args["resource_id"].as_str() else {
                    return (400, error_body("resource_id 必须是字符串。"));
                };
                match self
                    .manager
                    .remote_reset_plan(&request.request_id, resource_id)
                {
                    Ok(plan) => (200, plan),
                    Err(error) => (409, error_body(error)),
                }
            }
            "resource.reset"
                if exact_keys(
                    args,
                    &["plan_request_id", "resource_id", "token", "confirmed"],
                    &[],
                ) =>
            {
                let (Some(plan_request_id), Some(resource_id), Some(token), Some(confirmed)) = (
                    args["plan_request_id"].as_str(),
                    args["resource_id"].as_str(),
                    args["token"].as_str(),
                    args["confirmed"].as_bool(),
                ) else {
                    return (
                        400,
                        error_body("重置目标、一次性确认值或用户确认参数无效。"),
                    );
                };
                match self.manager.remote_reset(
                    plan_request_id,
                    resource_id.to_owned(),
                    token,
                    confirmed,
                ) {
                    Ok(job) => (202, job),
                    Err(error) => (409, error_body(error)),
                }
            }
            "management.status"
            | "system.ssh.status"
            | "resource.inventory"
            | "resource.jobs"
            | "resource.job.read"
            | "resource.job.cancel"
            | "resource.install.plan"
            | "resource.install.start"
            | "resource.install.required"
            | "resource.import.confirm"
            | "resource.reset.plan"
            | "resource.reset"
            | "resource.import.source.reserve"
            | "resource.import.source.commit"
            | "resource.import.source.abort" => {
                (400, error_body("远程资源操作参数字段不符合固定契约。"))
            }
            _ => (404, error_body("当前原生管理服务未登记该操作。")),
        }
    }

    fn start_resource_job(&self, args: &Value) -> (u16, Value) {
        let Some(kind) = args["kind"].as_str() else {
            return (400, error_body("资源任务类型缺失。"));
        };
        let result = match kind {
            "download" if exact_keys(args, &["kind", "resource_id"], &[]) => {
                let Some(resource_id) = args["resource_id"].as_str() else {
                    return (400, error_body("resource_id 必须是字符串。"));
                };
                if resource_id != "voice-server"
                    && resource_id != voice_desktop_common::runtime::CPU_RESOURCE
                    && resource_id != voice_desktop_common::runtime::GPU_RESOURCE
                    && !valid_resource_id(resource_id)
                {
                    return (400, error_body("资源编号格式无效。"));
                }
                self.manager.download(resource_id.to_owned())
            }
            "required_batch" if exact_keys(args, &["kind"], &[]) => {
                self.manager.download_required()
            }
            "configure_runtime" if exact_keys(args, &["kind", "group"], &[]) => {
                let Some(group) = args["group"].as_str() else {
                    return (400, error_body("group 必须是固定环境组名称。"));
                };
                if !matches!(group, "cpu" | "gpu" | "all" | "base") {
                    return (400, error_body("运行环境组名称不在固定白名单中。"));
                }
                self.manager.configure_runtime(group.to_owned())
            }
            "aniflive_import" => {
                // aniflive_import（仅接续服务端本机已暂存的 AnifLive 转换任务）。
                return start_aniflive_import_job(args, |task_id| {
                    self.manager.start_aniflive_import(task_id)
                });
            }
            "download" | "required_batch" | "configure_runtime" => {
                return (400, error_body("资源任务参数字段不符合固定契约。"))
            }
            _ => return (404, error_body("资源任务类型未登记。")),
        };
        match result {
            Ok(value) => (202, value),
            Err(error)
                if error.contains("已处理中")
                    || error.contains("请等待")
                    || error.contains("已确认") =>
            {
                (409, error_body(error))
            }
            Err(error) => (503, error_body(error)),
        }
    }

    fn prune_expired_state(&self) {
        if let Ok(mut state) = self.state.lock() {
            prune_state(&mut state);
        }
    }
}

fn start_aniflive_import_job<F>(args: &Value, start: F) -> (u16, Value)
where
    F: FnOnce(String) -> Result<Value, String>,
{
    // kind（固定任务类型）与 task_id（服务器已暂存任务编号）是唯一允许字段；客户端不能提供路径。
    if !exact_keys(args, &["kind", "task_id"], &[])
        || args["kind"].as_str() != Some("aniflive_import")
    {
        return (
            400,
            error_body("AnifLive 转换任务必须只提供固定任务类型及 task_id。"),
        );
    }
    let Some(task_id) = args["task_id"].as_str().filter(|value| {
        value.len() == 32
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }) else {
        return (
            400,
            error_body("AnifLive 导入任务编号必须是 32 位小写十六进制值。"),
        );
    };
    match start(task_id.to_owned()) {
        Ok(job) => (202, job),
        Err(error) => (409, error_body(error)),
    }
}

fn ssh_enable_decision(args: &Value, ready: bool) -> Result<SshEnableDecision, String> {
    if !exact_keys(args, &["confirmed"], &[]) || args["confirmed"].as_bool() != Some(true) {
        return Err("启用 OpenSSH Server 必须提供 confirmed: true 的明确用户确认。".into());
    }
    Ok(if ready {
        SshEnableDecision::StatusOnly
    } else {
        SshEnableDecision::StartManagedJob
    })
}

fn ssh_status_payload(status: Value, job: Option<Value>) -> Value {
    json!({
        "status": status,
        "job": job,
        "fields": {
            "status": "只读 OpenSSH Server 系统功能、服务、签名与项目访问状态",
            "job": "固定 OpenSSH Server 管理任务的最新状态；没有任务时为空值",
        }
    })
}

fn ssh_server_job(manager: &ResourceManager) -> Option<Value> {
    let job_id = voice_desktop_common::system_feature::OPENSSH_SERVER_JOB_ID;
    manager
        .snapshots()
        .into_iter()
        .find(|job| job["job_id"] == job_id)
}

fn ssh_job_is_active(job: &Value) -> bool {
    !matches!(
        job["state"].as_str(),
        Some("complete" | "completed" | "failed" | "cancelled" | "canceled")
    )
}

impl RemoteManagementHandle {
    /// local_addr（返回监听地址）：供 main（主入口）记录或测试动态端口。
    pub fn local_addr(&self) -> SocketAddr {
        self.address
    }

    /// is_running（检查管理 listener）：只检查 accept worker（接受线程）的实际线程状态。
    pub fn is_running(&self) -> bool {
        self.accept_worker
            .as_ref()
            .is_some_and(|worker| !worker.is_finished())
    }

    /// stop（停止管理服务）：关闭 listener 与活跃连接并等待工作线程收尾。
    pub fn stop(&mut self) -> Result<(), String> {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.accept_worker.take() {
            worker
                .join()
                .map_err(|_| "远程管理 listener 线程异常退出。".to_owned())?;
        }
        if let Ok(connections) = self.connections.lock() {
            for stream in connections.values() {
                let _ = stream.shutdown(Shutdown::Both);
            }
        }
        if let Ok(mut workers) = self.workers.lock() {
            for worker in workers.drain(..) {
                let _ = worker.join();
            }
        }
        Ok(())
    }
}

impl Drop for RemoteManagementHandle {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn validate_open_request(
    request: &RemoteManagementOpenRequest,
    expected_target_host_id: &str,
    authenticated_client_device_id: &str,
) -> Result<(), String> {
    if request.protocol_version != DEVICE_MANAGEMENT_PROTOCOL_VERSION
        || !valid_request_id(&request.request_id)
        || !valid_host_id(&request.target_host_id)
        || !valid_host_id(&request.client_device_id)
        || request.target_host_id != expected_target_host_id
        || request.client_device_id != authenticated_client_device_id
    {
        return Err("远程管理会话申请版本、目标或 SSH 客户端身份无效。".into());
    }
    let required = [
        "protocol_version",
        "request_id",
        "target_host_id",
        "client_device_id",
    ];
    if request.fields.len() != required.len()
        || required
            .iter()
            .any(|key| !request.fields.contains_key(*key))
        || request
            .fields
            .values()
            .any(|value| value.trim().is_empty() || value.len() > 120)
    {
        return Err("远程管理会话申请缺少逐字段中文说明。".into());
    }
    Ok(())
}

fn exact_keys(args: &Value, required: &[&str], optional: &[&str]) -> bool {
    let Some(object) = args.as_object() else {
        return false;
    };
    required.iter().all(|key| object.contains_key(*key))
        && object
            .keys()
            .all(|key| required.contains(&key.as_str()) || optional.contains(&key.as_str()))
}

fn same_operation(left: &RemoteManagementRequest, right: &RemoteManagementRequest) -> bool {
    left.protocol_version == right.protocol_version
        && left.request_id == right.request_id
        && left.target_host_id == right.target_host_id
        && left.operation == right.operation
        && left.arguments == right.arguments
}

/// dispatch_with_ledger（管理请求准入与幂等账本）：只读状态不挤占写入记录。
fn dispatch_with_ledger(
    target_host_id: &str,
    state_lock: &Mutex<ServiceState>,
    requests_changed: &Condvar,
    request: RemoteManagementRequest,
    dispatch: impl FnOnce(&RemoteManagementRequest, &str) -> (u16, Value),
) -> RemoteManagementResponse {
    let invalid = request.validate().err();
    if let Some(error) = invalid {
        return response(&request, 400, error_body(error));
    }
    if request.target_host_id != target_host_id {
        return response(
            &request,
            403,
            error_body("请求目标服务端与当前原生管理服务不匹配。"),
        );
    }
    let cache_result = !read_only_operation(&request.operation);
    let client_device_id = {
        let Ok(mut state) = state_lock.lock() else {
            return response(&request, 503, error_body("远程管理会话状态不可用。"));
        };
        loop {
            prune_state(&mut state);
            let Some(session) = state.sessions.get(&request.session_generation) else {
                return response(
                    &request,
                    410,
                    error_body("管理会话代次不存在或已过期，请重新建立管理会话。"),
                );
            };
            if session.expires_at <= Instant::now() {
                return response(
                    &request,
                    410,
                    error_body("管理会话已过期，请重新建立管理会话。"),
                );
            }
            if session.client_device_id.is_empty() {
                return response(&request, 403, error_body("管理会话缺少已认证客户端身份。"));
            }
            let client_device_id = session.client_device_id.clone();
            if let Some(previous) = state.requests.get(&request.request_id) {
                if previous.client_device_id == client_device_id
                    && same_operation(&previous.request, &request)
                {
                    return response(
                        &request,
                        previous.response.status,
                        previous.response.body.clone(),
                    );
                }
                return response(
                    &request,
                    409,
                    error_body("同一操作编号已用于不同请求参数。"),
                );
            }
            if let Some(previous) = state.inflight.get(&request.request_id) {
                if previous.client_device_id != client_device_id
                    || !same_operation(&previous.request, &request)
                {
                    return response(
                        &request,
                        409,
                        error_body("同一执行中操作编号已用于不同请求参数。"),
                    );
                }
                state = match requests_changed.wait(state) {
                    Ok(state) => state,
                    Err(_) => {
                        return response(&request, 503, error_body("远程管理请求状态不可用。"))
                    }
                };
                continue;
            }
            // 只读补查必须读取最新状态，也不能占满有副作用操作的幂等结果表。
            if !cache_result {
                break client_device_id;
            }
            if state.requests.len().saturating_add(state.inflight.len()) >= MAX_REQUEST_RESULTS {
                return response(&request, 429, error_body("远程管理操作幂等记录容量已满。"));
            }
            state.inflight.insert(
                request.request_id.clone(),
                InFlightRequest {
                    request: request.clone(),
                    client_device_id: client_device_id.clone(),
                },
            );
            break client_device_id;
        }
    };

    let (status, body) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        dispatch(&request, &client_device_id)
    }))
    .unwrap_or_else(|_| {
        (
            500,
            error_body("原生资源操作异常退出；请查询原任务后再决定是否重试。"),
        )
    });
    let result = response(&request, status, body);
    if cache_result {
        if let Ok(mut state) = state_lock.lock() {
            state.inflight.remove(&request.request_id);
            prune_state(&mut state);
            if state.requests.len() < MAX_REQUEST_RESULTS {
                let request_id = request.request_id.clone();
                state.requests.insert(
                    request_id,
                    RequestResult {
                        request,
                        response: result.clone(),
                        client_device_id,
                        expires_at: Instant::now() + REQUEST_RESULT_TTL,
                    },
                );
            }
            requests_changed.notify_all();
        }
    }
    result
}

/// read_only_operation（无副作用的状态读取）：计划生成、授权及写入仍保留幂等结果。
fn read_only_operation(operation: &str) -> bool {
    matches!(
        operation,
        "management.status"
            | "resource.inventory"
            | "resource.jobs"
            | "resource.job.read"
            | "system.ssh.status"
    )
}

fn response(
    request: &RemoteManagementRequest,
    status: u16,
    body: Value,
) -> RemoteManagementResponse {
    RemoteManagementResponse {
        protocol_version: DEVICE_MANAGEMENT_PROTOCOL_VERSION,
        request_id: request.request_id.clone(),
        target_host_id: request.target_host_id.clone(),
        session_generation: request.session_generation,
        operation: request.operation.clone(),
        status,
        body,
        fields: response_fields(),
    }
}

fn error_body(error: impl Into<String>) -> Value {
    json!({
        "ok": false,
        "error": error.into(),
        "fields": {
            "ok": "本机原生资源服务是否接受本次固定操作",
            "error": "服务端返回的实际失败原因",
        }
    })
}

fn open_response_fields() -> std::collections::BTreeMap<String, String> {
    std::collections::BTreeMap::from([
        ("protocol_version".into(), "独立远程管理协议版本".into()),
        ("request_id".into(), "对应会话申请的幂等编号".into()),
        ("target_host_id".into(), "本次管理目标服务端编号".into()),
        (
            "client_device_id".into(),
            "经 SSH 强制命令绑定的客户端设备编号".into(),
        ),
        (
            "session_generation".into(),
            "本机管理服务独立签发的会话代次".into(),
        ),
        ("expires_at_ms".into(), "管理会话到期时刻，Unix 毫秒".into()),
    ])
}

fn response_fields() -> std::collections::BTreeMap<String, String> {
    std::collections::BTreeMap::from([
        ("protocol_version".into(), "独立远程管理协议版本".into()),
        ("request_id".into(), "本次业务请求的稳定编号".into()),
        ("target_host_id".into(), "本次操作所属服务端编号".into()),
        ("session_generation".into(), "本次已认证管理会话代次".into()),
        ("operation".into(), "本机确认的固定操作名称".into()),
        ("status".into(), "本机原生资源操作状态码".into()),
        ("body".into(), "资源任务、组件清单或实际错误结果".into()),
    ])
}

fn prune_state(state: &mut ServiceState) {
    let now = Instant::now();
    state.sessions.retain(|_, session| session.expires_at > now);
    state.opens.retain(|_, result| result.expires_at > now);
    state.requests.retain(|_, result| result.expires_at > now);
}

fn generation_seed() -> u64 {
    let id = voice_desktop_common::workspace::new_id();
    let digits = id
        .bytes()
        .filter(|byte| byte.is_ascii_hexdigit())
        .take(16)
        .collect::<Vec<_>>();
    u64::from_str_radix(std::str::from_utf8(&digits).unwrap_or("1"), 16)
        .unwrap_or(1)
        .max(1)
}

fn unix_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u64::MAX as u128) as u64
}

fn read_line_bounded(reader: &mut impl BufRead, limit: usize) -> Result<Option<Vec<u8>>, String> {
    let mut output = Vec::new();
    loop {
        let available = reader.fill_buf().map_err(|_| "远程管理连接读取失败。")?;
        if available.is_empty() {
            return if output.is_empty() {
                Ok(None)
            } else {
                Err("远程管理 JSON 行未以换行结束。".into())
            };
        }
        if let Some(index) = available.iter().position(|byte| *byte == b'\n') {
            if output.len().saturating_add(index) > limit {
                return Err("远程管理 JSON 行超过大小限制。".into());
            }
            output.extend_from_slice(&available[..index]);
            reader.consume(index + 1);
            if output.last() == Some(&b'\r') {
                output.pop();
            }
            return Ok(Some(output));
        }
        let count = available.len();
        if output.len().saturating_add(count) > limit {
            let remaining = limit
                .saturating_add(1)
                .saturating_sub(output.len())
                .min(count);
            reader.consume(remaining);
            return Err("远程管理 JSON 行超过大小限制。".into());
        }
        output.extend_from_slice(available);
        reader.consume(count);
    }
}

fn write_json_line(
    writer: &mut impl Write,
    value: &impl serde::Serialize,
    limit: usize,
) -> Result<(), String> {
    let bytes = serde_json::to_vec(value).map_err(|_| "远程管理响应编码失败。")?;
    if bytes.len() > limit {
        return Err("远程管理响应超过大小限制。".into());
    }
    writer
        .write_all(&bytes)
        .map_err(|_| "远程管理响应写入失败。")?;
    writer
        .write_all(b"\n")
        .map_err(|_| "远程管理响应写入失败。")?;
    writer
        .flush()
        .map_err(|_| String::from("远程管理响应刷新失败。"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn polling_stays_fresh_beyond_ledger_capacity_and_preserves_write_replay() {
        let ledger = Mutex::new(ServiceState {
            next_generation: 10,
            sessions: HashMap::from([(
                9,
                ManagedSession {
                    client_device_id: "client-a".into(),
                    expires_at: Instant::now() + SESSION_TTL,
                },
            )]),
            opens: HashMap::new(),
            inflight: HashMap::new(),
            requests: HashMap::new(),
        });
        let changed = Condvar::new();
        let request = |id: usize, operation: &str| RemoteManagementRequest {
            protocol_version: 1,
            request_id: format!("{id:032x}"),
            target_host_id: "server-a".into(),
            session_generation: 9,
            operation: operation.into(),
            arguments: json!({}),
            fields: std::collections::BTreeMap::from([
                ("protocol_version".into(), "协议版本".into()),
                ("request_id".into(), "请求编号".into()),
                ("target_host_id".into(), "服务端编号".into()),
                ("session_generation".into(), "管理代次".into()),
                ("operation".into(), "固定操作".into()),
                ("arguments".into(), "操作参数".into()),
            ]),
        };
        for revision in 1..=700 {
            let result = dispatch_with_ledger(
                "server-a",
                &ledger,
                &changed,
                request(revision, "management.status"),
                |_, _| (200, json!({"revision":revision})),
            );
            assert_eq!(result.status, 200);
            assert_eq!(result.body["revision"], revision);
        }
        assert!(ledger.lock().unwrap().requests.is_empty());
        for id in 1..=MAX_REQUEST_RESULTS {
            let result = dispatch_with_ledger(
                "server-a",
                &ledger,
                &changed,
                request(id, "resource.install.start"),
                |_, client| {
                    assert_eq!(client, "client-a");
                    (202, json!({"job_id":id}))
                },
            );
            assert_eq!(result.status, 202);
        }
        let replay = dispatch_with_ledger(
            "server-a",
            &ledger,
            &changed,
            request(1, "resource.install.start"),
            |_, _| panic!("同一写入请求不可再次执行"),
        );
        assert_eq!(replay.body["job_id"], 1);
        let full = dispatch_with_ledger(
            "server-a",
            &ledger,
            &changed,
            request(900, "resource.install.start"),
            |_, _| panic!("容量耗尽时不可启动新写入"),
        );
        assert_eq!(full.status, 429);
        let status = dispatch_with_ledger(
            "server-a",
            &ledger,
            &changed,
            request(900, "resource.jobs"),
            |_, _| (200, json!([])),
        );
        assert_eq!(status.status, 200);
        let conflict = dispatch_with_ledger(
            "server-a",
            &ledger,
            &changed,
            request(1, "management.status"),
            |_, _| panic!("不可把旧写入编号改作其他请求"),
        );
        assert_eq!(conflict.status, 409);
        let mut expired = request(901, "resource.jobs");
        expired.session_generation = 8;
        assert_eq!(
            dispatch_with_ledger("server-a", &ledger, &changed, expired, |_, _| panic!(
                "不可执行旧代次"
            ))
            .status,
            410
        );
    }

    #[test]
    fn open_request_requires_bridge_identity_and_chinese_field_notes() {
        let request = RemoteManagementOpenRequest {
            protocol_version: 1,
            request_id: "a".repeat(32),
            target_host_id: "server-a".into(),
            client_device_id: "client-a".into(),
            fields: std::collections::BTreeMap::from([
                ("protocol_version".into(), "独立远程管理协议版本".into()),
                ("request_id".into(), "会话申请幂等编号".into()),
                ("target_host_id".into(), "目标服务端编号".into()),
                ("client_device_id".into(), "SSH 认证客户端编号".into()),
            ]),
        };
        assert!(validate_open_request(&request, "server-a", "client-a").is_ok());
        assert!(validate_open_request(&request, "server-b", "client-a").is_err());
        assert!(validate_open_request(&request, "server-a", "spoofed-client").is_err());
        let mut unlabeled = request;
        unlabeled.fields.remove("target_host_id");
        assert!(validate_open_request(&unlabeled, "server-a", "client-a").is_err());
    }

    #[test]
    fn ssh_enable_requires_explicit_confirmation_and_ready_state_is_status_only() {
        assert_eq!(
            ssh_enable_decision(&json!({"confirmed": true}), true).unwrap(),
            SshEnableDecision::StatusOnly,
        );
        assert_eq!(
            ssh_enable_decision(&json!({"confirmed": true}), false).unwrap(),
            SshEnableDecision::StartManagedJob,
        );
        assert!(ssh_enable_decision(&json!({"confirmed": false}), false).is_err());
        assert!(ssh_enable_decision(&json!({"confirmed": true, "path": "C:/bad"}), false).is_err());
    }

    #[test]
    fn ssh_status_payload_has_typed_status_job_and_field_notes() {
        let value = ssh_status_payload(json!({"ready": true, "service_state": "Running"}), None);
        assert_eq!(value["status"]["ready"], true);
        assert!(value["job"].is_null());
        assert!(value["fields"]["status"].as_str().is_some());
        assert!(value["fields"]["job"].as_str().is_some());
    }

    #[test]
    fn bounded_line_reader_rejects_oversized_and_unterminated_frames() {
        let mut oversized = BufReader::new(&b"123456\n"[..]);
        assert!(read_line_bounded(&mut oversized, 5).is_err());
        let mut unfinished = BufReader::new(&b"{}"[..]);
        assert!(read_line_bounded(&mut unfinished, 8).is_err());
        let mut good = BufReader::new(&b"{}\r\n"[..]);
        assert_eq!(
            read_line_bounded(&mut good, 8).unwrap(),
            Some(b"{}".to_vec())
        );
    }

    #[test]
    fn management_handle_running_state_tracks_accept_worker() {
        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
        let worker = std::thread::spawn(move || {
            let _ = release_rx.recv();
        });
        let handle = RemoteManagementHandle {
            address: "127.0.0.1:0".parse().unwrap(),
            stop: Arc::new(AtomicBool::new(false)),
            connections: Arc::new(Mutex::new(HashMap::new())),
            workers: Arc::new(Mutex::new(Vec::new())),
            accept_worker: Some(worker),
        };
        assert!(handle.is_running());
        release_tx.send(()).unwrap();
        while handle.is_running() {
            std::thread::yield_now();
        }
        assert!(!handle.is_running());
    }

    #[test]
    fn replay_identity_keeps_operation_parameters_across_session_generations() {
        let make_request = |generation, resource_id: &str| RemoteManagementRequest {
            protocol_version: 1,
            request_id: "b".repeat(32),
            target_host_id: "server-a".into(),
            session_generation: generation,
            operation: "resource.install.start".into(),
            arguments: json!({"resource_id":resource_id}),
            fields: std::collections::BTreeMap::from([
                ("protocol_version".into(), "独立远程管理协议版本".into()),
                ("request_id".into(), "稳定业务操作编号".into()),
                ("target_host_id".into(), "本次目标服务端编号".into()),
                ("session_generation".into(), "服务端管理会话代次".into()),
                ("operation".into(), "固定远程管理操作".into()),
                ("arguments".into(), "固定操作的 JSON 参数".into()),
            ]),
        };
        let first = make_request(7, "dsh");
        let retry = make_request(8, "dsh");
        let conflict = make_request(8, "tts-vits");
        assert!(same_operation(&first, &retry));
        assert!(!same_operation(&first, &conflict));
        assert!(exact_keys(
            &json!({"plan_request_id":"a","resource_id":"dsh","token":"b","confirmed":true}),
            &["plan_request_id", "resource_id", "token", "confirmed"],
            &[]
        ));
        assert!(!exact_keys(
            &json!({"plan_request_id":"a","resource_id":"dsh","token":"b","confirmed":true,"path":"C:/bad"}),
            &["plan_request_id", "resource_id", "token", "confirmed"],
            &[]
        ));
    }

    #[test]
    fn remote_aniflive_conversion_start_delegates_only_valid_server_task_ids() {
        let task_id = "a".repeat(32);
        let (status, body) = start_aniflive_import_job(
            &json!({"kind":"aniflive_import","task_id":task_id.clone()}),
            |received| Ok(json!({"job_id":format!("aniflive-import-{received}")})),
        );
        assert_eq!(status, 202);
        let expected_job_id = format!("aniflive-import-{task_id}");
        assert_eq!(body["job_id"].as_str(), Some(expected_job_id.as_str()));

        for invalid_id in [
            "A".repeat(32),
            "g".repeat(32),
            "a".repeat(31),
            format!("{}{}{}", "a".repeat(15), "/", "a".repeat(16)),
        ] {
            let mut delegated = false;
            let (status, _) = start_aniflive_import_job(
                &json!({"kind":"aniflive_import","task_id":invalid_id}),
                |_| {
                    delegated = true;
                    Ok(json!({"unexpected":"must not start"}))
                },
            );
            assert_eq!(status, 400, "invalid task id should be rejected");
            assert!(!delegated, "invalid task id must not reach ResourceManager");
        }

        let mut delegated = false;
        let (status, _) = start_aniflive_import_job(
            &json!({"kind":"aniflive_import","task_id":task_id,"path":"C:/client"}),
            |_| {
                delegated = true;
                Ok(json!({"unexpected":"must not start"}))
            },
        );
        assert_eq!(status, 400, "extra client path must be rejected");
        assert!(
            !delegated,
            "extra client path must not reach ResourceManager"
        );
    }
}
