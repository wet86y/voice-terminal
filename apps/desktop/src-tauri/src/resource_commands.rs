//! Resource commands（资源命令）：manage fixed model and DSH resources without Voice service.
use std::{
    collections::HashMap,
    ffi::OsString,
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, ChildStdout, Command, ExitStatus, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Condvar, Mutex, MutexGuard,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, State, WebviewWindow};
use voice_desktop_common::LaunchContext;

const PROGRESS_EVENT: &str = "desktop://resource-progress";
const MAX_PERSISTED_JOBS: usize = 128;
static NEXT_JOB: AtomicU64 = AtomicU64::new(1);

// ServiceControl（服务控制）：返回是否存在需要恢复的服务，管理页下载不自动启动后台。
pub type ServiceControl = Arc<dyn Fn(bool) -> Result<bool, String> + Send + Sync>;

#[derive(Clone)]
struct JobSnapshot {
    job_id: String, // job_id（任务标识）。
    resource_id: String, // resource_id（资源标识）。
    state: String, // state（任务状态）。
    phase: String, // phase（任务阶段说明）。
    completed_bytes: u64, // completed_bytes（已处理字节数）。
    total_bytes: Option<u64>, // total_bytes（总字节数）。
    error: Option<String>, // error（安全错误说明）。
    can_cancel: bool, // can_cancel（当前是否可取消）。
    installed_resources: Vec<String>, // installed_resources（本次安装资源编号）。
    import_preview: Option<Value>, // import_preview（预校验通过的导入识别结果）。
    created_at: u64, // created_at（任务创建时间，Unix 毫秒）。
    revision: u64, // revision（任务状态修订号）：事件与补查快照使用同一顺序。
}

struct JobControl {
    snapshot: JobSnapshot,
    cancel: Arc<AtomicBool>, // cancel（受控线程取消信号）。
    child: Option<Arc<Mutex<ControlledProcess>>>, // child（仅用于可取消阶段的受控进程）。
    last_event: Instant, // last_event（最近进度广播时刻）。
    last_persist: Instant, // last_persist（最近状态持久化时刻）。
}

struct ControlledProcess {
    child: Child,
    #[cfg(windows)] job: voice_desktop_common::windows::Job, // job（进程树终止容器）。
}

impl ControlledProcess {
    fn new(child: Child) -> Result<Self, String> {
        #[cfg(windows)] {
            use std::os::windows::io::AsRawHandle;
            let job = voice_desktop_common::windows::Job::new()?;
            if let Err(error) = job.assign(child.as_raw_handle() as _) {
                let mut child = child;
                let _ = child.kill();
                return Err(error);
            }
            Ok(Self { child, job })
        }
        #[cfg(not(windows))] { Ok(Self { child }) }
    }

    fn kill(&mut self) {
        #[cfg(windows)] self.job.terminate();
        let _ = self.child.kill();
    }

    fn wait(&mut self) -> std::io::Result<ExitStatus> { self.child.wait() }
}

enum JobError {
    Cancelled,
    Failed { message: String, installed: Vec<String> },
}

struct ProcessError {
    message: String,
    installed: Vec<String>,
    cancelled: bool,
}

#[derive(Clone,PartialEq,Eq)]
struct PreparedEnvironment {
    manifest_sha256:String, // manifest_sha256（已完整验证的环境清单摘要）。
    fingerprint:String, // fingerprint（全部文件的路径、大小及修改时间指纹）。
}
impl PreparedEnvironment{
    fn read(root:&Path)->Result<Self,String>{
        Ok(Self{manifest_sha256:voice_desktop_common::workspace::file_hash(&root.join("manifest.json"))?,fingerprint:voice_desktop_common::runtime::fingerprint(root)?})
    }
}

/// ResourceManager（资源管理器）：owns resource jobs and talks only to fixed local tools.
pub struct ResourceManager {
    context: LaunchContext,
    app: AppHandle,
    service_control: ServiceControl,
    jobs: Mutex<HashMap<String, JobControl>>,
    jobs_changed: Condvar, // jobs_changed（资源任务状态变化通知）。
    jobs_path: PathBuf,
    reviews: Mutex<HashMap<String,bool>>, // reviews（用户对导入内容的选择）。
    activation: Mutex<()>, // activation（仅串行化资源启用；下载、解包与校验不占锁）。
    environment: Mutex<()>, // environment（串行化完整语音环境的复制、组装与导入；模型下载仍可并行）。
    resume_pending: Mutex<bool>, // resume_pending（先前运行服务的延迟恢复意图；全部资源更新完成后才恢复）。
    validation_failures:Mutex<HashMap<String,String>>, // validation_failures（本次启动的完整校验失败）：管理页不能以字节数盘点掩盖同长度损坏。
    prepared_environment:Mutex<Option<PreparedEnvironment>>, // prepared_environment（本次任务已验证的共享环境）：启动仍做全量摘要复核。
}

impl ResourceManager {
    /// Create a manager（建立管理器）from the resolved product context.
    pub fn new(
        context: LaunchContext,
        app: AppHandle,
        service_control: ServiceControl,
    ) -> Result<Self, String> {
        let process_temp = voice_desktop_common::workspace::safe_path(&context.install, "tmp/process")?;
        fs::create_dir_all(&process_temp).map_err(|_| "资源临时目录不可用。")?;
        let state_dir = voice_desktop_common::workspace::safe_path(&context.data, "state")?;
        fs::create_dir_all(&state_dir).map_err(|_| "资源任务状态目录不可用。")?;
        let jobs_path = voice_desktop_common::workspace::safe_path(&context.data, "state/resource-jobs.json")?;
        let jobs = Self::load_jobs(&jobs_path)?;
        let manager = Self {
            context,
            app,
            service_control,
            jobs: Mutex::new(jobs),
            jobs_changed: Condvar::new(),
            jobs_path,
            activation: Mutex::new(()),
            environment: Mutex::new(()),
            reviews: Mutex::new(HashMap::new()),
            resume_pending: Mutex::new(false),
            validation_failures:Mutex::new(HashMap::new()),
            prepared_environment:Mutex::new(None),
        };
        manager.persist_jobs()?;
        Ok(manager)
    }

    /// Inventory（资源清单）：normalize the Python resource result for the frontend.
    pub fn inventory(&self) -> Result<Vec<Value>, String> {
        let mut rows=voice_desktop_common::resources::inventory(&self.context)?;
        let failures=self.validation_failures.lock().map_err(|_|"组件校验状态不可用。")?;
        for row in &mut rows{if let Some(reason)=row["resource_id"].as_str().and_then(|id|failures.get(id)){
            row["can_start"]=json!(false);row["files_installed"]=json!(false);row["installation_state"]=json!("incomplete");row["error"]=json!(reason);
        }}
        if rows.iter().skip(1).any(|row|row["can_start"]!=true){if let Some(server)=rows.first_mut(){server["can_start"]=json!(false);}}
        Ok(rows)
    }
    pub fn record_validation(&self,readiness:&Value){
        if let Ok(mut failures)=self.validation_failures.lock(){failures.clear();
            for row in readiness["missing"].as_array().into_iter().flatten(){
                if let (Some(id),Some(reason))=(row["resource_id"].as_str(),row["reason"].as_str()){
                    if reason.contains("完整摘要复核失败"){failures.insert(id.to_owned(),reason.to_owned());}
                }
            }
        }
    }

