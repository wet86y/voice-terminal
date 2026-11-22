//! Resource commands（资源命令）：manage fixed model and DSH resources without Voice service.
use std::{
    collections::{HashMap, HashSet},
    ffi::OsString,
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, ChildStdout, ExitStatus, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Condvar, Mutex, MutexGuard,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow};
use voice_desktop_common::remote_import_contract::{
    valid_source_id, RemoteImportManifest, RemoteImportSourceTicket,
};
use voice_desktop_common::LaunchContext;

const PROGRESS_EVENT: &str = "desktop://resource-progress";
const MAX_PERSISTED_JOBS: usize = 128;
const REMOTE_IMPORT_TICKET_TTL: Duration = Duration::from_secs(30 * 60); // REMOTE_IMPORT_TICKET_TTL（未提交远程来源有效期）。
const REMOTE_IMPORT_RESULT_TTL: Duration = Duration::from_secs(24 * 60 * 60); // REMOTE_IMPORT_RESULT_TTL（远程来源幂等结果保留时间）。
const REMOTE_IMPORT_MAX_ACTIVE: usize = 32; // REMOTE_IMPORT_MAX_ACTIVE（并行未提交来源上限）。
const REMOTE_IMPORT_MAX_RECORDS: usize = 128; // REMOTE_IMPORT_MAX_RECORDS（来源幂等记录上限）。
const REMOTE_RESET_CONFIRMATION_TTL: Duration = Duration::from_secs(5 * 60); // REMOTE_RESET_CONFIRMATION_TTL（重置计划一次性确认有效期）。
const REMOTE_RESET_MAX_APPROVALS: usize = 64; // REMOTE_RESET_MAX_APPROVALS（待确认重置计划上限）。
static NEXT_JOB: AtomicU64 = AtomicU64::new(1);

struct RemoteImportReservation {
    request_id: String,          // request_id（本次远程来源预留的稳定操作编号）。
    source_id: String,           // source_id（服务端签发的导入来源编号）。
    resource_id: Option<String>, // resource_id（服务端绑定的目标资源编号）。
    staging_dir: PathBuf,        // staging_dir（仅原生进程持有的受管暂存路径）。
    expires_at: Instant,         // expires_at（原生单调时钟到期时刻）。
    retain_until: Instant,       // retain_until（操作幂等记录清理时刻）。
    state: RemoteImportState,    // state（预留、提交、完成或取消状态）。
}

enum RemoteImportState {
    Reserved,                         // Reserved（允许 FILE 通道继续写入的未提交来源）。
    Committing(RemoteImportManifest), // Committing（清单已锁定并正在封存、核验及派发）。
    Committed {
        manifest: RemoteImportManifest, // manifest（已核验并绑定的原始文件清单）。
        result: Value,                  // result（现有导入任务的原始回执）。
        job_id: String,                 // job_id（资源管理器接受的原任务编号）。
        committed_dir: PathBuf,         // committed_dir（封存后仅原生任务可读取的导入目录）。
    },
    Aborted, // Aborted（已取消、过期或失败后清理的来源）。
}

struct RemoteResetApproval {
    resource_id: String, // resource_id（获准重置的固定组件编号）。
    token: String,       // token（组件清理模块生成的一次性重置范围确认值）。
    plan: Value,         // plan（原生生成的删除范围预览）。
    expires_at: Instant, // expires_at（用户确认有效期）。
}

// ready_import（待接续导入）：失败、取消、参考缺失及已派发任务均不自动重试。
fn ready_import(task: &Value, id: &str, known: bool) -> bool {
    !known
        && id.len() == 32
        && id
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        && task["task_id"] == id
        && task["status"] == "ready_to_convert"
}

// ServiceControl（服务控制）：返回是否存在需要恢复的服务，管理页下载不自动启动后台。
pub type ServiceControl = Arc<dyn Fn(bool) -> Result<bool, String> + Send + Sync>;

#[derive(Clone)]
struct JobSnapshot {
    job_id: String,                            // job_id（任务标识）。
    resource_id: String,                       // resource_id（资源标识）。
    state: String,                             // state（任务状态）。
    phase: String,                             // phase（任务阶段说明）。
    progress_kind: String, // progress_kind（进度类型）：下载、校验、安装或等待。
    attempt: u32,          // attempt（资源处理轮次）：首次加两次自动恢复。
    parent_job_id: Option<String>, // parent_job_id（所属一键任务编号）。
    completed_bytes: u64,  // completed_bytes（已处理字节数）。
    total_bytes: Option<u64>, // total_bytes（总字节数）。
    download_completed_bytes: Option<u64>, // download_completed_bytes（资源下载已完成字节）。
    download_total_bytes: Option<u64>, // download_total_bytes（资源下载总字节）。
    verification_completed_bytes: Option<u64>, // verification_completed_bytes（摘要校验已处理字节）。
    verification_total_bytes: Option<u64>,     // verification_total_bytes（摘要校验总字节）。
    failure_resource_id: Option<String>,       // failure_resource_id（本轮实际失败的依赖组件）。
    first_error: Option<String>,               // first_error（本轮首次错误，自动恢复不能覆盖）。
    error: Option<String>,                     // error（安全错误说明）。
    can_cancel: bool,                          // can_cancel（当前是否可取消）。
    installed_resources: Vec<String>,          // installed_resources（本次安装资源编号）。
    import_preview: Option<Value>,             // import_preview（预校验通过的导入识别结果）。
    created_at: u64,                           // created_at（任务创建时间，Unix 毫秒）。
    revision: u64,           // revision（任务状态修订号）：事件与补查快照使用同一顺序。
    recovery: Option<Value>, // recovery（共用恢复状态）：事件类别、处理结果和明确下一步。
}

struct JobControl {
    snapshot: JobSnapshot,
    cancel: Arc<AtomicBool>, // cancel（受控线程取消信号）。
    child: Option<Arc<Mutex<ControlledProcess>>>, // child（仅用于可取消阶段的受控进程）。
    last_event: Instant,     // last_event（最近进度广播时刻）。
    last_persist: Instant,   // last_persist（最近状态持久化时刻）。
}

struct PendingImportSelection {
    resource_id: Option<String>, // resource_id（目标资源编号）：与本次来源选择绑定。
    sources: Vec<PathBuf>,       // sources（本机来源路径）：仅保留在原生进程，不返回网页。
    expires_at: Instant,         // expires_at（票据过期时间）：避免来源选择长期悬挂。
}

struct ControlledProcess {
    child: Child,
    #[cfg(windows)]
    job: voice_desktop_common::windows::Job, // job（进程树终止容器）。
}

impl ControlledProcess {
    fn spawn(command: &mut std::process::Command) -> Result<Self, String> {
        #[cfg(windows)]
        {
            let job = voice_desktop_common::windows::Job::new()?;
            let child = job.spawn(command)?;
            Ok(Self { child, job })
        }
        #[cfg(not(windows))]
        {
            command
                .spawn()
                .map(|child| Self { child })
                .map_err(|error| error.to_string())
        }
    }

    fn kill(&mut self) {
        #[cfg(windows)]
        self.job.terminate();
        let _ = self.child.kill();
    }

    fn wait(&mut self) -> std::io::Result<ExitStatus> {
        self.child.wait()
    }
}

enum JobError {
    Cancelled,
    Failed {
        message: String,
        installed: Vec<String>,
    },
}

fn open_ssh_server_job_error(message: String) -> JobError {
    if message == voice_desktop_common::system_feature::OPENSSH_SERVER_UAC_CANCELLED_ERROR {
        JobError::Cancelled
    } else {
        JobError::Failed {
            message,
            installed: vec![],
        }
    }
}

fn open_ssh_server_wait_phase(elapsed: Duration) -> String {
    format!(
        "等待 Windows 管理员授权或官方功能处理（已等待 {} 秒）",
        elapsed.as_secs()
    )
}

struct ProcessError {
    message: String,
    installed: Vec<String>,
    cancelled: bool,
}

/// ResourceManager（资源管理器）：owns resource jobs and talks only to fixed local tools.
pub struct ResourceManager {
    context: LaunchContext,
    app: AppHandle,
    service_control: ServiceControl,
    jobs: Mutex<HashMap<String, JobControl>>,
    jobs_changed: Condvar, // jobs_changed（资源任务状态变化通知）。
    jobs_path: PathBuf,
    reviews: Mutex<HashMap<String, bool>>, // reviews（用户对导入内容的选择）。
    import_selections: Mutex<HashMap<String, PendingImportSelection>>, // import_selections（短期来源票据）：路径只保存在原生进程。
    remote_imports: Mutex<HashMap<String, RemoteImportReservation>>, // remote_imports（服务端签发的远程导入票据及幂等结果）。
    remote_reset_approvals: Mutex<HashMap<String, RemoteResetApproval>>, // remote_reset_approvals（短期一次性删除范围确认）。
    management_core_status: Mutex<Option<String>>, // management_core_status（最近向远端公开的语音核心状态）。
    management_core_status_revision: AtomicU64, // management_core_status_revision（核心状态变化修订号）。
    activation: Mutex<()>, // activation（仅串行化资源启用；下载、解包与校验不占锁）。
    environment: Mutex<()>, // environment（串行化完整语音环境的复制、组装与导入；模型下载仍可并行）。
    tts_runtime: Mutex<()>, // tts_runtime（串行化两款模型共用原生运行包的下载、补丁及启用）。
    resume_pending: Mutex<bool>, // resume_pending（先前运行服务的延迟恢复意图；全部资源更新完成后才恢复）。
    resetting: AtomicBool,       // resetting（重置独占准入）。
    batch_active: AtomicBool,    // batch_active（一键维护中）：整批结束后才恢复服务。
    accepting_jobs: AtomicBool, // accepting_jobs（接受新任务）：迁移排空前原子关闭，防止排空后再写旧工作区。
    validation_failures: Mutex<HashMap<String, String>>, // validation_failures（本次启动的完整校验失败）：管理页不能以字节数盘点掩盖同长度损坏。
}

impl ResourceManager {
    /// import_dispatcher（导入后台接续）：仅执行用户已确认且已完成暂存的任务，不依赖设置页面。
    pub fn start_import_dispatcher(self: &Arc<Self>) {
        let weak = Arc::downgrade(self);
        thread::spawn(move || {
            loop {
                thread::sleep(Duration::from_secs(2));
                let Some(manager) = weak.upgrade() else {
                    break;
                };
                if !manager.accepting_jobs.load(Ordering::Acquire) {
                    continue;
                }
                let Ok(root) = voice_desktop_common::workspace::safe_path(
                    &manager.context.install,
                    "cache/aniflive-tts/import-jobs",
                ) else {
                    continue;
                };
                let Ok(entries) = fs::read_dir(root) else {
                    continue;
                };
                for entry in entries.flatten() {
                    let path = entry.path();
                    let Some(id) = path.file_stem().and_then(|value| value.to_str()) else {
                        continue;
                    };
                    if id.len() != 32
                        || !id
                            .bytes()
                            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                    {
                        continue;
                    }
                    let Ok(path) = voice_desktop_common::workspace::safe_path(
                        &manager.context.install,
                        &format!("cache/aniflive-tts/import-jobs/{id}.json"),
                    ) else {
                        continue;
                    };
                    let Some(task) = fs::read(path)
                        .ok()
                        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                    else {
                        continue;
                    };
                    // 已创建的任务（包括失败或取消）由用户重试，不能后台循环重建。
                    let known = manager
                        .jobs
                        .lock()
                        .map(|jobs| jobs.contains_key(&format!("aniflive-import-{id}")))
                        .unwrap_or(true);
                    if !ready_import(&task, id, known) {
                        continue;
                    }
                    if let Err(error) = manager.start_aniflive_import(id.to_owned()) {
                        eprintln!("导入后台接续暂未启动：{error}");
                    }
                }
            }
        });
    }
    /// Create a manager（建立管理器）from the resolved product context.
    pub fn new(
        context: LaunchContext,
        app: AppHandle,
        service_control: ServiceControl,
    ) -> Result<Self, String> {
        if let Err(error) =
            voice_desktop_common::resources::disable_unavailable_enhancements(&context)
        {
            eprintln!("可选增强组件状态检查失败：{error}");
        }
        let process_temp =
            voice_desktop_common::workspace::safe_path(&context.install, "tmp/process")?;
        fs::create_dir_all(&process_temp).map_err(|_| "资源临时目录不可用。")?;
        let state_dir = voice_desktop_common::workspace::safe_path(&context.data, "state")?;
        fs::create_dir_all(&state_dir).map_err(|_| "资源任务状态目录不可用。")?;
        let jobs_path =
            voice_desktop_common::workspace::safe_path(&context.data, "state/resource-jobs.json")?;
        let mut jobs = Self::load_jobs(&jobs_path)?;
        if !context.install.join("tmp/native-install.json").exists() {
            if let Some(job) = jobs.get_mut("component-recovery") {
                job.snapshot.state = "complete".into();
                job.snapshot.phase = "上次组件事务问题已收尾".into();
                job.snapshot.recovery = None;
            }
        }
        let manager = Self {
            context,
            app,
            service_control,
            jobs: Mutex::new(jobs),
            jobs_changed: Condvar::new(),
            jobs_path,
            activation: Mutex::new(()),
            environment: Mutex::new(()),
            tts_runtime: Mutex::new(()),
            reviews: Mutex::new(HashMap::new()),
            import_selections: Mutex::new(HashMap::new()),
            remote_imports: Mutex::new(HashMap::new()),
            remote_reset_approvals: Mutex::new(HashMap::new()),
            management_core_status: Mutex::new(None),
            management_core_status_revision: AtomicU64::new(0),
            resume_pending: Mutex::new(false),
            resetting: AtomicBool::new(false),
            batch_active: AtomicBool::new(false),
            accepting_jobs: AtomicBool::new(true),
            validation_failures: Mutex::new(HashMap::new()),
        };
        // 仅清理本功能专用暂存根中带有效管理标记的过期来源；其他目录与用户文件不触碰。
        let _ = manager.cleanup_orphaned_remote_imports();
        for job in manager.snapshots() {
            if !is_system_feature_job(job["job_id"].as_str().unwrap_or_default())
                && job["state"] == "failed"
                && job["recovery"]["state"] != "resolved"
            {
                if let (Some(id), Some(reason)) = (
                    job["failure_resource_id"]
                        .as_str()
                        .or_else(|| job["resource_id"].as_str()),
                    job["error"].as_str(),
                ) {
                    voice_desktop_common::component_reset::fault(&manager.context, id, reason)?;
                }
            }
        }
        manager.persist_jobs()?;
        Ok(manager)
    }

    /// Inventory（资源清单）：normalize the Python resource result for the frontend.
    pub fn inventory(&self) -> Result<Vec<Value>, String> {
        voice_desktop_common::compatibility::inventory_with(&self.context, |rows| {
            if let Some(record) = voice_desktop_common::component_maintenance::load(&self.context)?
            {
                for issue in record["issues"].as_array().into_iter().flatten() {
                    if issue["content_failure"] == true {
                        if let (Some(id), Some(reason)) =
                            (issue["resource_id"].as_str(), issue["reason"].as_str())
                        {
                            voice_desktop_common::component_reset::fault(
                                &self.context,
                                id,
                                reason,
                            )?;
                        }
                    }
                }
            }
            voice_desktop_common::component_reset::apply(&self.context, rows)?;
            let failures = self
                .validation_failures
                .lock()
                .map_err(|_| "组件校验状态不可用。")?;
            for row in rows.iter_mut() {
                if let Some(reason) = row["resource_id"].as_str().and_then(|id| failures.get(id)) {
                    row["can_start"] = json!(false);
                    row["files_installed"] = json!(false);
                    row["installation_state"] = json!("incomplete");
                    row["error"] = json!(reason);
                }
            }
            voice_desktop_common::resources::update_tts_readiness(&self.context, rows)?;
            if rows
                .iter()
                .skip(1)
                .any(|row| row["required"] != false && row["can_start"] != true)
            {
                if let Some(server) = rows.first_mut() {
                    server["can_start"] = json!(false);
                }
            }
            Ok(())
        })
    }
    pub fn record_validation(&self, readiness: &Value) {
        if let Ok(mut failures) = self.validation_failures.lock() {
            failures.clear();
            for row in readiness["missing"].as_array().into_iter().flatten().chain(
                readiness["invalid_resources"]
                    .as_array()
                    .into_iter()
                    .flatten(),
            ) {
                if let (Some(id), Some(reason)) =
                    (row["resource_id"].as_str(), row["reason"].as_str())
                {
                    if reason.contains("完整摘要复核失败") {
                        failures.insert(id.to_owned(), reason.to_owned());
                    }
                }
            }
        }
    }

    pub(crate) fn application_handle(&self) -> AppHandle { self.app.clone() }

    pub fn context(&self) -> LaunchContext {
        self.context.clone()
    }

    /// remote_management_status（独立管理状态）：读取当前桌面宿主的真实核心进程与握手状态。
    pub(crate) fn remote_management_status(&self) -> Value {
        let Some(state) = self.app.try_state::<Arc<super::Desktop>>() else {
            let revision = self.next_management_status_revision("unavailable");
            return json!({
                "core_state":"unavailable",
                "resource_management_ready":true,
                "persistent_management":true,
                "revision":revision,
                "fields": {
                    "core_state":"语音核心的真实状态：ready、starting 或 unavailable",
                    "resource_management_ready":"原生 ResourceManager 是否可响应组件和任务请求",
                    "persistent_management":"连续管理请求逐次核验当前设备信任及连接代次，可复用通道",
                    "revision":"核心服务代次与当前状态编码的修订号",
                }
            });
        };
        let epoch = state.service_epoch.load(Ordering::Acquire);
        let process_alive = state
            .service
            .lock()
            .ok()
            .and_then(|mut service| {
                service
                    .as_mut()
                    .and_then(|current| matches!(current.child.try_wait(), Ok(None)).then_some(()))
            })
            .is_some();
        let current_core = state
            .recovery
            .lock()
            .map(|recovery| recovery.current_core)
            .unwrap_or(None);
        let core_ready = process_alive
            && state.service_ready_epoch.load(Ordering::Acquire) == epoch
            && current_core == Some(true);
        let starting = state.service_starting.load(Ordering::Acquire)
            || process_alive && current_core.is_none();
        let core_state = if core_ready {
            "ready"
        } else if starting {
            "starting"
        } else {
            "unavailable"
        };
        let state_revision = self.next_management_status_revision(core_state);
        json!({
            "core_state":core_state,
            "resource_management_ready":true,
            "persistent_management":true,
            "revision":epoch.saturating_mul(1_u64 << 32).saturating_add(state_revision.min(u64::from(u32::MAX))),
            "fields": {
                "core_state":"语音核心的真实状态：ready、starting 或 unavailable",
                "resource_management_ready":"原生 ResourceManager 已就绪，不依赖 Python 核心",
                "persistent_management":"连续管理请求逐次核验当前设备信任及连接代次，可复用通道",
                "revision":"核心服务代次与当前状态编码的修订号",
            }
        })
    }

    fn next_management_status_revision(&self, core_state: &str) -> u64 {
        let changed = if let Ok(mut previous) = self.management_core_status.lock() {
            let changed = previous.as_deref() != Some(core_state);
            if changed {
                *previous = Some(core_state.to_owned());
            }
            changed
        } else {
            true
        };
        if changed {
            self.management_core_status_revision
                .fetch_add(1, Ordering::AcqRel)
                .saturating_add(1)
        } else {
            self.management_core_status_revision.load(Ordering::Acquire)
        }
    }

    fn remote_import_root(&self) -> Result<PathBuf, String> {
        let root = voice_desktop_common::workspace::safe_path(
            &self.context.install,
            "cache/remote-management/import-staging",
        )?;
        fs::create_dir_all(&root).map_err(|_| "远程导入暂存根不可用。".to_owned())?;
        let verified = voice_desktop_common::workspace::safe_path(
            &self.context.install,
            "cache/remote-management/import-staging",
        )?;
        if verified != root {
            return Err("远程导入暂存根路径在创建期间发生变化。".into());
        }
        let metadata = fs::symlink_metadata(&verified).map_err(|_| "远程导入暂存根不可用。")?;
        if remote_import_is_reparse(&metadata) || !metadata.is_dir() {
            return Err("远程导入暂存根不是受管普通目录。".into());
        }
        Ok(root)
    }

    /// reserve_remote_import_source（签发远程导入暂存来源）：创建独占目录，不接受调用方路径。
    pub fn reserve_remote_import_source(
        &self,
        request_id: &str,
        resource_id: Option<String>,
    ) -> Result<RemoteImportSourceTicket, String> {
        if !voice_desktop_common::device_management_contract::valid_request_id(request_id) {
            return Err("远程导入操作编号无效。".into());
        }
        if resource_id
            .as_deref()
            .is_some_and(|id| !valid_resource_id(id))
        {
            return Err("远程导入目标资源编号无效。".into());
        }
        self.cleanup_remote_imports()?;

        let mut reservations = self
            .remote_imports
            .lock()
            .map_err(|_| "远程导入状态不可用。")?;
        if let Some(existing) = reservations.get(request_id) {
            if existing.resource_id != resource_id {
                return Err("同一远程导入操作编号不能更改目标资源。".into());
            }
            if matches!(&existing.state, RemoteImportState::Aborted)
                || existing.retain_until <= Instant::now()
            {
                return Err("该远程导入来源已取消或过期；请使用新的操作编号重新申请。".into());
            }
            if existing.expires_at <= Instant::now()
                && matches!(
                    &existing.state,
                    RemoteImportState::Reserved | RemoteImportState::Committing(_)
                )
            {
                return Err("该远程导入来源已过期；请使用新的操作编号重新申请。".into());
            }
            return Ok(RemoteImportSourceTicket {
                source_id: existing.source_id.clone(),
                resource_id: existing.resource_id.clone(),
                staging_dir: existing.staging_dir.clone(),
            });
        }
        let active = reservations
            .values()
            .filter(|reservation| {
                reservation.expires_at > Instant::now()
                    && matches!(
                        &reservation.state,
                        RemoteImportState::Reserved | RemoteImportState::Committing(_)
                    )
            })
            .count();
        if active >= REMOTE_IMPORT_MAX_ACTIVE {
            return Err("远程导入暂存容量已满，请提交或取消现有来源后重试。".into());
        }
        if reservations.len() >= REMOTE_IMPORT_MAX_RECORDS {
            return Err("远程导入幂等记录容量已满，请稍后重试。".into());
        }

        let root = self.remote_import_root()?;
        let (source_id, staging_dir) = create_remote_import_stage(&root)?;
        let now = Instant::now();
        let expires_at = now + REMOTE_IMPORT_TICKET_TTL;
        let expires_at_ms =
            unix_millis().saturating_add(REMOTE_IMPORT_TICKET_TTL.as_millis() as u64);
        let retain_until = now + REMOTE_IMPORT_RESULT_TTL;
        if let Err(error) = write_remote_import_owner_marker(&root, &source_id, expires_at_ms) {
            let _ = remove_remote_import_tree(&root, &source_id);
            let _ = remove_remote_import_marker(&root, &source_id);
            return Err(error);
        }
        reservations.insert(
            request_id.to_owned(),
            RemoteImportReservation {
                request_id: request_id.to_owned(),
                source_id: source_id.clone(),
                resource_id: resource_id.clone(),
                staging_dir: staging_dir.clone(),
                expires_at,
                retain_until,
                state: RemoteImportState::Reserved,
            },
        );
        Ok(RemoteImportSourceTicket {
            source_id,
            resource_id,
            staging_dir,
        })
    }

