//! RemoteImport（远程导入）：把 SSH FILE 上传收敛到 ResourceManager 的来源票据与导入事务。
use crate::resource_commands::ResourceManager;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use voice_desktop_common::{
    device_file_channel::{self, FileChannelPeer, FileReceiver, UploadBegin},
    remote_import_contract::{
        valid_source_id, RemoteImportFileEntry, RemoteImportManifest, RemoteImportSourceTicket,
        REMOTE_IMPORT_MAX_BYTES,
    },
};

const REMOTE_IMPORT_TTL: Duration = Duration::from_secs(30 * 60); // REMOTE_IMPORT_TTL（与 ResourceManager 的 30 分钟来源票据时限一致）。
const MAX_ACTIVE_REMOTE_IMPORTS: usize = 32; // MAX_ACTIVE_REMOTE_IMPORTS（同一服务端允许同时占用的导入来源数）。
const MAX_REMOTE_IMPORT_RECORDS: usize = 128; // MAX_REMOTE_IMPORT_RECORDS（与 ResourceManager 的幂等来源记录上限一致）。
const MAX_CHUNK_RECEIPTS_PER_FILE: usize = 65_536; // MAX_CHUNK_RECEIPTS_PER_FILE（每文件幂等重复块收据数量上限）。
const MAX_REMOTE_IMPORT_STAGING_BYTES: u64 = 8 * 1024 * 1024 * 1024; // MAX_REMOTE_IMPORT_STAGING_BYTES（沿用设备文件传输内核的并行暂存总容量上限）。
const REMOTE_IMPORT_RESULT_TTL: Duration = Duration::from_secs(24 * 60 * 60); // REMOTE_IMPORT_RESULT_TTL（与 ResourceManager 的结果幂等保留时限一致）。
const INCOMING_DIR_PREFIX: &str = ".remote-import-incoming-"; // INCOMING_DIR_PREFIX（上传期间的服务端内部分块暂存目录名）。

/// RemoteImportReservation（远程导入预留回执）：可返回管理通道的唯一信息，不含服务器路径。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RemoteImportReservation {
    pub source_id: String, // source_id（服务端资源管理器签发的导入来源编号）。
    pub resource_id: Option<String>, // resource_id（服务端已绑定的目标资源；为空表示批量导入）。
    pub expires_at_ms: u64, // expires_at_ms（服务端来源预留到期时刻，Unix 毫秒）。
    pub fields: std::collections::BTreeMap<String, String>, // fields（字段中文说明）。
}

/// RemoteImportResourceApi（资源导入适配）：唯一转入 ResourceManager 的边界。
pub trait RemoteImportResourceApi: Send + Sync + 'static {
    /// reserve（预留来源）：返回由服务端创建的唯一票据。
    fn reserve(
        &self,
        request_id: &str,
        resource_id: Option<String>,
    ) -> Result<RemoteImportSourceTicket, String>;

    /// commit（提交来源）：只有全部文件通过服务端落盘复核后才允许进入既有导入任务。
    fn commit(
        &self,
        ticket: &RemoteImportSourceTicket,
        manifest: RemoteImportManifest,
    ) -> Result<Value, String>;

    /// abort（中止来源）：释放尚未提交的服务端暂存来源。
    fn abort(&self, ticket: &RemoteImportSourceTicket) -> Result<(), String>;
}

impl RemoteImportResourceApi for Arc<ResourceManager> {
    fn reserve(
        &self,
        request_id: &str,
        resource_id: Option<String>,
    ) -> Result<RemoteImportSourceTicket, String> {
        self.reserve_remote_import_source(request_id, resource_id)
    }

    fn commit(
        &self,
        ticket: &RemoteImportSourceTicket,
        manifest: RemoteImportManifest,
    ) -> Result<Value, String> {
        self.commit_remote_import_source(ticket, manifest)
    }

    fn abort(&self, ticket: &RemoteImportSourceTicket) -> Result<(), String> {
        self.abort_remote_import_source(ticket)
    }
}

/// RemoteImportCoordinator（远程导入协调器）：仅由服务端原生管理进程持有来源票据路径。
pub struct RemoteImportCoordinator {
    manager: Arc<dyn RemoteImportResourceApi>, // manager（ResourceManager 的唯一适配入口）。
    state: Mutex<CoordinatorState>, // state（绑定请求、设备、票据、上传状态和幂等结果的私有台账）。
}

/// CoordinatorState（协调器台账）：保存来源 ticket（票据）与原 request_id（请求编号）映射。
#[derive(Default)]
struct CoordinatorState {
    records: HashMap<String, ImportRecord>, // records（服务端 source_id 到原生来源记录的映射）。
    request_ids: HashMap<String, String>, // request_ids（稳定请求编号到服务端 source_id 的反向映射）。
}

/// ImportRecord（导入来源记录）：服务端原生状态，不可序列化给远端。
struct ImportRecord {
    ticket: RemoteImportSourceTicket, // ticket（ResourceManager 签发的服务端原生票据，禁止序列化）。
    request_id: String,               // request_id（来源预留的稳定业务请求编号）。
    resource_id: Option<String>,      // resource_id（服务端绑定的目标资源编号）。
    device_id: String,                // device_id（仅允许对应 SSH 桥接身份上传此来源）。
    expires_at: Instant,              // expires_at（原生单调时钟到期点）。
    expires_at_ms: u64,               // expires_at_ms（可返回管理通道的到期时间）。
    retain_until: Instant,            // retain_until（已完成或取消结果的幂等记录保留截止点）。
    status: ImportStatus,             // status（当前票据的预留、接收、提交或终态）。
}

enum ImportStatus {
    Reserved,
    Uploading(UploadSession),
    Committing(RemoteImportManifest),
    Committed {
        manifest: RemoteImportManifest,
        result: Value,
    },
    Aborted,
}

