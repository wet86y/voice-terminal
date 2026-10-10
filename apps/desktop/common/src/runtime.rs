//! Voice runtime（语音运行环境）：共享依赖计划、可移动解释器、事务启用。
use std::{fs, path::{Path,PathBuf}, process::{Command,ExitStatus,Stdio}, sync::{Arc,atomic::AtomicBool}, time::{Duration,Instant}};
use serde_json::{Value,json};
use crate::{LaunchContext,download::{DownloadFile,download},resources::{self,Progress},workspace::{safe_path,atomic_json,file_hash,remove_owned}};
// ENVIRONMENT_GROUPS（完整语音环境）：语音服务、各模型及轻量 VAD（语音活动检测）所需的六个必需依赖组。
const ENVIRONMENT_GROUPS: [&str; 6] = ["voice-core", "torch-cpu", "onnx-cpu", "sherpa-cpu", "modelscope-download", "onnx-vad"];
pub fn model_group(id:&str)->&'static str {match id {
    "paraformer-streaming"|"fsmn-vad"|"sensevoice-small"=>"torch-cpu",
    "kws-sherpa-onnx"=>"sherpa-cpu",_=>"onnx-cpu"
}}
pub fn lock(context:&LaunchContext)->Result<Value,String>{resources::load(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))}
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
    for key in ENVIRONMENT_GROUPS {
        let ready=installed&&current_digest.is_some()&&record["schema"]==2&&record["groups"].as_array().is_some_and(|g|g.iter().any(|v|v==key))&&record["lock_sha256"].as_str()==current_digest.as_deref();
        groups.insert(key.into(),json!({"ready":ready,"reason":if ready{None}else{Some("解释器或该组固定依赖尚未配置")}}));
    }
    let full_ready=ENVIRONMENT_GROUPS.iter().all(|key|groups[*key]["ready"]==true);
    let update_available=installed&&current_digest.is_some()&&!full_ready;
    Ok(json!({"interpreter_ready":installed,"service_ready":groups["voice-core"]["ready"],"full_ready":full_ready,"python":context.python,"version":record["python"],"update_available":update_available,"groups":groups,
        "fields":{"interpreter_ready":"私有解释器存在","service_ready":"基础语音依赖已验证","full_ready":"六个完整语音依赖组均与当前锁定清单一致","python":"私有解释器路径","version":"解释器版本","update_available":"安装环境的依赖锁版本可更新","groups":"内部共享依赖组","ready":"该组就绪","reason":"未就绪原因"}}))
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
    let lock=lock(context)?;let names=requested(&lock,"all")?;let state=status(context)?;let mut candidates=vec![];
    let cancel=AtomicBool::new(false);
    for path in system_candidates(){
        let result=worker(context,&path,"interpreter",&[],&cancel);
        candidates.push(json!({"path":path,"accepted":result.as_ref().is_ok_and(|v|v["official"]==true),"result":result.unwrap_or_else(|e|json!({"reason":e}))}));
    }
    let wheels:Vec<_>=lock["wheels"].as_array().ok_or("缺少依赖锁")?.iter().filter(|v|names.iter().any(|n|v["name"]==*n)).cloned().collect();
    Ok(json!({"group":group,"already_ready":state["full_ready"].clone(),"python":lock["python"],"wheels":wheels,"system_candidates":candidates,
        "total_bytes":wheels.iter().map(|v|v["size"].as_u64().unwrap_or(0)).sum::<u64>()+lock["python"]["size"].as_u64().unwrap_or(0),
        "fields":{"group":"安装依赖组","already_ready":"完整匹配可跳过","system_candidates":"可复用的官方解释器候选","accepted":"筛选通过","wheels":"固定依赖包","total_bytes":"未扣除复用的最大下载大小"}}))
}
fn spec(value:&Value)->Result<DownloadFile,String>{Ok(DownloadFile{url:value["url"].as_str().ok_or("依赖来源缺失")?.into(),size:value["size"].as_u64().ok_or("大小缺失")?,sha256:value["sha256"].as_str().ok_or("摘要缺失")?.into(),sha512:String::new(),path:value["file"].as_str().unwrap_or("python.zip").into()})}
fn valid_cached(path:&Path,item:&Value)->bool{path.metadata().ok().map(|v|v.len())==item["size"].as_u64()&&file_hash(path).ok().as_deref()==item["sha256"].as_str()}
/// prepare_search_paths（私有解释器搜索路径）：同时兼容系统安装与嵌入式包。
fn prepare_search_paths(root:&Path)->Result<(),String>{
    // DLLs（标准扩展库目录）是系统解释器中 _socket（网络）、_ssl（加密）等模块的位置。
    fs::write(root.join("python312._pth"),"python312.zip\n.\nDLLs\nLib\nLib/site-packages\nimport site\n").map_err(|e|e.to_string())
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
        self.current_bytes=self.current_bytes.max(current);
        progress(phase,self.completed_bytes.saturating_add(self.current_bytes).min(self.total_bytes),self.total_bytes);
    }
    fn transfer(&mut self,file:&DownloadFile,target:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress,phase:&str)->Result<(),String>{
        download(file,target,cancel,&mut|done,_|self.report(progress,phase,done.min(file.size)),0)?;
        self.completed_bytes=self.completed_bytes.saturating_add(file.size);self.current_bytes=0;
        self.report(progress,phase,0);Ok(())
    }
}
fn copy_interpreter(source:&Path,target:&Path,cancel:&AtomicBool)->Result<(),String>{
    let root=source.parent().ok_or("解释器目录无效")?;
    for path in resources::walk(root)? {let relative=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/");
        if relative.starts_with("Lib/site-packages/")||relative.starts_with("Scripts/")||relative.starts_with("Doc/")||relative.ends_with(".pyc"){continue;}
        resources::check_cancel(cancel)?;let out=safe_path(target,&relative)?;fs::create_dir_all(out.parent().unwrap()).map_err(|e|e.to_string())?;fs::copy(path,out).map_err(|e|e.to_string())?;
    }Ok(())
}
pub fn configure(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    resources::recover(context)?; // 单独调用时先恢复；并行宿主在启动及串行启用时恢复事务。
    configure_with_activation(context,group,cancel,progress,&mut|stage,relative|resources::activate(context,stage,relative))
}
pub fn configure_with_activation(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    let _=group; // group（兼容接口参数）：更新运行环境始终原子替换完整依赖集合。
    configure_inner(context,"all",cancel,progress,true,enable)
}
fn configure_inner(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,allow_reuse:bool,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    let lock=lock(context)?;let requested=requested(&lock,group)?;
    let state=status(context)?;
    let old=context.install.join("runtimes/python");
    let existing=resources::load(&old.join("runtime.json")).unwrap_or(Value::Null);
    let compatible_gpu=allow_reuse&&(existing["compatible_gpu"]==true||(existing["schema"]==1&&existing["profile"]=="cu128")||registered_gpu_environment(context,&old));
    let reusable_environment=allow_reuse&&context.python.is_file()&&crate::workspace::verify_tree(&old,"manifest.json").is_ok();
    if compatible_gpu&&!reusable_environment{return Err("现用显卡运行环境完整性校验未通过，原环境保持不变；请修复或导入当前完整显卡环境后重试。".into());}
    let mut check_args=vec!["--names".into(),serde_json::to_string(&requested).unwrap(),"--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned()];
    if compatible_gpu{check_args.push("--compatible-gpu".into());}
    if state["full_ready"]==true&&reusable_environment{
        progress("检测完整语音依赖",0,0);
        if worker(context,&context.python,"check",&check_args,&cancel)?["ready"]==true{progress("复用已校验完整环境",1,1);return Ok(vec!["voice-server".into()]);}
    }
    let stage=safe_path(&context.install,"tmp/voice-environment/stage")?;remove_owned(&stage,&context.install)?;fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
    let site=stage.join("Lib/site-packages");fs::create_dir_all(&site).map_err(|e|e.to_string())?;
    let groups:std::collections::BTreeSet<String>=ENVIRONMENT_GROUPS.iter().map(|key|(*key).to_string()).collect();
    let mut reused=vec![];let mut official=None;let mut embedded=false;
    let maximum=lock["wheels"].as_array().ok_or("依赖锁无效")?.iter().filter(|item|requested.iter().any(|name|item["name"]==*name)).map(|item|item["size"].as_u64().unwrap_or(0)).sum::<u64>()+lock["python"]["size"].as_u64().unwrap_or(0);
    if crate::workspace::available_space(&context.install)?<maximum.saturating_mul(3){return Err("下载、展开和事务备份空间不足".into());}
    if reusable_environment{
        progress("复用已校验私有环境",0,0);resources::copy_verified(&old,&stage,&cancel,progress)?;
        prepare_search_paths(&stage)?;
        let inventory=worker(context,&stage.join("python.exe"),"inventory",&check_args,&cancel)?;
        if compatible_gpu{ensure_gpu_core_reuse(&inventory)?;}
        reused.extend(inventory["reused"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned));
    }else{
        for path in if allow_reuse{system_candidates()}else{vec![]}{if worker(context,&path,"interpreter",&[],&cancel).is_ok_and(|v|v["official"]==true){official=Some(path);break;}}
        if let Some(path)=&official{
            progress("复用官方系统解释器",0,0);copy_interpreter(path,&stage,&cancel)?;
        }else {embedded=true;}
    }
    if let Some(path)=official{progress("核验本机依赖复用条件",0,0);let response=worker(context,&path,"reuse",&["--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned(),"--names".into(),serde_json::to_string(&requested).unwrap(),"--target".into(),site.to_string_lossy().into_owned()],&cancel)?;
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
        if !valid_cached(&target,item){
            if let Some(bundled)=item["bundled"].as_str(){let source=safe_path(&context.program,bundled)?;fs::copy(source,&target).map_err(|e|e.to_string())?;if !valid_cached(&target,item){return Err("内置预编译包摘要错误".into());}}
            else{needs_download=true;}
        }
        resources::check_cancel(&cancel)?;
        wheels.push(RuntimeWheel{file:if needs_download{Some(spec(item)?)}else{None},target});
    }
    let python_cache=safe_path(&context.install,"cache/dependencies/python-3.12.10.zip")?;
    let python_download=embedded&&!valid_cached(&python_cache,&lock["python"]);
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
    let mut downloaded=vec![];
    for item in wheels {
        resources::check_cancel(&cancel)?;
        if let Some(file)=item.file {totals.transfer(&file,&item.target,cancel.clone(),progress,"正在下载语音运行依赖")?;}
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
    resources::check_cancel(&cancel)?;
    let mut packages:std::collections::BTreeSet<String>=reused.into_iter().collect();packages.extend(requested);
    atomic_json(&stage.join("runtime.json"),&json!({"schema":2,"python":"3.12.10","profile":if compatible_gpu{"compatible-private"}else{"cpu-private"},"compatible_gpu":compatible_gpu,"groups":groups,"packages":packages,"lock_sha256":file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?,
        "fields":{"schema":"运行环境记录格式","python":"解释器版本","profile":"私有环境组合","compatible_gpu":"按现有受支持策略复用兼容显卡依赖","groups":"已完成检查的完整依赖组","packages":"已安装依赖名称","lock_sha256":"固定运行依赖清单摘要"}}))?;
    totals.report(progress,"校验语音环境文件清单",0);
    atomic_json(&stage.join("manifest.json"),&resources::manifest(&stage,"python",&crate::workspace::new_id())?)?;
    totals.report(progress,"启用私有语音环境",0);resources::check_cancel(&cancel)?;enable(&stage,"runtimes/python")?;
    Ok(vec!["voice-server".into()])
}
pub fn import(context:&LaunchContext,source:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    resources::recover(context)?;
    import_with_activation(context,source,cancel,progress,&mut|stage,relative|resources::activate(context,stage,relative))
}
pub fn import_with_activation(context:&LaunchContext,source:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    let candidate=safe_path(&context.install,"tmp/voice-import")?;remove_owned(&candidate,&context.install)?;
    if source.is_file(){resources::unpack_zip(source,&candidate,&cancel,progress)?;}else{resources::copy_verified(source,&candidate,&cancel,progress)?;}
    crate::workspace::verify_tree(&candidate,"manifest.json")?;
    let identity=worker(context,&candidate.join("python.exe"),"interpreter",&[],&cancel)?;
    if identity["version"]!="3.12.10"||identity["official"]!=true{return Err("解释器版本或架构不匹配".into());}
    let mut record=resources::load(&candidate.join("runtime.json"))?;
    let current_lock=lock(context)?;
    let lock_sha256=file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?;
    let names=requested(&current_lock,"all")?;
    if !matches!(record["schema"].as_u64(),Some(1|2)){return Err("导入环境记录格式不受支持".into());}
    if record["schema"]==2&&record["lock_sha256"].as_str()!=Some(lock_sha256.as_str()){
        return Err("导入环境的依赖锁版本已过期，请重新配置完整语音环境".into());
    }
    let mut check_args=vec!["--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned()];
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
    atomic_json(&candidate.join("manifest.json"),&resources::manifest(&candidate,"python",&crate::workspace::new_id())?)?;
    resources::check_cancel(&cancel)?;progress("启用导入的语音环境",1,1);resources::check_cancel(&cancel)?;enable(&candidate,"runtimes/python")?;Ok(vec!["voice-server".into()])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn complete_environment_includes_all_model_and_download_dependencies() {
        let lock:Value=serde_json::from_str(include_str!("../../../../config/runtime/voice-runtime.lock.json")).unwrap();
        let names=requested(&lock,"all").unwrap();
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
        totals.report(&mut progress,"下载第一项",60);totals.report(&mut progress,"重试第一项",20);
        totals.completed_bytes=100;totals.current_bytes=0;
        totals.report(&mut progress,"下载第二项",0);totals.report(&mut progress,"下载第二项",50);totals.report(&mut progress,"下载第二项",200);
        assert_eq!(samples,vec![(60,300),(60,300),(100,300),(150,300),(300,300)]);
    }

    #[test]
    #[cfg(windows)]
    #[ignore = "手动验收：会在指定测试工作区下载并启用完整语音运行环境"]
    fn configure_test_workspace() {
        let root=PathBuf::from(std::env::var_os("VOICE_RUNTIME_TEST_ROOT").expect("请提供 VOICE_RUNTIME_TEST_ROOT（测试工作区）"));
        let context=LaunchContext::portable(&root).unwrap();
        let mut previous=String::new();let mut expected_total=0;let mut last_percent=0;
        let installed=configure(&context,"all",Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
            if phase=="本次总下载量已确认"{expected_total=total;last_percent=0;}
            let percent=if total>0{done*100/total}else{0};
            if phase.starts_with("正在下载"){assert_eq!(total,expected_total);assert!(percent>=last_percent);last_percent=percent;}
            if previous!=phase{println!("{phase}：{done} / {total}（字节）");previous=phase.into();}
        }).unwrap();
        assert_eq!(installed,vec!["voice-server"]);assert_eq!(status(&context).unwrap()["full_ready"],true);
        let names=requested(&lock(&context).unwrap(),"all").unwrap();
        let checked=worker(&context,&context.python,"check",&["--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned(),"--names".into(),serde_json::to_string(&names).unwrap()],&AtomicBool::new(false)).unwrap();
        assert_eq!(checked["ready"],true);println!("完整语音环境已在测试工作区启用并复核通过");
    }
}