    pub fn context(&self)->LaunchContext {self.context.clone()}
    pub fn probe_gpu(self:&Arc<Self>)->Result<Value,String>{
        let job_id="gpu-check".to_string();
        self.start_job(job_id.clone(),"gpu-environment".into(),"检测登记显卡环境".into(),true,move|manager,id|{
            let context=&manager.context;
            let path=context.data.join("config/gpu-environment.json");
            let mut registration=voice_desktop_common::resources::load(&path).map_err(|message|JobError::Failed{message,installed:vec![]})?;
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
                ("sensevoice",vec!["voice-core","torch-cpu","onnx-cpu"]),("smart_turn",vec!["voice-core","torch-cpu","onnx-cpu"]),
                ("wespeaker",vec!["voice-core","torch-cpu","onnx-cpu","sherpa-cpu"]),("kws",vec!["voice-core","torch-cpu","onnx-cpu","sherpa-cpu"])].into_iter().enumerate(){
                if cancel.load(Ordering::Acquire){return Err(JobError::Cancelled);}
                manager.set_phase(&id,&format!("实际推理检测：{model}（模型）"),true,index as u64,Some(6));
                let names:std::collections::BTreeSet<String>=groups.iter().flat_map(|group|lock["groups"][group].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned)).collect();
                let args=vec!["--names".into(),serde_json::to_string(&names).unwrap(),"--model".into(),model.into(),"--program".into(),context.program.to_string_lossy().into_owned(),"--install-root".into(),context.install.to_string_lossy().into_owned()];
                let resource=match model {"paraformer"=>"paraformer-streaming","voice_vad"=>"fsmn-vad","sensevoice"=>"sensevoice-small","smart_turn"=>"smart-turn", "wespeaker"=>"wespeaker-cnceleb-resnet34",_=>"kws-sherpa-onnx"};
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
            registration["environment_fingerprint"]=json!(voice_desktop_common::runtime::fingerprint(python.parent().unwrap()).map_err(|message|JobError::Failed{message,installed:vec![]})?);
            registration["models_fingerprint"]=json!(voice_desktop_common::runtime::fingerprint(&context.install.join("models")).map_err(|message|JobError::Failed{message,installed:vec![]})?);
            voice_desktop_common::workspace::atomic_json(&path,&registration).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let _=lease.finish();Ok(vec!["gpu-environment".into()])
        })?;Ok(json!({"job_id":job_id}))
    }
    pub fn snapshots(&self)->Vec<Value>{let mut rows=self.jobs.lock().unwrap().values().map(|job|event_value(&job.snapshot)).collect::<Vec<_>>();rows.sort_by_key(|row|row["created_at"].as_u64().unwrap_or(0));rows}
    pub fn has_active_jobs(&self)->bool{self.jobs.lock().map(|jobs|jobs.values().any(|job|!terminal(&job.snapshot.state))).unwrap_or(true)}
    pub fn configure_runtime(self:&Arc<Self>,group:String)->Result<Value,String>{
        let job_id=format!("env-{group}");
        self.start_job(job_id.clone(),"voice-server".into(),"正在配置语音环境".into(),true,move|manager,id|{
            let cancel=manager.cancel_signal(&id)?;
            let mut progress=|phase:&str,done:u64,total:u64|manager.set_phase(&id,phase,!phase.starts_with("启用"),done,Some(total));
            let _environment=acquire_environment_lock(&manager.environment,&cancel,&mut progress).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let result=manager.prepare_environment(&id,&group,cancel,&mut progress);
            result.map_err(|message|JobError::Failed{message,installed:vec![]})
        })?;Ok(json!({"job_id":job_id}))
    }
    pub fn import_runtime(self:&Arc<Self>)->Result<Value,String>{
        let job_id=new_job_id("env-import");
        self.start_job(job_id.clone(),"voice-server".into(),"选择语音环境目录".into(),true,move|manager,id|{
            let source=voice_desktop_common::native_ui::choose_folder("选择已准备的程序私有 Python（后端环境）目录")
                .map_err(|message|JobError::Failed{message,installed:vec![]})?.ok_or(JobError::Cancelled)?;
            let cancel=manager.cancel_signal(&id)?;
            let mut progress=|phase:&str,done:u64,total:u64|manager.set_phase(&id,phase,!phase.starts_with("启用"),done,Some(total));
            let _environment=acquire_environment_lock(&manager.environment,&cancel,&mut progress).map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let result=voice_desktop_common::runtime::import_with_activation(&manager.context,&source,cancel,&mut progress,&mut|stage,relative|manager.activate_resource(&id,stage,relative));
            result.map_err(|message|JobError::Failed{message,installed:vec![]})
        })?;Ok(json!({"job_id":job_id}))
    }

    /// Download（下载）：run a fixed plan, retaining a stable task ID for resuming files.
    pub fn download(self: &Arc<Self>, resource_id: String) -> Result<Value, String> {
        if resource_id=="voice-server"{return self.configure_runtime("all".into());}
        if !valid_resource_id(&resource_id) {
            return Err("资源编号格式无效。".into());
        }
        let job_id = format!("dl-{resource_id}"); // Same resource uses the same task ID across retries.
        self.start_job(
            job_id.clone(),
            resource_id.clone(),
            "正在准备资源下载".into(),
            true,
            move |manager, job_id| manager.run_download(&job_id, &resource_id),
        )?;
        Ok(json!({ "job_id": job_id }))
    }

    /// Import（导入）：open the native picker on a worker thread and install selected sources.
    pub fn import(self: &Arc<Self>, resource_id: Option<String>) -> Result<Value, String> {
        if resource_id.as_deref()==Some("voice-server"){return self.import_runtime();}
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

    /// OpenSource（打开来源）：read only the locally pinned URL from the packaged catalog.
    pub fn open_source(&self, resource_id: &str) -> Result<(), String> {
        if resource_id=="voice-server"{
            #[cfg(windows)]
            return voice_desktop_common::windows::open_url("https://www.python.org/downloads/release/python-31210/").map_err(|_|"无法打开 Python（后端解释器）官方来源。".into());
            #[cfg(not(windows))]
            return Err("打开资源来源仅支持 Windows（操作系统）。".into());
        }
        if !valid_resource_id(resource_id) {
            return Err("资源编号格式无效。".into());
        }
        let catalog_path = self.context.program.join("config/resources/catalog.json");
        let bytes = fs::read(catalog_path).map_err(|_| "资源来源清单不可用。")?;
        let catalog: Value = serde_json::from_slice(&bytes).map_err(|_| "资源来源清单格式无效。")?;
        let entries = catalog["resources"].as_array().ok_or("资源来源清单格式无效。")?;
        let entry = entries.iter().find(|item| item["resource_id"].as_str() == Some(resource_id))
            .ok_or("未找到该资源的固定来源。")?;
        let url_text = entry["source"]["page_url"].as_str()
            .or_else(|| entry["source"]["url"].as_str()).ok_or("该资源没有网页来源。")?;
        let url: tauri::Url = url_text.parse().map_err(|_| "资源来源地址无效。")?;
        if url.scheme() != "https"
            || url.query().is_some()
            || url.fragment().is_some()
            || !matches!(url.host_str(), Some("github.com" | "huggingface.co" | "modelscope.cn"))
        {
            return Err("资源来源地址不符合固定平台约束。".into());
        }
        #[cfg(windows)]
        { voice_desktop_common::windows::open_url(url.as_str()).map_err(|_| "无法打开系统默认浏览器。".into()) }
        #[cfg(not(windows))]
        { let _ = url; Err("打开资源来源仅支持 Windows（操作系统）。".into()) }
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
            for job in jobs.values_mut() {
                if terminal(&job.snapshot.state) || !job.snapshot.can_cancel { continue; }
                job.cancel.store(true, Ordering::Release);
                job.snapshot.state = "cancelling".into();
                job.snapshot.phase = "工作区切换前正在停止可取消阶段".into();
                job.snapshot.can_cancel = false;
                job.snapshot.revision=job.snapshot.revision.saturating_add(1);
                if let Some(child) = job.child.clone() { children.push(child); }
                events.push(event_value(&job.snapshot));
            }
            self.persist_locked(&jobs)?;
        }
        for child in children {
            if let Ok(mut process) = child.lock() { process.kill(); }
        }
        for event in events { let _ = self.app.emit(PROGRESS_EVENT, event); }

        let deadline = Instant::now() + Duration::from_secs(60);
        let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
        while jobs.values().any(|job| !terminal(&job.snapshot.state)) {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() { return Err("资源仍在原子安装，当前不能切换工作区。".into()); }
            let (next, timeout) = self.jobs_changed.wait_timeout(jobs, remaining)
                .map_err(|_| "资源任务状态不可用。")?;
            jobs = next;
            if timeout.timed_out() && jobs.values().any(|job| !terminal(&job.snapshot.state)) {
                return Err("资源仍在原子安装，当前不能切换工作区。".into());
            }
        }
        Ok(())
    }

    fn run_download(self: &Arc<Self>, job_id: &str, resource_id: &str) -> Result<Vec<String>, JobError> {
        if self.cancelled(job_id){return Err(JobError::Cancelled);}
        let cancel=self.cancel_signal(job_id)?;
        let mut progress=|phase:&str,done:u64,total:u64|{
            if phase.starts_with("启用")||phase.starts_with("正在启用"){let _=self.begin_non_cancellable_phase(job_id,phase,done,Some(total));}
            else{self.set_phase(job_id,phase,true,done,Some(total));}
        };
        // dependency_group（依赖组）模型先校验并配置完整运行环境，再下载自身模型文件。
        let mut installed=self.configure_resource_dependencies(job_id,resource_id,cancel.clone(),&mut progress)
            .map_err(|message|JobError::Failed{message,installed:vec![]})?;
        let result=voice_desktop_common::resources::download_resource_with_activation(&self.context,resource_id,cancel,&mut progress,&mut|stage,relative|self.activate_resource(job_id,stage,relative))
            .map_err(|message|JobError::Failed{message,installed:installed.clone()})?;
        installed.extend(result);Ok(installed)
    }

    fn run_import(self: &Arc<Self>, job_id: &str, resource_id: Option<String>) -> Result<Vec<String>, JobError> {
        let sources=voice_desktop_common::native_ui::choose_resource(resource_id.as_deref()).map_err(|message|JobError::Failed{message,installed:vec![]})?;
        if sources.is_empty(){return Err(JobError::Cancelled);}
        let mut installed=vec![];
        let mut progress=|phase:&str,done:u64,total:u64|{
            if phase.starts_with("正在启用")||phase.starts_with("启用"){let _=self.begin_non_cancellable_phase(job_id,phase,done,Some(total));}
            else{self.set_phase(job_id,phase,true,done,Some(total));}
        };
        if let Some(id)=resource_id.as_deref(){
            let dependencies=self.configure_resource_dependencies(job_id,id,self.cancel_signal(job_id)?,&mut progress)
                .map_err(|message|JobError::Failed{message,installed:installed.clone()})?;
            installed.extend(dependencies);
        }
        for source in sources {
            let result=voice_desktop_common::resources::import_review_with_activation(&self.context,&source,resource_id.as_deref(),self.cancel_signal(job_id)?,&mut progress,
                &mut|items|self.review_import(job_id,items),&mut|stage,relative|self.activate_resource(job_id,stage,relative))
                .map_err(|message|JobError::Failed{message,installed:installed.clone()})?;
            installed.extend(result);
        }
        Ok(installed)
    }

    fn review_import(&self,job_id:&str,items:&[Value])->Result<(),String>{
        self.update_job(job_id,|snapshot|{snapshot.state="waiting".into();snapshot.phase="整包校验通过，请确认识别结果".into();snapshot.import_preview=Some(json!(items));snapshot.can_cancel=true;});
        loop {
            if self.cancelled(job_id){return Err("已取消导入".into());}
            if let Some(accepted)=self.reviews.lock().map_err(|e|e.to_string())?.remove(job_id){
                self.update_job(job_id,|snapshot|{snapshot.import_preview=None;snapshot.state="running".into();});
                return if accepted{Ok(())}else{Err("已取消导入".into())};
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }
    pub fn confirm_import(&self,job_id:&str,accepted:bool)->Result<(),String>{
        if !self.jobs.lock().map_err(|e|e.to_string())?.get(job_id).is_some_and(|job|job.snapshot.import_preview.is_some()) {return Err("此任务没有待确认导入内容".into());}
        if !accepted{return self.cancel(job_id);}
        self.reviews.lock().map_err(|e|e.to_string())?.insert(job_id.into(),true);Ok(())
    }
    fn cli_args(&self, command: &str, args: &[OsString]) -> Vec<OsString> {
        let mut tail = vec![OsString::from(command)];
        tail.extend([OsString::from("--root"), self.context.install.clone().into_os_string(),
            OsString::from("--program"), self.context.program.clone().into_os_string()]);
        tail.extend(args.iter().cloned());
        tail
    }

    fn run_stream_cli<F>(&self, job_id: Option<&str>, script_name: &str, args: &[OsString], on_progress: F) -> Result<Value, ProcessError>
    where F: FnMut(&str, u64, u64) -> Result<(), String> {
        let script = self.context.program.join("scripts").join(script_name);
        self.run_stream_script(job_id.unwrap_or(""), &script, args, on_progress)
    }

    fn run_stream_script<F>(&self, job_id: &str, script: &Path, args: &[OsString], on_progress: F) -> Result<Value, ProcessError>
    where F: FnMut(&str, u64, u64) -> Result<(), String> {
        self.run_stream_script_with_ack(job_id, script, args, None, on_progress)
    }

    fn run_stream_script_with_ack<F>(&self, job_id: &str, script: &Path, args: &[OsString], native_ack: Option<(&str, String)>, mut on_progress: F) -> Result<Value, ProcessError>
    where F: FnMut(&str, u64, u64) -> Result<(), String> {
        if !script.is_file() {
            return Err(ProcessError { message: "资源管理脚本缺失。".into(), installed: vec![], cancelled: false });
        }
        let mut command = Command::new(&self.context.python);
        // -I（隔离模式）忽略 PYTHON* 环境变量；编码与缓存必须通过解释器参数明确指定。
        command.arg("-I").arg("-B").arg("-X").arg("utf8")
            .arg("-X").arg(format!("pycache_prefix={}",self.context.install.join("cache/python").display()))
            .arg(script).args(args)
            .current_dir(&self.context.data)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .stdin(if native_ack.is_some() { Stdio::piped() } else { Stdio::null() });
        command.env_clear();
        self.context.configure_command(&mut command);
        #[cfg(windows)] {
            // SystemRoot（Windows 系统目录变量）供 Python 的 Windows socket/asyncio 模块加载系统网络提供程序。
            if let Some(system_root) = std::env::var_os("SystemRoot") {
                command.env("SystemRoot", system_root);
            }
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command.spawn().map_err(|_| ProcessError {
            message: "无法启动固定 Python 资源管理器。".into(), installed: vec![], cancelled: false,
        })?;
        let stdout = child.stdout.take().ok_or_else(|| ProcessError {
            message: "资源管理器输出不可用。".into(), installed: vec![], cancelled: false,
        })?;
        let mut child_stdin = child.stdin.take();
        let child = ControlledProcess::new(child).map_err(|_| ProcessError {
            message: "无法建立可控资源进程。".into(), installed: vec![], cancelled: false,
        })?;
        let child = Arc::new(Mutex::new(child));
        if !job_id.is_empty() {
            self.attach_child(job_id, child.clone());
            if self.cancelled(job_id) {
                if let Ok(mut process) = child.lock() { process.kill(); }
            }
        }
        let mut final_record = None;
        let read_result = self.consume_stdout(job_id, stdout, &mut on_progress, &mut final_record,
            native_ack.as_ref().map(|(phase, _)| *phase), native_ack.as_ref().map(|(_, value)| value.as_str()), &mut child_stdin);
        if read_result.is_err() {
            if let Ok(mut process) = child.lock() { process.kill(); }
        }
        let status = child.lock().map_err(|_| ProcessError {
            message: "资源管理进程状态不可用。".into(), installed: vec![], cancelled: false,
        })?.wait().map_err(|_| ProcessError {
            message: "资源管理进程无法正常结束。".into(), installed: vec![], cancelled: false,
        })?;
        if !job_id.is_empty() { self.detach_child(job_id); }
        if let Err(message) = read_result {
            let cancelled = !job_id.is_empty() && self.cancelled(job_id);
            return Err(ProcessError { message, installed: Vec::new(), cancelled });
        }
        if !job_id.is_empty() && self.cancelled(job_id) {
            return Err(ProcessError { message: "资源任务已取消。".into(), installed: Vec::new(), cancelled: true });
        }
        let Some(record) = final_record else {
            return Err(ProcessError { message: if status.success() { "资源管理器没有返回结果。" } else { "资源管理操作失败。" }.into(), installed: vec![], cancelled: false });
        };
        response_result(record)
    }

    fn consume_stdout<F>(&self, job_id: &str, stdout: ChildStdout, on_progress: &mut F, final_record: &mut Option<Value>,
        ack_phase: Option<&str>, ack_json: Option<&str>, child_stdin: &mut Option<std::process::ChildStdin>) -> Result<(), String>
    where F: FnMut(&str, u64, u64) -> Result<(), String> {
        for line in BufReader::new(stdout).lines() {
            let line = line.map_err(|_| "资源管理器输出读取失败。".to_string())?;
            let Ok(record) = serde_json::from_str::<Value>(&line) else { continue; };
            if let Some(phase) = record["phase"].as_str() {
                let done = record["completed_bytes"].as_u64().unwrap_or(0);
                let total = record["total_bytes"].as_u64().unwrap_or(0);
                on_progress(phase, done, total)?;
                if ack_phase == Some(phase) {
                    use std::io::Write;
                    let writer = child_stdin.as_mut().ok_or_else(|| "DSH 安装器确认通道不可用。".to_string())?;
                    let ack = ack_json.ok_or_else(|| "DSH 安装器确认内容缺失。".to_string())?;
                    writer.write_all(ack.as_bytes()).and_then(|_| writer.write_all(b"\n")).and_then(|_| writer.flush())
                        .map_err(|_| "DSH 运行时切换确认失败。".to_string())?;
                    child_stdin.take();
                }
                continue;
            }
            if record.get("ok").is_some() { *final_record = Some(record); }
        }
        let _ = job_id;
        Ok(())
    }

    fn pause_service(&self) -> Result<ServiceLease<'_>, String> {
        let guard = self.activation.lock().map_err(|_| "资源激活锁不可用。")?;
        ServiceLease::pause(&self.service_control, guard)
    }

    fn remember_service_was_running(&self,was_running:bool)->Result<(),String>{
        if was_running{*self.resume_pending.lock().map_err(|_|"服务恢复意图不可用。")?=true;}
        Ok(())
    }

    fn pending_service_resume(&self)->Result<bool,String>{Ok(*self.resume_pending.lock().map_err(|_|"服务恢复意图不可用。")?)}

    fn all_resources_ready(&self)->Result<bool,String>{
        // 服务入口随后执行全量校验；这里仅判定能否进入该入口，避免恢复时连续读取两遍全部组件。
        Ok(voice_desktop_common::resources::inventory(&self.context)?.iter().all(|row|row["can_start"]==true))
    }

    fn maybe_resume_service(&self,lease:&mut ServiceLease<'_>)->Result<bool,String>{
        if !self.pending_service_resume()?{lease.keep_stopped();return Ok(false);}
        match self.all_resources_ready(){
            Ok(false)=>{lease.keep_stopped();return Ok(false);}
            Err(error)=>{lease.keep_stopped();return Err(format!("无法确认全部组件已就绪，语音服务保持停止：{error}"));}
            Ok(true)=>{}
        }
        lease.resume_service()?;
        Ok(true)
    }

    fn rollback_failed_activation(&self,lease:&mut ServiceLease<'_>,transaction:&mut voice_desktop_common::resources::PendingActivation,reason:&str)->String{
        if let Err(error)=lease.stop_partial(){
            lease.keep_stopped();
            return format!("{reason}；无法确认新服务已停止，资源回滚留待下次启动恢复：{error}");
        }
        if let Err(error)=voice_desktop_common::resources::rollback_activation(&self.context,transaction){
            lease.keep_stopped();
            return format!("{reason}；恢复原组件失败：{error}");
        }
        match self.maybe_resume_service(lease){
            Ok(resumed)=>{
                // prior-running intent（更新前正在运行的服务恢复意图）只有在旧状态恢复且服务确实重新启动后才能清除；
                // 若后续 transaction commit（事务提交）失败，仍需保留它以重新启动回滚后的旧服务。
                clear_resume_intent_if_resumed(&self.resume_pending,resumed);
                reason.to_owned()
            },
            Err(error)=>format!("{reason}；原组件已恢复，但语音服务未恢复：{error}"),
        }
    }

    fn settle_before_activation(&self,lease:&mut ServiceLease<'_>,reason:&str)->String{
        match self.maybe_resume_service(lease){
            Ok(resumed)=>{clear_resume_intent_if_resumed(&self.resume_pending,resumed);reason.to_owned()},
            Err(error)=>format!("{reason}；语音服务未恢复：{error}")
        }
    }

    fn activate_resource(&self,job_id:&str,stage:&Path,relative:&str)->Result<(),String>{
        let mut lease=self.pause_service()?;
        if let Err(error)=self.remember_service_was_running(lease.was_running()){lease.keep_stopped();return Err(error);}
        let cancel=match self.cancel_signal(job_id){Ok(value)=>value,Err(_)=>return Err(self.settle_before_activation(&mut lease,"资源任务已丢失"))};
        if let Err(error)=voice_desktop_common::resources::check_cancel(&cancel){return Err(self.settle_before_activation(&mut lease,&error));}
        let mut transaction=match voice_desktop_common::resources::begin_activation(&self.context,stage,relative){
            Ok(value)=>value,Err(error)=>return Err(self.settle_before_activation(&mut lease,&error)),
        };
        let pending=match self.pending_service_resume(){Ok(value)=>value,Err(error)=>return Err(self.rollback_failed_activation(&mut lease,&mut transaction,&error))};
        let resumed=if pending {
            match self.maybe_resume_service(&mut lease){
                Ok(value)=>value,Err(error)=>return Err(self.rollback_failed_activation(&mut lease,&mut transaction,&error)),
            }
        }else{lease.keep_stopped();false};
        match voice_desktop_common::resources::commit_activation(&self.context,&mut transaction){
            Ok(())=>{
                clear_resume_intent_if_resumed(&self.resume_pending,resumed);
                if relative=="runtimes/python"{*self.prepared_environment.lock().map_err(|_|"环境复用状态不可用。")?=None;}
                self.publish_installed(job_id,relative);
                Ok(())
            },
            Err(error) if transaction.is_committed()=>{
                lease.keep_stopped();
                clear_resume_intent_if_resumed(&self.resume_pending,resumed);
                Err(format!("资源已提交并启用，但旧备份清理失败：{error}"))
            }
            Err(error)=>Err(self.rollback_failed_activation(&mut lease,&mut transaction,&error)),
        }
    }

    fn publish_installed(&self,job_id:&str,relative:&str){
        let resource_id=if relative=="runtimes/python"{Some("voice-server".to_string())}else if relative=="runtimes/dsh"{Some("dsh".to_string())}
            else{voice_desktop_common::resources::specs(&self.context).ok().into_iter().flatten().find(|item|item["destination"].as_str().is_some_and(|path|relative==format!("models/{path}")))
                .and_then(|item|item["resource_id"].as_str().map(str::to_owned))};
        if let Some(id)=resource_id{
            if let Ok(mut failures)=self.validation_failures.lock(){failures.remove(&id);}
            self.update_job(job_id,|snapshot|{if !snapshot.installed_resources.contains(&id){snapshot.installed_resources.push(id);}});
        }
    }

    fn current_environment_identity(&self)->Result<PreparedEnvironment,String>{
        let root=self.context.install.join("runtimes/python");
        PreparedEnvironment::read(&root)
    }
    fn prepare_environment(&self,job_id:&str,group:&str,cancel:Arc<AtomicBool>,progress:&mut voice_desktop_common::resources::Progress<'_>)->Result<Vec<String>,String>{
        voice_desktop_common::resources::check_cancel(&cancel)?;
        let ready=voice_desktop_common::runtime::status(&self.context)?["full_ready"]==true;
        let verified=self.prepared_environment.lock().map_err(|_|"环境复用状态不可用。")?.clone();
        if ready&&verified.is_some(){
            progress("核对本次已验证共享环境是否变化",0,0);
            let current=self.current_environment_identity();voice_desktop_common::resources::check_cancel(&cancel)?;
            if current.as_ref().ok()==verified.as_ref(){
                progress("复用本次已验证完整语音环境",1,1);return Ok(vec!["voice-server".into()]);
            }
            *self.prepared_environment.lock().map_err(|_|"环境复用状态不可用。")?=None;
        }
        let result=voice_desktop_common::runtime::configure_with_activation(&self.context,group,cancel.clone(),progress,&mut|stage,relative|self.activate_resource(job_id,stage,relative))?;
        voice_desktop_common::resources::check_cancel(&cancel)?;
        *self.prepared_environment.lock().map_err(|_|"环境复用状态不可用。")?=Some(self.current_environment_identity()?);
        Ok(result)
    }

    fn start_job<F>(self: &Arc<Self>, job_id: String, resource_id: String, phase: String, can_cancel: bool, worker: F) -> Result<(), String>
    where F: FnOnce(Arc<ResourceManager>, String) -> Result<Vec<String>, JobError> + Send + 'static {
        {
            let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
            if jobs.get(&job_id).is_some_and(|job| !terminal(&job.snapshot.state)) {
                return Ok(());
            }
            if jobs.values().any(|job|job.snapshot.resource_id==resource_id&&!terminal(&job.snapshot.state)){
                return Err("此组件已有下载或导入任务，请等待该任务完成。".into());
            }
            let created_at=unix_millis().max(jobs.get(&job_id).map(|job|job.snapshot.created_at.saturating_add(1)).unwrap_or(0));
            let snapshot = JobSnapshot { job_id: job_id.clone(), resource_id, state: "queued".into(), phase,
                completed_bytes: 0, total_bytes: None, error: None, can_cancel, installed_resources: vec![],
                import_preview: None, created_at, revision: 1 };
            jobs.insert(job_id.clone(), JobControl { snapshot: snapshot.clone(), cancel: Arc::new(AtomicBool::new(false)), child: None,
                last_event:Instant::now(),last_persist:Instant::now() });
            self.persist_locked(&jobs)?;
            let _ = self.app.emit(PROGRESS_EVENT, event_value(&snapshot));
        }
        let manager = Arc::clone(self);
        let thread_id = job_id.clone();
        thread::Builder::new().name(format!("resource-{thread_id}")).spawn(move || {
            manager.set_phase(&thread_id, "正在运行资源任务", true, 0, None);
            let outcome = if manager.cancelled(&thread_id) { Err(JobError::Cancelled) }
                else { std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| worker(Arc::clone(&manager), thread_id.clone())))
                    .unwrap_or_else(|_| Err(JobError::Failed { message: "资源任务异常退出；未输出敏感诊断信息。".into(), installed: vec![] })) };
            match outcome {
                Ok(installed) => manager.finish_job(&thread_id, Ok(installed)),
                Err(JobError::Failed{installed,..}) if installed.is_empty() && manager.cancelled(&thread_id) => manager.finish_job(&thread_id,Err(JobError::Cancelled)),
                Err(error) => manager.finish_job(&thread_id, Err(error)),
            }
        }).map_err(|_| {
            self.finish_job(&job_id, Err(JobError::Failed { message: "无法建立资源任务线程。".into(), installed: vec![] }));
            "无法建立资源任务线程。".to_string()
        })?;
        Ok(())
    }

    fn finish_job(&self, job_id: &str, result: Result<Vec<String>, JobError>) {
        let (state, phase, error, installed) = match result {
            Ok(installed) => ("complete", "资源处理完成", None, installed),
            Err(JobError::Cancelled) => ("cancelled", "任务已取消；临时下载可供重试", None, vec![]),
            Err(JobError::Failed { message, installed }) => ("failed", "资源处理失败", Some(message), installed),
        };
        if let Ok(mut failures)=self.validation_failures.lock(){for id in &installed{failures.remove(id);}}
        self.update_job(job_id, |snapshot| {
            snapshot.state = state.into(); snapshot.phase = phase.into(); snapshot.error = error;
            snapshot.can_cancel = false; snapshot.installed_resources = installed;snapshot.import_preview=None;
        });
    }

    fn set_phase(&self, job_id: &str, phase: &str, can_cancel: bool, done: u64, total: Option<u64>) {
        self.update_job(job_id, |snapshot| {
            if snapshot.state == "cancelling" { return; }
            if !terminal(&snapshot.state) { snapshot.state = "running".into(); }
            snapshot.phase = phase.into(); snapshot.can_cancel = can_cancel;
            snapshot.completed_bytes = done; snapshot.total_bytes = total;
        });
    }

    fn begin_non_cancellable_phase(&self, job_id: &str, phase: &str, done: u64, total: Option<u64>) -> Result<(), String> {
        let event = {
            let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。".to_string())?;
            let job = jobs.get_mut(job_id).ok_or_else(|| "找不到该资源任务。".to_string())?;
            if job.cancel.load(Ordering::Acquire) || job.snapshot.state == "cancelling" {
                return Err("资源任务已取消。".into());
            }
            job.snapshot.state = "running".into();
            job.snapshot.phase = phase.into();
            job.snapshot.can_cancel = false;
            job.snapshot.completed_bytes = done;
            job.snapshot.total_bytes = total;
            job.snapshot.revision = job.snapshot.revision.saturating_add(1);
            job.last_event=Instant::now();job.last_persist=Instant::now();
            let event = event_value(&job.snapshot);
            self.persist_locked(&jobs)?;
            event
        };
        let _ = self.app.emit(PROGRESS_EVENT, event);
        Ok(())
    }

    fn update_job<F>(&self, job_id: &str, update: F)
    where F: FnOnce(&mut JobSnapshot) {
        let event = {
            let Ok(mut jobs) = self.jobs.lock() else { return; };
            let Some(job) = jobs.get_mut(job_id) else { return; };
            let previous_state=job.snapshot.state.clone();let previous_phase=job.snapshot.phase.clone();
            let previous_installed=job.snapshot.installed_resources.clone();
            update(&mut job.snapshot);
            job.snapshot.revision=job.snapshot.revision.saturating_add(1);
            let boundary=previous_state!=job.snapshot.state||previous_phase.split('（').next()!=job.snapshot.phase.split('（').next()||previous_installed!=job.snapshot.installed_resources;
            let publish=boundary||terminal(&job.snapshot.state)||job.last_event.elapsed()>=Duration::from_millis(100);
            let persist=boundary||terminal(&job.snapshot.state)||job.last_persist.elapsed()>=Duration::from_secs(1);
            let value=if publish{job.last_event=Instant::now();Some(event_value(&job.snapshot))}else{None};
            if persist{job.last_persist=Instant::now();let _=self.persist_locked(&jobs);}
            value
        };
        if let Some(event)=event{let _ = self.app.emit(PROGRESS_EVENT, event);}
        self.jobs_changed.notify_all();
    }

    fn persist_jobs(&self) -> Result<(), String> {
        let jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
        self.persist_locked(&jobs)
    }

    fn persist_locked(&self, jobs: &HashMap<String, JobControl>) -> Result<(), String> {
        let mut snapshots = jobs.values().map(|item| item.snapshot.clone()).collect::<Vec<_>>();
        snapshots.sort_by_key(|item| item.created_at);
        if snapshots.len() > MAX_PERSISTED_JOBS { snapshots.drain(0..snapshots.len() - MAX_PERSISTED_JOBS); }
        let value = json!({ "schema": 1, "jobs": snapshots.iter().map(event_value).collect::<Vec<_>>() });
        voice_desktop_common::workspace::atomic_json(&self.jobs_path, &value)
    }

    fn load_jobs(path: &Path) -> Result<HashMap<String, JobControl>, String> {
        if !path.exists() { return Ok(HashMap::new()); }
        let bytes = fs::read(path).map_err(|_| "资源任务记录无法读取。")?;
        let root: Value = serde_json::from_slice(&bytes).map_err(|_| "资源任务记录格式无效。")?;
        let records = root["jobs"].as_array().ok_or("资源任务记录格式无效。")?;
        let mut jobs = HashMap::new();
        for value in records {
            let (Some(job_id), Some(resource_id), Some(mut state), Some(mut phase)) = (
                value["job_id"].as_str(), value["resource_id"].as_str(), value["state"].as_str().map(str::to_owned), value["phase"].as_str().map(str::to_owned)) else { continue; };
            let mut error = value["error"].as_str().map(str::to_owned);
            let can_cancel = false;
            if !terminal(&state) {
                state = "failed".into(); phase = "应用退出时任务中断，可使用相同下载任务重试".into();
                error = Some("上次运行期间任务中断；固定下载任务可重试。".into());
            }
            let installed_resources = value["installed_resources"].as_array().map(|items| items.iter().filter_map(Value::as_str).map(str::to_owned).collect()).unwrap_or_default();
            let snapshot = JobSnapshot { job_id: job_id.into(), resource_id: resource_id.into(), state, phase,
                completed_bytes: value["completed_bytes"].as_u64().unwrap_or(0), total_bytes: value["total_bytes"].as_u64(),
                error, can_cancel, installed_resources, import_preview: None, created_at: value["created_at"].as_u64().unwrap_or(0),revision:value["revision"].as_u64().unwrap_or(0).saturating_add(1) };
            jobs.insert(snapshot.job_id.clone(), JobControl { snapshot, cancel: Arc::new(AtomicBool::new(false)), child: None,
                last_event:Instant::now(),last_persist:Instant::now() });
        }
        Ok(jobs)
    }

    fn attach_child(&self, job_id: &str, child: Arc<Mutex<ControlledProcess>>) {
        if let Ok(mut jobs) = self.jobs.lock() {
            if let Some(job) = jobs.get_mut(job_id) { job.child = Some(child); }
        }
    }

    fn detach_child(&self, job_id: &str) {
        if let Ok(mut jobs) = self.jobs.lock() {
            if let Some(job) = jobs.get_mut(job_id) { job.child = None; }
        }
    }

    fn cancel_signal(&self, job_id: &str) -> Result<Arc<AtomicBool>, JobError> {
        let jobs = self.jobs.lock().map_err(|_| JobError::Failed { message: "资源任务状态不可用。".into(), installed: vec![] })?;
        jobs.get(job_id).map(|job| job.cancel.clone()).ok_or_else(|| JobError::Failed { message: "找不到资源任务。".into(), installed: vec![] })
    }

    fn cancelled(&self, job_id: &str) -> bool {
        self.jobs.lock().ok().and_then(|jobs| jobs.get(job_id).map(|job| job.cancel.load(Ordering::Acquire))).unwrap_or(false)
    }

    fn configure_resource_dependencies(&self,job_id:&str,resource_id:&str,cancel:Arc<AtomicBool>,progress:&mut voice_desktop_common::resources::Progress<'_>)->Result<Vec<String>,String>{
        let spec=voice_desktop_common::resources::spec(&self.context,resource_id)?;
        let Some(group)=spec["dependency_group"].as_str() else{return Ok(vec![])};
        let _environment=acquire_environment_lock(&self.environment,&cancel,progress)?;
        let activation_job=job_id.to_owned();
        self.prepare_environment(&activation_job,group,cancel,progress)
    }
}

