//! RemoteServices（远程原生服务）：为已开启广播的本地模式提供动态回环管理和文件通道。
use crate::{
    remote_import::RemoteImportCoordinator, remote_management::RemoteManagementService,
    resource_commands::ResourceManager,
};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::Duration,
};
use voice_desktop_common::{
    device_file_channel::{
        self, DeviceFileTransferBegin, DeviceFileTransferChunk, DeviceFileTransferSession,
        FileChannelPeer, FileReceiver, FileTransferAuthorization, UploadBegin,
        FILE_TRANSFER_GATEWAY_PATH,
    },
    device_trust::DeviceTrustStore,
    service_endpoints::{BridgeChannel, EndpointDirectory, EndpointServer},
};

const DYNAMIC_LOOPBACK: &str = "127.0.0.1:0"; // DYNAMIC_LOOPBACK（由系统分配的回环监听地址）。

/// RemoteServices（远程原生服务集合）：管理 RPC 和文件传输只在产品本地广播期间运行。
pub struct RemoteServices {
    manager: Arc<ResourceManager>, // manager（复用正式服务端资源任务和事务）。
    running: Mutex<Option<RunningServices>>, // running（当前固定回环服务与停止句柄）。
    endpoints: Arc<EndpointDirectory>, // endpoints（正式宿主共用的当前端点目录）。
}

struct RunningServices {
    host_id: String,    // host_id（当前本机服务端稳定编号）。
    data_root: PathBuf, // data_root（当前工作区受管数据根目录）。
    management: crate::remote_management::RemoteManagementHandle, // management（独立管理服务句柄）。
    management_service: Arc<RemoteManagementService>, // management_service（已认证客户端的连接控制入口）。
    file_shutdown: Arc<AtomicBool>, // file_shutdown（文件 listener（监听器）停止信号）。
    file_worker: Option<JoinHandle<()>>, // file_worker（文件 listener（监听器）线程）。
    _endpoint_server: EndpointServer, // _endpoint_server（广播期间允许 SSH 桥接查询当前实际端口）。
}

impl RunningServices {
    fn is_running(&self) -> bool {
        services_are_running(
            self.management.is_running(),
            self.file_worker
                .as_ref()
                .is_some_and(|worker| !worker.is_finished()),
            self._endpoint_server.is_running(),
        )
    }
}

struct ValidatingFileReceiver {
    app: tauri::AppHandle, // app（独立本体版本同步复用正式维护入口）。
    diagnostic_root: std::path::PathBuf, // diagnostic_root（受管安装根目录，仅用于有界连接日志）。
    coordinator: Arc<RemoteImportCoordinator>, // coordinator（唯一远程导入票据和提交协调器）。
    data_root: PathBuf,                  // data_root（验证信任登记的当前工作区数据根目录）。
    workspace_id: String,                // workspace_id（本机正式工作区身份）。
    owner_sid: String,                   // owner_sid（本次正式程序当前 Windows 账户 SID）。
    build: String,                       // build（当前正式程序构建编号）。
    file_gateways: Mutex<HashMap<String, TcpStream>>, // file_gateways（每条 FILE 会话复用的固定 loopback HTTP keep-alive 连接）。
    endpoints: Arc<EndpointDirectory>,                // endpoints（文件代理只连接当前受管网关）。
}

impl RemoteServices {
    /// new（建立服务控制器）：不监听网络，直到保存配置明确开启本机广播。
    pub fn new(manager: Arc<ResourceManager>, endpoints: Arc<EndpointDirectory>) -> Self {
        Self {
            manager,
            running: Mutex::new(None),
            endpoints,
        }
    }

