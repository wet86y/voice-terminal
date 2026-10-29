//! Incremental update（依赖增量更新）：仅下载并解包变化的包，卸载也走同一文件事务。
use super::*;

pub(super) fn configure(context:&LaunchContext,existing:&Value,gpu:bool,compatible_gpu:bool,repair:bool,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut resources::Activation)->Result<Vec<String>,String>{
    let root=context.install.join("runtimes/python");let effective=effective_lock(context,gpu)?;let names=requested(&effective,"all")?;
    let identity=worker(context,&context.python,"interpreter",&[],&cancel)?;
    if identity["version"]!=effective["python"]["version"]{return Err("解释器版本发生变化，需要显式升级解释器；依赖增量更新保留原环境，不自动重建。".into());}
    let mut args=vec!["--target".into(),root.to_string_lossy().into_owned(),"--names".into(),serde_json::to_string(&names).unwrap(),"--lock".into(),context.installed_program().join("config/runtime/voice-runtime.lock.json").to_string_lossy().into_owned()];
    if gpu{args.extend(["--gpu-lock".into(),gpu_lock_path(context).to_string_lossy().into_owned()]);}
    if compatible_gpu{args.push("--compatible-gpu".into());}
    if repair{args.push("--repair-local".into());}
    progress("计算依赖增量与旧包卸载计划",0,0);
    let plan=worker(context,&context.python,"delta-plan",&args,&cancel).map_err(|error|if repair{format!("局部修复规划失败：{error}")}else{error})?;
    if compatible_gpu&&!gpu{ensure_gpu_core_reuse(&json!({"reused":plan["main"]["reused"]}))?;}
    let stage=safe_path(&context.install,"tmp/voice-environment/stage")?;remove_owned(&stage,&context.install)?;fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
    let base=lock(context)?;let cache=safe_path(&context.install,"cache/dependencies/wheels")?;
    fs::create_dir_all(&cache).map_err(|e|e.to_string())?;
    let mut installs=vec![];let mut transfers=vec![];let mut total=0u64;let mut changed_size=0u64;
    for (scope,catalog,relative) in [("main",&effective,"Lib/site-packages"),("overlay",&base,"Lib/cpu-packages")]{
        let mut paths=vec![];
        for name in plan[scope]["install"].as_array().into_iter().flatten(){
            let row=catalog["wheels"].as_array().ok_or("包集合缺失")?.iter().find(|row|row["name"]==*name).ok_or("增量计划引用未知包")?;
            changed_size=changed_size.saturating_add(row["size"].as_u64().ok_or("包大小缺失")?);
            let target=safe_path(&cache,row["file"].as_str().ok_or("包文件名缺失")?)?;
            if !valid_cached_with_progress(&target,row,&cancel,progress)?{
                if let Some(bundled)=row["bundled"].as_str(){resources::copy_file(&safe_path(&context.installed_program(),bundled)?,&target,&cancel,progress)?;
                    if !valid_cached_with_progress(&target,row,&cancel,progress)?{return Err("随包依赖摘要错误".into());}}
                else{let file=spec(row)?;total=total.saturating_add(file.size);transfers.push((file,target.clone()));}
            }
            paths.push(target.to_string_lossy().into_owned());
        }
        installs.push((relative,paths));
    }
    if crate::workspace::available_space(&context.install)?<changed_size.saturating_mul(4).saturating_add(32*1024*1024){return Err("增量依赖暂存空间不足".into());}
    progress("本次总下载量已确认",0,total);
    crate::download::download_batch(&transfers,cancel.clone(),progress)?;
    for (relative,paths) in installs{if !paths.is_empty(){
        resources::check_cancel(&cancel)?;progress("仅组装新增或变更的依赖",0,0);
        worker(context,&context.python,"install",&["--target".into(),stage.join(relative).to_string_lossy().into_owned(),"--wheels".into(),serde_json::to_string(&paths).unwrap()],&cancel)?;
    }}
    let mut deletes=vec![];
    for scope in ["main","overlay"]{deletes.extend(plan[scope]["delete"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned));}
    // 启动路径和 CRT（C++运行库）也只提交变化的文件。
    prepare_search_paths(&stage)?;
    let crt=context.installed_program().join("tools/python-crt");
    if crt.is_dir(){for path in resources::walk(&crt)?{
        let name=path.file_name().ok_or("运行库文件名无效")?;let old=root.join(name);
        if !old.is_file()||file_hash(&old)?!=file_hash(&path)?{resources::copy_file(&path,&stage.join(name),&cancel,progress)?;}
    }}
    let mut record=existing.clone();record["schema"]=json!(2);record["python"]=effective["python"]["version"].clone();
    record["profile"]=json!(if gpu{"gpu-private"}else if compatible_gpu{"compatible-private"}else{"cpu-private"});
    record["compatible_gpu"]=json!(compatible_gpu);record["groups"]=json!(ENVIRONMENT_GROUPS);record["packages"]=json!(names);
    record["lock_sha256"]=json!(file_hash(&context.installed_program().join("config/runtime/voice-runtime.lock.json"))?);
    record["gpu_profile"]=if gpu{json!("cu128")}else{Value::Null};record["gpu_lock_sha256"]=if gpu{json!(file_hash(&gpu_lock_path(context))?)}else{Value::Null};
    record["package_receipts"]=plan["main"]["receipts"].clone();record["cpu_overlay_receipts"]=plan["overlay"]["receipts"].clone();
    // 旧字段继续保存整锁摘要，供旧版程序读取；新字段独立判断处理器包内容。
    record["cpu_overlay_lock_sha256"]=if plan["overlay"].is_object(){record["lock_sha256"].clone()}else{Value::Null};
    record["cpu_overlay_content_sha256"]=if plan["overlay"].is_object(){json!(cpu_overlay_digest(context,&root)?)}else{Value::Null};
    if !record["fields"].is_object(){record["fields"]=json!({});}
    record["fields"]["package_receipts"]=json!("主依赖逐包锁定内容身份，兼容旧记录升级");
    record["fields"]["cpu_overlay_receipts"]=json!("独立处理器依赖逐包内容身份");
    record["fields"]["cpu_overlay_lock_sha256"]=json!("兼容旧版读取的处理器隔离目录整锁摘要");
    record["fields"]["cpu_overlay_content_sha256"]=json!("仅独立处理器依赖的内容摘要，不受其它基础包变更影响");
    record["package_receipt_fields"]=json!({"version":"锁定版本","file":"锁定预编译包文件名","size":"锁定包字节数","sha256":"锁定包内容摘要","contract":"历史兼容包所满足的依赖契约，不代表其原始包身份","installed_version":"实际安装构建版本","installed_files_sha256":"已完整校验的实际安装文件集合摘要","provenance":"原始固定包或已验证历史安装文件的身份来源"});
    record["fields"]["package_receipt_fields"]=json!("逐包身份回执的字段说明");
    atomic_json(&stage.join("runtime.json"),&record)?;
    if repair{crate::runtime_delta::prepare_repair(context,&stage,&deletes,&cancel,progress)?;}else{crate::runtime_delta::prepare(context,&stage,&deletes,&cancel,progress)?;}
    let mut summary=json!({"state":"prepared","main":plan["main"],"overlay":plan["overlay"],"download_bytes":total,
        "fields":{"state":"计划已准备、失败或完成","main":"主依赖增量","overlay":"独立处理器依赖增量","install":"新增或升级包","reused":"原位保留包","removed":"卸载包","delete":"旧包独占文件","receipts":"逐包内容身份","download_bytes":"本次实际计划网络下载字节","error":"更新或回滚失败原因"}});
    atomic_json(&context.install.join("cache/reports/runtime-update-latest.json"),&summary)?;
    resources::check_cancel(&cancel)?;progress("启用依赖增量并验证实际加载",0,0);
    resources::check_cancel(&cancel)?;
    if let Err(error)=enable(&stage,"runtimes/python"){
        summary["state"]=json!("failed");summary["error"]=json!(&error);let _=atomic_json(&context.install.join("cache/reports/runtime-update-latest.json"),&summary);return Err(error);
    }
    summary["state"]=json!("complete");
    if let Err(error)=atomic_json(&context.install.join("cache/reports/runtime-update-latest.json"),&summary){eprintln!("依赖更新已完成，但诊断记录写入失败：{error}");}
    // 这里只回收本任务的稀疏暂存，现用依赖已由事务单独提交。
    remove_owned(&stage,&context.install)?;Ok(installed_resources(gpu))
}

