//! tts_upgrade（语音组件随本体升级）：复用已验证基础库，离线应用随包原生补丁。
use std::{collections::HashSet,fs,io::{Read,Write},path::Path,sync::atomic::AtomicBool};
use serde_json::{json,Value};
use sha2::{Digest,Sha256};
use crate::{installation::Progress,resources::{self,check_cancel},workspace::{self,atomic_json,file_hash,safe_path}};

const RUNTIME:&str="models/audiocpp/runtime"; // RUNTIME（受管原生组件目录）。

/// prepare（准备候选）：返回加入本体事务的路径；此阶段不改写已安装组件。
pub(crate) fn prepare(root:&Path,stage:&Path,release:&mut Value,cancel:&AtomicBool,progress:&mut Progress)->Result<Vec<Value>,String>{
    prepare_with_space(root,stage,release,cancel,progress,||workspace::available_space(root))
}

fn defer(release:&mut Value,reason:String,action:&str){
    if !release["deferred_migrations"].is_object(){release["deferred_migrations"]=json!({});}
    release["deferred_migrations"]["tts_audiocpp"]=json!({"reason":reason,"action":action,
        "fields":{"reason":"暂缓离线升级原因","action":"完成组件修复的方法"}});
    if !release["fields"].is_object(){release["fields"]=json!({});}
    release["fields"]["deferred_migrations"]=json!("旧组件需修复，暂缓迁移但允许本体升级的记录");
}

fn prepare_with_space(root:&Path,stage:&Path,release:&mut Value,cancel:&AtomicBool,progress:&mut Progress,free_space:impl FnOnce()->Result<u64,String>)->Result<Vec<Value>,String>{
    let managed=safe_path(root,RUNTIME)?;
    let legacy=safe_path(root,"runtimes/audiocpp")?;
    let source=if managed.join("cuda/audiocpp_server.exe").is_file(){managed.clone()}
        else if legacy.join("cuda/audiocpp_server.exe").is_file(){legacy}else{return Ok(vec![]);};
    let catalog_path=stage.join("app/config/resources/catalog.json");
    if !catalog_path.is_file(){return Ok(vec![]);}
    let catalog=resources::load(&catalog_path)?;
    let Some(spec)=catalog["resources"].as_array().and_then(|rows|rows.iter().find(|row|row["resource_id"]=="tts-audiocpp-runtime")) else{return Ok(vec![]);};
    if spec["destination"]!="audiocpp/runtime"||spec["source"]["transfer"]["kind"]!="official_audiocpp_patch"{
        return Err("本地语音组件升级规则不受支持。".into());
    }
    let files=spec["files"].as_array().filter(|rows|!rows.is_empty()).ok_or("本地语音组件清单为空")?;
    let mut seen=HashSet::new();
    for row in files{
        let name=row["path"].as_str().ok_or("原生文件路径缺失")?;
        safe_path(&source,name)?;
        if !seen.insert(name.to_lowercase())||row["size_bytes"].as_u64().is_none()||row["sha256"].as_str().is_none(){return Err("原生文件清单无效或重复。".into());}
    }
    // 目标收据及必要文件大小一致时，无需再次核验、复制基础库，也无需预留迁移空间。
    // 全面内容检查由显式维修统一处理，避免迁移入口先读一遍未变化组件。
    if source==managed&&resources::model_receipt_current(&source,spec){
        progress("本地语音运行组件记录匹配，复用当前安装",1,1);
        return Ok(vec![]);
    }
    // 基础库只接受新清单的实际内容；旧收据缺失或失效可由此次强校验修复。
    // 基础库不兼容时仍允许本体升级，新后端会明确阻止过期组件参与本地语音。
    for row in files.iter().filter(|row|row["archive_id"]!="patch"&&row["optional"]!=true){
        check_cancel(cancel)?;
        let name=row["path"].as_str().unwrap();let path=safe_path(&source,name)?;
        let valid=path.is_file()&&path.metadata().map_err(|e|e.to_string())?.len()==row["size_bytes"].as_u64().unwrap()
            &&workspace::file_hash_with_progress(&path,cancel,&mut|_,done,total|progress("正在核验现有语音基础库",done,total))?==row["sha256"].as_str().unwrap();
        if !valid{
            defer(release,format!("基础文件缺失或与目标摘要不符：{name}"),"在组件管理中更新本地语音共用运行程序");
            progress("语音基础库需要修复：先更新本体，请在组件管理中更新语音运行程序",1,1);
            return Ok(vec![]);
        }
    }
    let total=files.iter().try_fold(0u64,|sum,row|sum.checked_add(row["size_bytes"].as_u64().unwrap()).ok_or("语音组件总大小溢出"))?;
    let required=total.checked_add(16*1024*1024).ok_or("语音组件空间需求溢出")?;
    let available=free_space()?;
    if available<required{
        defer(release,format!("暂存语音组件空间不足：需要 {required} 字节，可用 {available} 字节"),"清理磁盘空间后重新运行安装器，或在组件管理中更新本地语音共用运行程序");
        progress("语音组件暂存空间不足：保留旧组件，先更新程序本体",1,1);
        return Ok(vec![]);
    }
    let patch=&spec["source"]["transfer"]["patch"];
    let asset=patch["asset"].as_str().ok_or("随包语音补丁路径缺失")?;
    let patch_path=safe_path(&stage.join("app/config/resources"),asset)?;
    if patch_path.metadata().map_err(|e|e.to_string())?.len()!=patch["size_bytes"].as_u64().ok_or("语音补丁大小缺失")?
        ||workspace::file_hash_with_progress(&patch_path,cancel,progress)?!=patch["sha256"].as_str().ok_or("语音补丁摘要缺失")?{
        return Err("随包语音补丁校验失败，原程序与组件保持不变。".into());
    }
    let mut archive=zip::ZipArchive::new(fs::File::open(patch_path).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    let candidate=safe_path(stage,RUNTIME)?;fs::create_dir_all(&candidate).map_err(|e|e.to_string())?;
    let mut records=vec![];let mut buffer=vec![0u8;1024*1024];
    let mut done=0;
    for row in files{
        check_cancel(cancel)?;
        let name=row["path"].as_str().unwrap();let target=safe_path(&candidate,name)?;
        fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;
        let mut input:Box<dyn Read+'_>=if row["archive_id"]=="patch"{
            let entry_name=row["source_path"].as_str().ok_or("补丁文件来源路径缺失")?;
            workspace::relative_path(entry_name)?;
            let entry=archive.by_name(entry_name).map_err(|e|format!("补丁缺少文件 {entry_name}：{e}"))?;
            if entry.is_dir()||entry.unix_mode().is_some_and(|mode|mode&0o170000==0o120000)||entry.size()!=row["size_bytes"].as_u64().unwrap(){return Err("语音补丁包含无效文件。".into());}
            Box::new(entry)
        }else{
            let path=safe_path(&source,name)?;
            if !path.is_file()&&row["optional"]==true{continue;}
            Box::new(fs::File::open(path).map_err(|e|e.to_string())?)
        };
        let mut output=fs::OpenOptions::new().write(true).create_new(true).open(&target).map_err(|e|e.to_string())?;
        let mut hash=Sha256::new();let mut size=0;
        loop{check_cancel(cancel)?;let count=input.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}
            size+=count as u64;if size>row["size_bytes"].as_u64().unwrap(){return Err("语音候选文件超出锁定大小。".into());}
            output.write_all(&buffer[..count]).map_err(|e|e.to_string())?;hash.update(&buffer[..count]);done+=count as u64;progress("正在离线更新本地语音运行组件",done,total);
        }
        output.sync_all().map_err(|e|e.to_string())?;drop(output);
        let sha=format!("{:x}",hash.finalize());
        if size!=row["size_bytes"].as_u64().unwrap()||sha!=row["sha256"].as_str().unwrap()||file_hash(&target)?!=sha{return Err(format!("语音候选文件校验失败：{name}"));}
        records.push(json!({"path":name,"size":size,"sha256":sha,"optional":row["optional"]==true}));
    }
    resources::write_model_receipt(&candidate,spec,records.clone())?;
    update_manifest(root,stage,release,&candidate,records,cancel,progress)?;
    check_cancel(cancel)?;
    progress("本地语音修复已校验，将与程序一起启用",1,1);
    Ok(vec![json!({"path":RUNTIME,"original":managed.exists()}),json!({"path":"models/manifest.json","original":root.join("models/manifest.json").exists()})])
}

