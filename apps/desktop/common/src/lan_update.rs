//! lan_update（局域网本体更新）：固定 FILE（文件）桥接中的独立引导协议，不依赖业务核心。
use crate::app_update::RetainedInstaller;
use base64::Engine;
use serde::{Deserialize, Serialize};
use std::{
    cmp::Ordering,
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::AtomicBool,
};

pub const PROTOCOL_VERSION: u32 = 1; // PROTOCOL_VERSION（稳定的版本同步引导协议）。
pub const CHUNK_BYTES: usize = 64 * 1024; // CHUNK_BYTES（单块原始字节上限）。
const MAX_FRAME_BYTES: usize = CHUNK_BYTES * 2; // MAX_FRAME_BYTES（含编码数据的控制帧上限）。

/// compare_versions（比较正式版本）：构建编号不参与比较，同显示版本直接视为一致。
pub fn compare_versions(left: &str, right: &str) -> Result<Ordering, String> {
    fn parse(value: &str) -> Result<semver::Version, String> {
        let version = semver::Version::parse(value).map_err(|_| "产品版本号无效。")?;
        if !version.pre.is_empty() || !version.build.is_empty() {
            return Err("局域网同步仅接受正式产品版本号。".into());
        }
        Ok(version)
    }
    Ok(parse(left)?.cmp(&parse(right)?))
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    Status {
        version: String,
    }, // Status（交换双方产品版本）；version（客户端版本）。
    Download, // Download（申请读取服务端当前版本分发器）。
    Upload {
        installer: RetainedInstaller,
    }, // Upload（申请接收较新版本）；installer（分发器身份）。
    Read {
        offset: u64,
    }, // Read（读取顺序分块）；offset（原始字节偏移）。
    Write {
        offset: u64,
        data_base64: String,
        sha256: String,
    }, // Write（写入顺序分块）；data_base64（编码字节）、sha256（分块摘要）。
    Finish,   // Finish（复核完整长度与摘要）。
    Apply,    // Apply（完整接收后交接本机更新）。
    Cancel,   // Cancel（取消当前传输）。
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub protocol_version: u32, // protocol_version（稳定引导协议版本）。
    pub operation: Operation,  // operation（固定更新操作）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明）。
}
impl Request {
    pub fn new(operation: Operation) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            operation,
            fields: descriptions(),
        }
    }
    pub fn validate(&self) -> Result<(), String> {
        if self.protocol_version != PROTOCOL_VERSION
            || self
                .fields
                .get("operation")
                .is_none_or(|s| s.trim().is_empty())
        {
            return Err("局域网更新引导协议不兼容或缺少字段说明。".into());
        }
        Ok(())
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Response {
    pub protocol_version: u32, // protocol_version（稳定引导协议版本）。
    pub host_id: String,       // host_id（服务端已配对设备编号）。
    pub version: String,       // version（服务端正式产品版本）。
    pub state: String,         // state（状态、就绪、分块、已验证、交接、取消或失败）。
    pub installer: Option<RetainedInstaller>, // installer（版本匹配的维护分发器身份）。
    pub offset: u64,           // offset（下一原始字节偏移）。
    pub data_base64: Option<String>, // data_base64（有界文件块编码）。
    pub sha256: Option<String>, // sha256（当前分块摘要）。
    pub error: Option<String>, // error（中文失败原因或维护分发器不可用原因）。
    pub update_state: Option<String>, // update_state（服务端最近的本体同步状态）。
    pub update_error: Option<String>, // update_error（服务端更新交接的失败原因）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明）。
}
impl Response {
    pub fn new(host_id: &str, version: &str, state: &str) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            host_id: host_id.into(),
            version: version.into(),
            state: state.into(),
            installer: None,
            offset: 0,
            data_base64: None,
            sha256: None,
            error: None,
            update_state: None,
            update_error: None,
            fields: descriptions(),
        }
    }
    pub fn validate(&self, host_id: &str) -> Result<(), String> {
        if self.protocol_version == PROTOCOL_VERSION && self.state == "error" {
            return Err(self
                .error
                .clone()
                .unwrap_or_else(|| "局域网版本同步失败。".into()));
        }
        compare_versions(&self.version, &self.version)?;
        if self.protocol_version != PROTOCOL_VERSION || self.host_id != host_id {
            return Err("局域网更新回执的设备身份或引导版本不匹配。".into());
        }
        Ok(())
    }
}

