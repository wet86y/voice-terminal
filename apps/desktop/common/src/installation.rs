//! installation（正式本体安装）：统一程序包、可恢复事务与首次窗口交接。
use std::{collections::HashSet,fs,io::{Cursor,Read,Write},path::Path,process::Command,sync::{Arc,atomic::{AtomicBool,Ordering}},time::{Duration,Instant}};
use serde_json::json;
use sha2::{Digest,Sha256};
use crate::{canonical_path,workspace::{self,atomic_json,safe_path,remove_owned,file_hash}};
pub const ITEMS:&[&str]=&["app","VoiceTerminal.exe","Verify-Package.ps1","使用与环境说明.md"];
pub type Progress<'a>=dyn FnMut(&str,u64,u64)+'a; // Progress（阶段及字节进度回调）。

// WorkspaceGuard（工作区互斥句柄）：产品运行与本体替换共用，避免检查后再次启动。
pub struct WorkspaceGuard { _file:fs::File }
impl WorkspaceGuard {
    pub fn acquire(root:&Path)->Result<Self,String>{
        let path=safe_path(root,"tmp/program.lock")?;
        fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;
        let mut options=fs::OpenOptions::new();options.write(true).read(true).create(true).truncate(false);
        #[cfg(windows)] {use std::os::windows::fs::OpenOptionsExt;options.share_mode(0);}
        let file=options.open(path).map_err(|_|"此工作区的正式程序或安装任务正在运行，请退出后重试。".to_string())?;
        Ok(Self{_file:file})
    }
}
pub fn ensure_idle(root:&Path)->Result<(),String>{
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::{Foundation::{CloseHandle,INVALID_HANDLE_VALUE},System::Diagnostics::ToolHelp::{CreateToolhelp32Snapshot,Process32FirstW,Process32NextW,PROCESSENTRY32W,TH32CS_SNAPPROCESS}};
        let snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0);
        if snapshot==INVALID_HANDLE_VALUE{return Err("无法检查目标进程占用。".into());}
        let mut row:PROCESSENTRY32W=std::mem::zeroed();row.dwSize=std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut has=Process32FirstW(snapshot,&mut row);
        let expected=canonical_path(&root.join("app/VoiceTerminalDesktop.exe")).ok();
        let result:Result<(),String>=(||{while has!=0{
            if row.th32ProcessID!=std::process::id(){
                let name=String::from_utf16_lossy(&row.szExeFile).trim_end_matches('\0').to_owned();
                if name.eq_ignore_ascii_case("VoiceTerminalDesktop.exe"){
                    if let Some((_,path))=workspace::process_identity(row.th32ProcessID)?{
                        if expected.as_ref().is_some_and(|expected|path.to_string_lossy().eq_ignore_ascii_case(&expected.to_string_lossy())){
                            return Err("目标正式程序正在运行，请退出后重试。".into());
                        }
                    }
                }
            }
            has=Process32NextW(snapshot,&mut row);
        }Ok(())})();CloseHandle(snapshot);result?;
    }
    Ok(())
}
pub fn validate_target(root:&Path)->Result<(),String>{
    workspace::Locator::local(root).validate_target(root)?;
    if root.exists(){
        if root.join("portable.json").is_file(){
            let value=crate::resources::load(&safe_path(root,"portable.json")?)?;
            if value["schema"]!=2||value["app"]!="app"||value["data_dir"]!="data"{return Err("目标不是兼容的已有工作区。".into());}
            if let Some(task)=workspace::pending_task(root)?{if crate::resources::load(&task)?["helper_pid"].as_u64()!=Some(std::process::id() as u64){return Err("存在未完成工作区事务，请从原正式入口恢复后重试。".into());}}
            for relative in ["app","data","models","runtimes","tmp","portable.json"]{safe_path(root,relative)?;}
        }else if fs::read_dir(root).map_err(|e|e.to_string())?.next().is_some(){return Err("请选择空文件夹或已有工作区，不能向无关非空目录展开。".into());}
    }Ok(())
}
fn cancelled(cancel:&AtomicBool)->Result<(),String>{if cancel.load(Ordering::Acquire){Err("本体展开已取消，可重新尝试。".into())}else{Ok(())}}