/// UploadSession（单来源上传会话）：限制单个 source_id（来源编号）的一条活动 FILE 连接。
struct UploadSession {
    manifest: RemoteImportManifest, // manifest（服务端绑定 source_id 的预期相对文件清单）。
    files: Vec<UploadFile>,         // files（每个文件的服务端目标、偏移、摘要与块收据）。
    current_file: usize,            // current_file（下一个允许写入的顺序文件索引）。
    incoming_dir: PathBuf,          // incoming_dir（本次传输私有分块目录，不暴露给客户端）。
    created_dirs: Vec<PathBuf>,     // created_dirs（本次上传创建的目录，仅清理这些目录）。
}

/// UploadFile（服务端文件状态）：仅写入 ticket.staging_dir（票据暂存目录）下的清单路径。
struct UploadFile {
    entry: RemoteImportFileEntry, // entry（服务端锁定的相对路径、预期长度与摘要）。
    final_path: PathBuf,          // final_path（manifest 相对路径映射的服务端暂存文件）。
    partial_path: PathBuf,        // partial_path（尚未通过完整摘要校验的服务端分块文件）。
    offset: u64,                  // offset（已按序确认的文件字节偏移）。
    digest: Sha256,               // digest（当前文件新增块的流式 SHA-256 状态）。
    receipts: HashMap<u64, ChunkReceipt>, // receipts（已接受分块偏移的幂等收据）。
    complete: bool,               // complete（当前文件是否已完整核验并原子改名）。
    partial_created: bool,        // partial_created（是否由本协调器创建了分块文件）。
    final_created: bool,          // final_created（是否由本协调器创建了已核验文件）。
}

/// ChunkReceipt（分块收据）：用于重复分块幂等确认。
#[derive(Clone)]
struct ChunkReceipt {
    length: usize,    // length（原始分块字节数）。
    sha256: String,   // sha256（原始分块摘要）。
    next_offset: u64, // next_offset（首次接受该块后的下一所需偏移）。
}

impl RemoteImportCoordinator {
    /// new（建立协调器）：ResourceManager 是唯一票据、事务与任务的权威实现。
    pub fn new(manager: Arc<ResourceManager>) -> Self {
        Self::with_resource_api(Arc::new(manager))
    }

    fn with_resource_api(manager: Arc<dyn RemoteImportResourceApi>) -> Self {
        Self {
            manager,
            state: Mutex::new(CoordinatorState::default()),
        }
    }

    /// reserve_source（预留远程来源）：只回传 source_id、resource_id 和到期时间，不回传暂存路径。
    pub fn reserve_source(
        &self,
        request_id: &str,
        resource_id: Option<String>,
        authenticated_device_id: &str,
    ) -> Result<RemoteImportReservation, String> {
        validate_request_id(request_id)?;
        validate_device_id(authenticated_device_id)?;
        if let Some(resource_id) = &resource_id {
            validate_resource_id(resource_id)?;
            if resource_id.starts_with("tts-aniflive-") && resource_id != "tts-aniflive-runtime" {
                return Err("远程设备的 AnifLive 模型仅支持服务端从在线网址导入；客户端模型目录上传暂不支持。".into());
            }
        }
        self.expire_idle_records();
        {
            let state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
            if let Some(source_id) = state.request_ids.get(request_id) {
                let record = state
                    .records
                    .get(source_id)
                    .ok_or("远程导入来源记录不完整。")?;
                if record.device_id != authenticated_device_id || record.resource_id != resource_id
                {
                    return Err("同一远程导入请求编号已绑定其他设备或目标资源。".into());
                }
                if matches!(&record.status, ImportStatus::Aborted) {
                    return Err("远程导入来源已取消或过期，请重新选择来源。".into());
                }
                return Ok(reservation(record));
            }
            let active = state
                .records
                .values()
                .filter(|record| {
                    !matches!(
                        &record.status,
                        ImportStatus::Committed { .. } | ImportStatus::Aborted
                    )
                })
                .count();
            if active >= MAX_ACTIVE_REMOTE_IMPORTS
                || state.records.len() >= MAX_REMOTE_IMPORT_RECORDS
            {
                return Err("远程导入并行来源或幂等记录已达上限。".into());
            }
        }

        let expires_at = Instant::now() + REMOTE_IMPORT_TTL;
        let expires_at_ms = now_unix_ms().saturating_add(REMOTE_IMPORT_TTL.as_millis() as u64);
        let retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
        let resource_request_id = resource_import_request_id(request_id, authenticated_device_id);
        let ticket = self
            .manager
            .reserve(&resource_request_id, resource_id.clone())?;
        if !valid_source_id(&ticket.source_id) || ticket.resource_id != resource_id {
            let _ = self.manager.abort(&ticket);
            return Err("ResourceManager 返回了不匹配的远程来源票据。".into());
        }
        if let Err(error) = validate_staging_root(&ticket.staging_dir) {
            let _ = self.manager.abort(&ticket);
            return Err(error);
        }
        let record = ImportRecord {
            ticket: ticket.clone(),
            request_id: request_id.to_owned(),
            resource_id,
            device_id: authenticated_device_id.to_owned(),
            expires_at,
            expires_at_ms,
            retain_until,
            status: ImportStatus::Reserved,
        };
        let mut state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
        if let Some(existing_source) = state.request_ids.get(request_id) {
            let existing = state
                .records
                .get(existing_source)
                .ok_or("远程导入来源记录不完整。")?;
            if existing.ticket.source_id != ticket.source_id
                || existing.device_id != authenticated_device_id
            {
                if existing.ticket.source_id != ticket.source_id {
                    let _ = self.manager.abort(&ticket);
                }
                return Err("同一远程导入请求编号已由其他服务端操作占用。".into());
            }
            return Ok(reservation(existing));
        }
        if state.records.contains_key(&ticket.source_id) {
            let _ = self.manager.abort(&ticket);
            return Err("ResourceManager 重复签发了已登记的远程来源编号。".into());
        }
        state
            .request_ids
            .insert(request_id.to_owned(), ticket.source_id.clone());
        state.records.insert(ticket.source_id.clone(), record);
        Ok(reservation(
            state
                .records
                .get(&ticket.source_id)
                .expect("刚写入的来源记录"),
        ))
    }