    /// start（启动固定回环服务）：依次验证本地模式、受信存储与当前 Windows 身份。
    pub fn start(&self, data_root: &Path, host_id: &str) -> Result<(), String> {
        let context = self.manager.context();
        if context.data.as_path() != data_root {
            return Err("远程服务数据根目录与当前资源工作区不匹配。".into());
        }
        match voice_desktop_common::device_config::load_from_root(data_root) {
            voice_desktop_common::device_config::LoadResult::Valid(config)
                if config.mode == voice_desktop_common::device_config::RunMode::Local
                    && config.broadcast_enabled
                    && config.device_id == host_id => {}
            voice_desktop_common::device_config::LoadResult::Valid(_) => {
                return Err("仅可在已保存的本地广播模式启动远程原生服务。".into());
            }
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("本机设备配置尚未保存；未启动远程原生服务。".into());
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        {
            let running = self.running.lock().map_err(|_| "远程服务状态不可用。")?;
            if running.as_ref().is_some_and(|current| {
                current.host_id == host_id && current.data_root == data_root && current.is_running()
            }) {
                return Ok(());
            }
        }
        self.stop()?;

        let trust = Arc::new(DeviceTrustStore::open(data_root)?);
        if trust.snapshot().state == "invalid" {
            return Err("设备信任登记无效且原件已保留；远程管理保持关闭。".into());
        }
        let coordinator = Arc::new(RemoteImportCoordinator::new(self.manager.clone()));
        let management = RemoteManagementService::new(
            self.manager.clone(),
            host_id.to_owned(),
            trust,
            coordinator.clone(),
        )?;
        let listener = TcpListener::bind(DYNAMIC_LOOPBACK)
            .map_err(|error| format!("无法启动回环文件服务：{error}"))?;
        let file_address = listener.local_addr().map_err(|error| error.to_string())?;
        let address = DYNAMIC_LOOPBACK
            .parse::<SocketAddr>()
            .map_err(|_| "远程管理固定回环地址无效。")?;
        let management_handle = management.start(address)?;
        let generation = format!(
            "native-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|_| "系统时间无效。")?
                .as_nanos()
        );
        self.endpoints.register(
            BridgeChannel::Management,
            management_handle.local_addr(),
            std::process::id(),
            &generation,
        )?;
        self.endpoints.register(
            BridgeChannel::File,
            file_address,
            std::process::id(),
            &generation,
        )?;
        let endpoint_server = EndpointServer::start(&context.install, self.endpoints.clone())?;

        let current_sid = voice_desktop_common::system_feature::current_user_sid()?;
        let release = voice_desktop_common::resources::load(&context.program.join("release.json"))?;
        let build = release["build"]
            .as_str()
            .filter(|value| !value.is_empty())
            .ok_or("正式程序缺少有效构建编号。")?
            .to_owned();
        let receiver: Arc<dyn FileReceiver> = Arc::new(ValidatingFileReceiver {
            app: self.manager.application_handle(),
            diagnostic_root: context.install.clone(),
            coordinator,
            data_root: data_root.to_path_buf(),
            workspace_id: context.workspace_id.clone(),
            owner_sid: current_sid,
            build,
            file_gateways: Mutex::new(HashMap::new()),
            endpoints: self.endpoints.clone(),
        });
        let file_shutdown = Arc::new(AtomicBool::new(false));
        let thread_shutdown = file_shutdown.clone();
        let file_worker = thread::Builder::new()
            .name("remote-file-listener".into())
            .spawn(move || {
                let _ = device_file_channel::serve_file_channel_listener(
                    listener,
                    receiver,
                    thread_shutdown,
                );
            })
            .map_err(|error| format!("无法建立回环文件服务线程：{error}"))?;
        *self.running.lock().map_err(|_| "远程服务状态不可用。")? = Some(RunningServices {
            host_id: host_id.to_owned(),
            data_root: data_root.to_path_buf(),
            management: management_handle,
            management_service: management,
            file_shutdown,
            file_worker: Some(file_worker),
            _endpoint_server: endpoint_server,
        });
        Ok(())
    }

    /// stop（停止固定回环服务）：不停止微软 SSH 系统服务或修改其其他用途。
    pub fn stop(&self) -> Result<bool, String> {
        self.endpoints.remove(BridgeChannel::Management);
        self.endpoints.remove(BridgeChannel::File);
        let Some(mut running) = self
            .running
            .lock()
            .map_err(|_| "远程服务状态不可用。")?
            .take()
        else {
            return Ok(false);
        };
        running.file_shutdown.store(true, Ordering::Release);
        let management_result = running.management.stop();
        if let Some(worker) = running.file_worker.take() {
            worker
                .join()
                .map_err(|_| "回环文件服务线程异常退出。".to_owned())?;
        }
        management_result?;
        Ok(true)
    }

    /// is_running（检查实际状态）：管理、文件和端点查询入口必须都存活。
    pub fn management_service(&self) -> Option<Arc<RemoteManagementService>> {
        self.running.lock().ok()?.as_ref().map(|running| running.management_service.clone())
    }

    pub fn is_running(&self) -> bool {
        self.running
            .lock()
            .ok()
            .and_then(|running| running.as_ref().map(RunningServices::is_running))
            .unwrap_or(false)
    }
}

fn services_are_running(
    management_running: bool,
    file_running: bool,
    endpoints_running: bool,
) -> bool {
    management_running && file_running && endpoints_running
}

impl ValidatingFileReceiver {
    fn validate_peer_identity(&self, peer: &FileChannelPeer) -> Result<(), String> {
        if peer.workspace_id != self.workspace_id || peer.owner_sid != self.owner_sid {
            return Err("文件通道工作区或 Windows 登录账户身份与当前服务不匹配。".into());
        }
        let trust = DeviceTrustStore::open(&self.data_root)?;
        if trust.snapshot().state != "valid" || trust.trusted_public_key(&peer.device_id).is_none()
        {
            return Err("文件通道客户端当前未获服务端信任。".into());
        }
        // build（正式构建编号）随 forced-command（强制命令）验证，此处再比对当前受管发行身份。
        if peer.build != self.build {
            return Err("文件通道客户端桥接构建与当前服务端不匹配。".into());
        }
        Ok(())
    }

