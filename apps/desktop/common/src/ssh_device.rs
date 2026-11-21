//! SSH device gateway（SSH 设备网关）：从原生桌面进程直连已固定主机。
//!
//! 本模块只提供 SSH 主机验证、一次性密钥认证和到远端回环网关的受限
//! HTTP（超文本传输协议）控制通道。不得回退到命令行 SSH、密码认证或未固定主机。

use crate::device_file_channel::{
    DeviceFileTransferBegin, FileTransferAuthorization, FILE_TRANSFER_GATEWAY_PATH,
};
use crate::device_management_contract::{
    RemoteManagementOpenRequest, RemoteManagementOpenResponse, RemoteManagementRequest,
    RemoteManagementResponse, DEVICE_MANAGEMENT_PROTOCOL_VERSION,
};
use crate::device_protocol::{AudioCodec, MEDIA_PROTOCOL_VERSION};
use crate::device_receipts::{DeviceTaskReceiptJournal, PendingDeviceReceipt};
use base64::Engine;
use ed25519_dalek::{Signer, SigningKey};
use russh::keys::ssh_key::LineEnding;
use russh::{
    client::{self, AuthResult},
    keys::{Algorithm, HashAlg, PrivateKey, PrivateKeyWithHashAlg, PublicKeyOrCertificate},
    Disconnect,
};
use serde::{Deserialize, Serialize};
use sha1::{Digest as Sha1Digest, Sha1};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    future::Future,
    io::Read,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::io::{self, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadHalf, WriteHalf};
use zeroize::Zeroizing;

pub const DEFAULT_GATEWAY_PORT: u16 = 18_765; // DEFAULT_GATEWAY_PORT（旧客户端 HTTP 请求地址的兼容端口；服务端按通道标记解析实际监听）。
const CONTROL_BRIDGE_MARKER: &str = "voice-terminal-v1-control"; // CONTROL_BRIDGE_MARKER（受限 SSH 控制桥接标记）。
const MEDIA_BRIDGE_MARKER: &str = "voice-terminal-v1-media"; // MEDIA_BRIDGE_MARKER（受限 SSH 媒体桥接标记）。
const MANAGEMENT_BRIDGE_MARKER: &str = "voice-terminal-v1-management"; // MANAGEMENT_BRIDGE_MARKER（独立 SSH 原生管理桥接标记）。
const FILE_BRIDGE_MARKER: &str = "voice-terminal-v1-file"; // FILE_BRIDGE_MARKER（独立 SSH 大文件通道标记）。
const MAX_REQUEST_BYTES: usize = 1_048_576; // MAX_REQUEST_BYTES（控制请求体上限，字节）。
const MAX_RESPONSE_BYTES: usize = 2_097_152; // MAX_RESPONSE_BYTES（控制回执体上限，字节）。
const MAX_TASK_RECEIPT_BYTES: usize = 256 * 1024; // MAX_TASK_RECEIPT_BYTES（设备任务回执完整请求上限）。
const MAX_HEADER_BYTES: usize = 16_384; // MAX_HEADER_BYTES（HTTP 响应头上限，字节）。
const MAX_MEDIA_MESSAGE_BYTES: usize = 270_336; // MAX_MEDIA_MESSAGE_BYTES（WebSocket 单消息上限，字节）。
const REMOTE_DEVICE_OPERATIONS: &[&str] = &[
    "windows",
    "apps",
    "snapshot",
    "shot",
    "screen.capture_file",
    "tree",
    "click",
    "type",
    "scroll",
    "key",
    "move",
    "windowControl",
    "invokePattern",
    "readText",
    "readTable",
    "clipboard",
    "process",
    "displays",
    "cursorPosition",
    "notify",
    "multi_action",
    "terminal.create",
    "terminal.input",
    "terminal.output",
    "terminal.status",
    "terminal.cancel",
    "terminal.close",
    "file.list",
    "file.read",
    "file.stat",
    "file.sftp.upload",
    "file.sftp.download",
    "playback.pause",
    "playback.resume",
    "playback.stop_and_snapshot",
    "playback.prepare_replacement",
    "playback.bind_replacement",
    "playback.abort_replacement",
    "playback.wait_replacement",
]; // REMOTE_DEVICE_OPERATIONS（远端设备任务操作白名单）。

/// SshDeviceTarget（SSH 目标）：只含可公开的主机身份与 SSH 登录名。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SshDeviceTarget {
    pub address: String,         // address（主机 DNS 名称或 IP 地址）。
    pub port: u16,               // port（主机 SSH 端口）。
    pub username: String,        // username（受管 SSH 登录账户）。
    pub host_key_sha256: String, // host_key_sha256（用户已确认的 SHA-256 主机指纹）。
    pub gateway_port: u16,       // gateway_port（HTTP 请求地址的兼容端口，不用于选择服务端监听器）。
}

impl SshDeviceTarget {
    /// validate（校验）：拒绝命令分隔符、空白目标和无效主机指纹。
    pub fn validate(&self) -> Result<(), String> {
        let address_ok = !self.address.is_empty()
            && self.address.len() <= 255
            && self.address.trim() == self.address
            && self.address.bytes().all(|byte| {
                byte.is_ascii_alphanumeric()
                    || matches!(byte, b'.' | b'-' | b'_' | b':' | b'[' | b']')
            });
        let username_ok = !self.username.is_empty()
            && self.username.len() <= 128
            && self.username.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'\\')
            });
        let fingerprint_ok = self
            .host_key_sha256
            .strip_prefix("SHA256:")
            .is_some_and(|encoded| {
                base64::engine::general_purpose::STANDARD_NO_PAD
                    .decode(encoded)
                    .is_ok_and(|digest| digest.len() == 32)
            });
        if !address_ok
            || self.port == 0
            || !username_ok
            || !fingerprint_ok
            || self.gateway_port == 0
        {
            return Err("SSH 主机、端口、登录名或已确认指纹无效。".into());
        }
        Ok(())
    }
}

/// GatewayResponse（网关回执）：已限制大小并解析为 JSON（结构化数据）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GatewayResponse {
    pub status: u16,             // status（HTTP 状态码）。
    pub body: serde_json::Value, // body（JSON 业务回执）。
}

/// SftpTransferReceipt（兼容文件任务回执）：path 使用 transfer:<id>，不含任何服务器路径。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SftpTransferReceipt {
    pub path: String,   // path（文件传输目标路径）。
    pub bytes: u64,     // bytes（实际传输字节数）。
    pub sha256: String, // sha256（原始字节摘要）。
}

/// RemoteDeviceSessionLease（远端设备会话租约）：主机授予的连接与执行器代次。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RemoteDeviceSessionLease {
    pub protocol_version: u32,   // protocol_version（设备任务通道协议版本）。
    pub result: String,          // result（会话注册结果）。
    pub device_id: String,       // device_id（固定远端设备编号）。
    pub core_session_id: String, // core_session_id（当前权威核心运行编号）。
    pub connection_generation: u64, // connection_generation（主机授予的连接代次）。
    pub executor_generation: u64, // executor_generation（主机授予的执行器代次）。
    pub capabilities: Vec<String>, // capabilities（主机登记的设备能力）。
    #[serde(default)]
    pub separate_playback_tasks: bool, // separate_playback_tasks（主机支持独立播报控制轮询）。
}

/// RemoteDeviceTask（远端设备任务）：绑定设备、核心及两类代次的有界桌面命令。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RemoteDeviceTask {
    pub request_id: String,           // request_id（核心控制请求编号）。
    pub device_id: String,            // device_id（固定执行设备编号）。
    pub core_session_id: String,      // core_session_id（权威核心运行编号）。
    pub task_id: String,              // task_id（持久任务编号）。
    pub connection_generation: u64,   // connection_generation（设备连接代次）。
    pub executor_generation: u64,     // executor_generation（设备执行器代次）。
    pub sequence: u64,                // sequence（当前连接内的递增任务序号）。
    pub operation: String,            // operation（白名单桌面操作编号）。
    pub arguments: serde_json::Value, // arguments（JSON 桌面任务参数）。
}

/// RemoteDeviceTaskPoll（远端设备任务轮询）：无任务时 task（任务）为空。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RemoteDeviceTaskPoll {
    pub protocol_version: u32,   // protocol_version（设备任务通道协议版本）。
    pub result: String,          // result（轮询结果）。
    pub device_id: String,       // device_id（固定轮询设备编号）。
    pub core_session_id: String, // core_session_id（当前权威核心运行编号）。
    pub connection_generation: u64, // connection_generation（当前连接代次）。
    pub executor_generation: u64, // executor_generation（当前执行器代次）。
    pub task: Option<RemoteDeviceTask>, // task（本轮领取的任务；空值表示暂时无任务）。
}

/// RemoteDeviceTaskResult（远端执行结果）：只在辅助进程给出确定回执时提交。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RemoteDeviceTaskResult {
    pub succeeded: bool,           // succeeded（辅助进程是否明确确认成功）。
    pub result: serde_json::Value, // result（有限 JSON 操作结果或明确错误）。
}

fn task_completion_payload(
    task: &RemoteDeviceTask,
    succeeded: bool,
    result: &serde_json::Value,
) -> serde_json::Value {
    serde_json::json!({
        "protocol_version":1, "connection_generation":task.connection_generation,
        "executor_generation":task.executor_generation, "task_id":task.task_id,
        "sequence":task.sequence, "core_session_id":task.core_session_id,
        "succeeded":succeeded, "result":result,
    })
}

fn bounded_task_receipt(
    task: &RemoteDeviceTask,
    outcome: RemoteDeviceTaskResult,
) -> Result<RemoteDeviceTaskResult, String> {
    let payload = task_completion_payload(task, outcome.succeeded, &outcome.result);
    if serde_json::to_vec(&payload).map_or(false, |bytes| bytes.len() <= MAX_TASK_RECEIPT_BYTES) {
        return Ok(outcome);
    }
    if matches!(
        task.operation.as_str(),
        "windows" | "apps" | "snapshot" | "tree" | "shot" | "readText" | "readTable" | "displays"
            | "cursorPosition" | "file.list" | "file.read" | "file.stat"
    ) {
        return Ok(RemoteDeviceTaskResult {
            succeeded: false,
            result: serde_json::json!({
                "error":"只读设备结果超过单条控制回执容量，请缩小读取范围。",
                "fields":{"error":"设备读取回执未交付的原因"}
            }),
        });
    }
    Err("设备任务回执超过大小限制，操作结果未知。".into())
}

/// SshDeviceIdentity（SSH 设备身份）：私钥由当前项目目录加密维护，界面仅接收公钥。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SshDeviceIdentity {
    pub credential_ref: String, // credential_ref（项目内加密私钥引用）。
    pub public_key: String,     // public_key（供主机接受登记的 OpenSSH 公钥）。
    pub fingerprint: String,    // fingerprint（本设备公钥 SHA-256 指纹）。
}

/// SshTransportIdentity（SSH 登录身份）：登录密钥独立于主机批准的产品设备身份。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SshTransportIdentity {
    pub credential_ref: String, // credential_ref（本机 Windows 凭据引用；仅供 SSH 账户登录）。
    pub public_key: String,     // public_key（需由主机操作系统账户单独准入的 OpenSSH 公钥）。
    pub fingerprint: String,    // fingerprint（登录公钥 SHA-256 指纹）。
}

#[derive(Debug, Clone)]
struct DeviceGatewayAuthHeaders {
    device_id: String,       // device_id（已获主机批准的产品设备编号）。
    core_session_id: String, // core_session_id（本次权威核心运行编号）。
    challenge: String,       // challenge（本次请求的一次性签名挑战）。
    timestamp_ms: u64,       // timestamp_ms（客户端 Unix 时间毫秒）。
    signature: String,       // signature（签名后的 Base64 请求证明）。
}

#[derive(Debug, Deserialize)]
struct DeviceGatewayChallenge {
    protocol_version: u32,   // protocol_version（设备网关协议版本）。
    core_session_id: String, // core_session_id（本次权威核心运行编号）。
    challenge: String,       // challenge（一次性随机挑战）。
    expires_at_ms: u64,      // expires_at_ms（挑战过期 Unix 毫秒）。
}

struct ConnectedDeviceSession {
    connection: SshDeviceConnection, // connection（已固定主机身份的 SSH 通道，只发送控制、媒体与文件标记）。
    management: tokio::sync::Mutex<Option<SshDeviceConnection>>, // management（独立管理连接的专用 SSH 通道，只发送管理标记）：同一连接混用标记会被复用共用的原始命令挤占。
    device_id: String,               // device_id（当前远端客户端稳定编号）。
    identity_credential_ref: String, // identity_credential_ref（产品设备签名身份引用）。
    transport_credential_ref: String, // transport_credential_ref（独立文件传输连接复用的凭据引用）。
    target: SshDeviceTarget,          // target（固定主机、指纹及端口，文件传输不可替换）。
    #[cfg(any(test, feature = "ssh-device-test-support"))]
    signing_key_for_test: Option<Zeroizing<String>>, // signing_key_for_test（只在 SSH 集成测试中代替系统凭据库）。
    #[cfg(any(test, feature = "ssh-device-test-support"))]
    transport_key_for_test: Option<Zeroizing<String>>, // transport_key_for_test（只在 SSH 媒体集成测试中代替系统凭据库）。
}

impl ConnectedDeviceSession {
    /// close（关闭）：控制通道与独立管理通道分别结束；管理连接关闭失败不改写控制通道的结论。
    async fn close(&self) -> Result<(), String> {
        let result = self.connection.close().await;
        let management = self.management.try_lock().ok().and_then(|mut slot| slot.take());
        if let Some(management) = management {
            let _ = management.close().await;
        }
        result
    }
}

fn load_session_signing_key(
    manager: &SshDeviceManager,
    session: &ConnectedDeviceSession,
) -> Result<Zeroizing<String>, String> {
    #[cfg(any(test, feature = "ssh-device-test-support"))]
    if let Some(private_key) = &session.signing_key_for_test {
        return Ok(Zeroizing::new(private_key.to_string()));
    }
    manager.load_key(&session.identity_credential_ref)
}

fn load_session_transport_key(
    manager: &SshDeviceManager,
    session: &ConnectedDeviceSession,
) -> Result<Zeroizing<String>, String> {
    #[cfg(any(test, feature = "ssh-device-test-support"))]
    if let Some(private_key) = &session.transport_key_for_test {
        return Ok(Zeroizing::new(private_key.to_string()));
    }
    manager.load_key(&session.transport_credential_ref)
}

trait AsyncDeviceStream: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T> AsyncDeviceStream for T where T: AsyncRead + AsyncWrite + Unpin + Send {}

async fn write_management_line<T: serde::Serialize>(
    stream: &mut dyn AsyncDeviceStream,
    value: &T,
    max_bytes: usize,
) -> Result<(), String> {
    let mut bytes = serde_json::to_vec(value).map_err(|_| "远程管理请求无法编码。".to_string())?;
    if bytes.is_empty() || bytes.len() > max_bytes {
        return Err("远程管理请求超过大小上限。".into());
    }
    bytes.push(b'\n');
    stream
        .write_all(&bytes)
        .await
        .map_err(|_| "SSH 管理请求写入失败。".to_string())?;
    stream
        .flush()
        .await
        .map_err(|_| "SSH 管理请求刷新失败。".to_string())
}

async fn read_management_line<T: serde::de::DeserializeOwned>(
    stream: &mut dyn AsyncDeviceStream,
    max_bytes: usize,
) -> Result<T, String> {
    let mut bytes = Vec::with_capacity(4096);
    let mut byte = [0_u8; 1];
    loop {
        let count = stream
            .read(&mut byte)
            .await
            .map_err(|_| "SSH 管理回执读取失败。".to_string())?;
        if count == 0 {
            return Err("SSH 管理回执在换行终止前关闭。".into());
        }
        if byte[0] == b'\n' {
            break;
        }
        if bytes.len() >= max_bytes {
            return Err("SSH 管理回执超过大小上限。".into());
        }
        if byte[0] != b'\r' {
            bytes.push(byte[0]);
        }
    }
    serde_json::from_slice(&bytes).map_err(|_| "SSH 管理回执不是有效 JSON。".to_string())
}

/// SshMediaMessage（SSH 媒体消息）：服务端控制 JSON 或原始二进制音频帧。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SshMediaMessage {
    Text(String),    // text（服务端 JSON 控制消息）。
    Binary(Vec<u8>), // binary（设备媒体二进制帧）。
    Ping(Vec<u8>),   // ping（服务端 WebSocket 保活探测负载）。
    Pong(Vec<u8>),   // pong（服务端对客户端保活探测的响应）。
    Closed,          // closed（对端正常关闭 WebSocket）。
}

/// SshMediaChannel（SSH 媒体通道）：单独 SSH 连接承载一条已签名 WebSocket 音频流。
pub struct SshMediaChannel {
    websocket: SshWebSocketStream, // websocket（经过主机固定与签名握手的媒体通道）。
    _connection: SshDeviceConnection, // _connection（维持该媒体通道所属的 SSH 会话）。
    pub device_id: String,         // device_id（本机稳定设备编号）。
    pub stream_id: String,         // stream_id（本次连续媒体流编号）。
    pub core_session_id: String,   // core_session_id（主机本次权威核心运行编号）。
    pub connection_generation: u64, // connection_generation（主机授予的连接代次）。
    pub executor_generation: u64,  // executor_generation（设备执行器代次）。
    pub interaction_generation: u64, // interaction_generation（主机授予的语音占用代次）。
    pub uplink_codec: AudioCodec,  // uplink_codec（握手协商的设备上行编码）。
    pub downlink_codec: AudioCodec, // downlink_codec（握手协商的设备下行编码）。
    pub encoder_pre_skip_samples: u32, // encoder_pre_skip_samples（上行 Opus 编码器预延迟样本数）。
    pub downlink_encoder_pre_skip_samples: u32, // downlink_encoder_pre_skip_samples（下行 Opus 编码器预延迟样本数）。
    pub clock_uncertainty_us: Option<u64>, // clock_uncertainty_us（主机返回的设备时钟误差估计，微秒）。
    pub capture_active: bool,              // capture_active（主机当前是否已启动远端采集）。
}