    /// serve_connection（服务 FILE 通道连接）：先验证本机强制命令桥接前导，再进入有界文件协议。
    pub fn serve_connection<S: std::io::Read + std::io::Write>(
        &self,
        stream: S,
    ) -> Result<(), String> {
        device_file_channel::serve_file_channel_connection(stream, self)
    }

    /// abort_reserved_source（取消远程来源）：管理通道按 SSH 前导身份撤销尚未提交的预留或上传。
    pub fn abort_reserved_source(
        &self,
        source_id: &str,
        authenticated_device_id: &str,
    ) -> Result<(), String> {
        if !valid_source_id(source_id) {
            return Err("远程导入来源编号无效。".into());
        }
        validate_device_id(authenticated_device_id)?;
        self.expire_idle_records();
        {
            let state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
            let record = state
                .records
                .get(source_id)
                .ok_or("远程导入来源不存在或已过期。")?;
            if record.device_id != authenticated_device_id {
                return Err("远程导入来源已绑定其他 SSH 客户端设备。".into());
            }
            validate_request_id(&record.request_id)?;
            if record.expires_at <= Instant::now() {
                return Err("远程导入来源票据已过期。".into());
            }
            if matches!(&record.status, ImportStatus::Aborted) {
                return Ok(());
            }
            if matches!(&record.status, ImportStatus::Committed { .. }) {
                return Err("导入任务已经接受；请按原任务编号查询或取消。".into());
            }
        }
        self.abort_source_for_device(authenticated_device_id, source_id)
    }

    fn expire_idle_records(&self) {
        let expired = {
            let Ok(mut state) = self.state.lock() else {
                return;
            };
            let now = Instant::now();
            let prunable = state
                .records
                .iter()
                .filter_map(|(id, record)| {
                    (record.retain_until <= now
                        && matches!(
                            &record.status,
                            ImportStatus::Committed { .. } | ImportStatus::Aborted
                        ))
                    .then_some((id.clone(), record.request_id.clone()))
                })
                .collect::<Vec<_>>();
            for (source_id, request_id) in prunable {
                state.records.remove(&source_id);
                if state
                    .request_ids
                    .get(&request_id)
                    .is_some_and(|current| current == &source_id)
                {
                    state.request_ids.remove(&request_id);
                }
            }
            let ids = state
                .records
                .iter()
                .filter_map(|(id, record)| {
                    (record.expires_at <= now
                        && !matches!(
                            &record.status,
                            ImportStatus::Committed { .. } | ImportStatus::Aborted
                        ))
                    .then_some(id.clone())
                })
                .collect::<Vec<_>>();
            ids.into_iter()
                .filter_map(|id| {
                    let record = state.records.get_mut(&id)?;
                    let upload = match std::mem::replace(&mut record.status, ImportStatus::Aborted)
                    {
                        ImportStatus::Uploading(upload) => Some(upload),
                        ImportStatus::Reserved => None,
                        other => {
                            record.status = other;
                            return None;
                        }
                    };
                    record.retain_until = now + REMOTE_IMPORT_RESULT_TTL;
                    Some((record.ticket.clone(), upload))
                })
                .collect::<Vec<_>>()
        };
        for (ticket, upload) in expired {
            if let Some(upload) = upload {
                cleanup_upload(&upload);
            }
            let _ = self.manager.abort(&ticket);
        }
    }

    fn abort_source(&self, peer: &FileChannelPeer, source_id: &str) -> Result<(), String> {
        self.abort_source_for_device(&peer.device_id, source_id)
    }

    fn abort_source_for_device(&self, device_id: &str, source_id: &str) -> Result<(), String> {
        let (ticket, upload, already_terminal) = {
            let mut state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
            let record = state
                .records
                .get_mut(source_id)
                .ok_or("远程导入来源不存在或已过期。")?;
            if record.device_id != device_id {
                return Err("远程导入来源已绑定其他 SSH 客户端设备。".into());
            }
            let status = std::mem::replace(&mut record.status, ImportStatus::Aborted);
            match status {
                ImportStatus::Committed { manifest, result } => {
                    record.status = ImportStatus::Committed { manifest, result };
                    (record.ticket.clone(), None, true)
                }
                ImportStatus::Aborted => (record.ticket.clone(), None, true),
                ImportStatus::Uploading(upload) => {
                    record.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
                    (record.ticket.clone(), Some(upload), false)
                }
                ImportStatus::Reserved => {
                    record.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
                    (record.ticket.clone(), None, false)
                }
                ImportStatus::Committing(manifest) => {
                    record.status = ImportStatus::Committing(manifest);
                    return Err("远程导入正在提交，不能再取消原任务。".into());
                }
            }
        };
        if already_terminal {
            return Ok(());
        }
        if let Some(upload) = upload {
            cleanup_upload(&upload);
        }
        self.manager.abort(&ticket)
    }
}