    fn validate_active_broadcast(&self) -> Result<(), String> {
        match voice_desktop_common::device_config::load_from_root(&self.data_root) {
            voice_desktop_common::device_config::LoadResult::Valid(config)
                if config.mode == voice_desktop_common::device_config::RunMode::Local
                    && config.broadcast_enabled => {}
            _ => return Err("服务端已关闭本地广播，拒绝新的文件导入会话。".into()),
        };
        Ok(())
    }
}

impl FileReceiver for ValidatingFileReceiver {
    fn application_update(&self, peer: &FileChannelPeer) -> Result<Box<dyn voice_desktop_common::lan_update::Handler>, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        crate::lan_update::ServerHandler::new(self.app.clone(),peer.clone())
    }
    fn connection_error(&self, error: &str) {
        let _ = voice_desktop_common::logs::record_device_channel_error(
            &self.diagnostic_root,
            "file",
            error,
        );
    }

    fn begin(
        &self,
        peer: &FileChannelPeer,
        request: UploadBegin,
    ) -> Result<Option<serde_json::Value>, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        self.coordinator.begin(peer, request)
    }
    fn append_chunk(
        &self,
        peer: &FileChannelPeer,
        source_id: &str,
        file_index: usize,
        offset: u64,
        chunk_sha256: &str,
        bytes: &[u8],
    ) -> Result<u64, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        self.coordinator
            .append_chunk(peer, source_id, file_index, offset, chunk_sha256, bytes)
    }
    fn finish(&self, peer: &FileChannelPeer, source_id: &str) -> Result<serde_json::Value, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        self.coordinator.finish(peer, source_id)
    }
    fn abort(&self, peer: &FileChannelPeer, source_id: &str) -> Result<(), String> {
        self.validate_peer_identity(peer)?;
        self.coordinator.abort(peer, source_id)
    }

    fn begin_device_file_transfer(
        &self,
        peer: &FileChannelPeer,
        request: DeviceFileTransferBegin,
        authorization: FileTransferAuthorization,
    ) -> Result<DeviceFileTransferSession, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        request.validate()?;
        authorization.validate()?;
        if authorization.device_id != peer.device_id {
            return Err("文件网关签名设备编号与 SSH forced-command 桥接身份不一致。".into());
        }
        let body = serde_json::to_value(&request).map_err(|_| "固定文件网关开始请求无法编码。")?;
        let mut stream = connect_file_gateway(&self.endpoints)?;
        let (status, response) =
            file_gateway_exchange(&mut stream, &body, Some(&authorization), None)?;
        if status != 200 {
            return Err(file_gateway_error(status, &response));
        }
        let transfer = response
            .get("transfer")
            .ok_or("固定文件网关开始回执缺少 transfer（文件传输）。")?;
        let transfer_id = transfer
            .get("transfer_id")
            .and_then(serde_json::Value::as_str)
            .ok_or("固定文件网关回执缺少原传输编号。")?;
        let channel_token = transfer
            .get("channel_token")
            .and_then(serde_json::Value::as_str)
            .ok_or("固定文件网关回执缺少短期通道令牌。")?;
        let operation = transfer
            .get("operation")
            .and_then(serde_json::Value::as_str)
            .ok_or("固定文件网关回执缺少通道操作。")?;
        let total_bytes = transfer
            .get("total_bytes")
            .and_then(serde_json::Value::as_u64)
            .ok_or("固定文件网关回执缺少文件长度。")?;
        let next_offset = transfer
            .get("next_offset")
            .and_then(serde_json::Value::as_u64)
            .ok_or("固定文件网关回执缺少文件偏移。")?;
        let sha256 = transfer
            .get("sha256")
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned);
        let ready = transfer
            .get("ready")
            .and_then(serde_json::Value::as_bool)
            .ok_or("固定文件网关回执缺少 ticket 状态。")?;
        let session = DeviceFileTransferSession {
            device_id: peer.device_id.clone(),
            transfer_id: transfer_id.to_owned(),
            channel_token: channel_token.to_owned(),
            operation: operation.to_owned(),
            connection_generation: request.connection_generation,
            executor_generation: request.executor_generation,
            total_bytes,
            sha256,
            next_offset,
            ready,
        };
        if session.transfer_id != request.transfer_id
            || session.operation != request.operation
            || session.total_bytes > request.maximum_bytes
            || session.next_offset > session.total_bytes
            || !(32..=128).contains(&session.channel_token.len())
            || !session
                .channel_token
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
            || (session.ready
                && session
                    .sha256
                    .as_deref()
                    .map_or(true, |value| !valid_sha256(value)))
            || (request.operation == "file.sftp.download"
                && (!session.ready
                    || session.sha256.as_deref() != request.expected_sha256.as_deref()))
        {
            return Err("固定文件网关回执与原 DSH 票据或设备代次不匹配。".into());
        }
        let mut gateways = self
            .file_gateways
            .lock()
            .map_err(|_| "固定文件网关连接状态不可用。")?;
        if gateways.len() >= 32 || gateways.contains_key(&session.channel_token) {
            let _ = send_file_gateway_action(
                &mut stream,
                peer,
                &session,
                "cancel",
                serde_json::json!({
                    "fields":{"protocol_version":"设备文件通道协议版本","action":"固定文件通道操作",
                        "device_id":"受信 SSH 强制命令客户端编号","connection_generation":"设备连接代次",
                        "executor_generation":"设备执行器代次","transfer_id":"原 DSH 文件传输编号",
                        "channel_token":"原票据签发的短期通道能力令牌"}
                }),
            );
            return Err("固定文件网关并行会话达到本机上限。".into());
        }
        gateways.insert(session.channel_token.clone(), stream);
        Ok(session)
    }

    fn append_device_file_chunk(
        &self,
        peer: &FileChannelPeer,
        session: &DeviceFileTransferSession,
        offset: u64,
        chunk_sha256: &str,
        bytes: &[u8],
    ) -> Result<u64, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        validate_file_session_peer(peer, session, "file.sftp.upload")?;
        if bytes.is_empty()
            || bytes.len() > device_file_channel::MAX_FILE_CHUNK_BYTES
            || sha256_hex(bytes) != chunk_sha256
        {
            return Err("固定文件网关上传分块为空、超限或摘要不匹配。".into());
        }
        let payload = file_gateway_payload(
            peer,
            session,
            "upload_chunk",
            serde_json::json!({
                "offset": offset,
                "chunk_sha256": chunk_sha256,
                "fields": {
                    "protocol_version":"设备文件通道协议版本","action":"固定文件通道操作",
                    "device_id":"受信 SSH 强制命令客户端编号","connection_generation":"设备连接代次",
                    "executor_generation":"设备执行器代次","transfer_id":"原 DSH 文件传输编号",
                    "channel_token":"原票据签发的短期通道能力令牌","offset":"当前原始文件字节偏移",
                    "chunk_sha256":"当前原始分块 SHA-256 摘要"
                }
            }),
        );
        let mut gateways = self
            .file_gateways
            .lock()
            .map_err(|_| "固定文件网关连接状态不可用。")?;
        let stream = gateways
            .get_mut(&session.channel_token)
            .ok_or("固定文件网关会话已经关闭。")?;
        let (status, response) = file_gateway_exchange(stream, &payload, None, Some(bytes))?;
        if status != 200 {
            return Err(file_gateway_error(status, &response));
        }
        response
            .get("transfer")
            .and_then(|item| item.get("next_offset"))
            .and_then(serde_json::Value::as_u64)
            .ok_or_else(|| "固定文件网关上传回执缺少下一偏移。".into())
    }

    fn read_device_file_chunk(
        &self,
        peer: &FileChannelPeer,
        session: &DeviceFileTransferSession,
        offset: u64,
        limit: usize,
    ) -> Result<DeviceFileTransferChunk, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        validate_file_session_peer(peer, session, "file.sftp.download")?;
        if limit == 0 || limit > device_file_channel::MAX_FILE_CHUNK_BYTES {
            return Err("固定文件网关下载分块上限无效。".into());
        }
        let payload = file_gateway_payload(
            peer,
            session,
            "read_chunk",
            serde_json::json!({
                "offset": offset,
                "limit": limit,
                "fields": {
                    "protocol_version":"设备文件通道协议版本","action":"固定文件通道操作",
                    "device_id":"受信 SSH 强制命令客户端编号","connection_generation":"设备连接代次",
                    "executor_generation":"设备执行器代次","transfer_id":"原 DSH 文件传输编号",
                    "channel_token":"原票据签发的短期通道能力令牌","offset":"请求读取的原始文件偏移",
                    "limit":"本次读取最大字节数"
                }
            }),
        );
        let mut gateways = self
            .file_gateways
            .lock()
            .map_err(|_| "固定文件网关连接状态不可用。")?;
        let stream = gateways
            .get_mut(&session.channel_token)
            .ok_or("固定文件网关会话已经关闭。")?;
        let (status, response) = file_gateway_exchange(stream, &payload, None, None)?;
        if status != 200 {
            return Err(file_gateway_error(status, &response));
        }
        let transfer = response
            .get("transfer")
            .ok_or("固定文件网关下载回执缺少 transfer。")?;
        let expected_offset = offset;
        let transfer_id = transfer
            .get("transfer_id")
            .and_then(serde_json::Value::as_str)
            .ok_or("固定文件网关下载回执缺少原传输编号。")?;
        let total_bytes = transfer
            .get("total_bytes")
            .and_then(serde_json::Value::as_u64)
            .ok_or("固定文件网关下载回执缺少完整长度。")?;
        let sha256 = transfer
            .get("sha256")
            .and_then(serde_json::Value::as_str)
            .ok_or("固定文件网关下载回执缺少完整摘要.")?
            .to_owned();
        let chunk_sha256 = transfer
            .get("chunk_sha256")
            .and_then(serde_json::Value::as_str)
            .ok_or("固定文件网关下载回执缺少分块摘要。")?
            .to_owned();
        let byte_length = transfer
            .get("bytes")
            .and_then(serde_json::Value::as_u64)
            .and_then(|value| usize::try_from(value).ok())
            .ok_or("固定文件网关下载回执字节长度无效。")?;
        let encoded = transfer
            .get("data_base64")
            .and_then(serde_json::Value::as_str)
            .ok_or("固定文件网关下载回执缺少分块负载。")?;
        let bytes = device_file_channel::decode_file_channel_base64(encoded)?;
        let offset = transfer
            .get("offset")
            .and_then(serde_json::Value::as_u64)
            .ok_or("固定文件网关下载回执缺少原始偏移。")?;
        if transfer_id != session.transfer_id
            || offset != expected_offset
            || total_bytes != session.total_bytes
            || Some(sha256.as_str()) != session.sha256.as_deref()
            || bytes.len() != byte_length
            || bytes.len() > limit
            || sha256_hex(&bytes) != chunk_sha256
        {
            return Err("固定文件网关下载回执与原 DFT 票据不匹配。".into());
        }
        Ok(DeviceFileTransferChunk {
            transfer_id: transfer_id.to_owned(),
            offset,
            total_bytes,
            sha256,
            chunk_sha256,
            bytes,
        })
    }

    fn finish_device_file_transfer(
        &self,
        peer: &FileChannelPeer,
        session: &DeviceFileTransferSession,
        expected_bytes: u64,
        expected_sha256: &str,
    ) -> Result<serde_json::Value, String> {
        self.validate_peer_identity(peer)?;
        self.validate_active_broadcast()?;
        validate_file_session_peer(peer, session, &session.operation)?;
        if expected_bytes != session.total_bytes
            || !valid_sha256(expected_sha256)
            || (session.operation == "file.sftp.download"
                && session.sha256.as_deref() != Some(expected_sha256))
        {
            return Err("固定文件通道完成声明与原 DFT 票据长度或摘要不一致。".into());
        }
        let action = if session.operation == "file.sftp.upload" {
            "finish_upload"
        } else {
            "finish_download"
        };
        let payload = file_gateway_payload(
            peer,
            session,
            action,
            serde_json::json!({
                "expected_bytes": expected_bytes,
                "expected_sha256": expected_sha256,
                "fields": {
                    "protocol_version":"设备文件通道协议版本","action":"固定文件通道操作",
                    "device_id":"受信 SSH 强制命令客户端编号","connection_generation":"设备连接代次",
                    "executor_generation":"设备执行器代次","transfer_id":"原 DSH 文件传输编号",
                    "channel_token":"原票据签发的短期通道能力令牌","expected_bytes":"完整原始文件字节数",
                    "expected_sha256":"完整原始文件 SHA-256 摘要"
                }
            }),
        );
        let mut gateways = self
            .file_gateways
            .lock()
            .map_err(|_| "固定文件网关连接状态不可用。")?;
        let stream = gateways
            .get_mut(&session.channel_token)
            .ok_or("固定文件网关会话已经关闭。")?;
        let (status, response) = file_gateway_exchange(stream, &payload, None, None)?;
        if status != 200 {
            return Err(file_gateway_error(status, &response));
        }
        let result = response
            .get("transfer")
            .cloned()
            .ok_or("固定文件网关完成回执缺少原票据结果。")?;
        if result
            .get("transfer_id")
            .and_then(serde_json::Value::as_str)
            != Some(session.transfer_id.as_str())
            || result.get("state").and_then(serde_json::Value::as_str) != Some("ready")
            || result.get("bytes").and_then(serde_json::Value::as_u64) != Some(expected_bytes)
            || result.get("sha256").and_then(serde_json::Value::as_str) != Some(expected_sha256)
        {
            return Err("固定文件网关完成回执与原 DSH 票据长度或摘要不匹配。".into());
        }
        gateways.remove(&session.channel_token);
        Ok(result)
    }

    fn cancel_device_file_transfer(
        &self,
        peer: &FileChannelPeer,
        session: &DeviceFileTransferSession,
    ) -> Result<(), String> {
        self.validate_peer_identity(peer)?;
        validate_file_session_peer(peer, session, &session.operation)?;
        let payload = file_gateway_payload(
            peer,
            session,
            "cancel",
            serde_json::json!({
                "fields": {
                    "protocol_version":"设备文件通道协议版本","action":"固定文件通道操作",
                    "device_id":"受信 SSH 强制命令客户端编号","connection_generation":"设备连接代次",
                    "executor_generation":"设备执行器代次","transfer_id":"原 DSH 文件传输编号",
                    "channel_token":"原票据签发的短期通道能力令牌"
                }
            }),
        );
        let mut stream = self
            .file_gateways
            .lock()
            .map_err(|_| "固定文件网关连接状态不可用。")?
            .remove(&session.channel_token);
        if stream.is_none() {
            stream = Some(connect_file_gateway(&self.endpoints)?);
        }
        let mut stream = stream.ok_or("固定文件网关取消连接不可用。")?;
        let (status, response) = file_gateway_exchange(&mut stream, &payload, None, None)?;
        if status != 200 {
            return Err(file_gateway_error(status, &response));
        }
        let cancelled = response
            .get("transfer")
            .and_then(|value| value.get("cancelled"))
            .and_then(serde_json::Value::as_bool)
            == Some(true);
        if cancelled {
            Ok(())
        } else {
            Err("固定文件网关没有确认取消。".into())
        }
    }
}