    /// commit_remote_import_source（核验并接入现有导入事务）：服务端逐文件复核后才启动原导入任务。
    pub fn commit_remote_import_source(
        self: &Arc<Self>,
        ticket: &RemoteImportSourceTicket,
        manifest: RemoteImportManifest,
    ) -> Result<Value, String> {
        manifest.validate()?;
        if manifest.source_id != ticket.source_id {
            return Err("远程导入清单来源编号与服务端票据不匹配。".into());
        }
        let source_id = ticket.source_id.clone();
        let (request_id, resource_id, original_dir) = {
            let mut reservations = self
                .remote_imports
                .lock()
                .map_err(|_| "远程导入状态不可用。")?;
            let reservation = reservations
                .values_mut()
                .find(|row| row.source_id == source_id)
                .ok_or_else(|| "远程导入暂存票据不存在或已过期。".to_owned())?;
            if reservation.resource_id != ticket.resource_id
                || reservation.staging_dir != ticket.staging_dir
            {
                return Err("远程导入票据与服务端登记内容不匹配。".into());
            }
            match &reservation.state {
                RemoteImportState::Committed {
                    manifest: accepted,
                    result,
                    ..
                } => {
                    if accepted == &manifest {
                        return Ok(result.clone());
                    }
                    return Err("同一远程导入来源不能提交不同的清单。".into());
                }
                RemoteImportState::Committing(current) => {
                    if current == &manifest {
                        return Err("相同远程导入事务仍在提交，请查询原任务。".into());
                    }
                    return Err("同一远程导入来源不能并行提交不同的清单。".into());
                }
                RemoteImportState::Aborted => return Err("远程导入暂存票据已取消。".into()),
                RemoteImportState::Reserved => {}
            }
            if reservation.expires_at <= Instant::now() {
                return Err("远程导入暂存票据已过期。".into());
            }
            reservation.state = RemoteImportState::Committing(manifest.clone());
            (
                reservation.request_id.clone(),
                reservation.resource_id.clone(),
                reservation.staging_dir.clone(),
            )
        };

        let root = match self.remote_import_root() {
            Ok(root) => root,
            Err(error) => {
                self.restore_remote_import_reservation(&request_id, &manifest);
                return Err(error);
            }
        };
        let committed_dir = match seal_remote_import_stage(&root, &source_id, &original_dir) {
            Ok(path) => path,
            Err(error) => {
                self.restore_remote_import_reservation(&request_id, &manifest);
                return Err(error);
            }
        };
        if let Err(error) = verify_remote_import_tree(&committed_dir, &manifest) {
            if fs::rename(&committed_dir, &original_dir).is_ok() {
                self.restore_remote_import_reservation(&request_id, &manifest);
            } else {
                if let Ok(mut reservations) = self.remote_imports.lock() {
                    if let Some(reservation) = reservations.get_mut(&request_id) {
                        reservation.state = RemoteImportState::Aborted;
                        reservation.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
                    }
                }
                let _ = remove_remote_import_path(&root, &committed_dir);
                let _ = remove_remote_import_marker(&root, &source_id);
            }
            return Err(error);
        }
        let result = match self.start_import_job(resource_id.clone(), vec![committed_dir.clone()]) {
            Ok(result) => result,
            Err(error) => {
                let restored = fs::rename(&committed_dir, &original_dir).is_ok();
                if restored {
                    self.restore_remote_import_reservation(&request_id, &manifest);
                } else {
                    if let Ok(mut reservations) = self.remote_imports.lock() {
                        if let Some(reservation) = reservations.get_mut(&request_id) {
                            reservation.state = RemoteImportState::Aborted;
                            reservation.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
                        }
                    }
                    let _ = remove_remote_import_path(&root, &committed_dir);
                    let _ = remove_remote_import_marker(&root, &source_id);
                }
                return Err(error);
            }
        };
        let job_id = result["job_id"]
            .as_str()
            .ok_or("导入任务已启动但未返回任务编号。")?
            .to_owned();
        {
            let mut reservations = self
                .remote_imports
                .lock()
                .map_err(|_| "远程导入状态不可用。")?;
            let reservation = reservations
                .get_mut(&request_id)
                .ok_or("远程导入任务已启动，但幂等记录不可用。")?;
            reservation.state = RemoteImportState::Committed {
                manifest,
                result: result.clone(),
                job_id: job_id.clone(),
                committed_dir,
            };
            reservation.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
        }
        if self.snapshots().iter().any(|row| {
            row["job_id"] == job_id && terminal(row["state"].as_str().unwrap_or_default())
        }) {
            self.cleanup_remote_import_job(&job_id);
        }
        Ok(result)
    }

    /// abort_remote_import_source（取消未提交远程导入来源）：仅移除服务端签发的独占暂存目录。
    pub fn abort_remote_import_source(
        &self,
        ticket: &RemoteImportSourceTicket,
    ) -> Result<(), String> {
        if !valid_source_id(&ticket.source_id) {
            return Err("远程导入暂存来源编号无效。".into());
        }
        let root = self.remote_import_root()?;
        let request_id = {
            let mut reservations = self
                .remote_imports
                .lock()
                .map_err(|_| "远程导入状态不可用。")?;
            let Some((request_id, reservation)) = reservations
                .iter_mut()
                .find(|(_, row)| row.source_id == ticket.source_id)
            else {
                return Ok(());
            };
            if reservation.resource_id != ticket.resource_id
                || reservation.staging_dir != ticket.staging_dir
            {
                return Err("远程导入票据与服务端登记内容不匹配。".into());
            }
            if matches!(&reservation.state, RemoteImportState::Committed { .. }) {
                return Err("导入任务已经接受；请使用原任务编号查询或取消。".into());
            }
            if matches!(&reservation.state, RemoteImportState::Committing(_)) {
                return Err("导入事务正在提交，不能取消暂存来源。".into());
            }
            if matches!(&reservation.state, RemoteImportState::Aborted) {
                return Ok(());
            }
            reservation.state = RemoteImportState::Aborted;
            reservation.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
            request_id.clone()
        };
        self.abort_remote_import_record(&request_id, &root);
        Ok(())
    }

    fn restore_remote_import_reservation(&self, request_id: &str, manifest: &RemoteImportManifest) {
        if let Ok(mut reservations) = self.remote_imports.lock() {
            if let Some(reservation) = reservations.get_mut(request_id) {
                if matches!(&reservation.state, RemoteImportState::Committing(current) if current == manifest)
                {
                    reservation.state = RemoteImportState::Reserved;
                }
            }
        }
    }

    fn abort_remote_import_record(&self, request_id: &str, root: &Path) {
        let target = self
            .remote_imports
            .lock()
            .ok()
            .and_then(|mut reservations| {
                let reservation = reservations.get_mut(request_id)?;
                reservation.state = RemoteImportState::Aborted;
                reservation.retain_until = Instant::now() + REMOTE_IMPORT_RESULT_TTL;
                Some((
                    reservation.source_id.clone(),
                    reservation.staging_dir.clone(),
                ))
            });
        if let Some((source_id, staging_dir)) = target {
            let _ = remove_remote_import_path(root, &staging_dir);
            let _ = remove_remote_import_marker(root, &source_id);
        }
    }

    pub(crate) fn cleanup_remote_imports(&self) -> Result<(), String> {
        let now = Instant::now();
        let terminal_jobs = self
            .snapshots()
            .into_iter()
            .filter_map(|row| {
                let job_id = row["job_id"].as_str()?.to_owned();
                terminal(row["state"].as_str().unwrap_or_default()).then_some(job_id)
            })
            .collect::<HashSet<_>>();
        let mut remove_paths = Vec::new();
        let mut remove_records = Vec::new();
        if let Ok(mut reservations) = self.remote_imports.lock() {
            for (request_id, reservation) in reservations.iter_mut() {
                if matches!(&reservation.state, RemoteImportState::Reserved)
                    && reservation.expires_at <= now
                {
                    reservation.state = RemoteImportState::Aborted;
                    reservation.retain_until = now + REMOTE_IMPORT_RESULT_TTL;
                    remove_paths.push((
                        reservation.source_id.clone(),
                        reservation.staging_dir.clone(),
                    ));
                    continue;
                }
                let committed_cleanup = match &reservation.state {
                    RemoteImportState::Committed {
                        job_id,
                        committed_dir,
                        ..
                    } if reservation.retain_until <= now && terminal_jobs.contains(job_id) => {
                        Some(committed_dir.clone())
                    }
                    _ => None,
                };
                if let Some(path) = committed_cleanup {
                    remove_paths.push((reservation.source_id.clone(), path));
                    remove_records.push(request_id.clone());
                    continue;
                }
                let committed_active = match &reservation.state {
                    RemoteImportState::Committed { job_id, .. }
                        if reservation.retain_until <= now && !terminal_jobs.contains(job_id) =>
                    {
                        true
                    }
                    _ => false,
                };
                if committed_active {
                    reservation.retain_until = now + REMOTE_IMPORT_RESULT_TTL;
                } else if matches!(&reservation.state, RemoteImportState::Committing(_))
                    && reservation.retain_until <= now
                {
                    reservation.retain_until = now + REMOTE_IMPORT_RESULT_TTL;
                } else if reservation.retain_until <= now {
                    remove_records.push(request_id.clone());
                }
            }
            for request_id in &remove_records {
                reservations.remove(request_id);
            }
        }
        if let Ok(root) = self.remote_import_root() {
            for (source_id, path) in remove_paths {
                let _ = remove_remote_import_path(&root, &path);
                let _ = remove_remote_import_marker(&root, &source_id);
            }
        }
        Ok(())
    }

    fn cleanup_remote_import_job(&self, job_id: &str) {
        let cleanup = self.remote_imports.lock().ok().and_then(|reservations| {
            reservations
                .values()
                .find_map(|reservation| match &reservation.state {
                    RemoteImportState::Committed {
                        job_id: committed_job,
                        committed_dir,
                        ..
                    } if committed_job == job_id => {
                        Some((reservation.source_id.clone(), committed_dir.clone()))
                    }
                    _ => None,
                })
        });
        if let Some((source_id, committed_dir)) = cleanup {
            if let Ok(root) = self.remote_import_root() {
                let _ = remove_remote_import_path(&root, &committed_dir);
                let _ = remove_remote_import_marker(&root, &source_id);
            }
        }
    }

