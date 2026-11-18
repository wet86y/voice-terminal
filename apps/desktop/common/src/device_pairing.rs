//! DevicePairing（设备配对）：六位短码只作为 OPAQUE（口令认证密钥交换）输入。
use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce,
};
use base64::Engine;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use opaque_ke::argon2::Argon2;
use opaque_ke::ciphersuite::CipherSuite;
use opaque_ke::{
    ClientLogin, ClientLoginFinishParameters, ClientRegistration,
    ClientRegistrationFinishParameters, CredentialFinalization, CredentialRequest,
    CredentialResponse, RegistrationUpload, ServerLogin, ServerLoginParameters, ServerRegistration,
    ServerSetup,
};
use rand_pake::{rngs::OsRng, Rng, RngCore};
use russh::keys::{HashAlg, PublicKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256, Sha512};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::Notify,
    task::JoinHandle,
};
use zeroize::Zeroizing;

const PAIRING_WINDOW_SECONDS: u64 = 120; // PAIRING_WINDOW_SECONDS（六位配对码窗口秒数）。
const MAX_ATTEMPTS: u8 = 10; // MAX_ATTEMPTS（本次窗口允许的 OPAQUE 认证尝试次数）。
const CREDENTIAL_IDENTIFIER: &[u8] = b"voice-terminal-device-pairing-v1";
const MAX_PAIRING_FRAME_BYTES: usize = 16 * 1024; // MAX_PAIRING_FRAME_BYTES（配对协议单帧上限，字节）。
pub const DEFAULT_PAIRING_PORT: u16 = 18_766; // DEFAULT_PAIRING_PORT（固定局域网 OPAQUE 配对端口）。
const PAIRING_PROOF_DOMAIN: &[u8] = b"voice-terminal-opaque-pairing-proof-v1\0";
const PAIRING_AEAD_DOMAIN: &[u8] = b"voice-terminal-opaque-pairing-aead-v1\0";

/// DevicePairingCipherSuite（设备配对算法套件）：固定 Ristretto255、TripleDh、SHA-512 和 Argon2。
pub struct DevicePairingCipherSuite;

impl CipherSuite for DevicePairingCipherSuite {
    type OprfCs = opaque_ke::Ristretto255;
    type KeyExchange = opaque_ke::TripleDh<opaque_ke::Ristretto255, Sha512>;
    type Ksf = Argon2<'static>;
}

/// OpaqueClientStart（客户端 OPAQUE 首步）：只保留易失客户端状态，不序列化配对码。
pub struct OpaqueClientStart {
    state: ClientLogin<DevicePairingCipherSuite>,
    password: Zeroizing<Vec<u8>>,
}

impl OpaqueClientStart {
    /// start（开始客户端登录）：返回可公开传输的 OPAQUE CredentialRequest（认证请求）。
    pub fn start(pairing_code: &str) -> Result<(Self, Vec<u8>), String> {
        validate_code(pairing_code)?;
        let password = Zeroizing::new(pairing_code.as_bytes().to_vec());
        let result =
            ClientLogin::<DevicePairingCipherSuite>::start(&mut OsRng, password.as_slice())
                .map_err(|_| "OPAQUE 客户端无法开始设备配对。".to_string())?;
        let request = result.message.serialize().to_vec();
        Ok((
            Self {
                state: result.state,
                password,
            },
            request,
        ))
    }

    /// finish（完成客户端认证）：核验服务端响应后返回终结消息与会话密钥。
    pub fn finish(self, response: &[u8]) -> Result<(Vec<u8>, Zeroizing<Vec<u8>>), String> {
        let response = CredentialResponse::<DevicePairingCipherSuite>::deserialize(response)
            .map_err(|_| "OPAQUE 服务端认证响应无效。".to_string())?;
        let result = self
            .state
            .finish(
                &mut OsRng,
                self.password.as_slice(),
                response,
                ClientLoginFinishParameters::default(),
            )
            .map_err(|_| "配对码错误、过期或服务端身份不匹配。".to_string())?;
        let finalization = result.message.serialize().to_vec();
        Ok((
            finalization,
            Zeroizing::new(result.session_key.as_slice().to_vec()),
        ))
    }
}

