//! 安装器兼容检查：保留本体及配置维护，组件问题交给正式程序的组件列表。
use super::*;
use std::sync::Arc;
use crate::{runtime, installation::{self, WorkspaceGuard}};

/// MaintenanceOutcome（维护结束结果）：正常取消与实际维护失败分别返回。
#[derive(Debug,PartialEq,Eq)]
pub enum MaintenanceOutcome {
    Complete, // Complete（本次维护完成）。
    Cancelled, // Cancelled（取消已收尾，未把取消当作维护失败）。
}

// remove_invalid（强制校验后的受管故障清除）：路径来自当前组件目录，禁止扩大到用户资料或父目录。
fn remove_invalid(context:&LaunchContext,path:&Path)->Result<(),String>{
    let relative=path.strip_prefix(&context.install).map_err(|_|"故障组件越出工作区")?.to_string_lossy().replace('\\',"/");
    let registered=relative=="runtimes/python"||resources::specs(context)?.iter().any(|item|resources::destination(context,item).is_ok_and(|target|target==path));
    if !registered{return Err("不能清除未登记的受管组件目录。".into());}
    let target=safe_path(&context.install,&relative)?;
    crate::workspace::remove_owned(&target,&context.install)
}

fn checkpoint(context:&LaunchContext,phase:&str,reset_devices:bool,reason:Option<&str>)->Result<(),String>{
    let path=state_path(context)?;
    let mut state=read(&path).unwrap_or(json!({"schema":1}));
    let release=read(&context.installed_program().join("release.json"))?;
    if state["workspace_id"]!=context.workspace_id||state["target_build"]!=release["build"]{
        state=record(&context.workspace_id,&release["build"],&rules(&context.program,false)?,&state,"installer");
    }
    state["enforcement"]=json!({"phase":phase,"reset_devices":reset_devices,"reason":reason,
        "fields":{"phase":"本体兼容维护阶段","reset_devices":"待配置修复消费的历史设备回退要求","reason":"尚未完成的本体或配置维护原因"}});
    state["checked_at_ms"]=json!(now());
    state["revision"]=json!(state["revision"].as_u64().unwrap_or(0).saturating_add(1));
    atomic_json(&path,&state)
}

fn settings(context:&LaunchContext,reset_devices:bool,cancel:&AtomicBool)->Result<(),String>{
    let mut args=vec!["--program".into(),context.program.to_string_lossy().into_owned(),"--data".into(),context.data.to_string_lossy().into_owned()];
    if reset_devices{args.push("--reset-devices".into());}
    let result=runtime::worker(context,&context.python,"compatibility-repair",&args,cancel)?;
    if result["ready"]!=true{return Err("配置修复处理器未确认完成。".into());}
    Ok(())
}

// fallback_tts（未配置在线语音时的既有回退）：保留旧兼容语义，不在分发器下载本地组件。
fn fallback_tts(context:&LaunchContext)->Result<bool,String>{
    if crate::credentials::configured_tts_selection(&context.data)["backend"]=="voxcpm1"
        ||crate::credentials::status(&context.data)?["providers"]["minimax"]["configured"]==true{return Ok(false);}
    let path=safe_path(&context.data,"config/product-settings.json")?;
    let mut value=read(&path)?;
    value["tts"]["backend"]=json!("voxcpm1");value["tts"]["local_model"]=json!("voxcpm2");
    atomic_json(&path,&value)?;Ok(true)
}

fn repair_bootstrap_policy(context:&LaunchContext)->Result<(),String>{
    let path=safe_path(&context.data,"config/runtime-resources.json")?;
    if !path.exists(){return Ok(());}
    let value=read(&path).unwrap_or(Value::Null);
    let usable=value.is_object()
        && value.get("policy_schema").is_none_or(|value|matches!(value.as_u64(),Some(1|2)))
        && value.get("vad_backend").is_none_or(|value|matches!(value.as_str(),Some("auto"|"funasr"|"onnx")))
        && ["shared_audio","disk_history"].iter().all(|name|value.get(*name).is_none_or(Value::is_boolean))
        && value.get("history_cache_bytes").is_none_or(|value|value.as_u64().is_some())
        && value.get("native_threads").is_none_or(|value|value.as_object().is_some_and(|threads|threads.iter().all(|(role,count)|
            matches!(role.as_str(),"bootstrap"|"asr"|"analysis"|"voice"|"speaker")&&count.as_u64().is_some_and(|count|(1..=256).contains(&count)))));
    if !usable{preserve_file(&path,&context.install)?;atomic_json(&path,&json!({"policy_schema":2}))?;}
    Ok(())
}