    fn cleanup_orphaned_remote_imports(&self) -> Result<(), String> {
        let root = voice_desktop_common::workspace::safe_path(
            &self.context.install,
            "cache/remote-management/import-staging",
        )?;
        let root_metadata = match fs::symlink_metadata(&root) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err("远程导入暂存根不可读取。".into()),
        };
        if remote_import_is_reparse(&root_metadata) || !root_metadata.is_dir() {
            return Err("远程导入暂存根不是受管普通目录。".into());
        }
        let now_ms = unix_millis();
        for entry in fs::read_dir(&root).map_err(|_| "远程导入暂存根不可读取。")? {
            let entry = entry.map_err(|_| "远程导入暂存目录项不可读取。")?;
            let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            if let Some(source_id) = name
                .strip_prefix('.')
                .and_then(|value| value.strip_suffix(".owner.json"))
                .filter(|value| valid_source_id(value))
            {
                if read_remote_import_owner_marker(&root, source_id)
                    .is_ok_and(|expires_at| now_ms >= expires_at)
                {
                    let _ = remove_remote_import_tree(&root, source_id);
                    let _ = remove_remote_import_path(
                        &root,
                        &root.join(format!("accepted-{source_id}")),
                    );
                    let _ = remove_remote_import_marker(&root, source_id);
                }
                continue;
            }
            let source_id = if valid_source_id(&name) {
                name.as_str()
            } else if let Some(source_id) = name
                .strip_prefix("accepted-")
                .filter(|id| valid_source_id(id))
            {
                source_id
            } else {
                continue;
            };
            let metadata = match fs::symlink_metadata(entry.path()) {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(_) => return Err("远程导入暂存项不可读取。".into()),
            };
            if remote_import_is_reparse(&metadata) || !metadata.is_dir() {
                continue;
            }
            let marker = read_remote_import_owner_marker(&root, source_id).ok();
            if marker.is_some_and(|expires_at| now_ms >= expires_at) {
                let _ = remove_remote_import_path(&root, &entry.path());
                let _ = remove_remote_import_marker(&root, source_id);
            }
        }
        Ok(())
    }

    fn require_local_runtime(&self) -> Result<(), String> {
        let snapshot = voice_desktop_common::device_config::inspect(&self.context.data);
        voice_desktop_common::system_feature::require_local_runtime(&snapshot)
    }

    /// open_ssh_server_status（读取 OpenSSH Server 状态）：只读且仅限本地运行设备。
    pub fn open_ssh_server_status(
        &self,
        refresh: bool, // refresh（显式检查按钮强制读取，其他请求合并短时快照）。
    ) -> Result<voice_desktop_common::system_feature::OpenSshServerStatus, String> {
        self.require_local_runtime()?;
        voice_desktop_common::system_feature::cached_open_ssh_server_runtime_status(refresh)
    }

    /// enable_open_ssh_server（启动受管主机功能任务）：使用固定任务编号，不进入普通文件下载器。
    pub fn enable_open_ssh_server(self: &Arc<Self>) -> Result<Value, String> {
        self.require_local_runtime()?;
        let job_id = voice_desktop_common::system_feature::OPENSSH_SERVER_JOB_ID.to_owned();
        self.start_job(
            job_id.clone(),
            voice_desktop_common::system_feature::OPENSSH_SERVER_ID.to_owned(),
            "准备 OpenSSH Server 系统功能管理".into(),
            false,
            move |manager, id| {
                manager
                    .require_local_runtime()
                    .map_err(|message| JobError::Failed { message, installed: vec![] })?;
                manager.set_phase(&id, "核对系统状态并等待 Windows 管理员授权", false, 0, None);
                manager
                    .require_local_runtime()
                    .map_err(|message| JobError::Failed { message, installed: vec![] })?;
                let after = voice_desktop_common::system_feature::enable_open_ssh_server_with_progress(
                    |elapsed| {
                        manager.set_phase(&id, &open_ssh_server_wait_phase(elapsed), false, 0, None);
                    },
                )
                .map_err(open_ssh_server_job_error)?;
                manager.set_phase(&id, "复核 OpenSSH Server 与 sshd 状态", false, 0, None);
                manager
                    .require_local_runtime()
                    .map_err(|message| JobError::Failed { message, installed: vec![] })?;
                if !after.ready {
                    return Err(JobError::Failed {
                        message: "管理任务结束后没有确认 sshd 正在运行；请刷新状态并检查 Windows 功能源及策略。".into(),
                        installed: vec![],
                    });
                }
                Ok(vec![])
            },
        )?;
        Ok(json!({
            "job_id": job_id,
            "fields": {"job_id": "OpenSSH Server 受管系统功能任务编号"}
        }))
    }

    /// set_open_ssh_project_access（设置项目专用入站访问）：只操作 Voice Terminal 命名的防火墙规则。
    pub fn set_open_ssh_project_access(
        &self,
        enabled: bool,
    ) -> Result<voice_desktop_common::system_feature::OpenSshServerStatus, String> {
        self.require_local_runtime()?;
        voice_desktop_common::system_feature::set_open_ssh_project_access(enabled)
    }

    pub(crate) fn reset_resource(
        self: &Arc<Self>,
        resource_id: String,
        token: String,
    ) -> Result<Value, String> {
        let uninstall = voice_desktop_common::component_reset::pending(&self.context)?.is_some_and(|value|value["uninstall"] == true && value["resource_id"] == resource_id);
        self.remove_resource(resource_id, token, uninstall)
    }
    pub(crate) fn uninstall_resource(self: &Arc<Self>, resource_id: String, token: String) -> Result<Value, String> {
        if resource_id == voice_desktop_common::runtime::GPU_RESOURCE {
            let plan = voice_desktop_common::component_reset::uninstall_plan(&self.context, &resource_id)?;
            if plan["token"] != token { return Err("卸载范围已变化，请重新扫描。".into()); }
            return self.configure_runtime_child("remove-gpu".into(), None);
        }
        self.remove_resource(resource_id, token, true)
    }
    fn remove_resource(self: &Arc<Self>, resource_id: String, token: String, uninstall: bool) -> Result<Value, String> {
        let plan = if uninstall { voice_desktop_common::component_reset::uninstall_plan(&self.context, &resource_id)? }
            else { voice_desktop_common::component_reset::plan(&self.context, &resource_id)? };
        if plan["token"] != token
            && voice_desktop_common::component_reset::pending(&self.context)?.is_none()
        {
            return Err("重置范围已变化，请重新确认。".into());
        }
        {
            let jobs = self.jobs.lock().map_err(|_| "任务状态不可用")?;
            if jobs.values().any(|j| !terminal(&j.snapshot.state))
                || self.resetting.swap(true, Ordering::AcqRel)
            {
                return Err("请等待当前组件任务完成。".into());
            }
        }
        let job_id = if uninstall { format!("reset-uninstall-{resource_id}") } else { format!("reset-{resource_id}") };
        let result=self.start_job(job_id.clone(),resource_id.clone(),if uninstall {"准备卸载组件"} else {"准备重置组件"}.into(),false,move|manager,id|{
            let result=(||{
                let mut lease=manager.pause_service()?;
                manager.remember_service_was_running(lease.was_running())?;
                lease.keep_stopped();
                let mut progress=|phase: &str,done,total|manager.set_phase(&id,phase,false,done,if total==0{None}else{Some(total)});
                let removed=if uninstall { voice_desktop_common::component_reset::uninstall(&manager.context,&resource_id,&token,&mut progress)? }
                    else { voice_desktop_common::component_reset::execute(&manager.context,&resource_id,&token,&mut progress)? };
                if let Ok(mut failures)=manager.validation_failures.lock(){for resource in &removed{failures.remove(resource);}}
                let old=manager.snapshots();for job in old{if job["job_id"]!=id&&removed.iter().any(|r|job["resource_id"]==*r){if let Some(key)=job["job_id"].as_str(){manager.update_job(key,|row|row.recovery=Some(json!({"event":"reset","state":"resolved","next_action":"none","message":"组件已重置，等待下载或导入。"})));}}}
                if removed.len()==1{manager.maybe_resume_service(&mut lease)?;}
                Ok(vec![])
            })();
            manager.resetting.store(false,Ordering::Release);
            result.map_err(|message|JobError::Failed{message,installed:vec![]})
        });
        if result.is_err() {
            self.resetting.store(false, Ordering::Release);
        }
        result?;
        Ok(json!({"job_id":job_id}))
    }

    pub fn record_recovery_failure(&self, reason: &str) -> Result<(), String> {
        use voice_desktop_common::component_recovery::{self, Event};
        if let Err(error) =
            voice_desktop_common::component_reset::transaction_fault(&self.context, reason)
        {
            eprintln!("组件事务重置资格未能保存：{error}");
        }
        let event = component_recovery::classify(reason);
        let routine = matches!(event, Event::FileBusy | Event::Space | Event::Permission);
        let mut recovery = component_recovery::guidance(event, false);
        if routine {
            recovery["next_action"] = json!("restart");
            recovery["message"] = json!(format!(
                "{} 处理后请重新打开程序，启动时将自动恢复本次事务。",
                recovery["message"].as_str().unwrap_or("请解决当前问题。")
            ));
        }
        let snapshot = JobSnapshot {
            job_id: "component-recovery".into(),
            resource_id: "maintenance".into(),
            state: "failed".into(),
            phase: if routine {
                "组件事务等待占用或环境问题解除"
            } else if event == Event::Unknown {
                "组件事务恢复失败，可重试或重置已确认组件"
            } else {
                "组件事务需要全面完整性校验"
            }
            .into(),
            progress_kind: "waiting".into(),
            attempt: 1,
            parent_job_id: None,
            completed_bytes: 0,
            total_bytes: None,
            download_completed_bytes: None,
            download_total_bytes: None,
            verification_completed_bytes: None,
            verification_total_bytes: None,
            failure_resource_id: None,
            first_error: Some(reason.into()),
            error: Some(reason.into()),
            can_cancel: false,
            installed_resources: vec![],
            import_preview: None,
            created_at: unix_millis(),
            revision: 1,
            recovery: Some(recovery),
        };
        self.jobs.lock().map_err(|_| "资源任务状态不可用")?.insert(
            snapshot.job_id.clone(),
            JobControl {
                snapshot,
                cancel: Arc::new(AtomicBool::new(false)),
                child: None,
                last_event: Instant::now(),
                last_persist: Instant::now(),
            },
        );
        self.persist_jobs()
    }
    /// resume_maintenance（承接主动完整性维修）：管理页挂载后复用既有资源任务。
    pub fn resume_maintenance(self: &Arc<Self>) -> Result<(), String> {
        if let Some(pending) = voice_desktop_common::component_reset::pending(&self.context)? {
            return self
                .reset_resource(
                    pending["resource_id"]
                        .as_str()
                        .ok_or("重置记录缺少组件编号")?
                        .into(),
                    pending["token"].as_str().unwrap_or("").into(),
                )
                .map(|_| ());
        }
        let targets = voice_desktop_common::component_maintenance::pending_requests(&self.context)?;
        if targets.is_empty() {
            return Ok(());
        }
        self.start_job(
            "repair-components".into(),
            "required-batch".into(),
            "修复完整性校验发现的组件问题".into(),
            true,
            move |manager, parent| {
                let result = (|| {
                    let mut installed = vec![];
                    let mut remaining = targets;
                    if remaining.iter().any(|request| {
                        request["resource_id"] == voice_desktop_common::runtime::CPU_RESOURCE
                    }) {
                        let gpu = remaining.iter().any(|request| {
                            request["resource_id"] == voice_desktop_common::runtime::CPU_RESOURCE
                                && request["runtime_profile"] == "cu128"
                        });
                        remaining.retain(|request| {
                            request["resource_id"] != voice_desktop_common::runtime::CPU_RESOURCE
                        });
                        let job = if gpu {
                            manager.configure_runtime_child("gpu".into(), Some(parent.clone()))
                        } else {
                            manager.download_child(
                                voice_desktop_common::runtime::CPU_RESOURCE.into(),
                                Some(parent.clone()),
                            )
                        }
                        .map_err(|message| JobError::Failed {
                            message,
                            installed: vec![],
                        })?;
                        let dispatch_result =
                            voice_desktop_common::component_maintenance::dispatched(
                                &manager.context,
                                voice_desktop_common::runtime::CPU_RESOURCE,
                            );
                        manager.wait_required(
                            &parent,
                            &[job["job_id"].as_str().unwrap().to_owned()],
                            &mut installed,
                        )?;
                        dispatch_result.map_err(|message| JobError::Failed {
                            message,
                            installed: installed.clone(),
                        })?;
                    }
                    let mut children = vec![];
                    let mut errors = vec![];
                    for request in remaining {
                        if manager.cancelled(&parent) {
                            break;
                        }
                        let id = request["resource_id"].as_str().unwrap().to_owned();
                        let job = if request["offline"] == true {
                            manager.migrate_child(id.clone(), parent.clone())
                        } else {
                            manager.download_child(id.clone(), Some(parent.clone()))
                        };
                        match job {
                            Ok(job) => {
                                children.push(job["job_id"].as_str().unwrap().to_owned());
                                if let Err(error) =
                                    voice_desktop_common::component_maintenance::dispatched(
                                        &manager.context,
                                        &id,
                                    )
                                {
                                    errors.push(error);
                                }
                            }
                            Err(error) => errors.push(format!("{id}（组件）：{error}")),
                        }
                    }
                    manager.wait_required(&parent, &children, &mut installed)?;
                    if errors.is_empty() {
                        Ok(installed)
                    } else {
                        Err(JobError::Failed {
                            message: errors.join("；"),
                            installed,
                        })
                    }
                })();
                manager.batch_active.store(false, Ordering::Release);
                if manager.pending_service_resume().unwrap_or(false) {
                    let mut lease =
                        manager
                            .pause_service()
                            .map_err(|message| JobError::Failed {
                                message,
                                installed: vec![],
                            })?;
                    match manager.maybe_resume_service(&mut lease) {
                        Ok(resumed) => {
                            clear_resume_intent_if_resumed(&manager.resume_pending, resumed)
                        }
                        Err(message) => {
                            return Err(JobError::Failed {
                                message,
                                installed: vec![],
                            });
                        }
                    }
                }
                result
            },
        )
    }
    fn migrate_child(
        self: &Arc<Self>,
        resource_id: String,
        parent: String,
    ) -> Result<Value, String> {
        let job_id = format!("migration-{resource_id}");
        self.start_child_job(
            job_id.clone(),
            resource_id.clone(),
            "准备随包离线兼容迁移".into(),
            true,
            Some(parent),
            move |manager, id| {
                let cancel = manager.cancel_signal(&id)?;
                let mut progress = |phase: &str, done: u64, total: u64| {
                    if phase.starts_with("正在启用") {
                        let _ = manager.begin_non_cancellable_phase(&id, phase, done, Some(total));
                    } else {
                        manager.set_phase(&id, phase, true, done, Some(total));
                    }
                };
                let _shared = if resource_id == "tts-audiocpp-runtime" {
                    Some(
                        acquire_environment_lock(&manager.tts_runtime, &cancel, &mut progress)
                            .map_err(|message| JobError::Failed {
                                message,
                                installed: vec![],
                            })?,
                    )
                } else {
                    None
                };
                manager.retry_download(&id, || {
                    voice_desktop_common::resources::upgrade_bundled_with_activation(
                        &manager.context,
                        &resource_id,
                        cancel.clone(),
                        &mut progress,
                        &mut |stage, relative| manager.activate_resource(&id, stage, relative),
                    )
                    .map_err(|message| JobError::Failed {
                        message,
                        installed: vec![],
                    })
                })
            },
        )?;
        Ok(json!({"job_id":job_id}))
    }
    pub fn probe_gpu(self: &Arc<Self>) -> Result<Value, String> {
        let job_id = "gpu-check".to_string();
        self.start_job(job_id.clone(),"gpu-environment".into(),"检测登记显卡环境".into(),true,move|manager,id|{
            let context=&manager.context;
            let path=context.data.join("config/gpu-environment.json");
            let mut registration=voice_desktop_common::resources::load(&path).unwrap_or(Value::Null);
            let state=voice_desktop_common::runtime::status(context).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let managed=state["components"].as_array().is_some_and(|rows|rows.iter().any(|row|row["resource_id"]==voice_desktop_common::runtime::GPU_RESOURCE&&row["can_start"]==true));
            if managed&&registration["path"]!="runtimes/python"{registration=Value::Null;}
            // 显卡卡片安装后可直接检测现用环境，无需用户再手动登记第二份解释器。
            if registration.is_null(){
                if !managed{
                    return Err(JobError::Failed{message:"请先安装 GPU（图形处理器）运行依赖。".into(),installed:vec![]});
                }
                registration=json!({"schema":1,"path":"runtimes/python","models":{},"fields":{"schema":"显卡登记格式","path":"现用工作区解释器目录","models":"逐模型实际推理检测结果","environment_fingerprint":"运行环境文件指纹","models_fingerprint":"共享模型文件指纹","gpu_hardware":"显卡硬件信息"}});
            }
            let relative=registration["path"].as_str().unwrap_or("");
            if !["runtimes/python-gpu","runtimes/python"].contains(&relative){return Err(JobError::Failed{message:"登记环境路径无效".into(),installed:vec![]});}
            let python=voice_desktop_common::workspace::safe_path(&context.install,&format!("{relative}/python.exe")).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let lease=manager.pause_service().map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let cancel=manager.cancel_signal(&id)?;let mut results=serde_json::Map::new();
            let identity=voice_desktop_common::runtime::worker(context,&python,"interpreter",&[],&cancel).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            if identity["official"]!=true{return Err(JobError::Failed{message:"显卡解释器需要可移动的 CPython（官方实现）3.12.10、Windows x64（64 位）。".into(),installed:vec![]});}
            let cpu=voice_desktop_common::resources::inventory(context).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let lock=voice_desktop_common::runtime::lock(context).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            for (index,(model,groups)) in [("paraformer",vec!["voice-core","torch-cpu"]),("voice_vad",vec!["voice-core","torch-cpu","onnx-cpu","sherpa-cpu"]),
                ("emotion",vec!["voice-core","torch-cpu","onnx-cpu"]),("smart_turn",vec!["voice-core","torch-cpu","onnx-cpu"]),
                ("campplus",vec!["voice-core","torch-cpu"]),("kws",vec!["voice-core","torch-cpu","onnx-cpu","sherpa-cpu"])].into_iter().enumerate(){
                if cancel.load(Ordering::Acquire){return Err(JobError::Cancelled);}
                manager.set_phase(&id,&format!("实际推理检测：{model}（模型）"),true,index as u64,Some(6));
                let names:std::collections::BTreeSet<String>=groups.iter().flat_map(|group|lock["groups"][group].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned)).collect();
                let args=vec!["--names".into(),serde_json::to_string(&names).unwrap(),"--model".into(),model.into(),"--program".into(),context.program.to_string_lossy().into_owned(),"--install-root".into(),context.install.to_string_lossy().into_owned()];
                let resource=match model {"paraformer"=>"paraformer-streaming","voice_vad"=>"fsmn-vad","emotion"=>"emotion2vec-plus-base","smart_turn"=>"smart-turn", "campplus"=>"campplus-zh-cn-common",_=>"kws-sherpa-onnx"};
                let result=(||{
                    let spec=voice_desktop_common::resources::spec(context,resource)?;
                    let model_root=voice_desktop_common::workspace::safe_path(&context.install,&format!("models/{}",spec["destination"].as_str().ok_or("模型目录缺失")?))?;
                    voice_desktop_common::resources::model_validate(context,&spec,&model_root)?;
                    voice_desktop_common::runtime::worker(context,&python,"gpu",&args,&cancel)
                })();
                let mut capability=result.unwrap_or_else(|reason|json!({"gpu_available":false,"reason":reason}));
                capability["cpu_available"]=json!(cpu.iter().any(|item|item["resource_id"]==resource&&item["can_start"]==true));
                capability["environment"]=json!(python.parent().unwrap().strip_prefix(&context.install).unwrap().to_string_lossy().replace('\\',"/"));
                capability["interpreter_version"]=identity["version"].clone();
                if capability["gpu_available"]==true && capability["hardware"]["gpu"].is_object(){registration["gpu_hardware"]=capability["hardware"]["gpu"].clone();}
                capability.as_object_mut().map(|values|{values.remove("hardware");});
                results.insert(model.into(),capability);
            }
            let environment_fingerprint=voice_desktop_common::runtime::fingerprint(python.parent().unwrap()).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let models_fingerprint=voice_desktop_common::runtime::fingerprint(&context.install.join("models")).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            for capability in results.values_mut(){
                if capability["gpu_available"]==true && (capability["environment_fingerprint"]!=environment_fingerprint||capability["models_fingerprint"]!=models_fingerprint){
                    capability["gpu_available"]=json!(false);capability["reason"]=json!("检测期间环境或模型已变化，请重新检测。");
                }
            }
            registration["models"]=json!(results);
            registration["environment_fingerprint"]=json!(environment_fingerprint);
            registration["models_fingerprint"]=json!(models_fingerprint);
            registration["validation_identity"]=voice_desktop_common::runtime::validation_identity(&context.install,python.parent().unwrap()).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            registration["fields"]["validation_identity"]=json!("解释器及受管清单的轻量身份，日常状态不遍历目录");
            voice_desktop_common::workspace::atomic_json(&path,&registration).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            lease.finish().map_err(|message|JobError::Failed{message,installed:vec!["gpu-environment".into()]})?;
            Ok(vec!["gpu-environment".into()])
        })?;
        Ok(json!({"job_id":job_id}))
    }
    pub fn snapshots(&self) -> Vec<Value> {
        let mut rows = self
            .jobs
            .lock()
            .unwrap()
            .values()
            .map(|job| event_value(&job.snapshot))
            .collect::<Vec<_>>();
        rows.sort_by_key(|row| row["created_at"].as_u64().unwrap_or(0));
        rows
    }

    /// remote_install_plan（读取固定组件安装计划）：复用原生清单与依赖计划，不启动任务。
    pub(crate) fn remote_install_plan(&self, resource_id: &str) -> Result<Value, String> {
        let context = &self.context;
        let mut plan = if matches!(
            resource_id,
            "voice-server"
                | voice_desktop_common::runtime::CPU_RESOURCE
                | voice_desktop_common::runtime::GPU_RESOURCE
        ) {
            let group = if resource_id == voice_desktop_common::runtime::GPU_RESOURCE {
                "gpu"
            } else {
                "cpu"
            };
            json!({
                "resource_id":resource_id,
                "title":if group == "gpu" { "GPU（图形处理器）运行依赖" } else if resource_id == "voice-server" { "Voice Server（语音服务）运行环境" } else { "CPU（中央处理器）运行依赖" },
                "model_bytes":0,
                "environment":voice_desktop_common::runtime::plan(context, group)?,
                "fields":{"resource_id":"固定资源编号","title":"资源名称","model_bytes":"模型文件下载字节数","environment":"完整语音运行环境安装计划"}
            })
        } else {
            if !valid_resource_id(resource_id) {
                return Err("资源编号格式无效。".into());
            }
            let spec = voice_desktop_common::resources::spec(context, resource_id)?;
            let model_bytes = voice_desktop_common::resources::download_bytes(&spec);
            let mut shared_resources = Vec::new();
            if let Some(shared) = spec["runtime_resource"]
                .as_str()
                .filter(|_| spec["preparation"] != "aniflive_local_build")
            {
                let ready = voice_desktop_common::resources::inventory(context)?
                    .iter()
                    .any(|row| row["resource_id"] == shared && row["can_start"] == true);
                if !ready {
                    let shared_spec = voice_desktop_common::resources::spec(context, shared)?;
                    shared_resources.push(json!({
                        "resource_id":shared,
                        "title":shared_spec["title"],
                        "download_bytes":voice_desktop_common::resources::download_bytes(&shared_spec),
                        "fields":{"resource_id":"需要先安装的固定共用资源编号","title":"共用资源名称","download_bytes":"共用资源最大下载字节数"}
                    }));
                }
            }
            let environment = if spec["dependency_group"].as_str().is_some() {
                voice_desktop_common::runtime::plan(context, "all")?
            } else {
                Value::Null
            };
            json!({
                "resource_id":resource_id,
                "title":spec["title"],
                "model_bytes":model_bytes,
                "environment":environment,
                "shared_resources":shared_resources,
                "fields":{"resource_id":"固定资源编号","title":"资源名称","model_bytes":"模型或组件最大下载字节数","shared_resources":"需要先安装的共用原生运行程序","environment":"组件专用依赖组对应的完整共享运行环境计划；空值表示无需单独更新依赖"}
            })
        };
        let sources = if matches!(
            resource_id,
            "voice-server"
                | voice_desktop_common::runtime::CPU_RESOURCE
                | voice_desktop_common::runtime::GPU_RESOURCE
        ) {
            let urls = std::iter::once(&plan["environment"]["python"]["url"]).chain(
                plan["environment"]["wheels"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|row| &row["url"]),
            );
            let mut hosts = Vec::new();
            for url in urls.filter_map(Value::as_str).filter(|url| !url.is_empty()) {
                for candidate in voice_desktop_common::download_sources::candidates(url) {
                    let host = voice_desktop_common::download_sources::host(&candidate).to_owned();
                    if !hosts.contains(&host) {
                        hosts.push(host);
                    }
                }
            }
            hosts
        } else {
            voice_desktop_common::download_sources::plan_sources(
                &voice_desktop_common::resources::spec(context, resource_id)?,
            )
        };
        plan["download_sources"] = json!(sources);
        plan["fields"]["download_sources"] =
            json!("各类资源使用的镜像和原站域名，下载失败时按资源切换备用来源");
        Ok(plan)
    }

    /// remote_reset_plan（签发短期一次性重置确认）：服务端保存计划与 token（确认值）的绑定。
    pub(crate) fn remote_reset_plan(
        &self,
        request_id: &str,
        resource_id: &str,
    ) -> Result<Value, String> {
        if !voice_desktop_common::device_management_contract::valid_request_id(request_id)
            || !valid_resource_id(resource_id)
        {
            return Err("重置计划操作编号或组件编号无效。".into());
        }
        let mut approvals = self
            .remote_reset_approvals
            .lock()
            .map_err(|_| "远程重置确认状态不可用。")?;
        approvals.retain(|_, approval| approval.expires_at > Instant::now());
        if let Some(existing) = approvals.get(request_id) {
            if existing.resource_id != resource_id {
                return Err("同一重置计划编号不能更改组件目标。".into());
            }
            return Ok(existing.plan.clone());
        }
        if approvals.len() >= REMOTE_RESET_MAX_APPROVALS {
            return Err("远程重置确认容量已满，请稍后重试。".into());
        }
        let mut plan = voice_desktop_common::component_reset::plan(&self.context, resource_id)?;
        let token = plan["token"]
            .as_str()
            .ok_or("原生重置计划缺少确认值。")?
            .to_owned();
        let expires_at = Instant::now() + REMOTE_RESET_CONFIRMATION_TTL;
        plan["plan_request_id"] = json!(request_id);
        plan["expires_at_ms"] =
            json!(unix_millis().saturating_add(REMOTE_RESET_CONFIRMATION_TTL.as_millis() as u64));
        plan["fields"]["plan_request_id"] =
            json!("本次重置范围预览操作编号；执行重置必须回传相同编号");
        plan["fields"]["expires_at_ms"] = json!("本次重置确认到期时刻，Unix 毫秒");
        approvals.insert(
            request_id.to_owned(),
            RemoteResetApproval {
                resource_id: resource_id.to_owned(),
                token,
                plan: plan.clone(),
                expires_at,
            },
        );
        Ok(plan)
    }

    /// remote_reset（消费一次性确认并调用既有重置任务）：不能凭客户端自报路径删除文件。
    pub(crate) fn remote_reset(
        self: &Arc<Self>,
        plan_request_id: &str,
        resource_id: String,
        token: &str,
        confirmed: bool,
    ) -> Result<Value, String> {
        if !confirmed {
            return Err("用户尚未确认本次组件重置。".into());
        }
        if !voice_desktop_common::device_management_contract::valid_request_id(plan_request_id)
            || !valid_resource_id(&resource_id)
            || token.is_empty()
            || token.len() > 128
        {
            return Err("重置目标或一次性确认值无效。".into());
        }
        let mut approvals = self
            .remote_reset_approvals
            .lock()
            .map_err(|_| "远程重置确认状态不可用。")?;
        approvals.retain(|_, approval| approval.expires_at > Instant::now());
        let approved = approvals
            .get(plan_request_id)
            .is_some_and(|approval| approval.resource_id == resource_id && approval.token == token);
        if !approved {
            return Err("重置确认值不存在、已过期、已使用或不属于该组件。".into());
        }
        approvals.retain(|_, approval| approval.token != token);
        drop(approvals);
        let current = voice_desktop_common::component_reset::plan(&self.context, &resource_id)?;
        if current["token"] != token {
            return Err("组件重置范围已变化，请重新查看计划并确认。".into());
        }
        self.reset_resource(resource_id, token.to_owned())
    }

    pub fn has_active_jobs(&self) -> bool {
        self.jobs
            .lock()
            .map(|jobs| jobs.values().any(|job| !terminal(&job.snapshot.state)))
            .unwrap_or(true)
    }

    pub(crate) fn cleanup_local_storage(&self, token: &str, entries: &[String]) -> Result<Value, String> {
        self.with_idle_cleanup(|| {
            let mut lease = self.pause_service()?;
            self.remember_service_was_running(lease.was_running())?;
            let result = voice_desktop_common::workspace_cleanup::run_selected(&self.context, env!("CARGO_PKG_VERSION"), Some(token), true, Some(entries));
            self.maybe_resume_service(&mut lease)?;
            result
        })
    }
    pub(crate) fn with_idle_cleanup<T>(
        &self,
        operation: impl FnOnce() -> Result<T, String>,
    ) -> Result<T, String> {
        let jobs = self.jobs.try_lock().map_err(|_| "资源任务状态正在更新，请稍后清理。")?;
        if !self.accepting_jobs.load(Ordering::Acquire)
            || self.resetting.load(Ordering::Acquire)
            || jobs.values().any(|job| !terminal(&job.snapshot.state))
        {
            return Err("组件任务正在处理，请等待结束后清理工作区。".into());
        }
        operation()
    }
    pub fn configure_runtime(self: &Arc<Self>, group: String) -> Result<Value, String> {
        self.configure_runtime_child(group, None)
    }
    fn configure_runtime_child(
        self: &Arc<Self>,
        group: String,
        parent: Option<String>,
    ) -> Result<Value, String> {
        let job_id = format!("env-{group}");
        let resource_id = if voice_desktop_common::runtime::gpu_requested(&group) {
            voice_desktop_common::runtime::GPU_RESOURCE
        } else {
            "voice-server"
        };
        self.start_child_job(
            job_id.clone(),
            resource_id.into(),
            "正在配置语音环境".into(),
            true,
            parent,
            move |manager, id| {
                let cancel = manager.cancel_signal(&id)?;
                let mut progress = |phase: &str, done: u64, total: u64| {
                    manager.set_phase(&id, phase, !phase.starts_with("启用"), done, Some(total))
                };
                let _environment =
                    acquire_environment_lock(&manager.environment, &cancel, &mut progress)
                        .map_err(|message| JobError::Failed {
                            message,
                            installed: vec![],
                        })?;
                manager.retry_download(&id, || {
                    manager
                        .prepare_environment(&id, &group, cancel.clone(), &mut progress)
                        .map_err(|message| JobError::Failed {
                            message,
                            installed: vec![],
                        })
                })
            },
        )?;
        Ok(json!({"job_id":job_id}))
    }
    pub fn import_runtime(self: &Arc<Self>) -> Result<Value, String> {
        self.import_runtime_profile("all".into())
    }
    fn import_runtime_profile(self: &Arc<Self>, group: String) -> Result<Value, String> {
        let job_id = new_job_id("env-import");
        let resource_id = if voice_desktop_common::runtime::gpu_requested(&group) {
            voice_desktop_common::runtime::GPU_RESOURCE
        } else {
            "voice-server"
        };
        self.start_job(
            job_id.clone(),
            resource_id.into(),
            "选择语音环境目录".into(),
            true,
            move |manager, id| {
                let source = voice_desktop_common::native_ui::choose_folder(
                    "选择已准备的程序私有 Python（后端环境）目录",
                )
                .map_err(|message| JobError::Failed {
                    message,
                    installed: vec![],
                })?
                .ok_or(JobError::Cancelled)?;
                let cancel = manager.cancel_signal(&id)?;
                let mut progress = |phase: &str, done: u64, total: u64| {
                    manager.set_phase(&id, phase, !phase.starts_with("启用"), done, Some(total))
                };
                let _environment =
                    acquire_environment_lock(&manager.environment, &cancel, &mut progress)
                        .map_err(|message| JobError::Failed {
                            message,
                            installed: vec![],
                        })?;
                let result = voice_desktop_common::runtime::import_profile_with_activation(
                    &manager.context,
                    &source,
                    &group,
                    cancel.clone(),
                    &mut progress,
                    &mut |stage, relative| manager.activate_resource(&id, stage, relative),
                );
                result
                    .and_then(|installed| {
                        voice_desktop_common::compatibility::finish_deferred_settings(
                            &manager.context,
                            &cancel,
                        )?;
                        Ok(installed)
                    })
                    .map_err(|message| JobError::Failed {
                        message,
                        installed: vec![],
                    })
            },
        )?;
        Ok(json!({"job_id":job_id}))
    }

    /// Download（下载）：run a fixed plan, retaining a stable task ID for resuming files.
    pub fn download(self: &Arc<Self>, resource_id: String) -> Result<Value, String> {
        self.download_child(resource_id, None)
    }
    fn download_child(
        self: &Arc<Self>,
        resource_id: String,
        parent: Option<String>,
    ) -> Result<Value, String> {
        if matches!(
            resource_id.as_str(),
            "voice-server" | voice_desktop_common::runtime::CPU_RESOURCE
        ) {
            return self.configure_runtime_child("cpu".into(), parent);
        }
        if resource_id == voice_desktop_common::runtime::GPU_RESOURCE {
            return self.configure_runtime_child("gpu".into(), parent);
        }
        if !valid_resource_id(&resource_id) {
            return Err("资源编号格式无效。".into());
        }
        let job_id = format!("dl-{resource_id}"); // Same resource uses the same task ID across retries.
        self.start_child_job(
            job_id.clone(),
            resource_id.clone(),
            "正在准备资源下载".into(),
            true,
            parent,
            move |manager, job_id| {
                manager.retry_download(&job_id, || manager.run_download(&job_id, &resource_id))
            },
        )?;
        Ok(json!({ "job_id": job_id }))
    }

    /// download_required（一键下载）：原生端持有依赖顺序与取消状态，页面关闭不影响任务。
    pub fn download_required(self: &Arc<Self>) -> Result<Value, String> {
        let id = "dl-required".to_string();
        let result = self.start_job(
            id.clone(),
            "required-batch".into(),
            "准备必要组件".into(),
            true,
            |manager, parent| {
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    manager.run_required(&parent)
                }))
                .unwrap_or_else(|_| {
                    Err(JobError::Failed {
                        message: "一键下载异常中断，可重试。".into(),
                        installed: vec![],
                    })
                });
                manager.batch_active.store(false, Ordering::Release);
                if manager.pending_service_resume().unwrap_or(false) {
                    let mut lease =
                        manager
                            .pause_service()
                            .map_err(|message| JobError::Failed {
                                message,
                                installed: vec![],
                            })?;
                    match manager.maybe_resume_service(&mut lease) {
                        Ok(resumed) => {
                            clear_resume_intent_if_resumed(&manager.resume_pending, resumed)
                        }
                        Err(message) => {
                            return Err(JobError::Failed {
                                message,
                                installed: vec![],
                            });
                        }
                    }
                }
                outcome
            },
        );
        result?;
        Ok(json!({"job_id":id}))
    }

    fn run_required(self: &Arc<Self>, parent: &str) -> Result<Vec<String>, JobError> {
        let mut installed = vec![];
        let cpu = self
            .configure_runtime_child("cpu".into(), Some(parent.into()))
            .map_err(|message| JobError::Failed {
                message,
                installed: vec![],
            })?;
        let cpu_id = cpu["job_id"].as_str().unwrap().to_owned();
        self.wait_required(parent, &[cpu_id], &mut installed)?;
        let specs = voice_desktop_common::resources::specs(&self.context).map_err(|message| {
            JobError::Failed {
                message,
                installed: installed.clone(),
            }
        })?;
        let mut children = vec![];
        let mut errors = vec![];
        for item in specs.iter().filter(|item| include_in_required_batch(item)) {
            if self.cancelled(parent) {
                break;
            }
            let Some(resource) = item["resource_id"].as_str() else {
                continue;
            };
            match self.download_child(resource.into(), Some(parent.into())) {
                Ok(value) => {
                    let child = value["job_id"].as_str().unwrap().to_owned();
                    children.push(child);
                }
                Err(error) => errors.push(format!("{resource}（组件）：{error}")),
            }
        }
        if let Err(error) = self.wait_required(parent, &children, &mut installed) {
            return Err(error);
        }
        if errors.is_empty() {
            Ok(installed)
        } else {
            Err(JobError::Failed {
                message: errors.join("；"),
                installed,
            })
        }
    }

    fn wait_required(
        &self,
        parent: &str,
        children: &[String],
        installed: &mut Vec<String>,
    ) -> Result<(), JobError> {
        loop {
            if self.cancelled(parent) {
                for child in children {
                    let _ = self.cancel(child);
                }
            }
            let rows = {
                let jobs = self.jobs.lock().map_err(|_| JobError::Failed {
                    message: "资源任务状态不可用。".into(),
                    installed: installed.clone(),
                })?;
                children
                    .iter()
                    .filter_map(|id| jobs.get(id).map(|job| job.snapshot.clone()))
                    .collect::<Vec<_>>()
            };
            for row in &rows {
                for id in &row.installed_resources {
                    if !installed.contains(id) {
                        installed.push(id.clone());
                    }
                }
            }
            let active = rows
                .iter()
                .filter(|row| !terminal(&row.state))
                .collect::<Vec<_>>();
            if active.is_empty() {
                if self.cancelled(parent) {
                    return Err(JobError::Cancelled);
                }
                let errors = rows
                    .iter()
                    .filter(|row| row.state != "complete")
                    .map(|row| {
                        format!(
                            "{}（组件）：{}",
                            row.resource_id,
                            row.error.as_deref().unwrap_or("任务已取消，可重试")
                        )
                    })
                    .collect::<Vec<_>>();
                return if errors.is_empty() {
                    Ok(())
                } else {
                    Err(JobError::Failed {
                        message: errors.join("；"),
                        installed: installed.clone(),
                    })
                };
            }
            let phase = format!(
                "一键下载：已完成 {} / {} 项；{}",
                rows.len() - active.len(),
                children.len(),
                active
                    .first()
                    .map(|row| row.phase.as_str())
                    .unwrap_or("正在处理资源")
            );
            self.set_phase(parent, &phase, true, 0, None);
            self.update_job(parent, |row| row.installed_resources = installed.clone());
            thread::sleep(Duration::from_millis(100));
        }
    }

    /// retry_download（任务内自动恢复）：最多三轮，只清理本任务拥有的错误候选。
    fn retry_download<F>(&self, id: &str, mut operation: F) -> Result<Vec<String>, JobError>
    where
        F: FnMut() -> Result<Vec<String>, JobError>,
    {
        let mut installed = vec![];
        for attempt in 1..=3 {
            if self.cancelled(id) {
                return Err(JobError::Cancelled);
            }
            self.update_job(id, |row| {
                row.attempt = attempt;
                row.failure_resource_id = None;
                row.can_cancel = true;
                row.progress_kind = "waiting".into();
                row.completed_bytes = 0;
                row.total_bytes = None;
                row.download_completed_bytes = None;
                row.download_total_bytes = None;
                row.verification_completed_bytes = None;
                row.verification_total_bytes = None;
            });
            let recovered = if attempt > 1
                && self
                    .context
                    .install
                    .join("tmp/native-install.json")
                    .exists()
            {
                self.recover_pending_transaction()
            } else {
                Ok(())
            };
            let outcome = recovered
                .map_err(|message| JobError::Failed {
                    message,
                    installed: vec![],
                })
                .and_then(|_| {
                    voice_desktop_common::download::with_attempt(Some(attempt), &mut operation)
                });
            match outcome {
                Ok(mut result) => {
                    installed.append(&mut result);
                    installed.sort();
                    installed.dedup();
                    return Ok(installed);
                }
                Err(JobError::Failed {
                    message,
                    installed: partial,
                }) => {
                    installed.extend(partial);
                    self.update_job(id, |row| {
                        if row.first_error.is_none() {
                            row.first_error = Some(message.clone());
                        }
                    });
                    let event = voice_desktop_common::component_recovery::classify(&message);
                    if !voice_desktop_common::component_recovery::automatic(event, attempt) {
                        return Err(JobError::Failed { message, installed });
                    }
                    let guidance = voice_desktop_common::component_recovery::guidance(event, true);
                    self.update_job(id, |row| row.recovery = Some(guidance));
                    self.set_phase(
                        id,
                        &format!(
                            "{}；准备第 {} / 3 轮",
                            if voice_desktop_common::component_recovery::local_rebuild(event) {
                                "核验本机材料并局部重建当前组件"
                            } else {
                                "等待后重试当前操作"
                            },
                            attempt + 1
                        ),
                        true,
                        0,
                        None,
                    );
                    let resource = self
                        .jobs
                        .lock()
                        .ok()
                        .and_then(|rows| rows.get(id).map(|row| row.snapshot.resource_id.clone()))
                        .unwrap_or_default();
                    if voice_desktop_common::component_recovery::local_rebuild(event) {
                        if let Err(error) = self.clear_failed_candidate(&resource) {
                            return Err(JobError::Failed {
                                message: format!("{message}；局部重建准备失败：{error}"),
                                installed,
                            });
                        }
                    }
                    for _ in 0..if attempt == 1 { 10 } else { 30 } {
                        if self.cancelled(id) {
                            return Err(JobError::Cancelled);
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!()
    }

    fn clear_failed_candidate(&self, resource: &str) -> Result<(), String> {
        let mut relatives = vec![];
        // 未完成事务的旧内容与证据不能作为失败候选清理。
        if self
            .context
            .install
            .join("tmp/native-install.json")
            .exists()
        {
            return Err("组件启用事务尚未恢复，保留事务恢复记录，请执行全面完整性校验。".into());
        }
        if [
            "voice-server",
            voice_desktop_common::runtime::CPU_RESOURCE,
            voice_desktop_common::runtime::GPU_RESOURCE,
        ]
        .contains(&resource)
        {
            relatives.push("tmp/voice-environment/stage".to_owned());
        } else if valid_resource_id(resource) {
            for suffix in ["extracted", "model", "candidate", "unpacked"] {
                relatives.push(format!("tmp/resource-downloads/{resource}/{suffix}"));
            }
        }
        for relative in relatives {
            let path =
                voice_desktop_common::workspace::safe_path(&self.context.install, &relative)?;
            voice_desktop_common::workspace::remove_owned(&path, &self.context.install)?;
        }
        Ok(())
    }
    fn recover_pending_transaction(&self) -> Result<(), String> {
        let mut lease = self.pause_service()?;
        self.remember_service_was_running(lease.was_running())?;
        if let Err(error) = voice_desktop_common::resources::recover(&self.context) {
            lease.keep_stopped();
            return Err(error);
        }
        self.update_job("component-recovery", |row| {
            row.state = "complete".into();
            row.phase = "上次组件事务问题已恢复".into();
            row.error = None;
            row.recovery = None;
        });
        let resumed = self.maybe_resume_service(&mut lease)?;
        clear_resume_intent_if_resumed(&self.resume_pending, resumed);
        Ok(())
    }

    /// Import（导入）：open the native picker on a worker thread and install selected sources.
    pub fn import(self: &Arc<Self>, resource_id: Option<String>) -> Result<Value, String> {
        if resource_id.as_deref() == Some("voice-server") {
            return self.import_runtime();
        }
        if resource_id.as_deref() == Some(voice_desktop_common::runtime::CPU_RESOURCE) {
            return self.import_runtime_profile("cpu".into());
        }
        if resource_id.as_deref() == Some(voice_desktop_common::runtime::GPU_RESOURCE) {
            return self.import_runtime_profile("gpu".into());
        }
        if let Some(id) = &resource_id {
            if !valid_resource_id(id) {
                return Err("资源编号格式无效。".into());
            }
        }
        let job_id = new_job_id("im");
        let resource_key = resource_id.clone().unwrap_or_else(|| "batch".into());
        self.start_job(
            job_id.clone(),
            resource_key,
            "正在选择资源来源".into(),
            true,
            move |manager, job_id| manager.run_import(&job_id, resource_id),
        )?;
        Ok(json!({ "job_id": job_id }))
    }

    /// choose_import_sources（选择导入来源）：只返回短期票据，来源路径始终留在原生进程。
    pub fn choose_import_sources(&self, resource_id: Option<String>) -> Result<Value, String> {
        if let Some(id) = &resource_id {
            if !valid_resource_id(id) {
                return Err("资源编号格式无效。".into());
            }
        }
        let sources = voice_desktop_common::native_ui::choose_resource(resource_id.as_deref())?;
        if sources.is_empty() {
            return Ok(json!({
                "cancelled": true,
                "selection_id": null,
                "count": 0,
                "fields": {
                    "cancelled": "用户取消了本机来源选择",
                    "selection_id": "一次性原生来源票据；取消时为空",
                    "count": "选择的来源数量",
                }
            }));
        }
        if sources.len() > 64 {
            return Err("一次最多选择 64 个导入来源。".into());
        }
        for source in &sources {
            let metadata =
                fs::symlink_metadata(source).map_err(|_| "所选导入来源已不存在或不可访问。")?;
            if metadata.file_type().is_symlink() || !(metadata.is_file() || metadata.is_dir()) {
                return Err("导入来源必须是普通文件或文件夹，不能是链接或特殊文件。".into());
            }
        }
        let selection_id = new_job_id("is");
        let mut selections = self
            .import_selections
            .lock()
            .map_err(|_| "导入来源状态不可用。")?;
        selections.retain(|_, row| row.expires_at > Instant::now());
        while selections.len() >= 64 {
            let Some(oldest) = selections
                .iter()
                .min_by_key(|(_, row)| row.expires_at)
                .map(|(key, _)| key.clone())
            else {
                break;
            };
            selections.remove(&oldest);
        }
        selections.insert(
            selection_id.clone(),
            PendingImportSelection {
                resource_id,
                sources,
                expires_at: Instant::now() + Duration::from_secs(10 * 60),
            },
        );
        Ok(json!({
            "cancelled": false,
            "selection_id": selection_id,
            "count": selections[&selection_id].sources.len(),
            "fields": {
                "cancelled": "用户是否取消了本机来源选择",
                "selection_id": "一次性原生来源票据；绝对路径不会返回前端",
                "count": "选择的来源数量",
            }
        }))
    }

    /// start_import_sources（启动已选择来源的导入）：消费一次性票据并启动既有导入事务。
    pub fn start_import_sources(self: &Arc<Self>, selection_id: String) -> Result<Value, String> {
        if selection_id.len() > 96 || !selection_id.starts_with("is-") {
            return Err("导入来源票据无效。".into());
        }
        let selection = self
            .import_selections
            .lock()
            .map_err(|_| "导入来源状态不可用。")?
            .remove(&selection_id)
            .ok_or_else(|| "导入来源票据已使用、取消或过期，请重新选择。".to_owned())?;
        if selection.expires_at <= Instant::now() {
            return Err("导入来源票据已过期，请重新选择。".into());
        }
        self.start_import_job(selection.resource_id, selection.sources)
    }

    fn start_import_job(
        self: &Arc<Self>,
        resource_id: Option<String>,
        sources: Vec<PathBuf>,
    ) -> Result<Value, String> {
        if sources.is_empty() || sources.len() > 64 {
            return Err("导入来源数量无效，请重新选择。".into());
        }
        let job_id = new_job_id("im");
        let resource_key = resource_id.clone().unwrap_or_else(|| "batch".into());
        self.start_job(
            job_id.clone(),
            resource_key,
            "正在校验所选资源来源".into(),
            true,
            move |manager, job_id| manager.run_import_sources(&job_id, resource_id, sources),
        )?;
        Ok(json!({ "job_id": job_id }))
    }

    /// AnifLive import（AnifLive 导入）：使用一份已暂存输入说明启动现有转换任务，保持唯一转换链路。
    pub fn start_aniflive_import(self: &Arc<Self>, task_id: String) -> Result<Value, String> {
        if task_id.len() != 32
            || !task_id
                .bytes()
                .all(|value| value.is_ascii_hexdigit() && !value.is_ascii_uppercase())
        {
            return Err("AnifLive 导入任务编号无效。".into());
        }
        let stage_relative = format!("cache/aniflive-tts/import-staging/{task_id}");
        let job_id = format!("aniflive-import-{task_id}");
        if self
            .jobs
            .lock()
            .map_err(|_| "资源任务状态不可用。")?
            .get(&job_id)
            .is_some_and(|job| !terminal(&job.snapshot.state) || job.snapshot.state == "complete")
        {
            return Ok(json!({ "job_id": job_id }));
        }
        let stage =
            voice_desktop_common::workspace::safe_path(&self.context.install, &stage_relative)?;
        let spec = stage.join("aniflive-input.json");
        if !spec.is_file() || spec.is_symlink() {
            return Err("模型尚未完成暂存，或仍需补充参考录音与转写。".into());
        }
        let manifest: Value = serde_json::from_slice(
            &fs::read(stage.join("import-manifest.json")).map_err(|_| "导入暂存清单不可用。")?,
        )
        .map_err(|_| "导入暂存清单格式无效。")?;
        if manifest["task_id"] != task_id || manifest["model_id"].as_str().is_none() {
            return Err("导入暂存清单与任务编号不匹配。".into());
        }
        let input: Value =
            serde_json::from_slice(&fs::read(&spec).map_err(|_| "转换输入说明文件不可用。")?)
                .map_err(|_| "转换输入说明文件格式无效。")?;
        if input["import_id"] != task_id || input["model_id"] != manifest["model_id"] {
            return Err("转换输入说明与本次导入任务不匹配。".into());
        }
        let model_id = input["model_id"]
            .as_str()
            .ok_or("导入模型编号缺失。")?
            .to_owned();
        if model_id.len() > 50
            || model_id.is_empty()
            || !model_id.bytes().enumerate().all(|(index, value)| {
                value.is_ascii_lowercase()
                    || value.is_ascii_digit()
                    || (index > 0 && matches!(value, b'.' | b'_' | b'-'))
            })
        {
            return Err("导入模型编号超出受管资源编号范围。".into());
        }
        self.start_job(job_id.clone(), "tts-aniflive-import".into(), "等待 AnifLive 模型转换".into(), true,
            move |manager, id| {
                let cancel = manager.cancel_signal(&id)?;
                let mut progress = |phase: &str, done: u64, total: u64| {
                    manager.set_phase(&id, phase, !phase.starts_with("启用"), done, if total == 0 { None } else { Some(total) });
                };
                let _runtime = acquire_environment_lock(&manager.tts_runtime, &cancel, &mut progress)
                    .map_err(|message| JobError::Failed { message, installed: vec![] })?;
                let registration_marker = stage.join("conversion-registered.json");
                let input_cache_relative = format!("cache/aniflive-tts/inputs/imports/{task_id}");
                let input_cache = voice_desktop_common::workspace::safe_path(&manager.context.install, &input_cache_relative)
                    .map_err(|message| JobError::Failed { message, installed: vec![] })?;
                let installed = if registration_marker.is_file() {
                    let marker: Value = serde_json::from_slice(&fs::read(&registration_marker)
                        .map_err(|_| JobError::Failed { message: "转换登记收据无法读取，暂存文件保留。".into(), installed: vec![] })?)
                        .map_err(|_| JobError::Failed { message: "转换登记收据格式无效，暂存文件保留。".into(), installed: vec![] })?;
                    if marker["task_id"] != task_id || marker["model_id"] != model_id
                        || !voice_desktop_common::resources::aniflive_model_ready(&manager.context, &model_id, false).unwrap_or(false) {
                        return Err(JobError::Failed { message: "已登记模型与本次导入收据不匹配，暂存文件保留。".into(), installed: vec![] });
                    }
                    vec![format!("tts-aniflive-{model_id}")]
                } else {
                    let registry_path = voice_desktop_common::workspace::safe_path(&manager.context.data, "config/aniflive-models.json")
                        .map_err(|message| JobError::Failed { message, installed: vec![] })?;
                    let mut already_registered = false;
                    if registry_path.is_file() {
                        let registry: Value = serde_json::from_slice(&fs::read(&registry_path)
                            .map_err(|_| JobError::Failed { message: "AnifLive 模型登记不可读取，未覆盖现有模型。".into(), installed: vec![] })?)
                            .map_err(|_| JobError::Failed { message: "AnifLive 模型登记格式无效，未覆盖现有模型。".into(), installed: vec![] })?;
                        if registry["models"].get(model_id.as_str()).is_some() {
                            let owned_input = input_cache.join("input.json");
                            let cached = fs::read(&owned_input).ok()
                                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
                            let revision = registry["models"][model_id.as_str()]["current_revision"].as_str().unwrap_or("");
                            let identity = registry["models"][model_id.as_str()]["versions"][revision]["identity"].clone();
                            let inputs_match = cached.as_ref().is_some_and(|cached| {
                                if cached["import_id"] != task_id || cached["model_id"] != model_id { return false; }
                                let hash_matches = ["gpt", "sovits", "reference"].into_iter().all(|field| {
                                    let Some(source) = cached[field].as_str().map(PathBuf::from) else { return false; };
                                    let Ok(relative) = source.strip_prefix(&input_cache) else { return false; };
                                    let relative = relative.to_string_lossy().replace('\\', "/");
                                    let Ok(source) = voice_desktop_common::workspace::safe_path(&input_cache, &relative) else { return false; };
                                    let Ok(actual) = voice_desktop_common::workspace::file_hash(&source) else { return false; };
                                    identity[field].as_str() == Some(actual.as_str())
                                });
                                let transcript = cached["reference_text"].as_str().and_then(|path| fs::read_to_string(path).ok());
                                hash_matches
                                    && transcript.as_deref().map(str::trim) == identity["reference_text"].as_str()
                                    && cached["reference_language"] == identity["language"]
                            });
                            if !inputs_match || !voice_desktop_common::resources::aniflive_model_ready(&manager.context, &model_id, false).unwrap_or(false) {
                                return Err(JobError::Failed { message: "该模型编号已经登记；请为导入模型使用一个新的唯一编号。".into(), installed: vec![] });
                            }
                            voice_desktop_common::workspace::atomic_json(&registration_marker,
                                &json!({"task_id":task_id,"model_id":model_id,"state":"registered","fields":{"task_id":"对应的导入任务编号","model_id":"已验证登记模型编号","state":"该导入任务已完成转换、验证与登记"}}))
                                .map_err(|message| JobError::Failed { message: format!("已登记模型的任务收据写入失败，暂存文件保留：{message}"), installed: vec![] })?;
                            already_registered = true;
                        }
                    }
                    if already_registered {
                        vec![format!("tts-aniflive-{model_id}")]
                    } else {
                        let args = vec![
                            OsString::from("--root"), manager.context.install.clone().into_os_string(),
                            OsString::from("--program"), manager.context.program.clone().into_os_string(),
                            OsString::from("--prepare-runtime"), OsString::from("--input-spec"), spec.into_os_string(),
                            OsString::from("--convert"), OsString::from("--native-ack"),
                        ];
                        let installed = manager.run_aniflive_task(&id, &args, Arc::clone(&cancel))?;
                        voice_desktop_common::workspace::atomic_json(&registration_marker,
                            &json!({"task_id":task_id,"model_id":model_id,"state":"registered","fields":{"task_id":"对应的导入任务编号","model_id":"已验证登记模型编号","state":"该导入任务已完成转换、验证与登记"}}))
                            .map_err(|message| JobError::Failed { message: format!("模型已登记，但转换收据写入失败，暂存文件保留：{message}"), installed: installed.clone() })?;
                        installed
                    }
                };
                // 独占暂停业务后登记参考资料，避免依赖页面请求恢复后端监视器。
                let mut lease = manager.pause_service().map_err(|message| JobError::Failed { message, installed: installed.clone() })?;
                manager.remember_service_was_running(lease.was_running()).map_err(|message| JobError::Failed { message, installed: installed.clone() })?;
                manager.set_phase(&id, "正在登记音色参考", true, 0, None);
                let registration_args = vec![OsString::from("--root"), manager.context.install.clone().into_os_string(),
                    OsString::from("--data"), manager.context.data.clone().into_os_string(), OsString::from("--task"), OsString::from(&task_id)];
                let result = manager.run_stream_script(&id, &manager.context.program.join("scripts/register-aniflive-references.py"),
                    &registration_args, |_, done, total| { manager.set_phase(&id, "正在登记音色参考", true, done,
                        if total == 0 { None } else { Some(total) }); Ok(()) });
                let resumed = manager.maybe_resume_service(&mut lease).map_err(|message| JobError::Failed { message, installed: installed.clone() })?;
                clear_resume_intent_if_resumed(&manager.resume_pending, resumed);
                result.map_err(|error| if error.cancelled { JobError::Cancelled } else { JobError::Failed {
                    message: format!("模型已登记，但参考资料登记失败，可重试：{}", error.message), installed: installed.clone() } })?;
                let receipt_relative = format!("tts/aniflive/reference-imports/{task_id}/reference-registration.json");
                let reference_receipt = voice_desktop_common::workspace::safe_path(&manager.context.data, &receipt_relative)
                    .map_err(|message| JobError::Failed { message, installed: installed.clone() })?;
                // 大模型的完整摘要核验可能超过三分钟；超时仍保留模型与全部暂存供重试。
                let deadline = Instant::now() + Duration::from_secs(1800);
                while !reference_receipt.is_file() {
                    if cancel.load(Ordering::Acquire) { return Err(JobError::Cancelled); }
                    if Instant::now() >= deadline {
                        return Err(JobError::Failed { message: "等待参考资料登记超时；模型已登记，暂存资料已保留，可重试。".into(), installed });
                    }
                    thread::sleep(Duration::from_millis(200));
                }
                let reference: Value = serde_json::from_slice(&fs::read(&reference_receipt)
                    .map_err(|_| JobError::Failed { message: "参考资料登记收据无法读取，暂存文件保留。".into(), installed: installed.clone() })?)
                    .map_err(|_| JobError::Failed { message: "参考资料登记收据格式无效，暂存文件保留。".into(), installed: installed.clone() })?;
                if reference["task_id"] != task_id || reference["model_id"] != model_id || !reference["registration"].is_object() {
                    return Err(JobError::Failed { message: "参考资料登记收据与当前任务不匹配，暂存文件保留。".into(), installed });
                }
                if input_cache.exists() {
                    let cached_bytes = fs::read(input_cache.join("input.json"))
                        .map_err(|_| JobError::Failed { message: "导入输入缓存无法确认归属，保留等待人工核对。".into(), installed: installed.clone() })?;
                    let cached: Value = serde_json::from_slice(&cached_bytes)
                        .map_err(|_| JobError::Failed { message: "导入输入缓存格式无效，保留等待人工核对。".into(), installed: installed.clone() })?;
                    if cached["import_id"] != task_id || cached["model_id"] != model_id {
                        return Err(JobError::Failed { message: "导入输入缓存不属于本次任务，未执行清理。".into(), installed });
                    }
                    voice_desktop_common::workspace::remove_owned(&input_cache, &manager.context.install)
                        .map_err(|message| JobError::Failed { message: format!("模型已登记，但本次输入缓存清理失败：{message}"), installed: installed.clone() })?;
                }
                let stage_for_cleanup = voice_desktop_common::workspace::safe_path(&manager.context.install, &stage_relative)
                    .map_err(|message| JobError::Failed { message, installed: installed.clone() })?;
                let saved_bytes = fs::read(stage_for_cleanup.join("import-manifest.json"))
                    .map_err(|_| JobError::Failed { message: "导入暂存清单丢失，原件保留。".into(), installed: installed.clone() })?;
                let saved: Value = serde_json::from_slice(&saved_bytes)
                    .map_err(|_| JobError::Failed { message: "导入暂存清单格式无效，原件保留。".into(), installed: installed.clone() })?;
                if saved["task_id"] != task_id {
                    return Err(JobError::Failed { message: "导入暂存目录不属于本次任务，未执行清理。".into(), installed });
                }
                voice_desktop_common::workspace::remove_owned(&stage_for_cleanup, &manager.context.install)
                    .map_err(|message| JobError::Failed { message: format!("模型已登记，但本次暂存文件清理失败：{message}"), installed: installed.clone() })?;
                Ok(installed)
            })?;
        Ok(json!({ "job_id": job_id }))
    }

    /// OpenSource（打开来源）：read only the locally pinned URL from the packaged catalog.
    pub fn open_source(&self, resource_id: &str) -> Result<(), String> {
        if resource_id == voice_desktop_common::runtime::GPU_RESOURCE {
            #[cfg(windows)]
            return voice_desktop_common::windows::open_url(
                "https://pytorch.org/get-started/locally/",
            )
            .map_err(|_| "无法打开 PyTorch（张量框架）官方来源。".into());
            #[cfg(not(windows))]
            return Err("打开资源来源仅支持 Windows（操作系统）。".into());
        }
        if matches!(
            resource_id,
            "voice-server" | voice_desktop_common::runtime::CPU_RESOURCE
        ) {
            #[cfg(windows)]
            return voice_desktop_common::windows::open_url(
                "https://www.python.org/downloads/release/python-31210/",
            )
            .map_err(|_| "无法打开 Python（后端解释器）官方来源。".into());
            #[cfg(not(windows))]
            return Err("打开资源来源仅支持 Windows（操作系统）。".into());
        }
        if !valid_resource_id(resource_id) {
            return Err("资源编号格式无效。".into());
        }
        if resource_id == "dsh" {
            let spec = voice_desktop_common::resources::spec(&self.context, resource_id)?;
            if spec["origin"] == "npm_package" && !voice_desktop_common::dsh::node_installed() {
                #[cfg(windows)]
                return voice_desktop_common::windows::open_url(
                    voice_desktop_common::download_sources::policy()["node_download_page"]
                        .as_str()
                        .unwrap(),
                );
            }
        }
        let catalog_path = self.context.program.join("config/resources/catalog.json");
        let bytes = fs::read(catalog_path).map_err(|_| "资源来源清单不可用。")?;
        let catalog: Value =
            serde_json::from_slice(&bytes).map_err(|_| "资源来源清单格式无效。")?;
        let entries = catalog["resources"]
            .as_array()
            .ok_or("资源来源清单格式无效。")?;
        let entry = entries
            .iter()
            .find(|item| item["resource_id"].as_str() == Some(resource_id))
            .ok_or("未找到该资源的固定来源。")?;
        let url_text = entry["source"]["page_url"]
            .as_str()
            .or_else(|| entry["source"]["url"].as_str())
            .ok_or("该资源没有网页来源。")?;
        let url: tauri::Url = url_text.parse().map_err(|_| "资源来源地址无效。")?;
        if url.scheme() != "https"
            || url.query().is_some()
            || url.fragment().is_some()
            || !matches!(
                url.host_str(),
                Some("github.com" | "huggingface.co" | "modelscope.cn")
            )
        {
            return Err("资源来源地址不符合固定平台约束。".into());
        }
        #[cfg(windows)]
        {
            voice_desktop_common::windows::open_url(url.as_str())
                .map_err(|_| "无法打开系统默认浏览器。".into())
        }
        #[cfg(not(windows))]
        {
            let _ = url;
            Err("打开资源来源仅支持 Windows（操作系统）。".into())
        }
    }

    /// Cancel（取消）：reject cancellation once an installer enters its commit phase.
    pub fn cancel(&self, job_id: &str) -> Result<(), String> {
        let (child, event) = {
            let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
            let job = jobs.get_mut(job_id).ok_or("找不到该资源任务。")?;
            if !job.snapshot.can_cancel {
                return Err("当前资源阶段不能取消。".into());
            }
            job.cancel.store(true, Ordering::Release);
            job.snapshot.state = "cancelling".into();
            job.snapshot.phase = "正在取消当前阶段".into();
            job.snapshot.can_cancel = false;
            job.snapshot.revision = job.snapshot.revision.saturating_add(1);
            let child = job.child.clone();
            let event = event_value(&job.snapshot);
            self.persist_locked(&jobs)?;
            (child, event)
        };
        if let Some(child) = child {
            if let Ok(mut process) = child.lock() {
                process.kill();
            }
        }
        let _ = self.app.emit(PROGRESS_EVENT, event);
        Ok(())
    }

    /// PauseAndWait（切换前排空任务）：cancel copy/download work and wait for commits to finish.
    pub fn pause_and_wait(&self) -> Result<(), String> {
        let mut children = Vec::new();
        let mut events = Vec::new();
        {
            let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
            self.accepting_jobs.store(false, Ordering::Release);
            for job in jobs.values_mut() {
                if terminal(&job.snapshot.state) || !job.snapshot.can_cancel {
                    continue;
                }
                job.cancel.store(true, Ordering::Release);
                job.snapshot.state = "cancelling".into();
                job.snapshot.phase = "工作区切换前正在停止可取消阶段".into();
                job.snapshot.can_cancel = false;
                job.snapshot.revision = job.snapshot.revision.saturating_add(1);
                if let Some(child) = job.child.clone() {
                    children.push(child);
                }
                events.push(event_value(&job.snapshot));
            }
            self.persist_locked(&jobs)?;
        }
        for child in children {
            if let Ok(mut process) = child.lock() {
                process.kill();
            }
        }
        for event in events {
            let _ = self.app.emit(PROGRESS_EVENT, event);
        }

        let deadline = Instant::now() + Duration::from_secs(60);
        let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
        while jobs.values().any(|job| !terminal(&job.snapshot.state)) {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("资源仍在原子安装，当前不能切换工作区。".into());
            }
            let (next, timeout) = self
                .jobs_changed
                .wait_timeout(jobs, remaining)
                .map_err(|_| "资源任务状态不可用。")?;
            jobs = next;
            if timeout.timed_out() && jobs.values().any(|job| !terminal(&job.snapshot.state)) {
                return Err("资源仍在原子安装，当前不能切换工作区。".into());
            }
        }
        drop(jobs);
        // 已排空资源提交后暂停现用服务；意图保留到交接成功或取消后的恢复完成。
        let mut lease = self.pause_service()?;
        let remembered = self.remember_service_was_running(lease.was_running());
        lease.keep_stopped();
        remembered
    }

    pub fn resume_accepting_jobs(&self) -> Result<(), String> {
        self.accepting_jobs.store(true, Ordering::Release);
        if self.pending_service_resume()? {
            let mut lease = self.pause_service()?;
            let resumed = self.maybe_resume_service(&mut lease)?;
            clear_resume_intent_if_resumed(&self.resume_pending, resumed);
            if resumed {
                self.finish_recovery(None);
            }
        }
        Ok(())
    }

    fn run_download(
        self: &Arc<Self>,
        job_id: &str,
        resource_id: &str,
    ) -> Result<Vec<String>, JobError> {
        if self.cancelled(job_id) {
            return Err(JobError::Cancelled);
        }
        let cancel = self.cancel_signal(job_id)?;
        let mut progress = |phase: &str, done: u64, total: u64| {
            if phase.starts_with("启用") || phase.starts_with("正在启用") {
                let _ = self.begin_non_cancellable_phase(job_id, phase, done, Some(total));
            } else {
                self.set_phase(
                    job_id,
                    phase,
                    true,
                    done,
                    if total == 0 { None } else { Some(total) },
                );
            }
        };
        // dependency_group（依赖组）模型先核对共享环境登记，仅有依赖变更时配置增量，再下载模型文件。
        let mut installed = self
            .configure_resource_dependencies(job_id, resource_id, cancel.clone(), &mut progress)
            .map_err(|message| {
                self.update_job(job_id, |row| {
                    row.failure_resource_id =
                        Some(voice_desktop_common::runtime::CPU_RESOURCE.into())
                });
                JobError::Failed {
                    message,
                    installed: vec![],
                }
            })?;
        let item = voice_desktop_common::resources::spec(&self.context, resource_id).map_err(
            |message| JobError::Failed {
                message,
                installed: installed.clone(),
            },
        )?;
        if item["preparation"] == "aniflive_runtime" || item["preparation"] == "aniflive_converter" {
            let _shared = acquire_environment_lock(&self.tts_runtime, &cancel, &mut progress)
                .map_err(|message| JobError::Failed {
                    message,
                    installed: installed.clone(),
                })?;
            let args = vec![
                OsString::from("--root"),
                self.context.install.clone().into_os_string(),
                OsString::from("--program"),
                self.context.program.clone().into_os_string(),
                OsString::from(if item["preparation"] == "aniflive_runtime" {"--prepare-inference"} else {"--prepare-conversion"}),
                OsString::from("--native-ack"),
            ];
            installed.extend(self.run_aniflive_task(job_id, &args, cancel)?);
            return Ok(installed);
        }
        if item["preparation"] == "aniflive_local_build" {
            let _shared = acquire_environment_lock(&self.tts_runtime, &cancel, &mut progress)
                .map_err(|message| JobError::Failed {
                    message,
                    installed: installed.clone(),
                })?;
            let preset = item["model_id"].as_str().ok_or_else(|| JobError::Failed {
                message: "本地模型缺少固定输入编号。".into(),
                installed: installed.clone(),
            })?;
            let args = vec![
                OsString::from("--root"),
                self.context.install.clone().into_os_string(),
                OsString::from("--program"),
                self.context.program.clone().into_os_string(),
                OsString::from("--prepare-runtime"),
                OsString::from(if item["imported"] == true {
                    "--registered-model"
                } else {
                    "--preset"
                }),
                OsString::from(preset),
                OsString::from("--convert"),
                OsString::from("--native-ack"),
            ];
            installed.extend(self.run_aniflive_task(job_id, &args, cancel.clone())?);
            if item["imported"] != true {
                // 独占暂停业务服务，由组件任务中的唯一写入者登记；结束后恢复原服务。
                let mut lease = self.pause_service().map_err(|message| JobError::Failed {
                    message,
                    installed: installed.clone(),
                })?;
                self.remember_service_was_running(lease.was_running())
                    .map_err(|message| JobError::Failed {
                        message,
                        installed: installed.clone(),
                    })?;
                self.set_phase(job_id, "正在登记预设音色参考", true, 0, None);
                let registration_args = vec![
                    OsString::from("--root"),
                    self.context.install.clone().into_os_string(),
                    OsString::from("--data"),
                    self.context.data.clone().into_os_string(),
                    OsString::from("--model"),
                    OsString::from(preset),
                ];
                let result = self.run_stream_script(
                    job_id,
                    &self
                        .context
                        .program
                        .join("scripts/register-aniflive-references.py"),
                    &registration_args,
                    |phase, done, total| {
                        self.set_phase(
                            job_id,
                            phase,
                            true,
                            done,
                            if total == 0 { None } else { Some(total) },
                        );
                        Ok(())
                    },
                );
                let resumed =
                    self.maybe_resume_service(&mut lease)
                        .map_err(|message| JobError::Failed {
                            message,
                            installed: installed.clone(),
                        })?;
                clear_resume_intent_if_resumed(&self.resume_pending, resumed);
                result.map_err(|error| {
                    if error.cancelled {
                        JobError::Cancelled
                    } else {
                        JobError::Failed {
                            message: format!(
                                "模型已登记，但参考资料登记失败，可重试：{}",
                                error.message
                            ),
                            installed: installed.clone(),
                        }
                    }
                })?;
            }
            return Ok(installed);
        }
        if let Some(shared) = item["runtime_resource"]
            .as_str()
            .filter(|_| !matches!(resource_id, "tts-voxcpm1" | "tts-voxcpm2"))
        {
            let _shared = acquire_environment_lock(&self.tts_runtime, &cancel, &mut progress)
                .map_err(|message| JobError::Failed {
                    message,
                    installed: installed.clone(),
                })?;
            let result = voice_desktop_common::resources::download_resource_with_activation(
                &self.context,
                shared,
                cancel.clone(),
                &mut progress,
                &mut |stage, relative| self.activate_resource(job_id, stage, relative),
            )
            .map_err(|message| JobError::Failed {
                message,
                installed: installed.clone(),
            })?;
            installed.extend(result);
        }
        let _shared = if resource_id == "tts-audiocpp-runtime" {
            Some(
                acquire_environment_lock(&self.tts_runtime, &cancel, &mut progress).map_err(
                    |message| JobError::Failed {
                        message,
                        installed: installed.clone(),
                    },
                )?,
            )
        } else {
            None
        };
        let result = voice_desktop_common::resources::download_resource_with_activation(
            &self.context,
            resource_id,
            cancel,
            &mut progress,
            &mut |stage, relative| self.activate_resource(job_id, stage, relative),
        )
        .map_err(|message| JobError::Failed {
            message,
            installed: installed.clone(),
        })?;
        installed.extend(result);
        Ok(installed)
    }

    fn run_aniflive_task(
        &self,
        job_id: &str,
        args: &[OsString],
        cancel: Arc<AtomicBool>,
    ) -> Result<Vec<String>, JobError> {
        if cancel.load(Ordering::Acquire) {
            return Err(JobError::Cancelled);
        }
        let mut lease = None;
        let result = self.run_stream_script_with_ack(
            job_id,
            &self.context.program.join("scripts/install-aniflive.py"),
            args,
            Some(("等待显卡构建", "{\"continue\":true}".into())),
            |phase, done, total| {
                if phase == "等待显卡构建" {
                    let paused = self.pause_service()?;
                    self.remember_service_was_running(paused.was_running())?;
                    lease = Some(paused);
                }
                if phase.starts_with("启用") {
                    self.begin_non_cancellable_phase(job_id, phase, done, Some(total))?;
                } else {
                    self.set_phase(
                        job_id,
                        phase,
                        true,
                        done,
                        if total == 0 { None } else { Some(total) },
                    );
                }
                Ok(())
            },
        );
        let cleanup_args = vec![
            OsString::from("--root"),
            self.context.install.clone().into_os_string(),
            OsString::from("--cleanup-aliases"),
        ];
        let cleanup = self.run_stream_script(
            "",
            &self.context.program.join("scripts/install-aniflive.py"),
            &cleanup_args,
            |_, _, _| Ok(()),
        );
        if let Some(lease) = lease.as_mut() {
            let resumed = self
                .maybe_resume_service(lease)
                .map_err(|message| JobError::Failed {
                    message,
                    installed: vec![],
                })?;
            clear_resume_intent_if_resumed(&self.resume_pending, resumed);
        }
        cleanup.map_err(|error| JobError::Failed {
            message: format!("任务已退出，但临时盘符回收失败：{}", error.message),
            installed: vec![],
        })?;
        let result = result.map_err(|error| {
            if error.cancelled {
                JobError::Cancelled
            } else {
                JobError::Failed {
                    message: error.message,
                    installed: vec![],
                }
            }
        })?;
        if result["runtime_ready"] == true
            && voice_desktop_common::resources::native_runtime_status(&self.context)
                .is_ok_and(|row| row["can_start"] == true)
        {
            return Ok(vec!["tts-aniflive-runtime".into()]);
        }
        let model = result["model_id"]
            .as_str()
            .ok_or_else(|| JobError::Failed {
                message: "模型任务未返回登记身份。".into(),
                installed: vec![],
            })?;
        if !voice_desktop_common::resources::aniflive_model_ready(&self.context, model, false)
            .unwrap_or(false)
        {
            return Err(JobError::Failed {
                message: "模型登记与实际生成文件不一致。".into(),
                installed: vec![],
            });
        }
        Ok(vec![format!("tts-aniflive-{model}")])
    }

    fn run_import(
        self: &Arc<Self>,
        job_id: &str,
        resource_id: Option<String>,
    ) -> Result<Vec<String>, JobError> {
        let sources = voice_desktop_common::native_ui::choose_resource(resource_id.as_deref())
            .map_err(|message| JobError::Failed {
                message,
                installed: vec![],
            })?;
        if sources.is_empty() {
            return Err(JobError::Cancelled);
        }
        self.run_import_sources(job_id, resource_id, sources)
    }

    fn run_import_sources(
        self: &Arc<Self>,
        job_id: &str,
        resource_id: Option<String>,
        sources: Vec<PathBuf>,
    ) -> Result<Vec<String>, JobError> {
        if sources.is_empty() || sources.len() > 64 {
            return Err(JobError::Failed {
                message: "导入来源数量无效，请重新选择。".into(),
                installed: vec![],
            });
        }
        for source in &sources {
            let metadata = fs::symlink_metadata(source).map_err(|_| JobError::Failed {
                message: "所选导入来源已不存在或不可访问，请重新选择。".into(),
                installed: vec![],
            })?;
            if metadata.file_type().is_symlink() || !(metadata.is_file() || metadata.is_dir()) {
                return Err(JobError::Failed {
                    message: "导入来源必须是普通文件或文件夹，不能是链接或特殊文件。".into(),
                    installed: vec![],
                });
            }
        }
        let mut installed = vec![];
        let mut progress = |phase: &str, done: u64, total: u64| {
            if phase.starts_with("正在启用") || phase.starts_with("启用") {
                let _ = self.begin_non_cancellable_phase(job_id, phase, done, Some(total));
            } else {
                self.set_phase(
                    job_id,
                    phase,
                    true,
                    done,
                    if total == 0 { None } else { Some(total) },
                );
            }
        };
        if let Some(id) = resource_id.as_deref() {
            let dependencies = self
                .configure_resource_dependencies(
                    job_id,
                    id,
                    self.cancel_signal(job_id)?,
                    &mut progress,
                )
                .map_err(|message| {
                    self.update_job(job_id, |row| {
                        row.failure_resource_id =
                            Some(voice_desktop_common::runtime::CPU_RESOURCE.into())
                    });
                    JobError::Failed {
                        message,
                        installed: installed.clone(),
                    }
                })?;
            installed.extend(dependencies);
        }
        if resource_id
            .as_deref()
            .is_some_and(|id| id.starts_with("tts-aniflive-"))
        {
            let cancel = self.cancel_signal(job_id)?;
            let _shared = acquire_environment_lock(&self.tts_runtime, &cancel, &mut progress)
                .map_err(|message| JobError::Failed {
                    message,
                    installed: installed.clone(),
                })?;
            for source in sources {
                let specification = source.join("aniflive-input.json");
                if !specification.is_file() {
                    return Err(JobError::Failed{message:"导入文件夹需要 aniflive-input.json（模型编号、名称、配对权重、参考录音、准确转写及语言）。".into(),installed});
                }
                let args = vec![
                    OsString::from("--root"),
                    self.context.install.clone().into_os_string(),
                    OsString::from("--program"),
                    self.context.program.clone().into_os_string(),
                    OsString::from("--prepare-runtime"),
                    OsString::from("--input-spec"),
                    specification.into_os_string(),
                    OsString::from("--convert"),
                    OsString::from("--native-ack"),
                ];
                installed.extend(self.run_aniflive_task(job_id, &args, cancel.clone())?);
            }
            return Ok(installed);
        }
        for source in sources {
            let result = voice_desktop_common::resources::import_review_with_activation(
                &self.context,
                &source,
                resource_id.as_deref(),
                self.cancel_signal(job_id)?,
                &mut progress,
                &mut |items| self.review_import(job_id, items),
                &mut |stage, relative| self.activate_resource(job_id, stage, relative),
            )
            .map_err(|message| JobError::Failed {
                message,
                installed: installed.clone(),
            })?;
            installed.extend(result);
        }
        Ok(installed)
    }

    fn review_import(&self, job_id: &str, items: &[Value]) -> Result<(), String> {
        self.update_job(job_id, |snapshot| {
            snapshot.state = "waiting".into();
            snapshot.phase = "整包校验通过，请确认识别结果".into();
            snapshot.import_preview = Some(json!(items));
            snapshot.can_cancel = true;
        });
        loop {
            if self.cancelled(job_id) {
                return Err("已取消导入".into());
            }
            if let Some(accepted) = self
                .reviews
                .lock()
                .map_err(|e| e.to_string())?
                .remove(job_id)
            {
                self.update_job(job_id, |snapshot| {
                    snapshot.import_preview = None;
                    snapshot.state = "running".into();
                });
                return if accepted {
                    Ok(())
                } else {
                    Err("已取消导入".into())
                };
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }
    pub fn confirm_import(&self, job_id: &str, accepted: bool) -> Result<(), String> {
        if !self
            .jobs
            .lock()
            .map_err(|e| e.to_string())?
            .get(job_id)
            .is_some_and(|job| job.snapshot.import_preview.is_some())
        {
            return Err("此任务没有待确认导入内容".into());
        }
        if !accepted {
            return self.cancel(job_id);
        }
        self.reviews
            .lock()
            .map_err(|e| e.to_string())?
            .insert(job_id.into(), true);
        Ok(())
    }
    fn cli_args(&self, command: &str, args: &[OsString]) -> Vec<OsString> {
        let mut tail = vec![OsString::from(command)];
        tail.extend([
            OsString::from("--root"),
            self.context.install.clone().into_os_string(),
            OsString::from("--program"),
            self.context.program.clone().into_os_string(),
        ]);
        tail.extend(args.iter().cloned());
        tail
    }

    fn run_stream_cli<F>(
        &self,
        job_id: Option<&str>,
        script_name: &str,
        args: &[OsString],
        on_progress: F,
    ) -> Result<Value, ProcessError>
    where
        F: FnMut(&str, u64, u64) -> Result<(), String>,
    {
        let script = self.context.program.join("scripts").join(script_name);
        self.run_stream_script(job_id.unwrap_or(""), &script, args, on_progress)
    }

    fn run_stream_script<F>(
        &self,
        job_id: &str,
        script: &Path,
        args: &[OsString],
        on_progress: F,
    ) -> Result<Value, ProcessError>
    where
        F: FnMut(&str, u64, u64) -> Result<(), String>,
    {
        self.run_stream_script_with_ack(job_id, script, args, None, on_progress)
    }

    fn run_stream_script_with_ack<F>(
        &self,
        job_id: &str,
        script: &Path,
        args: &[OsString],
        native_ack: Option<(&str, String)>,
        mut on_progress: F,
    ) -> Result<Value, ProcessError>
    where
        F: FnMut(&str, u64, u64) -> Result<(), String>,
    {
        if !script.is_file() {
            return Err(ProcessError {
                message: "资源管理脚本缺失。".into(),
                installed: vec![],
                cancelled: false,
            });
        }
        let mut command = voice_desktop_common::process_host::python_command(&self.context.python);
        // -I（隔离模式）忽略 PYTHON* 环境变量；编码与缓存必须通过解释器参数明确指定。
        command
            .arg("-I")
            .arg("-B")
            .arg("-X")
            .arg("utf8")
            .arg("-X")
            .arg(format!(
                "pycache_prefix={}",
                self.context.install.join("cache/python").display()
            ))
            .arg(script)
            .args(args)
            .current_dir(&self.context.data)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .stdin(if native_ack.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            });
        command.env_clear();
        self.context.configure_command(&mut command);
        // Windows（操作系统）隔离环境没有 USERNAME（用户名）时，PyTorch 无法推导默认缓存目录。
        // 始终使用受管项目缓存，避免导出时回退到平台不存在的 pwd（用户数据库）模块。
        command.env(
            "TORCHINDUCTOR_CACHE_DIR",
            self.context.install.join("cache/torchinductor"),
        );
        #[cfg(windows)]
        {
            // SystemRoot（Windows 系统目录变量）供 Python 的 Windows socket/asyncio 模块加载系统网络提供程序。
            if let Some(system_root) = std::env::var_os("SystemRoot") {
                command.env("SystemRoot", system_root);
            }
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = ControlledProcess::spawn(&mut command).map_err(|_| ProcessError {
            message: "无法建立受管资源进程。".into(),
            installed: vec![],
            cancelled: false,
        })?;
        let stdout = child.child.stdout.take().ok_or_else(|| ProcessError {
            message: "资源管理器输出不可用。".into(),
            installed: vec![],
            cancelled: false,
        })?;
        let mut child_stdin = child.child.stdin.take();
        let child = Arc::new(Mutex::new(child));
        if !job_id.is_empty() {
            self.attach_child(job_id, child.clone());
            if self.cancelled(job_id) {
                if let Ok(mut process) = child.lock() {
                    process.kill();
                }
            }
        }
        let mut final_record = None;
        let read_result = self.consume_stdout(
            job_id,
            stdout,
            &mut on_progress,
            &mut final_record,
            native_ack.as_ref().map(|(phase, _)| *phase),
            native_ack.as_ref().map(|(_, value)| value.as_str()),
            &mut child_stdin,
        );
        if read_result.is_err() {
            if let Ok(mut process) = child.lock() {
                process.kill();
            }
        }
        let status = child
            .lock()
            .map_err(|_| ProcessError {
                message: "资源管理进程状态不可用。".into(),
                installed: vec![],
                cancelled: false,
            })?
            .wait()
            .map_err(|_| ProcessError {
                message: "资源管理进程无法正常结束。".into(),
                installed: vec![],
                cancelled: false,
            })?;
        if !job_id.is_empty() {
            self.detach_child(job_id);
        }
        if let Err(message) = read_result {
            let cancelled = !job_id.is_empty() && self.cancelled(job_id);
            return Err(ProcessError {
                message,
                installed: Vec::new(),
                cancelled,
            });
        }
        if !job_id.is_empty() && self.cancelled(job_id) {
            return Err(ProcessError {
                message: "资源任务已取消。".into(),
                installed: Vec::new(),
                cancelled: true,
            });
        }
        let Some(record) = final_record else {
            return Err(ProcessError {
                message: if status.success() {
                    "资源管理器没有返回结果。"
                } else {
                    "资源管理操作失败。"
                }
                .into(),
                installed: vec![],
                cancelled: false,
            });
        };
        response_result(record)
    }

    fn consume_stdout<F>(
        &self,
        job_id: &str,
        stdout: ChildStdout,
        on_progress: &mut F,
        final_record: &mut Option<Value>,
        ack_phase: Option<&str>,
        ack_json: Option<&str>,
        child_stdin: &mut Option<std::process::ChildStdin>,
    ) -> Result<(), String>
    where
        F: FnMut(&str, u64, u64) -> Result<(), String>,
    {
        for line in BufReader::new(stdout).lines() {
            let line = line.map_err(|_| "资源管理器输出读取失败。".to_string())?;
            let Ok(record) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            if let Some(phase) = record["phase"].as_str() {
                let done = record["completed_bytes"].as_u64().unwrap_or(0);
                let total = record["total_bytes"].as_u64().unwrap_or(0);
                on_progress(phase, done, total)?;
                if ack_phase == Some(phase) {
                    use std::io::Write;
                    let writer = child_stdin
                        .as_mut()
                        .ok_or_else(|| "DSH 安装器确认通道不可用。".to_string())?;
                    let ack = ack_json.ok_or_else(|| "DSH 安装器确认内容缺失。".to_string())?;
                    writer
                        .write_all(ack.as_bytes())
                        .and_then(|_| writer.write_all(b"\n"))
                        .and_then(|_| writer.flush())
                        .map_err(|_| "DSH 运行时切换确认失败。".to_string())?;
                    child_stdin.take();
                }
                continue;
            }
            if record.get("ok").is_some() {
                *final_record = Some(record);
            }
        }
        let _ = job_id;
        Ok(())
    }

    fn pause_service(&self) -> Result<ServiceLease<'_>, String> {
        let guard = self.activation.lock().map_err(|_| "资源激活锁不可用。")?;
        ServiceLease::pause(&self.service_control, guard)
    }

    fn remember_service_was_running(&self, was_running: bool) -> Result<(), String> {
        if was_running {
            *self
                .resume_pending
                .lock()
                .map_err(|_| "服务恢复意图不可用。")? = true;
        }
        Ok(())
    }

    fn pending_service_resume(&self) -> Result<bool, String> {
        Ok(*self
            .resume_pending
            .lock()
            .map_err(|_| "服务恢复意图不可用。")?)
    }

    fn recovery_environment_ready(&self) -> Result<bool, String> {
        // 恢复原来已运行的降级服务；其它未安装模型不能把组件更新卡在永久停机状态。
        // 候选在换入前已完整验证，实际模型加载和能力终态仍由新宿主核对。
        Ok(self.context.python.is_file())
    }

    fn maybe_resume_service(&self, lease: &mut ServiceLease<'_>) -> Result<bool, String> {
        resume_service_if_ready(
            lease,
            &self.resume_pending,
            &self.accepting_jobs,
            &self.batch_active,
            || self.recovery_environment_ready(),
        )
    }

    fn rollback_failed_activation(
        &self,
        lease: &mut ServiceLease<'_>,
        transaction: &mut voice_desktop_common::resources::PendingActivation,
        reason: &str,
    ) -> String {
        if let Err(error) = lease.stop_partial() {
            lease.keep_stopped();
            return format!("{reason}；无法确认新服务已停止，资源回滚留待下次启动恢复：{error}");
        }
        if let Err(error) =
            voice_desktop_common::resources::rollback_activation(&self.context, transaction)
        {
            lease.keep_stopped();
            return format!("{reason}；恢复原组件失败：{error}");
        }
        match self.maybe_resume_service(lease) {
            Ok(resumed) => {
                // prior-running intent（更新前正在运行的服务恢复意图）只有在旧状态恢复且服务确实重新启动后才能清除；
                // 若后续 transaction commit（事务提交）失败，仍需保留它以重新启动回滚后的旧服务。
                clear_resume_intent_if_resumed(&self.resume_pending, resumed);
                reason.to_owned()
            }
            Err(error) => format!("{reason}；原组件已恢复，但语音服务未恢复：{error}"),
        }
    }

    fn settle_before_activation(&self, lease: &mut ServiceLease<'_>, reason: &str) -> String {
        match self.maybe_resume_service(lease) {
            Ok(resumed) => {
                clear_resume_intent_if_resumed(&self.resume_pending, resumed);
                reason.to_owned()
            }
            Err(error) => format!("{reason}；语音服务未恢复：{error}"),
        }
    }

    fn activate_resource(&self, job_id: &str, stage: &Path, relative: &str) -> Result<(), String> {
        let mut lease = self.pause_service()?;
        if let Err(error) = self.remember_service_was_running(lease.was_running()) {
            lease.keep_stopped();
            return Err(error);
        }
        let cancel = match self.cancel_signal(job_id) {
            Ok(value) => value,
            Err(_) => return Err(self.settle_before_activation(&mut lease, "资源任务已丢失")),
        };
        if let Err(error) = voice_desktop_common::resources::check_cancel(&cancel) {
            return Err(self.settle_before_activation(&mut lease, &error));
        }
        let mut transaction =
            match voice_desktop_common::resources::begin_activation(&self.context, stage, relative)
            {
                Ok(value) => value,
                Err(error) => return Err(self.settle_before_activation(&mut lease, &error)),
            };
        if relative == "runtimes/dsh" {
            if let Err(error) =
                voice_desktop_common::resources::validate_dsh_activation(&self.context)
            {
                return Err(self.rollback_failed_activation(&mut lease, &mut transaction, &error));
            }
        }
        let pending = match self.pending_service_resume() {
            Ok(value) => value,
            Err(error) => {
                return Err(self.rollback_failed_activation(&mut lease, &mut transaction, &error));
            }
        };
        let resumed = if pending {
            match self.maybe_resume_service(&mut lease) {
                Ok(value) => value,
                Err(error) => {
                    return Err(self.rollback_failed_activation(
                        &mut lease,
                        &mut transaction,
                        &error,
                    ));
                }
            }
        } else {
            lease.keep_stopped();
            false
        };
        match voice_desktop_common::resources::commit_activation(&self.context, &mut transaction) {
            Ok(()) => {
                clear_resume_intent_if_resumed(&self.resume_pending, resumed);
                self.publish_installed(job_id, relative);
                Ok(())
            }
            Err(error) if transaction.is_committed() => {
                lease.keep_stopped();
                clear_resume_intent_if_resumed(&self.resume_pending, resumed);
                Err(format!("资源已提交并启用，但旧备份清理失败：{error}"))
            }
            Err(error) => {
                Err(self.rollback_failed_activation(&mut lease, &mut transaction, &error))
            }
        }
    }

    fn publish_installed(&self, job_id: &str, relative: &str) {
        let resource_id = if relative == "runtimes/python" {
            Some("voice-server".to_string())
        } else if relative == "runtimes/dsh" {
            Some("dsh".to_string())
        } else {
            voice_desktop_common::resources::specs(&self.context)
                .ok()
                .into_iter()
                .flatten()
                .find(|item| {
                    item["destination"]
                        .as_str()
                        .is_some_and(|path| relative == format!("models/{path}"))
                })
                .and_then(|item| item["resource_id"].as_str().map(str::to_owned))
        };
        if let Some(id) = resource_id {
            let mut resolved = vec![id.clone()];
            if id == "voice-server" {
                resolved.push(voice_desktop_common::runtime::CPU_RESOURCE.into());
                if voice_desktop_common::runtime::status(&self.context).is_ok_and(|state| {
                    state["components"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .any(|row| {
                            row["resource_id"] == voice_desktop_common::runtime::GPU_RESOURCE
                                && row["can_start"] == true
                        })
                }) {
                    resolved.push(voice_desktop_common::runtime::GPU_RESOURCE.into());
                }
            }
            if let Err(error) =
                voice_desktop_common::component_maintenance::resolved(&self.context, &resolved)
            {
                eprintln!("组件校验交接收尾失败：{error}");
            }
            if let Ok(mut failures) = self.validation_failures.lock() {
                failures.remove(&id);
            }
            self.update_job(job_id, |snapshot| {
                if !snapshot.installed_resources.contains(&id) {
                    snapshot.installed_resources.push(id);
                }
            });
        }
    }

    fn prepare_environment(
        &self,
        job_id: &str,
        group: &str,
        cancel: Arc<AtomicBool>,
        progress: &mut voice_desktop_common::resources::Progress<'_>,
    ) -> Result<Vec<String>, String> {
        voice_desktop_common::resources::check_cancel(&cancel)?;
        // 共享环境统一由运行依赖入口按登记和已知故障判断复用，不为进程内缓存遍历文件树。
        let repairing = self
            .jobs
            .lock()
            .map_err(|_| "资源任务状态不可用")?
            .get(job_id)
            .is_some_and(|job| {
                job.snapshot.attempt > 1
                    && job
                        .snapshot
                        .recovery
                        .as_ref()
                        .is_some_and(|state| state["state"] == "repairing")
            });
        let outcome = (|| {
            let result = if repairing {
                voice_desktop_common::runtime::repair_with_activation(
                    &self.context,
                    group,
                    cancel.clone(),
                    progress,
                    &mut |stage, relative| self.activate_resource(job_id, stage, relative),
                )
            } else {
                voice_desktop_common::runtime::configure_with_activation(
                    &self.context,
                    group,
                    cancel.clone(),
                    progress,
                    &mut |stage, relative| self.activate_resource(job_id, stage, relative),
                )
            }?;
            voice_desktop_common::compatibility::finish_deferred_settings(&self.context, &cancel)?;
            voice_desktop_common::resources::check_cancel(&cancel)?;
            Ok::<Vec<String>, String>(result)
        })();
        let report_path = self
            .context
            .install
            .join("cache/reports/runtime-update-latest.json");
        if let Ok(mut report) = voice_desktop_common::resources::load(&report_path) {
            report["job_id"] = json!(job_id);
            report["fields"]["job_id"] = json!("创建本轮运行环境操作的资源任务编号");
            report["fields"]["phase"] = json!("任务最后到达的阶段");
            if let Some(row) = self
                .jobs
                .lock()
                .ok()
                .and_then(|jobs| jobs.get(job_id).map(|job| job.snapshot.clone()))
            {
                report["phase"] = json!(row.phase);
            }
            if let Err(error) = &outcome {
                report["state"] = json!(if cancel.load(Ordering::Acquire) {
                    "cancelled"
                } else {
                    "failed"
                });
                report["error"] = json!(error);
            }
            if let Err(error) = voice_desktop_common::workspace::atomic_json(&report_path, &report)
            {
                eprintln!("运行环境任务报告写入失败：{error}");
            }
        }
        outcome
    }

    fn start_job<F>(
        self: &Arc<Self>,
        job_id: String,
        resource_id: String,
        phase: String,
        can_cancel: bool,
        worker: F,
    ) -> Result<(), String>
    where
        F: FnOnce(Arc<ResourceManager>, String) -> Result<Vec<String>, JobError> + Send + 'static,
    {
        self.start_child_job(job_id, resource_id, phase, can_cancel, None, worker)
    }

    // start_child_job（任务准入）：在同一状态锁内预留批量任务，并只放行所属子任务。
    fn start_child_job<F>(
        self: &Arc<Self>,
        job_id: String,
        resource_id: String,
        phase: String,
        can_cancel: bool,
        parent: Option<String>,
        worker: F,
    ) -> Result<(), String>
    where
        F: FnOnce(Arc<ResourceManager>, String) -> Result<Vec<String>, JobError> + Send + 'static,
    {
        if self.context.install.join("tmp/storage-model-cleanup.json").exists() {
            return Err("请先在清理页面完成上次模型维护。".into());
        }
        if !job_id.starts_with("reset-")
            && voice_desktop_common::component_reset::pending(&self.context)?.is_some()
        {
            return Err("已确认的组件重置尚未完成，请先继续重置。".into());
        }
        if !job_id.starts_with("reset-")
            && self
                .context
                .install
                .join("tmp/native-install.json")
                .exists()
        {
            if self.has_active_jobs() {
                return Err("组件正在完成启用，请稍候再试。".into());
            }
            if let Err(error) = self.recover_pending_transaction() {
                let _ = self.record_recovery_failure(&error);
                return Err(error);
            }
        }
        {
            let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
            if self.resetting.load(Ordering::Acquire) && !job_id.starts_with("reset-") {
                return Err("组件正在重置，请等待完成。".into());
            }
            if !self.accepting_jobs.load(Ordering::Acquire) {
                return Err("工作区正在迁移、更新或退出，不能启动新的资源任务。".into());
            }
            if jobs
                .get(&job_id)
                .is_some_and(|job| !terminal(&job.snapshot.state))
            {
                if jobs[&job_id].snapshot.parent_job_id != parent {
                    return Err("此组件由其他资源任务处理中，请等待完成。".into());
                }
                return Ok(());
            }
            validate_batch_admission(&jobs, &resource_id, parent.as_deref())?;
            if jobs.values().any(|job| {
                job.snapshot.resource_id == resource_id && !terminal(&job.snapshot.state)
            }) {
                return Err("此组件已有下载或导入任务，请等待该任务完成。".into());
            }
            let created_at = unix_millis().max(
                jobs.get(&job_id)
                    .map(|job| job.snapshot.created_at.saturating_add(1))
                    .unwrap_or(0),
            );
            let snapshot = JobSnapshot {
                job_id: job_id.clone(),
                resource_id,
                state: "queued".into(),
                phase,
                completed_bytes: 0,
                total_bytes: None,
                download_completed_bytes: None,
                download_total_bytes: None,
                verification_completed_bytes: None,
                verification_total_bytes: None,
                error: None,
                can_cancel,
                installed_resources: vec![],
                import_preview: None,
                created_at,
                revision: 1,
                failure_resource_id: None,
                first_error: None,
                progress_kind: "waiting".into(),
                attempt: 1,
                parent_job_id: parent,
                recovery: None,
            };
            // previous_run（旧任务历史）：同一固定任务重试前保留终态及首轮错误。
            let historical = jobs
                .get(&job_id)
                .and_then(|previous| terminal_job_history_for_retry(&job_id, &previous.snapshot));
            if let Some(historical) = historical {
                jobs.insert(
                    historical.job_id.clone(),
                    JobControl {
                        snapshot: historical,
                        cancel: Arc::new(AtomicBool::new(false)),
                        child: None,
                        last_event: Instant::now(),
                        last_persist: Instant::now(),
                    },
                );
            }
            jobs.insert(
                job_id.clone(),
                JobControl {
                    snapshot: snapshot.clone(),
                    cancel: Arc::new(AtomicBool::new(false)),
                    child: None,
                    last_event: Instant::now(),
                    last_persist: Instant::now(),
                },
            );
            if let Err(error) = self.persist_locked(&jobs) {
                jobs.remove(&job_id);
                return Err(error);
            }
            if snapshot.resource_id == "required-batch" {
                self.batch_active.store(true, Ordering::Release);
            }
            let _ = self.app.emit(PROGRESS_EVENT, event_value(&snapshot));
        }
        let manager = Arc::clone(self);
        let thread_id = job_id.clone();
        thread::Builder::new()
            .name(format!("resource-{thread_id}"))
            .spawn(move || {
                manager.set_phase(
                    &thread_id,
                    "正在运行资源任务",
                    !thread_id.starts_with("reset-"),
                    0,
                    None,
                );
                let outcome = if manager.cancelled(&thread_id) {
                    Err(JobError::Cancelled)
                } else {
                    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        worker(Arc::clone(&manager), thread_id.clone())
                    }))
                    .unwrap_or_else(|_| {
                        Err(JobError::Failed {
                            message: "资源任务异常退出；未输出敏感诊断信息。".into(),
                            installed: vec![],
                        })
                    })
                };
                match outcome {
                    Ok(installed) => manager.finish_job(&thread_id, Ok(installed)),
                    Err(JobError::Failed { installed, .. })
                        if installed.is_empty() && manager.cancelled(&thread_id) =>
                    {
                        manager.finish_job(&thread_id, Err(JobError::Cancelled))
                    }
                    Err(error) => manager.finish_job(&thread_id, Err(error)),
                }
            })
            .map_err(|_| {
                self.finish_job(
                    &job_id,
                    Err(JobError::Failed {
                        message: "无法建立资源任务线程。".into(),
                        installed: vec![],
                    }),
                );
                "无法建立资源任务线程。".to_string()
            })?;
        Ok(())
    }

    fn finish_job(&self, job_id: &str, result: Result<Vec<String>, JobError>) {
        let system_feature_job = is_system_feature_job(job_id);
        if job_id.starts_with("reset-") {
            self.resetting.store(false, Ordering::Release);
        }
        let maintenance_completed = job_id == "repair-components" && result.is_ok();
        if job_id == "dl-required" || job_id == "repair-components" {
            self.batch_active.store(false, Ordering::Release);
        }
        let (state, phase, error, installed) = match result {
            Ok(installed) => (
                "complete",
                if job_id.starts_with("reset-uninstall-") {
                    "卸载完成，用户资料已保留"
                } else if job_id.starts_with("reset-") {
                    "重置完成，等待下载或导入"
                } else {
                    "资源处理完成"
                },
                None,
                installed,
            ),
            Err(JobError::Cancelled) => (
                "cancelled",
                if system_feature_job {
                    "Windows 管理员授权已取消；可重新尝试 OpenSSH Server 管理任务"
                } else {
                    "任务已取消；临时下载可供重试"
                },
                None,
                vec![],
            ),
            Err(JobError::Failed { message, installed }) => {
                ("failed", "资源处理失败", Some(message), installed)
            }
        };
        if let Ok(mut failures) = self.validation_failures.lock() {
            for id in &installed {
                failures.remove(id);
            }
        }
        if !installed.is_empty() {
            if let Err(error) =
                voice_desktop_common::component_maintenance::resolved(&self.context, &installed)
            {
                eprintln!("组件校验交接收尾失败：{error}");
            }
        }
        if !system_feature_job {
            if let Some(id) = self.jobs.lock().ok().and_then(|jobs| {
                jobs.get(job_id).map(|job| {
                    job.snapshot
                        .failure_resource_id
                        .clone()
                        .unwrap_or_else(|| job.snapshot.resource_id.clone())
                })
            }) {
                if let Some(reason) = error.as_deref() {
                    if let Err(e) =
                        voice_desktop_common::component_reset::fault(&self.context, &id, reason)
                    {
                        eprintln!("重置资格保存失败：{e}");
                    }
                }
            }
        }
        if !installed.is_empty() {
            if let Err(e) = voice_desktop_common::component_reset::clear(&self.context, &installed)
            {
                eprintln!("重置资格清除失败：{e}");
            }
        }
        let operation_error = error.clone();
        let recovered_resources = installed.clone();
        let guidance = if system_feature_job {
            None
        } else {
            error.as_deref().map(|message| {
                voice_desktop_common::component_recovery::guidance(
                    voice_desktop_common::component_recovery::classify(message),
                    false,
                )
            })
        };
        self.update_job(job_id, |snapshot| {
            snapshot.state = state.into();
            snapshot.phase = phase.into();
            if snapshot.first_error.is_none() {
                snapshot.first_error = error.clone();
            }
            snapshot.error = error;
            snapshot.can_cancel = false;
            for id in installed {
                if !snapshot.installed_resources.contains(&id) {
                    snapshot.installed_resources.push(id);
                }
            }
            snapshot.import_preview = None;
            snapshot.recovery = guidance;
        });
        if !recovered_resources.is_empty() {
            let resolved = self
                .jobs
                .lock()
                .ok()
                .map(|jobs| {
                    jobs.iter()
                        .filter(|(id, job)| {
                            id.as_str() != job_id
                                && job.snapshot.state == "failed"
                                && recovered_resources.contains(&job.snapshot.resource_id)
                        })
                        .map(|(id, _)| id.clone())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            for id in resolved {
                self.update_job(&id,|snapshot|snapshot.recovery=Some(json!({"event":"recovered","state":"resolved","next_action":"none","message":"该组件已由后续任务恢复。","fields":{"event":"后续任务恢复完成","state":"故障已解决","next_action":"不需要继续处理","message":"用户可见恢复结果"}})));
            }
        }
        // 成功、失败及取消均重读实际组件；任务完成本身不代表兼容检查通过。
        if !system_feature_job {
            if let Err(error) = self.inventory() {
                eprintln!("兼容性组件重新盘点失败：{error}");
            }
            self.finish_recovery(operation_error.as_deref());
        }
        // 初次交接为等待组件任务而暂缓启动；成功后接回原有轻量启动，不把用户留在管理页。
        if maintenance_completed
            && !self.has_active_jobs()
            && self.accepting_jobs.load(Ordering::Acquire)
            && !voice_desktop_common::component_maintenance::pending_repairs(&self.context)
                .unwrap_or(true)
        {
            if let Some(state) = self.app.try_state::<Arc<super::Desktop>>() {
                if !state.stopping.load(Ordering::Acquire)
                    && !state.startup_skipped.load(Ordering::Acquire)
                    && !state.service_requested.swap(true, Ordering::AcqRel)
                {
                    if let Err(error) =
                        super::initialization::auto_start(self.app.clone(), state.inner().clone())
                    {
                        *state.error.lock().unwrap() = Some(error);
                    }
                }
            }
        }
        self.cleanup_remote_import_job(job_id);
    }

    fn finish_recovery(&self, error: Option<&str>) {
        if self.pending_service_resume().unwrap_or(true) {
            return;
        }
        if let Some(state) = self.app.try_state::<Arc<super::Desktop>>() {
            let mut recovery = state.recovery.lock().unwrap();
            let snapshot = recovery.snapshot();
            if snapshot["operation"]["state"] != "running" || snapshot["suspended"] == true {
                return;
            }
            if recovery
                .result
                .as_ref()
                .is_none_or(|(epoch, _)| *epoch != state.service_epoch.load(Ordering::Acquire))
            {
                return;
            }
            let error = error.or_else(|| {
                recovery
                    .result
                    .as_ref()
                    .filter(|(_, value)| value["core_available"] != true)
                    .map(|_| "组件已启用，部分语音能力仍不可用，请继续补齐或恢复组件。")
            });
            recovery.finish(error);
            drop(recovery);
            state.publish_recovery(&self.app);
        }
    }

    fn set_phase(
        &self,
        job_id: &str,
        phase: &str,
        can_cancel: bool,
        done: u64,
        total: Option<u64>,
    ) {
        self.update_job(job_id, |snapshot| {
            if snapshot.state == "cancelling" {
                return;
            }
            if !terminal(&snapshot.state) {
                snapshot.state = "running".into();
            }
            snapshot.phase = phase.into();
            snapshot.can_cancel = can_cancel;
            snapshot.progress_kind = progress_kind(phase).into();
            if phase.starts_with(voice_desktop_common::download::BATCH_DOWNLOAD_PROGRESS_PREFIX) {
                snapshot.download_completed_bytes = Some(done);
                snapshot.download_total_bytes = total;
                if snapshot.verification_total_bytes.is_none() {
                    snapshot.verification_completed_bytes = Some(0);
                    snapshot.verification_total_bytes = total;
                }
            } else if phase
                .starts_with(voice_desktop_common::download::BATCH_VERIFY_PROGRESS_PREFIX)
            {
                snapshot.verification_completed_bytes = Some(done);
                snapshot.verification_total_bytes = total;
                if snapshot.download_total_bytes.is_none() {
                    snapshot.download_completed_bytes = Some(0);
                    snapshot.download_total_bytes = total;
                }
            } else {
                snapshot.download_completed_bytes = None;
                snapshot.download_total_bytes = None;
                snapshot.verification_completed_bytes = None;
                snapshot.verification_total_bytes = None;
                snapshot.completed_bytes = done;
                snapshot.total_bytes = total;
            }
        });
    }

    fn begin_non_cancellable_phase(
        &self,
        job_id: &str,
        phase: &str,
        done: u64,
        total: Option<u64>,
    ) -> Result<(), String> {
        let event = {
            let mut jobs = self
                .jobs
                .lock()
                .map_err(|_| "资源任务状态不可用。".to_string())?;
            let job = jobs
                .get_mut(job_id)
                .ok_or_else(|| "找不到该资源任务。".to_string())?;
            if job.cancel.load(Ordering::Acquire) || job.snapshot.state == "cancelling" {
                return Err("资源任务已取消。".into());
            }
            job.snapshot.state = "running".into();
            job.snapshot.phase = phase.into();
            job.snapshot.can_cancel = false;
            job.snapshot.progress_kind = "install".into();
            job.snapshot.completed_bytes = done;
            job.snapshot.total_bytes = total;
            job.snapshot.download_completed_bytes = None;
            job.snapshot.download_total_bytes = None;
            job.snapshot.verification_completed_bytes = None;
            job.snapshot.verification_total_bytes = None;
            job.snapshot.revision = job.snapshot.revision.saturating_add(1);
            job.last_event = Instant::now();
            job.last_persist = Instant::now();
            let event = event_value(&job.snapshot);
            self.persist_locked(&jobs)?;
            event
        };
        let _ = self.app.emit(PROGRESS_EVENT, event);
        Ok(())
    }

    fn update_job<F>(&self, job_id: &str, update: F)
    where
        F: FnOnce(&mut JobSnapshot),
    {
        let event = {
            let Ok(mut jobs) = self.jobs.lock() else {
                return;
            };
            let Some(job) = jobs.get_mut(job_id) else {
                return;
            };
            let previous_state = job.snapshot.state.clone();
            let previous_phase = job.snapshot.phase.clone();
            let previous_installed = job.snapshot.installed_resources.clone();
            update(&mut job.snapshot);
            job.snapshot.revision = job.snapshot.revision.saturating_add(1);
            let boundary = previous_state != job.snapshot.state
                || previous_phase.split('（').next() != job.snapshot.phase.split('（').next()
                || previous_installed != job.snapshot.installed_resources;
            let publish = boundary
                || terminal(&job.snapshot.state)
                || job.last_event.elapsed() >= Duration::from_millis(100);
            let persist = boundary
                || terminal(&job.snapshot.state)
                || job.last_persist.elapsed() >= Duration::from_secs(1);
            let value = if publish {
                job.last_event = Instant::now();
                Some(event_value(&job.snapshot))
            } else {
                None
            };
            if persist {
                job.last_persist = Instant::now();
                let _ = self.persist_locked(&jobs);
            }
            value
        };
        if let Some(event) = event {
            let _ = self.app.emit(PROGRESS_EVENT, event);
        }
        self.jobs_changed.notify_all();
    }

    fn persist_jobs(&self) -> Result<(), String> {
        let jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
        self.persist_locked(&jobs)
    }

    fn persist_locked(&self, jobs: &HashMap<String, JobControl>) -> Result<(), String> {
        let mut snapshots = jobs
            .values()
            .map(|item| item.snapshot.clone())
            .collect::<Vec<_>>();
        snapshots.sort_by_key(|item| item.created_at);
        if snapshots.len() > MAX_PERSISTED_JOBS {
            snapshots.drain(0..snapshots.len() - MAX_PERSISTED_JOBS);
        }
        let value =
            json!({ "schema": 1, "jobs": snapshots.iter().map(event_value).collect::<Vec<_>>() });
        voice_desktop_common::workspace::atomic_json(&self.jobs_path, &value)
    }

    fn load_jobs(path: &Path) -> Result<HashMap<String, JobControl>, String> {
        if !path.exists() {
            return Ok(HashMap::new());
        }
        let bytes = fs::read(path).map_err(|_| "资源任务记录无法读取。")?;
        let root: Value = serde_json::from_slice(&bytes).map_err(|_| "资源任务记录格式无效。")?;
        let records = root["jobs"].as_array().ok_or("资源任务记录格式无效。")?;
        let mut jobs = HashMap::new();
        for value in records {
            let (Some(job_id), Some(resource_id), Some(mut state), Some(mut phase)) = (
                value["job_id"].as_str(),
                value["resource_id"].as_str(),
                value["state"].as_str().map(str::to_owned),
                value["phase"].as_str().map(str::to_owned),
            ) else {
                continue;
            };
            let system_feature_job = is_system_feature_job(job_id);
            let mut error = value["error"].as_str().map(str::to_owned);
            // 系统功能历史记录可能误带组件恢复建议；保留原始错误，移除错误的下载入口。
            let mut recovery = if system_feature_job {
                None
            } else {
                value
                    .get("recovery")
                    .filter(|value| value.is_object())
                    .cloned()
            };
            let can_cancel = false;
            if !terminal(&state) {
                state = "failed".into();
                if system_feature_job {
                    phase = "应用退出时系统功能任务中断，请前往设备与角色重新检查".into();
                    error = Some("上次系统功能任务中断；请在设备与角色中检查状态后重新操作。".into());
                } else {
                    phase = "应用退出时任务中断，可使用相同下载任务重试".into();
                    error = Some("上次运行期间任务中断；固定下载任务可重试。".into());
                    recovery = Some(voice_desktop_common::component_recovery::interrupted());
                }
            }
            if !system_feature_job && recovery.is_none() && state == "failed" {
                if let Some(message) = error.as_deref() {
                    recovery = Some(voice_desktop_common::component_recovery::guidance(
                        voice_desktop_common::component_recovery::classify(message),
                        false,
                    ));
                }
            }
            let installed_resources = value["installed_resources"]
                .as_array()
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default();
            let snapshot = JobSnapshot {
                job_id: job_id.into(),
                resource_id: resource_id.into(),
                state,
                phase,
                completed_bytes: value["completed_bytes"].as_u64().unwrap_or(0),
                total_bytes: value["total_bytes"].as_u64(),
                download_completed_bytes: value["download_completed_bytes"].as_u64(),
                download_total_bytes: value["download_total_bytes"].as_u64(),
                verification_completed_bytes: value["verification_completed_bytes"].as_u64(),
                verification_total_bytes: value["verification_total_bytes"].as_u64(),
                failure_resource_id: value["failure_resource_id"].as_str().map(str::to_owned),
                first_error: value["first_error"].as_str().map(str::to_owned),
                error,
                can_cancel,
                installed_resources,
                import_preview: None,
                created_at: value["created_at"].as_u64().unwrap_or(0),
                revision: value["revision"].as_u64().unwrap_or(0).saturating_add(1),
                progress_kind: value["progress_kind"].as_str().unwrap_or("waiting").into(),
                attempt: value["attempt"].as_u64().unwrap_or(1) as u32,
                parent_job_id: value["parent_job_id"].as_str().map(str::to_owned),
                recovery,
            };
            jobs.insert(
                snapshot.job_id.clone(),
                JobControl {
                    snapshot,
                    cancel: Arc::new(AtomicBool::new(false)),
                    child: None,
                    last_event: Instant::now(),
                    last_persist: Instant::now(),
                },
            );
        }
        Ok(jobs)
    }

    fn attach_child(&self, job_id: &str, child: Arc<Mutex<ControlledProcess>>) {
        if let Ok(mut jobs) = self.jobs.lock() {
            if let Some(job) = jobs.get_mut(job_id) {
                job.child = Some(child);
            }
        }
    }

    fn detach_child(&self, job_id: &str) {
        if let Ok(mut jobs) = self.jobs.lock() {
            if let Some(job) = jobs.get_mut(job_id) {
                job.child = None;
            }
        }
    }

    fn cancel_signal(&self, job_id: &str) -> Result<Arc<AtomicBool>, JobError> {
        let jobs = self.jobs.lock().map_err(|_| JobError::Failed {
            message: "资源任务状态不可用。".into(),
            installed: vec![],
        })?;
        jobs.get(job_id)
            .map(|job| job.cancel.clone())
            .ok_or_else(|| JobError::Failed {
                message: "找不到资源任务。".into(),
                installed: vec![],
            })
    }

    fn cancelled(&self, job_id: &str) -> bool {
        self.jobs
            .lock()
            .ok()
            .and_then(|jobs| {
                jobs.get(job_id)
                    .map(|job| job.cancel.load(Ordering::Acquire))
            })
            .unwrap_or(false)
    }

    fn configure_resource_dependencies(
        &self,
        job_id: &str,
        resource_id: &str,
        cancel: Arc<AtomicBool>,
        progress: &mut voice_desktop_common::resources::Progress<'_>,
    ) -> Result<Vec<String>, String> {
        let spec = voice_desktop_common::resources::spec(&self.context, resource_id)?;
        if spec["preparation"]
            .as_str()
            .is_some_and(|value| value.starts_with("aniflive_"))
            && !self.context.python.is_file()
        {
            let _environment = acquire_environment_lock(&self.environment, &cancel, progress)?;
            return self.prepare_environment(job_id, "cpu", cancel, progress);
        }
        let Some(group) = spec["dependency_group"].as_str() else {
            return Ok(vec![]);
        };
        let _environment = acquire_environment_lock(&self.environment, &cancel, progress)?;
        let activation_job = job_id.to_owned();
        self.prepare_environment(&activation_job, group, cancel, progress)
    }
}

fn clear_resume_intent_if_resumed(resume_pending: &Mutex<bool>, resumed: bool) {
    if resumed {
        if let Ok(mut pending) = resume_pending.lock() {
            *pending = false;
        }
    }
}

// resume_service_if_ready（维护后恢复）：暂停准入时保留意图，恢复后只启动原先运行的服务。
fn resume_service_if_ready(
    lease: &mut ServiceLease<'_>,
    pending: &Mutex<bool>,
    accepting: &AtomicBool,
    batch: &AtomicBool,
    ready: impl FnOnce() -> Result<bool, String>,
) -> Result<bool, String> {
    if !accepting.load(Ordering::Acquire) || batch.load(Ordering::Acquire) {
        lease.keep_stopped();
        return Ok(false);
    }
    if !*pending.lock().map_err(|_| "服务恢复意图不可用。")? {
        lease.keep_stopped();
        return Ok(false);
    }
    match ready() {
        Ok(false) => {
            lease.keep_stopped();
            return Ok(false);
        }
        Err(error) => {
            lease.keep_stopped();
            return Err(format!("无法确认运行环境可恢复，语音服务保持停止：{error}"));
        }
        Ok(true) => {}
    }
    lease.resume_service()?;
    Ok(true)
}

fn acquire_environment_lock<'a>(
    environment: &'a Mutex<()>,
    cancel: &AtomicBool,
    progress: &mut voice_desktop_common::resources::Progress<'_>,
) -> Result<MutexGuard<'a, ()>, String> {
    let mut reported_wait = false;
    loop {
        voice_desktop_common::resources::check_cancel(cancel)?;
        match environment.try_lock() {
            Ok(guard) => return Ok(guard),
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return Err("语音环境配置锁不可用。".into());
            }
            Err(std::sync::TryLockError::WouldBlock) => {
                if !reported_wait {
                    progress("等待语音运行环境专用更新锁", 0, 0);
                    reported_wait = true;
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
    }
}

struct ServiceLease<'a> {
    control: &'a ServiceControl, // control（服务控制回调）：启用结束后只恢复原先运行的服务。
    _guard: MutexGuard<'a, ()>,
    active: bool,
    resume: bool, // resume（恢复标识）：只恢复此前实际运行的服务。
}

impl<'a> ServiceLease<'a> {
    fn pause(control: &'a ServiceControl, guard: MutexGuard<'a, ()>) -> Result<Self, String> {
        let resume = control(false).map_err(|_| "无法暂停语音服务以安全激活资源。")?;
        Ok(Self {
            control,
            _guard: guard,
            active: true,
            resume,
        })
    }

    fn was_running(&self) -> bool {
        self.resume
    }

    fn resume_service(&mut self) -> Result<(), String> {
        match (self.control)(true) {
            Ok(true) => {
                self.active = false;
                Ok(())
            }
            Ok(false) => {
                self.active = false;
                Err("语音服务启动命令已返回，但服务入口未报告就绪。".into())
            }
            Err(error) => {
                self.active = false;
                Err(format!("语音服务未能恢复：{error}"))
            }
        }
    }

    fn stop_partial(&mut self) -> Result<(), String> {
        (self.control)(false)
            .map_err(|error| format!("无法停止恢复失败后可能残留的新服务：{error}"))?;
        self.active = false;
        Ok(())
    }

    fn keep_stopped(&mut self) {
        self.active = false;
    }

    fn finish(mut self) -> Result<(), String> {
        if self.resume {
            self.resume_service()
        } else {
            self.keep_stopped();
            Ok(())
        }
    }
}

impl Drop for ServiceLease<'_> {
    fn drop(&mut self) {
        if self.active && self.resume {
            let _ = (self.control)(true);
        }
    }
}

fn response_result(record: Value) -> Result<Value, ProcessError> {
    if record["ok"].as_bool() == Some(true) {
        return Ok(record.get("result").cloned().unwrap_or(Value::Null));
    }
    let error = &record["error"];
    let installed = error["installed_resource_ids"]
        .as_array()
        .or_else(|| error["details"]["installed_resource_ids"].as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    let message = error["message"]
        .as_str()
        .filter(|value| value.len() <= 512)
        .unwrap_or("资源操作失败；敏感诊断信息已省略。")
        .to_owned();
    Err(ProcessError {
        message,
        installed,
        cancelled: false,
    })
}

fn event_value(snapshot: &JobSnapshot) -> Value {
    let mut value = json!({
        "job_id": snapshot.job_id,
        "resource_id": snapshot.resource_id,
        "state": snapshot.state,
        "phase": snapshot.phase,
        "progress_kind": snapshot.progress_kind,
        "attempt": snapshot.attempt,
        "recovery":snapshot.recovery,
        "parent_job_id": snapshot.parent_job_id,
        "completed_bytes": snapshot.completed_bytes,
        "total_bytes": snapshot.total_bytes,
        "download_completed_bytes":snapshot.download_completed_bytes,
        "download_total_bytes":snapshot.download_total_bytes,
        "verification_completed_bytes":snapshot.verification_completed_bytes,
        "verification_total_bytes":snapshot.verification_total_bytes,
        "error": snapshot.error,
        "can_cancel": snapshot.can_cancel,
        "installed_resources": snapshot.installed_resources,
        "created_at": snapshot.created_at,
        "revision": snapshot.revision,
        "import_preview": snapshot.import_preview,
        "fields":{"progress_kind":"当前进度类型，下载与校验分别计数","attempt":"当前自动恢复轮次","recovery":"故障类别、自动恢复结果和明确下一步","parent_job_id":"所属一键任务编号","created_at":"本次任务创建时间，Unix 毫秒","revision":"状态修订号，拒绝迟到事件","job_id":"任务编号","resource_id":"组件编号","state":"任务状态","phase":"当前阶段","completed_bytes":"其他阶段已处理字节数","total_bytes":"其他阶段总字节数","download_completed_bytes":"资源下载已完成字节数","download_total_bytes":"资源下载总字节数","verification_completed_bytes":"本地摘要校验已处理字节数","verification_total_bytes":"本地摘要校验总字节数","error":"失败说明","can_cancel":"是否允许取消","installed_resources":"本次启用的组件编号","import_preview":"导入确认内容"},
    });
    value["operation"] = json!(if is_system_feature_job(&snapshot.job_id) {
        "system_feature"
    } else if snapshot.job_id.starts_with("reset-uninstall-") || snapshot.job_id.starts_with("history-reset-uninstall-") {
        "uninstall"
    } else if snapshot.job_id.starts_with("reset-") || snapshot.job_id.starts_with("history-reset-")
    {
        "reset"
    } else {
        "install"
    });
    value["first_error"] = json!(snapshot.first_error);
    value["failure_resource_id"] = json!(snapshot.failure_resource_id);
    value["fields"]["failure_resource_id"] = json!("本轮实际失败的组件，依赖故障不能归给请求模型");
    value["fields"]["first_error"] = json!("本轮任务首次错误，自动恢复不能覆盖");
    value["fields"]["operation"] = json!(if is_system_feature_job(&snapshot.job_id) {
        "系统功能查询或启动任务"
    } else {
        "组件安装或用户确认的重置"
    });
    if is_system_feature_job(&snapshot.job_id) {
        value["recovery"] = Value::Null;
        value["fields"]["resource_id"] = json!("Windows 系统功能编号");
        value["fields"]["installed_resources"] =
            json!("本任务不安装工作区组件；系统功能状态单独复核");
        value["fields"]["recovery"] = json!("此任务不进入组件恢复事务");
        value["fields"]["failure_resource_id"] = json!("本轮系统功能任务的固定失败目标");
    }
    value
}

fn terminal(state: &str) -> bool {
    matches!(
        state,
        "complete" | "completed" | "failed" | "cancelled" | "canceled"
    )
}

fn terminal_job_history_for_retry(job_id: &str, previous: &JobSnapshot) -> Option<JobSnapshot> {
    if !terminal(&previous.state) {
        return None;
    }
    let mut history = previous.clone();
    history.job_id = format!("history-{job_id}-{}", history.created_at);
    Some(history)
}

fn is_system_feature_job(job_id: &str) -> bool {
    job_id.starts_with("system-feature-") || job_id.starts_with("history-system-feature-")
}

fn validate_batch_admission(
    jobs: &HashMap<String, JobControl>,
    resource: &str,
    parent: Option<&str>,
) -> Result<(), String> {
    let batch = jobs
        .values()
        .find(|job| job.snapshot.resource_id == "required-batch" && !terminal(&job.snapshot.state));
    if resource == "required-batch" {
        if jobs.values().any(|job| !terminal(&job.snapshot.state)) {
            return Err("请等待当前资源任务完成后再一键下载。".into());
        }
    } else if let Some(parent) = parent {
        if !batch
            .is_some_and(|job| job.snapshot.job_id == parent && !job.cancel.load(Ordering::Acquire))
        {
            return Err("所属一键任务已结束或取消，不再启动新组件。".into());
        }
    } else if batch.is_some() {
        return Err("一键下载正在处理必要组件，请等待完成或取消后再操作。".into());
    }
    Ok(())
}

fn progress_kind(phase: &str) -> &'static str {
    // progress_kind（阶段投影）：字节只属于当前阶段。
    if phase.starts_with("构建模型：") || phase.starts_with("构建推理引擎：") {
        "build"
    } else if phase.starts_with(voice_desktop_common::download::BATCH_DOWNLOAD_PROGRESS_PREFIX) {
        "download"
    } else if phase.starts_with(voice_desktop_common::download::BATCH_VERIFY_PROGRESS_PREFIX) {
        "verify"
    } else if phase.contains("下载固定资源") || phase.contains("正在下载") {
        "download"
    } else if phase.contains("等待")
        || phase.contains("来源")
        || phase.starts_with("一键下载：")
        || phase.starts_with("自动恢复")
    {
        "waiting"
    } else if phase.contains("校验") || phase.contains("核验") || phase.contains("核对") {
        "verify"
    } else if phase.contains("下载进度") {
        "download"
    } else {
        "install"
    }
}
fn include_in_required_batch(item: &Value) -> bool {
    item["required"] != false
        || (item["selected_enhancement"] == true && item["download_in_required_batch"] != false)
}
pub(crate) fn retryable_failure(message: &str) -> bool {
    // retryable_failure（可自动恢复）：取消、永久配置与权限错误停止重试。
    ![
        "取消",
        "空间不足",
        "磁盘空间",
        "目录权限",
        "拒绝访问",
        "Permission denied",
        "os error 5",
        "系统仍拒绝",
        "系统错误码：5",
        "缺少可信",
        "清单无效",
        "清单缺失",
        "未锁定",
        "来源必须",
        "兼容显卡环境",
        "无法暂停",
        "恢复旧资源失败",
        "恢复原组件失败",
        "资源回滚留待",
    ]
    .iter()
    .any(|reason| message.contains(reason))
}

pub(crate) fn valid_resource_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || (index > 0 && byte == b'-')
        })
}