fn validate_file_session_peer(
    peer: &FileChannelPeer,
    session: &DeviceFileTransferSession,
    expected_operation: &str,
) -> Result<(), String> {
    if peer.device_id != session.device_id
        || session.operation != expected_operation
        || session.transfer_id.len() != 32
        || !session
            .transfer_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || session.connection_generation == 0
        || session.executor_generation == 0
        || session.total_bytes > 4 * 1024 * 1024 * 1024
        || session.channel_token.len() < 32
        || session.channel_token.len() > 128
    {
        return Err("固定 FILE 会话与受信设备、操作或 DSH 票据身份不匹配。".into());
    }
    Ok(())
}

fn file_gateway_payload(
    peer: &FileChannelPeer,
    session: &DeviceFileTransferSession,
    action: &str,
    extras: serde_json::Value,
) -> serde_json::Value {
    let mut payload = serde_json::json!({
        "protocol_version": 1,
        "action": action,
        "device_id": peer.device_id,
        "connection_generation": session.connection_generation,
        "executor_generation": session.executor_generation,
        "transfer_id": session.transfer_id,
        "channel_token": session.channel_token,
    });
    if let (Some(target), Some(extra)) = (payload.as_object_mut(), extras.as_object()) {
        for (key, value) in extra {
            target.insert(key.clone(), value.clone());
        }
    }
    payload
}