fn clear_resume_intent_if_resumed(resume_pending:&Mutex<bool>,resumed:bool){
    if resumed{if let Ok(mut pending)=resume_pending.lock(){*pending=false;}}
}

fn acquire_environment_lock<'a>(environment:&'a Mutex<()>,cancel:&AtomicBool,progress:&mut voice_desktop_common::resources::Progress<'_>)->Result<MutexGuard<'a,()>,String>{
    let mut reported_wait=false;
    loop{
        voice_desktop_common::resources::check_cancel(cancel)?;
        match environment.try_lock(){
            Ok(guard)=>return Ok(guard),
            Err(std::sync::TryLockError::Poisoned(_))=>return Err("语音环境配置锁不可用。".into()),
            Err(std::sync::TryLockError::WouldBlock)=>{
                if !reported_wait{progress("等待语音运行环境专用更新锁",0,0);reported_wait=true;}
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
        Ok(Self { control, _guard: guard, active: true, resume })
    }

    fn was_running(&self)->bool{self.resume}

    fn resume_service(&mut self)->Result<(),String>{
        match (self.control)(true){
            Ok(true)=>{self.active=false;Ok(())}
            Ok(false)=>{self.active=false;Err("语音服务启动命令已返回，但服务入口未报告就绪。".into())}
            Err(error)=>{self.active=false;Err(format!("语音服务未能恢复：{error}"))}
        }
    }

    fn stop_partial(&mut self)->Result<(),String>{
        (self.control)(false).map_err(|error|format!("无法停止恢复失败后可能残留的新服务：{error}"))?;
        self.active=false;Ok(())
    }

    fn keep_stopped(&mut self){self.active=false;}

    fn finish(mut self) -> Result<(), String> {
        if self.resume{self.resume_service()}else{self.keep_stopped();Ok(())}
    }
}

impl Drop for ServiceLease<'_> {
    fn drop(&mut self) {
        if self.active&&self.resume { let _ = (self.control)(true); }
    }
}

fn response_result(record: Value) -> Result<Value, ProcessError> {
    if record["ok"].as_bool() == Some(true) {
        return Ok(record.get("result").cloned().unwrap_or(Value::Null));
    }
    let error = &record["error"];
    let installed = error["installed_resource_ids"].as_array()
        .or_else(|| error["details"]["installed_resource_ids"].as_array())
        .map(|items| items.iter().filter_map(Value::as_str).map(str::to_owned).collect()).unwrap_or_default();
    let message = error["message"].as_str().filter(|value| value.len() <= 512)
        .unwrap_or("资源操作失败；敏感诊断信息已省略。").to_owned();
    Err(ProcessError { message, installed, cancelled: false })
}

fn event_value(snapshot: &JobSnapshot) -> Value {
    json!({
        "job_id": snapshot.job_id,
        "resource_id": snapshot.resource_id,
        "state": snapshot.state,
        "phase": snapshot.phase,
        "completed_bytes": snapshot.completed_bytes,
        "total_bytes": snapshot.total_bytes,
        "error": snapshot.error,
        "can_cancel": snapshot.can_cancel,
        "installed_resources": snapshot.installed_resources,
        "created_at": snapshot.created_at,
        "revision": snapshot.revision,
        "import_preview": snapshot.import_preview,
        "fields":{"created_at":"本次任务创建时间，Unix 毫秒","revision":"状态修订号，拒绝迟到事件","job_id":"任务编号","resource_id":"组件编号","state":"任务状态","phase":"当前阶段","completed_bytes":"已处理字节数","total_bytes":"总字节数","error":"失败说明","can_cancel":"是否允许取消","installed_resources":"本次启用的组件编号","import_preview":"导入确认内容"},
    })
}

fn terminal(state: &str) -> bool { matches!(state, "complete" | "completed" | "failed" | "cancelled" | "canceled") }

fn valid_resource_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 64 && value.bytes().enumerate().all(|(index, byte)| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || (index > 0 && byte == b'-')
    })
}