impl FileReceiver for RemoteImportCoordinator {
    fn begin(&self, peer: &FileChannelPeer, request: UploadBegin) -> Result<Option<Value>, String> {
        request.validate()?;
        let manifest = RemoteImportManifest {
            source_id: request.source_id.clone(),
            total_bytes: request.total_bytes,
            files: request.files,
        };
        let mut state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
        let reserved_bytes = state
            .records
            .values()
            .filter_map(|record| match &record.status {
                ImportStatus::Uploading(upload) => Some(upload.manifest.total_bytes),
                ImportStatus::Committing(manifest) => Some(manifest.total_bytes),
                _ => None,
            })
            .fold(0_u64, u64::saturating_add);
        let record = state
            .records
            .get_mut(&request.source_id)
            .ok_or("服务端没有签发该远程导入来源。")?;
        verify_peer(record, peer)?;
        if record.expires_at <= Instant::now() {
            return Err("远程导入来源票据已过期，请重新选择。".into());
        }
        if record.request_id != request.request_id {
            return Err("远程上传请求编号与服务端预留票据不匹配。".into());
        }
        match &mut record.status {
            ImportStatus::Reserved => {
                if reserved_bytes.saturating_add(manifest.total_bytes)
                    > MAX_REMOTE_IMPORT_STAGING_BYTES
                {
                    return Err("远程导入并行暂存容量超过 8 GiB 上限。".into());
                }
                let upload = create_upload_session(&record.ticket.staging_dir, manifest)?;
                record.status = ImportStatus::Uploading(upload);
                Ok(None)
            }
            ImportStatus::Uploading(upload) if upload.manifest == manifest => {
                Err("同一远程导入来源已有 FILE 上传连接在运行。".into())
            }
            ImportStatus::Committed {
                manifest: previous,
                result,
            } if previous == &manifest => Ok(Some(result.clone())),
            ImportStatus::Committing(previous) if previous == &manifest => {
                Err("同一远程导入请求仍在提交；请查询原任务状态。".into())
            }
            ImportStatus::Aborted => Err("远程导入来源已经取消或失效，请重新选择来源。".into()),
            _ => Err("远程导入来源已经绑定其他文件清单。".into()),
        }
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
        if bytes.is_empty()
            || bytes.len() > device_file_channel::MAX_FILE_CHUNK_BYTES
            || !valid_sha256(chunk_sha256)
            || sha256_hex(bytes) != chunk_sha256
        {
            return Err("远程文件分块为空、超限或摘要不匹配。".into());
        }
        let mut state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
        let record = state
            .records
            .get_mut(source_id)
            .ok_or("服务端没有签发该远程导入来源。")?;
        verify_peer(record, peer)?;
        if record.expires_at <= Instant::now() {
            return Err("远程导入来源票据已过期。".into());
        }
        let ImportStatus::Uploading(upload) = &mut record.status else {
            return Err("远程导入来源当前不接受文件分块。".into());
        };
        let file = upload
            .files
            .get_mut(file_index)
            .ok_or("远程文件序号超出已签发清单。")?;
        if let Some(receipt) = file.receipts.get(&offset) {
            if receipt.length != bytes.len() || receipt.sha256 != chunk_sha256 {
                return Err("同一文件偏移收到冲突的重复分块。".into());
            }
            return Ok(receipt.next_offset);
        }
        if file.receipts.len() >= MAX_CHUNK_RECEIPTS_PER_FILE {
            return Err("远程文件分块数量超过幂等收据上限。".into());
        }
        if file_index != upload.current_file || file.complete || offset != file.offset {
            return Err("远程文件分块偏移重复、越序或不属于当前文件。".into());
        }
        let next_offset = file
            .offset
            .checked_add(bytes.len() as u64)
            .ok_or("远程文件分块偏移发生溢出。")?;
        if next_offset > file.entry.byte_length || next_offset > REMOTE_IMPORT_MAX_BYTES {
            return Err("远程文件分块超过票据锁定的文件长度。".into());
        }
        ensure_private_regular_parent(&upload.incoming_dir)?;
        let mut output = if file.partial_created {
            if is_reparse(&file.partial_path) {
                return Err("远程文件分块暂存项不能是链接。".into());
            }
            OpenOptions::new()
                .append(true)
                .write(true)
                .open(&file.partial_path)
                .map_err(|_| "远程文件分块暂存写入失败。".to_string())?
        } else {
            if file.partial_path.exists() || is_reparse(&file.partial_path) {
                return Err("远程文件分块暂存目标已被占用。".into());
            }
            let output = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&file.partial_path)
                .map_err(|_| "远程文件分块暂存创建失败。".to_string())?;
            file.partial_created = true;
            output
        };
        output
            .write_all(bytes)
            .map_err(|_| "远程文件分块暂存写入失败。".to_string())?;
        output
            .flush()
            .map_err(|_| "远程文件分块暂存刷新失败。".to_string())?;
        file.digest.update(bytes);
        file.offset = next_offset;
        file.receipts.insert(
            offset,
            ChunkReceipt {
                length: bytes.len(),
                sha256: chunk_sha256.to_owned(),
                next_offset,
            },
        );
        if file.offset == file.entry.byte_length {
            output
                .sync_all()
                .map_err(|_| "远程文件分块暂存无法持久化。".to_string())?;
            drop(output);
            verify_file_digest(&file.partial_path, &file.entry)?;
            if file.final_path.exists() || is_reparse(&file.final_path) {
                return Err("远程导入相对路径在提交前已被占用。".into());
            }
            fs::rename(&file.partial_path, &file.final_path)
                .map_err(|_| "完整远程文件无法提交到服务端暂存目录。".to_string())?;
            file.partial_created = false;
            file.final_created = true;
            file.complete = true;
            upload.current_file = upload.files.iter().take_while(|item| item.complete).count();
        }
        Ok(next_offset)
    }

    fn finish(&self, peer: &FileChannelPeer, source_id: &str) -> Result<Value, String> {
        let (ticket, manifest, file_paths, upload) = {
            let mut state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
            let record = state
                .records
                .get_mut(source_id)
                .ok_or("服务端没有签发该远程导入来源。")?;
            verify_peer(record, peer)?;
            match &record.status {
                ImportStatus::Committed { result, .. } => return Ok(result.clone()),
                ImportStatus::Committing(_) => {
                    return Err("远程导入仍在提交；请查询原任务状态。".into())
                }
                _ => {}
            }
            let ImportStatus::Uploading(current) = &record.status else {
                return Err("远程导入来源尚未接收完整文件清单。".into());
            };
            if current.current_file != current.files.len()
                || current.files.iter().any(|file| !file.complete)
            {
                return Err("远程导入仍有未接收或未校验的文件。".into());
            }
            let manifest = current.manifest.clone();
            manifest.validate()?;
            let status = std::mem::replace(&mut record.status, ImportStatus::Aborted);
            let ImportStatus::Uploading(upload) = status else {
                unreachable!("状态已在上方检查")
            };
            let file_paths = upload
                .files
                .iter()
                .map(|file| (file.final_path.clone(), file.entry.clone()))
                .collect::<Vec<_>>();
            let ticket = record.ticket.clone();
            record.status = ImportStatus::Committing(manifest.clone());
            (ticket, manifest, file_paths, upload)
        };

        if let Err(error) = fs::remove_dir(&upload.incoming_dir) {
            cleanup_upload(&upload);
            let _ = self.manager.abort(&ticket);
            self.mark_aborted(source_id);
            return Err(if error.kind() == std::io::ErrorKind::NotFound {
                "服务端远程分块目录缺失。".into()
            } else {
                "服务端远程分块目录无法收尾。".into()
            });
        }
        let verified = verify_staging_manifest(&ticket.staging_dir, &manifest, &file_paths);
        if let Err(error) = verified {
            cleanup_upload(&upload);
            let _ = self.manager.abort(&ticket);
            self.mark_aborted(source_id);
            return Err(error);
        }
        let result = match self.manager.commit(&ticket, manifest.clone()) {
            Ok(result) => result,
            Err(error) => {
                cleanup_upload(&upload);
                let _ = self.manager.abort(&ticket);
                self.mark_aborted(source_id);
                return Err(error);
            }
        };
        let mut state = self.state.lock().map_err(|_| "远程导入来源状态不可用。")?;
        let record = state
            .records
            .get_mut(source_id)
            .ok_or("远程导入提交后来源记录丢失。")?;
        record.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
        record.status = ImportStatus::Committed {
            manifest,
            result: result.clone(),
        };
        Ok(result)
    }

    fn abort(&self, peer: &FileChannelPeer, source_id: &str) -> Result<(), String> {
        self.abort_source(peer, source_id)
    }
}

