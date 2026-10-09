//! Native resources（原生资源）：模型文件与 DSH 安装不需要 Python（后端解释器）。
use std::{fs, io::{Read, Write}, path::{Path, PathBuf}, sync::{Arc, atomic::{AtomicBool, Ordering}}, process::{Command, Stdio}};
use serde_json::{Value, json};
use sha2::{Digest, Sha512};
use base64::Engine;
use crate::{LaunchContext, download::{DownloadFile, download}, workspace::{safe_path, file_hash, atomic_json, remove_owned}};
pub type Progress<'a> = dyn FnMut(&str,u64,u64) + 'a; // Progress（阶段与字节进度回调）。
pub type Activation<'a> = dyn FnMut(&Path,&str)->Result<(),String> + 'a; // Activation（启用回调）：候选目录与固定目标，宿主在此短暂停机并串行替换。

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
    let ready=runtime["full_ready"]==true;
    let mut rows=vec![json!({"resource_id":"voice-server","label":"Voice Server（语音服务）","installation_state":if ready{"installed"}else if runtime["interpreter_ready"]==true{"incomplete"}else{"missing"},
        "files_installed":runtime["interpreter_ready"],"runtime_ready":ready,"can_start":ready,"runtime_group":"all","python":runtime["python"],"version":runtime["version"],
        "source_url":"https://www.python.org/downloads/release/python-31210/","error":if ready{Value::Null}else{json!("请下载或导入完整 Python（后端运行环境），程序将一次检测并补齐全部语音依赖。")},
        "fields":{"files_installed":"私有解释器已安装","runtime_ready":"全部语音依赖已就绪","can_start":"具备启动条件","runtime_group":"完整语音依赖","python":"私有解释器路径","version":"解释器版本"}})];
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
/// startup_readiness（启动组件检查）：只读取本地清单与文件，不启动解释器、服务或下载。
pub fn startup_readiness(context:&LaunchContext)->Result<Value,String>{
    Ok(readiness_from_inventory(&inventory(context)?))
}
fn readiness_from_inventory(rows:&[Value])->Value{
    let missing=rows.iter().filter(|row|row["can_start"]!=true).map(|row|json!({
        "resource_id":row["resource_id"],"label":row["label"],
        "reason":row["error"].as_str().unwrap_or("组件文件或共享运行依赖尚未完备")
    })).collect::<Vec<_>>();
    json!({"ready":!rows.is_empty()&&missing.is_empty(),"missing":missing,"checked_components":rows.len(),
        "fields":{"ready":"全部组件具备启动条件","missing":"待用户手动补齐的组件","checked_components":"已检查组件数量","resource_id":"组件编号","label":"组件名称","reason":"缺失原因"}})
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
    import_review_with_activation(context,source,id,cancel,progress,review,&mut|stage,relative|activate(context,stage,relative))
}
pub fn import_review_with_activation(context:&LaunchContext,source:&Path,id:Option<&str>,cancel:Arc<AtomicBool>,progress:&mut Progress,review:&mut dyn FnMut(&[Value])->Result<(),String>,enable:&mut Activation)->Result<Vec<String>,String>{
    // 每个下载或导入任务独立暂存，网络传输、解包和校验可以并行。
    let task=safe_path(&context.install,&format!("tmp/native-resources-{}",crate::workspace::new_id()))?;fs::create_dir_all(&task).map_err(|e|e.to_string())?;
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
        let relative=destination(context,&resource)?.strip_prefix(&context.install).unwrap().to_string_lossy().replace('\\',"/");enable(&candidate,&relative)?;
        installed.push(key);
    }
    remove_owned(&task,&context.install)?;Ok(installed)
}
fn refresh_models(context:&LaunchContext)->Result<(),String>{
    let root=context.install.join("models");let record=manifest(&root,"models",&crate::workspace::new_id())?;atomic_json(&root.join("manifest.json"),&record)?;bind_component(context,"models","models",&root)
}
pub fn download_resource(context:&LaunchContext,id:&str,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    download_resource_with_activation(context,id,cancel,progress,&mut|stage,relative|activate(context,stage,relative))
}
pub fn download_resource_with_activation(context:&LaunchContext,id:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut Activation)->Result<Vec<String>,String>{
    let item=spec(context,id)?;
    let installed=destination(context,&item)?;
    if (id=="dsh"&&dsh_validate(context,&installed,false).is_ok())||(id!="dsh"&&model_validate(context,&item,&installed).is_ok()) {progress("复用已校验资源",1,1);return Ok(vec![id.into()]);}
    let root=safe_path(&context.install,&format!("tmp/resource-downloads/{id}"))?;fs::create_dir_all(&root).map_err(|e|e.to_string())?;
    if let Some(artifact)=item["source"]["transfer"]["artifact"].as_object(){
        let value=Value::Object(artifact.clone());let spec=file_spec(&value,value["url"].as_str().ok_or("缺少下载来源")?.into())?;let path=safe_path(&root,&spec.path)?;
        download(&spec,&path,cancel.clone(),&mut|done,total|progress("正在下载固定附件",done,total),0)?;
        if id=="dsh"{return import_review_with_activation(context,&path,Some(id),cancel,progress,&mut|_|Ok(()),enable);}
        let seven=context.program.join("tools/7zip/7z.exe");let out=root.join("extracted");run_seven(&seven,&path,&out,&cancel)?;
        let tar=walk(&out)?.into_iter().find(|p|p.extension().is_some_and(|v|v=="tar")).ok_or("模型归档缺少 tar（内层归档）")?;
        let model=root.join("model");run_seven(&seven,&tar,&model,&cancel)?;
        let folder=walk(&model)?.into_iter().find(|p|p.file_name().is_some_and(|v|v=="tokens.txt")).ok_or("唤醒词归档不完整")?.parent().unwrap().to_path_buf();
        return import_review_with_activation(context,&folder,Some(id),cancel,progress,&mut|_|Ok(()),enable);
    }
    let files=item["files"].as_array().ok_or("下载文件清单缺失")?;
    let total=files.iter().filter(|row|row["optional"]!=true&&!row["asset"].is_string()).map(|row|row["size_bytes"].as_u64().unwrap_or(0)).sum::<u64>();
    let mut completed=0u64;
    for row in files {
        if row["optional"]==true||row["asset"].is_string(){continue;}
        let url=model_file_url(&item,row)?;
        let file=file_spec(row,url)?;let path=safe_path(&root,&file.path)?;fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;
        let mut current=0;
        download(&file,&path,cancel.clone(),&mut|done,_|{current=current.max(done.min(file.size));progress("正在下载模型文件",completed.saturating_add(current),total);},0)?;
        completed+=file.size;
    }
    import_review_with_activation(context,&root,Some(id),cancel,progress,&mut|_|Ok(()),enable)
}
/// model_file_url（模型文件来源）：直接访问固定版本，下载过程不调用 Python（运行环境）。
fn model_file_url(item:&Value,row:&Value)->Result<String,String>{
    let repo=item["source"]["repo_id"].as_str().ok_or("仓库缺失")?;
    let revision=item["source"]["revision"].as_str().ok_or("版本缺失")?;
    let path=row["path"].as_str().ok_or("文件名缺失")?;
    let encode=|text:&str,slash:bool|text.bytes().map(|byte|{
        if byte.is_ascii_alphanumeric()||b"-._~".contains(&byte)||(slash&&byte==b'/'){(byte as char).to_string()}else{format!("%{byte:02X}")}
    }).collect::<String>();
    match item["source"]["platform"].as_str(){
        Some("modelscope")=>Ok(format!("https://modelscope.cn/api/v1/models/{}/repo?Revision={}&FilePath={}",encode(repo,true),encode(revision,false),encode(path,false))),
        Some("huggingface")=>Ok(format!("https://huggingface.co/{}/resolve/{}/{}",encode(repo,true),encode(revision,false),encode(path,true))),
        _=>Err("该模型缺少受支持的直接文件来源".into()),
    }
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

#[cfg(test)]
mod download_tests {
    use super::*;

    #[test]
    fn startup_requires_complete_files_and_shared_dependencies() {
        let complete=json!({"resource_id":"voice-server","label":"语音服务","can_start":true});
        let missing=json!({"resource_id":"paraformer-streaming","label":"流式识别模型","files_installed":false,"can_start":false,"error":"模型文件不完整"});
        let missing_runtime=json!({"resource_id":"sensevoice-small","label":"精修模型","files_installed":true,"runtime_ready":false,"can_start":false});
        let readiness=readiness_from_inventory(&[complete.clone(),missing,missing_runtime]);
        assert_eq!(readiness["ready"],false);
        assert_eq!(readiness["checked_components"],3);
        assert_eq!(readiness["missing"][0]["resource_id"],"paraformer-streaming");
        assert_eq!(readiness["missing"][0]["reason"],"模型文件不完整");
        assert_eq!(readiness["missing"][1]["reason"],"组件文件或共享运行依赖尚未完备");
        assert_eq!(readiness_from_inventory(&[complete])["ready"],true);
        assert_eq!(readiness_from_inventory(&[])["ready"],false);
    }

    #[test]
    fn parallel_imports_keep_independent_stages_and_both_installed_models() {
        use std::{sync::{Mutex,Condvar},time::Duration};
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-parallel-resources-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 测试启动上下文，不运行此脚本").unwrap();
        let mut entries=vec![];
        for (id,marker) in [("first-model",1u8),("second-model",2u8)] {
            let source=root.join(format!("tmp/source-{id}"));fs::create_dir_all(&source).unwrap();
            let file=source.join("model.bin");fs::write(&file,vec![marker;256*1024]).unwrap();
            entries.push(json!({"resource_id":id,"title":"并行验收模型","kind":"model","version":"1","destination":id,
                "files":[{"path":"model.bin","size_bytes":256*1024,"sha256":file_hash(&file).unwrap()}]}));
        }
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":entries})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        let copying=(Mutex::new(0usize),Condvar::new());
        let activation=Mutex::new(());let stages=Mutex::new(vec![]);
        std::thread::scope(|scope| {
            let context=&context;let root=&root;let copying=&copying;let activation=&activation;let stages=&stages;
            let threads=["first-model","second-model"].map(|id|scope.spawn(move|| {
                let mut reached=false;
                let installed=import_review_with_activation(&context,&root.join(format!("tmp/source-{id}")),Some(id),Arc::new(AtomicBool::new(false)),
                    &mut|phase,_,_|{
                        if !reached&&phase=="正在复制并校验" {
                            reached=true;let mut count=copying.0.lock().unwrap();*count+=1;copying.1.notify_all();
                            let (count,timeout)=copying.1.wait_timeout_while(count,Duration::from_secs(5),|count|*count<2).unwrap();
                            assert!(!timeout.timed_out()&&*count==2,"两个任务必须同时进入复制阶段");
                        }
                    },&mut|_|Ok(()),&mut|stage,relative|{
                        let _guard=activation.lock().unwrap();stages.lock().unwrap().push(stage.parent().unwrap().to_path_buf());
                        activate(&context,stage,relative)
                    }).unwrap();
                assert_eq!(installed,vec![id]);
            }));
            for thread in threads{thread.join().unwrap();}
        });
        let stages=stages.into_inner().unwrap();assert_eq!(stages.len(),2);assert_ne!(stages[0],stages[1]);
        for id in ["first-model","second-model"] {model_validate(&context,&spec(&context,id).unwrap(),&root.join(format!("models/{id}"))).unwrap();}
        assert_eq!(load(&root.join("models/manifest.json")).unwrap()["files"].as_array().unwrap().len(),2);
        assert!(!root.join("tmp/native-install.json").exists());
        // 清理仅限本测试创建的独立子目录。
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    #[cfg(windows)]
    #[ignore = "手动验收：在指定工作区同时下载流式识别和精修模型，保留原有数据与下载缓存"]
    fn download_streaming_and_refinement_in_parallel_without_python() {
        use std::{sync::{Mutex,Condvar},time::Duration};
        let root=PathBuf::from(std::env::var_os("VOICE_RUNTIME_TEST_ROOT").expect("请提供 VOICE_RUNTIME_TEST_ROOT（测试工作区）"));
        let mut context=LaunchContext::portable(&root).unwrap();context.python=root.join("不存在的解释器/python.exe");
        let before=crate::runtime::fingerprint(&root.join("runtimes")).unwrap();
        let transfers=(Mutex::new(0usize),Condvar::new());let activation=Mutex::new(());let barrier_failed=AtomicBool::new(false);
        std::thread::scope(|scope| {
            let context=&context;let transfers=&transfers;let activation=&activation;let barrier_failed=&barrier_failed;
            let threads=["paraformer-streaming","sensevoice-small"].map(|id|scope.spawn(move|| {
                let mut entered=false;let mut previous=String::new();let mut milestone=0;
                let installed=download_resource_with_activation(&context,id,Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
                    if phase=="正在下载模型文件"&&!entered {
                        entered=true;let mut count=transfers.0.lock().unwrap();*count+=1;transfers.1.notify_all();
                        let (count,timeout)=transfers.1.wait_timeout_while(count,Duration::from_secs(30),|count|*count<2).unwrap();
                        if timeout.timed_out()||*count!=2{barrier_failed.store(true,Ordering::Release);}
                    }
                    if previous!=phase||done/(50*1024*1024)>milestone {
                        println!("{id}（模型编号）：{phase}，{done} / {total}（字节）");previous=phase.into();milestone=done/(50*1024*1024);
                    }
                },&mut|stage,relative|{let _guard=activation.lock().unwrap();activate(&context,stage,relative)}).unwrap();
                assert_eq!(installed,vec![id]);
                model_validate(&context,&spec(&context,id).unwrap(),&destination(&context,&spec(&context,id).unwrap()).unwrap()).unwrap();
            }));
            for thread in threads{thread.join().unwrap();}
        });
        assert_eq!(*transfers.0.lock().unwrap(),2);
        assert!(!barrier_failed.load(Ordering::Acquire),"两项模型下载必须同时进入传输阶段");
        assert_eq!(before,crate::runtime::fingerprint(&root.join("runtimes")).unwrap());
        assert!(!context.python.exists());println!("两项模型并行下载、独立校验和串行启用通过，Python（运行环境）未被调用或修改");
    }

    #[test]
    fn modelscope_uses_pinned_direct_file_url_with_encoded_query() {
        let model=json!({"source":{"platform":"modelscope","repo_id":"iic/example","revision":"v2.0.4"}});
        let row=json!({"path":"目录/config & data.json"});
        assert_eq!(model_file_url(&model,&row).unwrap(),"https://modelscope.cn/api/v1/models/iic/example/repo?Revision=v2.0.4&FilePath=%E7%9B%AE%E5%BD%95%2Fconfig%20%26%20data.json");
    }

    #[test]
    fn all_pinned_model_files_have_native_download_sources() {
        let catalog:Value=serde_json::from_str(include_str!("../../../../config/resources/catalog.json")).unwrap();
        for model in catalog["resources"].as_array().unwrap().iter().filter(|item|item["kind"]=="model"&&!item["source"]["transfer"]["artifact"].is_object()) {
            for row in model["files"].as_array().unwrap().iter().filter(|row|row["optional"]!=true&&!row["asset"].is_string()) {
                let file=file_spec(row,model_file_url(model,row).unwrap()).unwrap();
                assert!(file.size>0);assert_eq!(file.sha256.len(),64);assert!(file.url.starts_with("https://"));
            }
        }
    }

    #[test]
    #[cfg(windows)]
    #[ignore = "手动验收：只在指定测试工作区下载固定 FSMN-VAD（语音活动检测）模型文件"]
    fn download_model_without_python() {
        let root=PathBuf::from(std::env::var_os("VOICE_RUNTIME_TEST_ROOT").expect("请提供 VOICE_RUNTIME_TEST_ROOT（测试工作区）"));
        let mut context=LaunchContext::portable(&root).unwrap();context.python=root.join("不存在的解释器/python.exe");
        let runtime_before=crate::runtime::fingerprint(&root.join("runtimes")).unwrap();let mut previous=String::new();
        let installed=download_resource(&context,"fsmn-vad",Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
            if previous!=phase{println!("{phase}：{done} / {total}（字节）");previous=phase.into();}
        }).unwrap();
        assert_eq!(installed,vec!["fsmn-vad"]);
        model_validate(&context,&spec(&context,"fsmn-vad").unwrap(),&destination(&context,&spec(&context,"fsmn-vad").unwrap()).unwrap()).unwrap();
        assert_eq!(runtime_before,crate::runtime::fingerprint(&root.join("runtimes")).unwrap());
        assert!(!context.python.exists());println!("模型下载与校验通过，未调用或修改 Python（运行环境）");
    }
}