fn descriptions() -> BTreeMap<String, String> {
    [
        ("protocol_version", "独立版本同步引导协议版本"),
        (
            "operation",
            "固定状态、上传、下载、分块、验证、应用或取消操作",
        ),
        ("operation.version", "客户端正式产品版本"),
        ("operation.installer", "分发器的版本、大小、摘要与构建身份"),
        ("host_id", "已配对服务端设备编号"),
        ("version", "服务端正式产品版本"),
        ("state", "同步操作的实际状态"),
        ("installer", "受管维护分发器身份"),
        ("offset", "原始文件的下一字节偏移"),
        ("data_base64", "单块文件的编码内容"),
        ("sha256", "当前原始文件块的内容摘要"),
        ("error", "中文失败原因"),
        ("update_state", "服务端本体同步状态"),
        ("update_error", "服务端本体更新交接错误"),
        ("fields", "字段中文说明"),
    ]
    .into_iter()
    .map(|(k, v)| (k.into(), v.into()))
    .collect()
}

pub trait FileStream: Read + Write {} // FileStream（已认证文件桥接的双向字节流）。
impl<T: Read + Write> FileStream for T {}
pub trait Handler: Send {
    // Handler（一条已认证同步连接的正式处理器）。
    fn request(&mut self, request: Request) -> Result<Response, String>;
    fn flushed(&mut self, _response: &Response) -> Result<(), String> {
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestFrame {
    #[serde(rename = "type")]
    kind: String, // type（固定为 application_update 的文件通道类型）。
    request: Request, // request（本体同步请求）。
}

/// serve（同步连接分派）：交接回执完整写出之后才允许退出当前程序。
pub fn serve(
    stream: &mut dyn FileStream,
    first: Request,
    mut handler: Box<dyn Handler>,
) -> Result<(), String> {
    let mut request = first;
    loop {
        let response = request.validate().and_then(|()| handler.request(request));
        let response = match response {
            Ok(value) => value,
            Err(error) => {
                let mut value = Response::new("", "0.0.0", "error");
                value.error = Some(error.clone());
                write_response(stream, &value)?;
                return Err(error);
            }
        };
        write_response(stream, &response)?;
        handler.flushed(&response)?;
        if matches!(response.state.as_str(), "handing_off" | "cancelled") {
            return Ok(());
        }
        let mut prefix = [0; 4];
        // 正常完成后客户端关闭读取连接；未完成的接收由处理器析构回收。
        match stream.read_exact(&mut prefix) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(error) => return Err(format!("局域网更新连接中断：{error}")),
        }
        let length = u32::from_be_bytes(prefix) as usize;
        if length == 0 || length > MAX_FRAME_BYTES {
            return Err("局域网更新帧超出限制。".into());
        }
        let mut bytes = vec![0; length];
        stream.read_exact(&mut bytes).map_err(|e| e.to_string())?;
        let frame: RequestFrame =
            serde_json::from_slice(&bytes).map_err(|_| "局域网更新请求格式无效。")?;
        if frame.kind != "application_update" {
            return Err("同步连接不能切换为其他文件业务。".into());
        }
        request = frame.request;
    }
}
fn write_response(stream: &mut dyn FileStream, response: &Response) -> Result<(), String> {
    let bytes = serde_json::to_vec(response).map_err(|e| e.to_string())?;
    if bytes.len() > MAX_FRAME_BYTES {
        return Err("局域网更新回执超出限制。".into());
    }
    stream
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .and_then(|()| stream.write_all(&bytes))
        .and_then(|()| stream.flush())
        .map_err(|e| format!("局域网更新回执未完整写出：{e}"))
}

/// exchange（客户端正式协议交换）：每帧有界，支持取消与单次请求超时。
pub async fn exchange<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + ?Sized>(
    stream: &mut S,
    operation: Operation,
    cancel: &AtomicBool,
) -> Result<Response, String> {
    use std::sync::atomic::Ordering as AtomicOrdering;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let task = async {
        let frame = RequestFrame {
            kind: "application_update".into(),
            request: Request::new(operation),
        };
        let bytes = serde_json::to_vec(&frame).map_err(|e| e.to_string())?;
        if bytes.len() > MAX_FRAME_BYTES {
            return Err("局域网更新请求超出限制。".into());
        }
        stream
            .write_all(&(bytes.len() as u32).to_be_bytes())
            .await
            .map_err(|e| e.to_string())?;
        stream.write_all(&bytes).await.map_err(|e| e.to_string())?;
        stream.flush().await.map_err(|e| e.to_string())?;
        let mut prefix = [0; 4];
        stream
            .read_exact(&mut prefix)
            .await
            .map_err(|_| "对端未响应版本同步；请确认对端已支持局域网更新引导。")?;
        let length = u32::from_be_bytes(prefix) as usize;
        if length == 0 || length > MAX_FRAME_BYTES {
            return Err("局域网更新回执超出限制。".into());
        }
        let mut bytes = vec![0; length];
        stream
            .read_exact(&mut bytes)
            .await
            .map_err(|_| "局域网更新回执中断。")?;
        serde_json::from_slice(&bytes).map_err(|_| "对端未提供有效的局域网版本同步回执。".into())
    };
    tokio::select! {
        result = tokio::time::timeout(std::time::Duration::from_secs(60), task) => result.map_err(|_| "局域网版本同步请求超时。".to_string())?,
        _ = async { loop { if cancel.load(AtomicOrdering::Acquire) { break; } tokio::time::sleep(std::time::Duration::from_millis(50)).await; } } => Err("局域网版本同步已取消。".into()),
    }
}

pub fn chunk_hash(bytes: &[u8]) -> String {
    // chunk_hash（原始分块内容摘要）。
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}
pub fn decode_chunk(encoded: &str, expected: &str) -> Result<Vec<u8>, String> {
    if encoded.len() > CHUNK_BYTES.div_ceil(3) * 4 {
        return Err("局域网更新分块超过上限。".into());
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|_| "局域网更新分块编码无效。")?;
    if bytes.is_empty() || bytes.len() > CHUNK_BYTES || chunk_hash(&bytes) != expected {
        return Err("局域网更新分块摘要不一致。".into());
    }
    Ok(bytes)
}
pub fn validate_installer(record: &RetainedInstaller, version: &str) -> Result<(), String> {
    if !record.matches(version, &record.build)
        || record.build.is_empty()
        || record.build.len() > 128
        || !record
            .build
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return Err("分发器身份与目标产品版本不一致。".into());
    }
    Ok(())
}
pub fn verify_installer(path: &Path, record: &RetainedInstaller) -> Result<(), String> {
    verify_installer_cancellable(path, record, &AtomicBool::new(false))
}
pub fn verify_installer_cancellable(
    path: &Path,
    record: &RetainedInstaller,
    cancel: &AtomicBool,
) -> Result<(), String> {
    use sha2::{Digest, Sha256};
    validate_installer(record, &record.manifest.version)?;
    crate::device_file_channel::reject_reparse_chain(path)?;
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    if file.metadata().map_err(|e| e.to_string())?.len() != record.manifest.size {
        return Err("分发器文件长度不一致。".into());
    }
    let mut hash = Sha256::new();
    let mut buffer = vec![0; CHUNK_BYTES];
    loop {
        if cancel.load(std::sync::atomic::Ordering::Acquire) {
            return Err("局域网分发器校验已取消。".into());
        }
        let count = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    if !format!("{:x}", hash.finalize()).eq_ignore_ascii_case(&record.manifest.sha256) {
        return Err("分发器长度或完整内容摘要不一致。".into());
    }
    Ok(())
}

/// IncomingInstaller（本体更新候选）：复用当前工作区固定更新暂存，未交接时自动回收。
pub struct IncomingInstaller {
    pub path: PathBuf,                // path（本次受管安装器候选）。
    pub installer: RetainedInstaller, // installer（锁定目标身份）。
    pub offset: u64,                  // offset（已验证写入字节数）。
    file: Option<File>,               // file（本次维护准入保护的候选文件）。
    verified: bool,                   // verified（完整摘要已验证）。
    handed_off: bool,                 // handed_off（已经交给安装器，保留文件）。
}
impl IncomingInstaller {
    pub fn create(root: &Path, installer: RetainedInstaller) -> Result<Self, String> {
        validate_installer(&installer, &installer.manifest.version)?;
        // 更新准入锁由调用方持有，复用正式更新固定暂存位置及现有重复分发器清理规则。
        let path = crate::app_update::update_installer_path(root)?;
        let directory = path.parent().ok_or("更新暂存目录无效。")?;
        fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
        crate::device_file_channel::reject_reparse_chain(&path)?;
        let file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&path)
            .map_err(|e| e.to_string())?;
        Ok(Self {
            path,
            installer,
            offset: 0,
            file: Some(file),
            verified: false,
            handed_off: false,
        })
    }
    pub fn append(&mut self, offset: u64, encoded: &str, sha256: &str) -> Result<(), String> {
        if self.verified || offset != self.offset {
            return Err("更新分块偏移不匹配或候选已经完成。".into());
        }
        let bytes = decode_chunk(encoded, sha256)?;
        if self.offset + bytes.len() as u64 > self.installer.manifest.size {
            return Err("更新文件超过声明大小。".into());
        }
        self.file
            .as_mut()
            .ok_or("更新候选已关闭。")?
            .write_all(&bytes)
            .map_err(|e| e.to_string())?;
        self.offset += bytes.len() as u64;
        Ok(())
    }
    pub fn finish(&mut self) -> Result<(), String> {
        self.finish_cancellable(&AtomicBool::new(false))
    }
    pub fn finish_cancellable(&mut self, cancel: &AtomicBool) -> Result<(), String> {
        if self.verified {
            return Ok(());
        }
        if self.offset != self.installer.manifest.size {
            return Err("更新文件尚未完整接收。".into());
        }
        if let Some(file) = self.file.take() {
            file.sync_all().map_err(|e| e.to_string())?;
        }
        verify_installer_cancellable(&self.path, &self.installer, cancel)?;
        self.verified = true;
        Ok(())
    }
    pub fn handoff(&mut self) -> Result<(), String> {
        if !self.verified {
            return Err("更新候选尚未完整校验。".into());
        }
        self.handed_off = true;
        Ok(())
    }
}
impl Drop for IncomingInstaller {
    fn drop(&mut self) {
        self.file.take();
        if !self.handed_off {
            // 只删除本对象创建的已知文件与空目录，不递归删除，不触碰现用维护分发器。
            if crate::device_file_channel::reject_reparse_chain(&self.path).is_ok() {
                let _ = fs::remove_file(&self.path);
                if let Some(parent) = self.path.parent() {
                    let _ = fs::remove_dir(parent);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn record(bytes: &[u8]) -> RetainedInstaller {
        serde_json::from_value(serde_json::json!({"schema":1,"build":"product-test","fields":{},"manifest":{
            "version":"1.1.0","tag":"v1.1.0","asset":"VoiceTerminalSetup.exe","size":bytes.len(),"sha256":chunk_hash(bytes),"releaseNotes":"新增局域网升级",
            "fields":{"version":"产品版本","tag":"发行标签","asset":"分发器名称","size":"文件大小","sha256":"文件摘要","releaseNotes":"发行说明"}}})).unwrap()
    }
    fn root() -> PathBuf {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp/tests/lan-update")
            .join(crate::workspace::new_id());
        fs::create_dir_all(&root).unwrap();
        root.canonicalize().unwrap()
    }
    #[test]
    fn official_version_order_is_numeric_and_equal_versions_need_no_build() {
        assert_eq!(
            compare_versions("1.10.0", "1.9.9").unwrap(),
            Ordering::Greater
        );
        assert_eq!(compare_versions("1.0.0", "1.0.0").unwrap(), Ordering::Equal);
        assert_eq!(compare_versions("1.0.0", "2.0.0").unwrap(), Ordering::Less);
        assert!(compare_versions("bad", "1.0.0").is_err());
        assert!(compare_versions("1.0.0-preview", "1.0.0").is_err());
    }
    #[test]
    fn candidate_requires_correct_offsets_chunks_and_whole_file_before_handoff() {
        let root = root();
        let bytes = vec![42; 1024 * 1024];
        let mut incoming = IncomingInstaller::create(&root, record(&bytes)).unwrap();
        let candidate = incoming.path.clone();
        assert!(incoming.handoff().is_err());
        assert!(incoming.finish().is_err());
        let chunk = &bytes[..CHUNK_BYTES];
        let encoded = base64::engine::general_purpose::STANDARD.encode(chunk);
        assert!(incoming.append(1, &encoded, &chunk_hash(chunk)).is_err());
        assert!(incoming.append(0, &encoded, &"0".repeat(64)).is_err());
        for chunk in bytes.chunks(CHUNK_BYTES) {
            incoming
                .append(
                    incoming.offset,
                    &base64::engine::general_purpose::STANDARD.encode(chunk),
                    &chunk_hash(chunk),
                )
                .unwrap();
        }
        incoming.finish().unwrap();
        verify_installer(&candidate, &record(&bytes)).unwrap();
        assert!(incoming
            .append(incoming.offset, &encoded, &chunk_hash(chunk))
            .is_err());
        drop(incoming);
        assert!(!candidate.exists());
    }
    #[test]
    fn failed_whole_hash_and_aborted_upload_preserve_maintenance_source() {
        let root = root();
        fs::create_dir_all(root.join("maintenance")).unwrap();
        let maintenance = root.join("maintenance/VoiceTerminalSetup.exe");
        fs::write(&maintenance, b"keep").unwrap();
        let bytes = vec![1; 1024 * 1024];
        let mut installer = record(&bytes);
        installer.manifest.sha256 = "0".repeat(64);
        let mut incoming = IncomingInstaller::create(&root, installer).unwrap();
        let candidate = incoming.path.clone();
        for chunk in bytes.chunks(CHUNK_BYTES) {
            incoming
                .append(
                    incoming.offset,
                    &base64::engine::general_purpose::STANDARD.encode(chunk),
                    &chunk_hash(chunk),
                )
                .unwrap();
        }
        assert!(incoming.finish().is_err());
        drop(incoming);
        assert!(!candidate.exists());
        assert_eq!(fs::read(maintenance).unwrap(), b"keep");
    }
    #[test]
    fn receiver_verification_can_cancel_and_reclaims_only_its_candidate() {
        let root = root();
        let bytes = vec![9; 1024 * 1024];
        let mut incoming = IncomingInstaller::create(&root, record(&bytes)).unwrap();
        let path = incoming.path.clone();
        for chunk in bytes.chunks(CHUNK_BYTES) {
            incoming
                .append(
                    incoming.offset,
                    &base64::engine::general_purpose::STANDARD.encode(chunk),
                    &chunk_hash(chunk),
                )
                .unwrap();
        }
        let error = incoming
            .finish_cancellable(&AtomicBool::new(true))
            .unwrap_err();
        assert!(error.contains("取消"));
        assert!(incoming.handoff().is_err());
        drop(incoming);
        assert!(!path.exists());
    }
    #[test]
    fn incomplete_handoff_receipt_never_starts_the_update_worker() {
        use std::sync::{atomic::Ordering as AtomicOrdering, Arc};
        struct Apply(Arc<AtomicBool>);
        impl Handler for Apply {
            fn request(&mut self, _: Request) -> Result<Response, String> {
                Ok(Response::new("server-a", "1.0.0", "handing_off"))
            }
            fn flushed(&mut self, _: &Response) -> Result<(), String> {
                self.0.store(true, AtomicOrdering::Release);
                Ok(())
            }
        }
        struct BrokenWriter;
        impl Read for BrokenWriter {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                Ok(0)
            }
        }
        impl Write for BrokenWriter {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::Error::from(std::io::ErrorKind::BrokenPipe))
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let started = Arc::new(AtomicBool::new(false));
        assert!(serve(
            &mut BrokenWriter,
            Request::new(Operation::Apply),
            Box::new(Apply(started.clone()))
        )
        .is_err());
        assert!(!started.load(AtomicOrdering::Acquire));
    }
    #[test]
    fn protocol_rejects_unknown_operations_and_wrong_target() {
        assert!(serde_json::from_value::<Operation>(
            serde_json::json!({"action":"execute","path":"C:/other.exe"})
        )
        .is_err());
        assert!(Response::new("server-b", "1.1.0", "status")
            .validate("server-a")
            .is_err());
        let mut request = Request::new(Operation::Status {
            version: "1.0.0".into(),
        });
        request.protocol_version = 99;
        assert!(request.validate().is_err());
    }
    #[tokio::test]
    async fn stable_file_protocol_exchanges_version_before_any_business_protocol() {
        struct Versions;
        impl Handler for Versions {
            fn request(&mut self, request: Request) -> Result<Response, String> {
                match request.operation {
                    Operation::Status { version } => {
                        assert_eq!(version, "1.0.0");
                        Ok(Response::new("server-a", "1.2.0", "status"))
                    }
                    Operation::Cancel => Ok(Response::new("server-a", "1.2.0", "cancelled")),
                    _ => Err("未授权更新".into()),
                }
            }
        }
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut prefix = [0; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut bytes = vec![0; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut bytes).unwrap();
            let first: RequestFrame = serde_json::from_slice(&bytes).unwrap();
            serve(&mut stream, first.request, Box::new(Versions)).unwrap();
        });
        let mut stream = tokio::net::TcpStream::connect(address).await.unwrap();
        let cancel = AtomicBool::new(false);
        let response = exchange(
            &mut stream,
            Operation::Status {
                version: "1.0.0".into(),
            },
            &cancel,
        )
        .await
        .unwrap();
        response.validate("server-a").unwrap();
        assert_eq!(response.version, "1.2.0");
        assert_eq!(
            exchange(&mut stream, Operation::Cancel, &cancel)
                .await
                .unwrap()
                .state,
            "cancelled"
        );
        server.join().unwrap();
    }
}