impl RemoteImportCoordinator {
    fn mark_aborted(&self, source_id: &str) {
        if let Ok(mut state) = self.state.lock() {
            if let Some(record) = state.records.get_mut(source_id) {
                record.status = ImportStatus::Aborted;
                record.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
            }
        }
    }
}

fn reservation(record: &ImportRecord) -> RemoteImportReservation {
    RemoteImportReservation {
        source_id: record.ticket.source_id.clone(),
        resource_id: record.resource_id.clone(),
        expires_at_ms: record.expires_at_ms,
        fields: std::collections::BTreeMap::from([
            ("source_id".into(), "服务端签发的远程导入来源编号".into()),
            (
                "resource_id".into(),
                "服务端已绑定的目标资源编号；为空表示批量导入".into(),
            ),
            ("expires_at_ms".into(), "来源预留到期时刻，Unix 毫秒".into()),
        ]),
    }
}

fn verify_peer(record: &ImportRecord, peer: &FileChannelPeer) -> Result<(), String> {
    if record.device_id != peer.device_id {
        return Err("远程导入来源已绑定其他 SSH 客户端设备。".into());
    }
    Ok(())
}

fn validate_request_id(value: &str) -> Result<(), String> {
    if value.len() != 32
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("远程导入请求编号必须是 32 位小写十六进制值。".into());
    }
    Ok(())
}

fn resource_import_request_id(request_id: &str, authenticated_device_id: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(b"voice-terminal-remote-import-v1\0");
    digest.update((authenticated_device_id.len() as u32).to_be_bytes());
    digest.update(authenticated_device_id.as_bytes());
    digest.update((request_id.len() as u32).to_be_bytes());
    digest.update(request_id.as_bytes());
    digest
        .finalize()
        .iter()
        .take(16)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn validate_device_id(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 128
        || value.trim() != value
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        return Err("远程导入客户端设备编号无效。".into());
    }
    Ok(())
}

fn validate_resource_id(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 128
        || value.trim() != value
        || value.chars().any(char::is_control)
        || value.contains(['/', '\\', ':'])
    {
        return Err("远程导入目标资源编号无效。".into());
    }
    Ok(())
}

fn validate_staging_root(root: &Path) -> Result<(), String> {
    if !root.is_absolute()
        || root
            .components()
            .any(|component| matches!(component, Component::ParentDir | Component::CurDir))
    {
        return Err("ResourceManager 签发的服务端暂存目录无效。".into());
    }
    reject_reparse_chain(root)?;
    let metadata = fs::symlink_metadata(root)
        .map_err(|_| "ResourceManager 尚未建立服务端远程导入暂存目录。".to_string())?;
    if is_reparse_metadata(&metadata) || !metadata.is_dir() {
        return Err("ResourceManager 签发的服务端暂存目录不是安全目录。".into());
    }
    Ok(())
}

fn create_upload_session(
    root: &Path,
    manifest: RemoteImportManifest,
) -> Result<UploadSession, String> {
    manifest.validate()?;
    validate_staging_root(root)?;
    if fs::read_dir(root)
        .map_err(|_| "服务端导入暂存目录无法列举。")?
        .next()
        .is_some()
    {
        return Err("服务端新导入暂存目录不为空，拒绝覆盖既有内容。".into());
    }
    let incoming_dir = root.join(format!("{INCOMING_DIR_PREFIX}{}", manifest.source_id));
    let reserved_name = incoming_dir
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or("服务端分块目录名称无效。")?;
    if manifest.files.iter().any(|entry| {
        entry
            .relative_path
            .split('/')
            .next()
            .is_some_and(|component| component.eq_ignore_ascii_case(reserved_name))
    }) {
        return Err("远程导入清单使用了服务端保留的暂存目录名称。".into());
    }
    fs::create_dir(&incoming_dir).map_err(|_| "服务端远程分块目录无法安全创建。".to_string())?;
    let mut created_dirs = vec![incoming_dir.clone()];
    let mut files = Vec::with_capacity(manifest.files.len());
    for (index, entry) in manifest.files.iter().enumerate() {
        let final_path = match destination_path(root, &entry.relative_path, &mut created_dirs) {
            Ok(path) => path,
            Err(error) => {
                cleanup_paths(&[], &created_dirs);
                return Err(error);
            }
        };
        if final_path.exists() || is_reparse(&final_path) {
            cleanup_paths(&[], &created_dirs);
            return Err("远程导入相对路径已在服务端暂存目录中占用。".into());
        }
        let partial_path = incoming_dir.join(format!("{index:04x}.part"));
        files.push(UploadFile {
            entry: entry.clone(),
            final_path,
            partial_path,
            offset: 0,
            digest: Sha256::new(),
            receipts: HashMap::new(),
            complete: false,
            partial_created: false,
            final_created: false,
        });
    }
    Ok(UploadSession {
        manifest,
        files,
        current_file: 0,
        incoming_dir,
        created_dirs,
    })
}

