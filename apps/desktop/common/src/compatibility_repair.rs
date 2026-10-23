//! 安装器强制兼容路径：修复尝试、清除非法受管资源、默认回退、固定源下载及最终复核。
use super::*;
use std::sync::Arc;
use crate::{runtime, installation::{self, WorkspaceGuard}, workspace::{remove_owned, file_hash}};

fn checkpoint(context:&LaunchContext,phase:&str,reset_devices:bool,reason:Option<&str>)->Result<(),String>{
    let path=state_path(context)?;
    let mut state=read(&path).unwrap_or(json!({"schema":1}));
    state["enforcement"]=json!({"phase":phase,"reset_devices":reset_devices,"reason":reason,
        "fields":{"phase":"强制升级处理阶段","reset_devices":"是否已清除旧环境并回退计算设备","reason":"未完成原因，重试继续修复而不退回旧程序"}});
    state["checked_at_ms"]=json!(now());
    state["revision"]=json!(state["revision"].as_u64().unwrap_or(0).saturating_add(1));
    state["status"]=json!(if phase=="complete"{"compatible"}else{"needs_repair"});
    atomic_json(&path,&state)
}

fn settings(context:&LaunchContext,reset_devices:bool,cancel:&AtomicBool)->Result<(),String>{
    let mut args=vec!["--program".into(),context.program.to_string_lossy().into_owned(),"--data".into(),context.data.to_string_lossy().into_owned()];
    if reset_devices{args.push("--reset-devices".into());}
    let result=runtime::worker(context,&context.python,"compatibility-repair",&args,cancel)?;
    if result["ready"]!=true{return Err("配置修复处理器未确认完成。".into());}
    Ok(())
}

fn repair_bootstrap_policy(context:&LaunchContext)->Result<(),String>{
    let path=safe_path(&context.data,"config/runtime-resources.json")?;
    if !path.exists(){return Ok(());}
    let value=read(&path).unwrap_or(Value::Null);
    // 该文件先于重依赖导入；无效值必须在解释器加载校验之前撤出。
    let usable=value.is_object()
        && value.get("policy_schema").is_none_or(|value|matches!(value.as_u64(),Some(1|2)))
        && value.get("vad_backend").is_none_or(|value|matches!(value.as_str(),Some("auto"|"funasr"|"onnx")))
        && ["shared_audio","disk_history"].iter().all(|name|value.get(*name).is_none_or(Value::is_boolean))
        && value.get("history_cache_bytes").is_none_or(|value|value.as_u64().is_some())
        && value.get("native_threads").is_none_or(|value|value.as_object().is_some_and(|threads|threads.iter().all(|(role,count)|
            matches!(role.as_str(),"bootstrap"|"asr"|"analysis"|"voice"|"speaker")&&count.as_u64().is_some_and(|count|(1..=256).contains(&count)))));
    if !usable {preserve_file(&path,&context.install)?;atomic_json(&path,&json!({"policy_schema":2}))?;}
    Ok(())
}

fn remove_invalid(context:&LaunchContext,path:&Path)->Result<(),String>{
    // 只接受当前目录/依赖清单推导的精确受管组件；父目录和链接由 safe_path 再核对。
    let relative=path.strip_prefix(&context.install).map_err(|_|"非法组件目录越出工作区")?.to_string_lossy().replace('\\',"/");
    if relative!="runtimes/python" && relative!="runtimes/dsh" && !relative.starts_with("models/"){return Err("不能清除未登记的受管组件目录。".into());}
    let checked=safe_path(&context.install,&relative)?;
    if relative.starts_with("models/") && !resources::specs(context)?.iter().any(|item|resources::destination(context,item).is_ok_and(|path|path==checked)) {
        return Err("组件清除目标未登记在当前资源目录中。".into());
    }
    remove_owned(&checked,&context.install)
}

fn repair_twice<T>(cancel:&AtomicBool,mut attempt:impl FnMut(bool)->Result<T,String>,mut cleanup:impl FnMut()->Result<(),String>)->Result<T,String>{
    match attempt(false){
        Ok(value)=>Ok(value),
        Err(_)=>{resources::check_cancel(cancel)?;cleanup()?;resources::check_cancel(cancel)?;attempt(true)}
    }
}

