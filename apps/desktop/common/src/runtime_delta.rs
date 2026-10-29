//! Runtime delta（运行依赖文件增量）：稀疏候选、逐文件换入及可重复的崩溃回滚。
use std::{collections::{BTreeMap,BTreeSet},fs,path::Path,sync::atomic::AtomicBool};
use serde_json::{Value,json};
use crate::{LaunchContext,resources,workspace::{safe_path,atomic_json,file_hash,remove_owned}};

pub const DESCRIPTOR:&str=".runtime-delta.json"; // DESCRIPTOR（稀疏候选说明），不进入正式环境。
pub const JOURNAL_TARGET:&str="runtime-file-update"; // JOURNAL_TARGET（新事务标识）：旧程序遇到它会安全拒绝，不会按整目录回滚。

/// prepare（生成增量清单）：仅计算新文件摘要，沿用已经完整验证的未变化文件记录。
pub fn prepare(context:&LaunchContext,stage:&Path,deletes:&[String],cancel:&AtomicBool,progress:&mut resources::Progress)->Result<(),String>{
    prepare_inner(context,stage,deletes,false,cancel,progress)
}
pub fn prepare_repair(context:&LaunchContext,stage:&Path,deletes:&[String],cancel:&AtomicBool,progress:&mut resources::Progress)->Result<(),String>{
    prepare_inner(context,stage,deletes,true,cancel,progress)
}
fn prepare_inner(context:&LaunchContext,stage:&Path,deletes:&[String],repair:bool,cancel:&AtomicBool,progress:&mut resources::Progress)->Result<(),String>{
    let root=context.install.join("runtimes/python");
    let old=resources::load(&root.join("manifest.json"))?;
    let mut files:BTreeMap<String,Value>=old["files"].as_array().ok_or("旧环境缺少清单")?.iter()
        .map(|row|Ok((row["path"].as_str().ok_or("旧文件路径无效")?.to_string(),row.clone()))).collect::<Result<_,String>>()?;
    let originals=files.clone();
    let mut changes=BTreeSet::new();
    for name in deletes{let path=safe_path(&root,name)?;if files.remove(name).is_some()||(name.ends_with(".pyc")&&path.is_file()){changes.insert(name.clone());}}
    let fresh=crate::workspace::manifest_with_progress(stage,"python","delta",cancel,progress)?;
    for row in fresh["files"].as_array().unwrap(){
        let name=row["path"].as_str().ok_or("新文件路径无效")?;
        let path=safe_path(&root,name)?;
        if let Some(previous)=files.get(name){
            if previous["sha256"]==row["sha256"]&&previous["size"]==row["size"]{
                fs::remove_file(safe_path(stage,name)?).map_err(|e|e.to_string())?;continue;
            }
            // 安装包不能覆盖其它未卸载包的文件；运行记录、解释器路径及 CRT（C++运行库）由原生层明确管理。
            if name.starts_with("Lib/")&&!deletes.iter().any(|item|item==name){return Err(format!("新依赖与保留文件冲突，原环境保持不变：{name}"));}
        }else if path.exists()&&!changes.contains(name){return Err(format!("新依赖与清单外文件冲突：{name}"));}
        files.insert(name.into(),row.clone());changes.insert(name.into());
    }
    let mut manifest=old;manifest["build"]=json!(crate::workspace::new_id());manifest["files"]=json!(files.into_values().collect::<Vec<_>>());
    atomic_json(&stage.join("manifest.json"),&manifest)?;changes.insert("manifest.json".into());
    let rows=changes.into_iter().map(|name|{
        let path=safe_path(&root,&name)?;let original=path.is_file();
        let (digest,size)=if original{(Some(if !repair&&originals.contains_key(&name){originals[&name]["sha256"].as_str().ok_or("旧文件摘要缺失")?.to_string()}else{file_hash(&path)?}),Some(path.metadata().map_err(|e|e.to_string())?.len()))}else{(None,None)};
        Ok(json!({"path":name,"original":original,"put":stage.join(&name).is_file(),"old_sha256":digest,"old_size":size}))
    }).collect::<Result<Vec<_>,String>>()?;
    atomic_json(&stage.join(DESCRIPTOR),&json!({"schema":1,"base_sha256":file_hash(&root.join("manifest.json"))?,"files":rows,
        "fields":{"schema":"增量候选格式","base_sha256":"更新前完整环境清单摘要","files":"仅变化的文件","path":"环境内相对路径","original":"旧文件存在","put":"写入新文件；否则仅卸载","old_sha256":"旧文件回滚摘要","old_size":"旧文件字节数"}}))
}

