//! Voice runtime（语音运行环境）：共享依赖计划、可移动解释器、事务启用。
use std::{fs, path::{Path,PathBuf}, process::{Command,ExitStatus,Stdio}, sync::{Arc,atomic::AtomicBool}, time::{Duration,Instant}};
use serde_json::{Value,json};
use crate::{LaunchContext,download::DownloadFile,resources::{self,Progress},workspace::{safe_path,atomic_json,file_hash,remove_owned}};
// ENVIRONMENT_GROUPS（完整语音环境）：语音服务、各模型及轻量 VAD（语音活动检测）所需的六个必需依赖组。
const ENVIRONMENT_GROUPS: [&str; 6] = ["voice-core", "torch-cpu", "onnx-cpu", "sherpa-cpu", "modelscope-download", "onnx-vad"];
pub fn model_group(id:&str)->&'static str {match id {
    "paraformer-streaming"|"paraformer-offline"|"fsmn-vad"|"emotion2vec-plus-base"=>"torch-cpu",
    "kws-sherpa-onnx"=>"sherpa-cpu",_=>"onnx-cpu"
}}
pub fn lock(context:&LaunchContext)->Result<Value,String>{resources::load(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))}
pub const CPU_RESOURCE: &str = "voice-runtime-cpu"; // CPU_RESOURCE（必需中央处理器依赖卡片编号）。
pub const GPU_RESOURCE: &str = "voice-runtime-gpu"; // GPU_RESOURCE（可选显卡依赖卡片编号）。
pub fn gpu_requested(group:&str)->bool{matches!(group,"gpu"|GPU_RESOURCE)}
fn managed_gpu(record:&Value)->bool{record["gpu_profile"]=="cu128"}
fn has_gpu(record:&Value)->bool{managed_gpu(record)||record["compatible_gpu"]==true||record["profile"]=="cu128"}
fn gpu_lock_path(context:&LaunchContext)->PathBuf{context.installed_program().join("config/runtime/voice-gpu.lock.json")}
fn effective_lock(context:&LaunchContext,gpu:bool)->Result<Value,String>{
    let mut value=lock(context)?;
    if gpu{
        let overlay=resources::load(&gpu_lock_path(context))?;
        let rows=overlay["wheels"].as_array().ok_or("显卡依赖锁缺少包集合")?;
        let names=rows.iter().filter_map(|row|row["name"].as_str()).collect::<std::collections::BTreeSet<_>>();
        if overlay["schema"]!=1||overlay["profile"]!="cu128"||rows.len()!=2||names!=std::collections::BTreeSet::from(["torch","torchaudio"]){return Err("可选显卡依赖锁格式或包集合无效".into());}
        let mut replaced=0;
        for row in value["wheels"].as_array_mut().ok_or("基础依赖锁缺少包集合")?{
            if let Some(replacement)=rows.iter().find(|item|item["name"]==row["name"]){
                let version=row["version"].as_str().ok_or("基础依赖版本缺失")?.split('+').next().unwrap();
                if replacement["version"]!=format!("{version}+cu128"){return Err("显卡依赖与基础依赖版本不匹配".into());}
                let groups=row["groups"].clone();*row=replacement.clone();row["groups"]=groups;replaced+=1;
            }
        }
        if replaced!=2{return Err("基础依赖锁缺少配套张量依赖".into());}
    }Ok(value)
}
fn gpu_current(context:&LaunchContext,record:&Value)->bool{
    managed_gpu(record)&&effective_lock(context,true).is_ok()&&file_hash(&gpu_lock_path(context)).is_ok_and(|digest|record["gpu_lock_sha256"]==digest)
}
fn cpu_overlay_current(context:&LaunchContext,record:&Value)->bool{
    let runtime=context.install.join("runtimes/python");
    cpu_overlay_valid(context,&runtime,record)
}
fn cpu_overlay_valid(context:&LaunchContext,runtime:&Path,record:&Value)->bool{
    let mut names=vec!["torch","torchaudio"];
    if fs::read_dir(runtime.join("Lib/site-packages")).is_ok_and(|entries|entries.filter_map(Result::ok)
        .any(|entry|entry.file_name().to_string_lossy().starts_with("onnxruntime_gpu-"))){names.push("onnxruntime");}
    !has_gpu(record)||(file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))
        .is_ok_and(|digest|record["cpu_overlay_lock_sha256"]==digest)
        &&names.iter().all(|name|runtime.join(format!("Lib/cpu-packages/{name}/__init__.py")).is_file()))
}
pub fn fingerprint(root:&Path)->Result<String,String>{
    use sha2::{Sha256,Digest};
    let mut rows=vec![];
    for path in resources::walk(root)? {let name=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/");
        if name.split('/').any(|v|v=="__pycache__")||name.ends_with(".pyc"){continue;}
        let info=path.metadata().map_err(|e|e.to_string())?;
        let time=info.modified().map_err(|e|e.to_string())?.duration_since(std::time::UNIX_EPOCH).map_err(|e|e.to_string())?.as_nanos();
        rows.push((name,info.len(),time));
    }rows.sort_by(|a,b|a.0.cmp(&b.0));let mut hash=Sha256::new();
    for (name,size,time) in rows{hash.update(format!("{name}:{size}:{time}\n").as_bytes());}Ok(format!("{:x}",hash.finalize()))
}
fn registered_gpu_environment(context:&LaunchContext,root:&Path)->bool{
    let Some(record)=resources::load(&context.data.join("config/gpu-environment.json")).ok()else{return false;};
    if record["path"]!="runtimes/python"||root!=context.install.join("runtimes/python"){return false;}
    let Ok(environment_fingerprint)=fingerprint(root)else{return false;};
    // 模型更新会使推理能力登记过期，但不能取消未变化的显卡运行库的复用资格。
    // 候选运行库仍需通过当前锁定版本、完整文件和实际加载校验；模型能力另行重新检测。
    if record["environment_fingerprint"]!=environment_fingerprint{return false;}
    record["models"].as_object().is_some_and(|models|models.values().any(|capability|capability["gpu_available"]==true&&
        capability["environment_fingerprint"]==environment_fingerprint))
}
fn ensure_gpu_core_reuse(inventory:&Value)->Result<(),String>{
    // GPU core reuse（显卡核心运行库复用）：缺失或版本不符时不能以处理器包覆盖原环境。
    let valid=["torch","torchaudio","onnxruntime"].iter().all(|name|
        inventory["reused"].as_array().is_some_and(|rows|rows.iter().any(|row|row.as_str()==Some(*name))));
    if valid{Ok(())}else{Err("显卡核心推理库未通过当前版本复用校验，原环境保持不变；请导入当前完整显卡环境后重试。".into())}
}
fn requested(lock:&Value,group:&str)->Result<Vec<String>,String>{
    let mut names=std::collections::BTreeSet::new();
    let groups=if group=="all"||group=="base" {ENVIRONMENT_GROUPS.to_vec()} else {vec!["voice-core",group]};
    for key in groups {for name in lock["groups"][key].as_array().ok_or("未锁定的依赖组")?{names.insert(name.as_str().ok_or("依赖名称错误")?.to_string());}}
    Ok(names.into_iter().collect())
}
pub fn status(context:&LaunchContext)->Result<Value,String>{
    let record=resources::load(&context.install.join("runtimes/python/runtime.json")).unwrap_or(Value::Null);
    let installed=context.python.is_file();let mut groups=serde_json::Map::new();
    let current=lock(context).ok();
    let current_digest=current.as_ref().and_then(|_|file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json")).ok());
    let cpu_current=cpu_overlay_current(context,&record);
    for key in ENVIRONMENT_GROUPS {
        let ready=installed&&current_digest.is_some()&&cpu_current&&record["schema"]==2&&record["groups"].as_array().is_some_and(|g|g.iter().any(|v|v==key))&&record["lock_sha256"].as_str()==current_digest.as_deref();
        groups.insert(key.into(),json!({"ready":ready,"reason":if ready{None}else{Some("解释器或该组固定依赖尚未配置")}}));
    }
    let full_ready=ENVIRONMENT_GROUPS.iter().all(|key|groups[*key]["ready"]==true);
    let update_available=installed&&current_digest.is_some()&&!full_ready;
    let gpu_installed=installed&&has_gpu(&record);
    let gpu_ready=full_ready&&gpu_current(context,&record);
    let component=|id:&str,label:&str,required:bool,present:bool,ready:bool,update:bool|json!({
        "resource_id":id,"parent_resource_id":"voice-server","label":label,"required":required,
        "installation_state":if present{"installed"}else{"missing"},"files_installed":present,"runtime_ready":ready,"can_start":ready,"update_available":update,
        "fields":{"resource_id":"运行依赖卡片编号","parent_resource_id":"所属语音服务组件","label":"卡片名称","required":"是否参与整体启动门槛","installation_state":"安装状态","files_installed":"依赖已登记安装","runtime_ready":"依赖与当前锁定版本匹配；显卡实际可用性另行检测","can_start":"该组运行依赖已就绪","update_available":"已安装依赖需要更新"}});
    let components=vec![component(CPU_RESOURCE,"CPU（中央处理器）运行依赖",true,installed,full_ready,update_available),
        resources::native_runtime_status(context)?,component(GPU_RESOURCE,"GPU（图形处理器）运行依赖",false,gpu_installed,gpu_ready,gpu_installed&&!gpu_ready)];
    Ok(json!({"interpreter_ready":installed,"service_ready":groups["voice-core"]["ready"],"full_ready":full_ready,"python":context.python,"version":record["python"],"update_available":update_available,"groups":groups,"components":components,
        "fields":{"interpreter_ready":"私有解释器存在","service_ready":"基础语音依赖已验证","full_ready":"六个必需语音依赖组与基础锁定清单一致，不要求安装可选显卡或原生语音依赖","python":"私有解释器路径","version":"解释器版本","update_available":"必需运行环境的依赖锁版本可更新","groups":"内部共享依赖组","components":"语音服务下三个运行依赖卡片","ready":"该组就绪","reason":"未就绪原因"}}))
}
pub fn system_candidates()->Vec<PathBuf>{
    // qualification（隔离验收）允许检查纯下载路线，正式构建不接受此开关。
    if cfg!(feature="qualification")&&std::env::var_os("VOICE_QUALIFICATION_NO_SYSTEM_REUSE").is_some(){return vec![];}
    let mut candidates=vec![];
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::System::Registry::*;
        let wide=|s:&str|s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        for hive in [HKEY_CURRENT_USER,HKEY_LOCAL_MACHINE] {let mut key=std::ptr::null_mut();
            if RegOpenKeyExW(hive,wide(r"Software\Python\PythonCore\3.12\InstallPath").as_ptr(),0,KEY_READ|KEY_WOW64_64KEY,&mut key)!=0{continue;}
            let mut text=[0u16;32768];let mut size=65536;let mut kind=0;
            let result=RegQueryValueExW(key,std::ptr::null(),std::ptr::null(),&mut kind,text.as_mut_ptr() as _,&mut size);RegCloseKey(key);
            if result==0&&kind==REG_SZ{let path=PathBuf::from(String::from_utf16_lossy(&text[..text.iter().position(|v|*v==0).unwrap_or(32768)])).join("python.exe");if path.is_file(){candidates.push(path);}}
        }
    }
    candidates.sort();candidates.dedup();candidates
}
pub fn worker(context:&LaunchContext,python:&Path,operation:&str,args:&[String],cancel:&AtomicBool)->Result<Value,String>{
    #[cfg(windows)] use std::os::windows::{process::CommandExt,io::AsRawHandle};
    let output_path=safe_path(&context.install,&format!("tmp/process/worker-{}.json",crate::workspace::new_id()))?;
    fs::create_dir_all(output_path.parent().unwrap()).map_err(|e|e.to_string())?;
    let output=fs::File::create(&output_path).map_err(|e|e.to_string())?;
    let error_path=output_path.with_extension("stderr");
    let diagnostic=fs::File::create(&error_path).map_err(|e|e.to_string())?;
    let mut command=Command::new(python);context.configure_command(&mut command);
    command.args(["-I","-B","-X","utf8"]).arg(context.program.join("scripts/voice-environment-worker.py")).arg(operation).args(args)
        .stdin(Stdio::null()).stdout(output).stderr(diagnostic).current_dir(&context.program);
    #[cfg(windows)] command.creation_flags(0x08000000);
    let mut child=command.spawn().map_err(|e|format!("解释器工作进程无法启动：{e}"))?;
    #[cfg(windows)] let job=crate::windows::Job::new()?;
    #[cfg(windows)] job.assign(child.as_raw_handle())?;
    // 完整语音依赖首次加载在测试机接近三分钟，单独放宽检查预算并继续响应取消。
    let timeout=if matches!(operation,"check"|"gpu"){600}else{180};
    let deadline=Instant::now()+Duration::from_secs(timeout);
    let status=loop {if resources::check_cancel(cancel).is_err()||Instant::now()>deadline{let _=child.kill();let _=child.wait();let _=fs::remove_file(&output_path);let _=fs::remove_file(&error_path);return Err("工作进程已取消或超时".into());}
        if let Some(status)=child.try_wait().map_err(|e|e.to_string())?{break status;}
        std::thread::sleep(Duration::from_millis(80));};
    let bytes=fs::read(&output_path);let diagnostic=fs::read(&error_path).unwrap_or_default();
    let _=fs::remove_file(output_path);let _=fs::remove_file(error_path);
    worker_result(&bytes.map_err(|e|e.to_string())?,&diagnostic,status,operation)
}
/// worker_result（工作进程结果）：保留启动阶段错误与退出码，标准错误不再被丢弃。
fn worker_result(bytes:&[u8],diagnostic:&[u8],status:ExitStatus,operation:&str)->Result<Value,String>{
    let record=bytes.split(|v|*v==b'\n').rev().filter(|line|!line.iter().all(u8::is_ascii_whitespace))
        .find_map(|line|serde_json::from_slice::<Value>(line).ok().filter(|value|value.get("ok").is_some()));
    if let Some(record)=record {
        if record["ok"]!=true{return Err(record["error"].as_str().unwrap_or("依赖工作进程失败").into());}
        if status.success(){return Ok(record["result"].clone());}
    }
    let text=String::from_utf8_lossy(diagnostic);
    let detail=text.lines().rev().find(|line|!line.trim().is_empty()).unwrap_or("解释器没有返回有效诊断").chars().take(500).collect::<String>();
    Err(format!("Python（运行环境）检查失败，步骤 {operation}（工作进程操作），退出码 {}：{detail}",status.code().map(|code|format!("{code} / 0x{:08X}",code as u32)).unwrap_or_else(||"未知".into())))
}
pub fn plan(context:&LaunchContext,group:&str)->Result<Value,String>{
    let existing=resources::load(&context.install.join("runtimes/python/runtime.json")).unwrap_or(Value::Null);
    let gpu=gpu_requested(group)||managed_gpu(&existing);
    let lock=effective_lock(context,gpu)?;let names=requested(&lock,"all")?;let state=status(context)?;let mut candidates=vec![];
    let cancel=AtomicBool::new(false);
    for path in system_candidates(){
        let result=worker(context,&path,"interpreter",&[],&cancel);
        candidates.push(json!({"path":path,"accepted":result.as_ref().is_ok_and(|v|v["official"]==true),"result":result.unwrap_or_else(|e|json!({"reason":e}))}));
    }
    let wheels:Vec<_>=lock["wheels"].as_array().ok_or("缺少依赖锁")?.iter().filter(|v|names.iter().any(|n|v["name"]==*n)).cloned().collect();
    let ready=state["full_ready"]==true&&(!gpu_requested(group)||gpu_current(context,&existing));
    Ok(json!({"group":group,"resource_id":if gpu_requested(group){GPU_RESOURCE}else{CPU_RESOURCE},"required":!gpu_requested(group),"profile":if gpu{"cu128"}else{"cpu"},"already_ready":ready,"python":lock["python"],"wheels":wheels,"system_candidates":candidates,
        "total_bytes":wheels.iter().map(|v|v["size"].as_u64().unwrap_or(0)).sum::<u64>()+lock["python"]["size"].as_u64().unwrap_or(0),
        "fields":{"group":"安装依赖组","resource_id":"运行依赖卡片编号","required":"是否为必需依赖","profile":"本次安装计算组合；处理器依赖更新保留已安装显卡组合","already_ready":"完整匹配可跳过","system_candidates":"可复用的官方解释器候选","accepted":"筛选通过","wheels":"本次完整环境的固定依赖包；含显卡时只替换张量框架与音频扩展","total_bytes":"未扣除复用的最大下载大小"}}))
}
fn spec(value:&Value)->Result<DownloadFile,String>{Ok(DownloadFile{url:value["url"].as_str().ok_or("依赖来源缺失")?.into(),size:value["size"].as_u64().ok_or("大小缺失")?,sha256:value["sha256"].as_str().ok_or("摘要缺失")?.into(),sha512:String::new(),path:value["file"].as_str().unwrap_or("python.zip").into(),download_variants:vec![]})}
fn valid_cached_with_progress(path:&Path,item:&Value,cancel:&AtomicBool,progress:&mut Progress)->Result<bool,String>{
    resources::check_cancel(cancel)?;
    if path.metadata().ok().map(|v|v.len())!=item["size"].as_u64(){return Ok(false);}
    let result=crate::workspace::file_hash_with_progress(path,cancel,&mut|phase,done,total|progress(&format!("核验固定下载缓存：{phase}"),done,total));
    resources::check_cancel(cancel)?;Ok(result.ok().as_deref()==item["sha256"].as_str())
}
/// prepare_search_paths（私有解释器搜索路径）：同时兼容系统安装与嵌入式包。
fn prepare_search_paths(root:&Path)->Result<(),String>{
    // DLLs（标准扩展库目录）是系统解释器中 _socket（网络）、_ssl（加密）等模块的位置。
    fs::write(root.join("python312._pth"),"python312.zip\n.\nDLLs\nLib\nLib/cpu-packages\nLib/site-packages\nimport site\n").map_err(|e|e.to_string())
}
fn prepare_cpu_overlay(context:&LaunchContext,stage:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    let base=lock(context)?;
    let site=stage.join("Lib/cpu-packages");
    let mut names=vec!["torch","torchaudio"];
    let main=stage.join("Lib/site-packages");
    if fs::read_dir(&main).map_err(|e|e.to_string())?.filter_map(Result::ok)
        .any(|entry|entry.file_name().to_string_lossy().starts_with("onnxruntime_gpu-")){
        names.push("onnxruntime");
    }
    let cache=safe_path(&context.install,"cache/dependencies/wheels")?;
    fs::create_dir_all(&cache).map_err(|e|e.to_string())?;
    let mut wheels=vec![];
    let mut transfers=vec![];
    for item in base["wheels"].as_array().ok_or("基础依赖锁缺少包集合")?{
        if !names.iter().any(|name|item["name"]==*name){continue;}
        let target=safe_path(&cache,item["file"].as_str().ok_or("依赖文件名缺失")?)?;
        if !valid_cached_with_progress(&target,item,&cancel,progress)?{transfers.push((spec(item)?,target.clone()));}
        wheels.push(target.to_string_lossy().into_owned());
    }
    if wheels.len()!=names.len(){return Err("基础依赖锁缺少处理器隔离包".into());}
    crate::download::download_batch(&transfers,cancel.clone(),progress)?;
    remove_owned(&site,&context.install)?;
    fs::create_dir_all(&site).map_err(|e|e.to_string())?;
    let interpreter=stage.join("python.exe");
    worker(context,&interpreter,"install",&["--target".into(),site.to_string_lossy().into_owned(),
        "--wheels".into(),serde_json::to_string(&wheels).unwrap()],&cancel)?;
    let checked=worker(context,&interpreter,"check",&["--tensor-device".into(),"cpu".into(),
        "--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned(),
        "--names".into(),serde_json::to_string(&names).unwrap()],&cancel)?;
    if checked["ready"]!=true{return Err(format!("独立处理器依赖校验失败：{}",checked["errors"]));}
    Ok(())
}
struct RuntimeWheel {
    file: Option<DownloadFile>, // file（需要网络下载的固定依赖；有效缓存或内置包为空）。
    target: PathBuf, // target（已校验的缓存路径）。
}
#[derive(Default)]
struct DownloadTotals {
    total_bytes: u64, // total_bytes（本次全部待下载文件的总字节）。
    completed_bytes: u64, // completed_bytes（此前已下载文件的累计字节）。
    current_bytes: u64, // current_bytes（当前文件的最高已下载字节，重试不使进度倒退）。
}
impl DownloadTotals {
    fn report(&mut self,progress:&mut Progress,phase:&str,current:u64){
        if !phase.contains("正在下载") && phase!="本次总下载量已确认"{progress(phase,0,0);return;}
        self.current_bytes=self.current_bytes.max(current);
        progress(phase,self.completed_bytes.saturating_add(self.current_bytes).min(self.total_bytes),self.total_bytes);
    }
    fn transfer(&mut self,file:&DownloadFile,target:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress,phase:&str)->Result<(),String>{
        crate::download::download_with_progress(file,target,cancel,&mut|stage,done,_|{
            self.transfer_progress(stage,done,file.size,progress,phase);
        },0)?;
        self.completed_bytes=self.completed_bytes.saturating_add(file.size);self.current_bytes=0;
        self.report(progress,phase,0);Ok(())
    }
    fn transfer_progress(&mut self,stage:&str,done:u64,size:u64,progress:&mut Progress,phase:&str){
        if crate::download::is_transfer_phase(stage){
            let detail=if stage=="下载固定资源"{phase.to_owned()}else{format!("{phase} · {stage}")};
            self.report(progress,&detail,done.min(size));
        }
        else if stage.starts_with("校验"){progress(&format!("下载缓存：{stage}"),done,size);}
        else{progress(stage,0,0);}
    }
}
fn copy_interpreter(source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let root=source.parent().ok_or("解释器目录无效")?;
    fn visit(root:&Path,folder:&Path,files:&mut Vec<PathBuf>,cancel:&AtomicBool)->Result<(),String>{
        for entry in fs::read_dir(folder).map_err(|e|e.to_string())?{resources::check_cancel(cancel)?;let entry=entry.map_err(|e|e.to_string())?;let path=entry.path();
            let relative=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/");let lower=relative.to_lowercase();
            if ["lib/site-packages","scripts","doc"].iter().any(|prefix|lower==*prefix||lower.starts_with(&format!("{prefix}/")))||lower.ends_with(".pyc"){continue;}
            crate::workspace::relative_path(&relative)?;let metadata=entry.metadata().map_err(|e|e.to_string())?;
            if crate::workspace::metadata_reparse(&metadata){return Err("本机解释器包含目录链接，请改用固定下载来源。".into());}
            if metadata.is_dir(){visit(root,&path,files,cancel)?;}else if metadata.is_file(){files.push(path);}else{return Err("本机解释器包含非普通文件。".into());}
        }Ok(())
    }
    let mut files=vec![];visit(root,root,&mut files,cancel)?;resources::copy_selected_verified(root,target,files,cancel,progress)
}
pub fn configure(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    resources::recover(context)?; // 单独调用时先恢复；并行宿主在启动及串行启用时恢复事务。
    configure_with_activation(context,group,cancel,progress,&mut|stage,relative|resources::activate(context,stage,relative))
}
pub fn configure_with_activation(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    if !matches!(group,"all"|"base"|"cpu"|CPU_RESOURCE|"gpu"|GPU_RESOURCE)&&!ENVIRONMENT_GROUPS.contains(&group){return Err("未知的语音运行依赖卡片".into());}
    // group（安装请求）：显卡卡片升级现用完整环境，其余入口更新必需依赖并保留已装显卡能力。
    configure_inner(context,group,cancel,progress,true,enable)
}
fn configure_inner(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,allow_reuse:bool,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    resources::check_cancel(&cancel)?;progress("检查现有语音环境登记",0,0);
    let state=status(context)?;
    let old=context.install.join("runtimes/python");
    let existing=resources::load(&old.join("runtime.json")).unwrap_or(Value::Null);
    let gpu=gpu_requested(group)||managed_gpu(&existing);
    let lock=effective_lock(context,gpu)?;let requested=requested(&lock,"all")?;
    let compatible_gpu=allow_reuse&&(existing["compatible_gpu"]==true||(existing["schema"]==1&&existing["profile"]=="cu128")||registered_gpu_environment(context,&old));
    let reusable_environment=if allow_reuse&&context.python.is_file(){
        let result=crate::workspace::verify_tree_with_progress(&old,"manifest.json",&cancel,&mut|phase,done,total|
            progress(&format!("校验现有语音环境：{phase}"),done,total));
        // 取消不能被当成完整性失败并继续下载，也不能把显卡环境改装成处理器环境。
        resources::check_cancel(&cancel)?;
        if compatible_gpu{if let Err(error)=&result{return Err(format!("现用显卡运行环境完整性校验未通过：{error} 原环境保持不变；请修复或导入当前完整显卡环境后重试。"));}}
        result.is_ok()
    }else{false};
    if compatible_gpu&&!reusable_environment{return Err("现用显卡运行环境完整性校验未通过，原环境保持不变；请修复或导入当前完整显卡环境后重试。".into());}
    let mut check_args=vec!["--names".into(),serde_json::to_string(&requested).unwrap(),"--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned()];
    if gpu{check_args.extend(["--gpu-lock".into(),gpu_lock_path(context).to_string_lossy().into_owned()]);}
    if compatible_gpu{check_args.push("--compatible-gpu".into());}
    if state["full_ready"]==true&&reusable_environment&&(!gpu||gpu_current(context,&existing)){
        progress("检测完整语音依赖",0,0);
        if worker(context,&context.python,"check",&check_args,&cancel)?["ready"]==true{progress("复用已校验完整环境",1,1);return Ok(installed_resources(gpu));}
    }
    let stage=safe_path(&context.install,"tmp/voice-environment/stage")?;remove_owned(&stage,&context.install)?;fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
    let site=stage.join("Lib/site-packages");fs::create_dir_all(&site).map_err(|e|e.to_string())?;
    let groups:std::collections::BTreeSet<String>=ENVIRONMENT_GROUPS.iter().map(|key|(*key).to_string()).collect();
    let mut reused=vec![];let mut official=None;let mut embedded=false;
    let maximum=lock["wheels"].as_array().ok_or("依赖锁无效")?.iter().filter(|item|requested.iter().any(|name|item["name"]==*name)).map(|item|item["size"].as_u64().unwrap_or(0)).sum::<u64>()+lock["python"]["size"].as_u64().unwrap_or(0);
    if crate::workspace::available_space(&context.install)?<maximum.saturating_mul(3){return Err("下载、展开和事务备份空间不足".into());}
    if reusable_environment{
        progress("复用已校验私有环境",0,0);
        // 只复制已经核验的发布文件；开发命令产生的清单外字节码不进入候选环境。
        let manifest=resources::load(&old.join("manifest.json"))?;
        let mut files=manifest["files"].as_array().ok_or("组件清单缺少文件集合")?.iter()
            .map(|file|safe_path(&old,file["path"].as_str().ok_or("清单路径无效")?)).collect::<Result<Vec<_>,String>>()?;
        files.push(old.join("manifest.json"));resources::copy_selected_verified(&old,&stage,files,&cancel,progress)?;
        prepare_search_paths(&stage)?;
        let inventory=worker(context,&stage.join("python.exe"),"inventory",&check_args,&cancel)?;
        if compatible_gpu&&!gpu{ensure_gpu_core_reuse(&inventory)?;}
        reused.extend(inventory["reused"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned));
        if gpu{
            // 旧 CPU（中央处理器）包及元数据必须先从候选移除，避免同名双版本与旧动态库残留。
            let replacements=["torch","torchaudio"].into_iter().filter(|name|!reused.iter().any(|item|item==name)).collect::<Vec<_>>();
            if !replacements.is_empty(){worker(context,&stage.join("python.exe"),"prune",&["--target".into(),site.to_string_lossy().into_owned(),"--names".into(),serde_json::to_string(&replacements).unwrap()],&cancel)?;}
        }
    }else{
        for path in if allow_reuse{system_candidates()}else{vec![]}{if worker(context,&path,"interpreter",&[],&cancel).is_ok_and(|v|v["official"]==true){official=Some(path);break;}}
        if let Some(path)=&official{
            progress("复用官方系统解释器",0,0);copy_interpreter(path,&stage,&cancel,progress)?;
        }else {embedded=true;}
    }
    if let Some(path)=official{progress("核验本机依赖复用条件",0,0);let mut reuse_args=check_args.clone();reuse_args.extend(["--target".into(),site.to_string_lossy().into_owned()]);let response=worker(context,&path,"reuse",&reuse_args,&cancel)?;
        reused.extend(response["reused"].as_array().unwrap_or(&vec![]).iter().filter_map(Value::as_str).map(str::to_owned));
    }
    progress("计算本次依赖总下载量",0,0);
    let cache=safe_path(&context.install,"cache/dependencies/wheels")?;fs::create_dir_all(&cache).map_err(|e|e.to_string())?;
    let mut wheels=vec![];
    for item in lock["wheels"].as_array().ok_or("依赖锁无效")? {
        let name=item["name"].as_str().ok_or("依赖名称缺失")?;
        if !requested.iter().any(|v|v==name)||reused.iter().any(|v|v==name){continue;}
        let file=item["file"].as_str().ok_or("依赖文件名缺失")?;let target=safe_path(&cache,file)?;
        let mut needs_download=false;
        if !valid_cached_with_progress(&target,item,&cancel,progress)?{
            if let Some(bundled)=item["bundled"].as_str(){let source=safe_path(&context.program,bundled)?;resources::copy_file(&source,&target,&cancel,progress)?;if !valid_cached_with_progress(&target,item,&cancel,progress)?{return Err("内置预编译包摘要错误".into());}}
            else{needs_download=true;}
        }
        resources::check_cancel(&cancel)?;
        wheels.push(RuntimeWheel{file:if needs_download{Some(spec(item)?)}else{None},target});
    }
    let python_cache=safe_path(&context.install,"cache/dependencies/python-3.12.10.zip")?;
    let python_download=embedded&&!valid_cached_with_progress(&python_cache,&lock["python"],&cancel,progress)?;
    let mut totals=DownloadTotals{total_bytes:wheels.iter().filter_map(|item|item.file.as_ref()).map(|file|file.size).sum::<u64>()+
        if python_download{lock["python"]["size"].as_u64().ok_or("解释器大小缺失")?}else{0},..Default::default()};
    totals.report(progress,"本次总下载量已确认",0);
    if embedded {
        fs::create_dir_all(python_cache.parent().unwrap()).map_err(|e|e.to_string())?;
        if python_download{totals.transfer(&spec(&lock["python"])?,&python_cache,cancel.clone(),progress,"正在下载官方解释器")?;}
        resources::unpack_zip(&python_cache,&stage,&cancel,&mut|phase,_,_|totals.report(progress,phase,0))?;
    }
    // ._pth（解释器搜索路径）只写相对目录，禁止继承系统或开发环境路径。
    prepare_search_paths(&stage)?;
    // 应用本地 CRT（C++ 运行库）随本体携带，仅释放到私有解释器旁。
    let crt=context.program.join("tools/python-crt");
    if crt.is_dir(){for path in resources::walk(&crt)?{let name=path.file_name().ok_or("运行库文件名无效")?;resources::copy_file(&path,&stage.join(name),&cancel,&mut|phase,_,_|totals.report(progress,phase,0))?;}}
    let interpreter=stage.join("python.exe");
    totals.report(progress,"核验私有解释器",0);worker(context,&interpreter,"interpreter",&[],&cancel)?;
    let transfers=wheels.iter().filter_map(|item|item.file.clone().map(|file|(file,item.target.clone()))).collect::<Vec<_>>();
    crate::download::download_batch(&transfers,cancel.clone(),&mut|phase,done,total|progress(phase,done,total))?;
    totals.completed_bytes=totals.total_bytes;totals.current_bytes=0;
    let mut downloaded=vec![];
    for item in wheels {
        resources::check_cancel(&cancel)?;
        downloaded.push(item.target.to_string_lossy().into_owned());
    }
    let args=vec!["--target".into(),site.to_string_lossy().into_owned(),"--wheels".into(),serde_json::to_string(&downloaded).unwrap()];
    totals.report(progress,"组装固定运行依赖",0);worker(context,&interpreter,"install",&args,&cancel)?;
    totals.report(progress,"核验全部语音运行依赖",0);
    let checked=worker(context,&interpreter,"check",&check_args,&cancel)?;
    if checked["ready"]!=true && !reused.is_empty(){
        if compatible_gpu{return Err("已登记的兼容显卡环境未通过当前完整依赖校验，旧环境保持不变。".into());}
        // DLL（动态链接库）闭包或本机路径失败：自动从固定包重新组装，缓存继续复用。
        remove_owned(&stage,&context.install)?;
        progress("本机复用未通过加载检查，改用固定下载包",0,0);
        return configure_inner(context,group,cancel,progress,false,enable);
    }
    if checked["ready"]!=true{return Err(format!("运行依赖加载失败：{}",checked["errors"]));}
    if gpu||compatible_gpu{prepare_cpu_overlay(context,&stage,cancel.clone(),progress)?;}
    resources::check_cancel(&cancel)?;
    let mut packages:std::collections::BTreeSet<String>=reused.into_iter().collect();packages.extend(requested);
    atomic_json(&stage.join("runtime.json"),&json!({"schema":2,"python":"3.12.10","profile":if gpu{"gpu-private"}else if compatible_gpu{"compatible-private"}else{"cpu-private"},"compatible_gpu":compatible_gpu,"groups":groups,"packages":packages,"lock_sha256":file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?,
        "gpu_profile":if gpu{Some("cu128")}else{None},"gpu_lock_sha256":if gpu{Some(file_hash(&gpu_lock_path(context))?)}else{None},
        "cpu_overlay_lock_sha256":if gpu||compatible_gpu{Some(file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?)}else{None},
        "fields":{"schema":"运行环境记录格式","python":"解释器版本","profile":"私有环境组合","compatible_gpu":"保留已验证的历史显卡依赖组合","gpu_profile":"可选显卡张量依赖组合；空值表示未通过显卡卡片安装","gpu_lock_sha256":"可选显卡依赖锁摘要；与基础依赖锁独立","cpu_overlay_lock_sha256":"已验证的处理器独立依赖锁摘要","groups":"已完成检查的完整依赖组","packages":"已安装依赖名称","lock_sha256":"必需运行依赖清单摘要"}}))?;
    totals.report(progress,"校验语音环境文件清单",0);
    atomic_json(&stage.join("manifest.json"),&crate::workspace::manifest_with_progress(&stage,"python",&crate::workspace::new_id(),&cancel,&mut|phase,done,total|
        progress(&format!("生成语音环境清单：{phase}"),done,total))?)?;
    totals.report(progress,"启用私有语音环境",0);resources::check_cancel(&cancel)?;enable(&stage,"runtimes/python")?;
    Ok(installed_resources(gpu))
}
fn installed_resources(gpu:bool)->Vec<String>{if gpu{vec!["voice-server".into(),CPU_RESOURCE.into(),GPU_RESOURCE.into()]}else{vec!["voice-server".into(),CPU_RESOURCE.into()]}}
pub fn import(context:&LaunchContext,source:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    resources::recover(context)?;
    import_with_activation(context,source,cancel,progress,&mut|stage,relative|resources::activate(context,stage,relative))
}
pub fn import_with_activation(context:&LaunchContext,source:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    import_profile_with_activation(context,source,"all",cancel,progress,enable)
}
pub fn import_profile_with_activation(context:&LaunchContext,source:&Path,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    let candidate=safe_path(&context.install,"tmp/voice-import")?;remove_owned(&candidate,&context.install)?;
    if source.is_file(){resources::unpack_zip(source,&candidate,&cancel,progress)?;}else{resources::copy_verified(source,&candidate,&cancel,progress)?;}
    crate::workspace::verify_tree_with_progress(&candidate,"manifest.json",&cancel,&mut|phase,done,total|
        progress(&format!("校验导入的语音环境：{phase}"),done,total))?;
    let mut record=resources::load(&candidate.join("runtime.json"))?;
    let existing=resources::load(&context.install.join("runtimes/python/runtime.json")).unwrap_or(Value::Null);
    if has_gpu(&existing)&&!has_gpu(&record){return Err("现用环境已安装显卡依赖，不能用纯中央处理器环境覆盖；请导入包含显卡依赖的完整环境。".into());}
    let gpu=managed_gpu(&record);
    if gpu_requested(group)&&!gpu{return Err("所选环境未包含当前显卡依赖卡片，请使用下载入口配置，或导入当前显卡运行环境。".into());}
    if gpu&&!gpu_current(context,&record){return Err("导入环境的显卡依赖锁已过期，请重新配置显卡运行依赖。".into());}
    if !cpu_overlay_valid(context,&candidate,&record){return Err("导入的显卡环境缺少当前独立处理器依赖，请先在来源程序中更新语音运行依赖并重新导出；现用环境保持不变。".into());}
    let identity=worker(context,&candidate.join("python.exe"),"interpreter",&[],&cancel)?;
    if identity["version"]!="3.12.10"||identity["official"]!=true{return Err("解释器版本或架构不匹配".into());}
    let current_lock=effective_lock(context,gpu)?;
    let lock_sha256=file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?;
    let names=requested(&current_lock,"all")?;
    if !matches!(record["schema"].as_u64(),Some(1|2)){return Err("导入环境记录格式不受支持".into());}
    if record["schema"]==2&&record["lock_sha256"].as_str()!=Some(lock_sha256.as_str()){
        return Err("导入环境的依赖锁版本已过期，请重新配置完整语音环境".into());
    }
    let mut check_args=vec!["--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned()];
    if gpu{check_args.extend(["--gpu-lock".into(),gpu_lock_path(context).to_string_lossy().into_owned()]);}
    if record["schema"]==1||record["compatible_gpu"]==true{check_args.push("--compatible-gpu".into());}
    check_args.extend(["--names".into(),serde_json::to_string(&names).unwrap()]);
    if worker(context,&candidate.join("python.exe"),"check",&check_args,&cancel)?["ready"]!=true{return Err("导入环境的完整语音依赖无法加载".into());}
    let mut packages:std::collections::BTreeSet<String>=record["packages"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned).collect();
    packages.extend(names);
    let compatible_gpu=record["compatible_gpu"]==true||record["profile"]=="cu128";
    record["schema"]=json!(2);record["groups"]=json!(ENVIRONMENT_GROUPS);record["packages"]=json!(packages);record["lock_sha256"]=json!(lock_sha256);record["compatible_gpu"]=json!(compatible_gpu);
    if !record["fields"].is_object(){record["fields"]=json!({});}
    record["fields"]["groups"]=json!("已完成检查的完整依赖组");record["fields"]["packages"]=json!("已安装依赖名称");record["fields"]["lock_sha256"]=json!("固定运行依赖清单摘要");
    atomic_json(&candidate.join("runtime.json"),&record)?;
    atomic_json(&candidate.join("manifest.json"),&crate::workspace::manifest_with_progress(&candidate,"python",&crate::workspace::new_id(),&cancel,progress)?)?;
    resources::check_cancel(&cancel)?;progress("启用导入的语音环境",1,1);resources::check_cancel(&cancel)?;enable(&candidate,"runtimes/python")?;Ok(installed_resources(gpu))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runtime_workspace()->(PathBuf,PathBuf,LaunchContext){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-runtime-cards-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/runtime")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 测试上下文").unwrap();
        fs::write(root.join("app/scripts/voice-environment-worker.py"),"# 不执行安装").unwrap();
        fs::write(root.join("app/config/runtime/voice-runtime.lock.json"),include_bytes!("../../../../config/runtime/voice-runtime.lock.json")).unwrap();
        fs::write(root.join("app/config/runtime/voice-gpu.lock.json"),include_bytes!("../../../../config/runtime/voice-gpu.lock.json")).unwrap();
        let runtime=root.join("runtimes/python");fs::create_dir_all(&runtime).unwrap();fs::write(runtime.join("python.exe"),b"fixture").unwrap();
        atomic_json(&runtime.join("runtime.json"),&json!({"schema":2,"profile":"cpu-private","python":"3.12.10","groups":ENVIRONMENT_GROUPS,
            "lock_sha256":file_hash(&root.join("app/config/runtime/voice-runtime.lock.json")).unwrap()})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();(owner,root,context)
    }

    #[test]
    fn dependency_cards_require_only_cpu_and_missing_optional_locks_do_not_block_cpu(){
        let (owner,root,context)=runtime_workspace();
        fs::remove_file(gpu_lock_path(&context)).unwrap();
        let state=status(&context).unwrap();let rows=state["components"].as_array().unwrap();
        assert_eq!(state["full_ready"],true);assert_eq!(rows.len(),3);
        assert_eq!(rows[0]["resource_id"],CPU_RESOURCE);assert_eq!(rows[0]["required"],true);assert_eq!(rows[0]["can_start"],true);
        assert_eq!(rows[1]["resource_id"],"tts-audiocpp-runtime");assert_eq!(rows[1]["required"],false);
        assert_eq!(rows[2]["resource_id"],GPU_RESOURCE);assert_eq!(rows[2]["required"],false);assert_eq!(rows[2]["can_start"],false);
        assert!(effective_lock(&context,false).is_ok());assert!(effective_lock(&context,true).is_err());
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn gpu_overlay_replaces_only_matching_tensor_wheels_and_keeps_one_interpreter(){
        let (owner,root,context)=runtime_workspace();
        let base=effective_lock(&context,false).unwrap();let gpu=effective_lock(&context,true).unwrap();
        for row in base["wheels"].as_array().unwrap(){
            let other=gpu["wheels"].as_array().unwrap().iter().find(|item|item["name"]==row["name"]).unwrap();
            if ["torch","torchaudio"].iter().any(|name|row["name"]==*name){
                assert_eq!(other["version"],"2.11.0+cu128");assert!(other["url"].as_str().unwrap().starts_with("https://download.pytorch.org/whl/cu128/"));
                assert_eq!(other["groups"],row["groups"]);
            }else{assert_eq!(row,other);}
        }
        assert_eq!(gpu["python"],base["python"]);assert!(!root.join("runtimes/python-gpu").exists());
        let mut overlay=resources::load(&gpu_lock_path(&context)).unwrap();overlay["wheels"][0]["version"]=json!("2.10.0+cu128");
        atomic_json(&gpu_lock_path(&context),&overlay).unwrap();assert!(effective_lock(&context,true).unwrap_err().contains("版本不匹配"));
        assert_eq!(lock(&context).unwrap(),base);
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn cpu_update_plan_preserves_gpu_and_optional_gpu_updates_keep_cpu_ready(){
        let (owner,root,context)=runtime_workspace();let path=root.join("runtimes/python/runtime.json");
        let mut record=resources::load(&path).unwrap();record["profile"]=json!("gpu-private");record["gpu_profile"]=json!("cu128");
        record["gpu_lock_sha256"]=json!(file_hash(&gpu_lock_path(&context)).unwrap());
        assert!(!cpu_overlay_current(&context,&record));
        record["cpu_overlay_lock_sha256"]=record["lock_sha256"].clone();
        for name in ["torch","torchaudio"]{
            let package=root.join(format!("runtimes/python/Lib/cpu-packages/{name}"));
            fs::create_dir_all(&package).unwrap();fs::write(package.join("__init__.py"),b"").unwrap();
        }
        atomic_json(&path,&record).unwrap();
        let plan=plan(&context,"cpu").unwrap();assert_eq!(plan["profile"],"cu128");assert_eq!(plan["resource_id"],CPU_RESOURCE);
        assert_eq!(status(&context).unwrap()["components"][2]["can_start"],true);
        let gpu_onnx=root.join("runtimes/python/Lib/site-packages/onnxruntime_gpu-1.24.4.dist-info");
        fs::create_dir_all(&gpu_onnx).unwrap();assert!(!cpu_overlay_current(&context,&record));
        let cpu_onnx=root.join("runtimes/python/Lib/cpu-packages/onnxruntime");
        fs::create_dir_all(&cpu_onnx).unwrap();fs::write(cpu_onnx.join("__init__.py"),b"").unwrap();
        assert!(cpu_overlay_current(&context,&record));
        fs::write(gpu_lock_path(&context),b"{}").unwrap();let state=status(&context).unwrap();
        assert_eq!(state["full_ready"],true);assert_eq!(state["update_available"],false);
        assert_eq!(state["components"][2]["can_start"],false);assert_eq!(state["components"][2]["update_available"],true);
        record["cpu_overlay_lock_sha256"]=json!("old");atomic_json(&path,&record).unwrap();
        assert_eq!(status(&context).unwrap()["full_ready"],false);
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn cpu_import_cannot_overwrite_installed_gpu_before_activation(){
        let (owner,root,context)=runtime_workspace();let current=root.join("runtimes/python/runtime.json");
        let mut record=resources::load(&current).unwrap();record["gpu_profile"]=json!("cu128");atomic_json(&current,&record).unwrap();
        let source=root.join("tmp/import-cpu-source");fs::create_dir_all(&source).unwrap();
        fs::write(source.join("python.exe"),b"fixture").unwrap();atomic_json(&source.join("runtime.json"),&json!({"schema":2,"profile":"cpu-private"})).unwrap();
        atomic_json(&source.join("manifest.json"),&resources::manifest(&source,"python","cpu-import").unwrap()).unwrap();
        let before=file_hash(&current).unwrap();let mut activated=false;
        let error=import_profile_with_activation(&context,&source,"cpu",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},&mut|_,_|{activated=true;Ok(())}).unwrap_err();
        assert!(error.contains("不能用纯中央处理器环境覆盖"));assert!(!activated);assert_eq!(file_hash(&current).unwrap(),before);
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn gpu_import_without_cpu_overlay_is_rejected_before_activation(){
        let (owner,root,context)=runtime_workspace();
        let current=root.join("runtimes/python/runtime.json");let before=file_hash(&current).unwrap();
        for schema in [1,2]{
            let source=root.join(format!("tmp/import-gpu-{schema}"));fs::create_dir_all(&source).unwrap();
            fs::write(source.join("python.exe"),b"fixture").unwrap();
            atomic_json(&source.join("runtime.json"),&json!({"schema":schema,"profile":"cu128"})).unwrap();
            atomic_json(&source.join("manifest.json"),&resources::manifest(&source,"python","gpu-import").unwrap()).unwrap();
            let mut activated=false;
            let error=import_profile_with_activation(&context,&source,"cpu",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},&mut|_,_|{activated=true;Ok(())}).unwrap_err();
            assert!(error.contains("独立处理器依赖"));assert!(!activated);assert_eq!(file_hash(&current).unwrap(),before);
        }
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn complete_environment_includes_all_model_and_download_dependencies() {
        let lock:Value=serde_json::from_str(include_str!("../../../../config/runtime/voice-runtime.lock.json")).unwrap();
        let names=requested(&lock,"all").unwrap();
        let wheels=lock["wheels"].as_array().unwrap().iter().filter_map(|item|item["name"].as_str()).collect::<std::collections::BTreeSet<_>>();
        for name in &names{assert!(wheels.contains(name.as_str()),"完整运行环境声明了 {name}（依赖），但没有固定下载包");}
        for name in ["fastapi","torch","torchaudio","funasr","onnxruntime","sherpa-onnx","modelscope","funasr-onnx"] {
            assert!(names.iter().any(|item|item==name),"完整环境缺少 {name}（依赖）");
        }
        assert_eq!(ENVIRONMENT_GROUPS.len(),6);
        assert!(lock["groups"]["onnx-vad"].as_array().is_some());
        assert_eq!(names.len(),names.iter().collect::<std::collections::BTreeSet<_>>().len());
    }

    #[test]
    fn registered_gpu_environment_survives_model_updates_but_rejects_environment_changes() {
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-gpu-registration-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 测试启动上下文，不运行此脚本").unwrap();
        let python=root.join("runtimes/python");let models=root.join("models");
        fs::create_dir_all(&python).unwrap();fs::create_dir_all(&models).unwrap();
        fs::write(python.join("python.exe"),b"fixture").unwrap();fs::write(models.join("model.bin"),b"fixture").unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        let environment_fingerprint=fingerprint(&python).unwrap();let models_fingerprint=fingerprint(&models).unwrap();
        crate::workspace::atomic_json(&context.data.join("config/gpu-environment.json"),&json!({
            "path":"runtimes/python","environment_fingerprint":environment_fingerprint,"models_fingerprint":models_fingerprint,
            "models":{"smart_turn":{"gpu_available":true,"environment_fingerprint":environment_fingerprint,"models_fingerprint":models_fingerprint}}
        })).unwrap();
        assert!(registered_gpu_environment(&context,&python));
        fs::write(models.join("changed.bin"),b"changed").unwrap();
        assert!(registered_gpu_environment(&context,&python));
        fs::write(python.join("changed.dll"),b"changed").unwrap();
        assert!(!registered_gpu_environment(&context,&python));
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn gpu_core_reuse_never_replaces_rejected_gpu_packages_with_cpu_wheels(){
        assert!(ensure_gpu_core_reuse(&json!({"reused":["torch","torchaudio","onnxruntime"],
            "rejected":{"funasr-onnx":"新增依赖未安装"}})).is_ok());
        assert!(ensure_gpu_core_reuse(&json!({"reused":["torch","torchaudio"],
            "rejected":{"onnxruntime":"显卡推理包版本不符"}})).is_err());
        assert!(ensure_gpu_core_reuse(&Value::Null).is_err());
    }

    #[test]
    fn missing_current_lock_never_marks_runtime_ready(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-missing-runtime-lock-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),"# 测试启动上下文，不运行此脚本").unwrap();
        let python=root.join("runtimes/python");fs::create_dir_all(&python).unwrap();fs::write(python.join("python.exe"),b"fixture").unwrap();
        fs::write(python.join("runtime.json"),serde_json::to_vec(&json!({"schema":2,"groups":ENVIRONMENT_GROUPS,"packages":[]})).unwrap()).unwrap();
        let context=LaunchContext::portable(&root).unwrap();let state=status(&context).unwrap();
        assert_eq!(state["interpreter_ready"],true);assert_eq!(state["full_ready"],false);
        assert!(ENVIRONMENT_GROUPS.iter().all(|group|state["groups"][*group]["ready"]==false));
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    fn exit_status(code:u32)->ExitStatus {
        #[cfg(windows)] {use std::os::windows::process::ExitStatusExt;ExitStatus::from_raw(code)}
        #[cfg(not(windows))] {use std::os::unix::process::ExitStatusExt;ExitStatus::from_raw((code as i32)<<8)}
    }

    #[test]
    fn startup_failure_reports_real_exception_and_exit_code() {
        let diagnostic=b"Traceback (most recent call last):\r\nModuleNotFoundError: No module named '_socket'\r\n";
        let error=worker_result(b"",diagnostic,exit_status(1),"check").unwrap_err();
        assert!(error.contains("_socket"));assert!(error.contains("退出码 1"));assert!(error.contains("check"));
        assert!(worker_result(br#"{"ok":true,"result":{"ready":true}}"#,diagnostic,exit_status(1),"check").is_err());
    }

    #[test]
    fn worker_accepts_json_after_library_logs_and_blank_lines() {
        let output=b"library output\r\n{\"ok\":true,\"result\":{\"ready\":true}}\r\n\r\n";
        assert_eq!(worker_result(output,b"",exit_status(0),"check").unwrap()["ready"],true);
    }

    #[test]
    fn dependency_progress_accumulates_across_files_and_does_not_rewind_on_retry() {
        let mut totals=DownloadTotals{total_bytes:300,..Default::default()};let mut samples=vec![];
        let mut progress=|_:&str,done,total|samples.push((done,total));
        totals.report(&mut progress,"正在下载第一项",60);totals.report(&mut progress,"正在下载第一项",20);
        totals.completed_bytes=100;totals.current_bytes=0;
        totals.report(&mut progress,"正在下载第二项",0);totals.report(&mut progress,"正在下载第二项",50);totals.report(&mut progress,"正在下载第二项",200);
        totals.report(&mut progress,"组装固定运行依赖",0);
        assert_eq!(&samples[..5],&[(60,300),(60,300),(100,300),(150,300),(300,300)]);
        assert_eq!(samples.last(),Some(&(0,0)));
    }

    #[test]
    fn accelerated_dependency_transfer_keeps_byte_progress_and_fallback_description(){
        let mut totals=DownloadTotals{total_bytes:300,completed_bytes:100,..Default::default()};let mut samples=vec![];
        let mut progress=|phase:&str,done,total|samples.push((phase.to_owned(),done,total));
        totals.transfer_progress("下载固定资源（四路加速）",60,200,&mut progress,"正在下载固定依赖");
        totals.transfer_progress("下载固定资源（四路加速失败，已回退单路下载）",20,200,&mut progress,"正在下载固定依赖");
        totals.transfer_progress("下载固定资源（四路加速失败，已回退单路下载）",150,200,&mut progress,"正在下载固定依赖");
        assert_eq!(samples.iter().map(|(_,done,total)|(*done,*total)).collect::<Vec<_>>(),vec![(160,300),(160,300),(250,300)]);
        assert!(samples[0].0.contains("四路加速"));assert!(samples[1].0.contains("已回退单路"));
    }

    #[test]
    fn cancel_existing_environment_validation_never_downloads_or_activates(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("voice-runtime-cancel-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/runtime")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),"# 测试入口").unwrap();
        fs::write(root.join("app/config/runtime/voice-runtime.lock.json"),include_bytes!("../../../../config/runtime/voice-runtime.lock.json")).unwrap();
        let runtime=root.join("runtimes/python");fs::create_dir_all(&runtime).unwrap();fs::write(runtime.join("python.exe"),b"fixture").unwrap();
        atomic_json(&runtime.join("runtime.json"),&json!({"schema":2,"compatible_gpu":false,"groups":[],"lock_sha256":"old"})).unwrap();
        atomic_json(&runtime.join("manifest.json"),&resources::manifest(&runtime,"python","old").unwrap()).unwrap();
        let original=file_hash(&runtime.join("manifest.json")).unwrap();let stage=root.join("tmp/voice-environment/stage");fs::create_dir_all(&stage).unwrap();fs::write(stage.join("sentinel.bin"),b"prior candidate").unwrap();
        let context=LaunchContext::portable(&root).unwrap();let cancel=Arc::new(AtomicBool::new(false));let mut activated=false;
        let error=configure_with_activation(&context,"all",cancel.clone(),&mut|phase,_,_|{
            if phase=="校验现有语音环境：校验组件文件内容"{cancel.store(true,std::sync::atomic::Ordering::Release);}
        },&mut|_,_|{activated=true;Ok(())}).unwrap_err();
        assert!(error.contains("取消"));assert!(!activated);assert_eq!(file_hash(&runtime.join("manifest.json")).unwrap(),original);
        assert_eq!(fs::read(stage.join("sentinel.bin")).unwrap(),b"prior candidate");assert!(!root.join("cache/dependencies").exists());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    #[cfg(windows)]
    #[ignore = "手动验收：会在指定测试工作区下载并启用完整语音运行环境"]
    fn configure_test_workspace() {
        let root=PathBuf::from(std::env::var_os("VOICE_RUNTIME_TEST_ROOT").expect("请提供 VOICE_RUNTIME_TEST_ROOT（测试工作区）"));
        let mut context=LaunchContext::portable(&root).unwrap();
        if let Some(program)=std::env::var_os("VOICE_RUNTIME_TEST_PROGRAM"){
            context.program=PathBuf::from(program);context.dev=true;
        }
        let mut previous=String::new();let mut expected_total=0;let mut last_percent=0;
        let installed=configure(&context,"all",Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
            if phase=="本次总下载量已确认"{expected_total=total;last_percent=0;}
            let percent=if total>0{done*100/total}else{0};
            if phase.starts_with("正在下载"){assert_eq!(total,expected_total);assert!(percent>=last_percent);last_percent=percent;}
            if previous!=phase{println!("{phase}：{done} / {total}（字节）");previous=phase.into();}
        }).unwrap();
        let record=resources::load(&root.join("runtimes/python/runtime.json")).unwrap();
        assert_eq!(installed,installed_resources(managed_gpu(&record)));assert_eq!(status(&context).unwrap()["full_ready"],true);
        let names=requested(&lock(&context).unwrap(),"all").unwrap();
        let checked=worker(&context,&context.python,"check",&["--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned(),"--names".into(),serde_json::to_string(&names).unwrap()],&AtomicBool::new(false)).unwrap();
        assert_eq!(checked["ready"],true);println!("完整语音环境已在测试工作区启用并复核通过");
    }
}