fn destination_path(
    root: &Path,
    relative: &str,
    created_dirs: &mut Vec<PathBuf>,
) -> Result<PathBuf, String> {
    let mut current = root.to_path_buf();
    let components = relative.split('/').collect::<Vec<_>>();
    if components.is_empty()
        || components
            .iter()
            .any(|part| part.is_empty() || *part == "." || *part == "..")
    {
        return Err("远程导入清单含无效相对路径段。".into());
    }
    for (index, component) in components.iter().enumerate() {
        current.push(component);
        if index + 1 == components.len() {
            break;
        }
        match fs::symlink_metadata(&current) {
            Ok(metadata) if is_reparse_metadata(&metadata) || !metadata.is_dir() => {
                return Err("远程导入路径父项不是安全目录。".into());
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                fs::create_dir(&current)
                    .map_err(|_| "远程导入相对目录无法安全创建。".to_string())?;
                created_dirs.push(current.clone());
            }
            Err(_) => return Err("远程导入路径父项状态无法读取。".into()),
        }
    }
    Ok(current)
}

fn ensure_private_regular_parent(path: &Path) -> Result<(), String> {
    reject_reparse_chain(path)?;
    let metadata =
        fs::symlink_metadata(path).map_err(|_| "服务端远程分块目录不可读取。".to_string())?;
    if is_reparse_metadata(&metadata) || !metadata.is_dir() {
        return Err("服务端远程分块目录不是安全目录。".into());
    }
    Ok(())
}

fn verify_file_digest(path: &Path, entry: &RemoteImportFileEntry) -> Result<(), String> {
    reject_reparse_chain(path)?;
    let metadata =
        fs::symlink_metadata(path).map_err(|_| "完整远程文件暂存项不可读取。".to_string())?;
    if is_reparse_metadata(&metadata) || !metadata.is_file() || metadata.len() != entry.byte_length
    {
        return Err("完整远程文件长度或文件类型与服务端清单不匹配。".into());
    }
    let (bytes, digest) = digest_file(path)?;
    if bytes != entry.byte_length || digest != entry.sha256 {
        return Err("完整远程文件的 SHA-256 摘要与服务端清单不匹配。".into());
    }
    Ok(())
}

fn verify_staging_manifest(
    root: &Path,
    manifest: &RemoteImportManifest,
    file_paths: &[(PathBuf, RemoteImportFileEntry)],
) -> Result<(), String> {
    validate_staging_root(root)?;
    if file_paths.len() != manifest.files.len() {
        return Err("远程导入暂存文件数与清单不一致。".into());
    }
    for ((path, actual), expected) in file_paths.iter().zip(&manifest.files) {
        if actual != expected {
            return Err("远程导入暂存清单在接收期间发生变化。".into());
        }
        verify_file_digest(path, expected)?;
    }
    let expected = manifest
        .files
        .iter()
        .map(|file| file.relative_path.to_ascii_lowercase())
        .collect::<HashSet<_>>();
    let mut actual = HashSet::new();
    collect_staged_file_paths(root, root, &mut actual)?;
    if actual != expected {
        return Err("服务端导入暂存目录含缺失或未登记文件。".into());
    }
    Ok(())
}

fn collect_staged_file_paths(
    root: &Path,
    current: &Path,
    files: &mut HashSet<String>,
) -> Result<(), String> {
    let entries = fs::read_dir(current).map_err(|_| "服务端导入暂存目录无法复核。".to_string())?;
    for entry in entries {
        let entry = entry.map_err(|_| "服务端导入暂存目录含无法读取的条目。".to_string())?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)
            .map_err(|_| "服务端导入暂存条目状态无法读取。".to_string())?;
        if is_reparse_metadata(&metadata) {
            return Err("服务端导入暂存目录含链接或重解析点。".into());
        }
        if metadata.is_dir() {
            collect_staged_file_paths(root, &path, files)?;
        } else if metadata.is_file() {
            let relative = path
                .strip_prefix(root)
                .map_err(|_| "服务端导入文件越出暂存目录。")?;
            let value = relative
                .to_str()
                .ok_or("服务端暂存路径不是有效 Unicode 文件名。")?
                .replace('\\', "/")
                .to_ascii_lowercase();
            files.insert(value);
        } else {
            return Err("服务端导入暂存目录含特殊文件。".into());
        }
    }
    Ok(())
}

fn digest_file(path: &Path) -> Result<(u64, String), String> {
    let mut file = File::open(path).map_err(|_| "服务端暂存文件无法读取。".to_string())?;
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|_| "服务端暂存文件无法完整读取。".to_string())?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(read as u64)
            .ok_or("服务端暂存文件长度溢出。")?;
        if total > REMOTE_IMPORT_MAX_BYTES {
            return Err("服务端暂存文件超过 4 GiB 上限。".into());
        }
        digest.update(&buffer[..read]);
    }
    Ok((total, sha256_final(digest)))
}

fn cleanup_upload(upload: &UploadSession) {
    let mut files = Vec::new();
    for file in &upload.files {
        if file.partial_created {
            files.push(file.partial_path.clone());
        }
        if file.final_created {
            files.push(file.final_path.clone());
        }
    }
    cleanup_paths(&files, &upload.created_dirs);
}