#[cfg(test)]
mod tests{
    use super::*;
    use std::io::Write;
    fn wheel(context:&LaunchContext,name:&str,version:&str,obsolete:bool)->Value{
        let path=context.install.join(format!("app/tools/wheels/{name}-{version}.whl"));fs::create_dir_all(path.parent().unwrap()).unwrap();
        let mut zip=zip::ZipWriter::new(fs::File::create(&path).unwrap());let options=zip::write::SimpleFileOptions::default();
        let module=name.replace('-',"_");let info=format!("{module}-{version}.dist-info");
        let mut files=vec![(format!("{module}/__init__.py"),format!("VERSION={version:?}\n")),(format!("{info}/METADATA"),format!("Name: {name}\nVersion: {version}\n"))];
        if obsolete{files.push((format!("{module}/obsolete.py"),"old\n".into()));}
        let mut record=files.iter().map(|(name,_)|format!("{name},,\n")).collect::<String>();record+=&format!("{info}/RECORD,,\n");files.push((format!("{info}/RECORD"),record));
        for (name,body) in files{zip.start_file(name,options).unwrap();zip.write_all(body.as_bytes()).unwrap();}zip.finish().unwrap();
        json!({"name":name,"version":version,"file":path.file_name().unwrap().to_str().unwrap(),"size":path.metadata().unwrap().len(),"sha256":file_hash(&path).unwrap(),"bundled":format!("tools/wheels/{}",path.file_name().unwrap().to_str().unwrap()),"url":""})
    }
    fn write_lock(context:&LaunchContext,wheels:Vec<Value>)->Value{
        let names=wheels.iter().map(|row|row["name"].clone()).collect::<Vec<_>>();let groups=ENVIRONMENT_GROUPS.iter().map(|group|(group.to_string(),json!(names))).collect::<serde_json::Map<_,_>>();
        let value=json!({"schema":1,"python":{"version":"3.12.10"},"groups":groups,"wheels":wheels});atomic_json(&context.program.join("config/runtime/voice-runtime.lock.json"),&value).unwrap();value
    }
    #[test]
    #[cfg(windows)]
    #[ignore="显式提供私有解释器后，在微型依赖夹具中验证真实增删改及加载失败回滚，不更改现用环境"]
    fn real_incremental_add_upgrade_uninstall_and_failed_load_rollback(){
        let source=PathBuf::from(std::env::var_os("VOICE_DELTA_TEST_PYTHON").expect("需要私有解释器路径"));
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("voice-delta-integration-{}",crate::workspace::new_id()));crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/runtime")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# fixture").unwrap();
        fs::write(root.join("app/scripts/voice-environment-worker.py"),include_bytes!("../../../../scripts/voice-environment-worker.py")).unwrap();
        atomic_json(&root.join("app/release.json"),&json!({"schema":1,"kind":"app","components":[]})).unwrap();
        let runtime=root.join("runtimes/python");fs::create_dir_all(&runtime).unwrap();
        // 只准备解释器及标准库，不复制第三方依赖或模型。
        for entry in fs::read_dir(source.parent().unwrap()).unwrap(){let path=entry.unwrap().path();if path.is_file()&&path.file_name().unwrap()!="manifest.json"&&path.file_name().unwrap()!="runtime.json"{fs::copy(&path,runtime.join(path.file_name().unwrap())).unwrap();}}
        fn standard_library(input:&Path,output:&Path){
            if !input.is_dir(){return;}fs::create_dir_all(output).unwrap();
            for entry in fs::read_dir(input).unwrap(){let path=entry.unwrap().path();let name=path.file_name().unwrap();
                if ["site-packages","cpu-packages","__pycache__"].iter().any(|skip|name==*skip){continue;}
                if path.is_dir(){standard_library(&path,&output.join(name));}else{fs::copy(&path,output.join(name)).unwrap();}
            }
        }
        for name in ["Lib","DLLs"]{standard_library(&source.parent().unwrap().join(name),&runtime.join(name));}
        prepare_search_paths(&runtime).unwrap();let context=LaunchContext::portable(&root).unwrap();
        let keep=wheel(&context,"delta-keep","1",false);let old=wheel(&context,"delta-old","1",true);let change=wheel(&context,"delta-change","1",true);
        let previous=write_lock(&context,vec![keep.clone(),old.clone(),change]);let old_hash=file_hash(&context.program.join("config/runtime/voice-runtime.lock.json")).unwrap();
        let paths=previous["wheels"].as_array().unwrap().iter().map(|row|context.program.join(row["bundled"].as_str().unwrap()).to_string_lossy().into_owned()).collect::<Vec<_>>();
        worker(&context,&context.python,"install",&["--target".into(),runtime.join("Lib/site-packages").to_string_lossy().into_owned(),"--wheels".into(),serde_json::to_string(&paths).unwrap()],&AtomicBool::new(false)).unwrap();
        atomic_json(&runtime.join("runtime.json"),&json!({"schema":1,"python":"3.12.10","profile":"cpu-private","packages":["delta-keep","delta-old","delta-change"],"groups":ENVIRONMENT_GROUPS,"lock_sha256":old_hash})).unwrap();
        atomic_json(&runtime.join("manifest.json"),&resources::manifest(&runtime,"python","old").unwrap()).unwrap();
        let historical=previous["wheels"].as_array().unwrap().iter().map(|row|(row["name"].as_str().unwrap().to_string(),json!({"version":row["version"],"file":row["file"],"size":row["size"],"sha256":row["sha256"]}))).collect::<serde_json::Map<_,_>>();
        atomic_json(&context.program.join("config/runtime/voice-runtime-history.json"),&json!({"schema":1,"locks":{old_hash:historical}})).unwrap();
        let keep_path=runtime.join("Lib/site-packages/delta_keep/__init__.py");let before=keep_path.metadata().unwrap().modified().unwrap();
        let change=wheel(&context,"delta-change","2",false);let added=wheel(&context,"delta-added","1",false);write_lock(&context,vec![keep.clone(),change.clone(),added.clone()]);
        let original_manifest=file_hash(&runtime.join("manifest.json")).unwrap();let cancelled=Arc::new(AtomicBool::new(false));
        assert!(super::super::configure(&context,"all",cancelled.clone(),&mut|phase,_,_|{if phase=="启用依赖增量并验证实际加载"{cancelled.store(true,std::sync::atomic::Ordering::Release);}}).unwrap_err().contains("取消"));
        assert_eq!(file_hash(&runtime.join("manifest.json")).unwrap(),original_manifest);assert!(!context.install.join("tmp/native-install.json").exists());
        super::super::configure(&context,"all",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).unwrap();
        assert_eq!(keep_path.metadata().unwrap().modified().unwrap(),before);assert!(!runtime.join("Lib/site-packages/delta_old/obsolete.py").exists());assert!(!runtime.join("Lib/site-packages/delta_change/obsolete.py").exists());
        assert!(runtime.join("Lib/site-packages/delta_added/__init__.py").exists());assert_eq!(status(&context).unwrap()["full_ready"],true);
        crate::workspace::verify_tree_with_progress(&runtime,"manifest.json",&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
        let damaged=runtime.join("Lib/site-packages/delta_added/__init__.py");let good=fs::read(&damaged).unwrap();fs::write(&damaged,b"damaged").unwrap();
        assert!(super::super::configure(&context,"all",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).is_err());
        super::super::repair_with_activation(&context,"all",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},&mut|stage,relative|resources::activate(&context,stage,relative)).unwrap();
        assert_eq!(fs::read(damaged).unwrap(),good);assert_eq!(keep_path.metadata().unwrap().modified().unwrap(),before);
        let baseline=file_hash(&runtime.join("manifest.json")).unwrap();let mut broken=wheel(&context,"delta-broken","1",false);broken["version"]=json!("2");write_lock(&context,vec![keep,change,added,broken]);
        assert!(super::super::configure(&context,"all",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).unwrap_err().contains("实际加载失败"));
        assert_eq!(file_hash(&runtime.join("manifest.json")).unwrap(),baseline);assert!(!runtime.join("Lib/site-packages/delta_broken/__init__.py").exists());assert_eq!(keep_path.metadata().unwrap().modified().unwrap(),before);
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
}
