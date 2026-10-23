//! Component upgrade（组件兼容迁移）：仅由本体资源任务执行，保留随包离线资源和旧版资料处置。
use std::{collections::{HashMap,HashSet},path::Path,sync::atomic::AtomicBool};
use serde_json::{json,Value};
use crate::{LaunchContext,resources::{self,Progress},workspace::{safe_path,file_hash}};

pub(crate) const SPEAKER:&str="campplus-zh-cn-common"; // SPEAKER（当前声纹组件编号）。
const CURRENT:&str="models/speaker/campplus-zh-cn-common"; // CURRENT（当前声纹组件目录）。
const LEGACY:&str="models/speaker/wespeaker-cnceleb-resnet34"; // LEGACY（旧声纹组件目录）。

fn speaker_rule(context:&LaunchContext)->Result<Option<Value>,String>{
    let release=resources::load(&context.installed_program().join("release.json"))?;
    let rule=&release["speaker_migration"];
    if rule.is_null(){return Ok(None);}
    if rule["revision"]!=1||rule["from_model"]!="wespeaker-cnceleb-resnet34"||rule["to_model"]!=SPEAKER||rule["delete_legacy_profiles"]!=true{
        return Err("声纹升级规则不受支持，原组件与档案保持不变。".into());
    }
    let item=resources::spec(context,SPEAKER)?;
    if item["version"]!="v1.0.0"||item["destination"]!="speaker/campplus-zh-cn-common"{return Err("CAM++（声纹模型）升级目标不匹配。".into());}
    Ok(Some(item))
}

/// bundled_speaker（随包声纹种子）：种子损坏须修复本体，不能悄悄改走网络下载。
pub(crate) fn bundled_speaker(context:&LaunchContext)->Result<Option<std::path::PathBuf>,String>{
    if speaker_rule(context)?.is_none(){return Ok(None);}
    let source=safe_path(&context.installed_program(),"resources/model-seeds/campplus-zh-cn-common")?;
    if !source.is_dir(){return Err("CAM++（声纹模型）升级种子缺失，请修复程序本体。".into());}
    Ok(Some(source))
}

pub(crate) fn speaker_pending(context:&LaunchContext)->Result<bool,String>{
    let Some(item)=speaker_rule(context)?else{return Ok(false)};
    let registry=safe_path(&context.install,"data/speaker/registry.json")?;
    Ok(!resources::model_receipt_current(&safe_path(&context.install,CURRENT)?,&item)
        ||safe_path(&context.install,LEGACY)?.exists()
        ||safe_path(&context.install,crate::installation::RETIRED_PROFILE_PATHS[0])?.exists()
        ||(registry.is_file()&&resources::load(&registry).is_ok_and(|value|value["model_id"]=="wespeaker-cnceleb-resnet34")))
}

/// pending（本次本体交接的离线迁移）：只读取声明、收据和文件属性，不在普通启动扫描模型。
pub(crate) fn pending(context:&LaunchContext)->Result<Vec<String>,String>{
    let policy=resources::load(&context.installed_program().join("config/compatibility.json"))?;
    let handlers=policy["upgrade_handlers"].as_array().ok_or("组件迁移声明无效。")?;
    let mut ids=vec![];
    if handlers.iter().any(|value|value=="speaker")&&speaker_pending(context)?{ids.push(SPEAKER.into());}
    if handlers.iter().any(|value|value=="tts_audiocpp"){
        if let Ok(item)=resources::spec(context,"tts-audiocpp-runtime"){
            if ["models/audiocpp/runtime","runtimes/audiocpp"].iter().any(|path|context.install.join(path).join("cuda/audiocpp_server.exe").is_file())
                &&!resources::current_for_upgrade(context,&item){ids.push("tts-audiocpp-runtime".into());}
        }
    }
    Ok(ids)
}