/// media_ready_codecs（就绪回执编码协商）：解析双方已签名约定的编解码参数。
fn media_ready_codecs(
    value: &serde_json::Value,
) -> Result<(AudioCodec, AudioCodec, u32, u32), String> {
    let object = value
        .as_object()
        .ok_or("主机媒体就绪回执必须是 JSON 对象。")?;
    let codec = |name: &str| -> Result<AudioCodec, String> {
        match object.get(name).and_then(serde_json::Value::as_str) {
            Some("pcm") => Ok(AudioCodec::Pcm),
            Some("opus") => Ok(AudioCodec::Opus),
            _ => Err(format!("主机媒体就绪回执字段 {name}（音频编码）无效。")),
        }
    };
    let uplink = codec("uplink_codec")?;
    let downlink = codec("downlink_codec")?;
    let pre_skip = object
        .get("encoder_pre_skip_samples")
        .and_then(serde_json::Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or("主机媒体就绪回执的 Opus 编码预延迟字段无效。")?;
    let downlink_pre_skip = object.get("downlink_encoder_pre_skip_samples")
        .and_then(serde_json::Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or("主机媒体就绪回执的下行 Opus 编码预延迟字段无效。")?;
    if pre_skip > 320
        || (uplink == AudioCodec::Pcm && pre_skip != 0)
        || (uplink == AudioCodec::Opus && pre_skip == 0)
        || downlink_pre_skip > 960
        || (downlink == AudioCodec::Pcm && downlink_pre_skip != 0)
        || (downlink == AudioCodec::Opus && downlink_pre_skip == 0)
    {
        return Err("主机媒体就绪回执的编码模式与方向预延迟样本数不匹配。".into());
    }
    Ok((uplink, downlink, pre_skip, downlink_pre_skip))
}

impl SshMediaChannel {
    /// send_text（发送控制消息）：用于时钟测量及有限媒体控制指令。
    pub async fn send_text(&mut self, value: &str) -> Result<(), String> {
        self.websocket.send_frame(0x1, value.as_bytes()).await
    }

    /// send_binary（发送音频帧）：只允许协议帧界定内的原始字节。
    pub async fn send_binary(&mut self, value: &[u8]) -> Result<(), String> {
        if value.is_empty() || value.len() > MAX_MEDIA_MESSAGE_BYTES {
            return Err("SSH 媒体帧为空或超过 WebSocket 单消息上限。".into());
        }
        self.websocket.send_frame(0x2, value).await
    }

    /// send_pcm_frame（发送上行 PCM 帧）：校验固定流身份后使用设备协议二进制封装。
    pub async fn send_pcm_frame(
        &mut self,
        header: &crate::device_protocol::MediaHeader,
        pcm_le_bytes: &[u8],
    ) -> Result<(), String> {
        if header.audio_codec != AudioCodec::Pcm || self.uplink_codec != AudioCodec::Pcm {
            return Err("SSH 媒体通道上行编解码器不是 PCM。".into());
        }
        self.send_media_frame(header, pcm_le_bytes).await
    }

    /// send_media_frame（发送已协商编码帧）：音频模式必须匹配签名握手结果。
    pub async fn send_media_frame(
        &mut self,
        header: &crate::device_protocol::MediaHeader,
        payload: &[u8],
    ) -> Result<(), String> {
        if header.device_id != self.device_id
            || header.core_session_id != self.core_session_id
            || header.stream_id != self.stream_id
            || header.connection_generation != self.connection_generation
            || header.executor_generation != self.executor_generation
            || header.interaction_generation != self.interaction_generation
            || header.audio_codec != self.uplink_codec
            || header.sample_count == 0
            || header.sample_count > 320
        {
            return Err("待发送媒体帧与 SSH 媒体通道身份、代次、模式或采样数不匹配。".into());
        }
        let frame = crate::device_protocol::encode_frame(header, payload)?;
        self.send_binary(&frame).await
    }

    /// receive（读取服务端媒体消息）：自动回应 WebSocket Ping（保活探测）。
    pub async fn receive(&mut self) -> Result<SshMediaMessage, String> {
        self.websocket.receive_message().await
    }

    /// synchronize_clock（同步设备时钟）：首个可信样本后就绪，其余测量在运行期完成。
    pub async fn synchronize_clock<F, Fut>(&mut self, mut clock_now: F) -> Result<(), String>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<u64, String>>,
    {
        let mut samples = 0;
        let ready = loop {
            let message = match self.receive().await? {
                SshMediaMessage::Text(value) => {
                    serde_json::from_str::<serde_json::Value>(&value)
                        .map_err(|_| "主机时钟同步控制消息不是有效 JSON。".to_string())?
                }
                SshMediaMessage::Closed => return Err("主机在完成时钟同步前关闭媒体通道。".into()),
                SshMediaMessage::Binary(_) => {
                    return Err("主机在时钟同步期间发送了二进制音频。".into())
                }
                SshMediaMessage::Ping(_) | SshMediaMessage::Pong(_) => {
                    return Err("主机时钟同步期间出现未处理的 WebSocket 控制帧。".into());
                }
            };
            let object = message
                .as_object()
                .ok_or("主机时钟同步控制消息必须是 JSON 对象。")?;
            if object.get("type").and_then(serde_json::Value::as_str) == Some("media.ready")
                && samples > 0
            {
                break message;
            }
            if object.get("type").and_then(serde_json::Value::as_str)
                    != Some("clock.sync.request")
                || object
                    .get("protocol_version")
                    .and_then(serde_json::Value::as_u64)
                    != Some(u64::from(MEDIA_PROTOCOL_VERSION))
            {
                return Err("主机时钟同步控制字段或协议版本无效。".into());
            }
            let sync_id = object
                .get("sync_id")
                .and_then(serde_json::Value::as_str)
                .filter(|value| !value.is_empty() && value.len() <= 128)
                .ok_or("主机时钟同步编号无效。")?;
            if !object
                .get("fields")
                .is_some_and(serde_json::Value::is_object)
            {
                return Err("主机时钟同步字段说明无效。".into());
            }
            let device_receive_us = clock_now().await?;
            let device_send_us = clock_now().await?;
            if device_send_us < device_receive_us {
                return Err("设备时钟读取顺序倒退。".into());
            }
            let _ = sync_id;
            self.send_text(&media_clock_response(&message, device_receive_us, device_send_us)?).await?;
            samples += 1;
        };
        let object = ready
            .as_object()
            .ok_or("主机媒体就绪回执必须是 JSON 对象。")?;
        let (ready_uplink_codec, ready_downlink_codec, ready_pre_skip, ready_downlink_pre_skip) =
            media_ready_codecs(&ready)?;
        if object.get("type").and_then(serde_json::Value::as_str) != Some("media.ready")
            || object
                .get("protocol_version")
                .and_then(serde_json::Value::as_u64)
                != Some(u64::from(MEDIA_PROTOCOL_VERSION))
            || object.get("device_id").and_then(serde_json::Value::as_str)
                != Some(self.device_id.as_str())
            || object.get("stream_id").and_then(serde_json::Value::as_str)
                != Some(self.stream_id.as_str())
            || object
                .get("core_session_id")
                .and_then(serde_json::Value::as_str)
                != Some(self.core_session_id.as_str())
            || object
                .get("connection_generation")
                .and_then(serde_json::Value::as_u64)
                != Some(self.connection_generation)
            || object
                .get("executor_generation")
                .and_then(serde_json::Value::as_u64)
                != Some(self.executor_generation)
            || object
                .get("interaction_generation")
                .and_then(serde_json::Value::as_u64)
                != Some(self.interaction_generation)
            || object
                .get("sample_rate")
                .and_then(serde_json::Value::as_u64)
                != Some(16_000)
            || object.get("channels").and_then(serde_json::Value::as_u64) != Some(1)
            || object
                .get("bits_per_sample")
                .and_then(serde_json::Value::as_u64)
                != Some(16)
            || ready_uplink_codec != self.uplink_codec
            || ready_downlink_codec != self.downlink_codec
            || ready_pre_skip != self.encoder_pre_skip_samples
            || ready_downlink_pre_skip != self.downlink_encoder_pre_skip_samples
            || !object
                .get("fields")
                .is_some_and(serde_json::Value::is_object)
        {
            return Err("主机媒体就绪回执与已签名设备租约不匹配。".into());
        }
        self.clock_uncertainty_us = object
            .get("clock_uncertainty_us")
            .and_then(serde_json::Value::as_u64);
        if self.clock_uncertainty_us.is_none() {
            return Err("主机媒体回执缺少设备时钟误差。".into());
        }
        self.capture_active = object
            .get("capture_active")
            .and_then(serde_json::Value::as_bool)
            .ok_or("主机媒体回执缺少采集状态。")?;
        Ok(())
    }

    /// close（关闭媒体通道）：先结束 WebSocket，再断开独立 SSH 会话。
    pub async fn close(mut self) -> Result<(), String> {
        let _ = tokio::time::timeout(Duration::from_millis(250), self.websocket.send_close()).await;
        self._connection.close().await
    }

    /// into_parts（拆分全双工通道）：读写各自独占一半 SSH 流，允许并行送入采集帧。
    pub fn into_parts(self) -> (SshMediaReader, SshMediaWriter, SshMediaSessionGuard) {
        let Self {
            websocket,
            _connection,
            ..
        } = self;
        let (reader, writer) = websocket.into_parts();
        (
            reader,
            writer,
            SshMediaSessionGuard {
                connection: Some(_connection),
            },
        )
    }
}

/// media_clock_response（时钟响应）：初始测量与运行期续测共用格式，不经过音频工作进程排队。
pub fn media_clock_response(request: &serde_json::Value, device_receive_us: u64, device_send_us: u64) -> Result<String, String> {
    if request.get("type").and_then(serde_json::Value::as_str) != Some("clock.sync.request")
        || request.get("protocol_version").and_then(serde_json::Value::as_u64) != Some(u64::from(MEDIA_PROTOCOL_VERSION))
        || device_send_us < device_receive_us {
        return Err("媒体时钟请求版本或设备读数无效。".into());
    }
    let sync_id = request.get("sync_id").and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128).ok_or("媒体时钟测量编号无效。")?;
    let response = serde_json::json!({"type":"clock.sync.response","protocol_version":MEDIA_PROTOCOL_VERSION,
        "sync_id":sync_id,"device_receive_us":device_receive_us,"device_send_us":device_send_us});
    Ok(response.to_string())
}

/// SshMediaReader（SSH 媒体读取端）：单独消费服务端 WebSocket 帧。
pub struct SshMediaReader {
    stream: ReadHalf<Box<dyn AsyncDeviceStream>>, // stream（SSH WebSocket 只读半流）。
    closed: bool,                                 // closed（是否已读取对端关闭帧）。
}

impl SshMediaReader {
    /// receive（接收服务端消息）：Ping（保活探测）由调用方提交给配对写端回应。
    pub async fn receive(&mut self) -> Result<SshMediaMessage, String> {
        if self.closed {
            return Ok(SshMediaMessage::Closed);
        }
        let (opcode, payload) = read_server_ws_frame(&mut self.stream).await?;
        match opcode {
            0x1 => String::from_utf8(payload)
                .map(SshMediaMessage::Text)
                .map_err(|_| "SSH WebSocket 文本控制消息不是 UTF-8。".to_string()),
            0x2 => Ok(SshMediaMessage::Binary(payload)),
            0x8 => {
                self.closed = true;
                Ok(SshMediaMessage::Closed)
            }
            0x9 => Ok(SshMediaMessage::Ping(payload)),
            0xa => Ok(SshMediaMessage::Pong(payload)),
            _ => Err("SSH WebSocket 收到不支持的帧操作码。".into()),
        }
    }
}

/// SshMediaWriter（SSH 媒体写入端）：对设备上行帧进行客户端掩码编码。
pub struct SshMediaWriter {
    stream: WriteHalf<Box<dyn AsyncDeviceStream>>, // stream（SSH WebSocket 只写半流）。
    closed: bool,                                  // closed（是否已写入关闭帧）。
}

impl SshMediaWriter {
    /// send_text（发送控制消息）：用于时钟同步与有限控制指令。
    pub async fn send_text(&mut self, value: &str) -> Result<(), String> {
        if self.closed || value.len() > MAX_MEDIA_MESSAGE_BYTES {
            return Err("SSH WebSocket 控制消息状态或长度无效。".into());
        }
        write_client_ws_frame(&mut self.stream, 0x1, value.as_bytes()).await
    }

    /// send_binary（发送媒体负载）：按 RFC 6455 掩码后写入 SSH 通道。
    pub async fn send_binary(&mut self, value: &[u8]) -> Result<(), String> {
        if self.closed || value.is_empty() || value.len() > MAX_MEDIA_MESSAGE_BYTES {
            return Err("SSH 媒体帧为空或超过 WebSocket 单消息上限。".into());
        }
        write_client_ws_frame(&mut self.stream, 0x2, value).await
    }

    /// send_pong（回应保活探测）：回显主机 Ping 负载，最大 125 字节。
    pub async fn send_pong(&mut self, payload: &[u8]) -> Result<(), String> {
        if self.closed {
            return Err("SSH WebSocket 媒体写入端已关闭。".into());
        }
        write_client_ws_frame(&mut self.stream, 0xa, payload).await
    }

    /// send_close（关闭 WebSocket）：发送一次空负载关闭帧。
    pub async fn send_close(&mut self) -> Result<(), String> {
        if self.closed {
            return Ok(());
        }
        write_client_ws_frame(&mut self.stream, 0x8, &[]).await?;
        self.closed = true;
        Ok(())
    }
}

/// SshMediaSessionGuard（SSH 媒体会话守卫）：读写半流关闭后释放专用 SSH 会话。
pub struct SshMediaSessionGuard {
    connection: Option<SshDeviceConnection>, // connection（仅属于该媒体流的 SSH 连接）。
}

impl SshMediaSessionGuard {
    /// close（关闭 SSH 会话）：明确结束用于实时媒体的独立连接。
    pub async fn close(mut self) -> Result<(), String> {
        match self.connection.take() {
            Some(connection) => connection.close().await,
            None => Ok(()),
        }
    }
}

struct SshWebSocketStream {
    reader: ReadHalf<Box<dyn AsyncDeviceStream>>, // reader（SSH WebSocket 只读半流）。
    writer: WriteHalf<Box<dyn AsyncDeviceStream>>, // writer（SSH WebSocket 只写半流）。
    closed: bool,                                 // closed（是否已发送关闭帧或收到对端关闭）。
}

impl SshWebSocketStream {
    fn new(stream: Box<dyn AsyncDeviceStream>) -> Self {
        let (reader, writer) = io::split(stream);
        Self {
            reader,
            writer,
            closed: false,
        }
    }

    fn into_parts(self) -> (SshMediaReader, SshMediaWriter) {
        (
            SshMediaReader {
                stream: self.reader,
                closed: false,
            },
            SshMediaWriter {
                stream: self.writer,
                closed: false,
            },
        )
    }

    async fn send_frame(&mut self, opcode: u8, payload: &[u8]) -> Result<(), String> {
        if self.closed
            || !matches!(opcode, 0x1 | 0x2 | 0x8 | 0x9 | 0xa)
            || payload.len() > MAX_MEDIA_MESSAGE_BYTES
            || (opcode >= 0x8 && payload.len() > 125)
        {
            return Err("WebSocket 客户端帧状态或长度无效。".into());
        }
        write_client_ws_frame(&mut self.writer, opcode, payload).await
    }

    async fn receive_message(&mut self) -> Result<SshMediaMessage, String> {
        loop {
            if self.closed {
                return Ok(SshMediaMessage::Closed);
            }
            let (opcode, payload) = read_server_ws_frame(&mut self.reader).await?;
            match opcode {
                0x1 => {
                    return String::from_utf8(payload)
                        .map(SshMediaMessage::Text)
                        .map_err(|_| "SSH WebSocket 文本控制消息不是 UTF-8。".to_string())
                }
                0x2 => return Ok(SshMediaMessage::Binary(payload)),
                0x8 => {
                    self.closed = true;
                    return Ok(SshMediaMessage::Closed);
                }
                0x9 => self.send_frame(0xa, &payload).await?,
                0xa => continue,
                _ => return Err("SSH WebSocket 收到不支持的帧操作码。".into()),
            }
        }
    }

    async fn send_close(&mut self) -> Result<(), String> {
        if self.closed {
            return Ok(());
        }
        self.send_frame(0x8, &[]).await?;
        self.closed = true;
        Ok(())
    }
}

async fn write_client_ws_frame<W: AsyncWrite + Unpin>(
    stream: &mut W,
    opcode: u8,
    payload: &[u8],
) -> Result<(), String> {
    if !matches!(opcode, 0x1 | 0x2 | 0x8 | 0x9 | 0xa)
        || payload.len() > MAX_MEDIA_MESSAGE_BYTES
        || (opcode >= 0x8 && payload.len() > 125)
    {
        return Err("WebSocket 客户端帧操作码或长度无效。".into());
    }
    let mask = rand::random::<[u8; 4]>();
    let mut header = Vec::with_capacity(14);
    header.push(0x80 | opcode); // FIN=1（单片帧），保留位为 0。
    match payload.len() {
        length @ 0..=125 => header.push(0x80 | length as u8),
        length @ 126..=65_535 => {
            header.push(0x80 | 126);
            header.extend_from_slice(&(length as u16).to_be_bytes());
        }
        length => {
            header.push(0x80 | 127);
            header.extend_from_slice(&(length as u64).to_be_bytes());
        }
    }
    header.extend_from_slice(&mask);
    stream
        .write_all(&header)
        .await
        .map_err(|_| "SSH WebSocket 帧头写入失败。".to_string())?;
    let mut masked = Vec::with_capacity(payload.len());
    masked.extend(
        payload
            .iter()
            .enumerate()
            .map(|(index, byte)| *byte ^ mask[index & 3]),
    );
    stream
        .write_all(&masked)
        .await
        .map_err(|_| "SSH WebSocket 帧负载写入失败。".to_string())?;
    stream
        .flush()
        .await
        .map_err(|_| "SSH WebSocket 帧刷新失败。".to_string())
}

async fn read_server_ws_frame<R: AsyncRead + Unpin>(
    stream: &mut R,
) -> Result<(u8, Vec<u8>), String> {
    let mut prefix = [0_u8; 2];
    stream
        .read_exact(&mut prefix)
        .await
        .map_err(|_| "SSH WebSocket 对端已断开。".to_string())?;
    let fin = prefix[0] & 0x80 != 0;
    let opcode = prefix[0] & 0x0f;
    if !fin
        || prefix[0] & 0x70 != 0
        || prefix[1] & 0x80 != 0
        || !matches!(opcode, 0x1 | 0x2 | 0x8 | 0x9 | 0xa)
    {
        return Err("SSH WebSocket 服务端帧标志或操作码无效。".into());
    }
    let indicator = prefix[1] & 0x7f;
    let length = match indicator {
        value @ 0..=125 => value as u64,
        126 => {
            let value = stream
                .read_u16()
                .await
                .map_err(|_| "SSH WebSocket 长度字段截断。".to_string())?
                as u64;
            if value < 126 {
                return Err("SSH WebSocket 长度编码不是最短形式。".into());
            }
            value
        }
        127 => {
            let value = stream
                .read_u64()
                .await
                .map_err(|_| "SSH WebSocket 长度字段截断。".to_string())?;
            if value <= 65_535 || value >> 63 != 0 {
                return Err("SSH WebSocket 64 位长度编码无效。".into());
            }
            value
        }
        _ => unreachable!(),
    };
    if length > MAX_MEDIA_MESSAGE_BYTES as u64 || (opcode >= 0x8 && length > 125) {
        return Err("SSH WebSocket 服务端帧超过长度上限。".into());
    }
    let mut payload = vec![0_u8; length as usize];
    stream
        .read_exact(&mut payload)
        .await
        .map_err(|_| "SSH WebSocket 帧负载截断。".to_string())?;
    Ok((opcode, payload))
}

fn websocket_accept_value(key: &str) -> String {
    let material = format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
    let digest = <Sha1 as Sha1Digest>::digest(material.as_bytes());
    base64::engine::general_purpose::STANDARD.encode(digest)
}

async fn read_http_header_bounded(
    stream: &mut dyn AsyncDeviceStream,
    limit: usize,
    error: &'static str,
) -> Result<Vec<u8>, String> {
    let mut response = Vec::with_capacity(1024);
    loop {
        if response.len() >= limit {
            return Err(error.into());
        }
        let byte = stream.read_u8().await.map_err(|_| error.to_string())?;
        response.push(byte);
        if response.ends_with(b"\r\n\r\n") {
            return Ok(response);
        }
    }
}

fn local_voice_upgrade_key(header: &[u8], token: &str) -> Result<String, String> {
    if token.len() != 64
        || !token
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("本机语音 WebSocket 转发票据无效。".into());
    }
    let text = std::str::from_utf8(header)
        .map_err(|_| "本机语音 WebSocket 握手头不是有效 UTF-8。".to_string())?;
    let mut lines = text.split("\r\n");
    let request = lines.next().ok_or("本机语音 WebSocket 握手缺少请求行。")?;
    if request != format!("GET /{token}/ws HTTP/1.1") {
        return Err("本机语音 WebSocket 只允许当前随机票据对应的固定路径。".into());
    }
    let mut upgrade = false;
    let mut connection_upgrade = false;
    let mut websocket_key = None;
    let mut websocket_version = None;
    let mut seen = std::collections::HashSet::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        let (name, value) = line
            .split_once(':')
            .ok_or("本机语音 WebSocket 握手头字段格式无效。")?;
        if name.is_empty()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            || value
                .bytes()
                .any(|byte| byte == b'\r' || byte == b'\n' || byte == 0)
        {
            return Err("本机语音 WebSocket 握手头包含无效字段。".into());
        }
        let lower = name.to_ascii_lowercase();
        let value = value.trim();
        if !seen.insert(lower.clone()) {
            return Err("本机语音 WebSocket 握手头不接受重复字段。".into());
        }
        match lower.as_str() {
            "upgrade" => upgrade = value.eq_ignore_ascii_case("websocket"),
            "connection" => {
                connection_upgrade = value
                    .split(',')
                    .any(|item| item.trim().eq_ignore_ascii_case("upgrade"))
            }
            "sec-websocket-key" => websocket_key = Some(value.to_owned()),
            "sec-websocket-version" => websocket_version = Some(value.to_owned()),
            "content-length" | "transfer-encoding" => {
                return Err("本机语音 WebSocket 握手不能带请求正文。".into())
            }
            _ => {}
        }
    }
    if !upgrade || !connection_upgrade || websocket_version.as_deref() != Some("13") {
        return Err("本机语音 WebSocket 握手升级字段无效。".into());
    }
    let key = websocket_key.ok_or("本机语音 WebSocket 握手缺少客户端随机键。")?;
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(&key)
        .map_err(|_| "本机语音 WebSocket 客户端随机键无效。".to_string())?;
    if decoded.len() != 16 || base64::engine::general_purpose::STANDARD.encode(&decoded) != key {
        return Err("本机语音 WebSocket 客户端随机键长度或编码无效。".into());
    }
    Ok(key)
}

fn validate_voice_upgrade_response(response: &[u8], key: &str) -> Result<(), String> {
    let text = std::str::from_utf8(response)
        .map_err(|_| "服务端语音 WebSocket 响应头不是有效 HTTP。".to_string())?;
    let mut lines = text.split("\r\n");
    let status = lines
        .next()
        .ok_or("服务端语音 WebSocket 响应缺少状态行。")?;
    if status.split_ascii_whitespace().take(2).collect::<Vec<_>>() != ["HTTP/1.1", "101"] {
        return Err("服务端没有接受语音 WebSocket 升级。".into());
    }
    let expected_accept = websocket_accept_value(key);
    let mut accept = None;
    let mut upgrade = false;
    for line in lines {
        if line.is_empty() {
            continue;
        }
        let (name, value) = line
            .split_once(':')
            .ok_or("服务端语音 WebSocket 响应头字段格式无效。")?;
        match name.trim().to_ascii_lowercase().as_str() {
            "sec-websocket-accept" => {
                if accept.is_some() {
                    return Err("服务端语音 WebSocket 重复返回随机键校验值。".into());
                }
                accept = Some(value.trim().to_owned());
            }
            "upgrade" => upgrade = value.trim().eq_ignore_ascii_case("websocket"),
            _ => {}
        }
    }
    if !upgrade || accept.as_deref() != Some(expected_accept.as_str()) {
        return Err("服务端语音 WebSocket 随机键校验失败。".into());
    }
    Ok(())
}

fn encode_websocket_upgrade_request(
    path_and_query: &str,
    auth: &DeviceGatewayAuthHeaders,
    key: &str,
    gateway_port: u16,
) -> Result<Vec<u8>, String> {
    validate_gateway_auth_headers(auth)?;
    if path_and_query.len() > 2048
        || !(path_and_query.starts_with("/v1/media?") || path_and_query.starts_with("/v1/voice?"))
        || !path_and_query.bytes().all(|byte| {
            byte.is_ascii_alphanumeric()
                || matches!(byte, b'/' | b'_' | b'-' | b'.' | b':' | b'?' | b'&' | b'=')
        })
    {
        return Err("SSH WebSocket 媒体路径或查询字段无效。".into());
    }
    let request = format!(
        concat!(
            "GET {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n",
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {}\r\n",
            "Sec-WebSocket-Version: 13\r\nX-Voice-Terminal-Device-Id: {}\r\n",
            "X-Voice-Terminal-Core-Session-Id: {}\r\nX-Voice-Terminal-Challenge: {}\r\n",
            "X-Voice-Terminal-Timestamp-Ms: {}\r\nX-Voice-Terminal-Signature: {}\r\n\r\n",
        ),
        path_and_query,
        gateway_port,
        key,
        auth.device_id,
        auth.core_session_id,
        auth.challenge,
        auth.timestamp_ms,
        auth.signature,
    );
    if request.len() > MAX_HEADER_BYTES {
        return Err("SSH WebSocket 握手头超过长度上限。".into());
    }
    Ok(request.into_bytes())
}

async fn read_websocket_upgrade_response(
    stream: &mut dyn AsyncDeviceStream,
    key: &str,
) -> Result<(), String> {
    let mut response = Vec::with_capacity(1024);
    loop {
        if response.len() >= MAX_HEADER_BYTES {
            return Err("SSH WebSocket 握手响应头超过长度上限。".into());
        }
        let byte = stream
            .read_u8()
            .await
            .map_err(|_| "SSH WebSocket 握手响应读取失败。".to_string())?;
        response.push(byte);
        if response.ends_with(b"\r\n\r\n") {
            break;
        }
    }
    let text = std::str::from_utf8(&response)
        .map_err(|_| "SSH WebSocket 握手响应不是 ASCII HTTP。".to_string())?;
    let mut lines = text.split("\r\n");
    let status = lines.next().ok_or("SSH WebSocket 握手响应缺少状态行。")?;
    let mut status_parts = status.split_ascii_whitespace();
    if status_parts.next() != Some("HTTP/1.1") || status_parts.next() != Some("101") {
        return Err("主机未接受 SSH WebSocket 媒体升级。".into());
    }
    let mut accept = None;
    let mut upgrade = false;
    let mut connection_upgrade = false;
    for line in lines {
        if line.is_empty() {
            break;
        }
        let Some((name, value)) = line.split_once(':') else {
            return Err("SSH WebSocket 握手响应头格式无效。".into());
        };
        let name = name.trim().to_ascii_lowercase();
        let value = value.trim();
        match name.as_str() {
            "sec-websocket-accept" => {
                if accept.replace(value.to_string()).is_some() {
                    return Err("SSH WebSocket 握手响应包含重复 accept 字段。".into());
                }
            }
            "upgrade" => upgrade = value.eq_ignore_ascii_case("websocket"),
            "connection" => {
                connection_upgrade = value
                    .split(',')
                    .any(|token| token.trim().eq_ignore_ascii_case("upgrade"));
            }
            _ => {}
        }
    }
    if accept.as_deref() != Some(websocket_accept_value(key).as_str())
        || !upgrade
        || !connection_upgrade
    {
        return Err("SSH WebSocket 握手响应校验失败。".into());
    }
    Ok(())
}

/// device_credential_reference（设备凭据引用）：稳定 ID 只以摘要进入凭据名。
pub fn device_credential_reference(device_id: &str) -> Result<String, String> {
    crate::device_config::credential_reference_for_device(device_id)
}

/// create_device_identity（生成设备 SSH 身份）：使用操作系统熵源生成 Ed25519 密钥。
pub fn create_device_identity(device_id: &str) -> Result<SshDeviceIdentity, String> {
    create_device_identity_in(None, device_id)
}

/// create_device_identity_in（项目数据目录内的设备身份操作；None 只用于旧调用兼容）。
pub fn create_device_identity_in(
    data: Option<&Path>,
    device_id: &str,
) -> Result<SshDeviceIdentity, String> {
    let credential_ref = device_credential_reference(device_id)?;
    let mut rng = rand::rng();
    let private_key = PrivateKey::random(&mut rng, Algorithm::Ed25519)
        .map_err(|_| "系统随机数服务无法生成设备 SSH 密钥。".to_string())?;
    let private_openssh = private_key
        .to_openssh(LineEnding::LF)
        .map_err(|_| "设备 SSH 私钥编码失败。".to_string())?;
    let public_key = private_key
        .public_key()
        .to_openssh()
        .map_err(|_| "设备 SSH 公钥编码失败。".to_string())?;
    let fingerprint = private_key
        .public_key()
        .fingerprint(HashAlg::Sha256)
        .to_string();
    crate::credentials::store_device_ssh_key_at(data, &credential_ref, private_openssh.as_str())?;
    Ok(SshDeviceIdentity {
        credential_ref,
        public_key,
        fingerprint,
    })
}

/// device_identity_status（读取本机 SSH 公钥）：不返回私钥，不修改凭据库。
pub fn device_identity_status(device_id: &str) -> Result<Option<SshDeviceIdentity>, String> {
    device_identity_status_in(None, device_id)
}

/// device_identity_status_in（项目数据目录内的设备身份操作；None 只用于旧调用兼容）。
pub fn device_identity_status_in(
    data: Option<&Path>,
    device_id: &str,
) -> Result<Option<SshDeviceIdentity>, String> {
    let credential_ref = device_credential_reference(device_id)?;
    if !crate::credentials::has_device_ssh_key_at(data, &credential_ref)? {
        return Ok(None);
    }
    let private_key = crate::credentials::load_device_ssh_key_at(data, &credential_ref)?;
    let private_key = PrivateKey::from_openssh(private_key.as_str())
        .map_err(|_| "本机存储中的设备 SSH 私钥无法读取。".to_string())?;
    let public_key = private_key
        .public_key()
        .to_openssh()
        .map_err(|_| "设备 SSH 公钥编码失败。".to_string())?;
    let fingerprint = private_key
        .public_key()
        .fingerprint(HashAlg::Sha256)
        .to_string();
    Ok(Some(SshDeviceIdentity {
        credential_ref,
        public_key,
        fingerprint,
    }))
}

/// sign_device_pairing_challenge（签署 OPAQUE 配对挑战）：仅由原生层读取本机设备密钥。
pub fn sign_device_pairing_challenge(device_id: &str, message: &[u8]) -> Result<String, String> {
    sign_device_pairing_challenge_in(None, device_id, message)
}

/// sign_device_pairing_challenge_in（项目数据目录内的设备身份操作；None 只用于旧调用兼容）。
pub fn sign_device_pairing_challenge_in(
    data: Option<&Path>,
    device_id: &str,
    message: &[u8],
) -> Result<String, String> {
    let credential_ref = device_credential_reference(device_id)?;
    let private_key_text = crate::credentials::load_device_ssh_key_at(data, &credential_ref)?;
    let private_key = PrivateKey::from_openssh(private_key_text.as_str())
        .map_err(|_| "本机设备 SSH 私钥无法读取。".to_string())?;
    if private_key.algorithm() != Algorithm::Ed25519 || private_key.is_encrypted() {
        return Err("本机配对身份必须是未加密的 Ed25519 设备密钥。".into());
    }
    let keypair = private_key
        .key_data()
        .ed25519()
        .ok_or_else(|| "本机配对身份不是 Ed25519 密钥。".to_string())?;
    let signing_key = SigningKey::from_bytes(keypair.private.as_ref());
    Ok(base64::engine::general_purpose::STANDARD.encode(signing_key.sign(message).to_bytes()))
}

/// delete_device_identity（撤销本机 SSH 身份）：只移除对应凭据库条目。
pub fn delete_device_identity(device_id: &str) -> Result<(), String> {
    delete_device_identity_in(None, device_id)
}

/// delete_device_identity_in（项目数据目录内的设备身份操作；None 只用于旧调用兼容）。
pub fn delete_device_identity_in(data: Option<&Path>, device_id: &str) -> Result<(), String> {
    let reference = device_credential_reference(device_id)?;
    crate::credentials::delete_device_ssh_key_at(data, &reference)
}

fn parse_transport_private_key(private_key_openssh: &str) -> Result<PrivateKey, String> {
    let private_key = PrivateKey::from_openssh(private_key_openssh)
        .map_err(|_| "SSH 登录密钥无法解析；只接受未加密的 OpenSSH Ed25519 私钥。".to_string())?;
    if private_key.algorithm() != Algorithm::Ed25519 || private_key.is_encrypted() {
        return Err("SSH 登录密钥必须是未加密的 Ed25519 OpenSSH 私钥。".into());
    }
    Ok(private_key)
}

fn transport_credential_reference(fingerprint: &str) -> Result<String, String> {
    let encoded = fingerprint
        .strip_prefix("SHA256:")
        .ok_or_else(|| "SSH 登录密钥指纹格式无效。".to_string())?;
    let digest = base64::engine::general_purpose::STANDARD_NO_PAD
        .decode(encoded)
        .map_err(|_| "SSH 登录密钥指纹格式无效。".to_string())?;
    if digest.len() != 32 {
        return Err("SSH 登录密钥指纹长度无效。".into());
    }
    let suffix = digest
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("VoiceTerminal/ssh-transport/{suffix}"))
}