fn send_file_gateway_action(
    stream: &mut TcpStream,
    peer: &FileChannelPeer,
    session: &DeviceFileTransferSession,
    action: &str,
    extras: serde_json::Value,
) -> Result<(u16, serde_json::Value), String> {
    let payload = file_gateway_payload(peer, session, action, extras);
    file_gateway_exchange(stream, &payload, None, None)
}

fn connect_file_gateway(endpoints: &EndpointDirectory) -> Result<TcpStream, String> {
    let stream = endpoints.connect(BridgeChannel::Gateway)?;
    stream
        .set_nodelay(true)
        .map_err(|_| "无法配置固定文件网关连接。")?;
    stream
        .set_read_timeout(Some(Duration::from_secs(60)))
        .map_err(|_| "无法配置固定文件网关读取超时。")?;
    stream
        .set_write_timeout(Some(Duration::from_secs(60)))
        .map_err(|_| "无法配置固定文件网关写入超时。")?;
    Ok(stream)
}

fn file_gateway_exchange(
    stream: &mut TcpStream,
    body: &serde_json::Value,
    authorization: Option<&FileTransferAuthorization>,
    chunk: Option<&[u8]>,
) -> Result<(u16, serde_json::Value), String> {
    let path = FILE_TRANSFER_GATEWAY_PATH;
    if path != "/v1/file-transfer" {
        return Err("固定设备文件网关路由常量无效。".into());
    }
    let payload = serde_json::to_vec(body).map_err(|_| "固定设备文件网关请求不是 JSON。")?;
    if payload.is_empty() || payload.len() > 128 * 1024 {
        return Err("固定设备文件网关请求超过 128 KiB 上限。".into());
    }
    let mut headers = String::new();
    if let Some(authorization) = authorization {
        authorization.validate()?;
        headers = format!(
            "X-Voice-Terminal-Device-ID: {}\r\nX-Voice-Terminal-Core-Session-ID: {}\r\nX-Voice-Terminal-Challenge: {}\r\nX-Voice-Terminal-Timestamp-Ms: {}\r\nX-Voice-Terminal-Signature: {}\r\n",
            authorization.device_id, authorization.core_session_id, authorization.challenge,
            authorization.timestamp_ms, authorization.signature,
        );
    }
    let (content_type, length) = match chunk {
        Some(chunk) if payload.len() <= 4096 => {
            ("application/octet-stream", 4 + payload.len() + chunk.len())
        }
        Some(_) => return Err("设备文件二进制分块元数据超过大小上限。".into()),
        None => ("application/json", payload.len()),
    };
    let authority = stream
        .peer_addr()
        .map_err(|_| "无法确认受管网关实际地址。")?;
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: {authority}\r\nAccept: application/json\r\nContent-Type: {content_type}\r\n{headers}Connection: keep-alive\r\nContent-Length: {length}\r\n\r\n",
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|_| "固定设备文件网关请求头写入失败。")?;
    if chunk.is_some() {
        stream
            .write_all(&(payload.len() as u32).to_be_bytes())
            .map_err(|_| "固定设备文件网关二进制元数据长度写入失败。")?;
    }
    stream
        .write_all(&payload)
        .map_err(|_| "固定设备文件网关请求体写入失败。")?;
    if let Some(chunk) = chunk {
        stream
            .write_all(chunk)
            .map_err(|_| "固定设备文件网关原始分块写入失败。")?;
    }
    stream
        .flush()
        .map_err(|_| "固定设备文件网关请求刷新失败。")?;
    read_file_gateway_response(stream)
}