fn unix_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u64::MAX as u128) as u64
}

fn new_job_id(prefix: &str) -> String {
    let counter = NEXT_JOB.fetch_add(1, Ordering::Relaxed);
    format!("{prefix}-{:x}-{:x}", unix_millis(), counter)
}

fn remote_import_is_reparse(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

fn new_remote_source_id() -> String {
    let mut value = String::with_capacity(32);
    while value.len() < 32 {
        value.extend(
            voice_desktop_common::workspace::new_id()
                .bytes()
                .filter(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
                .map(char::from),
        );
    }
    value.truncate(32);
    value
}

fn create_remote_import_stage(root: &Path) -> Result<(String, PathBuf), String> {
    for _ in 0..8 {
        let source_id = new_remote_source_id();
        if !valid_source_id(&source_id) {
            continue;
        }
        let staging_dir = root.join(&source_id);
        match fs::create_dir(&staging_dir) {
            Ok(()) => return Ok((source_id, staging_dir)),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err("无法创建独占远程导入暂存目录。".into()),
        }
    }
    Err("无法生成唯一远程导入来源编号。".into())
}

fn remote_import_marker_path(root: &Path, source_id: &str) -> Result<PathBuf, String> {
    if !valid_source_id(source_id) {
        return Err("远程导入来源编号无效。".into());
    }
    Ok(root.join(format!(".{source_id}.owner.json")))
}

fn write_remote_import_owner_marker(
    root: &Path,
    source_id: &str,
    expires_at_ms: u64,
) -> Result<(), String> {
    let marker = remote_import_marker_path(root, source_id)?;
    let marker_name = marker
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or("远程导入所有权标记名称无效。")?;
    let marker = voice_desktop_common::workspace::safe_path(root, marker_name)?;
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(marker)
        .map_err(|_| "无法登记远程导入暂存目录所有权。".to_owned())?;
    let payload = serde_json::to_vec(&json!({
        "source_id": source_id,
        "expires_at_ms": expires_at_ms,
        "fields": {
            "source_id": "服务端签发的暂存来源编号",
            "expires_at_ms": "暂存来源到期时刻，Unix 毫秒",
        }
    }))
    .map_err(|_| "无法编码远程导入暂存目录所有权。".to_owned())?;
    use std::io::Write;
    file.write_all(&payload)
        .map_err(|_| "无法写入远程导入暂存目录所有权。".to_owned())?;
    file.sync_all()
        .map_err(|_| "无法持久化远程导入暂存目录所有权。".to_owned())
}

fn read_remote_import_owner_marker(root: &Path, source_id: &str) -> Result<u64, String> {
    let marker = remote_import_marker_path(root, source_id)?;
    let marker_name = marker
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or("远程导入所有权标记名称无效。")?;
    let marker = voice_desktop_common::workspace::safe_path(root, marker_name)?;
    let metadata = fs::symlink_metadata(&marker).map_err(|_| "远程导入所有权标记不存在。")?;
    if remote_import_is_reparse(&metadata) || !metadata.is_file() || metadata.len() > 4096 {
        return Err("远程导入所有权标记无效。".into());
    }
    let value: Value =
        serde_json::from_slice(&fs::read(marker).map_err(|_| "远程导入所有权标记不可读取。")?)
            .map_err(|_| "远程导入所有权标记格式无效。")?;
    if value["source_id"] != source_id {
        return Err("远程导入所有权标记与目录不匹配。".into());
    }
    value["expires_at_ms"]
        .as_u64()
        .ok_or_else(|| "远程导入所有权标记缺少到期时间。".into())
}

fn remove_remote_import_marker(root: &Path, source_id: &str) -> Result<(), String> {
    let marker = remote_import_marker_path(root, source_id)?;
    let marker_name = marker
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or("远程导入所有权标记名称无效。")?;
    let marker = voice_desktop_common::workspace::safe_path(root, marker_name)?;
    match fs::symlink_metadata(&marker) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err("无法复核远程导入所有权标记。".into()),
        Ok(metadata) if remote_import_is_reparse(&metadata) || !metadata.is_file() => {
            Err("远程导入所有权标记不是普通文件，已保留。".into())
        }
        Ok(_) => fs::remove_file(marker).map_err(|_| "无法清理远程导入所有权标记。".into()),
    }
}

fn remove_remote_import_tree(root: &Path, source_id: &str) -> Result<(), String> {
    if !valid_source_id(source_id) {
        return Err("远程导入来源编号无效。".into());
    }
    remove_remote_import_path(root, &root.join(source_id))
}

fn remove_remote_import_path(root: &Path, path: &Path) -> Result<(), String> {
    let root_metadata = fs::symlink_metadata(root).map_err(|_| "远程导入暂存根无法复核。")?;
    if remote_import_is_reparse(&root_metadata) || !root_metadata.is_dir() {
        return Err("远程导入暂存根不是普通目录，已停止清理。".into());
    }
    let root = root
        .canonicalize()
        .map_err(|_| "远程导入暂存根无法复核。")?;
    if path
        .parent()
        .and_then(|parent| parent.canonicalize().ok())
        .as_deref()
        != Some(root.as_path())
    {
        return Err("远程导入清理路径不在专用暂存根内。".into());
    }
    let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
        return Err("远程导入清理路径名称无效。".into());
    };
    if !(valid_source_id(name) || name.strip_prefix("accepted-").is_some_and(valid_source_id)) {
        return Err("远程导入清理路径不属于签发目录。".into());
    }
    remove_tree_without_following_reparse(path)
}