#[cfg(windows)]
fn read_transport_key_file(path: &Path) -> Result<Zeroizing<Vec<u8>>, String> {
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    const MAX_PRIVATE_KEY_BYTES: u64 = 64 * 1024; // MAX_PRIVATE_KEY_BYTES（登录私钥文件上限，字节）。
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400; // FILE_ATTRIBUTE_REPARSE_POINT（Windows 链接属性）。
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000; // FILE_FLAG_OPEN_REPARSE_POINT（打开链接本身而非跟随）。

    if !path.is_absolute()
        || path
            .components()
            .any(|part| matches!(part, std::path::Component::ParentDir))
    {
        return Err("SSH 登录密钥路径必须是无父目录跳转的绝对路径。".into());
    }
    let absolute = path.to_path_buf();
    for component in absolute.ancestors() {
        let metadata = fs::symlink_metadata(component)
            .map_err(|_| "SSH 登录密钥路径不存在或不可读取。".to_string())?;
        if crate::workspace::metadata_reparse(&metadata) {
            return Err("SSH 登录密钥路径包含符号链接或重解析点。".into());
        }
    }

    let metadata = fs::symlink_metadata(&absolute)
        .map_err(|_| "SSH 登录密钥文件不存在或不可读取。".to_string())?;
    if crate::workspace::metadata_reparse(&metadata)
        || !metadata.is_file()
        || metadata.len() == 0
        || metadata.len() > MAX_PRIVATE_KEY_BYTES
    {
        return Err("SSH 登录密钥必须是 1 至 65536 字节的普通文件，不能是链接。".into());
    }
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&absolute)
        .map_err(|_| "无法打开 SSH 登录密钥文件。".to_string())?;
    let opened = file
        .metadata()
        .map_err(|_| "无法检查 SSH 登录密钥文件。".to_string())?;
    if !opened.is_file()
        || opened.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || opened.len() == 0
        || opened.len() > MAX_PRIVATE_KEY_BYTES
    {
        return Err("SSH 登录密钥打开后类型、链接或大小无效。".into());
    }
    let mut bytes = Zeroizing::new(Vec::with_capacity(opened.len() as usize));
    file.take(MAX_PRIVATE_KEY_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "读取 SSH 登录密钥文件失败。".to_string())?;
    if bytes.is_empty() || bytes.len() as u64 > MAX_PRIVATE_KEY_BYTES {
        return Err("SSH 登录密钥文件为空或超过 65536 字节。".into());
    }
    Ok(bytes)
}

/// import_transport_identity（导入 SSH 登录身份）：私钥只在原生层读取并保存到当前用户凭据库。
pub fn import_transport_identity(path: &Path) -> Result<SshTransportIdentity, String> {
    import_transport_identity_in(None, path)
}

/// import_transport_identity_in（项目数据目录内的设备身份操作；None 只用于旧调用兼容）。
pub fn import_transport_identity_in(
    data: Option<&Path>,
    path: &Path,
) -> Result<SshTransportIdentity, String> {
    #[cfg(windows)]
    {
        let bytes = read_transport_key_file(path)?;
        let text = std::str::from_utf8(&bytes)
            .map_err(|_| "SSH 登录密钥必须是 UTF-8 编码的 OpenSSH 私钥文件。".to_string())?;
        let private_key = parse_transport_private_key(text)?;
        let public_key = private_key
            .public_key()
            .to_openssh()
            .map_err(|_| "SSH 登录公钥编码失败。".to_string())?;
        let fingerprint = private_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let credential_ref = transport_credential_reference(&fingerprint)?;
        let private_key_openssh = Zeroizing::new(
            private_key
                .to_openssh(LineEnding::LF)
                .map_err(|_| "SSH 登录私钥规范化失败。".to_string())?,
        );
        crate::credentials::store_device_ssh_key_at(
            data,
            &credential_ref,
            private_key_openssh.as_str(),
        )?;
        Ok(SshTransportIdentity {
            credential_ref,
            public_key,
            fingerprint,
        })
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        Err("SSH 登录密钥导入仅支持 Windows 客户端。".into())
    }
}

/// transport_identity_status（读取 SSH 登录公钥）：只回传公钥和指纹，不返回私钥。
pub fn transport_identity_status(
    credential_ref: &str,
) -> Result<Option<SshTransportIdentity>, String> {
    transport_identity_status_in(None, credential_ref)
}

/// transport_identity_status_in（项目数据目录内的设备身份操作；None 只用于旧调用兼容）。
pub fn transport_identity_status_in(
    data: Option<&Path>,
    credential_ref: &str,
) -> Result<Option<SshTransportIdentity>, String> {
    if !credential_ref.starts_with("VoiceTerminal/ssh-transport/") {
        return Err("SSH 登录密钥凭据引用无效。".into());
    }
    if !crate::credentials::has_device_ssh_key_at(data, credential_ref)? {
        return Ok(None);
    }
    let private_key_text = crate::credentials::load_device_ssh_key_at(data, credential_ref)?;
    let private_key = parse_transport_private_key(private_key_text.as_str())?;
    let fingerprint = private_key
        .public_key()
        .fingerprint(HashAlg::Sha256)
        .to_string();
    if transport_credential_reference(&fingerprint)? != credential_ref {
        return Err("SSH 登录凭据与其确定性引用不匹配。".into());
    }
    let public_key = private_key
        .public_key()
        .to_openssh()
        .map_err(|_| "SSH 登录公钥编码失败。".to_string())?;
    Ok(Some(SshTransportIdentity {
        credential_ref: credential_ref.to_string(),
        public_key,
        fingerprint,
    }))
}

/// delete_transport_identity（删除 SSH 登录密钥）：仅删除指定本机凭据条目。
pub fn delete_transport_identity(credential_ref: &str) -> Result<(), String> {
    delete_transport_identity_in(None, credential_ref)
}

/// delete_transport_identity_in（项目数据目录内的设备身份操作；None 只用于旧调用兼容）。
pub fn delete_transport_identity_in(
    data: Option<&Path>,
    credential_ref: &str,
) -> Result<(), String> {
    let suffix = credential_ref.strip_prefix("VoiceTerminal/ssh-transport/");
    if !suffix.is_some_and(|suffix| {
        suffix.len() == 64
            && suffix
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }) {
        return Err("SSH 登录密钥凭据引用无效。".into());
    }
    crate::credentials::delete_device_ssh_key_at(data, credential_ref)
}

#[derive(Clone)]
struct LoopbackHostKeyHandler {
    observed: Arc<Mutex<Option<russh::keys::PublicKey>>>, // observed（本机运行服务在握手中证明持有的公钥）。
}

impl client::Handler for LoopbackHostKeyHandler {
    type Error = russh::Error;
    async fn check_server_key(
        &mut self,
        key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        if key.public_key().algorithm() != Algorithm::Ed25519 {
            return Ok(false);
        }
        if let Ok(mut observed) = self.observed.lock() {
            *observed = Some(key.public_key().clone());
            Ok(true)
        } else {
            Ok(false)
        }
    }
}

/// probe_loopback_host_public_key（运行中服务公钥）：仅本机回环，固定 Ed25519，不认证或执行命令。
pub async fn probe_loopback_host_public_key(
    port: u16,
    timeout: Duration,
) -> Result<russh::keys::PublicKey, String> {
    if port == 0 || timeout.is_zero() || timeout > Duration::from_secs(10) {
        return Err("本机 SSH 主机身份探测端口或时限无效。".into());
    }
    let observed = Arc::new(Mutex::new(None));
    let handler = LoopbackHostKeyHandler {
        observed: observed.clone(),
    };
    let mut config = client::Config::default();
    config.preferred.key = std::borrow::Cow::Owned(vec![Algorithm::Ed25519]);
    config.preferred.host_key_certificates = std::borrow::Cow::Borrowed(&[]);
    let handle = tokio::time::timeout(
        timeout,
        client::connect(Arc::new(config), ("127.0.0.1", port), handler),
    )
    .await
    .map_err(|_| "读取本机 SSH 主机公钥超时。".to_string())?
    .map_err(|_| {
        "无法从运行中的本机 SSH 服务取得 Ed25519 主机公钥；请检查服务实际监听和算法配置。"
            .to_string()
    })?;
    // disconnect（协议断开）有界等待，密钥交换结束后不进入账户认证。
    let _ = tokio::time::timeout(
        Duration::from_millis(200),
        handle.disconnect(Disconnect::ByApplication, "", ""),
    )
    .await;
    let key = observed
        .lock()
        .map_err(|_| "本机 SSH 主机公钥状态不可用。".to_string())?
        .clone();
    key.ok_or_else(|| "本机 SSH 服务没有提供受支持的主机公钥。".to_string())
}

#[derive(Clone)]
struct ProbeHostKeyHandler {
    observed: Arc<Mutex<Option<String>>>, // observed（未确认主机密钥候选）。
}

impl client::Handler for ProbeHostKeyHandler {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        let fingerprint = server_public_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        if let Ok(mut observed) = self.observed.lock() {
            *observed = Some(fingerprint);
            Ok(true)
        } else {
            Ok(false)
        }
    }
}

/// probe_host_key（读取候选主机指纹）：不认证、不执行命令、不自动建立信任。
pub async fn probe_host_key(address: &str, port: u16, timeout: Duration) -> Result<String, String> {
    let address_ok = !address.is_empty()
        && address.len() <= 255
        && address.trim() == address
        && address.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_' | b':' | b'[' | b']')
        });
    if !address_ok || port == 0 || timeout.is_zero() || timeout > Duration::from_secs(60) {
        return Err("SSH 主机探测地址、端口或超时无效。".into());
    }
    let observed = Arc::new(Mutex::new(None));
    let handler = ProbeHostKeyHandler {
        observed: observed.clone(),
    };
    let config = Arc::new(client::Config::default());
    let handle = tokio::time::timeout(timeout, client::connect(config, (address, port), handler))
        .await
        .map_err(|_| "SSH 主机指纹探测超时。".to_string())?
        .map_err(|_| "无法读取 SSH 主机候选指纹。".to_string())?;
    let fingerprint = observed
        .lock()
        .map_err(|_| "SSH 主机指纹暂存不可用。".to_string())?
        .clone()
        .ok_or_else(|| "SSH 握手未提供主机指纹。".to_string())?;
    handle
        .disconnect(Disconnect::ByApplication, "host key probe complete", "")
        .await
        .map_err(|_| "SSH 主机指纹探测会话关闭失败。".to_string())?;
    Ok(fingerprint)
}

#[derive(Clone)]
struct PinnedHostHandler {
    expected_fingerprint: String, // expected_fingerprint（已确认的主机指纹）。
}

impl client::Handler for PinnedHostHandler {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        Ok(host_fingerprint_matches(
            server_public_key,
            &self.expected_fingerprint,
        ))
    }
}

fn host_fingerprint_matches(server_public_key: &PublicKeyOrCertificate, expected: &str) -> bool {
    server_public_key
        .public_key()
        .fingerprint(HashAlg::Sha256)
        .to_string()
        == expected
}

fn device_ssh_client_config() -> client::Config {
    client::Config {
        keepalive_interval: Some(Duration::from_secs(15)),
        keepalive_max: 3,
        ..Default::default()
    }
}

/// SshDeviceConnection（SSH 设备连接）：持有一个固定主机的已认证 SSH 会话。
pub struct SshDeviceConnection {
    handle: client::Handle<PinnedHostHandler>, // handle（已验证 SSH 会话）。
    target: SshDeviceTarget,                   // target（不可在会话中途替换的主机身份）。
    management_open: Option<(RemoteManagementOpenRequest, std::time::Instant)>, // management_open（本条 SSH 连接复用的管理租约申请及本机到期时间）。
    management_reuse_allowed: bool, // management_reuse_allowed（服务端明确声明连续请求逐次核验信任后才复用通道）。
    management_channel:
        tokio::sync::Mutex<Option<(Box<dyn AsyncDeviceStream>, RemoteManagementOpenResponse)>>, // management_channel（同一租约下持续复用的独立管理通道）。
}

impl SshDeviceConnection {
    /// connect（连接）：仅允许已固定主机和 Windows 凭据库返回的密钥。
    pub async fn connect(
        target: SshDeviceTarget,
        private_key_openssh: &str,
        timeout: Duration,
    ) -> Result<Self, String> {
        target.validate()?;
        if timeout.is_zero() || timeout > Duration::from_secs(60) {
            return Err("SSH 连接超时必须在 1 至 60 秒之间。".into());
        }
        if private_key_openssh.is_empty() || private_key_openssh.len() > 64 * 1024 {
            return Err("本机存储中的 SSH 私钥为空或超过大小上限。".into());
        }
        let private_key = PrivateKey::from_openssh(private_key_openssh)
            .map_err(|_| "本机存储中的 SSH 私钥格式无效或需要口令。".to_string())?;
        if private_key.is_encrypted() {
            return Err("SSH 私钥须在写入本机加密存储前完成本机解锁。".into());
        }

        let handler = PinnedHostHandler {
            expected_fingerprint: target.host_key_sha256.clone(),
        };
        let config = Arc::new(device_ssh_client_config());
        let mut handle = tokio::time::timeout(
            timeout,
            client::connect(config, (target.address.as_str(), target.port), handler),
        )
        .await
        .map_err(|_| "SSH 连接或主机指纹验证超时。".to_string())?
        .map_err(|_| "SSH 连接失败或主机指纹不匹配。".to_string())?;

        let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
        let auth = tokio::time::timeout(
            timeout,
            handle.authenticate_publickey(target.username.clone(), key),
        )
        .await
        .map_err(|_| "SSH 公钥认证超时。".to_string())?
        .map_err(|_| "SSH 公钥认证失败。".to_string())?;
        if !matches!(auth, AuthResult::Success) {
            let _ = handle
                .disconnect(
                    Disconnect::ByApplication,
                    "device authentication failed",
                    "",
                )
                .await;
            return Err("SSH 公钥未获主机接受。".into());
        }
        Ok(Self {
            handle,
            target,
            management_open: None,
            management_reuse_allowed: false,
            management_channel: tokio::sync::Mutex::new(None),
        })
    }

    /// is_closed（检查认证 SSH 会话是否已被传输层关闭）：复用 russh（Rust SSH 库）连接代次事实。
    pub fn is_closed(&self) -> bool {
        self.handle.is_closed()
    }

    /// open_fixed_bridge（打开固定桥接）：只允许产品内置的控制、媒体、管理或文件标记。
    async fn open_fixed_bridge(
        &self,
        marker: &'static str,
    ) -> Result<Box<dyn AsyncDeviceStream>, String> {
        if !matches!(
            marker,
            CONTROL_BRIDGE_MARKER
                | MEDIA_BRIDGE_MARKER
                | MANAGEMENT_BRIDGE_MARKER
                | FILE_BRIDGE_MARKER
        ) {
            return Err("SSH 桥接标记不在固定白名单中。".into());
        }
        let channel = self
            .handle
            .channel_open_session()
            .await
            .map_err(|_| "无法打开受限 SSH 桥接会话。".to_string())?;
        channel
            .exec(true, marker.as_bytes())
            .await
            .map_err(|_| "受限 SSH 桥接命令被主机拒绝。".to_string())?;
        Ok(Box::new(channel.into_stream()))
    }

    /// post_json（提交控制请求）：经固定 SSH exec（远程执行）桥接进入主机回环网关。
    pub async fn post_json(
        &self,
        path: &str,
        body: &serde_json::Value,
        timeout: Duration,
    ) -> Result<GatewayResponse, String> {
        self.post_json_with_auth(path, body, None, timeout).await
    }

    async fn post_json_with_auth(
        &self,
        path: &str,
        body: &serde_json::Value,
        auth: Option<&DeviceGatewayAuthHeaders>,
        timeout: Duration,
    ) -> Result<GatewayResponse, String> {
        let request = encode_http_request(path, body, auth)?;
        if timeout.is_zero() || timeout > Duration::from_secs(600) {
            return Err("网关请求超时必须在 1 至 600 秒之间。".into());
        }
        let operation = async {
            let mut stream = self.open_fixed_bridge(CONTROL_BRIDGE_MARKER).await?;
            stream
                .write_all(&request)
                .await
                .map_err(|_| "SSH 业务请求写入失败。".to_string())?;
            // HTTP（超文本传输协议）请求带 Content-Length；客户端不提前发送 SSH EOF，
            // 以免关闭桥接双向流。收到完整响应后由本机网关关闭连接。
            let mut response = Vec::with_capacity(4096);
            let mut chunk = [0_u8; 8192];
            loop {
                let read = stream
                    .read(&mut chunk)
                    .await
                    .map_err(|_| "SSH 业务回执读取失败。".to_string())?;
                if read == 0 {
                    break;
                }
                let limit = MAX_RESPONSE_BYTES + MAX_HEADER_BYTES + 4;
                if response.len().saturating_add(read) > limit {
                    return Err("SSH 业务回执超过大小上限。".into());
                }
                response.extend_from_slice(&chunk[..read]);
            }
            parse_http_response(&response)
        };
        tokio::time::timeout(timeout, operation)
            .await
            .map_err(|_| "SSH 业务请求等待回执超时。".to_string())?
    }

    /// management_exchange（独立管理请求）：一条固定 SSH 会话交换管理租约与单个请求行。
    async fn management_exchange(
        &mut self,
        proposed_open: &RemoteManagementOpenRequest,
        request: &RemoteManagementRequest,
        timeout: Duration,
    ) -> Result<RemoteManagementResponse, String> {
        request.validate()?;
        if timeout.is_zero() || timeout > Duration::from_secs(600) {
            return Err("独立管理请求超时必须在 1 至 600 秒之间。".into());
        }
        let open_request = match &self.management_open {
            Some((request, deadline))
                if *deadline > std::time::Instant::now()
                    && request.target_host_id == proposed_open.target_host_id
                    && request.client_device_id == proposed_open.client_device_id =>
            {
                request.clone()
            }
            _ => proposed_open.clone(),
        };
        let cached = self
            .management_channel
            .get_mut()
            .take()
            .filter(|(_, opened)| opened.request_id == open_request.request_id);
        let operation = async {
            let (mut stream, opened) = if let Some(cached) = cached {
                cached
            } else {
                let mut stream = self.open_fixed_bridge(MANAGEMENT_BRIDGE_MARKER).await?;
                write_management_line(&mut *stream, &open_request, MAX_REQUEST_BYTES).await?;
                let opened: RemoteManagementOpenResponse =
                    read_management_line(&mut *stream, MAX_RESPONSE_BYTES).await?;
                if opened.protocol_version != DEVICE_MANAGEMENT_PROTOCOL_VERSION
                    || opened.request_id != open_request.request_id
                    || opened.target_host_id != open_request.target_host_id
                    || opened.client_device_id != open_request.client_device_id
                    || opened.session_generation == 0
                    || opened.expires_at_ms <= unix_timestamp_ms()?
                {
                    return Err("服务端管理会话回执与请求身份或时效不匹配。".into());
                }
                // 提前十秒换租约，不在有副作用请求失败后自动重发。
                let remaining_ms = opened.expires_at_ms.saturating_sub(unix_timestamp_ms()?);
                self.management_open = Some((
                    open_request.clone(),
                    std::time::Instant::now()
                        + Duration::from_millis(
                            remaining_ms.min(15 * 60 * 1000).saturating_sub(10_000),
                        ),
                ));
                (stream, opened)
            };
            let mut operation_request = request.clone();
            operation_request.session_generation = opened.session_generation;
            write_management_line(&mut *stream, &operation_request, MAX_REQUEST_BYTES).await?;
            let response: RemoteManagementResponse =
                read_management_line(&mut *stream, MAX_RESPONSE_BYTES).await?;
            if response.protocol_version != DEVICE_MANAGEMENT_PROTOCOL_VERSION
                || response.request_id != request.request_id
                || response.target_host_id != request.target_host_id
                || response.session_generation != opened.session_generation
                || response.operation != request.operation
                || !(100..=599).contains(&response.status)
            {
                return Err("服务端管理回执与请求编号、目标、会话或操作不匹配。".into());
            }
            if request.operation == "management.status" {
                self.management_reuse_allowed =
                    response.status == 200 && response.body["persistent_management"] == true;
            }
            if self.management_reuse_allowed {
                *self.management_channel.get_mut() = Some((stream, opened));
            }
            Ok(response)
        };
        tokio::time::timeout(timeout, operation)
            .await
            .map_err(|_| "SSH 独立管理请求等待回执超时。".to_string())?
    }

    async fn open_websocket(
        &mut self,
        path_and_query: &str,
        auth: &DeviceGatewayAuthHeaders,
        timeout: Duration,
    ) -> Result<SshWebSocketStream, String> {
        if timeout.is_zero() || timeout > Duration::from_secs(60) {
            return Err("SSH WebSocket 握手时限必须在 1 至 60 秒之间。".into());
        }
        let target_port = self.target.gateway_port;
        let key = base64::engine::general_purpose::STANDARD.encode(rand::random::<[u8; 16]>());
        let request = encode_websocket_upgrade_request(path_and_query, auth, &key, target_port)?;
        let operation = async {
            let mut stream = self.open_fixed_bridge(MEDIA_BRIDGE_MARKER).await?;
            stream
                .write_all(&request)
                .await
                .map_err(|_| "SSH WebSocket 握手请求写入失败。".to_string())?;
            stream
                .flush()
                .await
                .map_err(|_| "SSH WebSocket 握手请求刷新失败。".to_string())?;
            read_websocket_upgrade_response(stream.as_mut(), &key).await?;
            Ok(SshWebSocketStream::new(stream))
        };
        tokio::time::timeout(timeout, operation)
            .await
            .map_err(|_| "SSH WebSocket 媒体通道建立超时。".to_string())?
    }

    /// close（关闭）：显式关闭 SSH 会话，不迁移或重放未确认任务。
    pub async fn close(&self) -> Result<(), String> {
        self.handle
            .disconnect(Disconnect::ByApplication, "device session closed", "")
            .await
            .map_err(|_| "SSH 会话关闭失败。".into())
    }
}

/// SshDeviceManager（SSH 设备管理器）：串行管理一条当前信任主机连接。
#[derive(Default)]
pub struct SshDeviceManager {
    key_root: Mutex<Option<PathBuf>>, // key_root（当前工作区的项目私钥目录根；不使用用户目录）。
    connection: tokio::sync::Mutex<Option<Arc<ConnectedDeviceSession>>>, // connection（当前 SSH 与产品设备身份绑定会话）。
    active_lease: tokio::sync::Mutex<Option<RemoteDeviceSessionLease>>, // active_lease（当前任务通道确认的设备连接与执行器代次）。
}

impl SshDeviceManager {
    async fn current_session(&self) -> Result<Arc<ConnectedDeviceSession>, String> {
        self.connection.lock().await.clone()
            .ok_or_else(|| "当前没有已认证的 SSH 主机连接。".to_string())
    }

    pub fn set_key_root(&self, data: &Path) -> Result<(), String> {
        *self
            .key_root
            .lock()
            .map_err(|_| "项目密钥目录状态不可用。")? = Some(data.to_path_buf());
        Ok(())
    }

    fn load_key(&self, reference: &str) -> Result<Zeroizing<String>, String> {
        let root = self
            .key_root
            .lock()
            .map_err(|_| "项目密钥目录状态不可用。")?
            .clone();
        crate::credentials::load_device_ssh_key_at(root.as_deref(), reference)
    }

    /// connect（连接）：私钥从当前项目的加密记录加载，不经过前端参数或日志。
    pub async fn connect(
        &self,
        identity_credential_ref: &str,
        transport_credential_ref: Option<&str>,
        device_id: &str,
        target: SshDeviceTarget,
        timeout: Duration,
    ) -> Result<(), String> {
        let expected_identity_ref = device_credential_reference(device_id)?;
        if identity_credential_ref != expected_identity_ref {
            return Err("产品设备身份凭据引用与当前设备编号不匹配。".into());
        }
        {
            let signing_private_key = self.load_key(identity_credential_ref)?;
            let signing_private_key = PrivateKey::from_openssh(signing_private_key.as_str())
                .map_err(|_| "本机存储中的产品设备签名身份无法读取。".to_string())?;
            if signing_private_key.algorithm() != Algorithm::Ed25519
                || signing_private_key.is_encrypted()
            {
                return Err("产品设备身份必须是未加密的 Ed25519 私钥。".into());
            }
        }
        let login_credential_ref = transport_credential_ref.unwrap_or(identity_credential_ref);
        if login_credential_ref != identity_credential_ref
            && !login_credential_ref.starts_with("VoiceTerminal/ssh-transport/")
        {
            return Err("SSH 登录凭据必须引用本机产品身份或已导入的传输密钥。".into());
        }
        let login_private_key = self.load_key(login_credential_ref)?;
        let connection =
            SshDeviceConnection::connect(target.clone(), login_private_key.as_str(), timeout)
                .await?;
        let session = ConnectedDeviceSession {
            connection,
            management: tokio::sync::Mutex::new(None),
            device_id: device_id.to_string(),
            identity_credential_ref: identity_credential_ref.to_string(),
            transport_credential_ref: login_credential_ref.to_string(),
            target,
            #[cfg(any(test, feature = "ssh-device-test-support"))]
            signing_key_for_test: None,
            #[cfg(any(test, feature = "ssh-device-test-support"))]
            transport_key_for_test: None,
        };
        let old = {
            let mut slot = self.connection.lock().await;
            slot.replace(Arc::new(session))
        };
        if let Some(old) = old {
            let _ = old.close().await;
        }
        *self.active_lease.lock().await = None;
        Ok(())
    }

    /// connect_with_test_credentials（集成测试专用连接）：绕过系统凭据库，但仍验证真实 SSH 主机指纹。
    #[cfg(any(test, feature = "ssh-device-test-support"))]
    pub async fn connect_with_test_credentials(
        &self,
        device_id: &str,
        target: SshDeviceTarget,
        signing_private_key: &str,
        transport_private_key: &str,
        timeout: Duration,
    ) -> Result<(), String> {
        let identity_credential_ref = device_credential_reference(device_id)?;
        target.validate()?;
        let signing_key = PrivateKey::from_openssh(signing_private_key)
            .map_err(|_| "测试产品设备私钥格式无效。".to_string())?;
        if signing_key.algorithm() != Algorithm::Ed25519 || signing_key.is_encrypted() {
            return Err("测试产品设备身份必须是未加密的 Ed25519 私钥。".into());
        }
        let connection =
            SshDeviceConnection::connect(target.clone(), transport_private_key, timeout).await?;
        let session = ConnectedDeviceSession {
            connection,
            management: tokio::sync::Mutex::new(None),
            device_id: device_id.to_owned(),
            identity_credential_ref,
            transport_credential_ref: "test-only-transport-identity".into(),
            target,
            signing_key_for_test: Some(Zeroizing::new(signing_private_key.to_owned())),
            transport_key_for_test: Some(Zeroizing::new(transport_private_key.to_owned())),
        };
        let old = self.connection.lock().await.replace(Arc::new(session));
        if let Some(old) = old {
            let _ = old.close().await;
        }
        *self.active_lease.lock().await = None;
        Ok(())
    }

