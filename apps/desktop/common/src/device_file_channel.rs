//! DeviceFileChannel（设备文件通道）：独立于语音核心的受限、流式 SSH 文件通道。
use crate::device_management_contract::DeviceBridgePrelude;
use crate::remote_import_contract::{RemoteImportFileEntry, RemoteImportManifest};
#[cfg(any(test, feature = "ssh-device"))]
use crate::remote_import_contract::{REMOTE_IMPORT_MAX_BYTES, REMOTE_IMPORT_MAX_FILES};
use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap},
    fs,
    io::{self, Read, Write},
    net::TcpListener,
    path::Path,
    sync::atomic::{AtomicBool, Ordering},
};
#[cfg(any(test, feature = "ssh-device"))]
use std::{
    path::{Component, PathBuf},
    time::SystemTime,
};

pub const FILE_CHANNEL_PORT: u16 = 18_769; // FILE_CHANNEL_PORT（SSH FILE 专用桥接的回环端口）。
pub const FILE_CHANNEL_MARKER: &str = "voice-terminal-v1-file"; // FILE_CHANNEL_MARKER（固定 SSH 文件通道标记）。
pub const MAX_FILE_CHUNK_BYTES: usize = 64 * 1024; // MAX_FILE_CHUNK_BYTES（沿用设备文件传输内核的单块上限）。
pub const MAX_FILE_CONTROL_BYTES: usize = 8 * 1024 * 1024; // MAX_FILE_CONTROL_BYTES（文件清单控制帧上限）。
pub const FILE_TRANSFER_GATEWAY_PATH: &str = "/v1/file-transfer"; // FILE_TRANSFER_GATEWAY_PATH（唯一固定 DSH 文件操作路由）。
const MAX_CONCURRENT_FILE_CHANNELS: usize = 32; // MAX_CONCURRENT_FILE_CHANNELS（本机文件通道最大并行连接数）。
const FILE_CHANNEL_SHUTDOWN_DRAIN: std::time::Duration = std::time::Duration::from_secs(5); // FILE_CHANNEL_SHUTDOWN_DRAIN（关闭时等待活动接收线程的上限）。
const MAX_RELATIVE_PATH_COMPONENTS: usize = 128; // MAX_RELATIVE_PATH_COMPONENTS（单个规范文件路径的层级上限）。
const MAX_TOTAL_PATH_COMPONENTS: usize = 8_192; // MAX_TOTAL_PATH_COMPONENTS（完整导入树的总路径层级预算）。
#[cfg(feature = "ssh-device")]
const MAX_SELECTED_SOURCE_ROOTS: usize = 64; // MAX_SELECTED_SOURCE_ROOTS（本机一次可选择的文件或文件夹根数）。
#[cfg(any(test, feature = "ssh-device"))]
const MAX_SOURCE_TREE_ENTRIES: usize = 8_192; // MAX_SOURCE_TREE_ENTRIES（本机目录扫描的文件与目录总项上限）。
const MAX_PRELUDE_BYTES: usize = 16 * 1024; // MAX_PRELUDE_BYTES（本机桥接前导行上限）。

/// serve_file_channel_listener（服务本机 FILE 监听器）：只接受 loopback（回环）套接字并逐连接分派。
pub fn serve_file_channel_listener(
    listener: TcpListener,
    receiver: std::sync::Arc<dyn FileReceiver>,
    shutdown: std::sync::Arc<AtomicBool>,
) -> Result<(), String> {
    let address = listener
        .local_addr()
        .map_err(|_| "无法读取本机文件通道监听地址。".to_string())?;
    if !address.ip().is_loopback() {
        return Err("文件通道只允许绑定本机回环地址。".into());
    }
    listener
        .set_nonblocking(true)
        .map_err(|_| "无法配置本机文件通道监听器。".to_string())?;
    let mut next_connection_id = 1_usize;
    let mut active_streams = HashMap::new();
    let mut workers: HashMap<usize, std::thread::JoinHandle<()>> = HashMap::new();
    let mut accept_error = false;
    while !shutdown.load(Ordering::Acquire) {
        reap_finished_workers(&mut active_streams, &mut workers);
        match listener.accept() {
            Ok((stream, peer_address)) => {
                if !peer_address.ip().is_loopback() || workers.len() >= MAX_CONCURRENT_FILE_CHANNELS
                {
                    continue;
                }
                let shutdown_stream = match stream.try_clone() {
                    Ok(stream) => stream,
                    Err(_) => continue,
                };
                let stream = match crate::device_socket::CancellableSocket::new(
                    stream,
                    shutdown.clone(),
                    std::time::Duration::from_secs(10 * 60),
                    std::time::Duration::from_secs(10 * 60),
                ) {
                    Ok(stream) => stream,
                    Err(error) => {
                        receiver.connection_error(&format!("无法配置文件通道可取消读取：{error}"));
                        continue;
                    }
                };
                let connection_id = next_connection_id;
                next_connection_id = next_connection_id.wrapping_add(1).max(1);
                let receiver = receiver.clone();
                let connection_shutdown = shutdown.clone();
                let worker = std::thread::Builder::new()
                    .name(format!("voice-file-channel-{connection_id}"))
                    .spawn(move || {
                        if let Err(error) = serve_file_channel_connection(stream, receiver.as_ref())
                        {
                            if !connection_shutdown.load(Ordering::Acquire) {
                                receiver.connection_error(&error);
                            }
                        }
                    });
                match worker {
                    Ok(worker) => {
                        active_streams.insert(connection_id, shutdown_stream);
                        workers.insert(connection_id, worker);
                    }
                    Err(_) => continue,
                }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => {
                accept_error = true;
                break;
            }
        }
    }
    for stream in active_streams.values() {
        let _ = stream.shutdown(std::net::Shutdown::Both);
    }
    let deadline = std::time::Instant::now() + FILE_CHANNEL_SHUTDOWN_DRAIN;
    while !workers.is_empty() && std::time::Instant::now() < deadline {
        reap_finished_workers(&mut active_streams, &mut workers);
        if !workers.is_empty() {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
    }
    let timed_out = !workers.is_empty();
    for (connection_id, worker) in workers.drain() {
        if worker.is_finished() {
            let _ = worker.join();
        }
        active_streams.remove(&connection_id);
    }
    if accept_error {
        return Err("本机文件通道接受连接失败。".into());
    }
    if timed_out {
        return Err("关闭文件通道时有接收线程超过 5 秒收尾期限。".into());
    }
    Ok(())
}

fn reap_finished_workers(
    active_streams: &mut HashMap<usize, std::net::TcpStream>,
    workers: &mut HashMap<usize, std::thread::JoinHandle<()>>,
) {
    let finished = workers
        .iter()
        .filter_map(|(connection_id, worker)| worker.is_finished().then_some(*connection_id))
        .collect::<Vec<_>>();
    for connection_id in finished {
        if let Some(worker) = workers.remove(&connection_id) {
            let _ = worker.join();
        }
        active_streams.remove(&connection_id);
    }
}

/// reject_reparse_chain（拒绝链接路径）：供 Tauri 原生导入协调器复用通用安全路径检查。
pub fn reject_reparse_chain(path: &Path) -> Result<(), String> {
    crate::device_files::reject_reparse_chain(path)
}

/// metadata_is_reparse（检查重解析项）：识别 Windows 目录联接与符号链接。
pub fn metadata_is_reparse(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_symlink() || crate::workspace::metadata_reparse(metadata)
}

/// FileChannelPeer（已认证文件通道对端）：来源于本机桥接前导帧，不来自远端业务 JSON。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileChannelPeer {
    pub device_id: String, // device_id（SSH 强制命令已绑定并验证的客户端设备编号）。
    pub workspace_id: String, // workspace_id（桥接器已验证的工作区编号）。
    pub owner_sid: String, // owner_sid（桥接器已验证的 Windows 登录账户 SID）。
    pub build: String,     // build（桥接器已验证的正式程序构建编号）。
}

impl TryFrom<DeviceBridgePrelude> for FileChannelPeer {
    type Error = String;

    fn try_from(prelude: DeviceBridgePrelude) -> Result<Self, Self::Error> {
        prelude.validate()?;
        Ok(Self {
            device_id: prelude.client_device_id,
            workspace_id: prelude.workspace_id,
            owner_sid: prelude.owner_sid,
            build: prelude.build,
        })
    }
}

/// UploadBegin（上传清单申请）：只含服务端签发的来源编号、稳定请求编号和相对路径清单。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct UploadBegin {
    pub source_id: String, // source_id（管理通道预先签发的服务端来源编号；不接受客户端新建）。
    pub request_id: String, // request_id（与来源预留操作相同的稳定业务请求编号）。
    pub total_bytes: u64,  // total_bytes（所有文件的原始字节数之和）。
    pub files: Vec<RemoteImportFileEntry>, // files（每个文件的规范相对路径、长度和 SHA-256 摘要）。
}

impl UploadBegin {
    /// validate（校验上传清单）：把共享清单验证器用于服务端票据绑定前的纯结构检查。
    pub fn validate(&self) -> Result<(), String> {
        if self.request_id.len() != 32
            || !self
                .request_id
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err("远程文件上传的稳定请求编号格式无效。".into());
        }
        let mut total_components = 0_usize;
        for file in &self.files {
            let components = file.relative_path.split('/').count();
            if components > MAX_RELATIVE_PATH_COMPONENTS {
                return Err("远程导入单个相对路径层级超过 128 层。".into());
            }
            total_components = total_components
                .checked_add(components)
                .filter(|count| *count <= MAX_TOTAL_PATH_COMPONENTS)
                .ok_or_else(|| "远程导入目录结构超过 8192 个相对路径层级。".to_owned())?;
        }
        RemoteImportManifest {
            source_id: self.source_id.clone(),
            total_bytes: self.total_bytes,
            files: self.files.clone(),
        }
        .validate()
    }
}

/// FileTransferAuthorization（文件操作签名证明）：签名字段来自已批准设备身份。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FileTransferAuthorization {
    pub device_id: String,       // device_id（设备信任身份编号）。
    pub core_session_id: String, // core_session_id（签名挑战所属核心运行编号）。
    pub challenge: String,       // challenge（针对固定 /v1/file-transfer 的一次性挑战）。
    pub timestamp_ms: u64,       // timestamp_ms（签名时的 Unix 毫秒时间）。
    pub signature: String,       // signature（Ed25519 签名的 Base64 证明）。
}

impl FileTransferAuthorization {
    pub fn validate(&self) -> Result<(), String> {
        let device_ok = crate::device_management_contract::valid_host_id(&self.device_id);
        let session_ok = self.core_session_id.len() == 32
            && self
                .core_session_id
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
        let challenge_ok = (32..=128).contains(&self.challenge.len())
            && self
                .challenge
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'));
        let signature_ok = base64::engine::general_purpose::STANDARD
            .decode(self.signature.as_bytes())
            .is_ok_and(|signature| signature.len() == 64);
        if !device_ok || !session_ok || !challenge_ok || self.timestamp_ms == 0 || !signature_ok {
            return Err("设备文件操作签名证明字段无效。".into());
        }
        Ok(())
    }
}

/// DeviceFileTransferBegin（原票据通道申请）：只携带固定 DSH 操作、代次与长度摘要。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeviceFileTransferBegin {
    pub protocol_version: u32, // protocol_version（固定设备文件通道协议版本）。
    pub action: String,        // action（固定为 begin 的原票据通道申请）。
    pub transfer_id: String,   // transfer_id（原 DSH 文件传输票据编号）。
    pub operation: String,     // operation（兼容保留的 file.sftp.upload/download 任务编号）。
    pub parent_request_id: String, // parent_request_id（生成原 DSH 文件票据的稳定请求编号）。
    pub connection_generation: u64, // connection_generation（当前 SSH 设备连接代次）。
    pub executor_generation: u64, // executor_generation（当前设备执行器代次）。
    pub maximum_bytes: u64,    // maximum_bytes（原审批允许的最大文件字节数）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected_sha256: Option<String>, // expected_sha256（下载时原票据锁定的 SHA-256 摘要）。
    pub fields: BTreeMap<String, String>, // fields（JSON 字段的逐项中文说明）。
}

impl DeviceFileTransferBegin {
    pub fn validate(&self) -> Result<(), String> {
        let transfer_ok = self.transfer_id.len() == 32
            && self
                .transfer_id
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
        let parent_ok = !self.parent_request_id.is_empty()
            && self.parent_request_id.len() <= 128
            && self.parent_request_id.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-')
            });
        let download = self.operation == "file.sftp.download";
        if self.protocol_version != 1
            || self.action != "begin"
            || !transfer_ok
            || !parent_ok
            || !matches!(
                self.operation.as_str(),
                "file.sftp.upload" | "file.sftp.download"
            )
            || self.connection_generation == 0
            || self.connection_generation > (1_u64 << 53) - 1
            || self.executor_generation == 0
            || self.executor_generation > (1_u64 << 53) - 1
            || self.maximum_bytes == 0
            || self.maximum_bytes > 4 * 1024 * 1024 * 1024
            || (download && !self.expected_sha256.as_deref().is_some_and(valid_sha256))
            || (!download && self.expected_sha256.is_some())
        {
            return Err("原 DSH 文件票据申请编号、操作、代次、大小上限或摘要无效。".into());
        }
        let mut expected = vec![
            "protocol_version",
            "action",
            "transfer_id",
            "operation",
            "parent_request_id",
            "connection_generation",
            "executor_generation",
            "maximum_bytes",
        ];
        if download {
            expected.push("expected_sha256");
        }
        validate_named_fields(&self.fields, &expected)
    }
}