fn unix_millis() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis().min(u64::MAX as u128) as u64
}

fn new_job_id(prefix: &str) -> String {
    let counter = NEXT_JOB.fetch_add(1, Ordering::Relaxed);
    format!("{prefix}-{:x}-{:x}", unix_millis(), counter)
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

fn command_allowed(window: &WebviewWindow, desktop: &State<'_, Arc<super::Desktop>>) -> Result<(), String> {
    super::require_command(desktop.mode, super::DesktopCommand::FocusMain)?;
    super::trusted(window, desktop)
}

#[tauri::command]
pub fn resource_jobs(window:WebviewWindow,desktop:State<'_,Arc<super::Desktop>>,manager:State<'_,Arc<ResourceManager>>)->Result<Vec<Value>,String>{
    command_allowed(&window,&desktop)?;Ok(manager.snapshots())
}

#[tauri::command]
pub async fn resource_inventory(
    window: WebviewWindow,
    desktop: State<'_, Arc<super::Desktop>>,
    manager: State<'_, Arc<ResourceManager>>,
) -> Result<Vec<Value>, String> {
    command_allowed(&window, &desktop)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.inventory()).await.map_err(|_| "资源清单读取任务失败。")?
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
    tauri::async_runtime::spawn_blocking(move || manager.download(resource_id)).await.map_err(|_| "资源下载任务无法启动。")?
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
    tauri::async_runtime::spawn_blocking(move || manager.import(resource_id)).await.map_err(|_| "资源导入任务无法启动。")?
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
    tauri::async_runtime::spawn_blocking(move || manager.open_source(&resource_id)).await.map_err(|_| "打开资源来源任务失败。")?
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

#[tauri::command(rename_all="snake_case")]
pub fn resource_import_confirm(window:WebviewWindow,desktop:State<Arc<super::Desktop>>,manager:State<Arc<ResourceManager>>,job_id:String,accepted:bool)->Result<(),String>{
    super::trusted(&window,&desktop)?;manager.confirm_import(&job_id,accepted)
}

#[cfg(test)]
mod service_tests {
    use super::*;

    #[test]
    fn prepared_environment_identity_rejects_changed_manifest_and_file_collection(){
        let root=std::env::temp_dir().join(format!("voice-environment-reuse-{}",voice_desktop_common::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        fs::write(root.join("python.exe"),b"fixture").unwrap();voice_desktop_common::workspace::atomic_json(&root.join("manifest.json"),&json!({"files":[]})).unwrap();
        let original=PreparedEnvironment::read(&root).unwrap();assert!(PreparedEnvironment::read(&root).unwrap()==original);
        voice_desktop_common::workspace::atomic_json(&root.join("manifest.json"),&json!({"files":[],"build":"changed"})).unwrap();assert!(PreparedEnvironment::read(&root).unwrap()!=original);
        let current=PreparedEnvironment::read(&root).unwrap();fs::write(root.join("changed.dll"),b"new file").unwrap();assert!(PreparedEnvironment::read(&root).unwrap()!=current);
        fs::remove_file(root.join("manifest.json")).unwrap();assert!(PreparedEnvironment::read(&root).is_err());fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn persisted_jobs_keep_revision_order_and_interrupted_tasks_become_terminal(){
        let root=std::env::temp_dir().join(format!("voice-job-revision-{}",voice_desktop_common::workspace::new_id()));fs::create_dir_all(&root).unwrap();let path=root.join("jobs.json");
        voice_desktop_common::workspace::atomic_json(&path,&json!({"schema":1,"jobs":[{
            "job_id":"dl-model","resource_id":"model","state":"running","phase":"正在下载","created_at":100,"revision":7,"completed_bytes":50,"total_bytes":100
        }]})).unwrap();
        let jobs=ResourceManager::load_jobs(&path).unwrap();let event=event_value(&jobs["dl-model"].snapshot);
        assert_eq!(event["state"],"failed");assert_eq!(event["revision"],8);assert_eq!(event["created_at"],100);assert_eq!(event["can_cancel"],false);
        assert!(event["fields"]["revision"].as_str().unwrap().contains("修订号"));fs::remove_dir_all(root).unwrap();
    }

    fn control(running: bool) -> (ServiceControl, Arc<Mutex<Vec<bool>>>) {
        let calls=Arc::new(Mutex::new(vec![]));let log=calls.clone();
        let state=Arc::new(AtomicBool::new(running));
        (Arc::new(move|start|{log.lock().unwrap().push(start);if start{state.store(true,Ordering::Release);Ok(true)}else{Ok(state.swap(false,Ordering::AcqRel))}}),calls)
    }

    #[test]
    fn component_management_does_not_start_service_after_install() {
        let (control,calls)=control(false);let lock=Mutex::new(());
        ServiceLease::pause(&control,lock.lock().unwrap()).unwrap().finish().unwrap();
        assert_eq!(*calls.lock().unwrap(),vec![false]);
    }

    #[test]
    fn activation_restores_running_service_exactly_once() {
        let (control,calls)=control(true);let lock=Mutex::new(());
        ServiceLease::pause(&control,lock.lock().unwrap()).unwrap().finish().unwrap();
        assert_eq!(*calls.lock().unwrap(),vec![false,true]);
    }

    #[test]
    fn cancelled_activation_restores_only_previously_running_service() {
        for running in [false,true] {
            let (control,calls)=control(running);let lock=Mutex::new(());
            drop(ServiceLease::pause(&control,lock.lock().unwrap()).unwrap());
            assert_eq!(*calls.lock().unwrap(),if running{vec![false,true]}else{vec![false]});
        }
    }

    #[test]
    fn pending_update_keeps_previously_running_service_stopped(){
        let (control,calls)=control(true);let lock=Mutex::new(());
        let mut lease=ServiceLease::pause(&control,lock.lock().unwrap()).unwrap();
        assert!(lease.was_running());lease.keep_stopped();drop(lease);
        assert_eq!(*calls.lock().unwrap(),vec![false]);
    }

    #[test]
    fn preparation_failure_clears_resume_intent_only_when_service_restarts(){
        let pending=Mutex::new(true);
        clear_resume_intent_if_resumed(&pending,false);assert!(*pending.lock().unwrap());
        clear_resume_intent_if_resumed(&pending,true);assert!(!*pending.lock().unwrap());
    }

    #[test]
    fn environment_preparation_lock_serializes_and_wait_is_cancellable(){
        let environment=Mutex::new(());let held=environment.lock().unwrap();
        let waiting=Arc::new(AtomicBool::new(false));let cancel=Arc::new(AtomicBool::new(false));
        std::thread::scope(|scope|{
            let observed=waiting.clone();let cancel_waiter=cancel.clone();let gate=&environment;
            let worker=scope.spawn(move||{
                let mut progress=|phase:&str,_,_|{if phase.contains("等待"){observed.store(true,Ordering::Release);}};
                let error=acquire_environment_lock(gate,&cancel_waiter,&mut progress).unwrap_err();
                assert!(error.contains("取消"));
            });
            for _ in 0..100{if waiting.load(Ordering::Acquire){break;}thread::sleep(Duration::from_millis(10));}
            assert!(waiting.load(Ordering::Acquire),"第二个环境准备任务必须显示等待锁阶段");
            cancel.store(true,Ordering::Release);worker.join().unwrap();
        });
        drop(held);
        let mut progress=|_:&str,_,_|{};
        assert!(acquire_environment_lock(&environment,&AtomicBool::new(false),&mut progress).is_ok());
    }
}