/// speaker_retirements（声纹旧资料处置）：候选验证后才执行；所有处置路径加入同一组件事务。
pub(crate) fn speaker_retirements(context:&LaunchContext,relative:&str)->Result<Vec<String>,String>{
    if relative!=CURRENT||speaker_rule(context)?.is_none(){return Ok(vec![]);}
    let root=&context.install;
    let legacy_registry=safe_path(root,"data/speaker/registry.json")?;
    let legacy_flat=legacy_registry.is_file()&&resources::load(&legacy_registry).is_ok_and(|value|value["model_id"]=="wespeaker-cnceleb-resnet34");
    let mut paths=vec![];
    if safe_path(root,LEGACY)?.exists(){paths.push(LEGACY.into());}
    for relative in crate::installation::RETIRED_PROFILE_PATHS{
        if (*relative==crate::installation::RETIRED_PROFILE_PATHS[0]||legacy_flat)&&safe_path(root,relative)?.exists(){paths.push((*relative).into());}
    }
    if paths.is_empty(){return Ok(paths);}
    // 延续原迁移规则：旧总清单已登记内容的变化必须由合法组件收据确认，否则延期并保留原件。
    let manifest=resources::load(&root.join("models/manifest.json")).unwrap_or(Value::Null);
    let known=manifest["files"].as_array().cloned().unwrap_or_default();
    let mut actual=HashMap::<String,(u64,String)>::new();
    for path in resources::walk(&root.join("models"))?{
        let name=path.strip_prefix(root.join("models")).unwrap().to_string_lossy().replace('\\',"/");
        if name=="manifest.json"||name.starts_with("speaker/wespeaker-cnceleb-resnet34/")||name.starts_with("speaker/campplus-zh-cn-common/"){continue;}
        actual.insert(name,(path.metadata().map_err(|e|e.to_string())?.len(),file_hash(&path)?));
    }
    let mut confirmed=HashSet::new();
    for (name,(size,hash)) in &actual{
        if !known.iter().any(|row|row["path"]==*name&&(row["size"].as_u64()!=Some(*size)||row["sha256"]!=*hash)){continue;}
        let parts=name.split('/').collect::<Vec<_>>();
        let component=(1..parts.len()).rev().map(|index|parts[..index].join("/"))
            .find(|relative|root.join("models").join(relative).join("resource-install.json").is_file())
            .ok_or_else(||format!("保留模型文件无法确认完整性，声纹迁移延期：{name}"))?;
        if !confirmed.insert(component.clone()){continue;}
        let (_,_,records)=resources::read_model_receipt(&root.join("models").join(&component))?;
        for row in records{
            let name=format!("{component}/{}",row["path"].as_str().ok_or("安装收据路径缺失")?);
            match actual.get(&name){
                None if row["optional"]==true=>{},
                Some((size,hash)) if row["size"].as_u64()==Some(*size)&&row["sha256"]==*hash=>{},
                _=>return Err(format!("保留模型文件与安装收据不符，旧声纹及档案保持不变：{name}")),
            }
        }
    }
    Ok(paths)
}