/// DeviceFileTransferSession（服务端文件会话）：token 只由原生 FILE listener 保存。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeviceFileTransferSession {
    pub device_id: String,   // device_id（从强制命令桥接前导读取的受信设备编号）。
    pub transfer_id: String, // transfer_id（原 DSH 文件传输编号）。
    pub channel_token: String, // channel_token（仅原生服务与 Python DFT 间使用的短期能力令牌）。
    pub operation: String,   // operation（固定文件读取或写入任务编号）。
    pub connection_generation: u64, // connection_generation（票据绑定的连接代次）。
    pub executor_generation: u64, // executor_generation（票据绑定的执行器代次）。
    pub total_bytes: u64,    // total_bytes（服务端原票据锁定的完整字节数）。
    pub sha256: Option<String>, // sha256（服务端预先锁定或完成校验的完整摘要）。
    pub next_offset: u64,    // next_offset（原 DFT 状态机确认的下一上传偏移）。
    pub ready: bool,         // ready（原 DFT 票据是否已有完整且验证通过的内容）。
}

/// DeviceFileTransferChunk（文件会话分块）：原始负载只在单个 64 KiB 缓冲区内。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeviceFileTransferChunk {
    pub transfer_id: String,  // transfer_id（原 DSH 文件传输编号）。
    pub offset: u64,          // offset（当前文件内的原始字节偏移）。
    pub total_bytes: u64,     // total_bytes（服务端票据锁定的完整字节数）。
    pub sha256: String,       // sha256（服务端锁定的完整摘要）。
    pub chunk_sha256: String, // chunk_sha256（当前原始分块摘要）。
    pub bytes: Vec<u8>,       // bytes（单个有界原始文件分块）。
}

/// DeviceFileTransferReceipt（文件传输回执）：path 是稳定票据标识，不包含任何路径。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeviceFileTransferReceipt {
    pub path: String,        // path（固定 transfer:<id> 标识，不是文件路径）。
    pub bytes: u64,          // bytes（服务端复核的原始文件长度）。
    pub sha256: String,      // sha256（服务端复核的完整 SHA-256 摘要）。
    pub transfer_id: String, // transfer_id（原 DSH 文件传输编号）。
}

/// FileReceiver（原生文件接收器）：协调器持有票据并负责文件落盘与导入提交。
pub trait FileReceiver: Send + Sync + 'static {
    /// application_update（打开独立版本同步会话）：不经过组件导入或核心文件任务。
    fn application_update(&self, _peer: &FileChannelPeer) -> Result<Box<dyn crate::lan_update::Handler>, String> {
        Err("对端尚未支持局域网本体更新。".into())
    }
    /// connection_error（连接结束诊断）：只接收固定协议错误，不接收传输正文或凭据。
    fn connection_error(&self, _error: &str) {}

    /// begin（开始上传）：必须将签发来源编号与桥接认证对端绑定。
    /// Returns the original result（返回原结果）when the same request already committed.
    fn begin(&self, peer: &FileChannelPeer, request: UploadBegin) -> Result<Option<Value>, String>;

    /// append_chunk（写入分块）：只接收一个已校验分块，返回该文件下一次期望的偏移。
    fn append_chunk(
        &self,
        peer: &FileChannelPeer,
        source_id: &str,
        file_index: usize,
        offset: u64,
        chunk_sha256: &str,
        bytes: &[u8],
    ) -> Result<u64, String>;

    /// finish（完成上传）：只在全部文件通过复核后调用既有 ResourceManager 提交。
    fn finish(&self, peer: &FileChannelPeer, source_id: &str) -> Result<Value, String>;

    /// abort（取消上传）：断线或显式取消时回滚尚未提交的服务端暂存票据。
    fn abort(&self, peer: &FileChannelPeer, source_id: &str) -> Result<(), String>;

    /// begin_device_file_transfer（开始设备文件任务）：仅签名 begin 可开通原 ticket 能力令牌。
    fn begin_device_file_transfer(
        &self,
        _peer: &FileChannelPeer,
        _request: DeviceFileTransferBegin,
        _authorization: FileTransferAuthorization,
    ) -> Result<DeviceFileTransferSession, String> {
        Err("当前固定文件接收器未开放 DSH 设备文件通道。".into())
    }

    /// append_device_file_chunk（追加设备读取块）：偏移和摘要由原 DeviceFileTransferStore 确认。
    fn append_device_file_chunk(
        &self,
        _peer: &FileChannelPeer,
        _session: &DeviceFileTransferSession,
        _offset: u64,
        _chunk_sha256: &str,
        _bytes: &[u8],
    ) -> Result<u64, String> {
        Err("当前固定文件接收器未开放 DSH 设备文件上传。".into())
    }

    /// read_device_file_chunk（读取审批附件块）：返回既有 core_upload ticket 内容。
    fn read_device_file_chunk(
        &self,
        _peer: &FileChannelPeer,
        _session: &DeviceFileTransferSession,
        _offset: u64,
        _limit: usize,
    ) -> Result<DeviceFileTransferChunk, String> {
        Err("当前固定文件接收器未开放 DSH 设备文件下载。".into())
    }

    /// finish_device_file_transfer（结束设备文件任务）：由原 DFT 票据完成完整摘要复核。
    fn finish_device_file_transfer(
        &self,
        _peer: &FileChannelPeer,
        _session: &DeviceFileTransferSession,
        _expected_bytes: u64,
        _expected_sha256: &str,
    ) -> Result<Value, String> {
        Err("当前固定文件接收器未开放 DSH 设备文件完成操作。".into())
    }

    /// cancel_device_file_transfer（取消设备文件任务）：断线时回收未完成上传分块与短期令牌。
    fn cancel_device_file_transfer(
        &self,
        _peer: &FileChannelPeer,
        _session: &DeviceFileTransferSession,
    ) -> Result<(), String> {
        Ok(())
    }
}

