//! Voice runtime（语音运行环境）：共享依赖计划、可移动解释器、事务启用。
use std::{fs, path::{Path,PathBuf}, process::{Command,Stdio}, sync::{Arc,atomic::AtomicBool}, time::{Duration,Instant}};
use serde_json::{Value,json};
use crate::{LaunchContext,download::{DownloadFile,download},resources::{self,Progress},workspace::{safe_path,atomic_json,file_hash,remove_owned}};
// ENVIRONMENT_GROUPS（完整语音环境）：一次准备业务、全部本地模型及模型下载所需依赖。
const ENVIRONMENT_GROUPS: [&str; 5] = ["voice-core", "torch-cpu", "onnx-cpu", "sherpa-cpu", "modelscope-download"];
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
fn requested(lock:&Value,group:&str)->Result<Vec<String>,String>{
    let mut names=std::collections::BTreeSet::new();
    let groups=if group=="all" {ENVIRONMENT_GROUPS.to_vec()} else {vec!["voice-core",group]};
    for key in groups {for name in lock["groups"][key].as_array().ok_or("未锁定的依赖组")?{names.insert(name.as_str().ok_or("依赖名称错误")?.to_string());}}
    Ok(names.into_iter().collect())
}
pub fn status(context:&LaunchContext)->Result<Value,String>{
    let record=resources::load(&context.install.join("runtimes/python/runtime.json")).unwrap_or(Value::Null);
    let installed=context.python.is_file();let mut groups=serde_json::Map::new();
    let current=lock(context).ok();
    let legacy=record["schema"]==1&&record["profile"].is_string()&&record["python"]=="3.12.10";
    for key in ENVIRONMENT_GROUPS {
        let digest=current.as_ref().and_then(|_|file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json")).ok());
        let legacy_files=match key {
            "voice-core"=>vec!["fastapi/__init__.py","psutil/__init__.py","yaml/__init__.py","sounddevice.py"],
            "torch-cpu"=>vec!["torch/__init__.py","torchaudio/__init__.py","funasr/__init__.py"],
            "onnx-cpu"=>vec!["onnxruntime/__init__.py","transformers/__init__.py"],
            "sherpa-cpu"=>vec!["sherpa_onnx/__init__.py"],_=>vec!["modelscope/__init__.py"]
        };
        let legacy_ready=legacy&&legacy_files.iter().all(|file|context.install.join("runtimes/python/Lib/site-packages").join(file).is_file());
        let ready=installed&&(legacy_ready||(record["groups"].as_array().is_some_and(|g|g.iter().any(|v|v==key))&&record["lock_sha256"].as_str()==digest.as_deref()));
        groups.insert(key.into(),json!({"ready":ready,"reason":if ready{None}else{Some("解释器或固定依赖尚未配置")}}));
    }
    let full_ready=ENVIRONMENT_GROUPS.iter().all(|key|groups[*key]["ready"]==true);
    Ok(json!({"interpreter_ready":installed,"service_ready":groups["voice-core"]["ready"],"full_ready":full_ready,"python":context.python,"version":record["python"],"legacy_compatible":legacy,"groups":groups,
        "fields":{"interpreter_ready":"私有解释器存在","service_ready":"基础语音依赖已验证","full_ready":"全部语音及下载依赖已就绪","python":"私有解释器路径","version":"解释器版本","legacy_compatible":"兼容现用完整环境","groups":"内部共享依赖组","ready":"该组就绪","reason":"未就绪原因"}}))
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
    let mut command=Command::new(python);context.configure_command(&mut command);
    command.args(["-I","-B","-X","utf8"]).arg(context.program.join("scripts/voice-environment-worker.py")).arg(operation).args(args)
        .stdin(Stdio::null()).stdout(output).stderr(Stdio::null()).current_dir(&context.program);
    #[cfg(windows)] command.creation_flags(0x08000000);
    let mut child=command.spawn().map_err(|e|format!("解释器工作进程无法启动：{e}"))?;
    #[cfg(windows)] let job=crate::windows::Job::new()?;
    #[cfg(windows)] job.assign(child.as_raw_handle())?;
    let deadline=Instant::now()+Duration::from_secs(180);
    loop {if resources::check_cancel(cancel).is_err()||Instant::now()>deadline{let _=child.kill();let _=child.wait();let _=fs::remove_file(output_path);return Err("工作进程已取消或超时".into());}
        if child.try_wait().map_err(|e|e.to_string())?.is_some(){break;}
        std::thread::sleep(Duration::from_millis(80));}
    let bytes=fs::read(&output_path).map_err(|e|e.to_string())?;let _=fs::remove_file(output_path);
    let line=bytes.split(|v|*v==b'\n').rev().find(|line|!line.is_empty()).ok_or("解释器没有返回诊断")?;
    let record:Value=serde_json::from_slice(line).map_err(|_|"解释器诊断格式错误")?;
    if record["ok"]!=true{return Err(record["error"].as_str().unwrap_or("依赖工作进程失败").into());}Ok(record["result"].clone())
}
pub fn plan(context:&LaunchContext,group:&str)->Result<Value,String>{
    let lock=lock(context)?;let names=requested(&lock,group)?;let state=status(context)?;let mut candidates=vec![];
    let cancel=AtomicBool::new(false);
    for path in system_candidates(){
        let result=worker(context,&path,"interpreter",&[],&cancel);
        candidates.push(json!({"path":path,"accepted":result.as_ref().is_ok_and(|v|v["official"]==true),"result":result.unwrap_or_else(|e|json!({"reason":e}))}));
    }
    let wheels:Vec<_>=lock["wheels"].as_array().ok_or("缺少依赖锁")?.iter().filter(|v|names.iter().any(|n|v["name"]==*n)).cloned().collect();
    Ok(json!({"group":group,"already_ready":if group=="all"{state["full_ready"].clone()}else{state["groups"][group]["ready"].clone()},"python":lock["python"],"wheels":wheels,"system_candidates":candidates,
        "total_bytes":wheels.iter().map(|v|v["size"].as_u64().unwrap_or(0)).sum::<u64>()+lock["python"]["size"].as_u64().unwrap_or(0),
        "fields":{"group":"安装依赖组","already_ready":"完整匹配可跳过","system_candidates":"可复用的官方解释器候选","accepted":"筛选通过","wheels":"固定依赖包","total_bytes":"未扣除复用的最大下载大小"}}))
}
fn spec(value:&Value)->Result<DownloadFile,String>{Ok(DownloadFile{url:value["url"].as_str().ok_or("依赖来源缺失")?.into(),size:value["size"].as_u64().ok_or("大小缺失")?,sha256:value["sha256"].as_str().ok_or("摘要缺失")?.into(),sha512:String::new(),path:value["file"].as_str().unwrap_or("python.zip").into()})}
fn valid_cached(path:&Path,item:&Value)->bool{path.metadata().ok().map(|v|v.len())==item["size"].as_u64()&&file_hash(path).ok().as_deref()==item["sha256"].as_str()}
fn copy_interpreter(source:&Path,target:&Path,cancel:&AtomicBool)->Result<(),String>{
    let root=source.parent().ok_or("解释器目录无效")?;
    for path in resources::walk(root)? {let relative=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/");
        if relative.starts_with("Lib/site-packages/")||relative.starts_with("Scripts/")||relative.starts_with("Doc/")||relative.ends_with(".pyc"){continue;}
        resources::check_cancel(cancel)?;let out=safe_path(target,&relative)?;fs::create_dir_all(out.parent().unwrap()).map_err(|e|e.to_string())?;fs::copy(path,out).map_err(|e|e.to_string())?;
    }Ok(())
}
pub fn configure(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    let _=group; // group（历史分组参数）仅保留接口兼容，配置始终检查完整环境。
    configure_inner(context,"all",cancel,progress,true)
}
fn configure_inner(context:&LaunchContext,group:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,allow_reuse:bool)->Result<Vec<String>,String>{
    resources::recover(context)?;let lock=lock(context)?;let mut requested=requested(&lock,group)?;
    let state=status(context)?;
    let old=context.install.join("runtimes/python");
    let existing=resources::load(&old.join("runtime.json")).unwrap_or(Value::Null);
    let compatible_gpu=allow_reuse&&(state["legacy_compatible"]==true||existing["compatible_gpu"]==true);
    let mut check_args=vec!["--names".into(),serde_json::to_string(&requested).unwrap(),"--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned()];
    if compatible_gpu{check_args.push("--compatible-gpu".into());}
    if allow_reuse&&state["full_ready"]==true&&crate::workspace::verify_tree(&old,"manifest.json").is_ok(){
        progress("检测完整语音运行依赖",0,0);
        if worker(context,&context.python,"check",&check_args,&cancel)?["ready"]==true{progress("复用已校验完整环境",1,1);return Ok(vec!["voice-server".into()]);}
    }
    let stage=safe_path(&context.install,"tmp/voice-environment/stage")?;remove_owned(&stage,&context.install)?;fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
    let site=stage.join("Lib/site-packages");fs::create_dir_all(&site).map_err(|e|e.to_string())?;
    let mut groups:std::collections::BTreeSet<String>=ENVIRONMENT_GROUPS.iter().map(|key|(*key).to_string()).collect();
    let mut reused=vec![];let mut official=None;
    if !allow_reuse {
        if let Some(values)=existing["groups"].as_array(){for value in values{if let Some(text)=value.as_str(){groups.insert(text.into());}}}
        for installed_group in &groups {requested.extend(self::requested(&lock,installed_group)?);}
        requested.sort();requested.dedup();
    }
    let maximum=lock["wheels"].as_array().ok_or("依赖锁无效")?.iter().filter(|item|requested.iter().any(|name|item["name"]==*name)).map(|item|item["size"].as_u64().unwrap_or(0)).sum::<u64>()+lock["python"]["size"].as_u64().unwrap_or(0);
    if crate::workspace::available_space(&context.install)?<maximum.saturating_mul(3){return Err("下载、展开和事务备份空间不足".into());}
    if allow_reuse&&crate::workspace::verify_tree(&old,"manifest.json").is_ok()&&(state["legacy_compatible"]==true||existing["lock_sha256"].as_str()==Some(&file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?))&&context.python.is_file(){
        progress("复用已校验私有环境",0,0);resources::copy_verified(&old,&stage,&cancel,progress)?;
        if let Some(values)=existing["groups"].as_array(){for value in values{if let Some(text)=value.as_str(){groups.insert(text.into());}}}
        let inventory=worker(context,&stage.join("python.exe"),"inventory",&check_args,&cancel)?;
        reused.extend(inventory["reused"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned));
    }else{
        for path in if allow_reuse{system_candidates()}else{vec![]}{if worker(context,&path,"interpreter",&[],&cancel).is_ok_and(|v|v["official"]==true){official=Some(path);break;}}
        if let Some(path)=&official{
            progress("复用官方系统解释器",0,0);copy_interpreter(path,&stage,&cancel)?;
        }else {
            progress("准备程序私有解释器",0,0);let cache=safe_path(&context.install,"cache/dependencies/python-3.12.10.zip")?;fs::create_dir_all(cache.parent().unwrap()).map_err(|e|e.to_string())?;
            if !valid_cached(&cache,&lock["python"]){download(&spec(&lock["python"])?,&cache,cancel.clone(),&mut|done,total|progress("正在下载官方解释器",done,total),0)?;}
            resources::unpack_zip(&cache,&stage,&cancel,progress)?;
        }
    }
    // ._pth（解释器搜索路径）只写相对目录，禁止继承系统或开发环境路径。
    fs::write(stage.join("python312._pth"),"python312.zip\n.\nLib\nLib/site-packages\nimport site\n").map_err(|e|e.to_string())?;
    // 应用本地 CRT（C++ 运行库）随本体携带，仅释放到私有解释器旁。
    let crt=context.program.join("tools/python-crt");
    if crt.is_dir(){for path in resources::walk(&crt)?{let name=path.file_name().ok_or("运行库文件名无效")?;resources::copy_file(&path,&stage.join(name),&cancel,progress)?;}}
    let interpreter=stage.join("python.exe");
    if let Some(path)=official{progress("核验本机依赖复用条件",0,0);let response=worker(context,&path,"reuse",&["--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned(),"--names".into(),serde_json::to_string(&requested).unwrap(),"--target".into(),site.to_string_lossy().into_owned()],&cancel)?;
        reused.extend(response["reused"].as_array().unwrap_or(&vec![]).iter().filter_map(Value::as_str).map(str::to_owned));
    }
    let cache=safe_path(&context.install,"cache/dependencies/wheels")?;fs::create_dir_all(&cache).map_err(|e|e.to_string())?;
    let mut downloaded=vec![];
    for item in lock["wheels"].as_array().ok_or("依赖锁无效")? {
        let name=item["name"].as_str().ok_or("依赖名称缺失")?;
        if !requested.iter().any(|v|v==name)||reused.iter().any(|v|v==name){continue;}
        let file=item["file"].as_str().ok_or("依赖文件名缺失")?;let target=safe_path(&cache,file)?;
        if !valid_cached(&target,item){
            if let Some(bundled)=item["bundled"].as_str(){let source=safe_path(&context.program,bundled)?;fs::copy(source,&target).map_err(|e|e.to_string())?;if !valid_cached(&target,item){return Err("内置预编译包摘要错误".into());}}
            else{download(&spec(item)?,&target,cancel.clone(),&mut|done,total|progress(&format!("下载 {name}（依赖）"),done,total),0)?;}
        }
        downloaded.push(target.to_string_lossy().into_owned());
    }
    let args=vec!["--target".into(),site.to_string_lossy().into_owned(),"--wheels".into(),serde_json::to_string(&downloaded).unwrap()];
    progress("组装固定运行依赖",0,0);worker(context,&interpreter,"install",&args,&cancel)?;
    let checked=worker(context,&interpreter,"check",&check_args,&cancel)?;
    if checked["ready"]!=true && !reused.is_empty(){
        // DLL（动态链接库）闭包或本机路径失败：自动从固定包重新组装，缓存继续复用。
        remove_owned(&stage,&context.install)?;
        progress("本机复用未通过加载检查，改用固定下载包",0,0);
        return configure_inner(context,group,cancel,progress,false);
    }
    if checked["ready"]!=true{return Err(format!("运行依赖加载失败：{}",checked["errors"]));}
    resources::check_cancel(&cancel)?;
    let mut packages:std::collections::BTreeSet<String>=reused.into_iter().collect();packages.extend(requested);
    atomic_json(&stage.join("runtime.json"),&json!({"schema":2,"python":"3.12.10","profile":if compatible_gpu{"compatible-private"}else{"cpu-private"},"compatible_gpu":compatible_gpu,"groups":groups,"packages":packages,"lock_sha256":file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?,
        "fields":{"schema":"运行环境记录格式","python":"解释器版本","profile":"私有环境组合","compatible_gpu":"保留已安装的兼容显卡依赖","groups":"已完成检查的内部共享依赖组","packages":"已安装依赖名称","lock_sha256":"固定运行依赖清单摘要"}}))?;
    atomic_json(&stage.join("manifest.json"),&resources::manifest(&stage,"python",&crate::workspace::new_id())?)?;
    progress("启用私有语音环境",1,1);resources::check_cancel(&cancel)?;resources::activate(context,&stage,"runtimes/python")?;
    Ok(vec!["voice-server".into()])
}
pub fn import(context:&LaunchContext,source:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    let candidate=safe_path(&context.install,"tmp/voice-import")?;remove_owned(&candidate,&context.install)?;
    if source.is_file(){resources::unpack_zip(source,&candidate,&cancel,progress)?;}else{resources::copy_verified(source,&candidate,&cancel,progress)?;}
    crate::workspace::verify_tree(&candidate,"manifest.json")?;
    let identity=worker(context,&candidate.join("python.exe"),"interpreter",&[],&cancel)?;
    if identity["version"]!="3.12.10"||identity["official"]!=true{return Err("解释器版本或架构不匹配".into());}
    let mut record=resources::load(&candidate.join("runtime.json"))?;
    let mut names=requested(&lock(context)?,"all")?;
    let mut check_args=vec!["--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned()];
    if record["schema"]==1||record["compatible_gpu"]==true{check_args.push("--compatible-gpu".into());}
    if record["schema"]==2 {
        if record["lock_sha256"].as_str()!=Some(&file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?){return Err("导入环境与当前固定依赖清单不匹配".into());}
        for group in record["groups"].as_array().ok_or("导入环境缺少已验证依赖组")?{names.extend(requested(&lock(context)?,group.as_str().ok_or("依赖组名称无效")?)?);}
        names.sort();names.dedup();
    }
    check_args.extend(["--names".into(),serde_json::to_string(&names).unwrap()]);
    if worker(context,&candidate.join("python.exe"),"check",&check_args,&cancel)?["ready"]!=true{return Err("导入环境的完整语音依赖无法加载".into());}
    if record["schema"]==2{
        record["groups"]=json!(ENVIRONMENT_GROUPS);
        atomic_json(&candidate.join("runtime.json"),&record)?;
        atomic_json(&candidate.join("manifest.json"),&resources::manifest(&candidate,"python",&crate::workspace::new_id())?)?;
    }
    resources::check_cancel(&cancel)?;progress("启用导入的语音环境",1,1);resources::check_cancel(&cancel)?;resources::activate(context,&candidate,"runtimes/python")?;Ok(vec!["voice-server".into()])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn complete_environment_includes_all_model_and_download_dependencies() {
        let lock:Value=serde_json::from_str(include_str!("../../../../config/runtime/voice-runtime.lock.json")).unwrap();
        let names=requested(&lock,"all").unwrap();
        for name in ["fastapi","torch","torchaudio","funasr","onnxruntime","sherpa-onnx","modelscope"] {
            assert!(names.iter().any(|item|item==name),"完整环境缺少 {name}（依赖）");
        }
        assert_eq!(names.len(),names.iter().collect::<std::collections::BTreeSet<_>>().len());
    }
}