/// offline_tts（离线语音候选）：复用现用或旧版基础库，仅替换随包补丁中的文件。
pub(crate) fn offline_tts(context:&LaunchContext,item:&Value,candidate:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<bool,String>{
    offline_tts_with_space(context,item,candidate,cancel,progress,||crate::workspace::available_space(&context.install))
}
fn offline_tts_with_space(context:&LaunchContext,item:&Value,candidate:&Path,cancel:&AtomicBool,progress:&mut Progress,free_space:impl FnOnce()->Result<u64,String>)->Result<bool,String>{
    let managed=safe_path(&context.install,"models/audiocpp/runtime")?;
    let legacy=safe_path(&context.install,"runtimes/audiocpp")?;
    let source=if managed.join("cuda/audiocpp_server.exe").is_file(){managed}else if legacy.join("cuda/audiocpp_server.exe").is_file(){legacy}else{return Ok(false)};
    let files=item["files"].as_array().filter(|rows|!rows.is_empty()).ok_or("本地语音组件清单为空")?;
    let mut seen=HashSet::new();let mut total=0u64;
    for row in files{
        let name=row["path"].as_str().ok_or("原生文件路径缺失")?;safe_path(&source,name)?;
        if !seen.insert(name.to_lowercase()){return Err("原生文件清单包含重复路径。".into());}
        let size=row["size_bytes"].as_u64().ok_or("原生文件大小缺失")?;
        total=total.checked_add(size).ok_or("语音组件总大小溢出")?;
        if row["archive_id"]=="patch"{continue;}
        let path=safe_path(&source,name)?;
        if !path.is_file()&&row["optional"]==true{continue;}
        if !path.is_file()||path.metadata().map_err(|e|e.to_string())?.len()!=size
            ||crate::workspace::file_hash_with_progress(&path,cancel,progress)?!=row["sha256"].as_str().ok_or("基础库摘要缺失")?{
            resources::check_cancel(cancel)?;return Ok(false);
        }
    }
    if free_space()?<total.saturating_add(16*1024*1024){return Err("语音组件暂存空间不足，原组件保持不变。".into());}
    // 补丁已由资源入口验证并解包；逐文件复制继续核验摘要和写入内容。
    let patch=context.install.join("tmp/resource-downloads/tts-audiocpp-runtime/unpacked/patch");
    let mut records=vec![];
    for row in files{
        resources::check_cancel(cancel)?;
        let name=row["path"].as_str().unwrap();
        let input=if row["archive_id"]=="patch"{safe_path(&patch,row["source_path"].as_str().ok_or("补丁来源路径缺失")?)?}else{safe_path(&source,name)?};
        if !input.is_file()&&row["optional"]==true{continue;}
        let output=safe_path(candidate,name)?;
        resources::copy_file(&input,&output,cancel,progress)?;
        if output.metadata().map_err(|e|e.to_string())?.len()!=row["size_bytes"].as_u64().unwrap()
            ||file_hash(&output)?!=row["sha256"].as_str().ok_or("语音文件摘要缺失")?{return Err(format!("语音候选文件校验失败：{name}"));}
        records.push(json!({"path":name,"size":row["size_bytes"],"sha256":row["sha256"],"optional":row["optional"]==true}));
    }
    resources::write_model_receipt(candidate,item,records)?;
    Ok(true)
}

#[cfg(test)]mod tests{
    use super::*;
    use std::fs;
    #[test]fn low_space_and_cancellation_preserve_old_runtime_before_copying(){
        let root=Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.local/tmp/tasks").join(format!("component-upgrade-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();fs::create_dir_all(root.join("app/config")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# fixture").unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        let runtime=root.join("models/audiocpp/runtime/cuda");fs::create_dir_all(&runtime).unwrap();
        fs::write(runtime.join("audiocpp_server.exe"),b"old").unwrap();fs::write(runtime.join("base.dll"),b"base").unwrap();
        let spec=json!({"files":[{"path":"cuda/base.dll","archive_id":"binary","size_bytes":4,"sha256":file_hash(&runtime.join("base.dll")).unwrap()}]});
        let stage=root.join("tmp/candidate");let cancel=AtomicBool::new(false);
        assert!(offline_tts_with_space(&context,&spec,&stage,&cancel,&mut|_,_,_|{},||Ok(0)).unwrap_err().contains("空间不足"));
        assert!(!stage.exists());assert_eq!(fs::read(runtime.join("audiocpp_server.exe")).unwrap(),b"old");
        cancel.store(true,std::sync::atomic::Ordering::Release);
        assert!(offline_tts_with_space(&context,&spec,&stage,&cancel,&mut|_,_,_|{},||panic!("取消必须发生在空间检查前")).is_err());
        assert!(!stage.exists());crate::workspace::remove_owned(&root,root.parent().unwrap()).unwrap();
    }
}