fn read_file_gateway_response(stream: &mut TcpStream) -> Result<(u16, serde_json::Value), String> {
    let mut headers = Vec::with_capacity(1024);
    let mut byte = [0_u8; 1];
    while headers.len() < 16 * 1024 {
        stream
            .read_exact(&mut byte)
            .map_err(|_| "固定设备文件网关响应头读取失败。")?;
        headers.push(byte[0]);
        if headers.ends_with(b"\r\n\r\n") {
            break;
        }
    }
    if !headers.ends_with(b"\r\n\r\n") {
        return Err("固定设备文件网关响应头超过 16 KiB。".into());
    }
    let text = std::str::from_utf8(&headers).map_err(|_| "固定设备文件网关响应头不是 UTF-8。")?;
    let mut lines = text.split("\r\n");
    let status_line = lines.next().ok_or("固定设备文件网关缺少 HTTP 状态行。")?;
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|value| value.parse::<u16>().ok())
        .filter(|value| (100..=599).contains(value))
        .ok_or("固定设备文件网关 HTTP 状态码无效。")?;
    let mut content_length = None;
    for line in lines {
        if line.is_empty() {
            break;
        }
        let (name, value) = line
            .split_once(':')
            .ok_or("固定设备文件网关响应头格式无效。")?;
        if name.eq_ignore_ascii_case("content-length") {
            if content_length.is_some() {
                return Err("固定设备文件网关响应包含重复 Content-Length。".into());
            }
            content_length = Some(
                value
                    .trim()
                    .parse::<usize>()
                    .map_err(|_| "固定设备文件网关响应长度无效。")?,
            );
        }
        if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err("固定设备文件网关不接受分块编码响应。".into());
        }
    }
    let content_length = content_length
        .filter(|value| *value <= 256 * 1024)
        .ok_or("固定设备文件网关响应缺少长度或超过 256 KiB。")?;
    let mut body = vec![0_u8; content_length];
    stream
        .read_exact(&mut body)
        .map_err(|_| "固定设备文件网关响应体不完整。")?;
    let body = serde_json::from_slice(&body).map_err(|_| "固定设备文件网关响应体不是 JSON。")?;
    Ok((status, body))
}