/// OpaqueServerAttempt（服务端单次尝试）：会话状态只属于本次 TCP 连接。
pub struct OpaqueServerAttempt {
    state: ServerLogin<DevicePairingCipherSuite>,
}

/// PairingCodeWindow（配对码窗口）：不保留明文配对码，成功或十次尝试后立即失效。
pub struct PairingCodeWindow {
    setup: ServerSetup<DevicePairingCipherSuite>,
    record: ServerRegistration<DevicePairingCipherSuite>,
    expires_at: Instant,
    attempts: u8,
    completed: bool,
}

/// OpenedPairingWindow（已开启窗口回执）：配对码只出现在开启操作的单次结果中。
pub struct OpenedPairingWindow {
    pub pairing_code: String, // pairing_code（只在开启窗口回执中出现的六位数字）。
    pub expires_in_seconds: u64, // expires_in_seconds（窗口有效秒数）。
}

/// PairingIdentity（配对设备身份）：通过 OPAQUE 派生密钥加密传输。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PairingIdentity {
    pub device_id: String,   // device_id（客户端稳定设备编号）。
    pub public_key: String,  // public_key（客户端 OpenSSH Ed25519 公钥）。
    pub fingerprint: String, // fingerprint（客户端设备公钥 SHA-256 指纹）。
    pub signature: String,   // signature（针对服务端随机挑战的 Ed25519 签名）。
}

/// PairingServerInfo（配对服务端身份）：完成短码验证后经 AEAD（认证加密）回传。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PairingServerInfo {
    pub host_id: String,         // host_id（稳定服务端设备编号）。
    pub display_name: String,    // display_name（服务端显示名称）。
    pub username: String,        // username（当前 Windows 登录账户对应的 SSH 登录名）。
    pub ssh_port: u16,           // ssh_port（微软 OpenSSH 服务端口）。
    pub host_public_key: String, // host_public_key（微软 OpenSSH 主机 Ed25519 公钥）。
    pub host_key_sha256: String, // host_key_sha256（固定服务端主机指纹）。
    pub protocol_version: u16,   // protocol_version（远程设备桥接协议版本）。
    pub fields: std::collections::BTreeMap<String, String>, // fields（字段中文说明）。
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct PairingChallenge {
    server_nonce: String, // server_nonce（服务端一次性随机挑战，Base64 编码）。
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
enum PairingReply {
    Accepted(PairingServerInfo), // Accepted（已登记客户端并返回固定服务端身份）。
    Rejected { reason: String }, // Rejected（未能登记客户端的非敏感原因）。
}

/// PairingListener（配对监听租约）：仅绑定经筛选的私有局域网地址和固定端口。
pub struct PairingListener {
    pub port: u16,              // port（已绑定的本地配对 TCP 端口）。
    pub addresses: Vec<IpAddr>, // addresses（实际成功绑定的私有局域网地址）。
    stopping: Arc<AtomicBool>,
    wake: Arc<Notify>,
    tasks: Vec<JoinHandle<()>>,
}

impl PairingListener {
    pub async fn stop(mut self) {
        self.stopping.store(true, Ordering::Release);
        self.wake.notify_waiters();
        for task in self.tasks.drain(..) {
            let _ = task.await;
        }
    }
}

impl Drop for PairingListener {
    fn drop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        self.wake.notify_waiters();
        for task in self.tasks.drain(..) {
            task.abort();
        }
    }
}