pub fn verify_installed(root:&Path)->Result<(),String>{
    workspace::verify_tree(&root.join("app"),"release.json")?;
    let release=crate::resources::load(&root.join("app/release.json"))?;
    for item in release["root_files"].as_array().ok_or("正式本体缺少入口清单")?{
        let relative=item["path"].as_str().ok_or("入口路径无效")?;
        if !ITEMS.contains(&relative){return Err("入口清单越出固定本体范围。".into());}
        let path=safe_path(root,relative)?;
        if fs::metadata(&path).map_err(|e|e.to_string())?.len()!=item["size"].as_u64().ok_or("入口大小无效")?||file_hash(&path)?!=item["sha256"].as_str().ok_or("入口摘要无效")?{return Err(format!("正式入口校验失败：{relative}"));}
    }Ok(())
}

fn verify_package(stage:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let record=crate::resources::load(&stage.join("program-package.json"))?;
    if record["schema"]!=1||record["role"]!="product"{return Err("正式程序包格式无效。".into());}
    let rows=record["files"].as_array().ok_or("正式程序包缺少文件清单")?;
    let total=rows.iter().try_fold(0u64,|n,row|n.checked_add(row["size"].as_u64().ok_or("程序包文件大小无效")?).ok_or_else(||"程序包大小溢出".to_string()))?;
    let mut expected=HashSet::new();let mut completed=0;
    for row in rows{
        cancelled(cancel)?;
        let relative=row["path"].as_str().ok_or("程序包路径无效")?;
        if !ITEMS.contains(&relative)&&!relative.starts_with("app/"){return Err("程序包包含固定本体之外的内容。".into());}
        if !expected.insert(relative.to_lowercase()){return Err("程序包清单有重复路径。".into());}
        let path=safe_path(stage,relative)?;
        let mut input=fs::File::open(&path).map_err(|e|e.to_string())?;
        let mut hash=Sha256::new();let mut buffer=[0;65536];let mut size=0;
        loop{cancelled(cancel)?;let count=input.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}hash.update(&buffer[..count]);size+=count as u64;completed+=count as u64;progress("正在校验程序本体",completed,total);}
        if size!=row["size"].as_u64().unwrap()||format!("{:x}",hash.finalize())!=row["sha256"].as_str().ok_or("程序包摘要无效")?{return Err(format!("程序包文件校验失败：{relative}"));}
    }
    fn collect(root:&Path,path:&Path,out:&mut HashSet<String>)->Result<(),String>{
        for entry in fs::read_dir(path).map_err(|e|e.to_string())?{let path=entry.map_err(|e|e.to_string())?.path();let relative=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/");safe_path(root,&relative)?;if path.is_dir(){collect(root,&path,out)?;}else if relative!="program-package.json"{out.insert(relative.to_lowercase());}}
        Ok(())
    }
    let mut actual=HashSet::new();collect(stage,stage,&mut actual)?;
    if actual!=expected{return Err("程序包实际文件与清单不一致。".into());}
    for entry in ITEMS{if !stage.join(entry).exists(){return Err(format!("正式程序包缺少 {entry}（固定本体文件）。"));}}
    workspace::verify_tree(&stage.join("app"),"release.json")?;
    let release=crate::resources::load(&stage.join("app/release.json"))?;
    if release["build"]!=record["build"]{return Err("正式程序包的构建编号不一致。".into());}Ok(())
}
fn activate(root:&Path,stage:&Path,progress:&mut Progress)->Result<(),String>{
    // components（组件登记）属于工作区状态；固定本体仍来自同一程序包。
    let incoming=stage.join("app/release.json");let mut release=crate::resources::load(&incoming)?;
    if let Ok(old)=crate::resources::load(&root.join("app/release.json")){
        if let Some(rows)=old["components"].as_array(){release["components"]=json!(rows.iter().filter(|row|matches!((row["kind"].as_str(),row["path"].as_str()),(Some("python"),Some("runtimes/python"))|(Some("models"),Some("models"))|(Some("dsh"),Some("runtimes/dsh"))|(Some("webview2"),Some("runtimes/webview2")))).collect::<Vec<_>>());atomic_json(&incoming,&release)?;}
    }
    let journal=root.join("tmp/update-transaction.json");
    let rows=ITEMS.iter().map(|name|json!({"path":name,"original":root.join(name).exists()})).collect::<Vec<_>>();
    let mut record=json!({"schema":1,"state":"prepared","items":rows,"fields":{"schema":"本体替换事务格式","state":"准备或提交阶段","items":"固定受管文件","path":"相对路径","original":"是否存在原文件"}});
    atomic_json(&journal,&record)?;
    progress("启用程序本体",1,1); // 从这里开始禁止取消；失败按日志恢复原本体。
    let result:Result<(),String>=(||{
        for name in ITEMS{
            let target=safe_path(root,name)?;let old=safe_path(root,&format!("tmp/publish/old/{name}"))?;
            fs::create_dir_all(old.parent().unwrap()).map_err(|e|e.to_string())?;
            if target.exists(){fs::rename(&target,&old).map_err(|e|format!("目标文件被占用，无法启用：{e}"))?;}
            fs::rename(stage.join(name),target).map_err(|e|e.to_string())?;
        }
        workspace::normalize_layout(root)?;
        record["state"]=json!("committed");atomic_json(&journal,&record)?;Ok(())
    })();
    match result{Ok(())=>workspace::recover_install(root),Err(error)=>{workspace::recover_install(root).map_err(|recovery|format!("{error}；恢复原本体失败：{recovery}"))?;Err(error)}}
}
pub fn install(payload:&[u8],root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    validate_target(root)?;fs::create_dir_all(root).map_err(|e|e.to_string())?;
    let root=canonical_path(root)?;ensure_idle(&root)?;
    // 空目录已在上方验证；初始化与兼容转换也必须在同一互斥句柄内完成。
    let _guard=WorkspaceGuard::acquire(&root)?;
    if !root.join("portable.json").exists(){workspace::initialize_guarded(&root,&_guard)?;}
    workspace::recover_install(&root)?;workspace::recover_base(&root)?;
    workspace::normalize_layout(&root)?;
    let mut archive=zip::ZipArchive::new(Cursor::new(payload)).map_err(|e|e.to_string())?;
    let total=(0..archive.len()).try_fold(0u64,|sum,i|sum.checked_add(archive.by_index(i).map_err(|e|e.to_string())?.size()).ok_or_else(||"程序包大小溢出".to_string()))?;
    if workspace::available_space(&root)?<total.saturating_mul(2).saturating_add(16*1024*1024){return Err("工作区空间不足，无法暂存及替换本体。".into());}
    let stage=safe_path(&root,"tmp/publish/new")?;remove_owned(&stage,&root)?;fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
    let result=(||{
        let mut seen=HashSet::new();let mut done=0;let mut buffer=[0;65536];
        for index in 0..archive.len(){
            cancelled(&cancel)?;let mut entry=archive.by_index(index).map_err(|e|e.to_string())?;let relative=entry.name().trim_end_matches('/').to_owned();
            if !seen.insert(relative.to_lowercase())||entry.unix_mode().is_some_and(|mode|mode&0o170000==0o120000){return Err("程序包含有重复路径或链接。".into());}
            if !ITEMS.contains(&relative.as_str())&&!relative.starts_with("app/")&&relative!="program-package.json"{return Err("程序包含有用户数据或可选组件。".into());}
            let destination=safe_path(&stage,&relative)?;
            if entry.is_dir(){fs::create_dir_all(destination).map_err(|e|e.to_string())?;continue;}
            fs::create_dir_all(destination.parent().unwrap()).map_err(|e|e.to_string())?;
            let mut outgoing=fs::OpenOptions::new().write(true).create_new(true).open(destination).map_err(|e|e.to_string())?;
            loop{cancelled(&cancel)?;let count=entry.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}outgoing.write_all(&buffer[..count]).map_err(|e|e.to_string())?;done+=count as u64;progress("正在展开程序本体",done,total);}
            outgoing.sync_all().map_err(|e|e.to_string())?;
        }
        verify_package(&stage,&cancel,progress)?;cancelled(&cancel)?;activate(&root,&stage,progress)
    })();
    if !root.join("tmp/update-transaction.json").exists(){let _=remove_owned(&root.join("tmp/publish"),&root);}result
}
pub fn copy_program(source:&Path,target:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    // 空工作区从已经发布的本体初始化，仅复制清单内文件；不读取业务源码或可选组件。
    workspace::verify_tree(&source.join("app"),"release.json")?;
    let release=crate::resources::load(&source.join("app/release.json"))?;
    let mut bytes=Cursor::new(Vec::new());
    {let mut archive=zip::ZipWriter::new(&mut bytes);let options=zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Stored);
        let mut rows=Vec::new();
        for row in release["files"].as_array().ok_or("本体清单无效")?{let relative=format!("app/{}",row["path"].as_str().ok_or("本体路径无效")?);cancelled(&cancel)?;archive.start_file(&relative,options).map_err(|e|e.to_string())?;let data=fs::read(safe_path(source,&relative)?).map_err(|e|e.to_string())?;archive.write_all(&data).map_err(|e|e.to_string())?;rows.push(json!({"path":relative,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))}));}
        let mut clean=release.clone();clean["components"]=json!([]);let data=serde_json::to_vec_pretty(&clean).map_err(|e|e.to_string())?;archive.start_file("app/release.json",options).map_err(|e|e.to_string())?;archive.write_all(&data).map_err(|e|e.to_string())?;rows.push(json!({"path":"app/release.json","size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))}));
        for name in ITEMS.iter().filter(|name|**name!="app"){let path=safe_path(source,name)?;let expected=release["root_files"].as_array().and_then(|rows|rows.iter().find(|row|row["path"]==*name)).ok_or("正式入口缺少固定文件登记")?;if file_hash(&path)?!=expected["sha256"].as_str().ok_or("入口摘要无效")?{return Err("当前正式入口校验失败，不能初始化其他工作区。".into());}let data=fs::read(path).map_err(|e|e.to_string())?;archive.start_file(*name,options).map_err(|e|e.to_string())?;archive.write_all(&data).map_err(|e|e.to_string())?;rows.push(json!({"path":name,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))}));}
        archive.start_file("program-package.json",options).map_err(|e|e.to_string())?;archive.write_all(&serde_json::to_vec(&json!({"schema":1,"role":"product","build":release["build"],"files":rows})).unwrap()).map_err(|e|e.to_string())?;archive.finish().map_err(|e|e.to_string())?;
    }install(bytes.get_ref(),target,cancel,progress)
}

