//! Native resources（原生资源）：模型文件与 DSH 安装不需要 Python（后端解释器）。
use std::{fs, io::{Read, Write}, path::{Path, PathBuf}, sync::{Arc, atomic::{AtomicBool, Ordering}}, process::{Command, Stdio}};
use serde_json::{Value, json};
use sha2::{Digest, Sha512};
use base64::Engine;
use crate::{LaunchContext, download::{DownloadFile, download}, workspace::{safe_path, file_hash, atomic_json, remove_owned}};
pub type Progress<'a> = dyn FnMut(&str,u64,u64) + 'a; // Progress（阶段与字节进度回调）。

pub fn load(path:&Path)->Result<Value,String>{
    let bytes=fs::read(path).map_err(|e|e.to_string())?;
    serde_json::from_slice(bytes.strip_prefix(&[0xef,0xbb,0xbf]).unwrap_or(&bytes)).map_err(|e|e.to_string())
}
pub fn specs(context:&LaunchContext)->Result<Vec<Value>,String>{
    Ok(load(&context.installed_program().join("config/resources/catalog.json"))?["resources"].as_array().ok_or("资源目录格式无效")?.clone())
}
pub fn spec(context:&LaunchContext,id:&str)->Result<Value,String>{specs(context)?.into_iter().find(|v|v["resource_id"]==id).ok_or("不支持的资源编号".into())}
fn destination(context:&LaunchContext,spec:&Value)->Result<PathBuf,String>{
    let relative=spec["destination"].as_str().ok_or("资源缺少固定目录")?;
    safe_path(&context.install,&if spec["kind"]=="model"{format!("models/{relative}")}else{relative.into()})
}
fn file_spec(value:&Value,url:String)->Result<DownloadFile,String>{
    Ok(DownloadFile{path:value["path"].as_str().or(value["filename"].as_str()).ok_or("文件缺少名称")?.into(),url,
        size:value["size_bytes"].as_u64().ok_or("文件缺少大小")?,sha256:value["sha256"].as_str().unwrap_or("").into(),
        sha512:value["sha512_base64"].as_str().unwrap_or("").into()})
}
pub fn walk(root:&Path)->Result<Vec<PathBuf>,String>{
    fn visit(base:&Path,folder:&Path,out:&mut Vec<PathBuf>)->Result<(),String>{
        for entry in fs::read_dir(folder).map_err(|e|e.to_string())? {
            let path=entry.map_err(|e|e.to_string())?.path();
            let relative=path.strip_prefix(base).map_err(|e|e.to_string())?.to_string_lossy().replace('\\',"/");
            safe_path(base,&relative)?;
            if path.is_dir(){visit(base,&path,out)?;}else if path.is_file(){out.push(path);}
        }Ok(())
    }
    let mut out=Vec::new();visit(root,root,&mut out)?;out.sort();Ok(out)
}
pub fn manifest(root:&Path,kind:&str,build:&str)->Result<Value,String>{
    let mut files=vec![];
    for path in walk(root)?{if path==root.join("manifest.json"){continue;}
        files.push(json!({"path":path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/"),"size":path.metadata().map_err(|e|e.to_string())?.len(),"sha256":file_hash(&path)?}));}
    Ok(json!({"schema":1,"kind":kind,"build":build,"files":files,"fields":{"schema":"清单格式","kind":"资源类别","build":"版本标识","files":"文件列表","path":"相对路径","size":"字节数","sha256":"文件摘要"}}))
}
pub fn model_validate(context:&LaunchContext,spec:&Value,source:&Path)->Result<(),String>{
    validate_model_files(context,spec,source,false)
}
fn model_validate_source(context:&LaunchContext,spec:&Value,source:&Path)->Result<(),String>{
    validate_model_files(context,spec,source,true)
}
fn validate_model_files(context:&LaunchContext,spec:&Value,source:&Path,fill_assets:bool)->Result<(),String>{
    for file in spec["files"].as_array().ok_or("模型文件清单无效")? {
        let name=file["path"].as_str().ok_or("缺少模型文件名称")?;
        let path=if fill_assets&&file["asset"].is_string(){safe_path(&context.program,file["asset"].as_str().unwrap())?}else{safe_path(source,name)?};
        if !path.is_file()&&file["optional"]==true{continue;}
        if !path.is_file(){return Err(format!("缺少文件：{name}"));}
        if path.metadata().map_err(|e|e.to_string())?.len()!=file["size_bytes"].as_u64().ok_or("锁定大小缺失")? || file_hash(&path)?!=file["sha256"].as_str().ok_or("锁定摘要缺失")? {
            return Err(format!("版本、大小或摘要不匹配：{name}"));
        }
    }Ok(())
}
pub fn inventory(context:&LaunchContext)->Result<Vec<Value>,String>{
    let runtime=crate::runtime::status(context)?;let groups=runtime["groups"].clone();
    let mut rows=vec![];
    for item in specs(context)? {
        let root=destination(context,&item)?;
        // 状态查询只检查大小，完整摘要在导入、安装及显卡检测时校验。
        let result=if item["kind"]=="model"{(item["files"].as_array().ok_or("模型清单无效")?).iter().filter(|v|v["optional"]!=true).try_for_each(|file|{
            let path=safe_path(&root,file["path"].as_str().ok_or("缺少文件名")?)?;
            if path.metadata().ok().map(|v|v.len())!=file["size_bytes"].as_u64(){return Err(format!("缺少或不完整：{}",file["path"]));}Ok(())
        })}else{dsh_validate(context,&root,false)};
        let group=crate::runtime::model_group(item["resource_id"].as_str().unwrap_or(""));
        let dependencies=if item["kind"]=="dsh"{true}else{groups[group]["ready"]==true};
        rows.push(json!({"resource_id":item["resource_id"],"label":item["title"],"installation_state":if result.is_ok(){"installed"}else if root.exists(){"incomplete"}else{"missing"},
            "files_installed":result.is_ok(),"runtime_ready":dependencies,"can_start":result.is_ok()&&dependencies,
            "source_url":item["source"]["page_url"],"error":result.err(),"runtime_group":group,
            "fields":{"files_installed":"模型或组件文件已安装","runtime_ready":"共享运行依赖已就绪","can_start":"具备启动条件","runtime_group":"共享依赖组"}}));
    }Ok(rows)
}
pub fn unpack_zip(source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let mut archive=zip::ZipArchive::new(fs::File::open(source).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    let mut total=0u64;let mut names=std::collections::HashSet::new();
    for index in 0..archive.len(){let file=archive.by_index(index).map_err(|e|e.to_string())?;
        safe_path(target,file.name().trim_end_matches('/'))?;
        if !names.insert(file.name().to_lowercase())||file.unix_mode().is_some_and(|v|v&0o170000==0o120000){return Err("归档包含链接或重复文件".into());}
        total=total.checked_add(file.size()).ok_or("归档大小溢出")?;
        if total>64*1024*1024*1024{return Err("归档展开超过 64 GiB（吉二进制字节）上限".into());}
    }
    fs::create_dir_all(target).map_err(|e|e.to_string())?;
    if crate::workspace::available_space(target)?<total{return Err("解包空间不足".into());}
    let mut done=0;let mut buffer=vec![0;65536];
    for index in 0..archive.len(){let mut file=archive.by_index(index).map_err(|e|e.to_string())?;let path=safe_path(target,file.name().trim_end_matches('/'))?;
        if file.is_dir(){fs::create_dir_all(path).map_err(|e|e.to_string())?;continue;}
        fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;let mut output=fs::File::create(path).map_err(|e|e.to_string())?;
        loop {check_cancel(cancel)?;let count=file.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}
            output.write_all(&buffer[..count]).map_err(|e|e.to_string())?;done+=count as u64;progress("正在解包",done,total);}
    }Ok(())
}
pub fn check_cancel(cancel:&AtomicBool)->Result<(),String>{if cancel.load(Ordering::Acquire){Err("操作已取消；下载缓存保留。".into())}else{Ok(())}}
pub fn copy_file(source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    fs::create_dir_all(target.parent().ok_or("复制目标缺少父目录")?).map_err(|e|e.to_string())?;
    let total=source.metadata().map_err(|e|e.to_string())?.len();let mut done=0u64;
    let mut input=fs::File::open(source).map_err(|e|e.to_string())?;let mut output=fs::File::create(target).map_err(|e|e.to_string())?;
    let mut buffer=vec![0u8;65536];
    loop {check_cancel(cancel)?;let count=input.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}
        output.write_all(&buffer[..count]).map_err(|e|e.to_string())?;done+=count as u64;progress("正在复制并校验",done,total);}
    output.sync_all().map_err(|e|e.to_string())?;drop(output);
    check_cancel(cancel)?;if file_hash(source)?!=file_hash(target)?{return Err("复制校验失败".into());}Ok(())
}
pub fn copy_verified(source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let files=walk(source)?;let total=files.iter().map(|p|p.metadata().map(|v|v.len()).unwrap_or(0)).sum();let mut done=0u64;
    for file in files {check_cancel(cancel)?;let relative=file.strip_prefix(source).unwrap().to_string_lossy().replace('\\',"/");let out=safe_path(target,&relative)?;
        copy_file(&file,&out,cancel,&mut|phase,bytes,_|progress(phase,done+bytes,total))?;done+=file.metadata().map_err(|e|e.to_string())?.len();}
    Ok(())
}
// activate（事务启用）：安装记录绑定固定受管目标；启动时回滚未完成替换。
pub fn recover(context:&LaunchContext)->Result<(),String>{
    let journal=context.install.join("tmp/native-install.json");if !journal.is_file(){return Ok(());}
    let record=load(&journal)?;let relative=record["target"].as_str().ok_or("安装记录缺少目标")?;
    if !(relative.starts_with("models/")||["runtimes/python","runtimes/dsh"].contains(&relative)){return Err("安装记录目标不在受管资源范围".into());}
    let target=safe_path(&context.install,relative)?;
    let old=safe_path(&context.install,"tmp/native-old")?;
    if record["state"]!="committed" {
        if old.exists(){remove_owned(&target,&context.install)?;fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(&old,&target).map_err(|e|e.to_string())?;}
        else if record["original"]==false{remove_owned(&target,&context.install)?;}
    }
    if record["state"]=="committed" {
        if relative.starts_with("models/"){refresh_models(context)?;}
        else if relative=="runtimes/dsh" {bind_dsh(context)?;}
        else if relative=="runtimes/python" {bind_component(context,"python","runtimes/python",&target)?;}
    }
    remove_owned(&old,&context.install)?;fs::remove_file(journal).map_err(|e|e.to_string())?;Ok(())
}
pub fn activate(context:&LaunchContext,stage:&Path,relative:&str)->Result<(),String>{
    recover(context)?;let target=safe_path(&context.install,relative)?;let old=safe_path(&context.install,"tmp/native-old")?;
    let journal=context.install.join("tmp/native-install.json");
    atomic_json(&journal,&json!({"target":relative,"state":"prepared","original":target.exists(),"fields":{"target":"受管相对路径","state":"启用阶段","original":"原目录是否存在"}}))?;
    if target.exists(){fs::rename(&target,&old).map_err(|e|e.to_string())?;}
    fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;
    if let Err(e)=fs::rename(stage,&target){let _=recover(context);return Err(e.to_string());}
    atomic_json(&journal,&json!({"target":relative,"state":"committed"}))?;
    recover(context)
}
pub fn import(context:&LaunchContext,source:&Path,id:Option<&str>,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    import_review(context,source,id,cancel,progress,&mut|_|Ok(()))
}
pub fn import_review(context:&LaunchContext,source:&Path,id:Option<&str>,cancel:Arc<AtomicBool>,progress:&mut Progress,review:&mut dyn FnMut(&[Value])->Result<(),String>)->Result<Vec<String>,String>{
    let task=safe_path(&context.install,"tmp/native-resources")?;remove_owned(&task,&context.install)?;fs::create_dir_all(&task).map_err(|e|e.to_string())?;
    let mut inputs=vec![];
    if source.is_file()&&source.extension().is_some_and(|v|v.eq_ignore_ascii_case("zip")) {
        let unpacked=task.join("bundle");unpack_zip(source,&unpacked,&cancel,progress)?;
        let index=load(&unpacked.join("resources.json"))?;
        if index["schema_version"]!=1{return Err("资源包格式版本不支持".into());}
        let mut ids=std::collections::HashSet::new();
        for entry in index["resources"].as_array().ok_or("资源包清单无效")? {
            let key=entry["resource_id"].as_str().ok_or("资源包缺少编号")?;
            if !ids.insert(key){return Err("资源包重复编号".into());}if id.is_some_and(|v|v!=key){continue;}
            let resource=spec(context,key)?;if resource["version"]!=entry["version"]&&key!="dsh"{return Err(format!("资源版本不匹配：{key}"));}
            let stage=task.join(format!("candidate-{key}"));fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
            for row in entry["files"].as_array().ok_or("资源包缺少文件映射")? {
                let input=safe_path(&unpacked,row["package_path"].as_str().ok_or("资源包文件来源缺失")?)?;
                let output=safe_path(&stage,row["path"].as_str().ok_or("资源包文件名称缺失")?)?;
                copy_file(&input,&output,&cancel,progress)?;
            }
            inputs.push((key.to_owned(),stage));
        }
    }else{inputs.push((id.ok_or("文件夹导入需要指定组件")?.into(),source.to_path_buf()));}
    if inputs.is_empty(){return Err("没有识别到可导入资源".into());}
    // 整包预校验在替换任何现有资源之前完成。
    for (key,path) in &inputs {progress(&format!("预校验 {key}（资源编号）"),0,0);
        if key=="dsh" {if path.is_dir(){dsh_validate(context,path,true)?;}else{verify_installer(context,path)?;}}
        else{model_validate_source(context,&spec(context,key)?,path)?;}}
    let review_items=inputs.iter().map(|(key,_)|{
        let resource=spec(context,key)?;
        Ok(json!({"resource_id":key,"title":resource["title"],"version":resource["version"],"files_verified":true,
            "fields":{"resource_id":"已识别资源编号","title":"组件名称","version":"固定版本","files_verified":"整包预校验已通过"}}))
    }).collect::<Result<Vec<Value>,String>>()?;
    review(&review_items)?;check_cancel(&cancel)?;
    let mut installed=vec![];
    for (key,path) in inputs {check_cancel(&cancel)?;let resource=spec(context,&key)?;let candidate=task.join(format!("install-{key}"));
        let current=destination(context,&resource)?;
        let identical=if key=="dsh" {path.is_dir()&&file_hash(&path.join("manifest.json")).ok()==file_hash(&current.join("manifest.json")).ok()&&dsh_validate(context,&current,true).is_ok()}
            else{model_validate(context,&resource,&current).is_ok()&&resource["files"].as_array().unwrap().iter().filter(|v|v["optional"]==true).all(|v|{
                let name=v["path"].as_str().unwrap_or("");!path.join(name).is_file()||current.join(name).is_file()
            })};
        if identical{progress(&format!("跳过相同完整资源：{key}（编号）"),1,1);installed.push(key);continue;}
        if key=="dsh"&&path.is_file(){extract_dsh(context,&path,&candidate,&cancel,progress)?;}
        else if key=="dsh"{copy_verified(&path,&candidate,&cancel,progress)?;dsh_validate(context,&candidate,true)?;}
        else {
            for row in resource["files"].as_array().unwrap(){let name=row["path"].as_str().unwrap();let from=if let Some(asset)=row["asset"].as_str(){safe_path(&context.program,asset)?}else{safe_path(&path,name)?};
                if !from.is_file()&&row["optional"]==true{continue;}check_cancel(&cancel)?;let to=safe_path(&candidate,name)?;copy_file(&from,&to,&cancel,progress)?;}
            model_validate(context,&resource,&candidate)?;
        }
        check_cancel(&cancel)?;progress("正在启用资源",1,1);check_cancel(&cancel)?;
        let relative=destination(context,&resource)?.strip_prefix(&context.install).unwrap().to_string_lossy().replace('\\',"/");activate(context,&candidate,&relative)?;
        installed.push(key);
    }
    remove_owned(&task,&context.install)?;Ok(installed)
}
fn refresh_models(context:&LaunchContext)->Result<(),String>{
    let root=context.install.join("models");let record=manifest(&root,"models",&crate::workspace::new_id())?;atomic_json(&root.join("manifest.json"),&record)?;bind_component(context,"models","models",&root)
}
pub fn download_resource(context:&LaunchContext,id:&str,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    let item=spec(context,id)?;
    let installed=destination(context,&item)?;
    if (id=="dsh"&&dsh_validate(context,&installed,false).is_ok())||(id!="dsh"&&model_validate(context,&item,&installed).is_ok()) {progress("复用已校验资源",1,1);return Ok(vec![id.into()]);}
    let root=safe_path(&context.install,&format!("tmp/resource-downloads/{id}"))?;fs::create_dir_all(&root).map_err(|e|e.to_string())?;
    if item["source"]["transfer"]["kind"]=="modelscope_sdk" {return Err("ModelScope（魔搭）任务需要下载工作进程。".into());}
    if let Some(artifact)=item["source"]["transfer"]["artifact"].as_object(){
        let value=Value::Object(artifact.clone());let spec=file_spec(&value,value["url"].as_str().ok_or("缺少下载来源")?.into())?;let path=safe_path(&root,&spec.path)?;
        download(&spec,&path,cancel.clone(),&mut|done,total|progress("正在下载固定附件",done,total),0)?;
        if id=="dsh"{return import(context,&path,Some(id),cancel,progress);}
        let seven=context.program.join("tools/7zip/7z.exe");let out=root.join("extracted");run_seven(&seven,&path,&out,&cancel)?;
        let tar=walk(&out)?.into_iter().find(|p|p.extension().is_some_and(|v|v=="tar")).ok_or("模型归档缺少 tar（内层归档）")?;
        let model=root.join("model");run_seven(&seven,&tar,&model,&cancel)?;
        let folder=walk(&model)?.into_iter().find(|p|p.file_name().is_some_and(|v|v=="tokens.txt")).ok_or("唤醒词归档不完整")?.parent().unwrap().to_path_buf();
        return import(context,&folder,Some(id),cancel,progress);
    }
    for row in item["files"].as_array().ok_or("下载文件清单缺失")? {
        if row["optional"]==true||row["asset"].is_string(){continue;}
        let url=format!("https://huggingface.co/{}/resolve/{}/{}",item["source"]["repo_id"].as_str().ok_or("仓库缺失")?,item["source"]["revision"].as_str().ok_or("版本缺失")?,row["path"].as_str().ok_or("文件名缺失")?);
        let file=file_spec(row,url)?;let path=safe_path(&root,&file.path)?;fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;
        download(&file,&path,cancel.clone(),&mut|done,total|progress("正在下载模型文件",done,total),0)?;
    }
    import(context,&root,Some(id),cancel,progress)
}
fn verify_installer(context:&LaunchContext,path:&Path)->Result<String,String>{
    let item=spec(context,"dsh")?;let artifact=&item["source"]["transfer"]["artifact"];
    if path.metadata().map_err(|e|e.to_string())?.len()!=artifact["size_bytes"].as_u64().unwrap_or(0){return Err("DSH 官方安装器大小不匹配".into());}
    let mut stream=fs::File::open(path).map_err(|e|e.to_string())?;let mut digest=Sha512::new();let mut buffer=vec![0;65536];
    loop {let count=stream.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}digest.update(&buffer[..count]);}
    let value=digest.finalize();if base64::engine::general_purpose::STANDARD.encode(value)!=artifact["sha512_base64"].as_str().unwrap_or(""){return Err("DSH 官方安装器摘要不匹配".into());}Ok(format!("{value:x}"))
}
fn run_seven(exe:&Path,source:&Path,target:&Path,cancel:&AtomicBool)->Result<(),String>{
    #[cfg(windows)] use std::os::windows::{process::CommandExt,io::AsRawHandle};
    fs::create_dir_all(target).map_err(|e|e.to_string())?;let mut command=Command::new(exe);
    command.arg("x").arg(source).arg(format!("-o{}",target.display())).args(["-y","-bd"]).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)] command.creation_flags(0x08000000);
    let mut child=command.spawn().map_err(|e|e.to_string())?;
    #[cfg(windows)] let job=crate::windows::Job::new()?;
    #[cfg(windows)] job.assign(child.as_raw_handle())?;
    loop {if cancel.load(Ordering::Acquire){let _=child.kill();let _=child.wait();return Err("解包已取消".into());}
        if let Some(status)=child.try_wait().map_err(|e|e.to_string())? {if !status.success(){return Err("7-Zip（压缩工具）解包失败".into());}break;}
        std::thread::sleep(std::time::Duration::from_millis(80));}
    walk(target)?;Ok(())
}
fn extract_dsh(context:&LaunchContext,source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let sha=verify_installer(context,source)?;let seven=context.program.join("tools/7zip/7z.exe");let staging=target.with_extension("extract");let outer=staging.join("nsis");
    progress("正在提取官方安装器",0,0);run_seven(&seven,source,&outer,cancel)?;
    let matches:Vec<_>=walk(&outer)?.into_iter().filter(|p|p.file_name().is_some_and(|n|n=="app-64.7z")).collect();if matches.len()!=1{return Err("官方安装器中无法唯一识别应用负载".into());}
    let inner=staging.join("payload");run_seven(&seven,&matches[0],&inner,cancel)?;
    let candidates:Vec<_>=walk(&inner)?.into_iter().filter(|p|p.file_name().is_some_and(|n|n=="DeepSeek Harness.exe")&&p.parent().unwrap().join("resources/app.asar").is_file()).collect();
    if candidates.len()!=1{return Err("官方应用目录无法唯一识别".into());}copy_verified(candidates[0].parent().unwrap(),target,cancel,progress)?;
    let lock=load(&context.program.join("config/dsh/runtime.lock.json"))?;
    atomic_json(&target.join("runtime.json"),&json!({"schema":2,"kind":"official_desktop","source_kind":"official_desktop","version":lock["version"],"bridge_protocol":"2.0","compat_expected_commit":lock["commit"],"source_sha512":sha,
        "executable":"DeepSeek Harness.exe","app_asar":"resources/app.asar","entry":"resources/app.asar/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js",
        "schedule":"resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-schedule/lib/index.js",
        "office_cli":"resources/app.asar.unpacked/dsh/node_modules/@deepseek-ai/libreoffice-kit/lib/cli.js",
        "office_engine":"resources/app.asar.unpacked/dsh/node_modules/@deepseek-ai/libreoffice-kit-win32-x64","asar_unpacked_root":"resources/app.asar.unpacked/dsh"}))?;
    atomic_json(&target.join("manifest.json"),&manifest(target,"dsh",&format!("official-{}-{}",lock["version"].as_str().unwrap_or(""),&sha[..16]))?)?;
    remove_owned(&staging,&context.install)?;Ok(())
}
fn dsh_validate(context:&LaunchContext,source:&Path,full:bool)->Result<(),String>{
    let metadata=load(&source.join("runtime.json"))?;let digest=file_hash(&source.join("manifest.json"))?;let item=spec(context,"dsh")?;
    if metadata["source_kind"]=="official_desktop" {
        let expected=base64::engine::general_purpose::STANDARD.decode(item["source"]["transfer"]["artifact"]["sha512_base64"].as_str().ok_or("官方摘要缺失")?).map_err(|e|e.to_string())?;
        let expected=expected.iter().map(|byte|format!("{byte:02x}")).collect::<String>();
        if metadata["version"]!=item["version"]||metadata["source_sha512"]!=expected{return Err("DSH（代理宿主）版本或官方来源不匹配".into());}
        let trusted=load(&context.program.join("config/dsh/official-runtime.lock.json")).ok();
        let installed=load(&context.data.join("state/native-dsh-binding.json")).ok();
        if ![trusted,installed].iter().flatten().any(|v|v["manifest_sha256"]==digest&&v["version"]==metadata["version"]&&v["source_sha512"]==metadata["source_sha512"]){return Err("提取目录缺少程序认可的可信摘要，请导入锁定官方安装器".into());}
    }else if item["legacy_export"]["manifest_sha256"]!=digest{return Err("旧 DSH 目录与兼容清单不匹配".into());}
    if full{crate::workspace::verify_tree(source,"manifest.json")?;}else{for row in load(&source.join("manifest.json"))?["files"].as_array().ok_or("DSH 清单无效")?{
        if safe_path(source,row["path"].as_str().ok_or("DSH 文件缺少路径")?)?.metadata().ok().map(|v|v.len())!=row["size"].as_u64(){return Err("DSH 运行文件不完整".into());}}}
    Ok(())
}
fn bind_dsh(context:&LaunchContext)->Result<(),String>{
    let root=context.install.join("runtimes/dsh");let metadata=load(&root.join("runtime.json"))?;
    if metadata["source_kind"]=="official_desktop"{atomic_json(&context.data.join("state/native-dsh-binding.json"),&json!({"version":metadata["version"],"source_sha512":metadata["source_sha512"],"manifest_sha256":file_hash(&root.join("manifest.json"))?,"fields":{"version":"固定 DSH 版本","source_sha512":"已核验官方安装器摘要","manifest_sha256":"程序签发的提取目录清单摘要"}}))?;}
    bind_component(context,"dsh","runtimes/dsh",&root)
}
pub fn bind_component(context:&LaunchContext,kind:&str,relative:&str,root:&Path)->Result<(),String>{
    let path=context.program.join("release.json");if !path.is_file(){return Ok(());}
    let mut release=load(&path)?;let manifest=load(&root.join("manifest.json"))?;
    if !release["components"].is_array(){release["components"]=json!([]);}
    let entries=release["components"].as_array_mut().unwrap();entries.retain(|v|v["kind"]!=kind);
    entries.push(json!({"kind":kind,"path":relative,"build":manifest["build"],"manifest_sha256":file_hash(&root.join("manifest.json"))?,"optional":true}));
    atomic_json(&path,&release)
}