fn remove_tree_without_following_reparse(path: &Path) -> Result<(), String> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err("无法复核远程导入暂存内容。".into()),
    };
    if remote_import_is_reparse(&metadata) {
        return if metadata.is_dir() {
            fs::remove_dir(path).map_err(|_| "无法移除远程导入重解析点。".into())
        } else {
            fs::remove_file(path).map_err(|_| "无法移除远程导入链接。".into())
        };
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(path).map_err(|_| "无法读取远程导入暂存目录。")? {
            let entry = entry.map_err(|_| "无法读取远程导入暂存项。")?;
            remove_tree_without_following_reparse(&entry.path())?;
        }
        fs::remove_dir(path).map_err(|_| "无法移除远程导入暂存目录。".into())
    } else if metadata.is_file() {
        fs::remove_file(path).map_err(|_| "无法移除远程导入暂存文件。".into())
    } else {
        Err("远程导入暂存内容含特殊文件，已停止清理。".into())
    }
}

fn seal_remote_import_stage(
    root: &Path,
    source_id: &str,
    original_dir: &Path,
) -> Result<PathBuf, String> {
    if !valid_source_id(source_id) || original_dir != root.join(source_id) {
        return Err("远程导入暂存目录与来源编号不匹配。".into());
    }
    let metadata = fs::symlink_metadata(original_dir).map_err(|_| "远程导入暂存目录已消失。")?;
    if remote_import_is_reparse(&metadata) || !metadata.is_dir() {
        return Err("远程导入暂存目录不是普通目录。".into());
    }
    let committed_dir = root.join(format!("accepted-{source_id}"));
    if fs::symlink_metadata(&committed_dir).is_ok() {
        return Err("已核验导入目录编号已存在，未覆盖现有内容。".into());
    }
    fs::rename(original_dir, &committed_dir)
        .map_err(|_| String::from("无法锁定已核验的远程导入暂存目录。"))?;
    Ok(committed_dir)
}