#[derive(Debug, Deserialize)]
// type（固定消息类型）：begin、chunk、finish 或 cancel；其他通用 SSH 操作均不接受。
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum ClientFrame {
    ApplicationUpdate {
        request: crate::lan_update::Request, // request（稳定引导协议的本体同步请求）。
    },
    Begin {
        source_id: String,                 // source_id（管理通道签发的服务端来源编号）。
        request_id: String,                // request_id（与来源预留相同的稳定业务请求编号）。
        total_bytes: u64,                  // total_bytes（清单中的原始总字节数）。
        files: Vec<RemoteImportFileEntry>, // files（规范相对路径文件清单）。
        fields: BTreeMap<String, String>,  // fields（逐字段中文说明）。
    },
    Chunk {
        file_index: usize,                // file_index（当前文件在清单中的从零开始序号）。
        offset: u64,                      // offset（当前文件内的原始字节偏移）。
        byte_length: usize,               // byte_length（紧随控制帧的原始文件块长度）。
        chunk_sha256: String,             // chunk_sha256（当前原始文件块的 SHA-256 摘要）。
        fields: BTreeMap<String, String>, // fields（逐字段中文说明）。
    },
    Finish {
        fields: BTreeMap<String, String>,
    }, // fields（结束请求的字段中文说明）。
    Cancel {
        source_id: String,                // source_id（管理通道预先签发的服务端来源编号）。
        fields: BTreeMap<String, String>, // fields（逐字段中文说明）。
    },
    DeviceFileBegin {
        begin: DeviceFileTransferBegin, // begin（对原 DSH 文件票据的签名固定申请）。
        authorization: FileTransferAuthorization, // authorization（已批准设备身份的一次性签名证明）。
        fields: BTreeMap<String, String>,         // fields（文件帧字段中文说明）。
    },
    DeviceFileUploadChunk {
        transfer_id: String,  // transfer_id（当前 FILE 会话绑定的 DSH 文件编号）。
        offset: u64,          // offset（当前原始文件字节偏移）。
        byte_length: usize,   // byte_length（后续原始文件块长度）。
        chunk_sha256: String, // chunk_sha256（当前原始分块的 SHA-256 摘要）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
    DeviceFileDownloadChunk {
        transfer_id: String, // transfer_id（当前 FILE 会话绑定的 DSH 文件编号）。
        offset: u64,         // offset（请求读取的原始文件字节偏移）。
        limit: usize,        // limit（请求的最大分块字节数）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
    DeviceFileFinish {
        transfer_id: String,     // transfer_id（当前 FILE 会话绑定的 DSH 文件编号）。
        expected_bytes: u64,     // expected_bytes（客户端完整读取或发送的原始字节数）。
        expected_sha256: String, // expected_sha256（客户端完整文件 SHA-256 摘要）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
    DeviceFileCancel {
        transfer_id: String, // transfer_id（当前 FILE 会话绑定的 DSH 文件编号）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ServerFrame<'a> {
    Ready {
        chunk_bytes: usize, // chunk_bytes（允许的最大文件分块字节数）。
        file_count: usize,  // file_count（服务端接受的文件数）。
        total_bytes: u64,   // total_bytes（服务端接受的原始总字节数）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明，含 type（消息类型））。
    },
    ChunkAck {
        file_index: usize, // file_index（已接收文件在清单中的序号）。
        next_offset: u64,  // next_offset（该文件下一分块所需字节偏移）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明，含 type（消息类型））。
    },
    Complete {
        result: &'a Value, // result（服务端既有资源导入入口返回的原始结果）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明，含 type（消息类型））。
    },
    Cancelled {
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明，含 type（消息类型））。
    },
    Error {
        message: &'a str, // message（面向用户的受限错误说明，不包含服务端暂存路径）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明）。
    },
    DeviceFileReady {
        transfer_id: &'a str,    // transfer_id（原 DSH 文件票据编号）。
        chunk_bytes: usize,      // chunk_bytes（单块最大字节数）。
        total_bytes: u64,        // total_bytes（原票据锁定的完整字节数）。
        next_offset: u64,        // next_offset（DFT 已确认的上传续传偏移）。
        sha256: Option<&'a str>, // sha256（服务端原票据摘要；尚未完成的上传为空）。
        ready: bool,             // ready（原票据是否已经完整复核）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明）。
    },
    DeviceFileChunkAck {
        transfer_id: &'a str, // transfer_id（原 DSH 文件票据编号）。
        next_offset: u64,     // next_offset（原票据确认的下一上传偏移）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明）。
    },
    DeviceFileChunk {
        transfer_id: &'a str,  // transfer_id（原 DSH 文件票据编号）。
        offset: u64,           // offset（原始文件字节偏移）。
        byte_length: usize,    // byte_length（后续原始文件块长度）。
        total_bytes: u64,      // total_bytes（原票据锁定的完整长度）。
        sha256: &'a str,       // sha256（服务端锁定的完整摘要）。
        chunk_sha256: &'a str, // chunk_sha256（当前原始分块摘要）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明）。
    },
    DeviceFileComplete {
        transfer_id: &'a str, // transfer_id（原 DSH 文件票据编号）。
        result: &'a Value,    // result（原 DFT 核验完成回执）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明）。
    },
    DeviceFileCancelled {
        transfer_id: &'a str, // transfer_id（已取消的原 DSH 文件票据编号）。
        fields: BTreeMap<&'static str, &'static str>, // fields（字段中文说明）。
    },
}

/// serve_file_channel_connection（服务单条 FILE 连接）：先验桥接前导，再运行独立长度帧。
///
/// The local bridge（本机桥接器）writes `DeviceBridgePrelude`（桥接前导帧） followed by LF（换行符）
/// to the loopback socket. That prelude is consumed here and is never forwarded to the remote client.
pub fn serve_file_channel_connection<S>(
    mut stream: S,
    receiver: &dyn FileReceiver,
) -> Result<(), String>
where
    S: Read + Write,
{
    let peer = read_bridge_peer(&mut stream)?;
    let mut active_source: Option<String> = None;
    let mut active_device_transfer: Option<DeviceFileTransferSession> = None;
    loop {
        let frame = match read_client_frame(&mut stream) {
            Ok(frame) => frame,
            Err(error) => {
                if let Some(source_id) = active_source.take() {
                    let _ = receiver.abort(&peer, &source_id);
                }
                cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                return Err(error);
            }
        };
        match frame {
            ClientFrame::ApplicationUpdate { request } => {
                if active_source.is_some() || active_device_transfer.is_some() {
                    return Err("已有文件业务不能切换为本体版本同步。".into());
                }
                return crate::lan_update::serve(&mut stream, request, receiver.application_update(&peer)?);
            }
            ClientFrame::Begin {
                source_id,
                request_id,
                total_bytes,
                files,
                fields,
            } => {
                if active_source.is_some() || active_device_transfer.is_some() {
                    let _ = write_error(&mut stream, "文件上传连接已经绑定来源票据。", "source_id");
                    if let Some(source_id) = active_source.take() {
                        let _ = receiver.abort(&peer, &source_id);
                    }
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err("文件上传连接已经绑定来源票据。".into());
                }
                let request = UploadBegin {
                    source_id: source_id.clone(),
                    request_id,
                    total_bytes,
                    files,
                };
                let begin_result = match validate_client_fields(
                    &fields,
                    &["source_id", "request_id", "total_bytes", "files"],
                )
                .and_then(|_| request.validate())
                .and_then(|_| receiver.begin(&peer, request.clone()))
                {
                    Ok(result) => result,
                    Err(error) => {
                        let _ = write_error(&mut stream, &error, "source_id");
                        return Err(error);
                    }
                };
                if let Some(result) = begin_result {
                    let fields = BTreeMap::from([
                        ("type", "固定文件通道回执类型"),
                        ("result", "服务端既有资源导入入口返回的幂等结果"),
                    ]);
                    return write_server_frame(
                        &mut stream,
                        ServerFrame::Complete {
                            result: &result,
                            fields,
                        },
                    );
                }
                active_source = Some(source_id);
                let fields = BTreeMap::from([
                    ("type", "固定文件通道回执类型"),
                    ("chunk_bytes", "允许的最大原始文件分块字节数"),
                    ("file_count", "服务端接受的文件数"),
                    ("total_bytes", "服务端接受的原始总字节数"),
                ]);
                if let Err(error) = write_server_frame(
                    &mut stream,
                    ServerFrame::Ready {
                        chunk_bytes: MAX_FILE_CHUNK_BYTES,
                        file_count: request.files.len(),
                        total_bytes: request.total_bytes,
                        fields,
                    },
                ) {
                    if let Some(source_id) = active_source.take() {
                        let _ = receiver.abort(&peer, &source_id);
                    }
                    return Err(error);
                }
            }
            ClientFrame::Chunk {
                file_index,
                offset,
                byte_length,
                chunk_sha256,
                fields,
            } => {
                let Some(source_id) = active_source.as_deref() else {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let _ = write_error(&mut stream, "文件分块之前必须先发送来源清单。", "type");
                    return Err("文件分块之前必须先发送来源清单。".into());
                };
                if validate_client_fields(
                    &fields,
                    &["file_index", "offset", "byte_length", "chunk_sha256"],
                )
                .is_err()
                    || byte_length == 0
                    || byte_length > MAX_FILE_CHUNK_BYTES
                    || !valid_sha256(&chunk_sha256)
                {
                    let error = "文件分块长度或 SHA-256 摘要格式无效。";
                    let _ = receiver.abort(&peer, source_id);
                    active_source = None;
                    let _ = write_error(&mut stream, error, "byte_length");
                    return Err(error.into());
                }
                let mut bytes = vec![0_u8; byte_length];
                if stream.read_exact(&mut bytes).is_err() {
                    let error = "文件分块负载中断或长度不足。";
                    let _ = receiver.abort(&peer, source_id);
                    active_source = None;
                    let _ = write_error(&mut stream, error, "byte_length");
                    return Err(error.into());
                }
                if sha256_hex(&bytes) != chunk_sha256 {
                    let error = "文件分块 SHA-256 摘要不匹配。";
                    let _ = receiver.abort(&peer, source_id);
                    active_source = None;
                    let _ = write_error(&mut stream, error, "chunk_sha256");
                    return Err(error.into());
                }
                let next_offset = match receiver.append_chunk(
                    &peer,
                    source_id,
                    file_index,
                    offset,
                    &chunk_sha256,
                    &bytes,
                ) {
                    Ok(next_offset) => next_offset,
                    Err(error) => {
                        let _ = receiver.abort(&peer, source_id);
                        active_source = None;
                        let _ = write_error(&mut stream, &error, "offset");
                        return Err(error);
                    }
                };
                let fields = BTreeMap::from([
                    ("type", "固定文件通道回执类型"),
                    ("file_index", "已接收文件在清单中的序号"),
                    ("next_offset", "该文件下一分块所需字节偏移"),
                ]);
                if let Err(error) = write_server_frame(
                    &mut stream,
                    ServerFrame::ChunkAck {
                        file_index,
                        next_offset,
                        fields,
                    },
                ) {
                    if let Some(source_id) = active_source.take() {
                        let _ = receiver.abort(&peer, &source_id);
                    }
                    return Err(error);
                }
            }
            ClientFrame::Finish { fields } => {
                if validate_client_fields(&fields, &[]).is_err() {
                    if let Some(source_id) = active_source.take() {
                        let _ = receiver.abort(&peer, &source_id);
                    }
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err("文件上传结束帧缺少字段中文说明。".into());
                }
                let Some(source_id) = active_source.take() else {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let error = "没有可完成的远程导入来源。";
                    let _ = write_error(&mut stream, error, "type");
                    return Err(error.into());
                };
                match receiver.finish(&peer, &source_id) {
                    Ok(result) => {
                        let fields = BTreeMap::from([
                            ("type", "固定文件通道回执类型"),
                            ("result", "服务端既有资源导入入口返回的结果"),
                        ]);
                        return write_server_frame(
                            &mut stream,
                            ServerFrame::Complete {
                                result: &result,
                                fields,
                            },
                        );
                    }
                    Err(error) => {
                        let _ = receiver.abort(&peer, &source_id);
                        let _ = write_error(&mut stream, &error, "result");
                        return Err(error);
                    }
                }
            }
            ClientFrame::Cancel { source_id, fields } => {
                if validate_client_fields(&fields, &["source_id"]).is_err() {
                    if let Some(active_id) = active_source.take() {
                        let _ = receiver.abort(&peer, &active_id);
                    }
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err("文件上传取消帧缺少字段中文说明。".into());
                }
                if active_device_transfer.is_some() {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err("资源来源取消帧不能取消 DSH 文件票据。".into());
                }
                if let Some(active_id) = active_source.take() {
                    if active_id != source_id {
                        let _ = receiver.abort(&peer, &active_id);
                        return Err("取消请求的来源编号与当前 FILE 连接不匹配。".into());
                    }
                    receiver.abort(&peer, &active_id)?;
                } else {
                    receiver.abort(&peer, &source_id)?;
                }
                let fields = BTreeMap::from([("type", "远程客户端取消上传后的服务端状态")]);
                write_server_frame(&mut stream, ServerFrame::Cancelled { fields })?;
                return Ok(());
            }
            ClientFrame::DeviceFileBegin {
                begin,
                authorization,
                fields,
            } => {
                if active_source.is_some() || active_device_transfer.is_some() {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let error = "FILE 连接已经绑定其他资源或 DSH 票据。";
                    let _ = write_error(&mut stream, error, "type");
                    return Err(error.into());
                }
                if validate_client_fields(&fields, &["begin", "authorization"]).is_err()
                    || begin.validate().is_err()
                    || authorization.validate().is_err()
                    || authorization.device_id != peer.device_id
                {
                    let error = "DSH FILE 开始帧的字段、签名证明或桥接设备身份无效。";
                    let _ = write_error(&mut stream, error, "begin");
                    return Err(error.into());
                }
                let session = match receiver.begin_device_file_transfer(
                    &peer,
                    begin.clone(),
                    authorization,
                ) {
                    Ok(session) => session,
                    Err(error) => {
                        let _ = write_error(&mut stream, &error, "begin");
                        return Err(error);
                    }
                };
                if !valid_device_file_session(&session, &begin) {
                    let _ = receiver.cancel_device_file_transfer(&peer, &session);
                    let error = "服务端原文件票据回执与请求设备、代次或摘要不匹配。";
                    let _ = write_error(&mut stream, error, "transfer_id");
                    return Err(error.into());
                }
                active_device_transfer = Some(session.clone());
                let fields = BTreeMap::from([
                    ("type", "固定 FILE 回执类型"),
                    ("transfer_id", "原 DSH 文件票据编号"),
                    ("chunk_bytes", "允许的最大文件分块字节数"),
                    ("total_bytes", "原票据锁定的文件字节数"),
                    ("next_offset", "原票据确认的下一上传字节偏移"),
                    ("sha256", "服务端锁定或复核的完整文件摘要"),
                    ("ready", "原票据内容是否已完整复核"),
                ]);
                if let Err(error) = write_server_frame(
                    &mut stream,
                    ServerFrame::DeviceFileReady {
                        transfer_id: &session.transfer_id,
                        chunk_bytes: MAX_FILE_CHUNK_BYTES,
                        total_bytes: session.total_bytes,
                        next_offset: session.next_offset,
                        sha256: session.sha256.as_deref(),
                        ready: session.ready,
                        fields,
                    },
                ) {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err(error);
                }
            }
            ClientFrame::DeviceFileUploadChunk {
                transfer_id,
                offset,
                byte_length,
                chunk_sha256,
                fields,
            } => {
                let Some(session) = active_device_transfer.as_ref().cloned() else {
                    let error = "DSH 文件分块之前必须先通过签名 begin 绑定原票据。";
                    let _ = write_error(&mut stream, error, "type");
                    return Err(error.into());
                };
                if session.transfer_id != transfer_id
                    || session.operation != "file.sftp.upload"
                    || validate_client_fields(
                        &fields,
                        &["transfer_id", "offset", "byte_length", "chunk_sha256"],
                    )
                    .is_err()
                    || byte_length == 0
                    || byte_length > MAX_FILE_CHUNK_BYTES
                    || !valid_sha256(&chunk_sha256)
                    || offset
                        .checked_add(byte_length as u64)
                        .map_or(true, |end| end > session.total_bytes)
                {
                    let error = "DSH 文件分块编号、偏移、长度或摘要无效。";
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let _ = write_error(&mut stream, error, "offset");
                    return Err(error.into());
                }
                let mut bytes = vec![0_u8; byte_length];
                if stream.read_exact(&mut bytes).is_err() || sha256_hex(&bytes) != chunk_sha256 {
                    let error = "DSH 文件分块负载中断或 SHA-256 摘要不匹配。";
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let _ = write_error(&mut stream, error, "chunk_sha256");
                    return Err(error.into());
                }
                let next_offset = match receiver.append_device_file_chunk(
                    &peer,
                    &session,
                    offset,
                    &chunk_sha256,
                    &bytes,
                ) {
                    Ok(next_offset) => next_offset,
                    Err(error) => {
                        cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                        let _ = write_error(&mut stream, &error, "offset");
                        return Err(error);
                    }
                };
                if next_offset != offset + byte_length as u64 {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let error = "原 DFT 文件偏移回执与已发送分块不一致。";
                    let _ = write_error(&mut stream, error, "next_offset");
                    return Err(error.into());
                }
                let fields = BTreeMap::from([
                    ("type", "固定 FILE 回执类型"),
                    ("transfer_id", "原 DSH 文件票据编号"),
                    ("next_offset", "原票据确认的下一上传字节偏移"),
                ]);
                if let Err(error) = write_server_frame(
                    &mut stream,
                    ServerFrame::DeviceFileChunkAck {
                        transfer_id: &session.transfer_id,
                        next_offset,
                        fields,
                    },
                ) {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err(error);
                }
            }
            ClientFrame::DeviceFileDownloadChunk {
                transfer_id,
                offset,
                limit,
                fields,
            } => {
                let Some(session) = active_device_transfer.as_ref().cloned() else {
                    let error = "DSH 下载分块之前必须先通过签名 begin 绑定原票据。";
                    let _ = write_error(&mut stream, error, "type");
                    return Err(error.into());
                };
                if session.transfer_id != transfer_id
                    || session.operation != "file.sftp.download"
                    || validate_client_fields(&fields, &["transfer_id", "offset", "limit"]).is_err()
                    || limit == 0
                    || limit > MAX_FILE_CHUNK_BYTES
                    || offset >= session.total_bytes
                {
                    let error = "DSH 下载分块编号、偏移或长度上限无效。";
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let _ = write_error(&mut stream, error, "offset");
                    return Err(error.into());
                }
                let chunk = match receiver.read_device_file_chunk(&peer, &session, offset, limit) {
                    Ok(chunk) => chunk,
                    Err(error) => {
                        cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                        let _ = write_error(&mut stream, &error, "offset");
                        return Err(error);
                    }
                };
                if chunk.transfer_id != session.transfer_id
                    || chunk.offset != offset
                    || chunk.total_bytes != session.total_bytes
                    || Some(chunk.sha256.as_str()) != session.sha256.as_deref()
                    || chunk.bytes.is_empty()
                    || chunk.bytes.len() > limit
                    || sha256_hex(&chunk.bytes) != chunk.chunk_sha256
                {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    let error = "原 DFT 下载分块回执长度、偏移或摘要不匹配。";
                    let _ = write_error(&mut stream, error, "chunk_sha256");
                    return Err(error.into());
                }
                let fields = BTreeMap::from([
                    ("type", "固定 FILE 回执类型"),
                    ("transfer_id", "原 DSH 文件票据编号"),
                    ("offset", "当前文件字节偏移"),
                    ("byte_length", "后续原始文件分块长度"),
                    ("total_bytes", "原票据锁定的文件字节数"),
                    ("sha256", "原票据锁定的完整摘要"),
                    ("chunk_sha256", "当前原始文件分块摘要"),
                ]);
                if let Err(error) = write_server_frame(
                    &mut stream,
                    ServerFrame::DeviceFileChunk {
                        transfer_id: &chunk.transfer_id,
                        offset: chunk.offset,
                        byte_length: chunk.bytes.len(),
                        total_bytes: chunk.total_bytes,
                        sha256: &chunk.sha256,
                        chunk_sha256: &chunk.chunk_sha256,
                        fields,
                    },
                ) {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err(error);
                }
                if stream.write_all(&chunk.bytes).is_err() || stream.flush().is_err() {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err("DSH 文件原始分块回写失败。".into());
                }
            }
            ClientFrame::DeviceFileFinish {
                transfer_id,
                expected_bytes,
                expected_sha256,
                fields,
            } => {
                let Some(session) = active_device_transfer.take() else {
                    let error = "没有已绑定的 DSH 文件传输票据可完成。";
                    let _ = write_error(&mut stream, error, "type");
                    return Err(error.into());
                };
                if session.transfer_id != transfer_id
                    || !valid_sha256(&expected_sha256)
                    || expected_bytes != session.total_bytes
                    || (session.operation == "file.sftp.download"
                        && session.sha256.as_deref() != Some(expected_sha256.as_str()))
                    || validate_client_fields(
                        &fields,
                        &["transfer_id", "expected_bytes", "expected_sha256"],
                    )
                    .is_err()
                {
                    let _ = receiver.cancel_device_file_transfer(&peer, &session);
                    let error = "DSH 文件完成编号、长度或摘要与原票据不匹配。";
                    let _ = write_error(&mut stream, error, "expected_sha256");
                    return Err(error.into());
                }
                match receiver.finish_device_file_transfer(
                    &peer,
                    &session,
                    expected_bytes,
                    &expected_sha256,
                ) {
                    Ok(result) => {
                        let fields = BTreeMap::from([
                            ("type", "固定 FILE 回执类型"),
                            ("transfer_id", "原 DSH 文件票据编号"),
                            ("result", "原 DFT 完整性复核回执"),
                        ]);
                        write_server_frame(
                            &mut stream,
                            ServerFrame::DeviceFileComplete {
                                transfer_id: &session.transfer_id,
                                result: &result,
                                fields,
                            },
                        )?;
                    }
                    Err(error) => {
                        let _ = receiver.cancel_device_file_transfer(&peer, &session);
                        let _ = write_error(&mut stream, &error, "result");
                        return Err(error);
                    }
                }
                return Ok(());
            }
            ClientFrame::DeviceFileCancel {
                transfer_id,
                fields,
            } => {
                if validate_client_fields(&fields, &["transfer_id"]).is_err() {
                    cancel_active_device_transfer(receiver, &peer, &mut active_device_transfer);
                    return Err("DSH 文件取消帧缺少字段中文说明。".into());
                }
                let Some(session) = active_device_transfer.take() else {
                    let error = "没有可取消的 DSH 文件会话。";
                    let _ = write_error(&mut stream, error, "transfer_id");
                    return Err(error.into());
                };
                if session.transfer_id != transfer_id {
                    let _ = receiver.cancel_device_file_transfer(&peer, &session);
                    let error = "DSH 文件取消编号与当前 FILE 会话不匹配。";
                    let _ = write_error(&mut stream, error, "transfer_id");
                    return Err(error.into());
                }
                receiver.cancel_device_file_transfer(&peer, &session)?;
                let fields = BTreeMap::from([
                    ("type", "固定 FILE 回执类型"),
                    ("transfer_id", "已取消的原 DSH 文件票据编号"),
                ]);
                write_server_frame(
                    &mut stream,
                    ServerFrame::DeviceFileCancelled {
                        transfer_id: &session.transfer_id,
                        fields,
                    },
                )?;
                return Ok(());
            }
        }
    }
}

fn read_bridge_peer<S: Read>(stream: &mut S) -> Result<FileChannelPeer, String> {
    let mut line = Vec::with_capacity(1024);
    loop {
        let mut byte = [0_u8; 1];
        stream
            .read_exact(&mut byte)
            .map_err(|_| "文件桥接前导帧缺失。".to_string())?;
        if byte[0] == b'\n' {
            break;
        }
        if line.len() >= MAX_PRELUDE_BYTES || byte[0] == b'\r' {
            return Err("文件桥接前导帧超限或换行格式无效。".into());
        }
        line.push(byte[0]);
    }
    let prelude: DeviceBridgePrelude =
        serde_json::from_slice(&line).map_err(|_| "文件桥接前导帧格式无效。".to_string())?;
    FileChannelPeer::try_from(prelude)
}

fn read_client_frame<S: Read>(stream: &mut S) -> Result<ClientFrame, String> {
    let mut length = [0_u8; 4];
    stream
        .read_exact(&mut length)
        .map_err(|_| "文件通道控制帧已断开。".to_string())?;
    let length = u32::from_be_bytes(length) as usize;
    if length == 0 || length > MAX_FILE_CONTROL_BYTES {
        return Err("文件通道控制帧长度超过限制。".into());
    }
    let mut bytes = vec![0_u8; length];
    stream
        .read_exact(&mut bytes)
        .map_err(|_| "文件通道控制帧内容不完整。".to_string())?;
    serde_json::from_slice(&bytes).map_err(|_| "文件通道控制帧不是受限 JSON 结构。".to_string())
}

fn write_server_frame<S: Write>(stream: &mut S, frame: ServerFrame<'_>) -> Result<(), String> {
    let bytes = serde_json::to_vec(&frame).map_err(|_| "文件通道回执无法编码。".to_string())?;
    if bytes.is_empty() || bytes.len() > MAX_FILE_CONTROL_BYTES || bytes.len() > u32::MAX as usize {
        return Err("文件通道回执超过控制帧上限。".into());
    }
    stream
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .map_err(|_| "文件通道回执写入失败。".to_string())?;
    stream
        .write_all(&bytes)
        .map_err(|_| "文件通道回执写入失败。".to_string())?;
    stream
        .flush()
        .map_err(|_| "文件通道回执刷新失败。".to_string())
}

fn write_error<S: Write>(stream: &mut S, message: &str, field: &'static str) -> Result<(), String> {
    let fields = BTreeMap::from([
        ("type", "固定文件通道回执类型"),
        (field, "对应请求字段的中文说明"),
    ]);
    write_server_frame(stream, ServerFrame::Error { message, fields })
}

fn validate_client_fields(
    fields: &BTreeMap<String, String>,
    expected: &[&str],
) -> Result<(), String> {
    if fields.len() != expected.len() + 1
        || !fields.contains_key("type")
        || expected.iter().any(|key| !fields.contains_key(*key))
        || fields
            .values()
            .any(|value| value.trim().is_empty() || value.len() > 120)
    {
        return Err("文件通道请求缺少逐字段中文说明。".into());
    }
    Ok(())
}

fn validate_named_fields(
    fields: &BTreeMap<String, String>,
    expected: &[&str],
) -> Result<(), String> {
    if fields.len() != expected.len()
        || expected.iter().any(|key| !fields.contains_key(*key))
        || fields
            .values()
            .any(|value| value.trim().is_empty() || value.len() > 120)
    {
        return Err("固定文件网关 JSON 缺少逐字段中文说明。".into());
    }
    Ok(())
}

fn valid_device_file_session(
    session: &DeviceFileTransferSession,
    begin: &DeviceFileTransferBegin,
) -> bool {
    let token_ok = (32..=128).contains(&session.channel_token.len())
        && session
            .channel_token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'));
    let sha_ok = session.sha256.as_deref().map_or(true, valid_sha256);
    let ready_ok = !session.ready || session.sha256.as_deref().is_some_and(valid_sha256);
    session.transfer_id == begin.transfer_id
        && session.operation == begin.operation
        && session.connection_generation == begin.connection_generation
        && session.executor_generation == begin.executor_generation
        && session.total_bytes <= begin.maximum_bytes
        && session.total_bytes <= 4 * 1024 * 1024 * 1024
        && session.next_offset <= session.total_bytes
        && token_ok
        && sha_ok
        && ready_ok
        && (begin.operation != "file.sftp.download"
            || (session.ready && session.sha256 == begin.expected_sha256))
}

fn cancel_active_device_transfer(
    receiver: &dyn FileReceiver,
    peer: &FileChannelPeer,
    active: &mut Option<DeviceFileTransferSession>,
) {
    if let Some(session) = active.take() {
        let _ = receiver.cancel_device_file_transfer(peer, &session);
    }
}

#[cfg(feature = "ssh-device")]
fn cancel_fields() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("type".into(), "固定文件通道消息类型".into()),
        ("source_id".into(), "服务端预留的导入来源编号".into()),
    ])
}

#[cfg(feature = "ssh-device")]
fn cancel_frame(source_id: &str) -> ClientFrameOut {
    ClientFrameOut::Cancel {
        source_id: source_id.to_owned(),
        fields: cancel_fields(),
    }
}

#[cfg(feature = "ssh-device")]
fn progress_fields() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("file_index".into(), "当前上传文件在清单中的序号".into()),
        ("files_completed".into(), "已经完整发送的文件数".into()),
        ("total_files".into(), "本次上传文件总数".into()),
        ("transferred_bytes".into(), "已确认的原始总字节数".into()),
        ("total_bytes".into(), "本次上传原始总字节数".into()),
    ])
}