/// start_pairing_listener（启动配对监听）：只绑定允许的物理局域网私有地址。
pub async fn start_pairing_listener(
    addresses: &[IpAddr],
    window: Arc<Mutex<Option<PairingCodeWindow>>>,
    server_info: PairingServerInfo,
    register_identity: Arc<dyn Fn(&PairingIdentity) -> Result<(), String> + Send + Sync>,
) -> Result<PairingListener, String> {
    let mut unique = addresses
        .iter()
        .copied()
        .filter(|address| crate::device_discovery::is_private_lan_address(*address))
        .collect::<std::collections::BTreeSet<_>>();
    if unique.is_empty() {
        return Err("没有允许发布配对服务的私有局域网地址。".into());
    }
    let mut listeners = Vec::new();
    let mut bound = Vec::new();
    let mut last_error = None;
    while let Some(address) = unique.pop_first() {
        match TcpListener::bind(SocketAddr::new(address, DEFAULT_PAIRING_PORT)).await {
            Ok(listener) => {
                listeners.push(listener);
                bound.push(address);
            }
            Err(error) => last_error = Some(error.to_string()),
        }
    }
    if listeners.is_empty() {
        return Err(format!(
            "无法在受限局域网地址绑定配对端口 {DEFAULT_PAIRING_PORT}：{}",
            last_error.unwrap_or_else(|| "无可用网络接口".into())
        ));
    }
    let stopping = Arc::new(AtomicBool::new(false));
    let wake = Arc::new(Notify::new());
    let mut tasks = Vec::new();
    for listener in listeners {
        let window = window.clone();
        let server_info = server_info.clone();
        let register_identity = register_identity.clone();
        let stopping = stopping.clone();
        let wake = wake.clone();
        tasks.push(tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = wake.notified() => break,
                    _ = tokio::time::sleep(Duration::from_millis(250)) => {
                        if !window.lock().map(|window| window.as_ref().is_some_and(PairingCodeWindow::is_open)).unwrap_or(false) { break; }
                    }
                    accepted = listener.accept() => {
                        if stopping.load(Ordering::Acquire) { break; }
                        let Ok((stream, _peer)) = accepted else { continue };
                        let register_identity = register_identity.clone();
                        tokio::select! {
                            _ = wake.notified() => break,
                            _ = accept_pairing_connection(stream, &window, &server_info,
                                move |identity| register_identity(identity)) => {}
                        }
                    }
                }
            }
        }));
    }
    Ok(PairingListener {
        port: DEFAULT_PAIRING_PORT,
        addresses: bound,
        stopping,
        wake,
        tasks,
    })
}

impl PairingCodeWindow {
    /// open（创建短期窗口）：随机生成带前导零的六位码并建立内存 OPAQUE 记录。
    pub fn open() -> Result<(Self, OpenedPairingWindow), String> {
        let pairing_code = format!("{:06}", OsRng.gen_range(0..1_000_000_u32));
        let mut rng = OsRng;
        let setup = ServerSetup::<DevicePairingCipherSuite>::new(&mut rng);
        let client_start = ClientRegistration::<DevicePairingCipherSuite>::start(
            &mut rng,
            pairing_code.as_bytes(),
        )
        .map_err(|_| "OPAQUE 无法创建配对窗口记录。".to_string())?;
        let server_start = ServerRegistration::<DevicePairingCipherSuite>::start(
            &setup,
            client_start.message,
            CREDENTIAL_IDENTIFIER,
        )
        .map_err(|_| "OPAQUE 无法创建配对窗口记录。".to_string())?;
        let client_finish = client_start
            .state
            .finish(
                &mut rng,
                pairing_code.as_bytes(),
                server_start.message,
                ClientRegistrationFinishParameters::default(),
            )
            .map_err(|_| "OPAQUE 无法建立安全配对记录。".to_string())?;
        let upload = RegistrationUpload::<DevicePairingCipherSuite>::deserialize(
            client_finish.message.serialize().as_slice(),
        )
        .map_err(|_| "OPAQUE 配对记录格式无效。".to_string())?;
        let record = ServerRegistration::finish(upload);
        let window = Self {
            setup,
            record,
            expires_at: Instant::now() + Duration::from_secs(PAIRING_WINDOW_SECONDS),
            attempts: 0,
            completed: false,
        };
        Ok((
            window,
            OpenedPairingWindow {
                pairing_code,
                expires_in_seconds: PAIRING_WINDOW_SECONDS,
            },
        ))
    }