fn verify_remote_import_tree(
    staging_dir: &Path,
    manifest: &RemoteImportManifest,
) -> Result<(), String> {
    manifest.validate()?;
    let root_metadata =
        fs::symlink_metadata(staging_dir).map_err(|_| "远程导入暂存目录不可用。")?;
    if remote_import_is_reparse(&root_metadata) || !root_metadata.is_dir() {
        return Err("远程导入暂存根不是普通目录。".into());
    }
    let mut actual_files = HashSet::new();
    collect_remote_import_files(staging_dir, staging_dir, &mut actual_files)?;
    let expected_files = manifest
        .files
        .iter()
        .map(|file| file.relative_path.clone())
        .collect::<HashSet<_>>();
    if actual_files != expected_files {
        return Err("远程导入清单未完整覆盖暂存目录中的文件。".into());
    }
    for entry in &manifest.files {
        let path =
            entry
                .relative_path
                .split('/')
                .fold(staging_dir.to_path_buf(), |mut path, part| {
                    path.push(part);
                    path
                });
        let safe = voice_desktop_common::workspace::safe_path(staging_dir, &entry.relative_path)?;
        if safe != path {
            return Err("远程导入相对路径解析结果不一致。".into());
        }
        let before = fs::symlink_metadata(&safe).map_err(|_| "远程导入清单中的文件不可读取。")?;
        if remote_import_is_reparse(&before)
            || !before.is_file()
            || before.len() != entry.byte_length
        {
            return Err("远程导入文件类型或长度与清单不匹配。".into());
        }
        let actual_hash = voice_desktop_common::workspace::file_hash(&safe)
            .map_err(|_| "远程导入文件读取或摘要核验失败。".to_owned())?;
        if voice_desktop_common::workspace::safe_path(staging_dir, &entry.relative_path)? != safe {
            return Err("远程导入文件路径在校验期间发生变化。".into());
        }
        let after = fs::symlink_metadata(&safe).map_err(|_| "远程导入文件在校验期间发生变化。")?;
        if remote_import_is_reparse(&after)
            || !after.is_file()
            || after.len() != entry.byte_length
            || actual_hash != entry.sha256
        {
            return Err("远程导入文件摘要、长度或普通文件状态复核失败。".into());
        }
    }
    Ok(())
}