pub fn rows(record:&Value)->Result<Vec<Value>,String>{
    let rows=record.as_array().ok_or("文件增量事务缺少变更集合")?;let mut seen=BTreeSet::new();
    for row in rows{
        let name=row["path"].as_str().ok_or("文件增量路径缺失")?;
        crate::workspace::relative_path(name)?;
        if name==DESCRIPTOR||!seen.insert(name.to_lowercase())||!row["original"].is_boolean()||!row["put"].is_boolean(){return Err("文件增量事务格式无效".into());}
        if row["original"]==true&&(row["old_size"].as_u64().is_none()||!row["old_sha256"].as_str().is_some_and(|hash|hash.len()==64&&hash.bytes().all(|byte|byte.is_ascii_hexdigit()))){return Err("文件增量事务缺少原件身份".into());}
    }Ok(rows.clone())
}

pub fn read(context:&LaunchContext,stage:&Path)->Result<Option<Vec<Value>>,String>{
    if !stage.join(DESCRIPTOR).exists(){return Ok(None);}
    let root=context.install.join("runtimes/python");let plan=resources::load(&safe_path(stage,DESCRIPTOR)?)?;
    if plan["schema"]!=1||plan["base_sha256"]!=file_hash(&root.join("manifest.json"))?{return Err("现用环境清单已变化，增量候选失效；请重试".into());}
    let result=rows(&plan["files"])?;
    for row in &result{
        let name=row["path"].as_str().unwrap();let target=safe_path(&root,name)?;let source=safe_path(stage,name)?;
        if target.exists()!=row["original"].as_bool().unwrap()||source.exists()!=row["put"].as_bool().unwrap(){return Err("文件增量候选与当前路径不一致".into());}
        if row["original"]==true&&(target.metadata().map_err(|e|e.to_string())?.len()!=row["old_size"].as_u64().unwrap()||file_hash(&target)?!=row["old_sha256"]){return Err(format!("待变更旧文件已变化，保留原环境：{name}"));}
    }
    // 只校验候选实际含有的内容；完整合并清单在原位加载检查前生效。
    let manifest=resources::load(&stage.join("manifest.json"))?;
    for row in &result{if row["put"]==true&&row["path"]!="manifest.json"{
        let name=row["path"].as_str().unwrap();let source=safe_path(stage,name)?;
        let expected=manifest["files"].as_array().ok_or("合并清单缺失")?.iter().find(|item|item["path"]==name).ok_or("增量文件没有内容登记")?;
        if source.metadata().map_err(|e|e.to_string())?.len()!=expected["size"].as_u64().ok_or("增量文件大小无效")?||file_hash(&source)?!=expected["sha256"]{return Err("增量文件摘要不符".into());}
    }}Ok(Some(result))
}