    /// begin_attempt（开始服务端认证）：无论后续是否中断，每次连接尝试都会消耗次数。
    pub fn begin_attempt(
        &mut self,
        request: &[u8],
    ) -> Result<(OpaqueServerAttempt, Vec<u8>), String> {
        if self.completed || Instant::now() >= self.expires_at || self.attempts >= MAX_ATTEMPTS {
            return Err("配对窗口已关闭或尝试次数已用尽。".into());
        }
        self.attempts = self.attempts.saturating_add(1);
        let request = CredentialRequest::<DevicePairingCipherSuite>::deserialize(request)
            .map_err(|_| "OPAQUE 客户端认证请求无效。".to_string())?;
        let result = ServerLogin::start(
            &mut OsRng,
            &self.setup,
            Some(self.record.clone()),
            request,
            CREDENTIAL_IDENTIFIER,
            ServerLoginParameters::default(),
        )
        .map_err(|_| "OPAQUE 服务端无法开始设备配对。".to_string())?;
        Ok((
            OpaqueServerAttempt {
                state: result.state,
            },
            result.message.serialize().to_vec(),
        ))
    }

    /// finish_attempt（完成服务端认证）：仅成功终结后得到相同会话密钥并关闭窗口。
    pub fn finish_attempt(
        &mut self,
        attempt: OpaqueServerAttempt,
        finalization: &[u8],
    ) -> Result<Zeroizing<Vec<u8>>, String> {
        if Instant::now() >= self.expires_at || self.completed {
            return Err("配对窗口已过期或已经使用。".into());
        }
        let message = CredentialFinalization::<DevicePairingCipherSuite>::deserialize(finalization)
            .map_err(|_| "OPAQUE 客户端认证终结消息无效。".to_string())?;
        let result = attempt
            .state
            .finish(message, ServerLoginParameters::default())
            .map_err(|_| "OPAQUE 客户端未通过配对码验证。".to_string())?;
        self.completed = true;
        Ok(Zeroizing::new(result.session_key.as_slice().to_vec()))
    }

    pub fn failed_attempts_remaining(&self) -> u8 {
        if self.completed || Instant::now() >= self.expires_at {
            0
        } else {
            MAX_ATTEMPTS.saturating_sub(self.attempts)
        }
    }

    pub fn is_open(&self) -> bool {
        !self.completed && Instant::now() < self.expires_at && self.attempts < MAX_ATTEMPTS
    }

    pub fn expires_in_ms(&self) -> u64 {
        if !self.is_open() {
            0
        } else {
            self.expires_at
                .saturating_duration_since(Instant::now())
                .as_millis()
                .min(u128::from(u64::MAX)) as u64
        }
    }
}

/// accept_pairing_connection（接收配对 TCP 会话）：认证、加密身份绑定并在登记后返回服务端资料。
pub async fn accept_pairing_connection<F>(
    mut stream: TcpStream,
    window: &std::sync::Arc<std::sync::Mutex<Option<PairingCodeWindow>>>,
    server_info: &PairingServerInfo,
    register_identity: F,
) -> Result<PairingIdentity, String>
where
    F: FnOnce(&PairingIdentity) -> Result<(), String>,
{
    let request = read_frame(&mut stream).await?;
    let (attempt, response) = window
        .lock()
        .map_err(|_| "配对窗口锁不可用。")?
        .as_mut()
        .ok_or("服务端尚未开启六位配对窗口。")?
        .begin_attempt(&request)?;
    write_frame(&mut stream, &response).await?;
    let finalization = read_frame(&mut stream).await?;
    let session_key = window
        .lock()
        .map_err(|_| "配对窗口锁不可用。")?
        .as_mut()
        .ok_or("配对窗口已关闭。")?
        .finish_attempt(attempt, &finalization)?;

    let mut server_nonce = [0u8; 32];
    OsRng.fill_bytes(&mut server_nonce);
    let challenge = PairingChallenge {
        server_nonce: base64::engine::general_purpose::STANDARD.encode(server_nonce),
    };
    write_frame(&mut stream, &encrypt_payload(&session_key, &challenge)?).await?;
    let identity: PairingIdentity = decrypt_payload(&session_key, &read_frame(&mut stream).await?)?;
    verify_pairing_identity(&identity, &server_nonce)?;
    if let Err(_error) = register_identity(&identity) {
        let reply = PairingReply::Rejected {
            reason: "服务端未能安全登记本次设备；请检查服务端配对状态后重试。".into(),
        };
        let _ = write_frame(&mut stream, &encrypt_payload(&session_key, &reply)?).await;
        return Err("服务端设备信任或 SSH 受限密钥登记失败。".into());
    }
    write_frame(
        &mut stream,
        &encrypt_payload(&session_key, &PairingReply::Accepted(server_info.clone()))?,
    )
    .await?;
    Ok(identity)
}