#[cfg(feature = "ssh-device")]
fn receipt_fields() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("source_id".into(), "服务端预留并校验的来源编号".into()),
        ("request_id".into(), "稳定业务请求编号".into()),
        ("total_bytes".into(), "服务端确认完成的原始总字节数".into()),
        ("files".into(), "已上传的相对路径、长度和摘要清单".into()),
        ("result".into(), "服务端既有资源导入任务结果".into()),
    ])
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// encode_file_channel_base64（编码单块）：服务端 loopback JSON 只封装一个 64 KiB 原始块。
pub fn encode_file_channel_base64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

/// decode_file_channel_base64（解码单块）：仅接受规范 Base64 与有界原始块。
pub fn decode_file_channel_base64(value: &str) -> Result<Vec<u8>, String> {
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(value.as_bytes())
        .map_err(|_| "固定文件网关的 Base64 分块格式无效。".to_string())?;
    if bytes.is_empty()
        || bytes.len() > MAX_FILE_CHUNK_BYTES
        || base64::engine::general_purpose::STANDARD.encode(&bytes) != value
    {
        return Err("固定文件网关分块为空、超过 64 KiB 或不是规范 Base64。".into());
    }
    Ok(bytes)
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// UploadProgress（上传进度）：不含本机路径或服务端暂存路径。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UploadProgress {
    pub file_index: usize,      // file_index（当前上传文件在清单中的序号）。
    pub files_completed: usize, // files_completed（已经完整发送的文件数）。
    pub total_files: usize,     // total_files（本次上传文件总数）。
    pub transferred_bytes: u64, // transferred_bytes（已确认的原始总字节数）。
    pub total_bytes: u64,       // total_bytes（本次上传原始总字节数）。
    pub fields: BTreeMap<String, String>, // fields（逐字段中文说明）。
}

/// UploadReceipt（远程导入上传回执）：不包含远程客户端本机的任何路径。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct UploadReceipt {
    pub source_id: String,  // source_id（管理通道预留并由服务端校验的来源编号）。
    pub request_id: String, // request_id（与来源预留相同的稳定业务请求编号）。
    pub total_bytes: u64,   // total_bytes（服务端确认完成的原始总字节数）。
    pub files: Vec<RemoteImportFileEntry>, // files（已上传的相对路径、长度和摘要清单）。
    pub result: Value,      // result（服务端既有 ResourceManager 的导入任务结果）。
    pub fields: BTreeMap<String, String>, // fields（逐字段中文说明）。
}

/// upload_manifested_files（受限清单上传客户端）：本机路径只在当前原生客户端中枚举和读取。
#[cfg(feature = "ssh-device")]
pub async fn upload_manifested_files<S, F>(
    stream: &mut S,
    source_id: &str,
    request_id: &str,
    local_sources: &[PathBuf],
    cancel: &AtomicBool,
    on_progress: F,
) -> Result<UploadReceipt, String>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    F: FnMut(UploadProgress),
{
    let result = upload_manifested_files_inner(
        stream,
        source_id,
        request_id,
        local_sources,
        cancel,
        on_progress,
    )
    .await;
    if result.is_err() {
        let not_cancelled = AtomicBool::new(false);
        let _ = write_client_frame_async(stream, &cancel_frame(source_id), &not_cancelled).await;
    }
    result
}