/// enforce_upgrade（普通升级检查）：记录组件状态，组件安装和更新留在本体。
pub fn enforce_upgrade(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    match maintain_with_settings(root,cancel,progress,false,settings)?{
        MaintenanceOutcome::Complete=>Ok(()),
        MaintenanceOutcome::Cancelled=>Err("本体兼容维护已取消。".into()),
    }
}

/// verify_and_repair（主动完整性维修）：本体已由安装事务修复，组件校验结果交给本体修复。
pub fn verify_and_repair(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<MaintenanceOutcome,String>{
    maintain_with_settings(root,cancel,progress,true,settings)
}

fn maintain_with_settings(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress,full:bool,
    mut settings_repair:impl FnMut(&LaunchContext,bool,&AtomicBool)->Result<(),String>)->Result<MaintenanceOutcome,String>{
    installation::ensure_idle(root)?;
    let _guard=WorkspaceGuard::acquire(root)?;
    let _verification=crate::verification::VerificationSession::begin();
    let context=LaunchContext::portable(root)?;
    let activation_fault=if full{resources::recover_for_integrity(&context)?}else{resources::recover(&context)?;None};
    rules(&context.program,false)?;
    let prior=read(&state_path(&context)?).unwrap_or(Value::Null);
    let mut reset=prior["enforcement"]["reset_devices"]==true;
    let mut changing=false; // changing（已进入持久变更）：只读扫描取消不写维护门禁。
    let finish=AtomicBool::new(false); // finish（设置收尾令牌）：已经开始的配置写入须完成后再响应退出。
    let result:Result<(),String>=(||{
        resources::check_cancel(&cancel)?;
        if full{
            progress("完整性维护：单轮核对现有环境、模型与代理组件",0,0);
            resources::prepare_maintenance_verification(&context,&cancel,progress)?;
        }
        let mut issues=resources::maintenance_issues(&context,full,&cancel,progress)?;
        if let Some(fault)=&activation_fault{if let Some(id)=&fault.resource_id{
            issues.retain(|row|row["resource_id"]!=*id);
            issues.push(json!({"resource_id":id,"reason":format!("组件启用事务一致性无法恢复：{}",fault.reason),"content_failure":true,"consistency_failure":true,"dispatched":false}));
        }}
        resources::check_cancel(&cancel)?;
        changing=true;
        checkpoint(&context,"verify_components",reset,None)?;
        repair_bootstrap_policy(&context)?;
        // 交接先落盘；清除后即使中断，正式程序仍知道哪些组件必须重新下载修复。
        crate::component_maintenance::save(&context,issues.clone(),full)?;
        if let Some(fault)=&activation_fault{
            // 扫描取消不动故障证据；进入维修后先保存交接，再解除旧事务门禁。
            if cancel.load(std::sync::atomic::Ordering::Acquire){return Ok(());}
            resources::archive_failed_activation(&context,fault)?;
        }
        if full{for issue in issues.iter().filter(|row|row["content_failure"]==true){
            if cancel.load(std::sync::atomic::Ordering::Acquire){break;}
            let id=issue["resource_id"].as_str().ok_or("故障组件编号缺失")?;
            let target=if id==runtime::CPU_RESOURCE{context.install.join("runtimes/python")}
                else{resources::destination(&context,&resources::spec(&context,id)?)?};
            if id==runtime::CPU_RESOURCE{
                reset=true;
                checkpoint(&context,"settings_pending",true,Some("故障运行依赖清除后，由系统组件重建并恢复有效计算设备设置。"))?;
            }
            progress(&format!("完整性维护：清除已确认损坏的受管组件 {id}（组件编号）"),0,0);
            // 先关闭界面的取消入口，再核对先前已经接受的取消，避免清除边界竞态。
            if cancel.load(std::sync::atomic::Ordering::Acquire){break;}
            remove_invalid(&context,&target)?;
        }}
        // 缺少或损坏的环境由系统组件处理；分发器不为了配置检查安装依赖。
        let environment_ready=!issues.iter().any(|row|row["resource_id"]==runtime::CPU_RESOURCE);
        if environment_ready{
            progress("兼容处理：归档故障资料并恢复有效设置",0,0);
            settings_repair(&context,reset,&finish)?;
            if fallback_tts(&context)?{settings_repair(&context,reset,&finish)?;}
            checkpoint(&context,"verify",false,None)?;
        }else{
            checkpoint(&context,"settings_pending",reset,Some("请在系统组件配置运行依赖，随后完成本次设置维护。"))?;
        }
        crate::verification::ensure_unchanged()?;
        let rows=complete_maintenance(&context)?;
        let state=&rows.first().ok_or("本体维护没有返回盘点结果。")?["compatibility"];
        if state["stable"]!=true||state["tasks"].as_array().into_iter().flatten().any(|task|task["resource_id"]=="program-body"&&task["status"]!="compatible"){
            return Err("本体维护或组件事务尚未完成。".into());
        }
        if environment_ready&&state["enforcement"]["phase"]!="complete"{return Err("本体设置维护尚未通过最终校验。".into());}
        crate::verification::ensure_unchanged()?;
        progress(if full{"校验完成，组件问题交由正式程序的组件列表修复"}else{"本体更新完成，组件状态交由正式程序显示"},1,1);
        Ok(())
    })();
    match result{
        Err(_) if !changing&&cancel.load(std::sync::atomic::Ordering::Acquire)=>{
            progress("校验已取消，原有组件与维护状态已保留",0,0);
            Ok(MaintenanceOutcome::Cancelled)
        }
        Err(error) if !changing&&matches!(crate::component_recovery::classify(&error),crate::component_recovery::Event::FileBusy|crate::component_recovery::Event::Permission|crate::component_recovery::Event::Space)=>Err(error),
        Err(error)=>{checkpoint(&context,"repair_pending",reset,Some(&error))?;Err(error)}
        Ok(()) if cancel.load(std::sync::atomic::Ordering::Acquire)=>{
            progress("校验已取消，必要收尾已完成",0,0);
            Ok(MaintenanceOutcome::Cancelled)
        }
        Ok(())=>Ok(MaintenanceOutcome::Complete),
    }
}

/// finish_deferred_settings（系统组件操作收尾）：只补做本次维护延期的设置处理，普通启动不调用。
pub fn finish_deferred_settings(context:&LaunchContext,cancel:&AtomicBool)->Result<(),String>{
    let state=read(&state_path(context)?).unwrap_or(Value::Null);
    if state["enforcement"]["phase"]!="settings_pending"{return Ok(());}
    let reset=state["enforcement"]["reset_devices"]==true;
    let result:Result<(),String>=(||{
        settings(context,reset,cancel)?;
        if fallback_tts(context)?{settings(context,reset,cancel)?;}
        checkpoint(context,"verify",reset,None)?;
        let rows=complete_maintenance(context)?;
        if rows.first().is_none_or(|row|row["compatibility"]["enforcement"]["phase"]!="complete"){
            return Err("系统组件已更新，本次设置维护仍需处理。".into());
        }
        checkpoint(context,"complete",false,None)
    })();
    if let Err(error)=&result{checkpoint(context,"settings_pending",reset,Some(error))?;}
    result
}

#[cfg(test)] mod tests{
    use super::*;
    use crate::workspace::file_hash;
    fn runtime(root:&Path){
        let lock=root.join("app/config/runtime/voice-runtime.lock.json");
        atomic_json(&lock,&json!({"schema":1,"python":{"version":"3.12.10"}})).unwrap();
        let runtime=root.join("runtimes/python");fs::create_dir_all(&runtime).unwrap();fs::write(runtime.join("python.exe"),b"fixture").unwrap();
        atomic_json(&runtime.join("runtime.json"),&json!({"schema":2,"python":"3.12.10","profile":"cpu-private","lock_sha256":file_hash(&lock).unwrap(),
            "groups":["voice-core","torch-cpu","onnx-cpu","sherpa-cpu","modelscope-download","onnx-vad"]})).unwrap();
        atomic_json(&runtime.join("manifest.json"),&resources::manifest(&runtime,"python","fixture").unwrap()).unwrap();
    }
    fn validate_settings(context:&LaunchContext,_:bool,_:&AtomicBool)->Result<(),String>{
        let path=context.data.join("config/product-settings.json");
        atomic_json(&path,&json!({"schema_version":rules(&context.program,false)?["settings_schema_max"],"tts":{"backend":"voxcpm1","local_model":"voxcpm2"}}))?;
        atomic_json(&context.data.join("state/settings-validation.json"),&json!({"schema":1,"settings_schema":rules(&context.program,false)?["settings_schema_max"],"sha256":file_hash(&path)?}))
    }
    #[test]fn failed_component_transaction_does_not_prevent_full_integrity_and_handoff(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let catalog=fixture.root.join("app/config/resources/catalog.json");
        let mut value=read(&catalog).unwrap();
        let other=fixture.root.join("models/unrelated");fs::create_dir_all(&other).unwrap();fs::write(other.join("model.bin"),b"bad!").unwrap();
        value["resources"].as_array_mut().unwrap().push(json!({"resource_id":"unrelated-model","kind":"model","version":"1","destination":"unrelated","required":false,"files":[{"path":"model.bin","size_bytes":4,"sha256":"00".repeat(32)}]}));atomic_json(&catalog,&value).unwrap();
        let journal=fixture.root.join("tmp/native-install.json");
        let python=&fixture.context.python;let digest=file_hash(python).unwrap();
        atomic_json(&journal,&json!({"target":crate::runtime_delta::JOURNAL_TARGET,"state":"swapped","original":true,"companions":[],"delta":[{"path":"python.exe","original":true,"put":true,"old_size":7,"old_sha256":digest}]})).unwrap();
        fs::write(python,b"corrupt").unwrap();
        assert!(resources::recover(&fixture.context).is_err());
        let mut scanned=false;
        maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{if phase=="完整性维护：单轮核对现有环境、模型与代理组件"{scanned=true;}},true,|_,_,_|panic!("损坏环境不能运行设置工作进程")).unwrap();
        assert!(scanned);assert!(!journal.exists());assert!(fixture.root.join("tmp/component-recovery").is_dir());
        let handoff=crate::component_maintenance::load(&fixture.context).unwrap().unwrap();assert_eq!(handoff["scan_scope"],"all");
        assert!(handoff["issues"].as_array().unwrap().iter().any(|row|row["resource_id"]==runtime::CPU_RESOURCE&&row["repair_action"]=="rebuild_component"));
        assert!(handoff["issues"].as_array().unwrap().iter().any(|row|row["resource_id"]=="unrelated-model"));
    }
    #[test]fn cancelled_readonly_integrity_preserves_startup_state_and_policy(){
        for trigger in ["完整性维护：单轮核对现有环境、模型与代理组件","批量校验组件文件内容","批量校验完成，复用结果并按需修复"]{
            let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
            maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},false,validate_settings).unwrap();
            let state=state_path(&fixture.context).unwrap();let before=fs::read(&state).unwrap();
            let handoff=fixture.context.data.join("state/component-maintenance.json");let handoff_before=fs::read(&handoff).unwrap();
            let policy=fixture.context.data.join("config/runtime-resources.json");fs::write(&policy,b"invalid-policy").unwrap();
            let cancel=Arc::new(AtomicBool::new(false));let mut reached=false;
            let outcome=maintain_with_settings(&fixture.root,cancel.clone(),&mut|phase,_,_|{
                if phase==trigger{reached=true;assert_eq!(fs::read(&state).unwrap(),before);cancel.store(true,std::sync::atomic::Ordering::Release);}
            },true,|_,_,_|panic!("只读校验取消后不能启动设置修复")).unwrap();
            assert!(reached);assert_eq!(outcome,MaintenanceOutcome::Cancelled);
            assert_eq!(fs::read(&state).unwrap(),before);assert_eq!(fs::read(&handoff).unwrap(),handoff_before);
            assert_eq!(fs::read(&policy).unwrap(),b"invalid-policy");assert_eq!(fs::read(&fixture.context.python).unwrap(),b"fixture");
            assert_eq!(resources::startup_readiness(&fixture.context).unwrap()["ready"],true);
        }
    }
    #[test]
    #[cfg(windows)]
    fn occupied_file_during_full_scan_preserves_previous_handoff_and_components(){
        use std::os::windows::fs::OpenOptionsExt;
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},false,validate_settings).unwrap();
        let handoff=fixture.context.data.join("state/component-maintenance.json");let before=fs::read(&handoff).unwrap();let state=fs::read(state_path(&fixture.context).unwrap()).unwrap();
        let held=fs::OpenOptions::new().read(true).share_mode(0).open(&fixture.context.python).unwrap();
        let error=maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},true,validate_settings).unwrap_err();
        assert!(matches!(crate::component_recovery::classify(&error),crate::component_recovery::Event::FileBusy|crate::component_recovery::Event::Permission));
        assert_eq!(fs::read(&handoff).unwrap(),before);assert_eq!(fs::read(state_path(&fixture.context).unwrap()).unwrap(),state);drop(held);assert!(fixture.context.python.is_file());
    }
    #[test]fn cancelled_scan_preserves_existing_repair_and_does_not_create_new_state(){
        for pending in [false,true]{
            let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
            let state=state_path(&fixture.context).unwrap();
            if pending{
                checkpoint(&fixture.context,"settings_pending",true,Some("原有维修尚未完成")).unwrap();
                crate::component_maintenance::save(&fixture.context,vec![json!({"resource_id":runtime::CPU_RESOURCE,"reason":"原有环境故障","content_failure":true,"dispatched":false})],true).unwrap();
            }
            let before=fs::read(&state).ok();let handoff=crate::component_maintenance::load(&fixture.context).unwrap();
            let cancel=Arc::new(AtomicBool::new(false));
            let outcome=maintain_with_settings(&fixture.root,cancel.clone(),&mut|phase,_,_|{
                if phase=="批量校验组件文件内容"{cancel.store(true,std::sync::atomic::Ordering::Release);}
            },true,|_,_,_|panic!("取消扫描不能执行设置修复")).unwrap();
            assert_eq!(outcome,MaintenanceOutcome::Cancelled);assert_eq!(fs::read(&state).ok(),before);
            assert_eq!(crate::component_maintenance::load(&fixture.context).unwrap(),handoff);
            if pending{assert_eq!(read(&state).unwrap()["enforcement"]["reset_devices"],true);assert!(crate::component_maintenance::pending_repairs(&fixture.context).unwrap());}
        }
    }
    #[test]fn cancellation_during_settings_finishes_validation_before_releasing_startup(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let cancel=Arc::new(AtomicBool::new(false));let mut calls=0;
        let outcome=maintain_with_settings(&fixture.root,cancel.clone(),&mut|_,_,_|{},true,|context,reset,finish|{
            calls+=1;cancel.store(true,std::sync::atomic::Ordering::Release);
            assert!(!finish.load(std::sync::atomic::Ordering::Acquire));
            validate_settings(context,reset,finish)
        }).unwrap();
        assert_eq!(calls,1);assert_eq!(outcome,MaintenanceOutcome::Cancelled);
        assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["phase"],"complete");
        let settings=fixture.context.data.join("config/product-settings.json");
        assert_eq!(read(&fixture.context.data.join("state/settings-validation.json")).unwrap()["sha256"],file_hash(&settings).unwrap());
        assert_eq!(resources::startup_readiness(&fixture.context).unwrap()["ready"],true);
    }
    #[test]fn cancellation_does_not_hide_a_real_settings_failure(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let cancel=Arc::new(AtomicBool::new(false));
        let error=maintain_with_settings(&fixture.root,cancel.clone(),&mut|_,_,_|{},true,|_,_,_|{
            cancel.store(true,std::sync::atomic::Ordering::Release);Err("配置落盘失败".into())
        }).unwrap_err();
        assert_eq!(error,"配置落盘失败");
        assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["phase"],"repair_pending");
        assert_eq!(resources::startup_readiness(&fixture.context).unwrap()["ready"],false);
    }
    #[test]fn cancellation_finishes_tts_fallback_and_revalidates_the_changed_settings(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let cancel=Arc::new(AtomicBool::new(false));let mut calls=0;
        let outcome=maintain_with_settings(&fixture.root,cancel.clone(),&mut|_,_,_|{},true,|context,reset,finish|{
            calls+=1;assert!(!finish.load(std::sync::atomic::Ordering::Acquire));
            validate_settings(context,reset,finish)?;
            if calls==1{
                let path=context.data.join("config/product-settings.json");let mut value=read(&path)?;
                value["tts"]["backend"]=json!("minimax");atomic_json(&path,&value)?;
                cancel.store(true,std::sync::atomic::Ordering::Release);
            }
            Ok(())
        }).unwrap();
        assert_eq!(outcome,MaintenanceOutcome::Cancelled);assert_eq!(calls,2);
        let settings=fixture.context.data.join("config/product-settings.json");
        assert_eq!(read(&settings).unwrap()["tts"]["backend"],"voxcpm1");
        assert_eq!(read(&fixture.context.data.join("state/settings-validation.json")).unwrap()["sha256"],file_hash(&settings).unwrap());
        assert_eq!(resources::startup_readiness(&fixture.context).unwrap()["ready"],true);
    }
    #[test]fn cancellation_at_component_cleanup_keeps_confirmed_damage_and_original_files(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let component=fixture.root.join("models/test-model");fs::create_dir_all(&component).unwrap();fs::write(component.join("model.bin"),b"good").unwrap();
        let item=json!({"resource_id":"test-model","kind":"model","version":"1","required":false,"destination":"test-model",
            "files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&component.join("model.bin")).unwrap()}]});
        resources::write_model_receipt(&component,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
        atomic_json(&fixture.root.join("app/config/resources/catalog.json"),&json!({"resources":[item]})).unwrap();
        fs::write(component.join("model.bin"),b"evil").unwrap();
        let cancel=Arc::new(AtomicBool::new(false));
        let outcome=maintain_with_settings(&fixture.root,cancel.clone(),&mut|phase,_,_|{
            if phase.starts_with("完整性维护：清除"){assert!(crate::component_maintenance::pending_repairs(&fixture.context).unwrap());cancel.store(true,std::sync::atomic::Ordering::Release);}
        },true,validate_settings).unwrap();
        assert_eq!(outcome,MaintenanceOutcome::Cancelled);assert_eq!(fs::read(component.join("model.bin")).unwrap(),b"evil");
        assert_eq!(crate::component_maintenance::load(&fixture.context).unwrap().unwrap()["issues"][0]["content_failure"],true);
        assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["phase"],"complete");
        assert_eq!(resources::inventory(&fixture.context).unwrap().iter().find(|row|row["resource_id"]=="test-model").unwrap()["can_start"],false);
    }
    #[test]fn cancellation_keeps_missing_environment_handoff_and_device_reset(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);fs::write(&fixture.context.python,b"corrupt").unwrap();
        let cancel=Arc::new(AtomicBool::new(false));
        let outcome=maintain_with_settings(&fixture.root,cancel.clone(),&mut|phase,_,_|{
            if phase.starts_with("校验完成"){cancel.store(true,std::sync::atomic::Ordering::Release);}
        },true,|_,_,_|panic!("缺失环境不能启动取消收尾工作进程")).unwrap();
        assert_eq!(outcome,MaintenanceOutcome::Cancelled);assert!(!fixture.context.python.exists());
        let state=read(&state_path(&fixture.context).unwrap()).unwrap();
        assert_eq!(state["enforcement"]["phase"],"settings_pending");assert_eq!(state["enforcement"]["reset_devices"],true);
        assert_eq!(crate::component_maintenance::pending_requests(&fixture.context).unwrap()[0]["resource_id"],runtime::CPU_RESOURCE);
    }
    #[test] fn ordinary_update_preserves_outdated_optional_components(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let component=fixture.root.join("models/test-model");fs::create_dir_all(&component).unwrap();fs::write(component.join("model.bin"),b"old!").unwrap();
        let item=json!({"resource_id":"test-model","kind":"model","version":"2","required":false,"destination":"test-model",
            "files":[{"path":"model.bin","size_bytes":4,"sha256":"a".repeat(64)}]});
        atomic_json(&fixture.root.join("app/config/resources/catalog.json"),&json!({"resources":[item]})).unwrap();
        let mut phases=vec![];
        maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|phases.push(phase.to_owned()),false,validate_settings).unwrap();
        assert_eq!(fs::read(component.join("model.bin")).unwrap(),b"old!");
        assert!(!fixture.root.join("tmp/resource-downloads").exists());
        assert!(!phases.iter().any(|phase|phase.contains("下载")||phase.contains("校验组件文件内容")));
        assert!(!crate::component_maintenance::pending_repairs(&fixture.context).unwrap());
        assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["phase"],"complete");
    }
    #[test]fn existing_unconfigured_online_tts_fallback_revalidates_only_changed_settings(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let mut calls=0;
        maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},false,|context,_,_|{
            calls+=1;
            let path=context.data.join("config/product-settings.json");
            let value=if calls==1{json!({"schema_version":rules(&context.program,false)?["settings_schema_max"],"tts":{"backend":"minimax","local_model":"voxcpm2"}})}else{read(&path)?};
            atomic_json(&path,&value)?;
            atomic_json(&context.data.join("state/settings-validation.json"),&json!({"schema":1,"settings_schema":value["schema_version"],"sha256":file_hash(&path)?}))
        }).unwrap();
        assert_eq!(calls,2);assert_eq!(read(&fixture.context.data.join("config/product-settings.json")).unwrap()["tts"]["backend"],"voxcpm1");
        assert!(!fixture.root.join("tmp/resource-downloads").exists());
        assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["phase"],"complete");
    }
    #[test] fn missing_environment_is_handed_to_system_components_without_running_worker(){
        for valid_receipt in [false,true]{
            let fixture=super::super::tests::Fixture::new();
            if valid_receipt{validate_settings(&fixture.context,false,&AtomicBool::new(false)).unwrap();}
            maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},false,|_,_,_|panic!("缺少环境不能启动配置工作进程")).unwrap();
            assert!(!fixture.context.python.exists());assert!(!fixture.root.join("tmp/resource-downloads").exists());
            assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["phase"],"settings_pending");
            assert!(!crate::component_maintenance::pending_repairs(&fixture.context).unwrap());
        }
    }
    #[test]fn integrity_clears_confirmed_environment_damage_after_persisting_handoff_and_preserves_user_data(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let record=fixture.root.join("runtimes/python/runtime.json");let mut value=read(&record).unwrap();value["compatible_gpu"]=json!(true);atomic_json(&record,&value).unwrap();
        atomic_json(&fixture.root.join("runtimes/python/manifest.json"),&resources::manifest(&fixture.root.join("runtimes/python"),"python","old").unwrap()).unwrap();
        fs::write(&fixture.context.python,b"corrupt").unwrap(); // 与 fixture（原微型解释器）相同长度。
        let user=fixture.context.data.join("speaker/keep.json");fs::create_dir_all(user.parent().unwrap()).unwrap();fs::write(&user,b"keep-user-data").unwrap();
        maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{
            if phase.starts_with("完整性维护：清除"){
                assert!(crate::component_maintenance::pending_repairs(&fixture.context).unwrap());
                assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["reset_devices"],true);
            }
        },true,|_,_,_|panic!("损坏环境不能启动设置工作进程")).unwrap();
        assert!(!fixture.root.join("runtimes/python").exists());assert_eq!(fs::read(user).unwrap(),b"keep-user-data");
        assert_eq!(crate::component_maintenance::pending_requests(&fixture.context).unwrap()[0]["resource_id"],runtime::CPU_RESOURCE);
        assert_eq!(read(&state_path(&fixture.context).unwrap()).unwrap()["enforcement"]["phase"],"settings_pending");
        assert!(remove_invalid(&fixture.context,&fixture.root).is_err());assert!(remove_invalid(&fixture.context,&fixture.context.data).is_err());
    }
    #[test]fn handoff_keeps_unstarted_components_pending_when_only_cpu_task_was_created(){
        let fixture=super::super::tests::Fixture::new();
        let item=json!({"resource_id":"test-model","kind":"model","version":"1","destination":"test-model","files":[]});
        atomic_json(&fixture.root.join("app/config/resources/catalog.json"),&json!({"resources":[item]})).unwrap();
        crate::component_maintenance::save(&fixture.context,vec![json!({"resource_id":runtime::CPU_RESOURCE,"reason":"环境损坏","content_failure":true,"dispatched":false}),
            json!({"resource_id":"test-model","reason":"模型损坏","content_failure":true,"dispatched":false})],true).unwrap();
        assert_eq!(crate::component_maintenance::pending_requests(&fixture.context).unwrap().len(),2);
        // 模拟父任务未准入：读取不写派发状态。
        assert_eq!(crate::component_maintenance::pending_requests(&fixture.context).unwrap().len(),2);
        crate::component_maintenance::dispatched(&fixture.context,runtime::CPU_RESOURCE).unwrap();
        let pending=crate::component_maintenance::pending_requests(&fixture.context).unwrap();assert_eq!(pending.len(),1);assert_eq!(pending[0]["resource_id"],"test-model");
        assert_eq!(crate::component_maintenance::load(&fixture.context).unwrap().unwrap()["issues"].as_array().unwrap().len(),2);
    }
    #[test] fn integrity_handoff_preserves_damage_and_dispatches_only_once(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let component=fixture.root.join("models/test-model");fs::create_dir_all(&component).unwrap();fs::write(component.join("model.bin"),b"good").unwrap();
        let item=json!({"resource_id":"test-model","kind":"model","version":"1","required":false,"destination":"test-model",
            "source":{"platform":"modelscope","repo_id":"fixture/model","revision":"fixed"},
            "files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&component.join("model.bin")).unwrap()}]});
        resources::write_model_receipt(&component,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
        atomic_json(&fixture.root.join("app/config/resources/catalog.json"),&json!({"resources":[item]})).unwrap();
        fs::write(component.join("model.bin"),b"evil").unwrap();
        maintain_with_settings(&fixture.root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},true,validate_settings).unwrap();
        assert!(!component.exists());
        let requests=crate::component_maintenance::pending_requests(&fixture.context).unwrap();assert_eq!(requests.len(),1);assert_eq!(requests[0]["resource_id"],"test-model");assert_eq!(requests[0]["offline"],false);
        // 未创建任务前重复读取不消费维修机会；实际子任务创建后才确认派发。
        assert!(crate::component_maintenance::pending_repairs(&fixture.context).unwrap());
        crate::component_maintenance::dispatched(&fixture.context,"test-model").unwrap();
        assert!(crate::component_maintenance::pending_requests(&fixture.context).unwrap().is_empty());
        assert_eq!(resources::inventory(&fixture.context).unwrap().iter().find(|row|row["resource_id"]=="test-model").unwrap()["can_start"],false);
        let cache=fixture.root.join("tmp/resource-downloads/test-model/model.bin");fs::create_dir_all(cache.parent().unwrap()).unwrap();fs::write(&cache,b"good").unwrap();
        resources::download_resource(&fixture.context,"test-model",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).unwrap();
        assert_eq!(fs::read(component.join("model.bin")).unwrap(),b"good");
        // 内容已经修好也必须等任务确认成功，轻量盘点本身不清除故障证据。
        assert_eq!(resources::inventory(&fixture.context).unwrap().iter().find(|row|row["resource_id"]=="test-model").unwrap()["can_start"],false);
        crate::component_maintenance::resolved(&fixture.context,&["test-model".into()]).unwrap();
        assert!(crate::component_maintenance::load(&fixture.context).unwrap().unwrap()["issues"].as_array().unwrap().is_empty());
        assert_eq!(resources::inventory(&fixture.context).unwrap().iter().find(|row|row["resource_id"]=="test-model").unwrap()["can_start"],true);
    }
    #[test] fn unavailable_enhancement_is_disabled_without_downgrading_settings_or_reading_weights(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);validate_settings(&fixture.context,false,&AtomicBool::new(false)).unwrap();
        let path=fixture.context.data.join("config/product-settings.json");let mut settings=read(&path).unwrap();
        settings["audio_enhancements"]=json!({"dual_pass":true,"emotion":false,"acoustic_events":false});
        atomic_json(&path,&settings).unwrap();
        let receipt=fixture.context.data.join("state/settings-validation.json");let mut validation=read(&receipt).unwrap();
        validation["sha256"]=json!(file_hash(&path).unwrap());atomic_json(&receipt,&validation).unwrap();
        let target=json!({"resource_id":"paraformer-offline","kind":"model","version":"2","required":false,"destination":"offline",
            "files":[{"path":"model.bin","size_bytes":4,"sha256":"a".repeat(64)}]});
        atomic_json(&fixture.root.join("app/config/resources/catalog.json"),&json!({"resources":[target]})).unwrap();
        resources::disable_unavailable_enhancements(&fixture.context).unwrap();
        let result=read(&path).unwrap();assert_eq!(result["audio_enhancements"]["dual_pass"],false);
        assert_eq!(result["schema_version"],settings["schema_version"]);assert_eq!(read(&receipt).unwrap()["sha256"],file_hash(&path).unwrap());
        assert!(!fixture.root.join("models/offline").exists());
        let once=fs::read(&path).unwrap();resources::disable_unavailable_enhancements(&fixture.context).unwrap();assert_eq!(fs::read(&path).unwrap(),once);
    }
    #[test] fn matching_optional_model_keeps_user_selection_and_stale_handoff_does_not_replay(){
        let fixture=super::super::tests::Fixture::new();runtime(&fixture.root);
        let root=fixture.root.join("models/offline");fs::create_dir_all(&root).unwrap();fs::write(root.join("model.bin"),b"good").unwrap();
        let item=json!({"resource_id":"paraformer-offline","kind":"model","version":"1","required":false,"destination":"offline",
            "files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&root.join("model.bin")).unwrap()}]});
        resources::write_model_receipt(&root,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
        atomic_json(&fixture.root.join("app/config/resources/catalog.json"),&json!({"resources":[item]})).unwrap();
        let path=fixture.context.data.join("config/product-settings.json");
        atomic_json(&path,&json!({"audio_enhancements":{"dual_pass":true,"emotion":false,"acoustic_events":false}})).unwrap();
        resources::disable_unavailable_enhancements(&fixture.context).unwrap();assert_eq!(read(&path).unwrap()["audio_enhancements"]["dual_pass"],true);
        crate::component_maintenance::save(&fixture.context,vec![json!({"resource_id":"paraformer-offline","reason":"故障","content_failure":true,"dispatched":false})],true).unwrap();
        let release=fixture.root.join("app/release.json");let mut value=read(&release).unwrap();value["build"]=json!("next");atomic_json(&release,&value).unwrap();
        assert!(!crate::component_maintenance::pending_repairs(&fixture.context).unwrap());
        assert!(crate::component_maintenance::pending_requests(&fixture.context).unwrap().is_empty());
        crate::component_maintenance::save(&fixture.context,vec![],false).unwrap();
        let carried=crate::component_maintenance::load(&fixture.context).unwrap().unwrap();
        assert_eq!(carried["issues"][0]["content_failure"],true);
        assert!(!crate::component_maintenance::pending_repairs(&fixture.context).unwrap());
    }
}