/// connect_pairing_stream（客户端配对会话）：签名密钥只在闭包中读取，码不进入明文网络帧。
pub async fn connect_pairing_stream<F>(
    mut stream: TcpStream,
    pairing_code: &str,
    device_id: &str,
    public_key: &str,
    sign_challenge: F,
) -> Result<PairingServerInfo, String>
where
    F: FnOnce(&[u8]) -> Result<String, String>,
{
    validate_code(pairing_code)?;
    validate_device_id(device_id)?;
    let (client, request) = OpaqueClientStart::start(pairing_code)?;
    write_frame(&mut stream, &request).await?;
    let response = read_frame(&mut stream).await?;
    let (finalization, session_key) = client.finish(&response)?;
    write_frame(&mut stream, &finalization).await?;
    let challenge: PairingChallenge =
        decrypt_payload(&session_key, &read_frame(&mut stream).await?)?;
    let server_nonce = base64::engine::general_purpose::STANDARD
        .decode(challenge.server_nonce.as_bytes())
        .map_err(|_| "服务端随机挑战编码无效。".to_string())?;
    if server_nonce.len() != 32 {
        return Err("服务端随机挑战长度无效。".into());
    }
    let (canonical_key, fingerprint) = validate_public_key(public_key)?;
    let proof = pairing_proof_message(device_id, &fingerprint, &server_nonce)?;
    let identity = PairingIdentity {
        device_id: device_id.to_owned(),
        public_key: canonical_key,
        fingerprint,
        signature: sign_challenge(&proof)?,
    };
    write_frame(&mut stream, &encrypt_payload(&session_key, &identity)?).await?;
    let reply: PairingReply = decrypt_payload(&session_key, &read_frame(&mut stream).await?)?;
    match reply {
        PairingReply::Accepted(info) => {
            validate_server_info(&info)?;
            Ok(info)
        }
        PairingReply::Rejected { reason } => Err(reason),
    }
}

/// connect_pairing（连接已发现服务端）：只尝试 mDNS（多播服务发现）返回的私有局域网地址。
pub async fn connect_pairing<F>(
    addresses: &[std::net::IpAddr],
    pairing_port: u16,
    pairing_code: &str,
    device_id: &str,
    public_key: &str,
    sign_challenge: F,
) -> Result<PairingServerInfo, String>
where
    F: FnOnce(&[u8]) -> Result<String, String>,
{
    connect_pairing_with_address(
        addresses,
        pairing_port,
        pairing_code,
        device_id,
        public_key,
        sign_challenge,
    )
    .await
    .map(|(identity, _)| identity)
}

/// connect_pairing_with_address（连接并返回选中的局域网地址）：地址只取自本次 mDNS 结果。
pub async fn connect_pairing_with_address<F>(
    addresses: &[std::net::IpAddr],
    pairing_port: u16,
    pairing_code: &str,
    device_id: &str,
    public_key: &str,
    sign_challenge: F,
) -> Result<(PairingServerInfo, std::net::IpAddr), String>
where
    F: FnOnce(&[u8]) -> Result<String, String>,
{
    if pairing_port == 0 {
        return Err("发现的配对端口无效。".into());
    }
    let mut connection = None;
    for address in addresses
        .iter()
        .copied()
        .filter(|address| crate::device_discovery::is_private_lan_address(*address))
    {
        if let Ok(Ok(stream)) = tokio::time::timeout(
            Duration::from_secs(3),
            TcpStream::connect(SocketAddr::new(address, pairing_port)),
        )
        .await
        {
            connection = Some((stream, address));
            break;
        }
    }
    let (stream, address) = connection.ok_or("无法通过已发现的局域网地址连接配对入口。")?;
    let identity =
        connect_pairing_stream(stream, pairing_code, device_id, public_key, sign_challenge).await?;
    Ok((identity, address))
}