#[cfg(feature = "ssh-device")]
async fn upload_manifested_files_inner<S, F>(
    stream: &mut S,
    source_id: &str,
    request_id: &str,
    local_sources: &[PathBuf],
    cancel: &AtomicBool,
    mut on_progress: F,
) -> Result<UploadReceipt, String>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    F: FnMut(UploadProgress),
{
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    if source_id.len() != 32
        || !source_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || request_id.len() != 32
        || !request_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("服务端文件来源票据或请求编号格式无效。".into());
    }
    if local_sources.is_empty()
        || local_sources.len() > MAX_SELECTED_SOURCE_ROOTS
        || cancel.load(Ordering::Acquire)
    {
        return Err(if local_sources.is_empty() {
            "本机未选择任何文件或文件夹。"
        } else if local_sources.len() > MAX_SELECTED_SOURCE_ROOTS {
            "一次最多上传 64 个本机文件或文件夹来源。"
        } else {
            "远程文件上传已取消。"
        }
        .into());
    }
    let source_paths = local_sources.to_vec();
    let plan = tokio::task::spawn_blocking(move || build_upload_plan(&source_paths))
        .await
        .map_err(|_| "本机文件清单无法建立。".to_string())??;
    let entries = plan
        .iter()
        .map(|file| file.entry.clone())
        .collect::<Vec<_>>();
    let total_bytes = entries.iter().map(|entry| entry.byte_length).sum::<u64>();
    let manifest = RemoteImportManifest {
        source_id: source_id.to_owned(),
        total_bytes,
        files: entries.clone(),
    };
    UploadBegin {
        source_id: source_id.to_owned(),
        request_id: request_id.to_owned(),
        total_bytes,
        files: entries.clone(),
    }
    .validate()?;
    manifest.validate()?;
    let begin = ClientFrameOut::Begin {
        source_id: source_id.to_owned(),
        request_id: request_id.to_owned(),
        total_bytes,
        files: entries.clone(),
        fields: BTreeMap::from([
            ("type".into(), "固定文件通道消息类型".into()),
            ("source_id".into(), "管理通道签发的服务端来源编号".into()),
            ("request_id".into(), "稳定业务请求编号".into()),
            ("total_bytes".into(), "所有文件的原始字节数总和".into()),
            ("files".into(), "规范相对路径文件清单".into()),
        ]),
    };
    write_client_frame_async(stream, &begin, cancel).await?;
    let ready = read_server_frame_async(stream, cancel).await?;
    let chunk_limit = match ready {
        ServerFrameIn::Ready {
            chunk_bytes,
            file_count,
            total_bytes: accepted_total,
        } => {
            if chunk_bytes == 0
                || chunk_bytes > MAX_FILE_CHUNK_BYTES
                || file_count != plan.len()
                || accepted_total != total_bytes
            {
                return Err("服务端文件通道返回了与来源清单不匹配的上传上限。".into());
            }
            chunk_bytes
        }
        ServerFrameIn::Complete { result } => {
            return Ok(UploadReceipt {
                source_id: source_id.to_owned(),
                request_id: request_id.to_owned(),
                total_bytes,
                files: entries,
                result,
                fields: receipt_fields(),
            });
        }
        ServerFrameIn::Error { message } => return Err(message),
        _ => return Err("服务端文件通道没有接受来源清单。".into()),
    };
    let mut transferred_bytes = 0_u64;
    for (file_index, file) in plan.iter().enumerate() {
        if cancel.load(Ordering::Acquire) {
            let _ = write_client_frame_async(stream, &cancel_frame(source_id), cancel).await;
            return Err("远程文件上传已取消。".into());
        }
        let before = safe_file_metadata(&file.source_path)?;
        if before.len() != file.entry.byte_length || modified_time(&before) != file.modified {
            let _ = write_client_frame_async(stream, &cancel_frame(source_id), cancel).await;
            return Err("本机文件在建立上传清单后发生变化。".into());
        }
        let mut input = tokio::fs::File::open(&file.source_path)
            .await
            .map_err(|_| "本机获准文件读取失败。".to_string())?;
        let mut file_offset = 0_u64;
        let mut file_digest = Sha256::new();
        let mut chunk = vec![0_u8; chunk_limit];
        loop {
            if cancel.load(Ordering::Acquire) {
                let _ = write_client_frame_async(stream, &cancel_frame(source_id), cancel).await;
                return Err("远程文件上传已取消。".into());
            }
            let read = input
                .read(&mut chunk)
                .await
                .map_err(|_| "本机文件读取中断。".to_string())?;
            if read == 0 {
                break;
            }
            let bytes = &chunk[..read];
            let chunk_sha256 = sha256_hex(bytes);
            file_digest.update(bytes);
            let control = ClientFrameOut::Chunk {
                file_index,
                offset: file_offset,
                byte_length: read,
                chunk_sha256,
                fields: BTreeMap::from([
                    ("type".into(), "固定文件通道消息类型".into()),
                    ("file_index".into(), "当前文件在清单中的序号".into()),
                    ("offset".into(), "当前文件原始字节偏移".into()),
                    ("byte_length".into(), "后续原始文件块长度".into()),
                    ("chunk_sha256".into(), "当前原始文件块摘要".into()),
                ]),
            };
            write_client_frame_async(stream, &control, cancel).await?;
            if tokio::select! {
                _ = wait_for_cancel(cancel) => {
                    let _ = write_client_frame_async(stream, &cancel_frame(source_id), cancel).await;
                    return Err("远程文件上传已取消。".into());
                }
                result = stream.write_all(bytes) => result.is_err(),
            } {
                return Err("远程文件分块写入中断。".into());
            }
            stream
                .flush()
                .await
                .map_err(|_| "远程文件分块刷新失败。".to_string())?;
            match read_server_frame_async(stream, cancel).await? {
                ServerFrameIn::ChunkAck {
                    file_index: acknowledged,
                    next_offset,
                } => {
                    let expected = file_offset + read as u64;
                    if acknowledged != file_index || next_offset != expected {
                        return Err("服务端确认的文件偏移与已发送分块不一致。".into());
                    }
                }
                ServerFrameIn::Error { message } => return Err(message),
                _ => return Err("服务端没有确认当前文件分块。".into()),
            }
            file_offset += read as u64;
            transferred_bytes += read as u64;
            on_progress(UploadProgress {
                file_index,
                files_completed: file_index,
                total_files: plan.len(),
                transferred_bytes,
                total_bytes,
                fields: progress_fields(),
            });
        }
        let after = safe_file_metadata(&file.source_path)?;
        let actual_digest = format_sha256(file_digest.finalize().as_slice());
        if file_offset != file.entry.byte_length
            || actual_digest != file.entry.sha256
            || after.len() != before.len()
            || modified_time(&after) != file.modified
        {
            let _ = write_client_frame_async(stream, &cancel_frame(source_id), cancel).await;
            return Err("本机文件上传期间发生变化或摘要不匹配。".into());
        }
        on_progress(UploadProgress {
            file_index,
            files_completed: file_index + 1,
            total_files: plan.len(),
            transferred_bytes,
            total_bytes,
            fields: progress_fields(),
        });
    }
    write_client_frame_async(
        stream,
        &ClientFrameOut::Finish {
            fields: BTreeMap::from([("type".into(), "固定文件通道消息类型".into())]),
        },
        cancel,
    )
    .await?;
    let result = match read_server_frame_async(stream, cancel).await? {
        ServerFrameIn::Complete { result } => result,
        ServerFrameIn::Error { message } => return Err(message),
        _ => return Err("服务端没有完成远程资源导入。".into()),
    };
    Ok(UploadReceipt {
        source_id: source_id.to_owned(),
        request_id: request_id.to_owned(),
        total_bytes,
        files: entries,
        result,
        fields: receipt_fields(),
    })
}