fn cleanup_paths(files: &[PathBuf], directories: &[PathBuf]) {
    for path in files {
        if let Ok(metadata) = fs::symlink_metadata(path) {
            if !is_reparse_metadata(&metadata) && metadata.is_file() {
                let _ = fs::remove_file(path);
            }
        }
    }
    let mut ordered = directories.to_vec();
    ordered.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
    for path in ordered {
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            if !is_reparse_metadata(&metadata) && metadata.is_dir() {
                let _ = fs::remove_dir(path);
            }
        }
    }
}

fn reject_reparse_chain(path: &Path) -> Result<(), String> {
    device_file_channel::reject_reparse_chain(path)
}

fn is_reparse(path: &Path) -> bool {
    fs::symlink_metadata(path)
        .map(|metadata| is_reparse_metadata(&metadata))
        .unwrap_or(false)
}

fn is_reparse_metadata(metadata: &fs::Metadata) -> bool {
    device_file_channel::metadata_is_reparse(metadata)
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

fn sha256_final(digest: Sha256) -> String {
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u64::MAX as u128) as u64
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct FakeResourceManager {
        root: PathBuf,        // root（隔离测试使用的服务端暂存目录）。
        commits: AtomicUsize, // commits（记录 ResourceManager 提交调用次数）。
        aborts: AtomicUsize,  // aborts（记录 ResourceManager 回滚调用次数）。
    }

    impl RemoteImportResourceApi for FakeResourceManager {
        fn reserve(
            &self,
            request_id: &str,
            resource_id: Option<String>,
        ) -> Result<RemoteImportSourceTicket, String> {
            validate_staging_root(&self.root)?;
            let source_id = request_id.chars().take(32).collect::<String>();
            Ok(RemoteImportSourceTicket {
                source_id,
                resource_id,
                staging_dir: self.root.clone(),
            })
        }

        fn commit(
            &self,
            _ticket: &RemoteImportSourceTicket,
            manifest: RemoteImportManifest,
        ) -> Result<Value, String> {
            manifest.validate()?;
            self.commits.fetch_add(1, Ordering::AcqRel);
            Ok(serde_json::json!({"job_id":"import-job-1","status":"accepted"}))
        }

        fn abort(&self, _ticket: &RemoteImportSourceTicket) -> Result<(), String> {
            self.aborts.fetch_add(1, Ordering::AcqRel);
            Ok(())
        }
    }

    fn temp_root(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "remote-import-{label}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ))
    }

    fn coordinator() -> (RemoteImportCoordinator, Arc<FakeResourceManager>) {
        let root = temp_root("stage");
        fs::create_dir_all(&root).unwrap();
        let manager = Arc::new(FakeResourceManager {
            root,
            commits: AtomicUsize::new(0),
            aborts: AtomicUsize::new(0),
        });
        (
            RemoteImportCoordinator::with_resource_api(manager.clone()),
            manager,
        )
    }

    fn peer() -> FileChannelPeer {
        FileChannelPeer {
            device_id: "device-a".into(),
            workspace_id: "a".repeat(32),
            owner_sid: "S-1-5-21-42".into(),
            build: "build-1".into(),
        }
    }

    fn request(source_id: String, paths: &[(&str, &[u8])]) -> UploadBegin {
        let files = paths
            .iter()
            .map(|(path, content)| RemoteImportFileEntry {
                relative_path: (*path).into(),
                byte_length: content.len() as u64,
                sha256: sha256_hex(content),
            })
            .collect::<Vec<_>>();
        UploadBegin {
            source_id,
            request_id: "0123456789abcdef0123456789abcdef".into(),
            total_bytes: files.iter().map(|file| file.byte_length).sum(),
            files,
        }
    }

    fn upload(
        coordinator: &RemoteImportCoordinator,
        peer: &FileChannelPeer,
        request: &UploadBegin,
        paths: &[(&str, &[u8])],
    ) -> Result<Value, String> {
        let manifest = request.clone();
        coordinator.begin(peer, manifest)?;
        for (index, (_, content)) in paths.iter().enumerate() {
            for (chunk_index, chunk) in content
                .chunks(device_file_channel::MAX_FILE_CHUNK_BYTES)
                .enumerate()
            {
                let offset = (chunk_index * device_file_channel::MAX_FILE_CHUNK_BYTES) as u64;
                coordinator.append_chunk(
                    peer,
                    &request.source_id,
                    index,
                    offset,
                    &sha256_hex(chunk),
                    chunk,
                )?;
            }
        }
        coordinator.finish(peer, &request.source_id)
    }

    #[test]
    fn reserve_binds_server_ticket_to_authenticated_device_and_does_not_expose_staging_path() {
        let (coordinator, manager) = coordinator();
        let reservation = coordinator
            .reserve_source(
                "0123456789abcdef0123456789abcdef",
                Some("vad".into()),
                "device-a",
            )
            .unwrap();
        let serialized = serde_json::to_string(&reservation).unwrap();
        assert!(serialized.contains(&reservation.source_id));
        assert!(!serialized.contains(manager.root.to_string_lossy().as_ref()));
        assert!(coordinator
            .reserve_source(
                "0123456789abcdef0123456789abcdef",
                Some("vad".into()),
                "device-b"
            )
            .is_err());
        assert!(coordinator
            .reserve_source(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                Some("tts-aniflive-tangyuge-v2pp-d78cf598".into()),
                "device-a",
            )
            .unwrap_err()
            .contains("在线网址导入"));
        fs::remove_dir_all(manager.root.clone()).unwrap();
    }

    #[test]
    fn resource_request_scope_is_stable_per_request_and_separate_per_device() {
        let request_id = "0123456789abcdef0123456789abcdef";
        assert_eq!(
            resource_import_request_id(request_id, "device-a"),
            resource_import_request_id(request_id, "device-a")
        );
        assert_ne!(
            resource_import_request_id(request_id, "device-a"),
            resource_import_request_id(request_id, "device-b")
        );
        assert_eq!(resource_import_request_id(request_id, "device-a").len(), 32);
    }

    #[test]
    fn import_receives_sequential_chunks_verifies_all_hashes_then_calls_existing_commit_once() {
        let (coordinator, manager) = coordinator();
        let reservation = coordinator
            .reserve_source("0123456789abcdef0123456789abcdef", None, "device-a")
            .unwrap();
        let data = b"model payload";
        let request = request(reservation.source_id.clone(), &[("models/model.bin", data)]);
        let result = upload(
            &coordinator,
            &peer(),
            &request,
            &[("models/model.bin", data)],
        )
        .unwrap();
        assert_eq!(result["job_id"], "import-job-1");
        assert_eq!(manager.commits.load(Ordering::Acquire), 1);
        assert_eq!(
            fs::read(manager.root.join("models/model.bin")).unwrap(),
            data
        );
        assert_eq!(
            coordinator.finish(&peer(), &reservation.source_id).unwrap(),
            result
        );
        assert_eq!(manager.commits.load(Ordering::Acquire), 1);
        fs::remove_dir_all(manager.root.clone()).unwrap();
    }

    #[test]
    fn path_traversal_and_manifest_conflicts_are_rejected_before_staging() {
        let (coordinator, manager) = coordinator();
        let reservation = coordinator
            .reserve_source("0123456789abcdef0123456789abcdef", None, "device-a")
            .unwrap();
        for path in [
            "../escape.bin",
            "C:/escape.bin",
            "folder\\escape.bin",
            "CON.txt",
        ] {
            let request = request(reservation.source_id.clone(), &[(path, b"x")]);
            assert!(coordinator.begin(&peer(), request).is_err(), "{path}");
        }
        assert_eq!(manager.aborts.load(Ordering::Acquire), 0);
        fs::remove_dir_all(manager.root.clone()).unwrap();
    }

    #[test]
    fn duplicate_chunk_is_idempotent_but_conflicting_duplicate_and_wrong_offset_fail() {
        let (coordinator, manager) = coordinator();
        let reservation = coordinator
            .reserve_source("0123456789abcdef0123456789abcdef", None, "device-a")
            .unwrap();
        let bytes = b"first block";
        let request = request(reservation.source_id.clone(), &[("file.bin", bytes)]);
        coordinator.begin(&peer(), request.clone()).unwrap();
        let digest = sha256_hex(bytes);
        assert_eq!(
            coordinator
                .append_chunk(&peer(), &reservation.source_id, 0, 0, &digest, bytes)
                .unwrap(),
            bytes.len() as u64
        );
        assert_eq!(
            coordinator
                .append_chunk(&peer(), &reservation.source_id, 0, 0, &digest, bytes)
                .unwrap(),
            bytes.len() as u64
        );
        assert!(coordinator
            .append_chunk(&peer(), &reservation.source_id, 0, 1, &digest, bytes)
            .is_err());
        assert!(coordinator
            .append_chunk(&peer(), &reservation.source_id, 0, 0, &digest, b"different")
            .is_err());
        coordinator.abort(&peer(), &reservation.source_id).unwrap();
        assert_eq!(manager.aborts.load(Ordering::Acquire), 1);
        assert!(!manager.root.join("file.bin").exists());
        fs::remove_dir_all(manager.root.clone()).unwrap();
    }

    #[test]
    fn bad_final_digest_and_oversize_chunk_abort_the_ticket() {
        let (coordinator, manager) = coordinator();
        let reservation = coordinator
            .reserve_source("0123456789abcdef0123456789abcdef", None, "device-a")
            .unwrap();
        let requested = request(reservation.source_id.clone(), &[("file.bin", b"good")]);
        coordinator.begin(&peer(), requested).unwrap();
        let wrong = b"evil";
        assert!(coordinator
            .append_chunk(
                &peer(),
                &reservation.source_id,
                0,
                0,
                &sha256_hex(wrong),
                wrong
            )
            .is_err());
        let oversized = vec![1; device_file_channel::MAX_FILE_CHUNK_BYTES + 1];
        assert!(coordinator
            .append_chunk(
                &peer(),
                &reservation.source_id,
                0,
                0,
                &sha256_hex(&oversized),
                &oversized
            )
            .is_err());
        coordinator.abort(&peer(), &reservation.source_id).unwrap();
        assert_eq!(manager.commits.load(Ordering::Acquire), 0);
        fs::remove_dir_all(manager.root.clone()).unwrap();
    }

    #[test]
    fn incomplete_disconnect_abort_removes_only_files_owned_by_upload() {
        let (coordinator, manager) = coordinator();
        let reservation = coordinator
            .reserve_source("0123456789abcdef0123456789abcdef", None, "device-a")
            .unwrap();
        let data = b"payload split";
        let request = request(reservation.source_id.clone(), &[("nested/file.bin", data)]);
        coordinator.begin(&peer(), request).unwrap();
        let first = &data[..4];
        coordinator
            .append_chunk(
                &peer(),
                &reservation.source_id,
                0,
                0,
                &sha256_hex(first),
                first,
            )
            .unwrap();
        coordinator.abort(&peer(), &reservation.source_id).unwrap();
        assert!(!manager.root.join("nested/file.bin").exists());
        assert!(fs::read_dir(&manager.root).unwrap().next().is_none());
        fs::remove_dir_all(manager.root.clone()).unwrap();
    }

    #[test]
    fn invalid_target_device_cannot_upload_or_cancel_a_reserved_ticket() {
        let (coordinator, manager) = coordinator();
        let reservation = coordinator
            .reserve_source("0123456789abcdef0123456789abcdef", None, "device-a")
            .unwrap();
        let mut wrong_peer = peer();
        wrong_peer.device_id = "device-b".into();
        let request = request(reservation.source_id.clone(), &[("x.bin", b"x")]);
        assert!(coordinator.begin(&wrong_peer, request).is_err());
        assert!(coordinator
            .abort(&wrong_peer, &reservation.source_id)
            .is_err());
        assert_eq!(manager.aborts.load(Ordering::Acquire), 0);
        fs::remove_dir_all(manager.root.clone()).unwrap();
    }
}