fn fallback_tts(context:&LaunchContext)->Result<(),String>{
    let path=safe_path(&context.data,"config/product-settings.json")?;
    let mut value=read(&path)?;
    // 在线接口没有凭据时使用可下载修复的本地合成路径，不伪造密钥或在线连通结论。
    value["tts"]["backend"]=json!("voxcpm1");
    value["tts"]["local_model"]=json!("voxcpm2");
    atomic_json(&path,&value)
}

fn selected_local(context:&LaunchContext)->Option<String>{
    let selection=crate::credentials::configured_tts_selection(&context.data);
    (selection["backend"]=="voxcpm1").then(||format!("tts-{}",selection["local_model"].as_str().unwrap_or("voxcpm2")))
}

/// enforce_upgrade（完成普通升级）：复用静态登记匹配的组件，只修复缺失或变更项。
pub fn enforce_upgrade(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    maintain(root,cancel,progress,false)
}

/// verify_and_repair（全面完整性维修）：由 --repair-config（当前构建维修参数）显式进入。
pub fn verify_and_repair(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    maintain(root,cancel,progress,true)
}

/// maintain（共用维护流程）：full_verification（全面内容校验）由入口决定，事务及故障恢复共用。
fn maintain(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress,full_verification:bool)->Result<(),String>{
    maintain_with_settings(root,cancel,progress,full_verification,settings)
}

