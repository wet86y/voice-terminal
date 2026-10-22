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

/// enforce_upgrade（强制完成升级）：持有产品互斥锁，完成前不启动正式业务。
/// 外部条件失败保留新版本体、修复阶段和已验证下载缓存，下次继续，不回滚旧配置。
pub fn enforce_upgrade(root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    installation::ensure_idle(root)?;
    let _guard=WorkspaceGuard::acquire(root)?;
    let _verification=crate::verification::VerificationSession::begin();
    let context=LaunchContext::portable(root)?;
    resources::recover(&context)?;
    let policy=rules(&context.program,false)?;
    let release=read(&context.program.join("release.json"))?;
    let state=read(&state_path(&context)?).unwrap_or(Value::Null);
    let mut reset_devices=state["enforcement"]["reset_devices"]==true;
    let result=(||{
        repair_bootstrap_policy(&context)?;
        resources::prepare_maintenance_verification(&context,&cancel,progress)?;
        checkpoint(&context,"repair_environment",reset_devices,None)?;
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
        checkpoint(&context,"repair_settings",reset_devices,None)?;
        progress("兼容处理：归档故障资料并恢复有效设置",0,0);
        settings(&context,reset_devices,&cancel)?;
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
        settings(&context,reset_devices,&cancel)?;
        let rows=resources::inventory_raw(&context)?;
        // 环境与每项受管组件已在本轮验证或修复换入；这里只复核组合状态，不再重读全部内容。
        if let Some(row)=rows.iter().find(|row|row["required"]!=false&&row["can_start"]!=true){
            return Err(format!("{}仍待修复：{}",row["label"].as_str().unwrap_or("必需组件"),row["error"].as_str().unwrap_or("未通过实际校验")));
        }
        let checked=inventory(&context)?;
        let compatibility=&checked.first().ok_or("最终兼容盘点缺少语音服务")?["compatibility"];
        if compatibility["stable"]!=true || compatibility["tasks"].as_array().is_none_or(|tasks|tasks.iter().any(|task|task["required"]==true&&task["status"]!="compatible")) {
            return Err("最终兼容盘点仍有必需修复事项，继续强制修复。".into());
        }
        checkpoint(&context,"complete",reset_devices,None)?;
        // 最终结论绑定当前工作区、构建、规则和实际设置；不复用上次升级的成功标记。
        let mut current=read(&state_path(&context)?)?;
        current["workspace_id"]=json!(context.workspace_id);
        current["target_build"]=release["build"].clone();
        current["rules_digest"]=json!(digest(&policy));
        current["settings_sha256"]=json!(file_hash(&safe_path(&context.data,"config/product-settings.json")?)?);
        atomic_json(&state_path(&context)?,&current)?;
        let ready=inventory(&context)?;
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