fn collect_remote_import_files(
    root: &Path,
    current: &Path,
    files: &mut HashSet<String>,
) -> Result<(), String> {
    for entry in fs::read_dir(current).map_err(|_| "无法读取远程导入暂存目录。")? {
        let entry = entry.map_err(|_| "无法读取远程导入暂存文件项。")?;
        let path = entry.path();
        let relative = path
            .strip_prefix(root)
            .map_err(|_| "远程导入路径越界。")?
            .components()
            .map(|part| {
                part.as_os_str()
                    .to_str()
                    .ok_or("远程导入文件名不能表示为 Unicode。")
            })
            .collect::<Result<Vec<_>, _>>()?
            .join("/");
        let safe = voice_desktop_common::workspace::safe_path(root, &relative)?;
        if safe != path {
            return Err("远程导入暂存路径解析结果不一致。".into());
        }
        let metadata = fs::symlink_metadata(&safe).map_err(|_| "无法复核远程导入暂存项。")?;
        if metadata.is_dir() {
            collect_remote_import_files(root, &safe, files)?;
        } else if metadata.is_file() {
            if !files.insert(relative) {
                return Err("远程导入暂存目录包含路径冲突。".into());
            }
        } else {
            return Err("远程导入暂存目录包含特殊文件。".into());
        }
    }
    Ok(())
}

fn localized_phase(value: &str) -> &'static str {
    match value {
        "starting" => "正在启动下载",
        "downloading" => "正在下载资源",
        "verifying" | "verifying_installer" => "正在校验下载文件",
        "installing" => "正在原子安装资源",
        "extracting_nsis" | "extracting_payload" => "正在展开 DSH 安装器",
        "swapping_runtime" => "正在切换 DSH 运行目录",
        "complete" => "资源处理完成",
        _ => "正在处理资源",
    }
}

fn command_allowed(
    window: &WebviewWindow,
    desktop: &State<'_, Arc<super::Desktop>>,
) -> Result<(), String> {
    super::require_command(desktop.mode, super::DesktopCommand::FocusMain)?;
    super::trusted(window, desktop)?;
    super::require_local_device_target(desktop)
}

fn schedule_after_system_feature_confirmation<T>(
    confirmed: bool,
    schedule: impl FnOnce() -> T,
) -> Result<T, String> {
    if !confirmed {
        return Err("启用 OpenSSH Server 必须先由用户明确确认 Windows 系统安装及服务启动。".into());
    }
    Ok(schedule())
}

#[tauri::command]
pub fn resource_jobs(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
) -> Result<Vec<Value>, String> {
    command_allowed(&window, &desktop)?;
    Ok(manager.snapshots())
}