    /// management_request（独立管理操作）：不读取任务/媒体租约，直接打开受限管理 SSH 标记。
    pub async fn management_request(
        &self,
        request_id: &str,
        target_host_id: &str,
        operation: &str,
        arguments: serde_json::Value,
        timeout: Duration,
    ) -> Result<RemoteManagementResponse, String> {
        if !crate::device_management_contract::valid_request_id(request_id)
            || !crate::device_management_contract::valid_host_id(target_host_id)
        {
            return Err("远程管理请求编号或服务端目标编号无效。".into());
        }
        let session = self.current_session().await?;
        let open_request = RemoteManagementOpenRequest {
            protocol_version: DEVICE_MANAGEMENT_PROTOCOL_VERSION,
            request_id: random_hex_id(),
            target_host_id: target_host_id.to_owned(),
            client_device_id: session.device_id.clone(),
            fields: BTreeMap::from([
                ("protocol_version".into(), "独立远程管理协议版本".into()),
                ("request_id".into(), "本次管理会话申请的幂等编号".into()),
                ("target_host_id".into(), "已配对服务端稳定编号".into()),
                (
                    "client_device_id".into(),
                    "当前 SSH 认证客户端设备编号".into(),
                ),
            ]),
        };
        let request = RemoteManagementRequest {
            protocol_version: DEVICE_MANAGEMENT_PROTOCOL_VERSION,
            request_id: request_id.to_owned(),
            target_host_id: target_host_id.to_owned(),
            // management_exchange（管理会话交换）先使用非零占位值，再换成服务端回执的独立代次。
            session_generation: 1,
            operation: operation.to_owned(),
            arguments,
            fields: BTreeMap::from([
                ("protocol_version".into(), "独立远程管理协议版本".into()),
                ("request_id".into(), "一次业务请求的稳定编号".into()),
                ("target_host_id".into(), "已配对服务端稳定编号".into()),
                ("session_generation".into(), "管理服务授予的会话代次".into()),
                ("operation".into(), "服务端固定操作名称".into()),
                ("arguments".into(), "与固定操作对应的 JSON 对象参数".into()),
            ]),
        };
        // 独立管理连接：微软 Win32 OpenSSH 可能在同一条连接上沿用首个 SSH_ORIGINAL_COMMAND
        // （原始命令标记）；与管理租约共用连接会让后续控制通道被送进管理 JSON 解析器。
        // 这里只更换承载连接，管理租约、通道复用与逐次信任核验都保留在管理通道内。
        let login_private_key = load_session_transport_key(self, &session)?;
        let target = session.target.clone();
        let mut slot = session.management.lock().await;
        if slot.as_ref().is_none_or(SshDeviceConnection::is_closed) {
            *slot = Some(SshDeviceConnection::connect(target, login_private_key.as_str(), timeout).await?);
        }
        let management = slot.as_mut().ok_or("独立管理 SSH 连接不可用。")?;
        let result = management.management_exchange(&open_request, &request, timeout).await;
        if management.is_closed() {
            *slot = None;
        }
        result
    }

    /// relay_voice_websocket（转发产品语音 WebSocket）：仅接受本机随机票据路径，并经固定 control（控制）强制桥接。
    pub async fn relay_voice_websocket(
        &self,
        mut local_stream: tokio::net::TcpStream,
        token: &str,
        mut stop: tokio::sync::watch::Receiver<bool>,
    ) -> Result<(), String> {
        if !local_stream
            .peer_addr()
            .map_err(|_| "本机语音 WebSocket 缺少回环对端地址。".to_string())?
            .ip()
            .is_loopback()
            || token.len() != 64
            || !token
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err("本机语音 WebSocket 转发对端或随机票据无效。".into());
        }
        local_stream
            .set_nodelay(true)
            .map_err(|_| "无法配置本机语音 WebSocket 套接字。".to_string())?;
        let relay = async {
            let local_header = read_http_header_bounded(
                &mut local_stream,
                MAX_HEADER_BYTES,
                "本机语音 WebSocket 握手头缺失或超过限制。",
            )
            .await?;
            let key = local_voice_upgrade_key(&local_header, token)?;
            let session = self.current_session().await?;
            let lease = self.active_lease.lock().await.clone().ok_or("设备任务会话尚未就绪。")?;
            if lease.device_id != session.device_id {
                return Err("语音控制连接与任务设备不匹配。".into());
            }
            let transport_key = load_session_transport_key(self, &session)?;
            let connection = SshDeviceConnection::connect(
                session.target.clone(), transport_key.as_str(), Duration::from_secs(8),
            ).await?;
            let path = "/v1/voice";
            let body = serde_json::json!({
                "protocol_version": 1,
                "connection_generation": lease.connection_generation,
                "executor_generation": lease.executor_generation,
            });
            let (challenge, auth) = self.authorize_gateway(
                &session, &connection, "GET", path, &body, Duration::from_secs(5),
            ).await?.map_err(|_| "主机未签发语音实时连接挑战。".to_string())?;
            if challenge.core_session_id != lease.core_session_id {
                return Err("服务端语音核心已重启，正在等待新的任务会话。".into());
            }
            let request = encode_websocket_upgrade_request(
                &format!("{path}?connection_generation={}&executor_generation={}",
                    lease.connection_generation, lease.executor_generation),
                &auth, &key, session.target.gateway_port,
            )?;
            let mut upstream = connection.open_fixed_bridge(CONTROL_BRIDGE_MARKER).await?;
            if request.len() > MAX_HEADER_BYTES {
                return Err("固定服务端语音 WebSocket 握手超过长度上限。".into());
            }
            upstream
                .write_all(&request)
                .await
                .map_err(|_| "SSH 语音 WebSocket 握手请求写入失败。".to_string())?;
            upstream
                .flush()
                .await
                .map_err(|_| "SSH 语音 WebSocket 握手刷新失败。".to_string())?;
            let response = read_http_header_bounded(
                upstream.as_mut(),
                MAX_HEADER_BYTES,
                "服务端语音 WebSocket 握手响应缺失或超过限制。",
            )
            .await?;
            validate_voice_upgrade_response(&response, &key)?;
            local_stream
                .write_all(&response)
                .await
                .map_err(|_| "本机语音 WebSocket 升级回执写入失败。".to_string())?;
            local_stream
                .flush()
                .await
                .map_err(|_| "本机语音 WebSocket 升级回执刷新失败。".to_string())?;
            let copied = tokio::io::copy_bidirectional(&mut local_stream, upstream.as_mut())
                .await
                .map_err(|_| "SSH 语音 WebSocket 双向数据通道中断。".to_string());
            drop(upstream);
            let close = connection.close().await;
            match (copied, close) {
                (Err(error), _) => Err(error),
                (Ok(_), Ok(())) => Ok(()),
                (Ok(_), Err(error)) => Err(format!("语音消息已转发，但 SSH 连接收尾失败：{error}")),
            }
        };
        tokio::select! {
            _ = async {
                if *stop.borrow() { return; }
                let _ = stop.changed().await;
            } => Ok(()),
            result = relay => result,
        }
    }

    /// upload_remote_import（上传客户端所选资源来源）：另建固定 FILE（文件）SSH 连接，避免阻塞管理或媒体通道。
    #[cfg(feature = "ssh-device")]
    pub async fn upload_remote_import<F>(
        &self,
        source_id: &str,
        request_id: &str,
        local_sources: &[PathBuf],
        cancel: &AtomicBool,
        on_progress: F,
    ) -> Result<crate::device_file_channel::UploadReceipt, String>
    where
        F: FnMut(crate::device_file_channel::UploadProgress) + Send,
    {
        let (target, transport_credential_ref) = {
            let slot = self.connection.lock().await;
            let session = slot
                .as_ref()
                .ok_or_else(|| "当前没有已认证的 SSH 主机连接。".to_string())?;
            (
                session.target.clone(),
                session.transport_credential_ref.clone(),
            )
        };
        let private_key = self.load_key(&transport_credential_ref)?;
        let connection =
            SshDeviceConnection::connect(target, private_key.as_str(), Duration::from_secs(20))
                .await?;
        let transfer = async {
            let mut stream = connection.open_fixed_bridge(FILE_BRIDGE_MARKER).await?;
            crate::device_file_channel::upload_manifested_files(
                &mut stream,
                source_id,
                request_id,
                local_sources,
                cancel,
                on_progress,
            )
            .await
        }
        .await;
        let close = connection.close().await;
        match (transfer, close) {
            (Ok(receipt), Ok(())) => Ok(receipt),
            (Ok(_), Err(error)) => Err(format!("文件已提交，但 SSH 文件会话关闭失败：{error}")),
            (Err(error), _) => Err(error),
        }
    }

    /// post_json（设备控制）：并发调用在单一连接锁上顺序结算。
    pub async fn post_json(
        &self,
        path: &str,
        body: &serde_json::Value,
        timeout: Duration,
    ) -> Result<GatewayResponse, String> {
        if !matches!(
            path,
            "/v1/status"
                | "/v1/devices"
                | "/v1/management"
                | "/v1/session/open"
                | "/v1/session/poll"
                | "/v1/session/complete"
                | "/v1/session/reconcile"
                | "/v1/session/heartbeat"
                | "/v1/session/close"
                | "/v1/interaction/prepare"
                | "/v1/interaction/abort"
        ) {
            return Err("当前 SSH 客户端只开放已登记的设备状态和任务通道接口。".into());
        }
        let session = self.current_session().await?;
        let connection = &session.connection;
        match self.authorize_gateway(&session, connection, "POST", path, body, timeout).await? {
            Ok((_, auth)) => connection.post_json_with_auth(path, body, Some(&auth), timeout).await,
            Err(response) => Ok(response),
        }
    }

    async fn authorize_gateway(
        &self, session: &ConnectedDeviceSession, connection: &SshDeviceConnection,
        method: &str, path: &str, body: &serde_json::Value, timeout: Duration,
    ) -> Result<Result<(DeviceGatewayChallenge, DeviceGatewayAuthHeaders), GatewayResponse>, String> {
        let challenge_body = serde_json::json!({
            "protocol_version": 1,
            "device_id": session.device_id,
            "method": method,
            "path": path,
        });
        let response = connection.post_json("/v1/challenge", &challenge_body, timeout).await?;
        if response.status != 200 {
            return Ok(Err(response));
        }
        let challenge: DeviceGatewayChallenge = serde_json::from_value(response.body)
            .map_err(|_| "主机设备签名挑战格式无效。".to_string())?;
        if challenge.protocol_version != 1 || challenge.expires_at_ms == 0 {
            return Err("主机设备签名挑战版本或时效无效。".into());
        }
        let key = load_session_signing_key(self, session)?;
        let key = PrivateKey::from_openssh(key.as_str())
            .map_err(|_| "本机存储中的设备签名身份无法读取。".to_string())?;
        let auth = sign_device_gateway_request_for_method(
            &key, method, &session.device_id, path, body, &challenge, unix_timestamp_ms()?,
        )?;
        Ok(Ok((challenge, auth)))
    }

    /// open_media_channel（开启 PCM 媒体通道）：新建独立固定主机 SSH 连接并签名升级 WebSocket。
    pub async fn open_media_channel(
        &self,
        stream_id: &str,
        connection_generation: u64,
        executor_generation: u64,
        interaction_generation: u64,
        timeout: Duration,
    ) -> Result<SshMediaChannel, String> {
        self.open_media_channel_with_codecs(
            stream_id,
            connection_generation,
            executor_generation,
            interaction_generation,
            AudioCodec::Pcm,
            AudioCodec::Pcm,
            0,
            0,
            timeout,
        )
        .await
    }

    /// open_media_channel_with_codecs（分别协商上/下行编码及各自预延迟）：签名主体绑定两个方向的完整参数。
    pub async fn open_media_channel_with_codecs(
        &self,
        stream_id: &str,
        connection_generation: u64,
        executor_generation: u64,
        interaction_generation: u64,
        uplink_codec: AudioCodec,
        downlink_codec: AudioCodec,
        encoder_pre_skip_samples: u32,
        downlink_encoder_pre_skip_samples: u32,
        timeout: Duration,
    ) -> Result<SshMediaChannel, String> {
        let valid_stream_id = !stream_id.is_empty()
            && stream_id.len() <= 128
            && stream_id.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-')
            });
        let max_generation = (1_u64 << 53) - 1;
        if !valid_stream_id
            || [
                connection_generation,
                executor_generation,
                interaction_generation,
            ]
            .iter()
            .any(|value| *value == 0 || *value > max_generation)
            || encoder_pre_skip_samples > 320
            || (uplink_codec == AudioCodec::Pcm && encoder_pre_skip_samples != 0)
            || (uplink_codec == AudioCodec::Opus && encoder_pre_skip_samples == 0)
            || downlink_encoder_pre_skip_samples > 960
            || (downlink_codec == AudioCodec::Pcm && downlink_encoder_pre_skip_samples != 0)
            || (downlink_codec == AudioCodec::Opus && downlink_encoder_pre_skip_samples == 0)
            || timeout.is_zero()
            || timeout > Duration::from_secs(60)
        {
            return Err("SSH 媒体流编号、设备代次或握手时限无效。".into());
        }
        let handshake = {
            let session = self.current_session().await?;
            let device_id = session.device_id.clone();
            let target = session.target.clone();
            let transport_private_key = load_session_transport_key(self, &session)?;
            let mut body = serde_json::json!({
                "protocol_version": MEDIA_PROTOCOL_VERSION,
                "stream_id": stream_id,
                "connection_generation": connection_generation,
                "executor_generation": executor_generation,
                "interaction_generation": interaction_generation,
            });
            if uplink_codec != AudioCodec::Pcm
                || downlink_codec != AudioCodec::Pcm
                || encoder_pre_skip_samples != 0
                || downlink_encoder_pre_skip_samples != 0
            {
                body["uplink_codec"] = serde_json::json!(uplink_codec.as_str());
                body["downlink_codec"] = serde_json::json!(downlink_codec.as_str());
                body["encoder_pre_skip_samples"] = serde_json::json!(encoder_pre_skip_samples);
                body["downlink_encoder_pre_skip_samples"] =
                    serde_json::json!(downlink_encoder_pre_skip_samples);
            }
            let (challenge, auth) = self.authorize_gateway(
                &session, &session.connection, "GET", "/v1/media", &body, timeout,
            ).await?.map_err(|_| "主机未签发 SSH 媒体通道挑战。".to_string())?;
            (
                device_id,
                target,
                transport_private_key,
                challenge.core_session_id,
                body,
                auth,
                uplink_codec,
                downlink_codec,
                encoder_pre_skip_samples,
                downlink_encoder_pre_skip_samples,
            )
        };
        let (
            device_id,
            target,
            login_private_key,
            core_session_id,
            body,
            auth,
            uplink_codec,
            downlink_codec,
            encoder_pre_skip_samples,
            downlink_encoder_pre_skip_samples,
        ) = handshake;
        let mut connection =
            SshDeviceConnection::connect(target.clone(), login_private_key.as_str(), timeout)
                .await?;
        let mut path_and_query = format!(
            "/v1/media?protocol_version={}&stream_id={}&connection_generation={}&executor_generation={}&interaction_generation={}",
            MEDIA_PROTOCOL_VERSION,
            body["stream_id"].as_str().ok_or("设备媒体流编号丢失。")?,
            connection_generation, executor_generation, interaction_generation,
        );
        if body.get("uplink_codec").is_some() {
            path_and_query.push_str(&format!(
                "&uplink_codec={}&downlink_codec={}&encoder_pre_skip_samples={}",
                uplink_codec.as_str(),
                downlink_codec.as_str(),
                encoder_pre_skip_samples,
            ));
            if let Some(pre_skip) = body.get("downlink_encoder_pre_skip_samples") {
                let pre_skip = pre_skip.as_u64().ok_or("下行 Opus 预延迟字段丢失。")?;
                path_and_query.push_str(&format!("&downlink_encoder_pre_skip_samples={pre_skip}"));
            }
        }
        let websocket = match connection
            .open_websocket(&path_and_query, &auth, timeout)
            .await
        {
            Ok(stream) => stream,
            Err(error) => {
                let _ = connection.close().await;
                return Err(error);
            }
        };
        Ok(SshMediaChannel {
            websocket,
            _connection: connection,
            device_id,
            stream_id: stream_id.to_string(),
            core_session_id,
            connection_generation,
            executor_generation,
            interaction_generation,
            uplink_codec,
            downlink_codec,
            encoder_pre_skip_samples,
            downlink_encoder_pre_skip_samples,
            clock_uncertainty_us: None,
            capture_active: false,
        })
    }

    /// device_file_upload（固定 FILE 上传）：签名 begin 绑定原票据、请求与活动设备代次。
    pub async fn device_file_upload(
        &self,
        local_path: &Path,
        transfer_id: &str,
        parent_request_id: &str,
        connection_generation: u64,
        executor_generation: u64,
        maximum_bytes: u64,
        timeout: Duration,
    ) -> Result<SftpTransferReceipt, String> {
        let (target, credential_ref, begin, authorization) = self
            .prepare_device_file_transfer(
                "file.sftp.upload",
                transfer_id,
                parent_request_id,
                connection_generation,
                executor_generation,
                maximum_bytes,
                None,
                timeout,
            )
            .await?;
        crate::device_files::validate_transfer_source(local_path, maximum_bytes)?;
        let private_key = self.load_key(&credential_ref)?;
        let connection = SshDeviceConnection::connect(
            target,
            private_key.as_str(),
            timeout.min(Duration::from_secs(60)),
        )
        .await?;
        let cancelled = AtomicBool::new(false);
        let transfer = tokio::time::timeout(timeout, async {
            let mut stream = connection.open_fixed_bridge(FILE_BRIDGE_MARKER).await?;
            crate::device_file_channel::upload_device_file(
                &mut stream,
                begin,
                authorization,
                local_path,
                &cancelled,
            )
            .await
        })
        .await
        .map_err(|_| {
            "固定 FILE 上传等待超时；原任务状态可能未知，请查询原任务编号。".to_string()
        })?;
        let close = connection.close().await;
        match (transfer, close) {
            (Ok(receipt), Ok(())) => Ok(SftpTransferReceipt {
                path: receipt.path,
                bytes: receipt.bytes,
                sha256: receipt.sha256,
            }),
            (Ok(_), Err(error)) => Err(format!("文件传输已完成但 SSH FILE 会话关闭失败：{error}")),
            (Err(error), _) => Err(error),
        }
    }

    /// device_file_download（固定 FILE 下载）：仅从原 DFT 附件票据读取并原子写入本机目标。
    pub async fn device_file_download(
        &self,
        local_path: &Path,
        transfer_id: &str,
        parent_request_id: &str,
        connection_generation: u64,
        executor_generation: u64,
        expected_sha256: &str,
        maximum_bytes: u64,
        timeout: Duration,
    ) -> Result<SftpTransferReceipt, String> {
        let (target, credential_ref, begin, authorization) = self
            .prepare_device_file_transfer(
                "file.sftp.download",
                transfer_id,
                parent_request_id,
                connection_generation,
                executor_generation,
                maximum_bytes,
                Some(expected_sha256),
                timeout,
            )
            .await?;
        crate::device_files::validate_transfer_target_parent(local_path)?;
        let private_key = self.load_key(&credential_ref)?;
        let connection = SshDeviceConnection::connect(
            target,
            private_key.as_str(),
            timeout.min(Duration::from_secs(60)),
        )
        .await?;
        let cancelled = AtomicBool::new(false);
        let transfer = tokio::time::timeout(timeout, async {
            let mut stream = connection.open_fixed_bridge(FILE_BRIDGE_MARKER).await?;
            crate::device_file_channel::download_device_file(
                &mut stream,
                begin,
                authorization,
                local_path,
                &cancelled,
            )
            .await
        })
        .await
        .map_err(|_| {
            "固定 FILE 下载等待超时；原任务状态可能未知，请查询原任务编号。".to_string()
        })?;
        let close = connection.close().await;
        match (transfer, close) {
            (Ok(receipt), Ok(())) => Ok(SftpTransferReceipt {
                path: receipt.path,
                bytes: receipt.bytes,
                sha256: receipt.sha256,
            }),
            (Ok(_), Err(error)) => Err(format!("文件传输已完成但 SSH FILE 会话关闭失败：{error}")),
            (Err(error), _) => Err(error),
        }
    }

    async fn prepare_device_file_transfer(
        &self,
        operation: &str,
        transfer_id: &str,
        parent_request_id: &str,
        connection_generation: u64,
        executor_generation: u64,
        maximum_bytes: u64,
        expected_sha256: Option<&str>,
        timeout: Duration,
    ) -> Result<
        (
            SshDeviceTarget,
            String,
            DeviceFileTransferBegin,
            FileTransferAuthorization,
        ),
        String,
    > {
        if !matches!(operation, "file.sftp.upload" | "file.sftp.download")
            || !valid_dsh_transfer_id(transfer_id)
            || !valid_dsh_request_id(parent_request_id)
            || connection_generation == 0
            || connection_generation > (1_u64 << 53) - 1
            || executor_generation == 0
            || executor_generation > (1_u64 << 53) - 1
            || maximum_bytes == 0
            || maximum_bytes > 4 * 1024 * 1024 * 1024
            || timeout.is_zero()
            || timeout > Duration::from_secs(600)
            || (operation == "file.sftp.download"
                && !expected_sha256.is_some_and(|value| validate_sha256(value).is_ok()))
            || (operation == "file.sftp.upload" && expected_sha256.is_some())
        {
            return Err("固定 FILE 票据、操作、设备代次、文件上限或超时时限无效。".into());
        }
        let lease = self
            .active_lease
            .lock()
            .await
            .clone()
            .ok_or_else(|| "当前没有正在运行的远端设备任务租约。".to_string())?;
        if lease.connection_generation != connection_generation
            || lease.executor_generation != executor_generation
        {
            return Err("文件任务连接或执行器代次与当前远端设备租约不匹配。".into());
        }
        let session = self.current_session().await?;
        if session.device_id != lease.device_id || session.connection.is_closed() {
            return Err("固定 FILE 连接的设备身份与当前任务租约不一致。".into());
        }
        let mut fields = BTreeMap::from([
            ("protocol_version".into(), "固定设备文件通道协议版本".into()),
            ("action".into(), "固定为 begin 的原票据申请".into()),
            ("transfer_id".into(), "原 DSH 文件传输票据编号".into()),
            ("operation".into(), "兼容保留的文件任务操作编号".into()),
            (
                "parent_request_id".into(),
                "生成原文件票据的稳定 DSH 请求编号".into(),
            ),
            (
                "connection_generation".into(),
                "当前 SSH 设备连接代次".into(),
            ),
            ("executor_generation".into(), "当前设备执行器代次".into()),
            ("maximum_bytes".into(), "原审批允许的最大文件字节数".into()),
        ]);
        if expected_sha256.is_some() {
            fields.insert(
                "expected_sha256".into(),
                "服务端已锁定的审批附件完整摘要".into(),
            );
        }
        let begin = DeviceFileTransferBegin {
            protocol_version: 1,
            action: "begin".into(),
            transfer_id: transfer_id.to_owned(),
            operation: operation.to_owned(),
            parent_request_id: parent_request_id.to_owned(),
            connection_generation,
            executor_generation,
            maximum_bytes,
            expected_sha256: expected_sha256.map(str::to_owned),
            fields,
        };
        begin.validate()?;
        let challenge_body = serde_json::json!({
            "protocol_version":1,"device_id":session.device_id,"method":"POST","path":FILE_TRANSFER_GATEWAY_PATH,
        });
        let challenge_response = session
            .connection
            .post_json("/v1/challenge", &challenge_body, timeout)
            .await?;
        if challenge_response.status != 200 {
            return Err(format!(
                "主机未签发固定 FILE 请求签名挑战，HTTP 状态码 {}。",
                challenge_response.status
            ));
        }
        let challenge: DeviceGatewayChallenge = serde_json::from_value(challenge_response.body)
            .map_err(|_| "固定 FILE 签名挑战格式无效。".to_string())?;
        if challenge.protocol_version != 1 || challenge.expires_at_ms == 0 {
            return Err("固定 FILE 签名挑战版本或有效期无效。".into());
        }
        let timestamp_ms = unix_timestamp_ms()?;
        if timestamp_ms > challenge.expires_at_ms {
            return Err("固定 FILE 签名挑战已过期。".into());
        }
        let signing_key_text = load_session_signing_key(self, &session)?;
        let signing_key = PrivateKey::from_openssh(signing_key_text.as_str())
            .map_err(|_| "本机存储中的产品设备签名身份无法读取。".to_string())?;
        let signed_body =
            serde_json::to_value(&begin).map_err(|_| "固定 FILE 签名正文无法编码。")?;
        let signed = sign_device_gateway_request(
            &signing_key,
            &session.device_id,
            FILE_TRANSFER_GATEWAY_PATH,
            &signed_body,
            &challenge,
            timestamp_ms,
        )?;
        let authorization = FileTransferAuthorization {
            device_id: signed.device_id,
            core_session_id: signed.core_session_id,
            challenge: signed.challenge,
            timestamp_ms: signed.timestamp_ms,
            signature: signed.signature,
        };
        authorization.validate()?;
        Ok((
            session.target.clone(),
            session.transport_credential_ref.clone(),
            begin,
            authorization,
        ))
    }

    /// open_remote_device_session（建立受信任务通道）：不授予远端语音交互租约。
    pub async fn open_remote_device_session(
        &self,
        timeout: Duration,
    ) -> Result<RemoteDeviceSessionLease, String> {
        self.open_remote_device_session_with_capabilities(
            vec![
                "desktop_control".into(),
                "file_transfer".into(),
                "terminal_control".into(),
            ],
            timeout,
        )
        .await
    }

    /// open_remote_device_session_with_capabilities（按已就绪能力开通）：不授予语音交互租约。
    pub async fn open_remote_device_session_with_capabilities(
        &self,
        capabilities: Vec<String>,
        timeout: Duration,
    ) -> Result<RemoteDeviceSessionLease, String> {
        let valid_capabilities = !capabilities.is_empty()
            && capabilities.len() <= 6
            && capabilities.windows(2).all(|pair| pair[0] < pair[1])
            && capabilities.iter().all(|value| {
                matches!(
                    value.as_str(),
                    "audio_stream"
                        | "client_tts"
                        | "desktop_control"
                        | "file_transfer"
                        | "screen_capture"
                        | "terminal_control"
                )
            });
        if !valid_capabilities {
            return Err("远端设备能力列表必须是固定清单中的排序子集。".into());
        }
        let response = self
            .post_json(
                "/v1/session/open",
                &serde_json::json!({"protocol_version":1,"capabilities":capabilities}),
                timeout,
            )
            .await?;
        if response.status != 200 {
            return Err(format!(
                "主机拒绝远端设备会话注册，HTTP 状态码 {}。",
                response.status
            ));
        }
        let lease: RemoteDeviceSessionLease = serde_json::from_value(response.body)
            .map_err(|_| "主机远端设备会话租约格式无效。".to_string())?;
        validate_remote_session_lease(&lease)?;
        let slot = self.connection.lock().await;
        let session = slot.as_ref().ok_or("当前没有已认证的 SSH 主机连接。")?;
        if lease.device_id != session.device_id {
            return Err("主机返回的会话租约属于其他设备。".into());
        }
        drop(slot);
        *self.active_lease.lock().await = Some(lease.clone());
        Ok(lease)
    }

    /// poll_remote_device_task（拉取一项远端设备任务）：单轮等待不超过 5 秒。
    pub async fn poll_remote_device_task(
        &self,
        lease: &RemoteDeviceSessionLease,
        wait_ms: u32,
        timeout: Duration,
    ) -> Result<Option<RemoteDeviceTask>, String> {
        self.poll_remote_device_task_lane(lease, wait_ms, timeout, None).await
    }

    async fn poll_remote_device_task_lane(
        &self,
        lease: &RemoteDeviceSessionLease,
        wait_ms: u32,
        timeout: Duration,
        lane: Option<&str>,
    ) -> Result<Option<RemoteDeviceTask>, String> {
        validate_remote_session_lease(lease)?;
        if wait_ms > 5_000 || timeout.is_zero() || timeout > Duration::from_secs(15) {
            return Err("远端设备轮询等待或超时时限无效。".into());
        }
        let mut payload = serde_json::json!({
            "protocol_version":1,
            "connection_generation":lease.connection_generation,
            "executor_generation":lease.executor_generation,
            "wait_ms":wait_ms,
        });
        if let Some(lane) = lane {
            payload["lane"] = serde_json::Value::String(lane.into());
        }
        let response = self.post_json("/v1/session/poll", &payload, timeout).await?;
        if response.status != 200 {
            return Err(format!(
                "远端设备任务轮询失败，HTTP 状态码 {}。",
                response.status
            ));
        }
        let poll: RemoteDeviceTaskPoll = serde_json::from_value(response.body)
            .map_err(|_| "远端设备任务轮询回执格式无效。".to_string())?;
        if poll.protocol_version != 1
            || poll.result != "accepted"
            || poll.device_id != lease.device_id
            || poll.core_session_id != lease.core_session_id
            || poll.connection_generation != lease.connection_generation
            || poll.executor_generation != lease.executor_generation
        {
            return Err("远端设备任务轮询回执与当前设备租约不匹配。".into());
        }
        if let Some(task) = &poll.task {
            validate_remote_device_task(task, lease)?;
            if lane.is_some_and(|lane| task.operation.starts_with("playback.") != (lane == "playback")) {
                return Err("主机返回了其他类别的设备任务。".into());
            }
        }
        Ok(poll.task)
    }

    /// complete_remote_device_task（提交原任务回执）：失败或断线时不自动重做。
    pub async fn complete_remote_device_task(
        &self,
        lease: &RemoteDeviceSessionLease,
        task: &RemoteDeviceTask,
        succeeded: bool,
        result: serde_json::Value,
        timeout: Duration,
    ) -> Result<String, String> {
        validate_remote_device_task(task, lease)?;
        if timeout.is_zero() || timeout > Duration::from_secs(600) {
            return Err("远端设备回执超时时限无效。".into());
        }
        let payload = task_completion_payload(task, succeeded, &result);
        if serde_json::to_vec(&payload).map_or(true, |encoded| encoded.len() > MAX_TASK_RECEIPT_BYTES) {
            return Err("远端设备操作结果超过大小限制。".into());
        }
        let response = self
            .post_json(
                "/v1/session/complete",
                &payload,
                timeout,
            )
            .await?;
        if response.status != 200 {
            return Err(format!(
                "主机未接受远端设备任务回执，HTTP 状态码 {}。",
                response.status
            ));
        }
        let receipt = response
            .body
            .get("result")
            .and_then(serde_json::Value::as_str)
            .ok_or("主机远端设备回执格式无效。")?;
        if !matches!(receipt, "accepted" | "already_completed") {
            return Err("主机返回了未知的远端设备回执状态。".into());
        }
        Ok(receipt.to_string())
    }

    /// reconcile_remote_device_receipt（跨连接结算旧回执）：只结算原任务，不派发副作用。
    pub async fn reconcile_remote_device_receipt(
        &self,
        lease: &RemoteDeviceSessionLease,
        receipt: &PendingDeviceReceipt,
        timeout: Duration,
    ) -> Result<String, String> {
        validate_remote_session_lease(lease)?;
        if receipt.device_id != lease.device_id
            || receipt.task_id.is_empty()
            || receipt.task_id.len() > 128
            || receipt.task_id.trim() != receipt.task_id
            || receipt.core_session_id.is_empty()
            || receipt.core_session_id.len() > 128
            || receipt.core_session_id.trim() != receipt.core_session_id
            || receipt.connection_generation == 0
            || receipt.executor_generation == 0
            || receipt.sequence == 0
            || timeout.is_zero()
            || timeout > Duration::from_secs(30)
            || serde_json::to_vec(&receipt.result).map_or(true, |bytes| bytes.len() > 256 * 1024)
        {
            return Err("待恢复设备回执与当前设备或原代次不匹配，或超过大小时限。".into());
        }
        let response = self
            .post_json(
                "/v1/session/reconcile",
                &serde_json::json!({
                    "protocol_version":1,
                    "task_id":receipt.task_id,
                    "core_session_id":receipt.core_session_id,
                    "connection_generation":receipt.connection_generation,
                    "executor_generation":receipt.executor_generation,
                    "sequence":receipt.sequence,
                    "succeeded":receipt.succeeded,
                    "result":receipt.result,
                }),
                timeout,
            )
            .await?;
        if response.status != 200 {
            let detail = response
                .body
                .get("detail")
                .and_then(serde_json::Value::as_str)
                .map(|value| value.chars().take(160).collect::<String>())
                .unwrap_or_default();
            return Err(format!(
                "核心未接受原任务回执恢复，HTTP 状态码 {}；{}；本地回执保留。",
                response.status, detail
            ));
        }
        if response
            .body
            .get("protocol_version")
            .and_then(serde_json::Value::as_u64)
            != Some(1)
            || response
                .body
                .get("task_id")
                .and_then(serde_json::Value::as_str)
                != Some(receipt.task_id.as_str())
        {
            return Err("原任务回执恢复响应与本地任务编号不匹配；本地回执保留。".into());
        }
        match response
            .body
            .get("result")
            .and_then(serde_json::Value::as_str)
        {
            Some(value @ ("reconciled" | "already_completed" | "pending" | "unknown")) => {
                Ok(value.to_owned())
            }
            _ => Err("核心返回了未知的原任务回执恢复状态；本地回执保留。".into()),
        }
    }

    /// close_remote_device_session（关闭远端任务通道）：不迁移或重放在途任务。
    /// heartbeat_remote_device_session（续接长任务租约）：只续租当前会话，不领取新任务。
    pub async fn heartbeat_remote_device_session(
        &self,
        lease: &RemoteDeviceSessionLease,
        timeout: Duration,
    ) -> Result<(), String> {
        validate_remote_session_lease(lease)?;
        if timeout.is_zero() || timeout > Duration::from_secs(15) {
            return Err("远端设备续租超时时限无效。".into());
        }
        let response = self
            .post_json(
                "/v1/session/heartbeat",
                &serde_json::json!({
                    "protocol_version":1,
                    "connection_generation":lease.connection_generation,
                    "executor_generation":lease.executor_generation,
                }),
                timeout,
            )
            .await?;
        if response.status != 200
            || response
                .body
                .get("result")
                .and_then(serde_json::Value::as_str)
                != Some("accepted")
            || response
                .body
                .get("device_id")
                .and_then(serde_json::Value::as_str)
                != Some(lease.device_id.as_str())
            || response
                .body
                .get("connection_generation")
                .and_then(serde_json::Value::as_u64)
                != Some(lease.connection_generation)
            || response
                .body
                .get("executor_generation")
                .and_then(serde_json::Value::as_u64)
                != Some(lease.executor_generation)
        {
            return Err(format!(
                "主机未确认当前远端设备租约续接，HTTP 状态码 {}。",
                response.status
            ));
        }
        Ok(())
    }

    pub async fn close_remote_device_session(
        &self,
        lease: &RemoteDeviceSessionLease,
        timeout: Duration,
    ) -> Result<bool, String> {
        validate_remote_session_lease(lease)?;
        {
            let mut active = self.active_lease.lock().await;
            if active.as_ref().is_some_and(|current| {
                current.device_id == lease.device_id
                    && current.connection_generation == lease.connection_generation
                    && current.executor_generation == lease.executor_generation
            }) {
                *active = None;
            }
        }
        let response = self
            .post_json(
                "/v1/session/close",
                &serde_json::json!({
                    "protocol_version":1,
                    "connection_generation":lease.connection_generation,
                    "executor_generation":lease.executor_generation,
                }),
                timeout,
            )
            .await?;
        if response.status != 200 {
            return Err(format!(
                "主机未接受远端设备会话关闭，HTTP 状态码 {}。",
                response.status
            ));
        }
        Ok(matches!(
            response
                .body
                .get("result")
                .and_then(serde_json::Value::as_str),
            Some("closed")
        ))
    }

    /// run_remote_device_task_channel（运行受管设备任务通道）：错误或断线时不重放原操作。
    pub async fn run_remote_device_task_channel<F, Fut>(
        &self,
        stop: Arc<AtomicBool>,
        execute: F,
    ) -> Result<(), String>
    where
        F: FnMut(RemoteDeviceTask) -> Fut,
        Fut: Future<Output = Result<RemoteDeviceTaskResult, String>>,
    {
        let lease = self
            .open_remote_device_session(Duration::from_secs(10))
            .await?;
        self.run_remote_device_task_channel_for_lease(lease, stop, execute)
            .await
    }

    /// run_remote_device_task_channel_for_lease（运行指定租约）：重连后不得复用旧代次。
    pub async fn run_remote_device_task_channel_for_lease<F, Fut>(
        &self,
        lease: RemoteDeviceSessionLease,
        stop: Arc<AtomicBool>,
        execute: F,
    ) -> Result<(), String>
    where
        F: FnMut(RemoteDeviceTask) -> Fut,
        Fut: Future<Output = Result<RemoteDeviceTaskResult, String>>,
    {
        self.run_remote_device_task_channel_inner(lease, stop, None, execute)
            .await
    }

    /// run_remote_device_task_channel_for_lease_with_receipts（带本地回执日志的任务通道）。
    pub async fn run_remote_device_task_channel_for_lease_with_receipts<F, Fut>(
        &self,
        lease: RemoteDeviceSessionLease,
        stop: Arc<AtomicBool>,
        journal: &DeviceTaskReceiptJournal,
        execute: F,
    ) -> Result<(), String>
    where
        F: FnMut(RemoteDeviceTask) -> Fut,
        Fut: Future<Output = Result<RemoteDeviceTaskResult, String>>,
    {
        self.run_remote_device_task_channel_inner(lease, stop, Some(journal), execute)
            .await
    }

    async fn run_remote_device_task_channel_inner<F, Fut>(
        &self,
        lease: RemoteDeviceSessionLease,
        stop: Arc<AtomicBool>,
        journal: Option<&DeviceTaskReceiptJournal>,
        execute: F,
    ) -> Result<(), String>
    where
        F: FnMut(RemoteDeviceTask) -> Fut,
        Fut: Future<Output = Result<RemoteDeviceTaskResult, String>>,
    {
        validate_remote_session_lease(&lease)?;
        *self.active_lease.lock().await = Some(lease.clone());
        let result = async {
            if let Some(journal) = journal {
                for receipt in journal.pending_for_device(&lease.device_id)? {
                    let state = self
                        .reconcile_remote_device_receipt(&lease, &receipt, Duration::from_secs(10))
                        .await?;
                    if matches!(state.as_str(), "reconciled" | "already_completed") {
                        journal.remove(&receipt.device_id, &receipt.task_id)?;
                    }
                }
            }
            let execute = tokio::sync::Mutex::new(execute);
            if lease.separate_playback_tasks {
                tokio::try_join!(
                    self.run_remote_device_task_lane(&lease, &stop, journal, &execute, Some("general")),
                    self.run_remote_device_task_lane(&lease, &stop, journal, &execute, Some("playback")),
                )?;
                Ok(())
            } else {
                self.run_remote_device_task_lane(&lease, &stop, journal, &execute, None).await
            }
        };
        let result = result.await;
        let close_result = self
            .close_remote_device_session(&lease, Duration::from_secs(5))
            .await;
        match (result, close_result) {
            (Err(error), _) => Err(error),
            (Ok(()), Ok(_) | Err(_)) => Ok(()),
        }
    }

    async fn run_remote_device_task_lane<F, Fut>(
        &self,
        lease: &RemoteDeviceSessionLease,
        stop: &AtomicBool,
        journal: Option<&DeviceTaskReceiptJournal>,
        execute: &tokio::sync::Mutex<F>,
        lane: Option<&str>,
    ) -> Result<(), String>
    where
        F: FnMut(RemoteDeviceTask) -> Fut,
        Fut: Future<Output = Result<RemoteDeviceTaskResult, String>>,
    {
        loop {
            if stop.load(Ordering::Acquire) {
                return Ok(());
            }
            if let Some(journal) = journal {
                journal.ensure_room()?;
            }
            let Some(task) = self
                .poll_remote_device_task_lane(lease, 1000, Duration::from_secs(8), lane)
                .await?
            else {
                continue;
            };
            let execution = (execute.lock().await)(task.clone());
            tokio::pin!(execution);
            let outcome = loop {
                tokio::select! {
                    // 含副作用的操作如果无法确认结果，会话关闭会保留原任务为未知。
                    result = &mut execution => break result?,
                    _ = tokio::time::sleep(Duration::from_secs(20)) => {
                        self.heartbeat_remote_device_session(lease, Duration::from_secs(8)).await?;
                    }
                }
            };
            let outcome = bounded_task_receipt(&task, outcome)?;
            if let Some(journal) = journal {
                journal.record(&task, &outcome)?;
            }
            self.complete_remote_device_task(
                lease,
                &task,
                outcome.succeeded,
                outcome.result,
                Duration::from_secs(10),
            )
            .await?;
            if let Some(journal) = journal {
                journal.remove(&task.device_id, &task.task_id)?;
            }
        }
    }

    /// submit_pairing_request（签名配对申请）：私钥只从当前项目加密记录读取，不经界面传输。
    pub async fn submit_pairing_request(
        &self,
        device_id: &str,
        pairing_code: &str,
        timeout: Duration,
    ) -> Result<GatewayResponse, String> {
        if pairing_code.len() != 6 || !pairing_code.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err("配对码必须是 6 位数字。".into());
        }
        let session = self.current_session().await?;
        if session.device_id != device_id {
            return Err("配对请求设备编号与当前 SSH 客户端身份不匹配。".into());
        }
        let private_key_text = load_session_signing_key(self, &session)?;
        if private_key_text.is_empty() || private_key_text.len() > 64 * 1024 {
            return Err("本机存储中的设备私钥为空或超过大小上限。".into());
        }
        let private_key = PrivateKey::from_openssh(private_key_text.as_str())
            .map_err(|_| "本机存储中的设备私钥无法读取。".to_string())?;
        let body = signed_pairing_request(&private_key, device_id, pairing_code)?;
        session
            .connection
            .post_json("/v1/pairing/request", &body, timeout)
            .await
    }

    /// disconnect（断开）：只关闭连接，不重试未结算业务任务。
    pub async fn disconnect(&self) -> Result<bool, String> {
        *self.active_lease.lock().await = None;
        let connection = self.connection.lock().await.take();
        match connection {
            Some(connection) => {
                connection.close().await?;
                Ok(true)
            }
            None => Ok(false),
        }
    }

    /// connected（连接状态）：只报告本机连接对象，不访问远端或续租。
    pub async fn connected(&self) -> bool {
        self.connection
            .lock()
            .await
            .as_ref()
            .is_some_and(|session| !session.connection.is_closed())
    }
}