fn pairing_proof_message(
    device_id: &str,
    fingerprint: &str,
    nonce: &[u8],
) -> Result<Vec<u8>, String> {
    if nonce.len() != 32 {
        return Err("配对挑战长度无效。".into());
    }
    let fields = [device_id.as_bytes(), fingerprint.as_bytes(), nonce];
    let mut message = PAIRING_PROOF_DOMAIN.to_vec();
    for field in fields {
        let length = u32::try_from(field.len()).map_err(|_| "配对身份字段超过长度限制。")?;
        message.extend_from_slice(&length.to_be_bytes());
        message.extend_from_slice(field);
    }
    Ok(message)
}

fn verify_pairing_identity(
    identity: &PairingIdentity,
    server_nonce: &[u8; 32],
) -> Result<(), String> {
    validate_device_id(&identity.device_id)?;
    let (canonical, fingerprint) = validate_public_key(&identity.public_key)?;
    if canonical != identity.public_key || fingerprint != identity.fingerprint {
        return Err("客户端设备公钥与指纹不一致。".into());
    }
    let key =
        PublicKey::from_openssh(&identity.public_key).map_err(|_| "客户端设备公钥格式无效。")?;
    let public_bytes: [u8; 32] = *key
        .key_data()
        .ed25519()
        .ok_or_else(|| "客户端只允许使用 Ed25519 设备公钥。".to_string())?
        .as_ref();
    let verifying_key =
        VerifyingKey::from_bytes(&public_bytes).map_err(|_| "客户端 Ed25519 公钥无效。")?;
    let signature_bytes = base64::engine::general_purpose::STANDARD
        .decode(identity.signature.as_bytes())
        .map_err(|_| "客户端设备签名编码无效。".to_string())?;
    let signature = Signature::from_slice(&signature_bytes)
        .map_err(|_| "客户端设备签名长度无效。".to_string())?;
    verifying_key
        .verify(
            &pairing_proof_message(&identity.device_id, &identity.fingerprint, server_nonce)?,
            &signature,
        )
        .map_err(|_| "客户端设备身份签名未通过验证。".to_string())
}

fn validate_public_key(value: &str) -> Result<(String, String), String> {
    if value.len() > 8192 {
        return Err("设备公钥超过大小限制。".into());
    }
    let key = PublicKey::from_openssh(value)
        .map_err(|_| "设备公钥不是有效 OpenSSH 格式。".to_string())?;
    if key.algorithm() != russh::keys::Algorithm::Ed25519 {
        return Err("仅接受 OpenSSH Ed25519 设备公钥。".into());
    }
    let fingerprint = key.fingerprint(HashAlg::Sha256).to_string();
    let canonical = key
        .to_openssh()
        .map_err(|_| "设备公钥无法规范编码。".to_string())?;
    Ok((canonical, fingerprint))
}

fn validate_device_id(value: &str) -> Result<(), String> {
    if !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        Ok(())
    } else {
        Err("设备编号格式无效。".into())
    }
}

fn validate_server_info(info: &PairingServerInfo) -> Result<(), String> {
    validate_device_id(&info.host_id)?;
    if info.display_name.trim().is_empty()
        || info.display_name.len() > 128
        || info.username.is_empty()
        || info.username.len() > 256
        || !info.username.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'\\' | b'@')
        })
        || info.ssh_port == 0
        || info.protocol_version == 0
    {
        return Err("配对服务端公开身份字段无效。".into());
    }
    let (canonical, fingerprint) = validate_public_key(&info.host_public_key)?;
    if canonical != info.host_public_key || fingerprint != info.host_key_sha256 {
        return Err("服务端 OpenSSH 主机公钥与指纹不一致。".into());
    }
    Ok(())
}