// acknowledge（首次受控窗口确认）：校验一次性凭据，仅写入工作区暂存区。
pub fn acknowledge(root:&Path,args:&[String])->Result<(),String>{
    if let Some(index)=args.iter().position(|arg|arg=="--startup-handoff"){
        let token=args.get(index+1).ok_or("启动交接缺少凭据")?;
        if token.is_empty()||!token.bytes().all(|b|b.is_ascii_hexdigit()||b==b'-'){return Err("启动交接凭据无效。".into());}
        let path=safe_path(root,&format!("tmp/handoff-{token}.json"))?;
        let request=crate::resources::load(&path)?;
        if request["token"]!=*token||request["state"]!="waiting"{return Err("启动交接已过期。".into());}
        let pid=std::process::id();let (created,executable)=workspace::process_identity(pid)?.ok_or("桌面进程身份不可用")?;
        atomic_json(&path,&json!({"token":token,"state":"window-ready","pid":pid,"create_time":created,"executable":executable,"fields":{"token":"一次性启动凭据","state":"首个受控窗口已显示","pid":"正式桌面进程编号","create_time":"进程创建时间","executable":"正式宿主位置"}}))?;
    }Ok(())
}
pub fn launch(root:&Path)->Result<u32,String>{
    let expected=canonical_path(&root.join("app/VoiceTerminalDesktop.exe"))?;
    let token=workspace::new_id();let path=safe_path(root,&format!("tmp/handoff-{token}.json"))?;
    atomic_json(&path,&json!({"token":token,"state":"waiting","fields":{"token":"一次性启动凭据","state":"等待受控窗口显示"}}))?;
    let mut command=Command::new(root.join("VoiceTerminal.exe"));command.args(["--startup-handoff",&token]).current_dir(root);
    command.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    #[cfg(windows)] {use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
    command.spawn().map_err(|e|format!("正式程序启动失败：{e}"))?;
    let deadline=Instant::now()+Duration::from_secs(45);
    while Instant::now()<deadline{
        if let Ok(record)=crate::resources::load(&path){if record["token"]==token&&record["state"]=="window-ready"{
            let pid=record["pid"].as_u64().ok_or("交接进程编号无效")? as u32;
            if let Some((created,exe))=workspace::process_identity(pid)?{if record["create_time"].as_u64()==Some(created)&&exe.to_string_lossy().eq_ignore_ascii_case(&expected.to_string_lossy()){let _=fs::remove_file(path);return Ok(pid);}}
        }}std::thread::sleep(Duration::from_millis(100));
    }
    let _=fs::remove_file(path);Err("正式程序未确认窗口交接。已安装本体保留，请退出目标程序后重试或双击工作区正式入口。".into())
}