#[cfg(feature = "ssh-device")]
#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
// ClientFrameOut（客户端请求帧）：type（固定消息类型）仅允许 begin、chunk、finish 与 cancel。
enum ClientFrameOut {
    Begin {
        source_id: String,                 // source_id（管理通道签发的服务端来源编号）。
        request_id: String,                // request_id（稳定业务请求编号）。
        total_bytes: u64,                  // total_bytes（所有文件的原始字节数总和）。
        files: Vec<RemoteImportFileEntry>, // files（规范相对路径文件清单）。
        fields: BTreeMap<String, String>,  // fields（逐字段中文说明）。
    },
    Chunk {
        file_index: usize,                // file_index（当前文件在清单中的序号）。
        offset: u64,                      // offset（当前文件原始字节偏移）。
        byte_length: usize,               // byte_length（后续原始文件块长度）。
        chunk_sha256: String,             // chunk_sha256（当前原始文件块摘要）。
        fields: BTreeMap<String, String>, // fields（逐字段中文说明）。
    },
    Finish {
        fields: BTreeMap<String, String>,
    }, // fields（结束请求的字段中文说明）。
    Cancel {
        source_id: String,                // source_id（服务端预留的导入来源编号）。
        fields: BTreeMap<String, String>, // fields（逐字段中文说明）。
    },
    DeviceFileBegin {
        begin: DeviceFileTransferBegin, // begin（原 DSH 文件票据签名申请）。
        authorization: FileTransferAuthorization, // authorization（已批准设备身份签名证明）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
    DeviceFileUploadChunk {
        transfer_id: String,              // transfer_id（原 DSH 文件传输编号）。
        offset: u64,                      // offset（当前原始文件字节偏移）。
        byte_length: usize,               // byte_length（后续原始文件块长度）。
        chunk_sha256: String,             // chunk_sha256（当前分块摘要）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
    DeviceFileDownloadChunk {
        transfer_id: String,              // transfer_id（原 DSH 文件传输编号）。
        offset: u64,                      // offset（当前原始文件字节偏移）。
        limit: usize,                     // limit（请求的最大分块字节数）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
    DeviceFileFinish {
        transfer_id: String,              // transfer_id（原 DSH 文件传输编号）。
        expected_bytes: u64,              // expected_bytes（完整原始文件长度）。
        expected_sha256: String,          // expected_sha256（完整原始文件 SHA-256 摘要）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
    DeviceFileCancel {
        transfer_id: String,              // transfer_id（原 DSH 文件传输编号）。
        fields: BTreeMap<String, String>, // fields（文件帧字段中文说明）。
    },
}

#[cfg(feature = "ssh-device")]
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
// ServerFrameIn（服务端文件通道回执）：客户端只读取固定回执类型及其有界数据。
enum ServerFrameIn {
    Ready {
        chunk_bytes: usize, // chunk_bytes（服务端允许的单块字节上限）。
        file_count: usize,  // file_count（服务端接受的文件数）。
        total_bytes: u64,   // total_bytes（服务端接受的原始总字节数）。
    },
    ChunkAck {
        file_index: usize, // file_index（已确认文件在清单中的序号）。
        next_offset: u64,  // next_offset（该文件下一分块所需字节偏移）。
    },
    Complete {
        result: Value,
    }, // result（服务端现有导入任务结果）。
    Cancelled, // Cancelled（服务端已确认取消来源票据）。
    Error {
        message: String,
    }, // message（服务端返回的受限错误说明）。
    DeviceFileReady {
        transfer_id: String,              // transfer_id（原 DSH 文件票据编号）。
        chunk_bytes: usize,               // chunk_bytes（服务端允许的单块字节上限）。
        total_bytes: u64,                 // total_bytes（原票据锁定的完整字节数）。
        next_offset: u64,                 // next_offset（原票据确认的下一上传偏移）。
        sha256: Option<String>,           // sha256（服务端已锁定或复核的完整摘要）。
        ready: bool,                      // ready（原票据内容是否已完整复核）。
        fields: BTreeMap<String, String>, // fields（字段中文说明）。
    },
    DeviceFileChunkAck {
        transfer_id: String,              // transfer_id（原 DSH 文件票据编号）。
        next_offset: u64,                 // next_offset（原票据确认的下一上传偏移）。
        fields: BTreeMap<String, String>, // fields（字段中文说明）。
    },
    DeviceFileChunk {
        transfer_id: String,              // transfer_id（原 DSH 文件票据编号）。
        offset: u64,                      // offset（原始文件字节偏移）。
        byte_length: usize,               // byte_length（后续原始文件块长度）。
        total_bytes: u64,                 // total_bytes（原票据锁定的完整长度）。
        sha256: String,                   // sha256（原票据锁定的完整摘要）。
        chunk_sha256: String,             // chunk_sha256（当前原始分块摘要）。
        fields: BTreeMap<String, String>, // fields（字段中文说明）。
    },
    DeviceFileComplete {
        transfer_id: String,              // transfer_id（原 DSH 文件票据编号）。
        result: Value,                    // result（原 DFT 完整性复核回执）。
        fields: BTreeMap<String, String>, // fields（字段中文说明）。
    },
    DeviceFileCancelled {
        transfer_id: String,              // transfer_id（已取消的原 DSH 文件票据编号）。
        fields: BTreeMap<String, String>, // fields（字段中文说明）。
    },
}

#[cfg(feature = "ssh-device")]
async fn write_client_frame_async<S: tokio::io::AsyncWrite + Unpin>(
    stream: &mut S,
    frame: &ClientFrameOut,
    cancel: &AtomicBool,
) -> Result<(), String> {
    use tokio::io::AsyncWriteExt;
    let bytes = serde_json::to_vec(frame).map_err(|_| "文件上传控制帧无法编码。".to_string())?;
    if bytes.is_empty() || bytes.len() > MAX_FILE_CONTROL_BYTES || bytes.len() > u32::MAX as usize {
        return Err("文件上传控制帧超过大小上限。".into());
    }
    let prefix = (bytes.len() as u32).to_be_bytes();
    cancellable_io(
        stream.write_all(&prefix),
        cancel,
        "文件上传控制帧写入失败。",
    )
    .await?;
    cancellable_io(stream.write_all(&bytes), cancel, "文件上传控制帧写入失败。").await?;
    cancellable_io(stream.flush(), cancel, "文件上传控制帧刷新失败。").await
}

#[cfg(feature = "ssh-device")]
async fn read_server_frame_async<S: tokio::io::AsyncRead + Unpin>(
    stream: &mut S,
    cancel: &AtomicBool,
) -> Result<ServerFrameIn, String> {
    use tokio::io::AsyncReadExt;
    let mut prefix = [0_u8; 4];
    cancellable_io(
        stream.read_exact(&mut prefix),
        cancel,
        "服务端文件通道回执读取失败。",
    )
    .await?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_FILE_CONTROL_BYTES {
        return Err("服务端文件通道回执长度超过上限。".into());
    }
    let mut bytes = vec![0_u8; length];
    cancellable_io(
        stream.read_exact(&mut bytes),
        cancel,
        "服务端文件通道回执内容不完整。",
    )
    .await?;
    serde_json::from_slice(&bytes).map_err(|_| "服务端文件通道回执格式无效。".into())
}

/// upload_device_file（设备到服务端大文件）：复用原 DSH DFT 票据并以 64 KiB 有界块传输。
#[cfg(feature = "ssh-device")]
pub async fn upload_device_file<S>(
    stream: &mut S,
    begin: DeviceFileTransferBegin,
    authorization: FileTransferAuthorization,
    local_path: &Path,
    cancel: &AtomicBool,
) -> Result<DeviceFileTransferReceipt, String>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let transfer_id = begin.transfer_id.clone();
    let result = upload_device_file_inner(stream, begin, authorization, local_path, cancel).await;
    if result.is_err() {
        let emergency = AtomicBool::new(false);
        let _ =
            write_client_frame_async(stream, &device_file_cancel_frame(&transfer_id), &emergency)
                .await;
    }
    result
}

#[cfg(feature = "ssh-device")]
async fn upload_device_file_inner<S>(
    stream: &mut S,
    begin: DeviceFileTransferBegin,
    authorization: FileTransferAuthorization,
    local_path: &Path,
    cancel: &AtomicBool,
) -> Result<DeviceFileTransferReceipt, String>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    begin.validate()?;
    authorization.validate()?;
    if begin.operation != "file.sftp.upload" || authorization.device_id.is_empty() {
        return Err("设备大文件上传任务操作或签名设备无效。".into());
    }
    let expected_bytes =
        crate::device_files::validate_transfer_source(local_path, begin.maximum_bytes)?;
    let before = std::fs::symlink_metadata(local_path)
        .map_err(|_| "获准设备文件在上传前无法读取。".to_string())?;
    if before.len() != expected_bytes {
        return Err("获准设备文件长度在上传开始前发生变化。".into());
    }
    let begin_frame = ClientFrameOut::DeviceFileBegin {
        begin: begin.clone(),
        authorization,
        fields: BTreeMap::from([
            ("type".into(), "固定 FILE 文件帧类型".into()),
            (
                "begin".into(),
                "绑定原 DSH 票据、操作和设备代次的签名申请".into(),
            ),
            (
                "authorization".into(),
                "已批准设备身份的一次性签名证明".into(),
            ),
        ]),
    };
    write_client_frame_async(stream, &begin_frame, cancel).await?;
    let ready = match read_server_frame_async(stream, cancel).await? {
        ServerFrameIn::DeviceFileReady {
            transfer_id,
            chunk_bytes,
            total_bytes,
            next_offset,
            sha256,
            ready,
            ..
        } if transfer_id == begin.transfer_id => {
            (chunk_bytes, total_bytes, next_offset, sha256, ready)
        }
        ServerFrameIn::Error { message } => return Err(message),
        _ => return Err("服务端没有绑定原 DSH 上传票据。".into()),
    };
    let (chunk_limit, total_bytes, next_offset, ready_sha256, ready) = ready;
    if chunk_limit == 0
        || chunk_limit > MAX_FILE_CHUNK_BYTES
        || total_bytes != expected_bytes
        || total_bytes > begin.maximum_bytes
        || next_offset > total_bytes
        || (ready
            && ready_sha256
                .as_deref()
                .map_or(true, |value| !valid_sha256(value)))
    {
        return Err("服务端上传票据回执与批准长度或分块上限不匹配。".into());
    }

    let mut source = tokio::fs::File::open(local_path)
        .await
        .map_err(|_| "获准设备大文件读取源无法打开。".to_string())?;
    let mut file_hasher = Sha256::new();
    let mut total = 0_u64;
    let mut offset = 0_u64;
    let mut buffer = vec![0_u8; chunk_limit];
    if !ready {
        loop {
            if cancel.load(Ordering::Acquire) {
                return Err("设备大文件上传已取消。".into());
            }
            let read = source
                .read(&mut buffer)
                .await
                .map_err(|_| "获准设备大文件读取中断。".to_string())?;
            if read == 0 {
                break;
            }
            total = total
                .checked_add(read as u64)
                .ok_or("设备大文件长度溢出。")?;
            if total > total_bytes || total > begin.maximum_bytes {
                return Err("设备大文件上传源超过原 DSH 审批长度。".into());
            }
            let bytes = &buffer[..read];
            let chunk_sha256 = sha256_hex(bytes);
            file_hasher.update(bytes);
            write_client_frame_async(
                stream,
                &ClientFrameOut::DeviceFileUploadChunk {
                    transfer_id: begin.transfer_id.clone(),
                    offset,
                    byte_length: read,
                    chunk_sha256: chunk_sha256.clone(),
                    fields: BTreeMap::from([
                        ("type".into(), "固定 FILE 文件帧类型".into()),
                        ("transfer_id".into(), "原 DSH 文件传输编号".into()),
                        ("offset".into(), "当前原始文件字节偏移".into()),
                        ("byte_length".into(), "后续原始分块长度".into()),
                        ("chunk_sha256".into(), "当前原始分块 SHA-256 摘要".into()),
                    ]),
                },
                cancel,
            )
            .await?;
            stream.write_all(bytes).await.map_err(|_| {
                "设备大文件分块写入 FILE 通道失败；原任务结果可能未知。".to_string()
            })?;
            stream
                .flush()
                .await
                .map_err(|_| "设备大文件分块刷新失败。".to_string())?;
            match read_server_frame_async(stream, cancel).await? {
                ServerFrameIn::DeviceFileChunkAck {
                    transfer_id,
                    next_offset,
                    ..
                } if transfer_id == begin.transfer_id && next_offset == offset + read as u64 => {}
                ServerFrameIn::Error { message } => return Err(message),
                _ => return Err("服务端分块偏移回执与上传内容不匹配。".into()),
            }
            offset += read as u64;
        }
    } else {
        loop {
            if cancel.load(Ordering::Acquire) {
                return Err("设备大文件上传结果确认已取消。".into());
            }
            let read = source
                .read(&mut buffer)
                .await
                .map_err(|_| "设备大文件复核读取中断。".to_string())?;
            if read == 0 {
                break;
            }
            total = total
                .checked_add(read as u64)
                .ok_or("设备大文件复核长度溢出。")?;
            if total > total_bytes {
                return Err("设备大文件复核长度超过原票据。".into());
            }
            file_hasher.update(&buffer[..read]);
        }
    }
    let after = std::fs::symlink_metadata(local_path)
        .map_err(|_| "获准设备文件在上传后无法复核。".to_string())?;
    if total != total_bytes
        || before.len() != after.len()
        || before.modified().ok() != after.modified().ok()
    {
        return Err("设备大文件上传期间文件长度或修改时间发生变化。".into());
    }
    let sha256 = format_sha256(file_hasher.finalize().as_slice());
    if ready && ready_sha256.as_deref() != Some(sha256.as_str()) {
        return Err("设备大文件内容与先前完成的原 DSH 上传摘要不同。".into());
    }
    write_client_frame_async(
        stream,
        &ClientFrameOut::DeviceFileFinish {
            transfer_id: begin.transfer_id.clone(),
            expected_bytes: total_bytes,
            expected_sha256: sha256.clone(),
            fields: BTreeMap::from([
                ("type".into(), "固定 FILE 文件帧类型".into()),
                ("transfer_id".into(), "原 DSH 文件传输编号".into()),
                ("expected_bytes".into(), "完整原始文件字节数".into()),
                ("expected_sha256".into(), "完整原始文件 SHA-256 摘要".into()),
            ]),
        },
        cancel,
    )
    .await?;
    let result = match read_server_frame_async(stream, cancel).await? {
        ServerFrameIn::DeviceFileComplete {
            transfer_id,
            result,
            ..
        } if transfer_id == begin.transfer_id => result,
        ServerFrameIn::Error { message } => return Err(message),
        _ => return Err("服务端没有完成原 DSH 文件上传校验。".into()),
    };
    validate_device_file_result(&result, &begin.transfer_id, total_bytes, &sha256)?;
    Ok(DeviceFileTransferReceipt {
        path: format!("transfer:{}", begin.transfer_id),
        bytes: total_bytes,
        sha256,
        transfer_id: begin.transfer_id,
    })
}

/// download_device_file（服务端到设备大文件）：仅在临时文件完整摘要复核后原子创建获批目标。
#[cfg(feature = "ssh-device")]
pub async fn download_device_file<S>(
    stream: &mut S,
    begin: DeviceFileTransferBegin,
    authorization: FileTransferAuthorization,
    local_path: &Path,
    cancel: &AtomicBool,
) -> Result<DeviceFileTransferReceipt, String>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let transfer_id = begin.transfer_id.clone();
    let temporary = device_file_temporary_path(local_path, &transfer_id)?;
    let result =
        download_device_file_inner(stream, begin, authorization, local_path, &temporary, cancel)
            .await;
    if result.is_err() {
        let emergency = AtomicBool::new(false);
        let _ =
            write_client_frame_async(stream, &device_file_cancel_frame(&transfer_id), &emergency)
                .await;
        let _ = tokio::fs::remove_file(&temporary).await;
    }
    result
}

#[cfg(feature = "ssh-device")]
async fn download_device_file_inner<S>(
    stream: &mut S,
    begin: DeviceFileTransferBegin,
    authorization: FileTransferAuthorization,
    local_path: &Path,
    temporary: &Path,
    cancel: &AtomicBool,
) -> Result<DeviceFileTransferReceipt, String>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    begin.validate()?;
    authorization.validate()?;
    if begin.operation != "file.sftp.download" || authorization.device_id.is_empty() {
        return Err("设备大文件下载任务操作或签名设备无效。".into());
    }
    if !begin.expected_sha256.as_deref().is_some_and(valid_sha256) {
        return Err("设备附件下载请求缺少有效的完整摘要。".into());
    }
    crate::device_files::validate_transfer_target_parent(local_path)?;
    crate::device_files::reject_reparse_chain(temporary)?;
    if tokio::fs::try_exists(temporary)
        .await
        .map_err(|_| "设备大文件临时目标状态无法读取。".to_string())?
    {
        return Err("设备大文件临时目标已存在，拒绝覆盖。".into());
    }
    let begin_frame = ClientFrameOut::DeviceFileBegin {
        begin: begin.clone(),
        authorization,
        fields: BTreeMap::from([
            ("type".into(), "固定 FILE 文件帧类型".into()),
            (
                "begin".into(),
                "绑定原 DSH 票据、操作和设备代次的签名申请".into(),
            ),
            (
                "authorization".into(),
                "已批准设备身份的一次性签名证明".into(),
            ),
        ]),
    };
    write_client_frame_async(stream, &begin_frame, cancel).await?;
    let (chunk_limit, total_bytes, server_sha256) =
        match read_server_frame_async(stream, cancel).await? {
            ServerFrameIn::DeviceFileReady {
                transfer_id,
                chunk_bytes,
                total_bytes,
                sha256,
                ready: true,
                ..
            } if transfer_id == begin.transfer_id => (chunk_bytes, total_bytes, sha256),
            ServerFrameIn::Error { message } => return Err(message),
            _ => return Err("服务端没有准备已核验的原 DSH 附件。".into()),
        };
    let expected_sha256 = begin.expected_sha256.as_deref().unwrap_or_default();
    if chunk_limit == 0
        || chunk_limit > MAX_FILE_CHUNK_BYTES
        || total_bytes > begin.maximum_bytes
        || server_sha256.as_deref() != Some(expected_sha256)
    {
        return Err("服务端附件票据回执与批准长度、摘要或分块上限不匹配。".into());
    }
    let mut destination = tokio::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(temporary)
        .await
        .map_err(|_| "设备大文件临时文件无法独占创建。".to_string())?;
    let mut hasher = Sha256::new();
    let mut offset = 0_u64;
    while offset < total_bytes {
        if cancel.load(Ordering::Acquire) {
            return Err("设备附件下载已取消。".into());
        }
        let limit = usize::try_from((total_bytes - offset).min(chunk_limit as u64))
            .map_err(|_| "设备附件分块上限溢出。".to_string())?;
        write_client_frame_async(
            stream,
            &ClientFrameOut::DeviceFileDownloadChunk {
                transfer_id: begin.transfer_id.clone(),
                offset,
                limit,
                fields: BTreeMap::from([
                    ("type".into(), "固定 FILE 文件帧类型".into()),
                    ("transfer_id".into(), "原 DSH 文件传输编号".into()),
                    ("offset".into(), "请求读取的原始文件偏移".into()),
                    ("limit".into(), "本次读取最大字节数".into()),
                ]),
            },
            cancel,
        )
        .await?;
        let (byte_length, chunk_sha256) = match read_server_frame_async(stream, cancel).await? {
            ServerFrameIn::DeviceFileChunk {
                transfer_id,
                offset: actual_offset,
                byte_length,
                total_bytes: actual_total,
                sha256,
                chunk_sha256,
                ..
            } if transfer_id == begin.transfer_id
                && actual_offset == offset
                && actual_total == total_bytes
                && sha256 == expected_sha256
                && byte_length > 0
                && byte_length <= limit =>
            {
                (byte_length, chunk_sha256)
            }
            ServerFrameIn::Error { message } => return Err(message),
            _ => return Err("服务端附件分块回执与原票据偏移或摘要不匹配。".into()),
        };
        let mut bytes = vec![0_u8; byte_length];
        stream
            .read_exact(&mut bytes)
            .await
            .map_err(|_| "服务端附件分块负载中断。".to_string())?;
        if sha256_hex(&bytes) != chunk_sha256 {
            return Err("服务端附件分块 SHA-256 摘要不匹配。".into());
        }
        destination
            .write_all(&bytes)
            .await
            .map_err(|_| "设备附件临时文件写入失败。".to_string())?;
        hasher.update(&bytes);
        offset = offset
            .checked_add(byte_length as u64)
            .ok_or("设备附件下载长度溢出。")?;
        if offset > total_bytes {
            return Err("设备附件分块超出原票据总长度。".into());
        }
    }
    destination
        .flush()
        .await
        .map_err(|_| "设备附件临时文件刷新失败。".to_string())?;
    destination
        .sync_all()
        .await
        .map_err(|_| "设备附件临时文件同步失败。".to_string())?;
    drop(destination);
    let actual_sha256 = format_sha256(hasher.finalize().as_slice());
    if offset != total_bytes || actual_sha256 != expected_sha256 {
        return Err("设备附件完整长度或 SHA-256 摘要与已审批票据不匹配。".into());
    }
    write_client_frame_async(
        stream,
        &ClientFrameOut::DeviceFileFinish {
            transfer_id: begin.transfer_id.clone(),
            expected_bytes: total_bytes,
            expected_sha256: actual_sha256.clone(),
            fields: BTreeMap::from([
                ("type".into(), "固定 FILE 文件帧类型".into()),
                ("transfer_id".into(), "原 DSH 文件传输编号".into()),
                ("expected_bytes".into(), "完整原始文件字节数".into()),
                ("expected_sha256".into(), "完整原始文件 SHA-256 摘要".into()),
            ]),
        },
        cancel,
    )
    .await?;
    let result = match read_server_frame_async(stream, cancel).await? {
        ServerFrameIn::DeviceFileComplete {
            transfer_id,
            result,
            ..
        } if transfer_id == begin.transfer_id => result,
        ServerFrameIn::Error { message } => return Err(message),
        _ => return Err("服务端没有确认原 DSH 附件读取完成。".into()),
    };
    validate_device_file_result(&result, &begin.transfer_id, total_bytes, &actual_sha256)?;
    if tokio::fs::try_exists(local_path)
        .await
        .map_err(|_| "设备附件最终目标状态无法复核。".to_string())?
    {
        return Err("设备附件目标在传输期间已被占用，拒绝覆盖。".into());
    }
    tokio::fs::rename(temporary, local_path)
        .await
        .map_err(|_| "设备附件原子提交到获批目标失败。".to_string())?;
    Ok(DeviceFileTransferReceipt {
        path: format!("transfer:{}", begin.transfer_id),
        bytes: total_bytes,
        sha256: actual_sha256,
        transfer_id: begin.transfer_id,
    })
}