fn derive_aead_key(session_key: &[u8]) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(PAIRING_AEAD_DOMAIN);
    hash.update(session_key);
    hash.finalize().into()
}

fn encrypt_payload<T: Serialize>(session_key: &[u8], value: &T) -> Result<Vec<u8>, String> {
    let plaintext = serde_json::to_vec(value).map_err(|_| "配对加密载荷编码失败。".to_string())?;
    if plaintext.len() > MAX_PAIRING_FRAME_BYTES - 28 {
        return Err("配对加密载荷超过长度限制。".into());
    }
    let key = derive_aead_key(session_key);
    let cipher =
        Aes256Gcm::new_from_slice(&key).map_err(|_| "配对会话加密密钥格式无效。".to_string())?;
    let mut nonce = [0u8; 12];
    OsRng.fill_bytes(&mut nonce);
    let nonce = Nonce::from(nonce);
    let ciphertext = cipher
        .encrypt(&nonce, plaintext.as_ref())
        .map_err(|_| "配对会话加密失败。".to_string())?;
    let mut frame = nonce.to_vec();
    frame.extend_from_slice(&ciphertext);
    Ok(frame)
}

fn decrypt_payload<T: for<'de> Deserialize<'de>>(
    session_key: &[u8],
    frame: &[u8],
) -> Result<T, String> {
    if !(28..=MAX_PAIRING_FRAME_BYTES).contains(&frame.len()) {
        return Err("配对加密帧长度无效。".into());
    }
    let key = derive_aead_key(session_key);
    let cipher =
        Aes256Gcm::new_from_slice(&key).map_err(|_| "配对会话加密密钥格式无效。".to_string())?;
    let nonce_bytes: [u8; 12] = frame[..12]
        .try_into()
        .map_err(|_| "配对会话随机数长度无效。")?;
    let nonce = Nonce::from(nonce_bytes);
    let plaintext = cipher
        .decrypt(&nonce, &frame[12..])
        .map_err(|_| "配对会话加密校验失败。".to_string())?;
    serde_json::from_slice(&plaintext).map_err(|_| "配对会话载荷格式无效。".into())
}

async fn write_frame(stream: &mut TcpStream, frame: &[u8]) -> Result<(), String> {
    if frame.is_empty() || frame.len() > MAX_PAIRING_FRAME_BYTES {
        return Err("配对协议帧长度超出范围。".into());
    }
    tokio::time::timeout(Duration::from_secs(15), async {
        stream
            .write_all(&(frame.len() as u32).to_be_bytes())
            .await?;
        stream.write_all(frame).await?;
        stream.flush().await
    })
    .await
    .map_err(|_| "配对协议发送超时。".to_string())?
    .map_err(|_| "配对协议发送失败。".to_string())
}

async fn read_frame(stream: &mut TcpStream) -> Result<Vec<u8>, String> {
    tokio::time::timeout(Duration::from_secs(15), async {
        let mut length = [0u8; 4];
        stream.read_exact(&mut length).await?;
        let length = u32::from_be_bytes(length) as usize;
        if length == 0 || length > MAX_PAIRING_FRAME_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "invalid pairing frame length",
            ));
        }
        let mut frame = vec![0u8; length];
        stream.read_exact(&mut frame).await?;
        Ok(frame)
    })
    .await
    .map_err(|_| "配对协议读取超时。".to_string())?
    .map_err(|_| "配对协议帧无效或连接已关闭。".to_string())
}

