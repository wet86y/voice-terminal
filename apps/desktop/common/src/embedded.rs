//! Embedded application（内嵌本体）：清单排除程序自身，启用后补记实际入口摘要。
use std::{fs, io::{Cursor, Read, Write}, path::Path, sync::{Arc, atomic::{AtomicBool, Ordering}}};
use serde_json::json;
use sha2::{Digest,Sha256};
use crate::workspace::{atomic_json, safe_path, file_hash, remove_owned, verify_tree};
const PAYLOAD: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/app.zip"));

pub fn available() -> bool { !PAYLOAD.is_empty() }
pub fn prepare(root: &Path, cancel: Arc<AtomicBool>, progress: &mut dyn FnMut(&str,u64,u64)) -> Result<(),String> {
    crate::workspace::recover_base(root)?;
    let target = safe_path(root,"app")?;
    let payload_digest=format!("{:x}",Sha256::digest(PAYLOAD));
    let current=crate::resources::load(&target.join("release.json")).ok();
    if target.join("VoiceTerminalDesktop.exe").is_file() && target.join("scripts/backend-entry.py").is_file() && current.as_ref().is_some_and(|v|v["build"]==format!("embedded-{}",env!("CARGO_PKG_VERSION"))&&v["embedded_payload_sha256"]==payload_digest) { return Ok(()); }
    if PAYLOAD.is_empty() { return Err("此程序未内嵌本体，请使用 desktop:dist（分发构建）产物。".into()); }
    let stage = safe_path(root,"tmp/bootstrap/app")?;
    remove_owned(&stage,root)?; fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
    let mut archive = zip::ZipArchive::new(Cursor::new(PAYLOAD)).map_err(|e|e.to_string())?;
    let total: u64 = (0..archive.len()).map(|i|archive.by_index(i).map(|f|f.size()).unwrap_or(0)).sum();
    if crate::workspace::available_space(root)? < total + PAYLOAD.len() as u64 { return Err("工作区空间不足。".into()); }
    let mut completed=0; let mut names=std::collections::HashSet::new(); let mut buffer=vec![0;65536];
    for index in 0..archive.len() {
        let mut file=archive.by_index(index).map_err(|e|e.to_string())?;
        let name=file.name().trim_end_matches('/');
        if !names.insert(name.to_lowercase()) || file.unix_mode().is_some_and(|m|m&0o170000==0o120000) { return Err("本体归档包含重复路径或链接。".into()); }
        let destination=safe_path(&stage,name)?;
        if file.is_dir(){fs::create_dir_all(destination).map_err(|e|e.to_string())?;continue;}
        fs::create_dir_all(destination.parent().unwrap()).map_err(|e|e.to_string())?;
        let mut output=fs::File::create(destination).map_err(|e|e.to_string())?;
        loop {
            if cancel.load(Ordering::Acquire){return Err("本体展开已取消，可重新尝试。".into());}
            let count=file.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}
            output.write_all(&buffer[..count]).map_err(|e|e.to_string())?; completed+=count as u64;
            progress("正在展开程序本体",completed,total);
        }
        output.sync_all().map_err(|e|e.to_string())?;
    }
    verify_tree(&stage,"release.json")?;
    let exe=std::env::current_exe().map_err(|e|e.to_string())?;
    let installed=stage.join("VoiceTerminalDesktop.exe");fs::copy(&exe,&installed).map_err(|e|e.to_string())?;
    let manifest_path=stage.join("release.json");
    let mut record:serde_json::Value=serde_json::from_slice(&fs::read(&manifest_path).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    record["embedded_payload_sha256"]=json!(payload_digest); // embedded_payload_sha256（内嵌本体摘要）。
    // 重新展开本体时保留已安装组件的可信清单绑定，用户数据与运行环境保持可用。
    if let Some(components)=current.as_ref().and_then(|value|value["components"].as_array()){
        record["components"]=json!(components.iter().filter(|value|matches!((value["kind"].as_str(),value["path"].as_str()),
            (Some("python"),Some("runtimes/python"))|(Some("models"),Some("models"))|(Some("dsh"),Some("runtimes/dsh"))|(Some("webview2"),Some("runtimes/webview2")))).collect::<Vec<_>>());
    }
    record["files"].as_array_mut().ok_or("本体清单格式错误")?.push(json!({"path":"VoiceTerminalDesktop.exe","size":installed.metadata().map_err(|e|e.to_string())?.len(),"sha256":file_hash(&installed)?}));
    atomic_json(&manifest_path,&record)?;
    if cancel.load(Ordering::Acquire){return Err("本体展开已取消，可重新尝试。".into());}
    progress("启用程序本体",total,total);
    if cancel.load(Ordering::Acquire){return Err("本体展开已取消，可重新尝试。".into());}
    let old=safe_path(root,"tmp/bootstrap-old/app")?;remove_owned(&old,root)?;
    fs::create_dir_all(old.parent().unwrap()).map_err(|e|e.to_string())?;
    let journal=root.join("tmp/bootstrap-transaction.json");
    atomic_json(&journal,&json!({"schema":1,"kind":"app","state":"prepared","original":target.exists()}))?;
    if target.exists(){fs::rename(&target,&old).map_err(|e|e.to_string())?;}
    fs::rename(&stage,&target).map_err(|e|e.to_string())?;
    atomic_json(&journal,&json!({"schema":1,"kind":"app","state":"committed","original":old.exists()}))?;
    remove_owned(&old,root)?;fs::remove_file(journal).map_err(|e|e.to_string())?;
    if crate::canonical_path(&exe).ok()!=crate::canonical_path(&root.join("VoiceTerminal.exe")).ok() { fs::copy(target.join("VoiceTerminalDesktop.exe"),root.join("VoiceTerminal.exe")).map_err(|e|e.to_string())?; }
    let docs=root.join("data/docs");fs::create_dir_all(&docs).map_err(|e|e.to_string())?;
    let guide=target.join("docs/模型运行环境配置指南.md");
    if guide.is_file(){fs::copy(guide,docs.join("模型运行环境配置指南.md")).map_err(|e|e.to_string())?;}
    Ok(())
}