fn file_gateway_error(status: u16, response: &serde_json::Value) -> String {
    let detail = response
        .get("detail")
        .and_then(serde_json::Value::as_str)
        .or_else(|| response.get("error").and_then(serde_json::Value::as_str))
        .unwrap_or("固定设备文件网关拒绝了原票据操作。");
    format!(
        "固定设备文件网关返回 HTTP {status}：{}",
        detail.chars().take(240).collect::<String>()
    )
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    #[test]
    fn dynamic_listeners_are_distinct_and_loopback_only() {
        let management_listener = std::net::TcpListener::bind(super::DYNAMIC_LOOPBACK).unwrap();
        let file_listener = std::net::TcpListener::bind(super::DYNAMIC_LOOPBACK).unwrap();
        let management = management_listener.local_addr().unwrap();
        let file = file_listener.local_addr().unwrap();
        assert_ne!(management.port(), file.port());
        assert!(management.port() > 0 && file.port() > 0);
        assert_eq!(
            management.ip(),
            std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)
        );
        assert_eq!(
            file.ip(),
            std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)
        );
    }

    #[test]
    fn broadcast_service_requires_both_management_and_file_listeners() {
        assert!(!super::services_are_running(false, false, true));
        assert!(!super::services_are_running(false, true, true));
        assert!(!super::services_are_running(true, false, true));
        assert!(!super::services_are_running(true, true, false));
        assert!(super::services_are_running(true, true, true));
    }
}
