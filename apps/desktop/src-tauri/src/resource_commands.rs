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

pub type ServiceControl = Arc<dyn Fn(bool) -> Result<(), String> + Send + Sync>;

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
}

struct JobControl {
    snapshot: JobSnapshot,
    cancel: Arc<AtomicBool>, // cancel（受控线程取消信号）。
    child: Option<Arc<Mutex<ControlledProcess>>>, // child（仅用于可取消阶段的受控进程）。
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

/// ResourceManager（资源管理器）：owns resource jobs and talks only to fixed local tools.
pub struct ResourceManager {
    context: LaunchContext,
    app: AppHandle,
    service_control: ServiceControl,
    jobs: Mutex<HashMap<String, JobControl>>,
    jobs_changed: Condvar, // jobs_changed（资源任务状态变化通知）。
    jobs_path: PathBuf,
    reviews: Mutex<HashMap<String,bool>>, // reviews（用户对导入内容的选择）。
    activation: Mutex<()>, // activation（串行化服务停机与资源激活）。
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
            reviews: Mutex::new(HashMap::new()),
        };
        manager.persist_jobs()?;
        Ok(manager)
    }

    /// Inventory（资源清单）：normalize the Python resource result for the frontend.
    pub fn inventory(&self) -> Result<Vec<Value>, String> {
        voice_desktop_common::resources::inventory(&self.context)
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
    pub fn snapshots(&self)->Vec<Value>{self.jobs.lock().unwrap().values().map(|job|event_value(&job.snapshot)).collect()}
    pub fn configure_runtime(self:&Arc<Self>,group:String)->Result<Value,String>{
        let job_id=format!("env-{group}");
        self.start_job(job_id.clone(),"voice-server".into(),"正在配置语音环境".into(),true,move|manager,id|{
            let lease=manager.pause_service().map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let cancel=manager.cancel_signal(&id)?;
            let result=voice_desktop_common::runtime::configure(&manager.context,&group,cancel,&mut|phase,done,total|manager.set_phase(&id,phase,!phase.starts_with("启用"),done,Some(total)));
            let _=lease.finish();
            result.map_err(|message|JobError::Failed{message,installed:vec![]})
        })?;Ok(json!({"job_id":job_id}))
    }
    pub fn import_runtime(self:&Arc<Self>)->Result<Value,String>{
        let job_id=new_job_id("env-import");
        self.start_job(job_id.clone(),"voice-server".into(),"选择语音环境目录".into(),true,move|manager,id|{
            let source=voice_desktop_common::native_ui::choose_folder("选择已准备的程序私有 Python（后端环境）目录")
                .map_err(|message|JobError::Failed{message,installed:vec![]})?.ok_or(JobError::Cancelled)?;
            let lease=manager.pause_service().map_err(|message|JobError::Failed{message,installed:vec![]})?;
            let result=voice_desktop_common::runtime::import(&manager.context,&source,manager.cancel_signal(&id)?,&mut|phase,done,total|manager.set_phase(&id,phase,!phase.starts_with("启用"),done,Some(total)));
            let _=lease.finish();result.map_err(|message|JobError::Failed{message,installed:vec![]})
        })?;Ok(json!({"job_id":job_id}))
    }

    /// Download（下载）：run a fixed plan, retaining a stable task ID for resuming files.
    pub fn download(self: &Arc<Self>, resource_id: String) -> Result<Value, String> {
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
        let lease=self.pause_service().map_err(|message|JobError::Failed{message,installed:vec![]})?;
        let cancel=self.cancel_signal(job_id)?;
        let mut progress=|phase:&str,done:u64,total:u64|{
            if phase.starts_with("启用")||phase.starts_with("正在启用"){let _=self.begin_non_cancellable_phase(job_id,phase,done,Some(total));}
            else{self.set_phase(job_id,phase,true,done,Some(total));}
        };
        if resource_id!="dsh" && self.context.channel==voice_desktop_common::LaunchChannel::Distribution {
            let group=voice_desktop_common::runtime::model_group(resource_id);
            voice_desktop_common::runtime::configure(&self.context,group,cancel.clone(),&mut progress)
                .map_err(|message|JobError::Failed{message,installed:vec![]})?;
        }
        let spec=voice_desktop_common::resources::spec(&self.context,resource_id).map_err(|message|JobError::Failed{message,installed:vec![]})?;
        let result=if spec["source"]["transfer"]["kind"]=="modelscope_sdk" {
            if self.context.channel==voice_desktop_common::LaunchChannel::Distribution {
                voice_desktop_common::runtime::configure(&self.context,"modelscope-download",cancel,&mut progress)
                    .map_err(|message|JobError::Failed{message,installed:vec![]})?;
            }
            // 工作进程只负责 SDK（平台开发库）下载；原生宿主负责验证与启用。
            let args=self.cli_args("download-modelscope",&["--resource-id".into(),resource_id.into(),"--task-id".into(),job_id.into(),"--download-only".into()]);
            let payload=self.run_stream_cli(Some(job_id),"manage-resources.py",&args,|phase,done,total|{self.set_phase(job_id,localized_phase(phase),true,done,Some(total));Ok(())})
                .map_err(|failure|if failure.cancelled{JobError::Cancelled}else{JobError::Failed{message:failure.message,installed:failure.installed}})?;
            let source=PathBuf::from(payload["staged_path"].as_str().ok_or_else(||JobError::Failed{message:"下载暂存目录缺失".into(),installed:vec![]})?);
            voice_desktop_common::resources::import(&self.context,&source,Some(resource_id),self.cancel_signal(job_id)?,&mut progress)
        }else{voice_desktop_common::resources::download_resource(&self.context,resource_id,self.cancel_signal(job_id)?,&mut progress)};
        let _=lease.finish();result.map_err(|message|JobError::Failed{message,installed:vec![]})
    }

    fn run_import(self: &Arc<Self>, job_id: &str, resource_id: Option<String>) -> Result<Vec<String>, JobError> {
        let sources=voice_desktop_common::native_ui::choose_resource(resource_id.as_deref()).map_err(|message|JobError::Failed{message,installed:vec![]})?;
        if sources.is_empty(){return Err(JobError::Cancelled);}
        let lease=self.pause_service().map_err(|message|JobError::Failed{message,installed:vec![]})?;
        let mut installed=vec![];
        for source in sources {
            let result=voice_desktop_common::resources::import_review(&self.context,&source,resource_id.as_deref(),self.cancel_signal(job_id)?,&mut|phase,done,total|{
                if phase.starts_with("正在启用"){let _=self.begin_non_cancellable_phase(job_id,phase,done,Some(total));}
                else{self.set_phase(job_id,phase,true,done,Some(total));}
            },&mut|items|self.review_import(job_id,items))
                .map_err(|message|JobError::Failed{message,installed:installed.clone()})?;
            installed.extend(result);
        }
        let _=lease.finish();Ok(installed)
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
        (self.service_control)(false).map_err(|_| "无法暂停语音服务以安全激活资源。")?;
        Ok(ServiceLease { manager: self, _guard: guard, active: true })
    }

    fn start_job<F>(self: &Arc<Self>, job_id: String, resource_id: String, phase: String, can_cancel: bool, worker: F) -> Result<(), String>
    where F: FnOnce(Arc<ResourceManager>, String) -> Result<Vec<String>, JobError> + Send + 'static {
        {
            let mut jobs = self.jobs.lock().map_err(|_| "资源任务状态不可用。")?;
            if jobs.get(&job_id).is_some_and(|job| !terminal(&job.snapshot.state)) {
                return Ok(());
            }
            let snapshot = JobSnapshot { job_id: job_id.clone(), resource_id, state: "queued".into(), phase,
                completed_bytes: 0, total_bytes: None, error: None, can_cancel, installed_resources: vec![],
                import_preview: None, created_at: unix_millis() };
            jobs.insert(job_id.clone(), JobControl { snapshot: snapshot.clone(), cancel: Arc::new(AtomicBool::new(false)), child: None });
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
            update(&mut job.snapshot);
            let value = event_value(&job.snapshot);
            let _ = self.persist_locked(&jobs);
            value
        };
        let _ = self.app.emit(PROGRESS_EVENT, event);
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
                error, can_cancel, installed_resources, import_preview: None, created_at: value["created_at"].as_u64().unwrap_or(0) };
            jobs.insert(snapshot.job_id.clone(), JobControl { snapshot, cancel: Arc::new(AtomicBool::new(false)), child: None });
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
}

struct ServiceLease<'a> {
    manager: &'a ResourceManager,
    _guard: MutexGuard<'a, ()>,
    active: bool,
}

impl ServiceLease<'_> {
    fn finish(mut self) -> Result<(), String> {
        self.active = false;
        (self.manager.service_control)(true).map_err(|_| "资源已切换，但语音服务未能恢复。".into())
    }
}

impl Drop for ServiceLease<'_> {
    fn drop(&mut self) {
        if self.active { let _ = (self.manager.service_control)(true); }
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
        "import_preview": snapshot.import_preview,
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