fn validate_code(pairing_code: &str) -> Result<(), String> {
    if pairing_code.len() == 6 && pairing_code.bytes().all(|byte| byte.is_ascii_digit()) {
        Ok(())
    } else {
        Err("配对码必须是六位 ASCII 数字。".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use russh::keys::{Algorithm, PrivateKey};
    use std::sync::{Arc, Mutex};

    #[test]
    fn opaque_login_accepts_the_code_without_sending_it_and_closes_after_success() {
        let (mut server, issued) = PairingCodeWindow::open().unwrap();
        assert_eq!(issued.pairing_code.len(), 6);
        assert!(issued
            .pairing_code
            .bytes()
            .all(|byte| byte.is_ascii_digit()));

        let (client, request) = OpaqueClientStart::start(&issued.pairing_code).unwrap();
        assert!(!request
            .windows(6)
            .any(|window| window == issued.pairing_code.as_bytes()));
        let (attempt, response) = server.begin_attempt(&request).unwrap();
        let (finalization, client_key) = client.finish(&response).unwrap();
        let server_key = server.finish_attempt(attempt, &finalization).unwrap();
        assert_eq!(client_key.as_slice(), server_key.as_slice());
        assert!(!server.is_open());
        assert!(server.begin_attempt(&request).is_err());
    }

    #[test]
    fn wrong_code_fails_client_verification_and_consumes_an_attempt() {
        let (mut server, issued) = PairingCodeWindow::open().unwrap();
        let wrong = if issued.pairing_code == "000000" {
            "000001"
        } else {
            "000000"
        };
        let (client, request) = OpaqueClientStart::start(wrong).unwrap();
        let (_attempt, response) = server.begin_attempt(&request).unwrap();
        assert!(client.finish(&response).is_err());
        assert_eq!(server.failed_attempts_remaining(), MAX_ATTEMPTS - 1);
    }

    #[test]
    fn rejects_non_six_digit_codes_before_protocol_work() {
        assert!(OpaqueClientStart::start("12345").is_err());
        assert!(OpaqueClientStart::start("12a456").is_err());
    }

    #[tokio::test]
    async fn encrypted_tcp_pairing_binds_the_client_key_and_server_identity() {
        let (window, opened) = PairingCodeWindow::open().unwrap();
        let window = Arc::new(Mutex::new(Some(window)));
        let host_private = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let host_public = host_private.public_key().to_openssh().unwrap();
        let host_fingerprint = host_private
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let info = PairingServerInfo {
            host_id: "host-device-a".into(),
            display_name: "Host A".into(),
            username: "test_user".into(),
            ssh_port: 22,
            host_public_key: host_public,
            host_key_sha256: host_fingerprint,
            protocol_version: 1,
            fields: std::collections::BTreeMap::new(),
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server_window = window.clone();
        let server_info = info.clone();
        let accepted = Arc::new(Mutex::new(None::<PairingIdentity>));
        let accepted_slot = accepted.clone();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            accept_pairing_connection(stream, &server_window, &server_info, |identity| {
                *accepted_slot.lock().unwrap() = Some(identity.clone());
                Ok(())
            })
            .await
            .unwrap()
        });

        let client_private = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let client_public = client_private.public_key().to_openssh().unwrap();
        let client_fingerprint = client_private
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let client_key_data = client_private.key_data().ed25519().unwrap();
        let signing_key = SigningKey::from_bytes(client_key_data.private.as_ref());
        let client_info = connect_pairing_stream(
            TcpStream::connect(address).await.unwrap(),
            &opened.pairing_code,
            "client-device-a",
            &client_public,
            move |message| {
                Ok(base64::engine::general_purpose::STANDARD
                    .encode(signing_key.sign(message).to_bytes()))
            },
        )
        .await
        .unwrap();
        let server_identity = server.await.unwrap();
        assert_eq!(client_info, info);
        assert_eq!(server_identity.device_id, "client-device-a");
        assert_eq!(server_identity.fingerprint, client_fingerprint);
        assert_eq!(accepted.lock().unwrap().as_ref(), Some(&server_identity));
        assert!(window.lock().unwrap().as_ref().unwrap().completed);
    }

    #[test]
    fn authenticated_pairing_ciphertext_rejects_tampering() {
        let key = Zeroizing::new(vec![0x42; 64]);
        let mut encrypted = encrypt_payload(
            key.as_slice(),
            &PairingChallenge {
                server_nonce: "sample".into(),
            },
        )
        .unwrap();
        let last = encrypted.len() - 1;
        encrypted[last] ^= 1;
        assert!(decrypt_payload::<PairingChallenge>(key.as_slice(), &encrypted).is_err());
    }
}