fn pairing_proof_message(
    device_id: &str,
    fingerprint: &str,
    pairing_code: &str,
    client_nonce: &[u8; 24],
) -> Result<Vec<u8>, String> {
    if device_id.is_empty()
        || device_id.len() > 128
        || device_id.trim() != device_id
        || !device_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
        || !fingerprint.starts_with("SHA256:")
        || pairing_code.len() != 6
        || !pairing_code.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err("设备配对证明字段无效。".into());
    }
    let fields = [
        device_id.as_bytes(),
        fingerprint.as_bytes(),
        pairing_code.as_bytes(),
        client_nonce.as_slice(),
    ];
    let mut message = b"voice-terminal-device-pairing-v1\0".to_vec();
    for field in fields {
        let length = u32::try_from(field.len()).map_err(|_| "配对证明字段超过长度上限。")?;
        message.extend_from_slice(&length.to_be_bytes());
        message.extend_from_slice(field);
    }
    Ok(message)
}

fn signed_pairing_request(
    private_key: &PrivateKey,
    device_id: &str,
    pairing_code: &str,
) -> Result<serde_json::Value, String> {
    if private_key.algorithm() != Algorithm::Ed25519 || private_key.is_encrypted() {
        return Err("配对申请只支持未加密的本机 Ed25519 设备身份。".into());
    }
    let keypair = private_key
        .key_data()
        .ed25519()
        .ok_or_else(|| "系统凭据不是 Ed25519 设备身份。".to_string())?;
    let public_key = private_key
        .public_key()
        .to_openssh()
        .map_err(|_| "设备公钥编码失败。".to_string())?;
    let fingerprint = private_key
        .public_key()
        .fingerprint(HashAlg::Sha256)
        .to_string();
    let client_nonce = rand::random::<[u8; 24]>();
    let proof = pairing_proof_message(device_id, &fingerprint, pairing_code, &client_nonce)?;
    let signing_key = SigningKey::from_bytes(keypair.private.as_ref());
    let signature = signing_key.sign(&proof).to_bytes();
    Ok(serde_json::json!({
        "protocol_version": 1,
        "device_id": device_id,
        "public_key": public_key,
        "pairing_code": pairing_code,
        "client_nonce": base64::engine::general_purpose::STANDARD.encode(client_nonce),
        "signature": base64::engine::general_purpose::STANDARD.encode(signature),
    }))
}

fn random_hex_id() -> String {
    rand::random::<[u8; 16]>()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn unix_timestamp_ms() -> Result<u64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis().min(u128::from(u64::MAX)) as u64)
        .map_err(|_| "系统时间早于 Unix epoch，无法签署设备请求。".to_string())
}

fn append_canonical_json(value: &serde_json::Value, output: &mut Vec<u8>) -> Result<(), String> {
    use serde_json::Value;
    match value {
        Value::Null => output.extend_from_slice(b"null"),
        Value::Bool(true) => output.extend_from_slice(b"true"),
        Value::Bool(false) => output.extend_from_slice(b"false"),
        Value::Number(value) => output.extend_from_slice(value.to_string().as_bytes()),
        Value::String(value) => output.extend_from_slice(
            &serde_json::to_vec(value).map_err(|_| "设备请求 JSON 字符串编码失败。")?,
        ),
        Value::Array(values) => {
            output.push(b'[');
            for (index, item) in values.iter().enumerate() {
                if index > 0 {
                    output.push(b',');
                }
                append_canonical_json(item, output)?;
            }
            output.push(b']');
        }
        Value::Object(values) => {
            output.push(b'{');
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort();
            for (index, key) in keys.iter().enumerate() {
                if index > 0 {
                    output.push(b',');
                }
                output.extend_from_slice(
                    &serde_json::to_vec(key).map_err(|_| "设备请求 JSON 字段编码失败。")?,
                );
                output.push(b':');
                let child = values.get(*key).ok_or("设备请求 JSON 字段丢失。")?;
                append_canonical_json(child, output)?;
            }
            output.push(b'}');
        }
    }
    Ok(())
}

#[cfg(test)]
fn device_request_proof_message(
    device_id: &str,
    path: &str,
    core_session_id: &str,
    challenge: &str,
    timestamp_ms: u64,
    body: &serde_json::Value,
) -> Result<Vec<u8>, String> {
    device_request_proof_message_for_method(
        "POST",
        device_id,
        path,
        core_session_id,
        challenge,
        timestamp_ms,
        body,
    )
}

fn device_request_proof_message_for_method(
    method: &str,
    device_id: &str,
    path: &str,
    core_session_id: &str,
    challenge: &str,
    timestamp_ms: u64,
    body: &serde_json::Value,
) -> Result<Vec<u8>, String> {
    let device_id_ok = !device_id.is_empty()
        && device_id.len() <= 128
        && device_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'));
    let session_ok = core_session_id.len() == 32
        && core_session_id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase());
    let challenge_ok = (32..=128).contains(&challenge.len())
        && challenge
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'));
    let target_ok = (method == "POST"
        && matches!(
            path,
            "/v1/status"
                | "/v1/devices"
                | "/v1/session/open"
                | "/v1/session/poll"
                | "/v1/session/complete"
                | "/v1/session/reconcile"
                | "/v1/session/heartbeat"
                | "/v1/session/close"
                | "/v1/interaction/prepare"
                | "/v1/interaction/abort"
                | "/v1/management"
                | "/v1/file-transfer"
        ))
        || (method == "GET" && matches!(path, "/v1/media" | "/v1/voice"));
    if !device_id_ok || !target_ok || !session_ok || !challenge_ok {
        return Err("设备业务请求签名字段无效。".into());
    }
    let mut canonical_body = Vec::new();
    append_canonical_json(body, &mut canonical_body)?;
    let body_digest = Sha256::digest(canonical_body);
    let timestamp = timestamp_ms.to_string();
    let fields: [&[u8]; 7] = [
        method.as_bytes(),
        path.as_bytes(),
        device_id.as_bytes(),
        core_session_id.as_bytes(),
        challenge.as_bytes(),
        timestamp.as_bytes(),
        body_digest.as_slice(),
    ];
    let mut message = b"voice-terminal-device-request-v1\0".to_vec();
    for field in fields {
        let length = u32::try_from(field.len()).map_err(|_| "设备请求签名字段超过长度上限。")?;
        message.extend_from_slice(&length.to_be_bytes());
        message.extend_from_slice(field);
    }
    Ok(message)
}

fn valid_dsh_transfer_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_dsh_request_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn validate_sha256(value: &str) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("固定文件通道摘要必须是小写 SHA-256。".into());
    }
    Ok(())
}

fn validate_remote_session_lease(lease: &RemoteDeviceSessionLease) -> Result<(), String> {
    let device_id_ok = !lease.device_id.is_empty()
        && lease.device_id.len() <= 128
        && lease.device_id != "local"
        && lease
            .device_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'));
    let core_session_ok = lease.core_session_id.len() == 32
        && lease
            .core_session_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    let capabilities_valid = !lease.capabilities.is_empty()
        && lease.capabilities.len() <= 6
        && lease.capabilities.windows(2).all(|pair| pair[0] < pair[1])
        && lease.capabilities.iter().all(|value| {
            matches!(
                value.as_str(),
                "audio_stream"
                    | "client_tts"
                    | "desktop_control"
                    | "file_transfer"
                    | "screen_capture"
                    | "terminal_control"
            )
        });
    if lease.protocol_version != 1
        || lease.result != "accepted"
        || !device_id_ok
        || !core_session_ok
        || lease.connection_generation == 0
        || lease.executor_generation == 0
        || !capabilities_valid
    {
        return Err("远端设备会话租约字段或能力无效。".into());
    }
    Ok(())
}

fn validate_remote_device_task(
    task: &RemoteDeviceTask,
    lease: &RemoteDeviceSessionLease,
) -> Result<(), String> {
    let request_id_ok = !task.request_id.is_empty()
        && task.request_id.len() <= 128
        && task
            .request_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'));
    if !request_id_ok
        || task.task_id != task.request_id
        || task.device_id != lease.device_id
        || task.core_session_id != lease.core_session_id
        || task.connection_generation != lease.connection_generation
        || task.executor_generation != lease.executor_generation
        || task.sequence == 0
        || !REMOTE_DEVICE_OPERATIONS.contains(&task.operation.as_str())
        || !task.arguments.is_object()
        || serde_json::to_vec(&task.arguments).map_or(true, |encoded| encoded.len() > 256 * 1024)
    {
        return Err("远端桌面任务与固定设备、代次或操作白名单不匹配。".into());
    }
    Ok(())
}

fn sign_device_gateway_request(
    private_key: &PrivateKey,
    device_id: &str,
    path: &str,
    body: &serde_json::Value,
    challenge: &DeviceGatewayChallenge,
    timestamp_ms: u64,
) -> Result<DeviceGatewayAuthHeaders, String> {
    sign_device_gateway_request_for_method(
        private_key,
        "POST",
        device_id,
        path,
        body,
        challenge,
        timestamp_ms,
    )
}