// settings_repair（权威设置处理入口）供微型工作区回归注入，正式路径始终使用上方工作进程。
fn maintain_with_settings(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress,full_verification:bool,
    mut settings_repair:impl FnMut(&LaunchContext,bool,&AtomicBool)->Result<(),String>)->Result<(),String>{
    installation::ensure_idle(root)?;
    let _guard=WorkspaceGuard::acquire(root)?;
    let _verification=crate::verification::VerificationSession::begin();
    let context=LaunchContext::portable(root)?;
    resources::recover(&context)?;
    rules(&context.program,false)?;
    let state=read(&state_path(&context)?).unwrap_or(Value::Null);
    let mut reset_devices=state["enforcement"]["reset_devices"]==true;
    let result=(||{
        repair_bootstrap_policy(&context)?;
        if full_verification{
            checkpoint(&context,"verify_components",reset_devices,None)?;
            progress("完整性维护：单轮核对现有环境、模型与代理组件",0,0);
            resources::prepare_maintenance_verification(&context,&cancel,progress)?;
        }
        checkpoint(&context,"repair_environment",reset_devices,None)?;
        resources::check_cancel(&cancel)?;
        if !full_verification && runtime::current_for_upgrade(&context)?{
            progress("复用登记与依赖锁匹配的语音环境",1,1);
        }else{
            progress("兼容处理：尝试修复现有运行环境",0,0);
            // 两阶段共享进度由 RefCell（同线程可变借用）串行借用，不创建并行安装。
            let reporter=std::cell::RefCell::new(&mut *progress);
            repair_twice(&cancel,|rebuild|runtime::configure(&context,if rebuild{"cpu"}else{"all"},cancel.clone(),&mut **reporter.borrow_mut()),||{
                resources::recover(&context)?;
                if runtime::validate_current(&context,&cancel,&mut **reporter.borrow_mut()).is_ok(){return Ok(());}
                resources::check_cancel(&cancel)?;
                reporter.borrow_mut()("兼容处理：清除未通过修复的旧运行环境",0,0);
                reset_devices=true;
                checkpoint(&context,"rebuild_environment",true,None)?; // 清除前持久化，重试不能丢失设备回退要求。
                remove_invalid(&context,&context.install.join("runtimes/python"))
            })?;
        }
        checkpoint(&context,"repair_settings",reset_devices,None)?;
        progress("兼容处理：归档故障资料并恢复有效设置",0,0);
        settings_repair(&context,reset_devices,&cancel)?;
        let settings_path=safe_path(&context.data,"config/product-settings.json")?;
        let validated_settings_hash=file_hash(&settings_path)?;
        if crate::credentials::configured_tts_selection(&context.data)["backend"]!="voxcpm1"
            && crate::credentials::status(&context.data)?["providers"]["minimax"]["configured"]!=true {
            fallback_tts(&context)?;
        }
        checkpoint(&context,"repair_components",reset_devices,None)?;
        let local=selected_local(&context);
        for item in resources::specs(&context)? {
            resources::check_cancel(&cancel)?;
            let id=item["resource_id"].as_str().ok_or("组件编号缺失")?;
            let destination=resources::destination(&context,&item)?;
            let needed=item["required"]!=false || local.as_deref()==Some(id)
                || (local.is_some()&&id=="tts-audiocpp-runtime");
            // 可选组件缺失仍非阻塞；已经安装的可选组件进入同样的强制修复路径。
            if !needed&&!destination.exists(){continue;}
            if !full_verification && resources::current_for_upgrade(&context,&item){
                progress(&format!("复用静态记录匹配的组件：{id}（组件编号）"),1,1);
                continue;
            }
            progress(&format!("兼容处理：核对并修复 {id}（组件编号）"),0,0);
            let attempt=resources::download_resource(&context,id,cancel.clone(),progress);
            if attempt.is_err(){
                resources::check_cancel(&cancel)?;
                resources::recover(&context)?;
                // 提交后清理可能报错；恢复事务后若当前目标已有效，就不能再次删除。
                let recovered=if item["kind"]=="model"{resources::model_validate(&context,&item,&destination)}else{resources::validate_dsh_activation(&context)};
                if recovered.is_ok(){continue;}
                resources::check_cancel(&cancel)?;
                progress(&format!("兼容处理：清除非法组件并重新下载 {id}（组件编号）"),0,0);
                remove_invalid(&context,&destination)?;
                if let Err(error)=resources::download_resource(&context,id,cancel.clone(),progress){
                    resources::check_cancel(&cancel)?;
                    if needed{return Err(error);}
                    // 可选资料已经清除；不再留下失败组件参与加载，缺失状态仍不阻塞。
                    progress(&format!("可选组件已清除故障来源，后续可继续下载：{id}（组件编号）"),0,0);
                }
            }
        }
        checkpoint(&context,"verify",reset_devices,None)?;
        progress("正在校验强制兼容处理结果",0,0);
        // 仅当组件处理或在线语音回退改变设置时，才重新运行权威设置修复及业务资料检查。
        if file_hash(&settings_path)?!=validated_settings_hash{settings_repair(&context,reset_devices,&cancel)?;}
        resources::check_cancel(&cancel)?;
        crate::verification::ensure_unchanged()?;
        // 单次即时盘点核对组件、规则、配置回执及事务，并仅在全部通过后提交 complete（完成）。
        let ready=complete_maintenance(&context)?;
        if ready.first().is_none_or(|row|row["can_start"]!=true || row["compatibility"]["status"]!="compatible") {
            return Err("最终启动条件在复核后发生变化，修复尚未完成。".into());
        }
        // 浅盘点和状态写回完成后再确认本轮凭据仍有效；失败统一回写 repair_pending（待修复）。
        crate::verification::ensure_unchanged()?;
        progress("兼容处理完成，准备启动正式程序",1,1);
        Ok(())
    })();
    if let Err(error)=&result {
        checkpoint(&context,"repair_pending",reset_devices,Some(error))?;
        return Err(format!("程序本体已就位，兼容修复进度已保留；继续处理将重试未完成项：{error}"));
    }
    result
}