/// resource_system_feature_status（读取系统功能状态）：显式管理页调用，角色不符时拒绝。
#[tauri::command]
pub async fn resource_system_feature_status(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    refresh: Option<bool>, // refresh（是否绕过同进程短时快照）。
) -> Result<voice_desktop_common::system_feature::OpenSshServerStatus, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        manager.open_ssh_server_status(refresh.unwrap_or(false))
    })
    .await
    .map_err(|_| "OpenSSH Server 状态查询任务无法启动。".to_string())?
}

/// resource_system_feature_enable（启动系统功能任务）：只接受固定 OpenSSH Server 主机功能任务。
#[tauri::command]
pub async fn resource_system_feature_enable(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    confirmed: bool, // confirmed（用户已确认 Windows 系统功能安装与 sshd 服务启动）。
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    let task = schedule_after_system_feature_confirmation(confirmed, || {
        let manager = manager.inner().clone();
        tauri::async_runtime::spawn_blocking(move || manager.enable_open_ssh_server())
    })?;
    task.await
        .map_err(|_| "OpenSSH Server 管理任务无法启动。".to_string())?
}

/// resource_system_feature_access（设置项目局域网访问）：系统授权只编辑程序专用规则。
#[tauri::command]
pub async fn resource_system_feature_access(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    enabled: bool,
) -> Result<voice_desktop_common::system_feature::OpenSshServerStatus, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.set_open_ssh_project_access(enabled))
        .await
        .map_err(|_| "OpenSSH Server 访问范围管理任务无法启动。".to_string())?
}

#[tauri::command]
pub async fn aniflive_import_choose_folder(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
) -> Result<Option<String>, String> {
    command_allowed(&window, &desktop)?;
    tauri::async_runtime::spawn_blocking(|| {
        voice_desktop_common::native_ui::choose_folder("选择 GPT-SoVITS 模型包所在文件夹")
    })
    .await
    .map_err(|_| "模型文件夹选择器无法启动。".to_string())?
    .map(|path| path.map(|value| value.to_string_lossy().into_owned()))
}

#[tauri::command(rename_all = "snake_case")]
pub async fn aniflive_import_start(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    task_id: String,
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.start_aniflive_import(task_id))
        .await
        .map_err(|_| "AnifLive 转换任务无法启动。".to_string())?
}

#[tauri::command]
pub async fn resource_inventory(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
) -> Result<Vec<Value>, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.inventory())
        .await
        .map_err(|_| "资源清单读取任务失败。")?
}

#[tauri::command(rename_all = "snake_case")]
pub async fn resource_download(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    resource_id: String,
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.download(resource_id))
        .await
        .map_err(|_| "资源下载任务无法启动。")?
}

#[tauri::command]
pub async fn resource_download_required(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    manager.inner().clone().download_required()
}

#[tauri::command(rename_all = "snake_case")]
pub async fn resource_import(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    resource_id: Option<String>,
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.import(resource_id))
        .await
        .map_err(|_| "资源导入任务无法启动。")?
}

/// resource_import_choose_sources（选择资源导入来源）：由点击按钮的设备调用原生选择器。
#[tauri::command(rename_all = "snake_case")]
pub async fn resource_import_choose_sources(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    resource_id: Option<String>, // resource_id（目标资源编号；空值表示批量导入）。
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.choose_import_sources(resource_id))
        .await
        .map_err(|_| "资源来源选择器无法启动。")?
}

/// resource_import_start（按来源票据启动导入）：服务端只处理本机原生已登记的来源。
#[tauri::command(rename_all = "snake_case")]
pub async fn resource_import_start(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    selection_id: String, // selection_id（原生进程签发的一次性来源票据）。
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.start_import_sources(selection_id))
        .await
        .map_err(|_| "资源导入任务无法启动。")?
}

#[tauri::command(rename_all = "snake_case")]
pub async fn resource_open_source(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    resource_id: String,
) -> Result<(), String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.open_source(&resource_id))
        .await
        .map_err(|_| "打开资源来源任务失败。")?
}

#[tauri::command(rename_all = "snake_case")]
pub async fn resource_cancel(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
    job_id: String,
) -> Result<(), String> {
    command_allowed(&window, &desktop)?;
    manager.cancel(&job_id)
}

#[tauri::command(rename_all = "snake_case")]
pub fn resource_import_confirm(
    window: WebviewWindow,
    desktop: State<Arc<super::Desktop>>,
    manager: State<Arc<ResourceManager>>,
    job_id: String,
    accepted: bool,
) -> Result<(), String> {
    super::trusted(&window, &desktop)?;
    manager.confirm_import(&job_id, accepted)
}

#[cfg(test)]
mod remote_import_tests {
    use super::*;

    fn temp_root(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "voice-{label}-{}",
            voice_desktop_common::workspace::new_id()
        ))
    }

    #[test]
    fn remote_import_source_ids_are_contract_sized_and_unique() {
        let first = new_remote_source_id();
        let second = new_remote_source_id();
        assert!(valid_source_id(&first));
        assert!(valid_source_id(&second));
        assert_ne!(first, second);
    }

    #[test]
    fn remote_import_tree_requires_exact_files_lengths_and_sha256() {
        let root = temp_root("remote-import-tree");
        fs::create_dir_all(root.join("models")).unwrap();
        fs::write(root.join("models/voice.bin"), b"checked-data").unwrap();
        let manifest = voice_desktop_common::remote_import_contract::RemoteImportManifest {
            source_id: "0123456789abcdef0123456789abcdef".into(),
            total_bytes: 12,
            files: vec![
                voice_desktop_common::remote_import_contract::RemoteImportFileEntry {
                    relative_path: "models/voice.bin".into(),
                    byte_length: 12,
                    sha256: voice_desktop_common::workspace::file_hash(
                        &root.join("models/voice.bin"),
                    )
                    .unwrap(),
                },
            ],
        };
        assert!(verify_remote_import_tree(&root, &manifest).is_ok());

        let mut bad_hash = manifest.clone();
        bad_hash.files[0].sha256 = "0".repeat(64);
        assert!(verify_remote_import_tree(&root, &bad_hash).is_err());
        fs::write(root.join("unlisted.bin"), b"extra").unwrap();
        assert!(verify_remote_import_tree(&root, &manifest).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn remote_import_cleanup_rejects_paths_outside_its_root() {
        let root = temp_root("remote-import-cleanup");
        fs::create_dir_all(&root).unwrap();
        let outside = root.join("outside");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("keep.txt"), b"keep").unwrap();
        let staging_root = root.join("staging");
        fs::create_dir_all(&staging_root).unwrap();

        assert!(remove_remote_import_path(&staging_root, &outside).is_err());
        assert!(outside.join("keep.txt").is_file());
        fs::remove_dir_all(root).unwrap();
    }
}

#[cfg(test)]
mod service_tests {
    #[test]
    fn system_feature_job_uses_separate_type_and_chinese_field_meanings() {
        let snapshot = super::JobSnapshot {
            job_id: voice_desktop_common::system_feature::OPENSSH_SERVER_JOB_ID.into(),
            resource_id: voice_desktop_common::system_feature::OPENSSH_SERVER_ID.into(),
            state: "running".into(),
            phase: "等待 Windows 管理员授权".into(),
            progress_kind: "waiting".into(),
            attempt: 1,
            parent_job_id: None,
            completed_bytes: 0,
            total_bytes: None,
            download_completed_bytes: None,
            download_total_bytes: None,
            verification_completed_bytes: None,
            verification_total_bytes: None,
            failure_resource_id: None,
            first_error: None,
            error: None,
            can_cancel: false,
            installed_resources: vec![],
            import_preview: None,
            created_at: 1,
            revision: 1,
            recovery: None,
        };
        let value = super::event_value(&snapshot);
        assert_eq!(value["operation"], "system_feature");
        assert_eq!(value["fields"]["resource_id"], "Windows 系统功能编号");
        assert_eq!(
            value["fields"]["installed_resources"],
            "本任务不安装工作区组件；系统功能状态单独复核"
        );
        assert!(super::is_system_feature_job(&snapshot.job_id));
        assert!(super::is_system_feature_job(
            "history-system-feature-openssh-server-17"
        ));
        assert!(!super::is_system_feature_job("dl-dsh"));
    }

    #[test]
    fn ssh_elevation_heartbeat_stays_a_waiting_phase_and_shows_elapsed_time() {
        let phase = super::open_ssh_server_wait_phase(Duration::from_secs(75));
        assert!(phase.contains("管理员授权或官方功能处理"));
        assert!(phase.contains("75 秒"));
        assert_eq!(super::progress_kind(&phase), "waiting");
    }

    #[test]
    fn local_system_feature_command_requires_confirmation_before_scheduling() {
        let scheduled = std::cell::Cell::new(0);
        let rejected = super::schedule_after_system_feature_confirmation(false, || {
            scheduled.set(scheduled.get() + 1);
            "scheduled"
        });
        assert!(rejected.is_err());
        assert_eq!(
            scheduled.get(),
            0,
            "unconfirmed request must not spawn the manager task"
        );

        let accepted = super::schedule_after_system_feature_confirmation(true, || {
            scheduled.set(scheduled.get() + 1);
            "scheduled"
        })
        .unwrap();
        assert_eq!(accepted, "scheduled");
        assert_eq!(scheduled.get(), 1);
    }

    #[test]
    fn fixed_ssh_job_reports_uac_cancel_and_archives_terminal_attempts_for_retry() {
        let cancelled = super::open_ssh_server_job_error(
            voice_desktop_common::system_feature::OPENSSH_SERVER_UAC_CANCELLED_ERROR.into(),
        );
        assert!(matches!(cancelled, super::JobError::Cancelled));
        let source_failure = super::open_ssh_server_job_error(
            "Windows 官方 OpenSSH Server 功能源安装失败；请检查 Windows Update。".into(),
        );
        assert!(
            matches!(source_failure, super::JobError::Failed { message, .. } if message.contains("功能源"))
        );

        let make_snapshot = |state: &str, created_at: u64| super::JobSnapshot {
            job_id: voice_desktop_common::system_feature::OPENSSH_SERVER_JOB_ID.into(),
            resource_id: voice_desktop_common::system_feature::OPENSSH_SERVER_ID.into(),
            state: state.into(),
            phase: "等待 Windows 管理员授权".into(),
            progress_kind: "waiting".into(),
            attempt: 1,
            parent_job_id: None,
            completed_bytes: 0,
            total_bytes: None,
            download_completed_bytes: None,
            download_total_bytes: None,
            verification_completed_bytes: None,
            verification_total_bytes: None,
            failure_resource_id: None,
            first_error: (state == "failed").then(|| "Windows 官方功能源失败".into()),
            error: (state == "failed").then(|| "Windows 官方功能源失败".into()),
            can_cancel: false,
            installed_resources: vec![],
            import_preview: None,
            created_at,
            revision: 2,
            recovery: None,
        };

        for state in ["failed", "cancelled"] {
            let previous = make_snapshot(state, 17);
            assert!(super::terminal(&previous.state));
            let history = super::terminal_job_history_for_retry(
                voice_desktop_common::system_feature::OPENSSH_SERVER_JOB_ID,
                &previous,
            )
            .expect("terminal fixed OpenSSH task must remain reviewable on retry");
            assert_eq!(
                history.job_id,
                format!(
                    "history-{}-17",
                    voice_desktop_common::system_feature::OPENSSH_SERVER_JOB_ID
                )
            );
            assert_eq!(history.state, state);
            assert_eq!(history.error, previous.error);
        }

        let active = make_snapshot("running", 18);
        assert!(!super::terminal(&active.state));
        assert!(super::terminal_job_history_for_retry(
            voice_desktop_common::system_feature::OPENSSH_SERVER_JOB_ID,
            &active,
        )
        .is_none());
    }

    #[test]
    fn unattended_import_dispatch_only_accepts_confirmed_unstarted_tasks() {
        let id = "a".repeat(32);
        let task = serde_json::json!({"task_id": id, "status": "ready_to_convert"});
        assert!(super::ready_import(&task, &id, false));
        assert!(!super::ready_import(&task, &id, true));
        assert!(!super::ready_import(&task, &"b".repeat(32), false));
        for status in [
            "staging",
            "pending_reference",
            "failed",
            "cancelled",
            "complete",
            "converting",
        ] {
            assert!(!super::ready_import(
                &serde_json::json!({"task_id": id, "status": status}),
                &id,
                false
            ));
        }
    }
    #[test]
    fn restored_jobs_preserve_event_classification_and_action_after_restart() {
        let root = std::env::temp_dir().join(format!(
            "voice-recovery-jobs-{}",
            voice_desktop_common::workspace::new_id()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("jobs.json");
        voice_desktop_common::workspace::atomic_json(&path,&json!({"jobs":[
            {"job_id":"network","resource_id":"model-a","state":"failed","phase":"下载失败","error":"下载失败：HTTP 503"},
            {"job_id":"content","resource_id":"model-b","state":"failed","phase":"未完成","error":"增量依赖实际加载失败：httpx"},
            {"job_id":"interrupted","resource_id":"model-c","state":"running","phase":"处理中"}
        ]})).unwrap();
        let jobs = ResourceManager::load_jobs(&path).unwrap();
        assert_eq!(
            jobs["network"].snapshot.recovery.as_ref().unwrap()["next_action"],
            "retry"
        );
        assert_eq!(
            jobs["content"].snapshot.recovery.as_ref().unwrap()["next_action"],
            "check_integrity"
        );
        assert_eq!(
            jobs["interrupted"].snapshot.recovery.as_ref().unwrap()["event"],
            "interrupted"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn restored_system_feature_jobs_remove_component_recovery_and_preserve_failure_history() {
        let root = std::env::temp_dir().join(format!(
            "voice-system-feature-jobs-{}",
            voice_desktop_common::workspace::new_id()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("jobs.json");
        let original_error = "OpenSSH Server 状态查询失败；请检查系统功能查询权限。";
        for job_id in [
            "system-feature-openssh-server",
            "history-system-feature-openssh-server-17",
        ] {
            for state in ["failed", "running"] {
                for recovery in [
                    Value::Null,
                    voice_desktop_common::component_recovery::guidance(
                        voice_desktop_common::component_recovery::Event::Unknown,
                        false,
                    ),
                ] {
                    voice_desktop_common::workspace::atomic_json(
                        &path,
                        &json!({"jobs":[{
                            "job_id":job_id,"resource_id":"openssh-server",
                            "state":state,"phase":"系统功能查询失败",
                            "error":original_error,"first_error":original_error,
                            "recovery":recovery,"created_at":17,"revision":2
                        }]}),
                    )
                    .unwrap();
                    let jobs = ResourceManager::load_jobs(&path).unwrap();
                    let snapshot = &jobs[job_id].snapshot;
                    assert_eq!(snapshot.state, "failed");
                    assert_eq!(snapshot.first_error.as_deref(), Some(original_error));
                    assert_eq!(snapshot.created_at, 17);
                    assert_eq!(snapshot.revision, 3);
                    assert!(snapshot.recovery.is_none());
                    assert!(!snapshot.can_cancel);
                    assert!(!snapshot.phase.contains("下载"));
                    if state == "failed" {
                        assert_eq!(snapshot.error.as_deref(), Some(original_error));
                    } else {
                        assert!(snapshot.phase.contains("设备与角色"));
                    }
                    let value = super::event_value(snapshot);
                    assert_eq!(value["operation"], "system_feature");
                    assert!(value["recovery"].is_null());
                    assert_eq!(value["first_error"], original_error);
                }
            }
        }
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn source_and_speed_details_preserve_transfer_progress_kind() {
        assert_eq!(super::progress_kind("构建推理引擎：已完成 3 / 9"), "build");
        assert_eq!(
            super::progress_kind(
                "下载固定资源（四路加速） · 8.20 MiB/秒（兆二进制字节每秒） · 来源：GitHub（代码托管平台）原站"
            ),
            "download"
        );
        assert_eq!(
            super::progress_kind("来源测速：GitHub（代码托管平台）原站"),
            "waiting"
        );
        assert_eq!(super::progress_kind("校验下载文件"), "verify");
    }
    #[test]
    fn optional_audio_models_stay_out_of_initial_required_batch() {
        for id in ["emotion2vec-plus-base", "yamnet"] {
            assert!(!super::include_in_required_batch(
                &serde_json::json!({"resource_id":id,"required":false,"selected_enhancement":true,"download_in_required_batch":false})
            ));
        }
        assert!(super::include_in_required_batch(
            &serde_json::json!({"resource_id":"paraformer-offline","required":false,"selected_enhancement":true})
        ));
        assert!(super::include_in_required_batch(
            &serde_json::json!({"resource_id":"required-model","required":true,"selected_enhancement":false,"download_in_required_batch":false})
        ));
    }
    use super::*;

    #[test]
    fn required_batch_reserves_jobs_and_only_admits_its_own_children() {
        let root = std::env::temp_dir().join(format!(
            "voice-job-admission-{}",
            voice_desktop_common::workspace::new_id()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("jobs.json");
        voice_desktop_common::workspace::atomic_json(&path,&json!({"schema":1,"jobs":[{"job_id":"dl-required","resource_id":"required-batch","state":"complete","phase":"已完成","created_at":100}]})).unwrap();
        let mut jobs = ResourceManager::load_jobs(&path).unwrap();
        jobs.get_mut("dl-required").unwrap().snapshot.state = "running".into();
        assert!(validate_batch_admission(&jobs, "model", None).is_err());
        assert!(validate_batch_admission(&jobs, "required-batch", None).is_err());
        assert!(validate_batch_admission(&jobs, "model", Some("dl-required")).is_ok());
        assert!(validate_batch_admission(&jobs, "model", Some("other-batch")).is_err());
        jobs["dl-required"].cancel.store(true, Ordering::Release);
        assert!(validate_batch_admission(&jobs, "model", Some("dl-required")).is_err());
        jobs.get_mut("dl-required").unwrap().snapshot.state = "cancelled".into();
        assert!(validate_batch_admission(&jobs, "model", None).is_ok());
        assert!(validate_batch_admission(&jobs, "model", Some("dl-required")).is_err());
        assert!(validate_batch_admission(&jobs, "required-batch", None).is_ok());
        jobs.get_mut("dl-required").unwrap().snapshot.resource_id = "model".into();
        jobs.get_mut("dl-required").unwrap().snapshot.state = "running".into();
        assert!(validate_batch_admission(&jobs, "required-batch", None).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn persisted_jobs_keep_revision_order_and_interrupted_tasks_become_terminal() {
        let root = std::env::temp_dir().join(format!(
            "voice-job-revision-{}",
            voice_desktop_common::workspace::new_id()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("jobs.json");
        voice_desktop_common::workspace::atomic_json(&path,&json!({"schema":1,"jobs":[{
            "job_id":"dl-model","resource_id":"model","state":"running","phase":"正在下载","created_at":100,"revision":7,"completed_bytes":50,"total_bytes":100
        }]})).unwrap();
        let jobs = ResourceManager::load_jobs(&path).unwrap();
        let event = event_value(&jobs["dl-model"].snapshot);
        assert_eq!(event["state"], "failed");
        assert_eq!(event["revision"], 8);
        assert_eq!(event["created_at"], 100);
        assert_eq!(event["can_cancel"], false);
        assert!(event["fields"]["revision"]
            .as_str()
            .unwrap()
            .contains("修订号"));
        fs::remove_dir_all(root).unwrap();
    }

    fn control(running: bool) -> (ServiceControl, Arc<Mutex<Vec<bool>>>) {
        let calls = Arc::new(Mutex::new(vec![]));
        let log = calls.clone();
        let state = Arc::new(AtomicBool::new(running));
        (
            Arc::new(move |start| {
                log.lock().unwrap().push(start);
                if start {
                    state.store(true, Ordering::Release);
                    Ok(true)
                } else {
                    Ok(state.swap(false, Ordering::AcqRel))
                }
            }),
            calls,
        )
    }

    #[test]
    fn component_management_does_not_start_service_after_install() {
        let (control, calls) = control(false);
        let lock = Mutex::new(());
        ServiceLease::pause(&control, lock.lock().unwrap())
            .unwrap()
            .finish()
            .unwrap();
        assert_eq!(*calls.lock().unwrap(), vec![false]);
    }

    #[test]
    fn activation_restores_running_service_exactly_once() {
        let (control, calls) = control(true);
        let lock = Mutex::new(());
        ServiceLease::pause(&control, lock.lock().unwrap())
            .unwrap()
            .finish()
            .unwrap();
        assert_eq!(*calls.lock().unwrap(), vec![false, true]);
    }

    #[test]
    fn cancelled_activation_restores_only_previously_running_service() {
        for running in [false, true] {
            let (control, calls) = control(running);
            let lock = Mutex::new(());
            drop(ServiceLease::pause(&control, lock.lock().unwrap()).unwrap());
            assert_eq!(
                *calls.lock().unwrap(),
                if running {
                    vec![false, true]
                } else {
                    vec![false]
                }
            );
        }
    }

    #[test]
    fn pending_update_keeps_previously_running_service_stopped() {
        let (control, calls) = control(true);
        let lock = Mutex::new(());
        let mut lease = ServiceLease::pause(&control, lock.lock().unwrap()).unwrap();
        assert!(lease.was_running());
        lease.keep_stopped();
        drop(lease);
        assert_eq!(*calls.lock().unwrap(), vec![false]);
    }

    #[test]
    fn cancelled_maintenance_preserves_resume_intent_without_starting_idle_services() {
        for running in [false, true] {
            let (control, calls) = control(running);
            let lock = Mutex::new(());
            let pending = Mutex::new(running);
            let accepting = AtomicBool::new(false);
            let batch = AtomicBool::new(false);
            let mut paused = ServiceLease::pause(&control, lock.lock().unwrap()).unwrap();
            assert!(!resume_service_if_ready(
                &mut paused,
                &pending,
                &accepting,
                &batch,
                || panic!("维护期间不能启动资源检查或服务")
            )
            .unwrap());
            drop(paused);
            assert_eq!(*pending.lock().unwrap(), running);
            accepting.store(true, Ordering::Release);
            let mut recovery = ServiceLease::pause(&control, lock.lock().unwrap()).unwrap();
            let resumed =
                resume_service_if_ready(&mut recovery, &pending, &accepting, &batch, || Ok(true))
                    .unwrap();
            clear_resume_intent_if_resumed(&pending, resumed);
            drop(recovery);
            assert_eq!(resumed, running);
            assert!(!*pending.lock().unwrap());
            assert_eq!(
                *calls.lock().unwrap(),
                if running {
                    vec![false, false, true]
                } else {
                    vec![false, false]
                }
            );
        }
    }

    #[test]
    fn preparation_failure_clears_resume_intent_only_when_service_restarts() {
        let pending = Mutex::new(true);
        clear_resume_intent_if_resumed(&pending, false);
        assert!(*pending.lock().unwrap());
        clear_resume_intent_if_resumed(&pending, true);
        assert!(!*pending.lock().unwrap());
    }

    #[test]
    fn environment_preparation_lock_serializes_and_wait_is_cancellable() {
        let environment = Mutex::new(());
        let held = environment.lock().unwrap();
        let waiting = Arc::new(AtomicBool::new(false));
        let cancel = Arc::new(AtomicBool::new(false));
        std::thread::scope(|scope| {
            let observed = waiting.clone();
            let cancel_waiter = cancel.clone();
            let gate = &environment;
            let worker = scope.spawn(move || {
                let mut progress = |phase: &str, _, _| {
                    if phase.contains("等待") {
                        observed.store(true, Ordering::Release);
                    }
                };
                let error =
                    acquire_environment_lock(gate, &cancel_waiter, &mut progress).unwrap_err();
                assert!(error.contains("取消"));
            });
            for _ in 0..100 {
                if waiting.load(Ordering::Acquire) {
                    break;
                }
                thread::sleep(Duration::from_millis(10));
            }
            assert!(
                waiting.load(Ordering::Acquire),
                "第二个环境准备任务必须显示等待锁阶段"
            );
            cancel.store(true, Ordering::Release);
            worker.join().unwrap();
        });
        drop(held);
        let mut progress = |_: &str, _, _| {};
        assert!(
            acquire_environment_lock(&environment, &AtomicBool::new(false), &mut progress).is_ok()
        );
    }
}

/// resource_reset_plan（读取组件重置范围）：不接受任意文件路径。
#[tauri::command(rename_all = "snake_case")]
pub fn resource_reset_plan(
    window: WebviewWindow,
    desktop: State<Arc<super::Desktop>>,
    manager: State<Arc<ResourceManager>>,
    resource_id: String,
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    voice_desktop_common::component_reset::plan(&manager.context, &resource_id)
}
/// resource_reset（确认删除组件）：原生任务负责安全收尾。
#[tauri::command(rename_all = "snake_case")]
pub fn resource_reset(
    window: WebviewWindow,
    desktop: State<Arc<super::Desktop>>,
    manager: State<Arc<ResourceManager>>,
    resource_id: String,
    token: String,
) -> Result<Value, String> {
    command_allowed(&window, &desktop)?;
    manager.inner().reset_resource(resource_id, token)
}