fn sign_device_gateway_request_for_method(
    private_key: &PrivateKey,
    method: &str,
    device_id: &str,
    path: &str,
    body: &serde_json::Value,
    challenge: &DeviceGatewayChallenge,
    timestamp_ms: u64,
) -> Result<DeviceGatewayAuthHeaders, String> {
    if private_key.algorithm() != Algorithm::Ed25519 || private_key.is_encrypted() {
        return Err("设备业务签名只支持未加密的本机 Ed25519 身份。".into());
    }
    let keypair = private_key
        .key_data()
        .ed25519()
        .ok_or_else(|| "系统凭据不是 Ed25519 设备身份。".to_string())?;
    let message = device_request_proof_message_for_method(
        method,
        device_id,
        path,
        &challenge.core_session_id,
        &challenge.challenge,
        timestamp_ms,
        body,
    )?;
    let signing_key = SigningKey::from_bytes(keypair.private.as_ref());
    let signature = signing_key.sign(&message).to_bytes();
    Ok(DeviceGatewayAuthHeaders {
        device_id: device_id.to_string(),
        core_session_id: challenge.core_session_id.clone(),
        challenge: challenge.challenge.clone(),
        timestamp_ms,
        signature: base64::engine::general_purpose::STANDARD.encode(signature),
    })
}

fn validate_gateway_auth_headers(auth: &DeviceGatewayAuthHeaders) -> Result<(), String> {
    let device_id_ok = !auth.device_id.is_empty()
        && auth.device_id.len() <= 128
        && auth
            .device_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'));
    let session_ok = auth.core_session_id.len() == 32
        && auth
            .core_session_id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase());
    let challenge_ok = (32..=128).contains(&auth.challenge.len())
        && auth
            .challenge
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'));
    let signature_ok = base64::engine::general_purpose::STANDARD
        .decode(auth.signature.as_bytes())
        .is_ok_and(|value| value.len() == 64);
    if !device_id_ok || !session_ok || !challenge_ok || !signature_ok {
        return Err("设备请求授权头字段无效。".into());
    }
    Ok(())
}

fn encode_http_request(
    path: &str,
    body: &serde_json::Value,
    auth: Option<&DeviceGatewayAuthHeaders>,
) -> Result<Vec<u8>, String> {
    if path.len() > 512
        || !path.starts_with("/v1/")
        || !path
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'/' | b'_' | b'-' | b'.'))
    {
        return Err("SSH 业务网关只接受固定版本路径。".into());
    }
    let payload = serde_json::to_vec(body).map_err(|_| "SSH 业务请求不是有效 JSON。")?;
    if payload.len() > MAX_REQUEST_BYTES {
        return Err("SSH 业务请求超过大小上限。".into());
    }
    let authorization_headers = if let Some(auth) = auth {
        validate_gateway_auth_headers(auth)?;
        format!(
            "X-Voice-Terminal-Device-ID: {}\r\nX-Voice-Terminal-Core-Session-ID: {}\r\nX-Voice-Terminal-Challenge: {}\r\nX-Voice-Terminal-Timestamp-Ms: {}\r\nX-Voice-Terminal-Signature: {}\r\n",
            auth.device_id, auth.core_session_id, auth.challenge, auth.timestamp_ms, auth.signature,
        )
    } else {
        String::new()
    };
    let head = format!(
        "POST {path} HTTP/1.1\r\nHost: voice-terminal-device-gateway\r\nAccept: application/json\r\nContent-Type: application/json\r\n{authorization_headers}Connection: close\r\nContent-Length: {}\r\n\r\n",
        payload.len()
    );
    let mut request = Vec::with_capacity(head.len() + payload.len());
    request.extend_from_slice(head.as_bytes());
    request.extend_from_slice(&payload);
    Ok(request)
}

fn parse_http_response(response: &[u8]) -> Result<GatewayResponse, String> {
    let header_end = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| {
            format!(
                "SSH 业务回执缺少完整 HTTP 响应头（已收到 {} 字节）。",
                response.len(),
            )
        })?;
    if header_end > MAX_HEADER_BYTES || response.len() > MAX_HEADER_BYTES + MAX_RESPONSE_BYTES + 4 {
        return Err("SSH 业务回执超过大小上限。".into());
    }
    let header = std::str::from_utf8(&response[..header_end])
        .map_err(|_| "SSH 业务回执 HTTP 响应头编码无效。".to_string())?;
    let mut lines = header.split("\r\n");
    let status_line = lines
        .next()
        .ok_or_else(|| "SSH 业务回执缺少 HTTP 状态行。".to_string())?;
    let mut status_parts = status_line.split_ascii_whitespace();
    let version = status_parts.next().unwrap_or_default();
    let status = status_parts
        .next()
        .and_then(|value| value.parse::<u16>().ok())
        .filter(|value| (100..=599).contains(value))
        .ok_or_else(|| "SSH 业务回执 HTTP 状态码无效。".to_string())?;
    if !matches!(version, "HTTP/1.0" | "HTTP/1.1") || status_parts.next().is_none() {
        return Err("SSH 业务回执 HTTP 状态行格式无效。".into());
    }
    let mut content_length = None;
    let mut content_type = None;
    for line in lines {
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| "SSH 业务回执 HTTP 响应头格式无效。".to_string())?;
        let value = value.trim();
        if name.eq_ignore_ascii_case("content-length") {
            if content_length.is_some() {
                return Err("SSH 业务回执重复声明 HTTP 内容长度。".into());
            }
            content_length = Some(
                value
                    .parse::<usize>()
                    .map_err(|_| "SSH 业务回执 HTTP 内容长度无效。".to_string())?,
            );
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err("SSH 业务回执不接受分块 HTTP 编码。".into());
        } else if name.eq_ignore_ascii_case("content-encoding")
            && !value.eq_ignore_ascii_case("identity")
        {
            return Err("SSH 业务回执不接受压缩 HTTP 内容。".into());
        } else if name.eq_ignore_ascii_case("content-type") {
            if content_type.is_some() {
                return Err("SSH 业务回执重复声明媒体类型。".into());
            }
            content_type = Some(value.to_ascii_lowercase());
        }
    }
    let expected =
        content_length.ok_or_else(|| "SSH 业务回执未声明 HTTP 内容长度。".to_string())?;
    if expected > MAX_RESPONSE_BYTES {
        return Err("SSH 业务回执超过大小上限。".into());
    }
    let body_start = header_end + 4;
    let payload = response
        .get(body_start..)
        .filter(|payload| payload.len() == expected)
        .ok_or_else(|| "SSH 业务回执 HTTP 内容长度不匹配。".to_string())?;
    if !content_type
        .as_deref()
        .is_some_and(|value| value.starts_with("application/json"))
    {
        return Err("SSH 业务回执媒体类型不是 application/json。".into());
    }
    let body = if payload.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(payload).map_err(|_| "SSH 业务回执 JSON 内容无效。".to_string())?
    };
    Ok(GatewayResponse { status, body })
}

#[cfg(test)]
mod tests {
    use super::*;

    static PYTHON_GATEWAY_TEST_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

    #[tokio::test]
    async fn management_reuses_a_pinned_ssh_channel_and_rotates_only_after_lease_expiry() {
        assert_management_reuse(true).await;
    }

    #[tokio::test]
    async fn legacy_management_keeps_lease_identity_but_revalidates_each_new_channel() {
        assert_management_reuse(false).await;
    }