#[cfg(test)] mod tests{
    use super::*;
    use std::path::PathBuf;
    fn fixture()->PathBuf{let root=std::env::temp_dir().join(format!("voice-install-中文 空格-{}",workspace::new_id()));fs::create_dir_all(&root).unwrap();root}
    fn package(build:&str,corrupt:bool)->Vec<u8>{
        let mut payload=Cursor::new(Vec::new());
        let app_bytes=build.as_bytes();let hash=format!("{:x}",Sha256::digest(app_bytes));
        let app=json!({"schema":1,"kind":"app","build":build,"files":[{"path":"business.py","size":app_bytes.len(),"sha256":hash}],"components":[],"required_components":[],"optional_components":["python","models","dsh","webview2"]});
        let entries=vec![("app/business.py",app_bytes.to_vec()),("app/release.json",serde_json::to_vec(&app).unwrap()),("VoiceTerminal.exe",b"formal-entry".to_vec()),("Verify-Package.ps1",b"verify".to_vec()),("使用与环境说明.md",b"instructions".to_vec())];
        let rows=entries.iter().map(|(path,data)|json!({"path":path,"size":data.len(),"sha256":if corrupt&&*path=="app/business.py"{"0".repeat(64)}else{format!("{:x}",Sha256::digest(data))}})).collect::<Vec<_>>();
        {let mut zip=zip::ZipWriter::new(&mut payload);let options=zip::write::SimpleFileOptions::default();for (name,data) in entries{zip.start_file(name,options).unwrap();zip.write_all(&data).unwrap();}zip.start_file("program-package.json",options).unwrap();zip.write_all(&serde_json::to_vec(&json!({"schema":1,"role":"product","build":build,"files":rows})).unwrap()).unwrap();zip.finish().unwrap();}payload.into_inner()
    }
    fn run(payload:&[u8],root:&Path)->Result<(),String>{install(payload,root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{})}
    #[test]fn reinstall_same_or_new_build_preserves_data_identity_and_components(){
        let root=fixture();run(&package("first",false),&root).unwrap();let id=crate::resources::load(&root.join("portable.json")).unwrap()["workspace_id"].clone();
        fs::write(root.join("data/settings.json"),b"user-settings").unwrap();fs::create_dir_all(root.join("runtimes/python")).unwrap();fs::write(root.join("runtimes/python/content.bin"),b"installed-python").unwrap();fs::write(root.join("models/model.bin"),b"installed-model").unwrap();
        let components=json!([{"kind":"python","path":"runtimes/python","build":"existing","manifest_sha256":"fixture"}]);let mut release=crate::resources::load(&root.join("app/release.json")).unwrap();release["components"]=components.clone();atomic_json(&root.join("app/release.json"),&release).unwrap();
        for build in ["first","second"]{run(&package(build,false),&root).unwrap();assert_eq!(fs::read(root.join("app/business.py")).unwrap(),build.as_bytes());assert_eq!(fs::read(root.join("data/settings.json")).unwrap(),b"user-settings");assert_eq!(fs::read(root.join("runtimes/python/content.bin")).unwrap(),b"installed-python");assert_eq!(fs::read(root.join("models/model.bin")).unwrap(),b"installed-model");assert_eq!(crate::resources::load(&root.join("portable.json")).unwrap()["workspace_id"],id);assert_eq!(crate::resources::load(&root.join("app/release.json")).unwrap()["components"],components);assert!(!root.join("tmp/update-transaction.json").exists());}
        fs::remove_dir_all(root).unwrap();
    }
    #[test]fn corrupt_package_and_cancel_do_not_replace_formal_program(){
        let root=fixture();run(&package("first",false),&root).unwrap();assert!(run(&package("broken",true),&root).is_err());
        let cancel=Arc::new(AtomicBool::new(false));let trigger=cancel.clone();let result=install(&package("cancelled",false),&root,cancel,&mut|phase,_,_|{if phase=="正在校验程序本体"{trigger.store(true,Ordering::Release);}});assert!(result.unwrap_err().contains("取消"));assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"first");fs::remove_dir_all(root).unwrap();
    }
    #[test]fn unrelated_nonempty_directory_is_rejected_without_mutation(){let root=fixture();fs::write(root.join("personal.txt"),b"personal").unwrap();assert!(run(&package("first",false),&root).unwrap_err().contains("非空"));assert!(!root.join("portable.json").exists());assert_eq!(fs::read(root.join("personal.txt")).unwrap(),b"personal");fs::remove_dir_all(root).unwrap();}
    #[cfg(windows)]#[test]fn workspace_lock_and_failed_activation_keep_original_program(){
        use std::os::windows::fs::OpenOptionsExt;
        let root=fixture();run(&package("first",false),&root).unwrap();let guard=WorkspaceGuard::acquire(&root).unwrap();assert!(run(&package("second",false),&root).unwrap_err().contains("正在运行"));drop(guard);
        let held=fs::OpenOptions::new().read(true).share_mode(0).open(root.join("VoiceTerminal.exe")).unwrap();assert!(run(&package("second",false),&root).is_err());drop(held);assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"first");assert_eq!(fs::read(root.join("VoiceTerminal.exe")).unwrap(),b"formal-entry");assert!(!root.join("tmp/update-transaction.json").exists());fs::remove_dir_all(root).unwrap();
    }
    #[test]fn interrupted_activation_restores_previous_body(){let root=fixture();run(&package("first",false),&root).unwrap();fs::create_dir_all(root.join("tmp/publish/old")).unwrap();fs::rename(root.join("app"),root.join("tmp/publish/old/app")).unwrap();fs::create_dir_all(root.join("app")).unwrap();fs::write(root.join("app/partial"),b"partial").unwrap();atomic_json(&root.join("tmp/update-transaction.json"),&json!({"schema":1,"state":"prepared","items":[{"path":"app","original":true}]})).unwrap();workspace::recover_install(&root).unwrap();assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"first");assert!(!root.join("app/partial").exists());fs::remove_dir_all(root).unwrap();}
}