#[cfg(test)]
mod tests{
    use super::*;
    #[test]
    fn current_tts_receipt_skips_content_scan_and_staging_space_check(){
        let root=std::env::temp_dir().join(format!("voice-tts-current-{}",workspace::new_id()));
        let runtime=root.join(RUNTIME);let stage=root.join("tmp/publish/new");
        fs::create_dir_all(runtime.join("cuda")).unwrap();fs::create_dir_all(stage.join("app/config/resources")).unwrap();
        fs::write(runtime.join("cuda/audiocpp_server.exe"),b"native").unwrap();
        fs::write(runtime.join("cuda/base.dll"),b"base").unwrap();
        let spec=json!({"resource_id":"tts-audiocpp-runtime","destination":"audiocpp/runtime","version":"current",
            "source":{"transfer":{"kind":"official_audiocpp_patch"}},"files":[
                {"path":"cuda/audiocpp_server.exe","archive_id":"patch","size_bytes":6,"sha256":file_hash(&runtime.join("cuda/audiocpp_server.exe")).unwrap()},
                {"path":"cuda/base.dll","archive_id":"binary","size_bytes":4,"sha256":file_hash(&runtime.join("cuda/base.dll")).unwrap()}]});
        resources::write_model_receipt(&runtime,&spec,spec["files"].as_array().unwrap().iter()
            .map(|row|json!({"path":row["path"],"size":row["size_bytes"],"sha256":row["sha256"],"optional":false})).collect()).unwrap();
        atomic_json(&stage.join("app/config/resources/catalog.json"),&json!({"resources":[spec]})).unwrap();
        fs::write(runtime.join("cuda/base.dll"),b"same").unwrap(); // 隐蔽内容损坏交由全面维修发现。
        let mut release=json!({"build":"next"});
        assert!(prepare_with_space(&root,&stage,&mut release,&AtomicBool::new(false),&mut|_,_,_|{},||panic!("未变化组件不需要暂存空间")).unwrap().is_empty());
        assert!(release["deferred_migrations"].is_null());
        fs::remove_file(runtime.join("cuda/base.dll")).unwrap();
        assert!(prepare_with_space(&root,&stage,&mut release,&AtomicBool::new(false),&mut|_,_,_|{},||panic!("缺失基础库应在复制前退出")).unwrap().is_empty());
        assert!(release["deferred_migrations"]["tts_audiocpp"]["reason"].as_str().unwrap().contains("base.dll"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn low_disk_space_defers_tts_before_copy_without_blocking_program_upgrade(){
        let root=std::env::temp_dir().join(format!("voice-tts-space-{}",workspace::new_id()));
        let runtime=root.join(RUNTIME);let stage=root.join("tmp/publish/new");
        fs::create_dir_all(runtime.join("cuda")).unwrap();fs::create_dir_all(stage.join("app/config/resources")).unwrap();
        fs::write(runtime.join("cuda/audiocpp_server.exe"),b"old-native").unwrap();fs::write(runtime.join("cuda/base.dll"),b"base").unwrap();
        let spec=json!({"resource_id":"tts-audiocpp-runtime","destination":"audiocpp/runtime","version":"fixed","source":{"transfer":{"kind":"official_audiocpp_patch"}},"files":[
            {"path":"cuda/audiocpp_server.exe","archive_id":"patch","size_bytes":10,"sha256":format!("{:x}",Sha256::digest(b"new-native"))},
            {"path":"cuda/base.dll","archive_id":"binary","size_bytes":4,"sha256":format!("{:x}",Sha256::digest(b"base"))}]});
        atomic_json(&stage.join("app/config/resources/catalog.json"),&json!({"resources":[spec]})).unwrap();
        let mut release=json!({"build":"new"});
        let rows=prepare_with_space(&root,&stage,&mut release,&AtomicBool::new(false),&mut|_,_,_|{},||Ok(0)).unwrap();
        assert!(rows.is_empty());assert_eq!(release["build"],"new");
        assert!(release["deferred_migrations"]["tts_audiocpp"]["reason"].as_str().unwrap().contains("空间不足"));
        assert_eq!(fs::read(runtime.join("cuda/audiocpp_server.exe")).unwrap(),b"old-native");assert!(!stage.join(RUNTIME).exists());
        fs::remove_dir_all(root).unwrap();
    }
}

fn update_manifest(root:&Path,stage:&Path,release:&mut Value,candidate:&Path,records:Vec<Value>,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    // 已准备的声纹迁移清单优先；保留所有其他组件的登记，不重新散列大型语音模型权重。
    let destination=stage.join("models/manifest.json");
    let old=if destination.is_file(){resources::load(&destination)?}
        else if root.join("models/manifest.json").is_file(){resources::load(&root.join("models/manifest.json"))?}
        else{workspace::manifest_with_progress(&root.join("models"),"models","retained",cancel,progress)?};
    if old["schema"]!=1||old["kind"]!="models"{return Err("模型总清单格式无效，无法登记语音升级。".into());}
    let mut files=old["files"].as_array().ok_or("模型总清单文件记录缺失")?.iter()
        .filter(|row|!row["path"].as_str().unwrap_or("").starts_with("audiocpp/runtime/")).cloned().collect::<Vec<_>>();
    for row in records{
        files.push(json!({"path":format!("audiocpp/runtime/{}",row["path"].as_str().unwrap()),"size":row["size"],"sha256":row["sha256"]}));
    }
    let receipt=candidate.join("resource-install.json");
    files.push(json!({"path":"audiocpp/runtime/resource-install.json","size":receipt.metadata().map_err(|e|e.to_string())?.len(),"sha256":file_hash(&receipt)?}));
    files.sort_by(|a,b|a["path"].as_str().cmp(&b["path"].as_str()));
    let build=format!("models-{:x}",Sha256::digest(serde_json::to_vec(&files).map_err(|e|e.to_string())?));
    atomic_json(&destination,&json!({"schema":1,"kind":"models","build":build,"files":files,
        "fields":{"schema":"模型清单版本","kind":"模型资源类别","build":"模型集合摘要编号","files":"安装文件记录","path":"相对路径","size":"字节数","sha256":"文件摘要"}}))?;
    if !release["components"].is_array(){release["components"]=json!([]);}
    let components=release["components"].as_array_mut().unwrap();components.retain(|row|row["kind"]!="models");
    components.push(json!({"kind":"models","path":"models","build":build,"manifest_sha256":file_hash(&destination)?,"optional":true}));
    Ok(())
}