    async fn assert_management_reuse(persistent: bool) {
        use tokio::io::{AsyncBufReadExt, BufReader};
        use tokio::net::TcpListener;
        let gateway = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let gateway_address = gateway.local_addr().unwrap();
        let served = tokio::spawn(async move {
            let generations = if persistent {
                vec![7, 8]
            } else {
                vec![7, 7, 8]
            };
            let mut first_open = None;
            for generation in generations {
                let (stream, _) = gateway.accept().await.unwrap();
                let mut stream = BufReader::new(stream);
                let mut line = String::new();
                stream.read_line(&mut line).await.unwrap();
                let open: RemoteManagementOpenRequest = serde_json::from_str(&line).unwrap();
                if let Some(first) = &first_open {
                    assert_eq!(open.request_id == *first, generation == 7);
                } else {
                    first_open = Some(open.request_id.clone());
                }
                let opened = RemoteManagementOpenResponse {
                    protocol_version: 1,
                    request_id: open.request_id,
                    target_host_id: open.target_host_id,
                    client_device_id: open.client_device_id,
                    session_generation: generation,
                    expires_at_ms: unix_timestamp_ms().unwrap() + 900_000,
                    fields: BTreeMap::new(),
                };
                write_management_line(stream.get_mut(), &opened, MAX_RESPONSE_BYTES)
                    .await
                    .unwrap();
                // 前两次请求必须共用同一通道和同一代次；第三次在过期后使用新通道。
                for _ in 0..if persistent && generation == 7 { 2 } else { 1 } {
                    line.clear();
                    stream.read_line(&mut line).await.unwrap();
                    let request: RemoteManagementRequest = serde_json::from_str(&line).unwrap();
                    assert_eq!(request.session_generation, generation);
                    let response = RemoteManagementResponse {
                        protocol_version: 1,
                        request_id: request.request_id,
                        target_host_id: request.target_host_id,
                        session_generation: generation,
                        operation: request.operation,
                        status: 200,
                        body: serde_json::json!({"core_state":"ready","persistent_management":persistent}),
                        fields: BTreeMap::new(),
                    };
                    write_management_line(stream.get_mut(), &response, MAX_RESPONSE_BYTES)
                        .await
                        .unwrap();
                }
            }
        });
        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let host_config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            ..Default::default()
        });
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        let ssh_server = tokio::spawn(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let mut server = LoopbackForwardSshServer {
                gateway: gateway_address,
            };
            let running = russh::server::Server::run_on_socket(&mut server, host_config, &listener);
            ready_tx.send((address, running.handle())).ok();
            running.await
        });
        let (address, handle) = ready_rx.await.unwrap();
        let key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519)
            .unwrap()
            .to_openssh(LineEnding::LF)
            .unwrap();
        let manager = SshDeviceManager::default();
        manager
            .connect_with_test_credentials(
                "test-device",
                SshDeviceTarget {
                    address: "127.0.0.1".into(),
                    port: address.port(),
                    username: "test-device".into(),
                    host_key_sha256: fingerprint,
                    gateway_port: DEFAULT_GATEWAY_PORT,
                },
                key.as_str(),
                key.as_str(),
                Duration::from_secs(5),
            )
            .await
            .unwrap();
        for id in 1..=3 {
            if id == 3 {
                manager
                    .connection
                    .lock()
                    .await
                    .as_mut()
                    .unwrap()
                    .management
                    .lock().await
                    .as_mut()
                    .unwrap()
                    .management_open
                    .as_mut()
                    .unwrap()
                    .1 = std::time::Instant::now();
            }
            let result = manager
                .management_request(
                    &format!("{id:032x}"),
                    "server-a",
                    "management.status",
                    serde_json::json!({}),
                    Duration::from_secs(5),
                )
                .await
                .unwrap();
            assert_eq!(result.session_generation, if id == 3 { 8 } else { 7 });
        }
        served.await.unwrap();
        manager.disconnect().await.unwrap();
        handle.shutdown("management reuse test completed".into());
        ssh_server.await.unwrap().unwrap();
    }

    /// 回归：微软 Win32 OpenSSH 已报告同一连接上的后续命令可能沿用首个 SSH_ORIGINAL_COMMAND
    /// （原始命令标记）。管理与控制必须各用一条 SSH 连接，否则控制请求会被送进管理 JSON 解析。
    #[tokio::test]
    async fn control_requests_keep_their_lane_when_the_server_replays_the_first_command() {
        use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
        use tokio::net::TcpListener;

        const MANAGEMENT_MARKER_BYTES: &[u8] = b"voice-terminal-v1-management";
        const CORE_STATE_JSON: &[u8] = br#"{"core_state":"ready"}"#;

        // 控制网关：一次连接服务一个 HTTP（超文本传输协议）请求，回执后立即结束，与真实桥接收尾一致。
        let control = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let control_address = control.local_addr().unwrap();
        let control_hits = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let control_task = {
            let hits = control_hits.clone();
            tokio::spawn(async move {
                while let Ok((mut stream, _)) = control.accept().await {
                    hits.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    let mut request = Vec::new();
                    let mut chunk = [0_u8; 1024];
                    while !request.windows(4).any(|window| window == b"\r\n\r\n") {
                        match stream.read(&mut chunk).await {
                            Ok(0) | Err(_) => break,
                            Ok(read) => request.extend_from_slice(&chunk[..read]),
                        }
                    }
                    let header = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
                        CORE_STATE_JSON.len()
                    );
                    let _ = stream.write_all(header.as_bytes()).await;
                    let _ = stream.write_all(CORE_STATE_JSON).await;
                    let _ = stream.flush().await;
                }
            })
        };

        // 管理服务：一次连接先交换会话申请，再接收一个请求行；复用通道期间保持连接。
        let management = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let management_address = management.local_addr().unwrap();
        let management_hits = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let management_task = {
            let hits = management_hits.clone();
            tokio::spawn(async move {
                while let Ok((stream, _)) = management.accept().await {
                    hits.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    tokio::spawn(async move {
                        let mut stream = BufReader::new(stream);
                        let mut line = String::new();
                        if stream.read_line(&mut line).await.is_err() {
                            return;
                        }
                        let Ok(open) = serde_json::from_str::<serde_json::Value>(&line) else {
                            return;
                        };
                        let opened = serde_json::json!({
                            "protocol_version": 1,
                            "request_id": open["request_id"].clone(),
                            "target_host_id": open["target_host_id"].clone(),
                            "client_device_id": open["client_device_id"].clone(),
                            "session_generation": 7,
                            "expires_at_ms": unix_timestamp_ms().unwrap() + 900_000,
                            "fields": {},
                        });
                        let mut bytes = serde_json::to_vec(&opened).unwrap();
                        bytes.push(b'\n');
                        if stream.get_mut().write_all(&bytes).await.is_err() {
                            return;
                        }
                        line.clear();
                        if stream.read_line(&mut line).await.is_err() {
                            return;
                        }
                        let Ok(request) = serde_json::from_str::<serde_json::Value>(&line) else {
                            return;
                        };
                        let response = serde_json::json!({
                            "protocol_version": 1,
                            "request_id": request["request_id"].clone(),
                            "target_host_id": request["target_host_id"].clone(),
                            "session_generation": 7,
                            "operation": request["operation"].clone(),
                            "status": 200,
                            "body": {"core_state": "ready", "persistent_management": true},
                            "fields": {},
                        });
                        let mut bytes = serde_json::to_vec(&response).unwrap();
                        bytes.push(b'\n');
                        if stream.get_mut().write_all(&bytes).await.is_err() {
                            return;
                        }
                        // 保持复用通道直到对端结束，避免服务端提前关闭。
                        let mut sink = [0_u8; 256];
                        let _ = stream.read(&mut sink).await;
                    });
                }
            })
        };

        /// ReplayedMarkerSshServer（沿用首个标记的测试 SSH 服务）：按连接记录首个 exec 命令并重复使用。
        struct ReplayedMarkerSshServer {
            control: std::net::SocketAddr, // control（控制网关回环地址）。
            management: std::net::SocketAddr, // management（管理服务回环地址）。
            channels: Arc<
                tokio::sync::Mutex<
                    std::collections::HashMap<russh::ChannelId, russh::Channel<russh::server::Msg>>,
                >,
            >, // channels（本连接已接受的会话通道）。
            first_marker: Option<Vec<u8>>, // first_marker（本连接首个 exec 命令，后续沿用）。
        }

        impl russh::server::Server for ReplayedMarkerSshServer {
            type Handler = Self;

            fn new_client(&mut self, _peer: Option<std::net::SocketAddr>) -> Self::Handler {
                ReplayedMarkerSshServer {
                    control: self.control,
                    management: self.management,
                    channels: Arc::new(tokio::sync::Mutex::new(std::collections::HashMap::new())),
                    first_marker: None,
                }
            }
        }

        impl russh::server::Handler for ReplayedMarkerSshServer {
            type Error = russh::Error;

            async fn auth_publickey(
                &mut self,
                _user: &str,
                _key: russh::server::VerifiedPublicKey<'_>,
            ) -> Result<russh::server::Auth, Self::Error> {
                Ok(russh::server::Auth::Accept)
            }

            async fn channel_open_session(
                &mut self,
                channel: russh::Channel<russh::server::Msg>,
                reply: russh::server::ChannelOpenHandle,
                _session: &mut russh::server::Session,
            ) -> Result<(), Self::Error> {
                let channel_id = channel.id();
                self.channels.lock().await.insert(channel_id, channel);
                reply.accept().await;
                Ok(())
            }

            async fn exec_request(
                &mut self,
                channel_id: russh::ChannelId,
                command: &[u8],
                session: &mut russh::server::Session,
            ) -> Result<(), Self::Error> {
                if self.first_marker.is_none() {
                    self.first_marker = Some(command.to_vec());
                }
                let effective = self.first_marker.clone().unwrap_or_default();
                let target = if effective == MANAGEMENT_MARKER_BYTES {
                    self.management
                } else {
                    self.control
                };
                let Some(channel) = self.channels.lock().await.remove(&channel_id) else {
                    let _ = session.channel_failure(channel_id);
                    return Ok(());
                };
                let Ok(upstream) = tokio::net::TcpStream::connect(target).await else {
                    let _ = session.channel_failure(channel_id);
                    return Ok(());
                };
                let _ = session.channel_success(channel_id);
                tokio::spawn(async move {
                    let mut channel = channel.into_stream();
                    let mut upstream = upstream;
                    let _ = tokio::io::copy_bidirectional(&mut channel, &mut upstream).await;
                });
                Ok(())
            }
        }

        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let host_config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            ..Default::default()
        });
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        let ssh_server = tokio::spawn(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let mut server = ReplayedMarkerSshServer {
                control: control_address,
                management: management_address,
                channels: Arc::new(tokio::sync::Mutex::new(std::collections::HashMap::new())),
                first_marker: None,
            };
            let running = russh::server::Server::run_on_socket(&mut server, host_config, &listener);
            ready_tx.send((address, running.handle())).ok();
            running.await
        });
        let (address, handle) = ready_rx.await.unwrap();
        let key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519)
            .unwrap()
            .to_openssh(LineEnding::LF)
            .unwrap();
        let manager = SshDeviceManager::default();
        manager
            .connect_with_test_credentials(
                "test-device",
                SshDeviceTarget {
                    address: "127.0.0.1".into(),
                    port: address.port(),
                    username: "test-device".into(),
                    host_key_sha256: fingerprint,
                    gateway_port: DEFAULT_GATEWAY_PORT,
                },
                key.as_str(),
                key.as_str(),
                Duration::from_secs(5),
            )
            .await
            .unwrap();
        // 与现场一致：先建立管理通道，再在同一设备会话上发起核心控制请求。
        let management_result = manager
            .management_request(
                &format!("{:032x}", 1),
                "server-a",
                "management.status",
                serde_json::json!({}),
                Duration::from_secs(5),
            )
            .await
            .unwrap();
        assert_eq!(management_result.status, 200);
        assert_eq!(management_result.session_generation, 7);
        let control_result = {
            let mut slot = manager.connection.lock().await;
            slot.as_mut()
                .unwrap()
                .connection
                .post_json(
                    "/v1/status",
                    &serde_json::json!({}),
                    Duration::from_secs(5),
                )
                .await
                .unwrap()
        };
        assert_eq!(control_result.status, 200);
        assert_eq!(control_result.body["core_state"], "ready");
        assert_eq!(
            control_hits.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "控制请求必须到达控制网关"
        );
        assert_eq!(
            management_hits.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "管理标记不得被复用到控制通道"
        );
        manager.disconnect().await.unwrap();
        handle.shutdown("replayed marker test completed".into());
        ssh_server.await.unwrap().unwrap();
        control_task.abort();
        management_task.abort();
    }

    #[test]
    fn device_ssh_client_uses_bounded_keepalive_to_detect_lost_sessions() {
        let config = device_ssh_client_config();
        assert_eq!(config.keepalive_interval, Some(Duration::from_secs(15)));
        assert_eq!(config.keepalive_max, 3);
    }

    #[tokio::test]
    async fn manager_stops_reporting_connected_after_the_authenticated_transport_closes() {
        use tokio::net::TcpListener;

        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let host_fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let host_config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            ..Default::default()
        });
        let (address_tx, address_rx) = tokio::sync::oneshot::channel();
        let gateway = std::net::SocketAddr::from(([127, 0, 0, 1], DEFAULT_GATEWAY_PORT));
        let server_task = tokio::spawn(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await?;
            let address = listener.local_addr()?;
            let mut server = LoopbackForwardSshServer { gateway };
            let running = russh::server::Server::run_on_socket(&mut server, host_config, &listener);
            let handle = running.handle();
            let _ = address_tx.send((address, handle));
            running.await
        });
        let (address, ssh_handle) = address_rx.await.unwrap();

        let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap().to_string();
        let target = SshDeviceTarget {
            address: "127.0.0.1".into(),
            port: address.port(),
            username: "test-device".into(),
            host_key_sha256: host_fingerprint,
            gateway_port: DEFAULT_GATEWAY_PORT,
        };
        let connection = SshDeviceConnection::connect(
            target.clone(),
            login_key_text.as_str(),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        let manager = SshDeviceManager::default();
        *manager.connection.lock().await = Some(Arc::new(ConnectedDeviceSession {
            connection,
            management: tokio::sync::Mutex::new(None),
            device_id: "test-device".into(),
            identity_credential_ref: "test-only-device-key".into(),
            transport_credential_ref: "test-only-login-key".into(),
            target,
            signing_key_for_test: Some(Zeroizing::new(login_key_text.clone())),
            transport_key_for_test: Some(Zeroizing::new(login_key_text)),
        }));
        assert!(manager.connected().await);

        ssh_handle.shutdown("connection liveness test completed".into());
        server_task.await.unwrap().unwrap();
        for _ in 0..100 {
            if !manager.connected().await {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(!manager.connected().await);
    }

    #[test]
    fn media_ready_requires_complete_typed_codec_state() {
        assert!(media_ready_codecs(&serde_json::json!({"type":"media.ready"})).is_err());
        assert_eq!(
            media_ready_codecs(&serde_json::json!({
                "uplink_codec":"opus", "downlink_codec":"pcm", "encoder_pre_skip_samples":104,
                "downlink_encoder_pre_skip_samples":0,
            }))
            .unwrap(),
            (AudioCodec::Opus, AudioCodec::Pcm, 104, 0),
        );
        assert_eq!(
            media_ready_codecs(&serde_json::json!({
                "uplink_codec":"opus", "downlink_codec":"opus",
                "encoder_pre_skip_samples":104,"downlink_encoder_pre_skip_samples":312,
            }))
            .unwrap(),
            (AudioCodec::Opus, AudioCodec::Opus, 104, 312),
        );
        assert!(media_ready_codecs(&serde_json::json!({
            "uplink_codec":"pcm", "downlink_codec":"pcm",
        }))
        .is_err());
        assert!(media_ready_codecs(&serde_json::json!({
            "uplink_codec":7, "downlink_codec":"pcm", "encoder_pre_skip_samples":0,
            "downlink_encoder_pre_skip_samples":0,
        }))
        .is_err());
        assert!(media_ready_codecs(&serde_json::json!({
            "uplink_codec":"opus", "downlink_codec":"pcm", "encoder_pre_skip_samples":0,
            "downlink_encoder_pre_skip_samples":0,
        }))
        .is_err());
        assert!(media_ready_codecs(&serde_json::json!({
            "uplink_codec":"opus", "downlink_codec":"opus",
            "encoder_pre_skip_samples":104,"downlink_encoder_pre_skip_samples":0,
        }))
        .is_err());
    }

    #[test]
    fn python_stop_task_poll_fixture_deserializes_with_the_native_client_contract() {
        let poll: RemoteDeviceTaskPoll = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/device-playback-stop-task.json"
        ))
        .unwrap();
        assert_eq!(poll.protocol_version, 1);
        assert_eq!(poll.result, "accepted");
        assert_eq!(poll.device_id, "device-a");
        assert_eq!(poll.core_session_id, "playback-core");
        let task = poll.task.expect("停止播放任务应该存在");
        assert_eq!(task.request_id, "request-1");
        assert_eq!(task.task_id, "request-1");
        assert_eq!(task.connection_generation, 1);
        assert_eq!(task.executor_generation, 1);
        assert_eq!(task.sequence, 1);
        assert_eq!(task.operation, "playback.stop_and_snapshot");
        assert_eq!(task.arguments["playback_key"], "speech-key");
    }

    struct PythonGatewayProcess(std::process::Child); // child（本地短时测试服务进程）。

    impl Drop for PythonGatewayProcess {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    async fn start_python_gateway_test_server(public_key: &str) -> PythonGatewayProcess {
        use std::process::{Command, Stdio};
        use tokio::net::TcpStream;
        const TEST_CONTROL_PORT: u16 = 18_766; // TEST_CONTROL_PORT（仅限本机集成测试控制端口）。

        let repository = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
            .unwrap();
        let python = repository.join(".local/runtimes/python/python.exe");
        let fixture = repository.join("tests/fixtures/device-gateway-ssh-task-e2e-server.py");
        let mut child = Command::new(python)
            .args(["-B", "-X", "utf8"])
            .arg(fixture)
            .current_dir(repository)
            .env("VOICE_DEVICE_E2E_ID", "test-device")
            .env("VOICE_DEVICE_E2E_PUBLIC_KEY", public_key)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("本机 Python 设备网关测试服务应能启动");
        for _ in 0..200 {
            if let Some(status) = child.try_wait().expect("读取测试服务状态失败") {
                panic!("Python 设备网关测试服务提前退出：{status}");
            }
            let gateway_ready = TcpStream::connect(("127.0.0.1", DEFAULT_GATEWAY_PORT))
                .await
                .is_ok();
            let control_ready = TcpStream::connect(("127.0.0.1", TEST_CONTROL_PORT))
                .await
                .is_ok();
            if gateway_ready && control_ready {
                return PythonGatewayProcess(child);
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let _ = child.kill();
        let _ = child.wait();
        panic!("Python 设备网关测试服务未在限定时间内监听回环端口");
    }

    async fn call_python_gateway_test_route(method: &str, path: &str) -> GatewayResponse {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpStream;
        const TEST_CONTROL_PORT: u16 = 18_766; // TEST_CONTROL_PORT（仅限本机集成测试控制端口）。

        let mut stream = TcpStream::connect(("127.0.0.1", TEST_CONTROL_PORT))
            .await
            .unwrap();
        let request = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{TEST_CONTROL_PORT}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        stream.write_all(request.as_bytes()).await.unwrap();
        let mut response = Vec::new();
        tokio::time::timeout(Duration::from_secs(15), stream.read_to_end(&mut response))
            .await
            .expect("Python 测试网关控制接口应返回")
            .expect("读取 Python 测试网关回执失败");
        parse_http_response(&response).unwrap()
    }
    #[derive(Clone)]
    struct LoopbackForwardSshServer {
        gateway: std::net::SocketAddr, // gateway（仅测试用的回环网关地址）。
    }

    impl russh::server::Server for LoopbackForwardSshServer {
        type Handler = Self;

        fn new_client(&mut self, _peer: Option<std::net::SocketAddr>) -> Self::Handler {
            self.clone()
        }
    }

    impl russh::server::Handler for LoopbackForwardSshServer {
        type Error = russh::Error;

        async fn auth_publickey(
            &mut self,
            _user: &str,
            _key: russh::server::VerifiedPublicKey<'_>,
        ) -> Result<russh::server::Auth, Self::Error> {
            Ok(russh::server::Auth::Accept)
        }

        async fn channel_open_session(
            &mut self,
            channel: russh::Channel<russh::server::Msg>,
            reply: russh::server::ChannelOpenHandle,
            _session: &mut russh::server::Session,
        ) -> Result<(), Self::Error> {
            let gateway = self.gateway;
            reply.accept().await;
            tokio::spawn(async move {
                let Ok(mut target) = tokio::net::TcpStream::connect(gateway).await else {
                    return;
                };
                let mut channel = channel.into_stream();
                let _ = tokio::io::copy_bidirectional(&mut channel, &mut target).await;
            });
            Ok(())
        }
    }

    #[tokio::test]
    async fn pairing_approval_and_revocation_round_trip_over_a_pinned_local_ssh_tunnel() {
        use tokio::net::TcpListener;
        use tokio::sync::oneshot;

        let _gateway_lock = PYTHON_GATEWAY_TEST_LOCK.lock().await;
        let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
        let public_key = product_key.public_key().to_openssh().unwrap();
        let mut gateway = start_python_gateway_test_server(&public_key).await;
        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            inactivity_timeout: Some(Duration::from_secs(30)),
            auth_rejection_time: Duration::ZERO,
            auth_rejection_time_initial: Some(Duration::ZERO),
            ..Default::default()
        });
        let (address_tx, address_rx) = oneshot::channel();
        let ssh_task = tokio::spawn(async move {
            let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
            let address = listener.local_addr()?;
            let mut server = LoopbackForwardSshServer {
                gateway: ([127, 0, 0, 1], DEFAULT_GATEWAY_PORT).into(),
            };
            let running = russh::server::Server::run_on_socket(&mut server, config, &listener);
            let _ = address_tx.send((address, running.handle()));
            running.await
        });
        let (address, ssh_handle) = address_rx.await.unwrap();
        let target = SshDeviceTarget {
            address: "127.0.0.1".into(),
            port: address.port(),
            username: "test-device".into(),
            host_key_sha256: fingerprint,
            gateway_port: DEFAULT_GATEWAY_PORT,
        };
        let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
        let mut wrong_target = target.clone();
        wrong_target.host_key_sha256 = format!("SHA256:{}", "A".repeat(43));
        assert!(SshDeviceConnection::connect(
            wrong_target,
            &login_key_text,
            Duration::from_secs(5)
        )
        .await
        .is_err());

        let manager = SshDeviceManager::default();
        manager
            .connect_with_test_credentials(
                "test-device",
                target,
                &product_key_text,
                &login_key_text,
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        let opened = call_python_gateway_test_route("POST", "/__test__/pairing-open").await;
        assert_eq!(opened.status, 200);
        let code = opened.body["pairing_code"].as_str().unwrap();
        let status_body = serde_json::json!({"protocol_version": 1});
        let denied = manager
            .post_json("/v1/status", &status_body, Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(denied.status, 401, "SSH 登录不应自动取得产品信任");
        let wrong_code = if code == "000000" { "000001" } else { "000000" };
        let refused = manager
            .submit_pairing_request("test-device", wrong_code, Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(refused.status, 403);
        let submitted = manager
            .submit_pairing_request("test-device", code, Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(submitted.status, 202);
        assert_eq!(submitted.body["device_id"], "test-device");
        assert!(submitted.body.get("public_key").is_none());
        assert!(submitted.body.get("pairing_code").is_none());
        let pending = manager
            .post_json("/v1/status", &status_body, Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(pending.status, 401, "申请成功后仍须主机明确批准");
        let approved = call_python_gateway_test_route("POST", "/__test__/pairing-approve").await;
        assert_eq!(approved.status, 200);
        let trusted = manager
            .post_json("/v1/status", &status_body, Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(trusted.status, 200);
        assert_eq!(trusted.body["ready"], true);
        let lease = manager
            .open_remote_device_session_with_capabilities(
                vec!["file_transfer".into()],
                Duration::from_secs(5),
            )
            .await
            .unwrap();
        assert_eq!(lease.device_id, "test-device");
        assert!(manager
            .close_remote_device_session(&lease, Duration::from_secs(5))
            .await
            .unwrap());
        let revoked = call_python_gateway_test_route("POST", "/__test__/pairing-revoke").await;
        assert_eq!(revoked.status, 200);
        let denied = manager
            .post_json("/v1/status", &status_body, Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(denied.status, 401, "撤销后不得继续使用产品接口");
        manager.disconnect().await.unwrap();
        ssh_handle.shutdown("local pairing acceptance complete".into());
        tokio::time::timeout(Duration::from_secs(10), ssh_task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(
            call_python_gateway_test_route("POST", "/__test__/shutdown")
                .await
                .status,
            200
        );
        for _ in 0..100 {
            if gateway.0.try_wait().unwrap().is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(
            gateway.0.try_wait().unwrap().is_some(),
            "本地配对测试网关须正常退出"
        );
    }

    #[tokio::test]
    async fn stop_task_channel_polls_executes_and_completes_over_a_pinned_ssh_tunnel() {
        use tokio::net::TcpListener;
        use tokio::sync::oneshot;

        let _gateway_lock = PYTHON_GATEWAY_TEST_LOCK.lock().await;
        let gateway_probe = TcpListener::bind(("127.0.0.1", DEFAULT_GATEWAY_PORT))
            .await
            .expect("本机测试网关默认端口必须空闲");
        drop(gateway_probe);
        let control_probe = TcpListener::bind(("127.0.0.1", 18_766))
            .await
            .expect("本机测试控制端口必须空闲");
        drop(control_probe);

        let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
        let public_key = product_key.public_key().to_openssh().unwrap();
        let mut python_gateway = start_python_gateway_test_server(&public_key).await;
        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let host_fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let host_config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            inactivity_timeout: Some(Duration::from_secs(30)),
            auth_rejection_time: Duration::ZERO,
            auth_rejection_time_initial: Some(Duration::ZERO),
            ..Default::default()
        });
        let (address_tx, address_rx) = oneshot::channel();
        let ssh_gateway = std::net::SocketAddr::from(([127, 0, 0, 1], DEFAULT_GATEWAY_PORT));
        let ssh_task = tokio::spawn(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await?;
            let address = listener.local_addr()?;
            let mut server = LoopbackForwardSshServer {
                gateway: ssh_gateway,
            };
            let running = russh::server::Server::run_on_socket(&mut server, host_config, &listener);
            let handle = running.handle();
            let _ = address_tx.send((address, handle));
            running.await
        });
        let (ssh_address, ssh_handle) = address_rx.await.unwrap();
        let target = SshDeviceTarget {
            address: "127.0.0.1".into(),
            port: ssh_address.port(),
            username: "test-device".into(),
            host_key_sha256: host_fingerprint,
            gateway_port: DEFAULT_GATEWAY_PORT,
        };
        let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
        let connection = SshDeviceConnection::connect(
            target.clone(),
            login_key_text.as_str(),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        let manager = SshDeviceManager::default();
        *manager.connection.lock().await = Some(Arc::new(ConnectedDeviceSession {
            connection,
            management: tokio::sync::Mutex::new(None),
            device_id: "test-device".into(),
            identity_credential_ref: "test-only-device-key".into(),
            transport_credential_ref: "test-only-login-key".into(),
            target,
            signing_key_for_test: Some(Zeroizing::new(product_key_text.to_string())),
            transport_key_for_test: Some(Zeroizing::new(login_key_text.to_string())),
        }));

        let lease = manager
            .open_remote_device_session_with_capabilities(
                vec!["audio_stream".into(), "client_tts".into()],
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        assert_eq!(lease.device_id, "test-device");
        assert_eq!(lease.capabilities, ["audio_stream", "client_tts"]);
        assert!(lease.separate_playback_tasks);
        assert_eq!(lease.connection_generation, 1);
        assert_eq!(lease.executor_generation, 1);
        let dispatch = call_python_gateway_test_route("POST", "/__test__/dispatch-stop").await;
        assert_eq!(dispatch.status, 200, "{dispatch:?}");

        let stop = Arc::new(AtomicBool::new(false));
        let stop_after_execute = stop.clone();
        let execute_count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let execute_count_ref = execute_count.clone();
        let screen_started = Arc::new(tokio::sync::Notify::new());
        let screen_can_finish = Arc::new(tokio::sync::Notify::new());
        manager
            .run_remote_device_task_channel_for_lease(lease, stop, move |task| {
                let stop = stop_after_execute.clone();
                let execute_count = execute_count_ref.clone();
                let screen_started = screen_started.clone();
                let screen_can_finish = screen_can_finish.clone();
                async move {
                    if task.operation == "screen.capture_file" {
                        screen_started.notify_one();
                        tokio::time::timeout(Duration::from_secs(5), screen_can_finish.notified())
                            .await.expect("截图执行期间播报停止任务应能独立到达");
                        return Ok(RemoteDeviceTaskResult {
                            succeeded: true,
                            result: serde_json::json!({"fields":{}}),
                        });
                    }
                    assert_eq!(task.operation, "playback.stop_and_snapshot");
                    tokio::time::timeout(Duration::from_secs(5), screen_started.notified())
                        .await.expect("普通任务与播报任务应并行领取");
                    assert_eq!(task.arguments["playback_key"], "speech-playback-e2e");
                    assert_eq!(task.connection_generation, 1);
                    assert_eq!(task.executor_generation, 1);
                    execute_count.fetch_add(1, Ordering::AcqRel);
                    screen_can_finish.notify_one();
                    stop.store(true, Ordering::Release);
                    Ok(RemoteDeviceTaskResult {
                        succeeded: true,
                        result: serde_json::json!({"playback_key":"speech-playback-e2e",
                        "enqueued_ms":420,"played_ms":280,"buffered_ms":140,
                        "output_estimated_ms":240,"started":true,
                        "device_time_us":9_000_000,"precision":"device_time"}),
                    })
                }
            })
            .await
            .unwrap();

        assert_eq!(execute_count.load(Ordering::Acquire), 1);
        manager.disconnect().await.unwrap();
        ssh_handle.shutdown("integration test completed".into());
        ssh_task.await.unwrap().unwrap();

        let status = loop {
            let response = call_python_gateway_test_route("GET", "/__test__/stop-status").await;
            if response.status != 202 {
                break response;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        };
        assert_eq!(status.status, 200, "{status:?}");
        assert_eq!(status.body["state"], "succeeded");
        assert_eq!(
            status.body["snapshot"]["playback_key"],
            "speech-playback-e2e"
        );
        assert_eq!(status.body["snapshot"]["played_ms"], 280);
        assert_eq!(status.body["snapshot"]["device_time_us"], 9_000_000);
        let stopped = call_python_gateway_test_route("POST", "/__test__/shutdown").await;
        assert_eq!(stopped.status, 200);
        for _ in 0..100 {
            if python_gateway.0.try_wait().unwrap().is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(
            python_gateway.0.try_wait().unwrap().is_some(),
            "Python 设备网关应已退出"
        );
    }
    #[tokio::test]
    async fn replacement_transactions_round_trip_over_a_pinned_ssh_task_channel() {
        use tokio::net::TcpListener;
        use tokio::sync::oneshot;

        let _gateway_lock = PYTHON_GATEWAY_TEST_LOCK.lock().await;
        let gateway_probe = TcpListener::bind(("127.0.0.1", DEFAULT_GATEWAY_PORT))
            .await
            .unwrap();
        drop(gateway_probe);
        let control_probe = TcpListener::bind(("127.0.0.1", 18_766)).await.unwrap();
        drop(control_probe);

        let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
        let public_key = product_key.public_key().to_openssh().unwrap();
        let mut python_gateway = start_python_gateway_test_server(&public_key).await;
        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let host_fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let host_config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            inactivity_timeout: Some(Duration::from_secs(30)),
            auth_rejection_time: Duration::ZERO,
            auth_rejection_time_initial: Some(Duration::ZERO),
            ..Default::default()
        });
        let (address_tx, address_rx) = oneshot::channel();
        let ssh_gateway = std::net::SocketAddr::from(([127, 0, 0, 1], DEFAULT_GATEWAY_PORT));
        let ssh_task = tokio::spawn(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await?;
            let address = listener.local_addr()?;
            let mut server = LoopbackForwardSshServer {
                gateway: ssh_gateway,
            };
            let running = russh::server::Server::run_on_socket(&mut server, host_config, &listener);
            let handle = running.handle();
            let _ = address_tx.send((address, handle));
            running.await
        });
        let (ssh_address, ssh_handle) = address_rx.await.unwrap();
        let target = SshDeviceTarget {
            address: "127.0.0.1".into(),
            port: ssh_address.port(),
            username: "test-device".into(),
            host_key_sha256: host_fingerprint,
            gateway_port: DEFAULT_GATEWAY_PORT,
        };
        let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
        let connection = SshDeviceConnection::connect(
            target.clone(),
            login_key_text.as_str(),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        let manager = SshDeviceManager::default();
        *manager.connection.lock().await = Some(Arc::new(ConnectedDeviceSession {
            connection,
            management: tokio::sync::Mutex::new(None),
            device_id: "test-device".into(),
            identity_credential_ref: "test-only-device-key".into(),
            transport_credential_ref: "test-only-login-key".into(),
            target,
            signing_key_for_test: Some(Zeroizing::new(product_key_text.to_string())),
            transport_key_for_test: Some(Zeroizing::new(login_key_text.to_string())),
        }));
        let lease = manager
            .open_remote_device_session_with_capabilities(
                vec!["audio_stream".into()],
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        let dispatch =
            call_python_gateway_test_route("POST", "/__test__/dispatch-replacement").await;
        assert_eq!(dispatch.status, 200, "{dispatch:?}");

        let stop = Arc::new(AtomicBool::new(false));
        let stop_after_wait = stop.clone();
        let replacement_id = Arc::new(std::sync::Mutex::new(None::<String>));
        let replacement_id_for_task = replacement_id.clone();
        let task_count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let task_count_for_task = task_count.clone();
        manager.run_remote_device_task_channel_for_lease(lease, stop, move |task| {
            let stop = stop_after_wait.clone();
            let replacement_id = replacement_id_for_task.clone();
            let task_count = task_count_for_task.clone();
            async move {
                assert_eq!(task.device_id, "test-device");
                assert_eq!(task.connection_generation, 1);
                assert_eq!(task.executor_generation, 1);
                task_count.fetch_add(1, Ordering::AcqRel);
                match task.operation.as_str() {
                    "playback.prepare_replacement" => {
                        assert_eq!(task.arguments, serde_json::json!({"playback_key":"speech-old","max_ms":300}));
                        *replacement_id.lock().unwrap() = Some(task.task_id.clone());
                        Ok(RemoteDeviceTaskResult { succeeded:true, result:serde_json::json!({
                            "replacement_id":task.task_id,"old_playback_key":"speech-old",
                            "prepared_device_time_us":9_000_000,
                            "snapshot":{"playback_key":"speech-old","enqueued_ms":500,"played_ms":120,
                                "buffered_ms":380,"output_estimated_ms":120,"started":true,
                                "device_time_us":9_000_000,"precision":"device_time"},
                        }) })
                    }
                    "playback.bind_replacement" => {
                        assert_eq!(task.arguments["replacement_id"].as_str(), replacement_id.lock().unwrap().as_deref());
                        assert_eq!(task.arguments["playback_key"], "speech-new");
                        Ok(RemoteDeviceTaskResult { succeeded:true, result:serde_json::json!({"bound":true}) })
                    }
                    "playback.wait_replacement" => {
                        assert_eq!(task.arguments["replacement_id"].as_str(), replacement_id.lock().unwrap().as_deref());
                        stop.store(true, Ordering::Release);
                        Ok(RemoteDeviceTaskResult { succeeded:true, result:serde_json::json!({
                            "replacement_id":task.arguments["replacement_id"],"reason":"early_handoff",
                            "started_device_time_us":9_000_000,"finished_device_time_us":9_240_000,
                            "events_device_time_us":{"cancel_accepted":9_000_000,"old_audio_silenced":9_240_000},
                            "attenuated_range_ms":[120,300],
                            "snapshot":{"playback_key":"speech-old","enqueued_ms":500,"played_ms":300,
                                "buffered_ms":0,"output_estimated_ms":300,"started":true,
                                "device_time_us":9_240_000,"precision":"device_time"},
                        }) })
                    }
                    operation => panic!("unexpected replacement operation: {operation}"),
                }
            }
        }).await.unwrap();
        assert_eq!(task_count.load(Ordering::Acquire), 3);
        manager.disconnect().await.unwrap();
        ssh_handle.shutdown("replacement integration test completed".into());
        ssh_task.await.unwrap().unwrap();

        let status = loop {
            let response =
                call_python_gateway_test_route("GET", "/__test__/replacement-status").await;
            if response.status != 202 {
                break response;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        };
        assert_eq!(status.status, 200, "{status:?}");
        assert_eq!(status.body["state"], "succeeded");
        assert_eq!(status.body["replacement"]["bound"], true);
        assert_eq!(
            status.body["replacement"]["ticket"]["replacement_id"].as_str(),
            replacement_id.lock().unwrap().as_deref()
        );
        assert_eq!(
            status.body["replacement"]["result"]["reason"],
            "early_handoff"
        );
        assert_eq!(
            status.body["replacement"]["result"]["events_device_time_us"]["old_audio_silenced"],
            9_240_000
        );
        assert_eq!(
            call_python_gateway_test_route("POST", "/__test__/shutdown")
                .await
                .status,
            200
        );
        for _ in 0..100 {
            if python_gateway.0.try_wait().unwrap().is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(
            python_gateway.0.try_wait().unwrap().is_some(),
            "Python 设备网关应已退出"
        );
    }

    #[tokio::test]
    async fn durable_client_receipt_reconciles_the_original_unknown_task_after_reconnect() {
        use crate::device_receipts::DeviceTaskReceiptJournal;
        use tokio::net::TcpListener;
        use tokio::sync::oneshot;

        let _gateway_lock = PYTHON_GATEWAY_TEST_LOCK.lock().await;
        let gateway_probe = TcpListener::bind(("127.0.0.1", DEFAULT_GATEWAY_PORT))
            .await
            .unwrap();
        drop(gateway_probe);
        let control_probe = TcpListener::bind(("127.0.0.1", 18_766)).await.unwrap();
        drop(control_probe);

        let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
        let public_key = product_key.public_key().to_openssh().unwrap();
        let mut python_gateway = start_python_gateway_test_server(&public_key).await;
        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let host_fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let host_config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            inactivity_timeout: Some(Duration::from_secs(30)),
            auth_rejection_time: Duration::ZERO,
            auth_rejection_time_initial: Some(Duration::ZERO),
            ..Default::default()
        });
        let (address_tx, address_rx) = oneshot::channel();
        let ssh_gateway = std::net::SocketAddr::from(([127, 0, 0, 1], DEFAULT_GATEWAY_PORT));
        let ssh_task = tokio::spawn(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await?;
            let address = listener.local_addr()?;
            let mut server = LoopbackForwardSshServer {
                gateway: ssh_gateway,
            };
            let running = russh::server::Server::run_on_socket(&mut server, host_config, &listener);
            let handle = running.handle();
            let _ = address_tx.send((address, handle));
            running.await
        });
        let (ssh_address, ssh_handle) = address_rx.await.unwrap();
        let target = SshDeviceTarget {
            address: "127.0.0.1".into(),
            port: ssh_address.port(),
            username: "test-device".into(),
            host_key_sha256: host_fingerprint,
            gateway_port: DEFAULT_GATEWAY_PORT,
        };
        let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
        let connection = SshDeviceConnection::connect(
            target.clone(),
            login_key_text.as_str(),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        let manager = SshDeviceManager::default();
        *manager.connection.lock().await = Some(Arc::new(ConnectedDeviceSession {
            connection,
            management: tokio::sync::Mutex::new(None),
            device_id: "test-device".into(),
            identity_credential_ref: "test-only-device-key".into(),
            transport_credential_ref: "test-only-login-key".into(),
            target,
            signing_key_for_test: Some(Zeroizing::new(product_key_text.to_string())),
            transport_key_for_test: Some(Zeroizing::new(login_key_text.to_string())),
        }));
        let first_lease = manager
            .open_remote_device_session_with_capabilities(
                vec!["audio_stream".into()],
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        let dispatch =
            call_python_gateway_test_route("POST", "/__test__/dispatch-receipt-reconciliation")
                .await;
        assert_eq!(dispatch.status, 200, "{dispatch:?}");
        let task = loop {
            if let Some(task) = manager
                .poll_remote_device_task(&first_lease, 1000, Duration::from_secs(8))
                .await
                .unwrap()
            {
                break task;
            }
        };
        assert_eq!(task.task_id, "receipt-reconcile-e2e");
        assert_eq!(
            task.connection_generation,
            first_lease.connection_generation
        );
        let original_result = RemoteDeviceTaskResult {
            succeeded: true,
            result: serde_json::json!({"playback_key":"speech-reconcile-e2e","played_ms":0,
                "enqueued_ms":0,"buffered_ms":0,"output_estimated_ms":0,"started":false,
                "device_time_us":10_500_000,"precision":"unknown"}),
        };
        let journal_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp")
            .join(format!("receipt-reconcile-{}", crate::workspace::new_id()));
        let receipt_journal = DeviceTaskReceiptJournal::new(journal_root.clone());
        let persisted_receipt = receipt_journal.record(&task, &original_result).unwrap();
        let mut stale_generation_receipt = persisted_receipt.clone();
        stale_generation_receipt.sequence += 1;
        assert!(manager
            .reconcile_remote_device_receipt(
                &first_lease,
                &stale_generation_receipt,
                Duration::from_secs(10),
            )
            .await
            .is_err());
        assert_eq!(
            receipt_journal
                .pending_for_device("test-device")
                .unwrap()
                .len(),
            1
        );
        assert!(manager
            .close_remote_device_session(&first_lease, Duration::from_secs(5))
            .await
            .unwrap());

        let unknown = loop {
            let response =
                call_python_gateway_test_route("GET", "/__test__/receipt-reconciliation-status")
                    .await;
            if response.status == 200 && response.body["state"] == "unknown" {
                break response;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        };
        assert_eq!(unknown.body["task_id"], task.task_id);
        assert_eq!(unknown.body["run_id"], first_lease.core_session_id);

        let second_lease = manager
            .open_remote_device_session_with_capabilities(
                vec!["audio_stream".into()],
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        assert_ne!(
            second_lease.connection_generation,
            first_lease.connection_generation
        );
        let stop = Arc::new(AtomicBool::new(true));
        manager
            .run_remote_device_task_channel_for_lease_with_receipts(
                second_lease.clone(),
                stop,
                &receipt_journal,
                move |_task| async move {
                    Err::<RemoteDeviceTaskResult, String>("恢复流程不应领取或重放新副作用".into())
                },
            )
            .await
            .unwrap();
        assert!(receipt_journal
            .pending_for_device("test-device")
            .unwrap()
            .is_empty());
        assert_eq!(
            manager
                .reconcile_remote_device_receipt(
                    &second_lease,
                    &persisted_receipt,
                    Duration::from_secs(10),
                )
                .await
                .unwrap(),
            "already_completed"
        );
        manager.disconnect().await.unwrap();
        ssh_handle.shutdown("receipt reconciliation test completed".into());
        ssh_task.await.unwrap().unwrap();

        let settled =
            call_python_gateway_test_route("GET", "/__test__/receipt-reconciliation-status").await;
        assert_eq!(settled.status, 200, "{settled:?}");
        assert_eq!(settled.body["state"], "succeeded");
        assert_eq!(settled.body["result"]["played_ms"], 0);
        assert_eq!(
            call_python_gateway_test_route("POST", "/__test__/shutdown")
                .await
                .status,
            200
        );
        for _ in 0..100 {
            if python_gateway.0.try_wait().unwrap().is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(
            python_gateway.0.try_wait().unwrap().is_some(),
            "Python 设备网关应已退出"
        );
        let _ = std::fs::remove_dir_all(journal_root);
    }

    #[tokio::test]
    async fn tts_pcm_reaches_the_python_gateway_over_the_signed_ssh_media_websocket() {
        use tokio::net::TcpListener;
        use tokio::sync::oneshot;

        let _gateway_lock = PYTHON_GATEWAY_TEST_LOCK.lock().await;
        let gateway_probe = TcpListener::bind(("127.0.0.1", DEFAULT_GATEWAY_PORT))
            .await
            .expect("本机测试网关默认端口必须空闲");
        drop(gateway_probe);
        let control_probe = TcpListener::bind(("127.0.0.1", 18_766))
            .await
            .expect("本机测试控制端口必须空闲");
        drop(control_probe);

        let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
        let public_key = product_key.public_key().to_openssh().unwrap();
        let mut python_gateway = start_python_gateway_test_server(&public_key).await;
        let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let host_fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let host_config = Arc::new(russh::server::Config {
            keys: vec![host_key],
            inactivity_timeout: Some(Duration::from_secs(30)),
            auth_rejection_time: Duration::ZERO,
            auth_rejection_time_initial: Some(Duration::ZERO),
            ..Default::default()
        });
        let (address_tx, address_rx) = oneshot::channel();
        let gateway = std::net::SocketAddr::from(([127, 0, 0, 1], DEFAULT_GATEWAY_PORT));
        let ssh_task = tokio::spawn(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await?;
            let address = listener.local_addr()?;
            let mut server = LoopbackForwardSshServer { gateway };
            let running = russh::server::Server::run_on_socket(&mut server, host_config, &listener);
            let handle = running.handle();
            let _ = address_tx.send((address, handle));
            running.await
        });
        let (ssh_address, ssh_handle) = address_rx.await.unwrap();
        let target = SshDeviceTarget {
            address: "127.0.0.1".into(),
            port: ssh_address.port(),
            username: "test-device".into(),
            host_key_sha256: host_fingerprint,
            gateway_port: DEFAULT_GATEWAY_PORT,
        };
        let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
        let connection = SshDeviceConnection::connect(
            target.clone(),
            login_key_text.as_str(),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        let manager = SshDeviceManager::default();
        *manager.connection.lock().await = Some(Arc::new(ConnectedDeviceSession {
            connection,
            management: tokio::sync::Mutex::new(None),
            device_id: "test-device".into(),
            identity_credential_ref: "test-only-device-key".into(),
            transport_credential_ref: "test-only-login-key".into(),
            target,
            signing_key_for_test: Some(Zeroizing::new(product_key_text.to_string())),
            transport_key_for_test: Some(Zeroizing::new(login_key_text.to_string())),
        }));
        let lease = manager
            .open_remote_device_session_with_capabilities(
                vec!["audio_stream".into()],
                Duration::from_secs(10),
            )
            .await
            .unwrap();

        let prepared = manager
            .post_json(
                "/v1/interaction/prepare",
                &serde_json::json!({
                    "protocol_version":1,
                    "connection_generation":lease.connection_generation,
                }),
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        assert_eq!(prepared.status, 200, "{prepared:?}");
        assert_eq!(prepared.body["result"], "pending_media");
        let interaction_generation = prepared.body["interaction_generation"].as_u64().unwrap();

        let mut encoder = crate::native_opus::OpusEncoder::new(16_000, None).unwrap();
        let uplink_pre_skip_samples = encoder.lookahead_samples();
        let mut media = manager
            .open_media_channel_with_codecs(
                "ssh-media-tts-e2e",
                lease.connection_generation,
                lease.executor_generation,
                interaction_generation,
                AudioCodec::Opus,
                AudioCodec::Pcm,
                uplink_pre_skip_samples,
                0,
                Duration::from_secs(10),
            )
            .await
            .unwrap();
        assert_eq!(media.device_id, "test-device");
        assert_eq!(media.stream_id, "ssh-media-tts-e2e");
        assert_eq!(media.uplink_codec, AudioCodec::Opus);
        assert_eq!(media.encoder_pre_skip_samples, uplink_pre_skip_samples);
        assert_eq!(media.downlink_encoder_pre_skip_samples, 0);
        let device_clock_us = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let clock_sync = device_clock_us.clone();
        media
            .synchronize_clock(move || {
                let sample = clock_sync.fetch_add(10_000, Ordering::AcqRel) + 10_000;
                async move { Ok(sample) }
            })
            .await
            .unwrap();
        assert!(media.clock_uncertainty_us.is_some());
        assert!(media.capture_active);

        for sequence in 0..2_u64 {
            let samples = (0..320_u64)
                .map(|index| {
                    (6_000.0_f64
                        * (2.0 * std::f64::consts::PI * 440.0 * (sequence * 320 + index) as f64
                            / 16_000.0)
                            .sin()) as i16
                })
                .collect::<Vec<_>>();
            let packet = encoder.encode(&samples).unwrap();
            let device_time_us = device_clock_us.fetch_add(20_000, Ordering::AcqRel) + 20_000;
            let header = crate::device_protocol::MediaHeader {
                protocol_version: MEDIA_PROTOCOL_VERSION,
                device_id: media.device_id.clone(),
                core_session_id: media.core_session_id.clone(),
                connection_generation: media.connection_generation,
                executor_generation: media.executor_generation,
                interaction_generation: media.interaction_generation,
                stream_id: media.stream_id.clone(),
                sequence,
                start_sample: sequence * 320,
                sample_count: 320,
                device_time_us,
                audio_codec: AudioCodec::Opus,
                discontinuity_before: false,
                audio_processing: crate::device_protocol::AudioProcessingMetadata {
                    state: crate::device_protocol::AudioProcessingHealth::Ready,
                    near_end_voice: crate::device_protocol::NullableMetadata(Some(false)),
                    protected_interruption_available: false,
                    reverse_generation: 0,
                    error: crate::device_protocol::NullableMetadata(None),
                    fields: BTreeMap::from([
                        ("error".into(), "设备前处理故障摘要".into()),
                        ("near_end_voice".into(), "近端语音结论".into()),
                        (
                            "protected_interruption_available".into(),
                            "受保护打断资格".into(),
                        ),
                        ("reverse_generation".into(), "反向参考代次".into()),
                        ("state".into(), "前处理健康状态".into()),
                    ]),
                },
            };
            media.send_media_frame(&header, &packet).await.unwrap();
        }
        let mut decoded_capture = None;
        for _ in 0..100 {
            let status =
                call_python_gateway_test_route("GET", "/__test__/media-capture-status").await;
            assert_eq!(status.status, 200, "{status:?}");
            if status.body["frames"].as_u64() == Some(1) {
                decoded_capture = Some(status.body);
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        let decoded_capture = decoded_capture.expect("网关应将两个 Opus 包解码为一个 20 ms PCM 帧");
        assert_eq!(decoded_capture["sample_bytes"], 640);

        let started = call_python_gateway_test_route("POST", "/__test__/tts-enqueue").await;
        assert_eq!(started.status, 200, "{started:?}");
        let binary = loop {
            let message = tokio::time::timeout(Duration::from_secs(10), media.receive())
                .await
                .expect("客户端应在期限内收到 TTS 播放帧")
                .unwrap();
            match message {
                SshMediaMessage::Binary(bytes) => break bytes,
                SshMediaMessage::Ping(_) => continue,
                SshMediaMessage::Text(text) => panic!("下行播放帧前出现意外控制消息：{text}"),
                SshMediaMessage::Pong(_) => {}
                SshMediaMessage::Closed => panic!("服务端在发送 TTS 播放帧前关闭媒体通道"),
            }
        };
        let (header, payload) = crate::device_protocol::decode_playback_frame(&binary).unwrap();
        assert_eq!(header.device_id, "test-device");
        assert_eq!(header.delivery_device_id, "test-device");
        assert_eq!(header.core_session_id, media.core_session_id);
        assert_eq!(header.connection_generation, lease.connection_generation);
        assert_eq!(header.executor_generation, lease.executor_generation);
        assert_eq!(header.interaction_generation, interaction_generation);
        assert_eq!(header.playback_key, "ssh-media-tts-e2e");
        assert_eq!(header.sequence, 0);
        assert_eq!(header.start_sample, 0);
        assert_eq!(header.sample_count, 320);
        assert_eq!(header.sample_rate, 16_000);
        assert_eq!(payload, b"\x2a\x00".repeat(320));

        media
            .send_text(
                &serde_json::json!({
                    "type":"playback.accepted", "protocol_version":MEDIA_PROTOCOL_VERSION,
                    "device_id":media.device_id, "delivery_device_id":media.device_id,
                    "core_session_id":media.core_session_id,
                    "connection_generation":media.connection_generation,
                    "executor_generation":media.executor_generation,
                    "interaction_generation":media.interaction_generation,
                    "stream_id":media.stream_id, "sequence":header.sequence,
                    "fields":{"type":"控制消息类型","sequence":"已加入模拟设备播放队列的帧序号"},
                })
                .to_string(),
            )
            .await
            .unwrap();
        media
            .send_text(
                &serde_json::json!({
                    "type":"playback.progress", "protocol_version":MEDIA_PROTOCOL_VERSION,
                    "device_id":media.device_id, "delivery_device_id":media.device_id,
                    "core_session_id":media.core_session_id,
                    "connection_generation":media.connection_generation,
                    "executor_generation":media.executor_generation,
                    "interaction_generation":media.interaction_generation,
                    "stream_id":media.stream_id, "playback_key":header.playback_key,
                    "receipt_sequence":1, "sequence":header.sequence,
                    "enqueued_ms":20, "played_ms":20, "buffered_ms":0,
                    "output_estimated_ms":20, "started":true,
                    "device_time_us":device_clock_us.fetch_add(1, Ordering::AcqRel) / 2 + 1,
                    "precision":"device_time",
                    "fields":{"type":"控制消息类型","played_ms":"模拟设备已提交输出的时长"},
                })
                .to_string(),
            )
            .await
            .unwrap();

        let status = loop {
            let response = call_python_gateway_test_route("GET", "/__test__/tts-status").await;
            if response.status != 202 {
                break response;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        };
        assert_eq!(status.status, 200, "{status:?}");
        assert_eq!(status.body["state"], "succeeded");
        assert_eq!(status.body["progress"]["enqueued_ms"], 20);
        assert_eq!(status.body["progress"]["played_ms"], 20);
        assert_eq!(status.body["progress"]["buffered_ms"], 0);
        media.close().await.unwrap();
        assert!(manager
            .close_remote_device_session(&lease, Duration::from_secs(5))
            .await
            .unwrap());
        manager.disconnect().await.unwrap();
        ssh_handle.shutdown("media integration test completed".into());
        ssh_task.await.unwrap().unwrap();
        let stopped = call_python_gateway_test_route("POST", "/__test__/shutdown").await;
        assert_eq!(stopped.status, 200);
        for _ in 0..100 {
            if python_gateway.0.try_wait().unwrap().is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(
            python_gateway.0.try_wait().unwrap().is_some(),
            "Python 设备网关应已退出"
        );
    }

    fn target() -> SshDeviceTarget {
        SshDeviceTarget {
            address: "voice-host.local".into(),
            port: 22,
            username: "voice_terminal".into(),
            host_key_sha256: "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA".into(),
            gateway_port: DEFAULT_GATEWAY_PORT,
        }
    }

    #[test]
    fn target_requires_valid_user_host_port_and_sha256_pin() {
        let mut value = target();
        assert!(value.validate().is_ok());
        value.address = "host\r\ncommand".into();
        assert!(value.validate().is_err());
        value = target();
        value.username = "user;command".into();
        assert!(value.validate().is_err());
        value = target();
        value.host_key_sha256 = "SHA256:short".into();
        assert!(value.validate().is_err());
        value = target();
        value.gateway_port = 0;
        assert!(value.validate().is_err());
    }

    #[test]
    fn runtime_clock_response_uses_current_media_protocol() {
        let mut request = serde_json::json!({"type":"clock.sync.request","protocol_version":MEDIA_PROTOCOL_VERSION,
            "sync_id":"sample-a","fields":{"sync_id":"测量编号"}});
        let refreshed: serde_json::Value = serde_json::from_str(&media_clock_response(&request, 200, 201).unwrap()).unwrap();
        assert_eq!(refreshed["sync_id"], "sample-a");
        assert!(media_clock_response(&request, 201, 200).is_err());
        request["protocol_version"] = serde_json::json!(99);
        assert!(media_clock_response(&request, 200, 201).is_err());
    }

    #[test]
    fn dsh_file_transfer_binds_strict_ticket_id_and_request_without_remote_paths() {
        assert!(valid_dsh_transfer_id("0123456789abcdef0123456789abcdef"));
        assert!(!valid_dsh_transfer_id("transfer-123"));
        assert!(valid_dsh_request_id("remote-large-read-1"));
        assert!(!valid_dsh_request_id("../outside"));
        assert!(validate_sha256(&"a".repeat(64)).is_ok());
        assert!(validate_sha256(&"A".repeat(64)).is_err());
    }

    #[test]
    fn host_key_handler_requires_the_exact_sha256_fingerprint() {
        let key: russh::keys::PublicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILM+rvN+ot98qgEN796jTiQfZfG1KaT0PtFDJ/XFSqti user@example.com"
            .parse()
            .unwrap();
        let host_key = PublicKeyOrCertificate::from(key.clone());
        let fingerprint = key.fingerprint(HashAlg::Sha256).to_string();
        assert!(host_fingerprint_matches(&host_key, &fingerprint));
        assert!(!host_fingerprint_matches(
            &host_key,
            "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        ));
    }

    #[test]
    fn device_private_key_reference_does_not_disclose_device_id() {
        let first = device_credential_reference("desktop-device-a").unwrap();
        let second = device_credential_reference("desktop-device-a").unwrap();
        let other = device_credential_reference("desktop-device-b").unwrap();
        assert_eq!(first, second);
        assert_ne!(first, other);
        assert!(!first.contains("desktop-device-a"));
    }

    #[test]
    fn imported_transport_identity_is_distinct_and_uses_fingerprint_reference() {
        let mut rng = rand::rng();
        let private_key = PrivateKey::random(&mut rng, Algorithm::Ed25519).unwrap();
        let openssh = private_key.to_openssh(LineEnding::LF).unwrap();
        let parsed = parse_transport_private_key(openssh.as_str()).unwrap();
        let fingerprint = parsed.public_key().fingerprint(HashAlg::Sha256).to_string();
        let reference = transport_credential_reference(&fingerprint).unwrap();
        let public_key = parsed.public_key().to_openssh().unwrap();
        assert!(reference.starts_with("VoiceTerminal/ssh-transport/"));
        assert_eq!(reference.len(), "VoiceTerminal/ssh-transport/".len() + 64);
        assert!(!reference.starts_with("VoiceTerminal/device/"));
        assert!(public_key.starts_with("ssh-ed25519 "));
        assert_eq!(
            transport_credential_reference(&fingerprint).unwrap(),
            reference
        );
        assert!(parse_transport_private_key("not an OpenSSH key").is_err());
        let public_only = serde_json::to_value(SshTransportIdentity {
            credential_ref: reference,
            public_key,
            fingerprint,
        })
        .unwrap();
        assert!(public_only.get("private_key").is_none());
    }

    #[test]
    fn pairing_proof_matches_the_versioned_length_delimited_protocol() {
        let nonce = [7_u8; 24];
        let message = pairing_proof_message(
            "desk-a",
            "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "001234",
            &nonce,
        )
        .unwrap();
        let mut expected = b"voice-terminal-device-pairing-v1\0".to_vec();
        for field in [
            &b"desk-a"[..],
            &b"SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"[..],
            &b"001234"[..],
            &nonce[..],
        ] {
            expected.extend_from_slice(&(field.len() as u32).to_be_bytes());
            expected.extend_from_slice(field);
        }
        assert_eq!(message, expected);
        assert!(pairing_proof_message("desk-a\n", "SHA256:bad", "001234", &nonce).is_err());
    }

    #[test]
    fn pairing_request_signature_verifies_against_its_open_ssh_public_key() {
        let mut rng = rand::rng();
        let private_key = PrivateKey::random(&mut rng, Algorithm::Ed25519).unwrap();
        let request = signed_pairing_request(&private_key, "client-a", "384729").unwrap();
        let public_key: russh::keys::PublicKey =
            request["public_key"].as_str().unwrap().parse().unwrap();
        let public_bytes: [u8; 32] = *public_key.key_data().ed25519().unwrap().as_ref();
        let verifying_key = ed25519_dalek::VerifyingKey::from_bytes(&public_bytes).unwrap();
        let nonce: [u8; 24] = base64::engine::general_purpose::STANDARD
            .decode(request["client_nonce"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        let signature_bytes: [u8; 64] = base64::engine::general_purpose::STANDARD
            .decode(request["signature"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        let proof = pairing_proof_message(
            "client-a",
            &public_key.fingerprint(HashAlg::Sha256).to_string(),
            "384729",
            &nonce,
        )
        .unwrap();
        verifying_key
            .verify_strict(
                &proof,
                &ed25519_dalek::Signature::from_bytes(&signature_bytes),
            )
            .unwrap();
        let mut tampered = proof.clone();
        tampered.extend_from_slice(b"tampered");
        assert!(verifying_key
            .verify_strict(
                &tampered,
                &ed25519_dalek::Signature::from_bytes(&signature_bytes)
            )
            .is_err());
        assert_eq!(request["protocol_version"], 1);
    }

    #[test]
    fn control_request_rejects_path_injection_and_oversized_body() {
        assert!(
            encode_http_request("/v1/status\r\nHost:evil", &serde_json::json!({}), None).is_err()
        );
        let too_large = serde_json::json!({"data":"x".repeat(MAX_REQUEST_BYTES)});
        assert!(encode_http_request("/v1/status", &too_large, None).is_err());
        assert!(
            encode_http_request("/v1/status", &serde_json::json!({"device_id":"A"}), None).is_ok()
        );
    }

    #[test]
    fn device_request_proof_matches_the_python_gateway_vector() {
        let body = serde_json::json!({"protocol_version": 1});
        let challenge = "a".repeat(43);
        let message = device_request_proof_message(
            "client-a",
            "/v1/devices",
            "0123456789abcdef0123456789abcdef",
            &challenge,
            1_700_000_000_123,
            &body,
        )
        .unwrap();
        let actual = message
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(actual,
            "766f6963652d7465726d696e616c2d6465766963652d726571756573742d76310000000004504f53540000000b2f76312f6465766963657300000008636c69656e742d610000002030313233343536373839616263646566303132333435363738396162636465660000002b616161616161616161616161616161616161616161616161616161616161616161616161616161616161610000000d313730303030303030303132330000002000aa4c2c857995eb8e19cb0fade07e4b49aac774e37a99c37a0c9f549204d9de");
    }

    #[test]
    fn media_request_signing_only_allows_signed_get_on_the_media_path() {
        let body = serde_json::json!({"protocol_version":1,"stream_id":"stream-a",
            "connection_generation":2,"executor_generation":3,"interaction_generation":4});
        let challenge = "a".repeat(43);
        assert!(device_request_proof_message_for_method(
            "GET",
            "client-a",
            "/v1/media",
            "0123456789abcdef0123456789abcdef",
            &challenge,
            1_700_000_000_123,
            &body,
        )
        .is_ok());
        assert!(device_request_proof_message_for_method(
            "POST",
            "client-a",
            "/v1/media",
            "0123456789abcdef0123456789abcdef",
            &challenge,
            1_700_000_000_123,
            &body,
        )
        .is_err());
        assert!(device_request_proof_message_for_method(
            "GET",
            "client-a",
            "/v1/status",
            "0123456789abcdef0123456789abcdef",
            &challenge,
            1_700_000_000_123,
            &body,
        )
        .is_err());
        assert!(device_request_proof_message_for_method(
            "POST",
            "client-a",
            FILE_TRANSFER_GATEWAY_PATH,
            "0123456789abcdef0123456789abcdef",
            &challenge,
            1_700_000_000_123,
            &serde_json::json!({
                "protocol_version":1,"action":"begin","transfer_id":"a".repeat(32)
            }),
        )
        .is_ok());
        assert!(device_request_proof_message_for_method(
            "POST",
            "client-a",
            "/v1/file-transfer/other",
            "0123456789abcdef0123456789abcdef",
            &challenge,
            1_700_000_000_123,
            &body,
        )
        .is_err());
    }

    #[test]
    fn websocket_accept_matches_the_rfc6455_example() {
        assert_eq!(
            websocket_accept_value("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        );
    }

    #[test]
    fn remote_voice_forwarder_accepts_only_the_random_loopback_path_and_valid_upgrade() {
        let token = "a".repeat(64);
        let key = base64::engine::general_purpose::STANDARD.encode([9_u8; 16]);
        let request = format!(
            "GET /{token}/ws HTTP/1.1\r\nHost: 127.0.0.1:40000\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n",
        );
        assert_eq!(
            local_voice_upgrade_key(request.as_bytes(), &token).unwrap(),
            key
        );

        let wrong_path = request.replace(&format!("/{token}/ws"), "/ws");
        assert!(local_voice_upgrade_key(wrong_path.as_bytes(), &token).is_err());
        let duplicate_key = request.replace(
            "Sec-WebSocket-Version: 13\r\n",
            &format!("Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"),
        );
        assert!(local_voice_upgrade_key(duplicate_key.as_bytes(), &token).is_err());
        let with_body = request.replace("\r\n\r\n", "\r\nContent-Length: 1\r\n\r\n");
        assert!(local_voice_upgrade_key(with_body.as_bytes(), &token).is_err());

        let response = format!(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {}\r\n\r\n",
            websocket_accept_value(&key),
        );
        assert!(validate_voice_upgrade_response(response.as_bytes(), &key).is_ok());
        assert!(
            validate_voice_upgrade_response(response.as_bytes(), "dGhlIHNhbXBsZSBub25jZQ==")
                .is_err()
        );
    }

    #[test]
    fn websocket_client_frames_are_masked_and_server_text_is_bounded() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let (client, mut server) = tokio::io::duplex(1024);
            let mut websocket = SshWebSocketStream::new(Box::new(client));
            websocket.send_frame(0x1, b"ping").await.unwrap();
            let mut prefix = [0_u8; 2];
            server.read_exact(&mut prefix).await.unwrap();
            assert_eq!(prefix[0], 0x81);
            assert_ne!(prefix[1] & 0x80, 0);
            assert_eq!(prefix[1] & 0x7f, 4);
            let mut mask = [0_u8; 4];
            server.read_exact(&mut mask).await.unwrap();
            let mut payload = [0_u8; 4];
            server.read_exact(&mut payload).await.unwrap();
            for index in 0..payload.len() {
                payload[index] ^= mask[index & 3];
            }
            assert_eq!(&payload, b"ping");

            server
                .write_all(&[0x81, 3, b'o', b'k', b'!'])
                .await
                .unwrap();
            assert_eq!(
                websocket.receive_message().await.unwrap(),
                SshMediaMessage::Text("ok!".into())
            );
        });
    }

    #[test]
    fn remote_session_signing_paths_and_task_targets_are_bounded() {
        let body = serde_json::json!({"protocol_version":1,"connection_generation":1});
        let challenge = "a".repeat(43);
        for path in [
            "/v1/session/open",
            "/v1/session/poll",
            "/v1/session/complete",
            "/v1/session/reconcile",
            "/v1/session/close",
            "/v1/interaction/prepare",
            "/v1/interaction/abort",
            "/v1/management",
        ] {
            assert!(device_request_proof_message(
                "client-a",
                path,
                "0123456789abcdef0123456789abcdef",
                &challenge,
                1_700_000_000_123,
                &body
            )
            .is_ok());
        }
        let lease = RemoteDeviceSessionLease {
            protocol_version: 1,
            result: "accepted".into(),
            device_id: "client-a".into(),
            core_session_id: "0123456789abcdef0123456789abcdef".into(),
            connection_generation: 2,
            executor_generation: 3,
            capabilities: vec!["desktop_control".into()],
            separate_playback_tasks: false,
        };
        validate_remote_session_lease(&lease).unwrap();
        let task = RemoteDeviceTask {
            request_id: "task-1".into(),
            device_id: lease.device_id.clone(),
            core_session_id: lease.core_session_id.clone(),
            task_id: "task-1".into(),
            connection_generation: lease.connection_generation,
            executor_generation: lease.executor_generation,
            sequence: 1,
            operation: "tree".into(),
            arguments: serde_json::json!({"target":{"windowId":7}}),
        };
        validate_remote_device_task(&task, &lease).unwrap();
        let playback_operations = [
            (
                "playback.pause",
                serde_json::json!({"playback_key":"speech-1"}),
            ),
            (
                "playback.resume",
                serde_json::json!({"playback_key":"speech-1"}),
            ),
            (
                "playback.stop_and_snapshot",
                serde_json::json!({"playback_key":"speech-1"}),
            ),
            (
                "playback.prepare_replacement",
                serde_json::json!({"playback_key":"speech-1","max_ms":400}),
            ),
            (
                "playback.bind_replacement",
                serde_json::json!({"replacement_id":"replace-1","playback_key":"speech-2"}),
            ),
            (
                "playback.abort_replacement",
                serde_json::json!({"replacement_id":"replace-1","reason":"cancelled"}),
            ),
            (
                "playback.wait_replacement",
                serde_json::json!({"replacement_id":"replace-1"}),
            ),
        ];
        for (index, (operation, arguments)) in playback_operations.into_iter().enumerate() {
            let mut playback_task = task.clone();
            playback_task.request_id = format!("playback-task-{index}");
            playback_task.task_id = playback_task.request_id.clone();
            playback_task.sequence = index as u64 + 1;
            playback_task.operation = operation.into();
            playback_task.arguments = arguments;
            validate_remote_device_task(&playback_task, &lease).unwrap();
        }
        let mut injected = task.clone();
        injected.operation = "powershell".into();
        assert!(validate_remote_device_task(&injected, &lease).is_err());
        let mut old_generation = task;
        old_generation.connection_generation += 1;
        assert!(validate_remote_device_task(&old_generation, &lease).is_err());
    }

    #[test]
    fn authenticated_gateway_request_carries_only_validated_signature_headers() {
        let auth = DeviceGatewayAuthHeaders {
            device_id: "client-a".into(),
            core_session_id: "0123456789abcdef0123456789abcdef".into(),
            challenge: "a".repeat(43),
            timestamp_ms: 1_700_000_000_123,
            signature: base64::engine::general_purpose::STANDARD.encode([7_u8; 64]),
        };
        let request = encode_http_request(
            "/v1/status",
            &serde_json::json!({"protocol_version":1}),
            Some(&auth),
        )
        .unwrap();
        let request = String::from_utf8(request).unwrap();
        assert!(request.contains("X-Voice-Terminal-Device-ID: client-a\r\n"));
        assert!(request
            .contains("X-Voice-Terminal-Core-Session-ID: 0123456789abcdef0123456789abcdef\r\n"));
        assert!(request.contains("X-Voice-Terminal-Timestamp-Ms: 1700000000123\r\n"));
        assert!(encode_http_request(
            "/v1/status",
            &serde_json::json!({}),
            Some(&DeviceGatewayAuthHeaders {
                signature: "bad".into(),
                ..auth
            })
        )
        .is_err());
    }

    #[test]
    fn response_requires_bounded_json_with_exact_content_length() {
        let bytes = b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: 14\r\n\r\n{\"ready\":true}";
        let parsed = parse_http_response(bytes).unwrap();
        assert_eq!(parsed.status, 200);
        assert_eq!(parsed.body["ready"], true);
        let truncated =
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{}";
        assert!(parse_http_response(truncated).is_err());
    }

    #[test]
    fn response_rejects_chunked_compressed_duplicate_or_non_json_payloads() {
        for bytes in [
            &b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\nContent-Type: application/json\r\n\r\n"[..],
            &b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\ncontent-length: 0\r\nContent-Type: application/json\r\n\r\n"[..],
            &b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\n{}"[..],
            &b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n\r\nx"[..],
        ] {
            assert!(parse_http_response(bytes).is_err());
        }
    }
}