pub fn apply(root:&Path,stage:&Path,backup:&Path,rows:&[Value])->Result<(),String>{
    for row in rows{
        let name=row["path"].as_str().ok_or("增量路径缺失")?;let target=safe_path(root,name)?;let saved=safe_path(backup,name)?;
        if row["original"]==true{fs::create_dir_all(saved.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(&target,&saved).map_err(|e|format!("旧依赖文件仍被占用或无法保留：{name}：{e}"))?;}
        if row["put"]==true{fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(safe_path(stage,name)?,target).map_err(|e|e.to_string())?;}
    }prune_empty_parents(root,rows)
}

fn prune_empty_parents(root:&Path,rows:&[Value])->Result<(),String>{
    for row in rows{
        let path=safe_path(root,row["path"].as_str().ok_or("增量路径缺失")?)?;let mut parent=path.parent();
        while let Some(folder)=parent.filter(|folder|*folder!=root){
            let name=folder.strip_prefix(root).map_err(|_|"空目录清理越界")?.to_string_lossy().replace('\\',"/");safe_path(root,&name)?;
            match fs::read_dir(folder){
                Ok(mut entries)=>{if entries.next().is_some(){break;}fs::remove_dir(folder).map_err(|e|e.to_string())?;},
                Err(error) if error.kind()==std::io::ErrorKind::NotFound=>{},
                Err(error)=>return Err(error.to_string()),
            }parent=folder.parent();
        }
    }Ok(())
}

pub fn rollback(install:&Path,root:&Path,backup:&Path,rows:&[Value])->Result<(),String>{
    for row in rows.iter().rev(){
        let name=row["path"].as_str().ok_or("增量路径缺失")?;let target=safe_path(root,name)?;let saved=safe_path(backup,name)?;
        let matches=|path:&Path|->Result<bool,String>{
            let info=match path.metadata(){Ok(info)=>info,Err(error) if error.kind()==std::io::ErrorKind::NotFound=>return Ok(false),Err(error)=>return Err(error.to_string())};
            if !info.is_file()||Some(info.len())!=row["old_size"].as_u64(){return Ok(false);}
            Ok(row["old_sha256"]==file_hash(path)?)
        };
        if saved.exists(){
            if !matches(&saved)?{return Err(format!("旧依赖备份损坏，保留事务恢复记录：{name}"));}
            remove_owned(&target,install)?;fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(saved,target).map_err(|e|e.to_string())?;
        }
        else if row["original"]==false{remove_owned(&target,install)?;}
        else if !matches(&target)?{return Err(format!("旧依赖原件及有效备份均缺失，保留事务恢复记录：{name}"));}
    }prune_empty_parents(root,rows)
}

#[cfg(test)]
mod tests{
    use super::*;
    fn fixture()->(std::path::PathBuf,LaunchContext){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("voice-delta-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        for (name,bytes) in [("app/config/default.yaml",b"{}".as_slice()),("app/scripts/backend-entry.py",b"# fixture"),("app/release.json",b"{}"),("runtimes/python/python.exe",b"fixture"),("runtimes/python/keep.dll",b"unchanged"),("runtimes/python/remove.py",b"old"),("runtimes/python/change.dll",b"old")]{
            let path=root.join(name);fs::create_dir_all(path.parent().unwrap()).unwrap();fs::write(path,bytes).unwrap();
        }
        let runtime=root.join("runtimes/python");atomic_json(&runtime.join("manifest.json"),&resources::manifest(&runtime,"python","old").unwrap()).unwrap();
        let context=LaunchContext::portable(&root).unwrap();(owner,context)
    }
    #[test]
    fn sparse_update_keeps_unchanged_file_and_rolls_back_add_upgrade_and_uninstall(){
        let(owner,context)=fixture();let root=context.install.join("runtimes/python");let stage=context.install.join("tmp/stage");fs::create_dir_all(&stage).unwrap();
        fs::write(stage.join("new.dll"),b"new").unwrap();fs::write(stage.join("change.dll"),b"new").unwrap();
        let before=root.join("keep.dll").metadata().unwrap().modified().unwrap();let original=file_hash(&root.join("manifest.json")).unwrap();
        prepare(&context,&stage,&["remove.py".into(),"change.dll".into()],&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
        let changes=read(&context,&stage).unwrap().unwrap();assert!(!changes.iter().any(|r|r["path"]=="keep.dll"));
        let backup=context.install.join("tmp/backup");apply(&root,&stage,&backup,&changes).unwrap();
        assert!(!root.join("remove.py").exists());assert_eq!(fs::read(root.join("change.dll")).unwrap(),b"new");
        assert_eq!(root.join("keep.dll").metadata().unwrap().modified().unwrap(),before);
        rollback(&context.install,&root,&backup,&changes).unwrap();rollback(&context.install,&root,&backup,&changes).unwrap();
        assert!(!root.join("new.dll").exists());assert_eq!(fs::read(root.join("remove.py")).unwrap(),b"old");assert_eq!(fs::read(root.join("change.dll")).unwrap(),b"old");
        assert_eq!(file_hash(&root.join("manifest.json")).unwrap(),original);crate::workspace::remove_owned(&context.install,&owner).unwrap();
    }
    #[test]
    fn crashes_before_or_after_each_file_move_recover_idempotently(){
        for stop in 0..=5{
            let(owner,context)=fixture();let root=context.install.join("runtimes/python");let stage=context.install.join("tmp/stage");fs::create_dir_all(&stage).unwrap();
            fs::write(stage.join("new.dll"),b"new").unwrap();fs::write(stage.join("change.dll"),b"new").unwrap();
            prepare(&context,&stage,&["remove.py".into(),"change.dll".into()],&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
            let changes=read(&context,&stage).unwrap().unwrap();let backup=context.install.join("tmp/native-old");let mut moves=0;
            'outer:for row in &changes{let name=row["path"].as_str().unwrap();let target=root.join(name);let saved=backup.join(name);
                if row["original"]==true{if moves==stop{break;}fs::create_dir_all(saved.parent().unwrap()).unwrap();fs::rename(&target,saved).unwrap();moves+=1;}
                if row["put"]==true{if moves==stop{break 'outer;}fs::rename(stage.join(name),target).unwrap();moves+=1;}
            }
            atomic_json(&context.install.join("tmp/native-install.json"),&json!({"target":JOURNAL_TARGET,"state":"prepared","original":true,"delta":changes,"companions":[{"path":"app/release.json","original":true,"copied":true}]})).unwrap();
            resources::recover(&context).unwrap();resources::recover(&context).unwrap();
            assert_eq!(fs::read(root.join("keep.dll")).unwrap(),b"unchanged");assert_eq!(fs::read(root.join("change.dll")).unwrap(),b"old");assert_eq!(fs::read(root.join("remove.py")).unwrap(),b"old");assert!(!root.join("new.dll").exists());
            crate::workspace::remove_owned(&context.install,&owner).unwrap();
        }
    }
    #[test]
    fn invalid_paths_and_changed_base_are_rejected_before_mutation(){
        assert!(rows(&json!([{"path":"../user-data","original":true,"put":false}])).is_err());
        assert!(rows(&json!([{"path":"A.dll","original":true,"put":false},{"path":"a.dll","original":true,"put":false}])).is_err());
        let(owner,context)=fixture();let stage=context.install.join("tmp/stage");fs::create_dir_all(&stage).unwrap();
        prepare(&context,&stage,&[],&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();fs::write(context.install.join("runtimes/python/manifest.json"),b"changed").unwrap();
        assert!(read(&context,&stage).unwrap_err().contains("已变化"));crate::workspace::remove_owned(&context.install,&owner).unwrap();
    }
    #[test]
    fn missing_or_corrupt_backup_keeps_recovery_journal_and_never_accepts_new_content(){
        for corrupted in [false,true]{
            let(owner,context)=fixture();let root=context.install.join("runtimes/python");let stage=context.install.join("tmp/stage");fs::create_dir_all(&stage).unwrap();fs::write(stage.join("change.dll"),b"new").unwrap();
            prepare(&context,&stage,&["change.dll".into()],&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();let changes=read(&context,&stage).unwrap().unwrap();
            let backup=context.install.join("tmp/native-old");apply(&root,&stage,&backup,&changes).unwrap();
            if corrupted{fs::write(backup.join("change.dll"),b"bad").unwrap();}else{fs::remove_file(backup.join("change.dll")).unwrap();}
            let journal=context.install.join("tmp/native-install.json");atomic_json(&journal,&json!({"target":JOURNAL_TARGET,"state":"swapped","original":true,"delta":changes,"companions":[]})).unwrap();
            assert!(resources::recover(&context).unwrap_err().contains("保留事务恢复记录"));assert!(journal.is_file());
            assert_eq!(fs::read(root.join("keep.dll")).unwrap(),b"unchanged");crate::workspace::remove_owned(&context.install,&owner).unwrap();
        }
    }
    #[test]
    fn committed_crash_keeps_new_files_and_only_finishes_backup_cleanup(){
        let(owner,context)=fixture();let root=context.install.join("runtimes/python");let stage=context.install.join("tmp/stage");fs::create_dir_all(&stage).unwrap();fs::write(stage.join("new.dll"),b"new").unwrap();
        prepare(&context,&stage,&[],&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();let changes=read(&context,&stage).unwrap().unwrap();let backup=context.install.join("tmp/native-old");apply(&root,&stage,&backup,&changes).unwrap();
        let journal=context.install.join("tmp/native-install.json");atomic_json(&journal,&json!({"target":JOURNAL_TARGET,"state":"committed","original":true,"delta":changes,"companions":[{"path":"app/release.json","original":true,"copied":true}]})).unwrap();
        resources::recover(&context).unwrap();assert_eq!(fs::read(root.join("new.dll")).unwrap(),b"new");assert!(!backup.exists());assert!(!journal.exists());crate::workspace::remove_owned(&context.install,&owner).unwrap();
    }
    #[test]
    #[cfg(windows)]
    fn occupied_original_is_not_reported_as_lost_backup_and_recovers_after_release(){
        use std::os::windows::fs::OpenOptionsExt;
        let(owner,context)=fixture();let root=context.install.join("runtimes/python");let stage=context.install.join("tmp/stage");fs::create_dir_all(&stage).unwrap();fs::write(stage.join("change.dll"),b"new").unwrap();
        prepare(&context,&stage,&["change.dll".into()],&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();let changes=read(&context,&stage).unwrap().unwrap();
        let journal=context.install.join("tmp/native-install.json");atomic_json(&journal,&json!({"target":JOURNAL_TARGET,"state":"prepared","original":true,"delta":changes,"companions":[]})).unwrap();
        let held=fs::OpenOptions::new().read(true).share_mode(0).open(root.join("change.dll")).unwrap();
        let error=resources::recover(&context).unwrap_err();assert!(matches!(crate::component_recovery::classify(&error),crate::component_recovery::Event::FileBusy|crate::component_recovery::Event::Permission));assert!(!error.contains("有效备份均缺失"));assert!(journal.is_file());
        drop(held);resources::recover(&context).unwrap();assert!(!journal.exists());assert_eq!(fs::read(root.join("change.dll")).unwrap(),b"old");crate::workspace::remove_owned(&context.install,&owner).unwrap();
    }
}