#[cfg(test)] mod tests {
    use super::*;
    #[test]
    fn ordinary_update_reuses_components_and_only_revalidates_changed_settings(){
        for fallback in [false,true]{
            let fixture=super::super::tests::Fixture::new();let root=&fixture.root;
            let lock=root.join("app/config/runtime/voice-runtime.lock.json");
            atomic_json(&lock,&json!({"schema":1,"python":{"version":"3.12.10"}})).unwrap();
            let runtime=root.join("runtimes/python");fs::create_dir_all(&runtime).unwrap();
            fs::write(runtime.join("python.exe"),b"unexecutable fixture").unwrap();
            atomic_json(&runtime.join("runtime.json"),&json!({"schema":2,"python":"3.12.10","profile":"cpu-private",
                "lock_sha256":file_hash(&lock).unwrap(),"groups":["voice-core","torch-cpu","onnx-cpu","sherpa-cpu","modelscope-download","onnx-vad"]})).unwrap();
            atomic_json(&runtime.join("manifest.json"),&resources::manifest(&runtime,"python","fixture").unwrap()).unwrap();
            let component=root.join("models/test-model");fs::create_dir_all(&component).unwrap();fs::write(component.join("model.bin"),b"good").unwrap();
            let item=json!({"resource_id":"test-model","kind":"model","version":"1","destination":"test-model",
                "files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&component.join("model.bin")).unwrap()}]});
            resources::write_model_receipt(&component,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
            atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[item]})).unwrap();
            let before=fs::read(component.join("resource-install.json")).unwrap();let mut calls=0;let mut phases=vec![];
            maintain_with_settings(root,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|phases.push(phase.to_owned()),false,|context,_,_|{
                calls+=1;
                let path=context.data.join("config/product-settings.json");
                let settings=if calls==1{json!({"schema_version":rules(&context.program,false)?["settings_schema_max"],
                    "tts":{"backend":if fallback{"minimax"}else{"voxcpm1"},"local_model":"voxcpm2"}})}else{read(&path)?};
                atomic_json(&path,&settings)?;
                atomic_json(&context.data.join("state/settings-validation.json"),&json!({"schema":1,"settings_schema":settings["schema_version"],"sha256":file_hash(&path)?}))
            }).unwrap();
            assert_eq!(calls,if fallback{2}else{1});
            assert!(phases.iter().any(|phase|phase=="复用登记与依赖锁匹配的语音环境"));
            assert!(phases.iter().any(|phase|phase.starts_with("复用静态记录匹配的组件")));
            assert!(!phases.iter().any(|phase|phase.contains("校验组件文件内容")));
            assert_eq!(fs::read(component.join("resource-install.json")).unwrap(),before);
            assert_eq!(read(&root.join(TRANSACTION_PATHS[0])).unwrap()["enforcement"]["phase"],"complete");
        }
    }

    #[test] fn cleanup_rejects_unregistered_data_and_root(){
        let fixture=super::super::tests::Fixture::new();
        assert!(remove_invalid(&fixture.context,&fixture.root.join("data/speaker")).is_err());
        assert!(remove_invalid(&fixture.context,&fixture.root).is_err());
        let target=fixture.root.join("models/test-invalid");fs::create_dir_all(&target).unwrap();fs::write(target.join("bad.bin"),b"bad").unwrap();
        assert!(remove_invalid(&fixture.context,&target).is_err());
        atomic_json(&fixture.root.join("app/config/resources/catalog.json"),&json!({"resources":[{"resource_id":"test-invalid","kind":"model","destination":"test-invalid","files":[]}]})).unwrap();
        remove_invalid(&fixture.context,&target).unwrap();assert!(!target.exists());
    }
    #[test] fn repair_failure_forces_cleanup_and_rebuild_in_order(){
        let events=std::cell::RefCell::new(vec![]);
        let result=repair_twice(&AtomicBool::new(false),|rebuild|{
            events.borrow_mut().push(if rebuild{"download_fixed"}else{"try_repair"});
            if rebuild{Ok("ready")}else{Err("旧环境不兼容".into())}
        },||{events.borrow_mut().push("remove_invalid");Ok(())}).unwrap();
        assert_eq!(result,"ready");assert_eq!(*events.borrow(),vec!["try_repair","remove_invalid","download_fixed"]);
        let cancel=AtomicBool::new(true);
        assert!(repair_twice::<()>(&cancel,|_|Err("取消".into()),||panic!("取消不能清除原组件")).is_err());
    }
}