#[cfg(feature = "ssh-device")]
fn device_file_cancel_frame(transfer_id: &str) -> ClientFrameOut {
    ClientFrameOut::DeviceFileCancel {
        transfer_id: transfer_id.to_owned(),
        fields: BTreeMap::from([
            ("type".into(), "固定 FILE 文件帧类型".into()),
            ("transfer_id".into(), "原 DSH 文件传输编号".into()),
        ]),
    }
}

#[cfg(feature = "ssh-device")]
fn device_file_temporary_path(local_path: &Path, transfer_id: &str) -> Result<PathBuf, String> {
    if !local_path.is_absolute() || !valid_transfer_id_for_file_channel(transfer_id) {
        return Err("设备附件目标路径或传输编号无效。".into());
    }
    let parent = local_path.parent().ok_or("设备附件目标缺少父目录。")?;
    let name = local_path
        .file_name()
        .ok_or("设备附件目标缺少文件名。")?
        .to_string_lossy();
    Ok(parent.join(format!("{name}.{transfer_id}.part")))
}

#[cfg(feature = "ssh-device")]
fn valid_transfer_id_for_file_channel(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(feature = "ssh-device")]
fn validate_device_file_result(
    result: &Value,
    transfer_id: &str,
    bytes: u64,
    sha256: &str,
) -> Result<(), String> {
    if result.get("transfer_id").and_then(Value::as_str) != Some(transfer_id)
        || result.get("state").and_then(Value::as_str) != Some("ready")
        || result.get("bytes").and_then(Value::as_u64) != Some(bytes)
        || result.get("sha256").and_then(Value::as_str) != Some(sha256)
    {
        return Err("服务端 DFT 完成回执与原文件传输编号、长度或摘要不一致。".into());
    }
    Ok(())
}

#[cfg(feature = "ssh-device")]
async fn cancellable_io<F, T>(
    future: F,
    cancel: &AtomicBool,
    message: &'static str,
) -> Result<T, String>
where
    F: std::future::Future<Output = io::Result<T>>,
{
    tokio::pin!(future);
    loop {
        if cancel.load(Ordering::Acquire) {
            return Err("远程文件上传已取消。".into());
        }
        tokio::select! {
            result = &mut future => return result.map_err(|_| message.to_string()),
            _ = tokio::time::sleep(std::time::Duration::from_millis(50)) => {},
        }
    }
}

#[cfg(feature = "ssh-device")]
async fn wait_for_cancel(cancel: &AtomicBool) {
    while !cancel.load(Ordering::Acquire) {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
}

#[cfg(any(test, feature = "ssh-device"))]
struct PlannedFile {
    source_path: PathBuf, // source_path（仅客户端原生进程使用的本机读取路径）。
    entry: RemoteImportFileEntry, // entry（只通过 SSH 文件通道发送的相对路径、长度和摘要）。
    modified: Option<SystemTime>, // modified（用于检查上传期间本机文件是否变化）。
}

#[cfg(any(test, feature = "ssh-device"))]
fn build_upload_plan(sources: &[PathBuf]) -> Result<Vec<PlannedFile>, String> {
    let mut plan = Vec::new();
    let mut scanned_entries = 0_usize;
    for source in sources {
        if !source.is_absolute() {
            return Err("本机文件选择结果必须是绝对路径。".into());
        }
        crate::device_files::reject_reparse_chain(source)?;
        let metadata = fs::symlink_metadata(source)
            .map_err(|_| "本机选择的文件或文件夹已不可读取。".to_string())?;
        if metadata.file_type().is_symlink() || !(metadata.is_file() || metadata.is_dir()) {
            return Err("本机选择只能包含普通文件或文件夹。".into());
        }
        let name = source
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or("本机选择的名称不是有效 Unicode 文件名。")?;
        if metadata.is_dir() {
            walk_source_tree(source, Path::new(name), &mut plan, &mut scanned_entries)?;
        } else {
            add_planned_file(source, Path::new(name), &mut plan)?;
        }
    }
    plan.sort_by(|left, right| left.entry.relative_path.cmp(&right.entry.relative_path));
    if plan.is_empty() || plan.len() > REMOTE_IMPORT_MAX_FILES {
        return Err("本机选择必须包含 1 至 4096 个普通文件。".into());
    }
    let total_bytes = plan.iter().try_fold(0_u64, |total, file| {
        total
            .checked_add(file.entry.byte_length)
            .filter(|value| *value <= REMOTE_IMPORT_MAX_BYTES)
            .ok_or_else(|| "本机选择的总字节数超过 4 GiB 上限。".to_string())
    })?;
    if total_bytes == 0 {
        return Err("远程导入至少需要一个非空文件。".into());
    }
    Ok(plan)
}

#[cfg(any(test, feature = "ssh-device"))]
fn walk_source_tree(
    root: &Path,
    relative: &Path,
    plan: &mut Vec<PlannedFile>,
    scanned_entries: &mut usize,
) -> Result<(), String> {
    if plan.len() > REMOTE_IMPORT_MAX_FILES {
        return Err("本机选择的文件数超过 4096 个。".into());
    }
    if relative.components().count() > 128 {
        return Err("本机文件夹路径层级超过 128 层。".into());
    }
    crate::device_files::reject_reparse_chain(root)?;
    let entries = fs::read_dir(root).map_err(|_| "本机选择的文件夹无法列举。".to_string())?;
    let mut children = Vec::new();
    for entry in entries {
        *scanned_entries = (*scanned_entries).saturating_add(1);
        if *scanned_entries > MAX_SOURCE_TREE_ENTRIES {
            return Err("本机文件夹树的文件与目录总项超过 8192 个。".into());
        }
        children.push(entry.map_err(|_| "本机文件夹包含不可读取的条目。".to_string())?);
        if children.len() > REMOTE_IMPORT_MAX_FILES {
            return Err("单个本机文件夹的目录项超过 4096 个。".into());
        }
    }
    children.sort_by_key(|entry| entry.file_name());
    for child in children {
        let path = child.path();
        crate::device_files::reject_reparse_chain(&path)?;
        let metadata =
            fs::symlink_metadata(&path).map_err(|_| "本机文件夹条目状态无法读取。".to_string())?;
        if metadata.file_type().is_symlink() || !(metadata.is_file() || metadata.is_dir()) {
            return Err("本机文件夹含链接或特殊文件，远程导入已拒绝。".into());
        }
        let name = child
            .file_name()
            .into_string()
            .map_err(|_| "本机文件夹含非 Unicode 文件名。".to_string())?;
        let child_relative = relative.join(name);
        if metadata.is_dir() {
            walk_source_tree(&path, &child_relative, plan, scanned_entries)?;
        } else {
            add_planned_file(&path, &child_relative, plan)?;
        }
    }
    Ok(())
}

#[cfg(any(test, feature = "ssh-device"))]
fn add_planned_file(
    path: &Path,
    relative: &Path,
    plan: &mut Vec<PlannedFile>,
) -> Result<(), String> {
    if plan.len() >= REMOTE_IMPORT_MAX_FILES {
        return Err("本机选择的文件数超过 4096 个。".into());
    }
    crate::device_files::reject_reparse_chain(path)?;
    let metadata = safe_file_metadata(path)?;
    let byte_length = metadata.len();
    if byte_length == 0 || byte_length > REMOTE_IMPORT_MAX_BYTES {
        return Err("远程导入不接受空文件或超过 4 GiB 的单个文件。".into());
    }
    let planned_bytes = plan.iter().fold(0_u64, |total, file| {
        total.saturating_add(file.entry.byte_length)
    });
    if planned_bytes.saturating_add(byte_length) > REMOTE_IMPORT_MAX_BYTES {
        return Err("本机选择的总字节数超过 4 GiB 上限。".into());
    }
    let relative_path = path_to_manifest_string(relative)?;
    // placeholder source_id（占位编号）仅调用共享路径/长度验证器，不进入任何上传帧。
    RemoteImportManifest {
        source_id: "0".repeat(32),
        total_bytes: byte_length,
        files: vec![RemoteImportFileEntry {
            relative_path: relative_path.clone(),
            byte_length,
            sha256: "0".repeat(64),
        }],
    }
    .validate()?;
    let modified = modified_time(&metadata);
    let mut input = fs::File::open(path).map_err(|_| "本机获准文件读取失败。".to_string())?;
    let mut digest = Sha256::new();
    let mut read_total = 0_u64;
    let mut buffer = [0_u8; MAX_FILE_CHUNK_BYTES];
    loop {
        let read = input
            .read(&mut buffer)
            .map_err(|_| "本机文件清单摘要计算失败。".to_string())?;
        if read == 0 {
            break;
        }
        read_total += read as u64;
        if read_total > byte_length || read_total > REMOTE_IMPORT_MAX_BYTES {
            return Err("本机文件在摘要计算期间增长或超过上限。".into());
        }
        digest.update(&buffer[..read]);
    }
    let after = safe_file_metadata(path)?;
    if read_total != byte_length || after.len() != byte_length || modified_time(&after) != modified
    {
        return Err("本机文件在建立导入清单时发生变化。".into());
    }
    plan.push(PlannedFile {
        source_path: path.to_path_buf(),
        entry: RemoteImportFileEntry {
            relative_path,
            byte_length,
            sha256: format_sha256(digest.finalize().as_slice()),
        },
        modified,
    });
    Ok(())
}

#[cfg(any(test, feature = "ssh-device"))]
fn path_to_manifest_string(path: &Path) -> Result<String, String> {
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(value) => {
                parts.push(value.to_str().ok_or("本机来源含非 Unicode 文件名。")?)
            }
            _ => return Err("本机来源无法转换为规范相对路径。".into()),
        }
    }
    let result = parts.join("/");
    if result.is_empty() {
        return Err("本机来源相对路径为空。".into());
    }
    Ok(result)
}

#[cfg(any(test, feature = "ssh-device"))]
fn safe_file_metadata(path: &Path) -> Result<fs::Metadata, String> {
    crate::device_files::reject_reparse_chain(path)?;
    let metadata = fs::symlink_metadata(path).map_err(|_| "本机获准文件不可读取。".to_string())?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err("本机获准文件必须是普通文件。".into());
    }
    Ok(metadata)
}

#[cfg(any(test, feature = "ssh-device"))]
fn modified_time(metadata: &fs::Metadata) -> Option<SystemTime> {
    metadata.modified().ok()
}

