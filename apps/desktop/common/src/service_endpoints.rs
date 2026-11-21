//! ServiceEndpoints（服务端点目录）：内存登记实际回环地址，经同账户本机管道交接。
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    net::{Ipv4Addr, SocketAddr, TcpStream},
    path::Path,
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
    time::Duration,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BridgeChannel {
    Gateway,    // Gateway（设备业务与媒体网关）。
    Management, // Management（原生管理服务）。
    File,       // File（文件传输服务）。
}

impl BridgeChannel {
    pub fn from_marker(marker: &str) -> Result<(Self, bool), String> {
        match marker {
            "voice-terminal-v1-control" | "voice-terminal-v1-media" => Ok((Self::Gateway, false)),
            "voice-terminal-v1-management" => Ok((Self::Management, true)),
            "voice-terminal-v1-file" => Ok((Self::File, true)),
            _ => Err("SSH 请求了未登记的桥接通道。".into()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServiceEndpoint {
    pub address: SocketAddr, // address（已绑定的实际回环地址）。
    pub pid: u32,            // pid（监听器所属进程编号）。
    pub created_at: u64,     // created_at（系统进程创建时间，防止编号复用）。
    pub generation: String,  // generation（监听器所属启动代次）。
}

impl ServiceEndpoint {
    pub fn validate(&self) -> Result<(), String> {
        if self.address.ip() != Ipv4Addr::LOCALHOST
            || self.address.port() == 0
            || self.pid == 0
            || self.generation.is_empty()
            || self.generation.len() > 128
            || !self
                .generation
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
        {
            return Err("服务端点不是有效的受管回环监听。".into());
        }
        if !crate::workspace::process_identity(self.pid)?
            .is_some_and(|(created, _)| created == self.created_at)
        {
            return Err("服务端点所属进程已退出或身份已变化。".into());
        }
        #[cfg(windows)]
        if !listener_owned(self.address, self.pid)? {
            return Err("实际监听器已关闭或不属于登记进程。".into());
        }
        Ok(())
    }

    pub fn connect(&self) -> Result<TcpStream, String> {
        self.validate()?;
        let stream = TcpStream::connect_timeout(&self.address, Duration::from_secs(5))
            .map_err(|_| "当前受管回环服务未就绪。".to_string())?;
        // 建立连接后再次确认监听归属，在转发任何业务字节前拒绝被其他进程重占的旧端口。
        self.validate()?;
        #[cfg(windows)]
        if !tcp_owned(
            self.address,
            self.pid,
            Some(stream.local_addr().map_err(|_| "无法读取本机连接地址。")?),
        )? {
            return Err("已建立连接的服务端进程与登记端点不匹配。".into());
        }
        Ok(stream)
    }
}

#[cfg(windows)]
fn listener_owned(address: SocketAddr, pid: u32) -> Result<bool, String> {
    tcp_owned(address, pid, None)
}

#[cfg(windows)]
fn tcp_owned(address: SocketAddr, pid: u32, remote: Option<SocketAddr>) -> Result<bool, String> {
    use std::{
        mem::{offset_of, size_of},
        ptr,
    };
    use windows_sys::Win32::{
        Foundation::ERROR_INSUFFICIENT_BUFFER,
        NetworkManagement::IpHelper::{
            GetExtendedTcpTable, MIB_TCPROW_OWNER_PID, MIB_TCPTABLE_OWNER_PID, MIB_TCP_STATE_ESTAB,
            MIB_TCP_STATE_LISTEN, TCP_TABLE_OWNER_PID_CONNECTIONS, TCP_TABLE_OWNER_PID_LISTENER,
        },
        Networking::WinSock::AF_INET,
    };
    let table_class = if remote.is_some() {
        TCP_TABLE_OWNER_PID_CONNECTIONS
    } else {
        TCP_TABLE_OWNER_PID_LISTENER
    };
    let mut size = 0;
    let result = unsafe {
        GetExtendedTcpTable(
            ptr::null_mut(),
            &mut size,
            0,
            AF_INET as u32,
            table_class,
            0,
        )
    };
    if result != ERROR_INSUFFICIENT_BUFFER {
        return Err(format!("无法读取回环监听归属，系统错误码 {result}。"));
    }
    let offset = offset_of!(MIB_TCPTABLE_OWNER_PID, table);
    for _ in 0..3 {
        if size < offset as u32 || size > 8 * 1024 * 1024 {
            return Err("回环监听表大小无效。".into());
        }
        // u64（八字节整数）缓冲保证系统结构体对齐；表内偏移仍由官方类型派生。
        let mut buffer = vec![0u64; (size as usize).div_ceil(size_of::<u64>())];
        let capacity = buffer.len() * size_of::<u64>();
        let result = unsafe {
            GetExtendedTcpTable(
                buffer.as_mut_ptr().cast(),
                &mut size,
                0,
                AF_INET as u32,
                table_class,
                0,
            )
        };
        if result == ERROR_INSUFFICIENT_BUFFER {
            continue;
        }
        if result != 0 {
            return Err(format!("无法读取回环监听归属，系统错误码 {result}。"));
        }
        let bytes = buffer.as_ptr().cast::<u8>();
        let count = unsafe { ptr::read_unaligned(bytes.cast::<u32>()) } as usize;
        let end = count
            .checked_mul(size_of::<MIB_TCPROW_OWNER_PID>())
            .and_then(|len| offset.checked_add(len))
            .ok_or("回环监听表条目数量无效。")?;
        if end > size as usize || end > capacity {
            return Err("回环监听表条目超过缓冲区。".into());
        }
        let local = u32::from_ne_bytes(Ipv4Addr::LOCALHOST.octets());
        let mut owned = false;
        for index in 0..count {
            let row = unsafe {
                ptr::read_unaligned(
                    bytes
                        .add(offset + index * size_of::<MIB_TCPROW_OWNER_PID>())
                        .cast::<MIB_TCPROW_OWNER_PID>(),
                )
            };
            let state = if remote.is_some() {
                MIB_TCP_STATE_ESTAB
            } else {
                MIB_TCP_STATE_LISTEN
            };
            let remote_matches = remote.is_none_or(|peer| {
                peer.ip() == Ipv4Addr::LOCALHOST
                    && row.dwRemoteAddr == local
                    && u16::from_be(row.dwRemotePort as u16) == peer.port()
            });
            if row.dwState == state as u32
                && remote_matches
                && u16::from_be(row.dwLocalPort as u16) == address.port()
                && (row.dwLocalAddr == local || row.dwLocalAddr == 0)
            {
                if row.dwOwningPid != pid || row.dwLocalAddr != local {
                    return Ok(false);
                }
                owned = true;
            }
        }
        return Ok(owned);
    }
    Err("回环监听表持续变化，未确认端点归属。".into())
}

#[derive(Default)]
pub struct EndpointDirectory {
    rows: Mutex<BTreeMap<BridgeChannel, ServiceEndpoint>>, // rows（仅当前宿主内存中的端点记录）。
    gateway_epoch: AtomicU64, // gateway_epoch（原生宿主当前核心服务代次）。
    gateway_revision: AtomicU64, // gateway_revision（当前代次已接纳的端点版本）。
}

impl EndpointDirectory {
    pub fn begin_gateway_epoch(&self, epoch: u64) {
        if let Ok(mut rows) = self.rows.lock() {
            rows.remove(&BridgeChannel::Gateway);
            self.gateway_epoch.store(epoch, Ordering::Release);
            self.gateway_revision.store(0, Ordering::Release);
        }
    }
    pub fn update_gateway(
        &self,
        epoch: u64,
        revision: u64,
        endpoint: Option<ServiceEndpoint>,
    ) -> Result<bool, String> {
        if let Some(endpoint) = &endpoint {
            endpoint.validate()?;
        }
        let mut rows = self.rows.lock().map_err(|_| "服务端点目录不可用。")?;
        if epoch != self.gateway_epoch.load(Ordering::Acquire)
            || revision < self.gateway_revision.load(Ordering::Acquire)
        {
            return Ok(false);
        }
        self.gateway_revision.store(revision, Ordering::Release);
        if let Some(endpoint) = endpoint {
            rows.insert(BridgeChannel::Gateway, endpoint);
        } else {
            rows.remove(&BridgeChannel::Gateway);
        }
        Ok(true)
    }
    pub fn register(
        &self,
        channel: BridgeChannel,
        address: SocketAddr,
        pid: u32,
        generation: &str,
    ) -> Result<(), String> {
        let (created_at, _) =
            crate::workspace::process_identity(pid)?.ok_or("无法确认服务端点所属进程。")?;
        let endpoint = ServiceEndpoint {
            address,
            pid,
            created_at,
            generation: generation.into(),
        };
        endpoint.validate()?;
        self.rows
            .lock()
            .map_err(|_| "服务端点目录不可用。")?
            .insert(channel, endpoint);
        Ok(())
    }
    pub fn remove(&self, channel: BridgeChannel) {
        if let Ok(mut rows) = self.rows.lock() {
            rows.remove(&channel);
        }
    }
    pub fn resolve(&self, channel: BridgeChannel) -> Result<ServiceEndpoint, String> {
        let endpoint = self
            .rows
            .lock()
            .map_err(|_| "服务端点目录不可用。")?
            .get(&channel)
            .cloned()
            .ok_or("当前服务端点尚未就绪。")?;
        endpoint.validate()?;
        Ok(endpoint)
    }
    pub fn connect(&self, channel: BridgeChannel) -> Result<TcpStream, String> {
        self.resolve(channel)?.connect()
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Query {
    protocol: u32,          // protocol（端点查询协议版本）。
    channel: BridgeChannel, // channel（正式登记的业务通道）。
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Reply {
    protocol: u32,                     // protocol（端点查询协议版本）。
    host_pid: u32,                     // host_pid（命名管道所属正式宿主进程）。
    endpoint: Option<ServiceEndpoint>, // endpoint（本轮有效端点；无记录时明确拒绝）。
}

#[cfg(windows)]
mod local_pipe {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::{ffi::c_void, os::windows::io::AsRawHandle, path::PathBuf, ptr, sync::Arc, thread};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::windows::named_pipe::{ClientOptions, NamedPipeServer, ServerOptions},
        sync::{oneshot, Semaphore},
    };
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::{
            Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW,
            SECURITY_ATTRIBUTES,
        },
        System::Pipes::{GetNamedPipeClientProcessId, GetNamedPipeServerProcessId},
    };
    const MAX_FRAME: usize = 4096; // MAX_FRAME（端点查询消息字节上限）。

    struct LocalEndpoint {
        name: String,        // name（按工作区与 Windows 账户隔离的命名管道）。
        sid: String,         // sid（允许交互与 SSH 会话共同访问的账户安全标识）。
        executable: PathBuf, // executable（双方核验的正式桌面宿主路径）。
    }
    impl LocalEndpoint {
        fn new(root: &Path, executable: &Path) -> Result<Self, String> {
            let root = crate::canonical_path(root)?;
            let sid = crate::system_feature::current_user_sid()?;
            let name = format!(
                r"\\.\pipe\VoiceTerminal-Services-{:x}",
                Sha256::digest(format!("{sid}\n{}", root.to_string_lossy().to_lowercase()))
            );
            Ok(Self {
                name,
                sid,
                executable: crate::canonical_path(executable)?,
            })
        }
        fn verify_peer(&self, pid: u32) -> Result<(), String> {
            if !crate::workspace::process_identity(pid)?.is_some_and(|(_, path)| {
                path.to_string_lossy()
                    .eq_ignore_ascii_case(&self.executable.to_string_lossy())
            }) {
                return Err("服务端点管道的正式程序身份无法确认。".into());
            }
            Ok(())
        }
        fn server(&self, first: bool) -> Result<NamedPipeServer, String> {
            let sddl = format!("D:P(A;;GA;;;{})", self.sid)
                .encode_utf16()
                .chain(Some(0))
                .collect::<Vec<_>>();
            let mut descriptor = ptr::null_mut();
            let result = unsafe {
                if ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl.as_ptr(),
                    1,
                    &mut descriptor,
                    ptr::null_mut(),
                ) == 0
                {
                    return Err("无法建立服务端点管道访问控制。".into());
                }
                let mut attributes = SECURITY_ATTRIBUTES {
                    nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                    lpSecurityDescriptor: descriptor,
                    bInheritHandle: 0,
                };
                let result = ServerOptions::new()
                    .first_pipe_instance(first)
                    .reject_remote_clients(true)
                    .create_with_security_attributes_raw(
                        &self.name,
                        (&mut attributes as *mut SECURITY_ATTRIBUTES).cast::<c_void>(),
                    );
                LocalFree(descriptor);
                result
            };
            result.map_err(|error| format!("无法建立服务端点管道：{error}"))
        }
    }
    async fn read_frame<R: tokio::io::AsyncRead + Unpin>(
        stream: &mut R,
    ) -> Result<Vec<u8>, String> {
        let size = stream
            .read_u32_le()
            .await
            .map_err(|_| "服务端点消息长度读取失败。")? as usize;
        if size == 0 || size > MAX_FRAME {
            return Err("服务端点消息超过长度上限。".into());
        }
        let mut bytes = vec![0; size];
        stream
            .read_exact(&mut bytes)
            .await
            .map_err(|_| "服务端点消息读取失败。")?;
        Ok(bytes)
    }
    async fn write_frame<W: tokio::io::AsyncWrite + Unpin>(
        stream: &mut W,
        value: &impl Serialize,
    ) -> Result<(), String> {
        let bytes = serde_json::to_vec(value).map_err(|_| "服务端点消息编码失败。")?;
        if bytes.is_empty() || bytes.len() > MAX_FRAME {
            return Err("服务端点消息超过长度上限。".into());
        }
        stream
            .write_u32_le(bytes.len() as u32)
            .await
            .map_err(|_| "服务端点消息写入失败。")?;
        stream
            .write_all(&bytes)
            .await
            .map_err(|_| "服务端点消息写入失败。")?;
        stream
            .flush()
            .await
            .map_err(|_| "服务端点消息写入失败。".into())
    }

    pub struct EndpointServer {
        stop: Option<oneshot::Sender<()>>, // stop（宿主停止或广播关闭时撤销管道）。
        worker: Option<thread::JoinHandle<()>>, // worker（独立端点管道运行线程）。
    }
    impl EndpointServer {
        pub fn is_running(&self) -> bool {
            self.worker
                .as_ref()
                .is_some_and(|worker| !worker.is_finished())
        }
        pub fn start(root: &Path, directory: Arc<EndpointDirectory>) -> Result<Self, String> {
            let endpoint = Arc::new(LocalEndpoint::new(
                root,
                &std::env::current_exe().map_err(|e| e.to_string())?,
            )?);
            let (stop, stopped) = oneshot::channel();
            let (ready, started) = std::sync::mpsc::sync_channel(1);
            let worker = thread::Builder::new().name("service-endpoints".into()).spawn(move || {
                let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
                    Ok(runtime) => runtime,
                    Err(error) => { let _ = ready.send(Err(error.to_string())); return; }
                };
                runtime.block_on(async move {
                    let mut pipe = match endpoint.server(true) {
                        Ok(pipe) => pipe,
                        Err(error) => { let _ = ready.send(Err(error)); return; }
                    };
                    let _ = ready.send(Ok(()));
                    let permits = Arc::new(Semaphore::new(8));
                    tokio::pin!(stopped);
                    loop {
                        let accepted = tokio::select! { _ = &mut stopped => break, result = pipe.connect() => result };
                        let connected = pipe;
                        pipe = match endpoint.server(false) { Ok(pipe) => pipe, Err(_) => break };
                        if accepted.is_err() { continue; }
                        let Ok(permit) = permits.clone().try_acquire_owned() else { continue };
                        let endpoint = endpoint.clone();
                        let directory = directory.clone();
                        tokio::spawn(async move {
                            let _permit = permit;
                            let mut pipe = connected;
                            let _ = tokio::time::timeout(Duration::from_secs(3), async {
                                let mut pid = 0;
                                if unsafe { GetNamedPipeClientProcessId(pipe.as_raw_handle().cast(), &mut pid) } == 0 {
                                    return Err("无法确认端点查询进程。".to_string());
                                }
                                endpoint.verify_peer(pid)?;
                                let query: Query = serde_json::from_slice(&read_frame(&mut pipe).await?).map_err(|_| "端点查询格式无效。")?;
                                if query.protocol != 1 { return Err("端点查询协议版本不兼容。".into()); }
                                write_frame(&mut pipe, &Reply { protocol: 1, host_pid: std::process::id(),
                                    endpoint: directory.resolve(query.channel).ok() }).await
                            }).await;
                        });
                    }
                });
            }).map_err(|error| error.to_string())?;
            match started.recv_timeout(Duration::from_secs(5)) {
                Ok(Ok(())) => Ok(Self {
                    stop: Some(stop),
                    worker: Some(worker),
                }),
                result => {
                    let _ = stop.send(());
                    let _ = worker.join();
                    Err(result
                        .ok()
                        .and_then(Result::err)
                        .unwrap_or_else(|| "服务端点管道启动超时。".into()))
                }
            }
        }
    }
    impl Drop for EndpointServer {
        fn drop(&mut self) {
            if let Some(stop) = self.stop.take() {
                let _ = stop.send(());
            }
            if let Some(worker) = self.worker.take() {
                let _ = worker.join();
            }
        }
    }
    pub fn query(
        root: &Path,
        executable: &Path,
        channel: BridgeChannel,
    ) -> Result<ServiceEndpoint, String> {
        let endpoint = LocalEndpoint::new(root, executable)?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        runtime.block_on(async {
            tokio::time::timeout(Duration::from_secs(3), async {
                let mut pipe = loop {
                    match ClientOptions::new().open(&endpoint.name) {
                        Ok(pipe) => break pipe,
                        Err(error) if matches!(error.raw_os_error(), Some(231 | 232 | 233)) => {
                            tokio::time::sleep(Duration::from_millis(20)).await;
                        }
                        Err(_) => return Err("当前工作区的服务端点管道未就绪。".into()),
                    }
                };
                let mut pid = 0;
                if unsafe { GetNamedPipeServerProcessId(pipe.as_raw_handle().cast(), &mut pid) }
                    == 0
                {
                    return Err("无法确认正式服务端点宿主。".into());
                }
                endpoint.verify_peer(pid)?;
                write_frame(
                    &mut pipe,
                    &Query {
                        protocol: 1,
                        channel,
                    },
                )
                .await?;
                let reply: Reply = serde_json::from_slice(&read_frame(&mut pipe).await?)
                    .map_err(|_| "服务端点回执格式无效。")?;
                if reply.protocol != 1 || reply.host_pid != pid {
                    return Err("服务端点回执宿主身份不匹配。".into());
                }
                let service = reply.endpoint.ok_or("当前业务通道尚未就绪。")?;
                service.validate()?;
                Ok(service)
            })
            .await
            .map_err(|_| "服务端点查询超时。".to_string())?
        })
    }
}
#[cfg(windows)]
pub use local_pipe::{query, EndpointServer};
#[cfg(not(windows))]
pub fn query(
    _root: &Path,
    _executable: &Path,
    _channel: BridgeChannel,
) -> Result<ServiceEndpoint, String> {
    Err("服务端点管道仅支持 Windows（操作系统）。".into())
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::{net::TcpListener, sync::Arc};

    #[test]
    fn directory_tracks_actual_listeners_and_rejects_invalid_process_identity() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let directory = EndpointDirectory::default();
        directory
            .register(
                BridgeChannel::Gateway,
                listener.local_addr().unwrap(),
                std::process::id(),
                "generation-1",
            )
            .unwrap();
        let record = directory.resolve(BridgeChannel::Gateway).unwrap();
        assert_eq!(record.address, listener.local_addr().unwrap());
        assert!(!listener_owned(record.address, 0).unwrap());
        assert!(record.connect().is_ok());
        assert!(ServiceEndpoint {
            created_at: record.created_at + 1,
            ..record.clone()
        }
        .validate()
        .is_err());
        assert!(directory
            .register(
                BridgeChannel::File,
                "0.0.0.0:12345".parse().unwrap(),
                std::process::id(),
                "generation-1"
            )
            .is_err());
        assert!(directory
            .register(
                BridgeChannel::File,
                "127.0.0.1:0".parse().unwrap(),
                std::process::id(),
                "generation-1"
            )
            .is_err());
        drop(listener);
        assert!(crate::workspace::process_identity(std::process::id())
            .unwrap()
            .is_some());
        assert!(record.validate().is_err());
        assert!(record.connect().is_err());
        directory.remove(BridgeChannel::Gateway);
        assert!(directory.resolve(BridgeChannel::Gateway).is_err());
    }

    #[test]
    fn old_core_epochs_and_out_of_order_endpoint_revisions_cannot_replace_current_gateway() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let directory = EndpointDirectory::default();
        directory
            .register(
                BridgeChannel::Gateway,
                listener.local_addr().unwrap(),
                std::process::id(),
                "generation-1",
            )
            .unwrap();
        let endpoint = directory.resolve(BridgeChannel::Gateway).unwrap();
        directory.begin_gateway_epoch(1);
        assert!(directory
            .update_gateway(1, 2, Some(endpoint.clone()))
            .unwrap());
        assert!(!directory.update_gateway(1, 1, None).unwrap());
        assert!(directory.resolve(BridgeChannel::Gateway).is_ok());
        directory.begin_gateway_epoch(2);
        assert!(!directory
            .update_gateway(1, 999, Some(endpoint.clone()))
            .unwrap());
        assert!(directory.resolve(BridgeChannel::Gateway).is_err());
        assert!(directory
            .update_gateway(2, 1, Some(endpoint.clone()))
            .unwrap());
        assert!(directory.update_gateway(2, 2, None).unwrap());
        assert!(!directory.update_gateway(2, 1, Some(endpoint)).unwrap());
        assert!(directory.resolve(BridgeChannel::Gateway).is_err());
    }

    #[test]
    fn real_pipe_resolves_current_ports_and_rejects_stale_or_foreign_workspaces() {
        let base = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.local/tmp/tasks");
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = base.join(format!("service-endpoints-{}-{unique}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let executable = std::env::current_exe().unwrap();
        let directory = Arc::new(EndpointDirectory::default());
        let first = TcpListener::bind("127.0.0.1:0").unwrap();
        let second = TcpListener::bind("127.0.0.1:0").unwrap();
        directory
            .register(
                BridgeChannel::Gateway,
                first.local_addr().unwrap(),
                std::process::id(),
                "generation-1",
            )
            .unwrap();
        let server = EndpointServer::start(&root, directory.clone()).unwrap();
        let record = query(&root, &executable, BridgeChannel::Gateway).unwrap();
        assert_eq!(record.address, first.local_addr().unwrap());
        directory
            .register(
                BridgeChannel::Gateway,
                second.local_addr().unwrap(),
                std::process::id(),
                "generation-2",
            )
            .unwrap();
        let replacement = query(&root, &executable, BridgeChannel::Gateway).unwrap();
        assert_ne!(replacement.address, record.address);
        assert_eq!(replacement.generation, "generation-2");
        assert!(query(&base, &executable, BridgeChannel::Gateway).is_err());
        assert!(query(
            &root,
            Path::new("C:\\Windows\\System32\\cmd.exe"),
            BridgeChannel::Gateway
        )
        .is_err());
        directory.remove(BridgeChannel::Gateway);
        assert!(query(&root, &executable, BridgeChannel::Gateway).is_err());
        drop(server);
        assert!(query(&root, &executable, BridgeChannel::Gateway).is_err());
        std::fs::remove_dir(root).unwrap();
    }
}