#[cfg(any(test, feature = "ssh-device"))]
fn format_sha256(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{Cursor, Read, Write},
        sync::Mutex,
    };

    fn prelude() -> DeviceBridgePrelude {
        DeviceBridgePrelude {
            message_type: "bridge.prelude".into(),
            protocol_version: 1,
            workspace_id: "a".repeat(32),
            owner_sid: "S-1-5-21-42".into(),
            client_device_id: "device-a".into(),
            build: "build-1".into(),
            fields: BTreeMap::from([
                ("type".into(), "固定桥接消息类型".into()),
                ("protocol_version".into(), "桥接协议版本".into()),
                ("workspace_id".into(), "正式工作区编号".into()),
                ("owner_sid".into(), "Windows 登录账户 SID".into()),
                (
                    "client_device_id".into(),
                    "SSH 强制命令绑定的客户端编号".into(),
                ),
                ("build".into(), "正式程序构建编号".into()),
            ]),
        }
    }

    #[test]
    fn upload_manifest_reuses_shared_relative_path_and_total_validation() {
        let request = UploadBegin {
            source_id: "0123456789abcdef0123456789abcdef".into(),
            request_id: "a".repeat(32),
            total_bytes: 2,
            files: vec![RemoteImportFileEntry {
                relative_path: "folder/file.bin".into(),
                byte_length: 2,
                sha256: "a".repeat(64),
            }],
        };
        assert!(request.validate().is_ok());
        let mut traversal = request.clone();
        traversal.files[0].relative_path = "../file.bin".into();
        assert!(traversal.validate().is_err());
        let mut wrong_total = request;
        wrong_total.total_bytes = 3;
        assert!(wrong_total.validate().is_err());
    }

    #[test]
    fn dsh_file_begin_binds_fixed_operation_ticket_generations_and_never_serializes_paths() {
        let mut fields = BTreeMap::from([
            ("protocol_version".into(), "设备文件通道协议版本".into()),
            ("action".into(), "固定为 begin 的原票据申请".into()),
            ("transfer_id".into(), "原 DSH 文件传输编号".into()),
            ("operation".into(), "兼容保留的文件任务操作编号".into()),
            (
                "parent_request_id".into(),
                "生成原文件票据的稳定 DSH 请求编号".into(),
            ),
            ("connection_generation".into(), "SSH 设备连接代次".into()),
            ("executor_generation".into(), "设备执行器代次".into()),
            ("maximum_bytes".into(), "原审批允许的最大文件字节数".into()),
        ]);
        let upload = DeviceFileTransferBegin {
            protocol_version: 1,
            action: "begin".into(),
            transfer_id: "b".repeat(32),
            operation: "file.sftp.upload".into(),
            parent_request_id: "dsh-read-request-1".into(),
            connection_generation: 4,
            executor_generation: 7,
            maximum_bytes: 1024,
            expected_sha256: None,
            fields: fields.clone(),
        };
        assert!(upload.validate().is_ok());
        let serialized = serde_json::to_value(&upload).unwrap();
        assert!(serialized.get("remote_path").is_none());
        assert!(serialized.get("local_path").is_none());
        assert!(serialized.get("path").is_none());
        assert!(serialized.get("staging_dir").is_none());

        let mut traversal = upload.clone();
        traversal.parent_request_id = "../other".into();
        assert!(traversal.validate().is_err());
        let mut path_in_fields = upload.clone();
        path_in_fields
            .fields
            .insert("remote_path".into(), "不允许路径字段".into());
        assert!(path_in_fields.validate().is_err());

        fields.insert(
            "expected_sha256".into(),
            "服务端已锁定的审批附件完整摘要".into(),
        );
        let download = DeviceFileTransferBegin {
            operation: "file.sftp.download".into(),
            expected_sha256: Some("c".repeat(64)),
            fields,
            ..upload
        };
        assert!(download.validate().is_ok());
        let mut missing_digest = download;
        missing_digest.expected_sha256 = None;
        assert!(missing_digest.validate().is_err());
    }

    #[test]
    fn bridge_prelude_is_required_before_any_file_frame() {
        let mut encoded = serde_json::to_vec(&prelude()).unwrap();
        encoded.push(b'\n');
        let peer = read_bridge_peer(&mut Cursor::new(encoded)).unwrap();
        assert_eq!(peer.device_id, "device-a");
        let bad = DeviceBridgePrelude {
            client_device_id: "../other".into(),
            ..prelude()
        };
        let mut encoded = serde_json::to_vec(&bad).unwrap();
        encoded.push(b'\n');
        assert!(read_bridge_peer(&mut Cursor::new(encoded)).is_err());
    }

    #[test]
    fn dsh_file_frames_bind_authenticated_peer_and_keep_channel_token_server_side() {
        struct MemoryDuplex {
            input: Cursor<Vec<u8>>,
            output: Vec<u8>,
        }
        impl Read for MemoryDuplex {
            fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
                self.input.read(buffer)
            }
        }
        impl Write for MemoryDuplex {
            fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
                self.output.extend_from_slice(buffer);
                Ok(buffer.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        struct DshReceiver {
            started: Mutex<Vec<DeviceFileTransferSession>>,
            cancelled: Mutex<Vec<String>>,
        }
        impl FileReceiver for DshReceiver {
            fn begin(
                &self,
                _peer: &FileChannelPeer,
                _request: UploadBegin,
            ) -> Result<Option<Value>, String> {
                Err("resource import not used".into())
            }
            fn append_chunk(
                &self,
                _peer: &FileChannelPeer,
                _source_id: &str,
                _file_index: usize,
                _offset: u64,
                _chunk_sha256: &str,
                _bytes: &[u8],
            ) -> Result<u64, String> {
                Err("resource import not used".into())
            }
            fn finish(&self, _peer: &FileChannelPeer, _source_id: &str) -> Result<Value, String> {
                Err("resource import not used".into())
            }
            fn abort(&self, _peer: &FileChannelPeer, _source_id: &str) -> Result<(), String> {
                Ok(())
            }
            fn begin_device_file_transfer(
                &self,
                peer: &FileChannelPeer,
                request: DeviceFileTransferBegin,
                authorization: FileTransferAuthorization,
            ) -> Result<DeviceFileTransferSession, String> {
                assert_eq!(peer.device_id, "device-a");
                assert_eq!(authorization.device_id, peer.device_id);
                request.validate()?;
                let session = DeviceFileTransferSession {
                    device_id: peer.device_id.clone(),
                    transfer_id: request.transfer_id,
                    channel_token: "z".repeat(43),
                    operation: request.operation,
                    connection_generation: request.connection_generation,
                    executor_generation: request.executor_generation,
                    total_bytes: 2,
                    sha256: None,
                    next_offset: 0,
                    ready: false,
                };
                self.started.lock().unwrap().push(session.clone());
                Ok(session)
            }
            fn cancel_device_file_transfer(
                &self,
                _peer: &FileChannelPeer,
                session: &DeviceFileTransferSession,
            ) -> Result<(), String> {
                self.cancelled
                    .lock()
                    .unwrap()
                    .push(session.transfer_id.clone());
                Ok(())
            }
        }

        let receiver = DshReceiver {
            started: Mutex::new(Vec::new()),
            cancelled: Mutex::new(Vec::new()),
        };
        let fields = BTreeMap::from([
            ("protocol_version".into(), "设备文件通道协议版本".into()),
            ("action".into(), "固定为 begin 的原票据申请".into()),
            ("transfer_id".into(), "原 DSH 文件传输票据编号".into()),
            ("operation".into(), "兼容保留的文件任务操作编号".into()),
            (
                "parent_request_id".into(),
                "生成原文件票据的稳定 DSH 请求编号".into(),
            ),
            ("connection_generation".into(), "SSH 设备连接代次".into()),
            ("executor_generation".into(), "设备执行器代次".into()),
            ("maximum_bytes".into(), "原审批允许的最大文件字节数".into()),
        ]);
        let begin = DeviceFileTransferBegin {
            protocol_version: 1,
            action: "begin".into(),
            transfer_id: "d".repeat(32),
            operation: "file.sftp.upload".into(),
            parent_request_id: "dsh-parent-1".into(),
            connection_generation: 2,
            executor_generation: 3,
            maximum_bytes: 2,
            expected_sha256: None,
            fields,
        };
        let authorization = FileTransferAuthorization {
            device_id: "device-a".into(),
            core_session_id: "1".repeat(32),
            challenge: "a".repeat(43),
            timestamp_ms: 1000,
            signature: base64::engine::general_purpose::STANDARD.encode([7_u8; 64]),
        };
        let begin_frame = serde_json::json!({
            "type":"device_file_begin", "begin":begin, "authorization":authorization,
            "fields":{"type":"固定 FILE 文件帧类型","begin":"原 DSH 票据签名申请",
                "authorization":"设备身份签名证明"}
        });
        let cancel_frame = serde_json::json!({
            "type":"device_file_cancel", "transfer_id":"d".repeat(32),
            "fields":{"type":"固定 FILE 文件帧类型","transfer_id":"原 DSH 文件传输编号"}
        });
        let mut input = serde_json::to_vec(&prelude()).unwrap();
        input.push(b'\n');
        for frame in [begin_frame, cancel_frame] {
            let bytes = serde_json::to_vec(&frame).unwrap();
            input.extend_from_slice(&(bytes.len() as u32).to_be_bytes());
            input.extend_from_slice(&bytes);
        }
        let mut cursor = MemoryDuplex {
            input: Cursor::new(input),
            output: Vec::new(),
        };
        let result = serve_file_channel_connection(&mut cursor, &receiver);
        assert_eq!(receiver.started.lock().unwrap().len(), 1);
        assert_eq!(
            receiver.cancelled.lock().unwrap().as_slice(),
            ["d".repeat(32)]
        );
        let response_text = String::from_utf8_lossy(&cursor.output);
        assert!(
            result.is_ok(),
            "unexpected fixed FILE channel rejection: {result:?}; responses={response_text}"
        );
        assert!(response_text.contains("device_file_ready"));
        assert!(response_text.contains("device_file_cancelled"));
        assert!(!response_text.contains(&"z".repeat(43)));
    }

    #[test]
    fn local_upload_plan_keeps_only_relative_names_and_checks_sha256() {
        let root = std::env::temp_dir().join(format!(
            "device-file-client-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(root.join("models")).unwrap();
        fs::write(root.join("models/model.bin"), b"model contents").unwrap();
        let plan = build_upload_plan(&[root.join("models")]).unwrap();
        assert_eq!(plan.len(), 1);
        assert_eq!(plan[0].entry.relative_path, "models/model.bin");
        assert_eq!(plan[0].entry.sha256, sha256_hex(b"model contents"));
        assert!(!plan[0].entry.relative_path.contains(':'));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn server_connection_aborts_a_source_when_transport_disconnects() {
        struct Receiver {
            aborted: Mutex<Vec<String>>,
        }
        impl FileReceiver for Receiver {
            fn begin(
                &self,
                _peer: &FileChannelPeer,
                _request: UploadBegin,
            ) -> Result<Option<Value>, String> {
                Ok(None)
            }
            fn append_chunk(
                &self,
                _peer: &FileChannelPeer,
                _source_id: &str,
                _file_index: usize,
                _offset: u64,
                _chunk_sha256: &str,
                _bytes: &[u8],
            ) -> Result<u64, String> {
                Ok(0)
            }
            fn finish(&self, _peer: &FileChannelPeer, _source_id: &str) -> Result<Value, String> {
                Ok(Value::Null)
            }
            fn abort(&self, _peer: &FileChannelPeer, source_id: &str) -> Result<(), String> {
                self.aborted.lock().unwrap().push(source_id.to_owned());
                Ok(())
            }
        }
        let receiver = Receiver {
            aborted: Mutex::new(Vec::new()),
        };
        let mut input = serde_json::to_vec(&prelude()).unwrap();
        input.push(b'\n');
        let mut begin = serde_json::to_vec(&serde_json::json!({
            "type":"begin", "source_id":"0123456789abcdef0123456789abcdef",
            "request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "total_bytes":1,
            "files":[{"relative_path":"x.bin","byte_length":1,"sha256":"a".repeat(64)}],
            "fields":{"type":"固定文件通道消息类型","source_id":"服务端来源编号",
                "request_id":"稳定请求编号","total_bytes":"总原始字节数","files":"规范相对路径文件清单"}
        })).unwrap();
        input.extend_from_slice(&(begin.len() as u32).to_be_bytes());
        input.append(&mut begin);
        let mut cursor = Cursor::new(input);
        assert!(serve_file_channel_connection(&mut cursor, &receiver).is_err());
        assert_eq!(
            receiver.aborted.lock().unwrap().as_slice(),
            ["0123456789abcdef0123456789abcdef"]
        );
    }

    #[test]
    fn listener_shutdown_closes_active_stream_and_aborts_incomplete_transfer() {
        use std::{
            net::{Shutdown, TcpStream},
            sync::{
                atomic::{AtomicBool as ShutdownFlag, AtomicUsize, Ordering as AtomicOrdering},
                Arc,
            },
            thread,
            time::Duration,
        };

        struct ShutdownReceiver {
            aborted: AtomicUsize, // aborted（服务关闭时已中止的传输数）。
        }
        impl FileReceiver for ShutdownReceiver {
            fn begin(
                &self,
                _peer: &FileChannelPeer,
                _request: UploadBegin,
            ) -> Result<Option<Value>, String> {
                Ok(None)
            }
            fn append_chunk(
                &self,
                _peer: &FileChannelPeer,
                _source_id: &str,
                _file_index: usize,
                _offset: u64,
                _chunk_sha256: &str,
                _bytes: &[u8],
            ) -> Result<u64, String> {
                Ok(0)
            }
            fn finish(&self, _peer: &FileChannelPeer, _source_id: &str) -> Result<Value, String> {
                Ok(Value::Null)
            }
            fn abort(&self, _peer: &FileChannelPeer, _source_id: &str) -> Result<(), String> {
                self.aborted.fetch_add(1, AtomicOrdering::AcqRel);
                Ok(())
            }
        }

        fn write_wire_frame(stream: &mut TcpStream, frame: &Value) {
            let bytes = serde_json::to_vec(frame).unwrap();
            stream
                .write_all(&(bytes.len() as u32).to_be_bytes())
                .unwrap();
            thread::sleep(Duration::from_millis(100));
            stream.write_all(&bytes).unwrap();
            stream.flush().unwrap();
        }
        fn read_wire_frame(stream: &mut TcpStream) -> Value {
            let mut prefix = [0_u8; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut bytes = vec![0_u8; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut bytes).unwrap();
            serde_json::from_slice(&bytes).unwrap()
        }

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let receiver = Arc::new(ShutdownReceiver {
            aborted: AtomicUsize::new(0),
        });
        let shutdown = Arc::new(ShutdownFlag::new(false));
        let service_shutdown = shutdown.clone();
        let service_receiver: Arc<dyn FileReceiver> = receiver.clone();
        let service = thread::spawn(move || {
            serve_file_channel_listener(listener, service_receiver, service_shutdown)
        });

        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        client
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut prelude_bytes = serde_json::to_vec(&prelude()).unwrap();
        prelude_bytes.push(b'\n');
        // 真实非阻塞监听入口必须等待首包及后续分段，不能把 WouldBlock（暂时无数据）当作断线。
        thread::sleep(Duration::from_millis(150));
        let split = prelude_bytes.len() / 2;
        client.write_all(&prelude_bytes[..split]).unwrap();
        thread::sleep(Duration::from_millis(150));
        client.write_all(&prelude_bytes[split..]).unwrap();
        write_wire_frame(
            &mut client,
            &serde_json::json!({
                "type":"begin", "source_id":"0123456789abcdef0123456789abcdef",
                "request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "total_bytes":1,
                "files":[{"relative_path":"x.bin","byte_length":1,"sha256":"a".repeat(64)}],
                "fields":{"type":"固定文件通道消息类型","source_id":"服务端来源编号",
                    "request_id":"稳定请求编号","total_bytes":"总原始字节数","files":"规范相对路径文件清单"}
            }),
        );
        assert_eq!(read_wire_frame(&mut client)["type"], "ready");

        shutdown.store(true, AtomicOrdering::Release);
        let service_result = service.join().unwrap();
        assert!(service_result.is_ok(), "{service_result:?}");
        assert_eq!(receiver.aborted.load(AtomicOrdering::Acquire), 1);
        let _ = client.shutdown(Shutdown::Both);
    }
}
