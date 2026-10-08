//! Workspace（工作区）：分发定位、基础安装与可恢复迁移。
use std::{fs, io::{Read, Write}, path::{Path, PathBuf}, sync::{mpsc, Arc, Mutex, atomic::{AtomicBool, AtomicUsize, Ordering}}, thread, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use crate::{canonical_path, LaunchContext, download::{DownloadFile, download}};

pub const EMBEDDED_CATALOG: &str = include_str!(concat!(env!("OUT_DIR"), "/distribution.json"));
const MIGRATION_COPY_WORKERS: usize = 4; // MIGRATION_COPY_WORKERS（迁移文件复制的有界工作线程数）。
const MIGRATION_COPY_BUFFER: usize = 1024 * 1024; // MIGRATION_COPY_BUFFER（每个复制线程复用的缓冲区字节数）。

#[derive(Clone, Debug)]
pub struct Locator {
    pub path: PathBuf, // path（定位记录文件）。
    pub source_base: Option<String>, // source_base（验收发布源覆盖）。
    pub instance_tag: String, // instance_tag（隔离实例标识）。
    pub initial_root: Option<PathBuf>, // initial_root（仅验收配置允许的自动目录选择）。
    pub headless: bool, // headless（验收无窗口入口）。
    pub qualification: Option<PathBuf>, // qualification（验收配置位置）。
}

#[derive(Clone, Serialize, Deserialize)]
struct Registry {
    schema: u32, // schema（定位格式）。
    workspace_id: String, // workspace_id（稳定编号）。
    workspace_root: PathBuf, // workspace_root（工作区绝对路径）。
}

#[derive(Deserialize)]
struct Catalog {
    schema: u32, // schema（分发格式）。
    source_base: String, // source_base（固定发布源）。
    packages: Vec<Package>, // packages（基础组件包）。
}
#[derive(Deserialize)]
struct Package {
    kind: String, // kind（组件类别）。
    archive: DownloadFile, // archive（完整归档大小与摘要）。
    #[serde(default)] parts: Vec<DownloadFile>, // parts（独立分片）。
    #[serde(default)] unpacked_size: u64, // unpacked_size（展开字节数）。
}

pub fn atomic_json(path: &Path, value: &Value) -> Result<(), String> {
    fs::create_dir_all(path.parent().ok_or("记录缺少父目录")?).map_err(|e|e.to_string())?;
    let temporary = path.with_extension(format!("{}.tmp", std::process::id()));
    let mut stream = fs::File::create(&temporary).map_err(|e|e.to_string())?;
    stream.write_all(&serde_json::to_vec_pretty(value).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    stream.sync_all().map_err(|e|e.to_string())?; drop(stream);
    #[cfg(windows)] {
        use std::os::windows::ffi::OsStrExt;
        unsafe extern "system" {fn MoveFileExW(from:*const u16,to:*const u16,flags:u32)->i32;}
        let source:Vec<_>=temporary.as_os_str().encode_wide().chain(Some(0)).collect();
        let target:Vec<_>=path.as_os_str().encode_wide().chain(Some(0)).collect();
        if unsafe {MoveFileExW(source.as_ptr(),target.as_ptr(),1|8)} == 0 {return Err(std::io::Error::last_os_error().to_string());}
    }
    #[cfg(not(windows))] fs::rename(temporary,path).map_err(|e|e.to_string())?;
    Ok(())
}

fn load(path:&Path)->Result<Value,String> {
    let content=fs::read(path).map_err(|e|format!("无法读取 {}：{e}",path.display()))?;
    serde_json::from_slice(content.strip_prefix(&[0xef,0xbb,0xbf]).unwrap_or(&content)).map_err(|e|e.to_string())
}
pub fn file_hash(path:&Path)->Result<String,String> {
    let mut buffer=vec![0;MIGRATION_COPY_BUFFER];
    file_hash_with_buffer(path,&mut buffer)
}
fn file_hash_with_buffer(path:&Path,buffer:&mut [u8])->Result<String,String>{
    let mut stream=fs::File::open(path).map_err(|e|e.to_string())?;
    let mut hash=Sha256::new();
    loop {let count=stream.read(buffer).map_err(|e|e.to_string())?;if count==0{break;}hash.update(&buffer[..count]);}
    Ok(format!("{:x}",hash.finalize()))
}
fn file_hash_for_copy(path:&Path,buffer:&mut [u8],cancel:&AtomicBool,stop:&AtomicBool)->Result<String,String>{
    let mut stream=fs::File::open(path).map_err(|error|error.to_string())?;
    let mut hash=Sha256::new();
    loop{
        if cancel.load(Ordering::Acquire){return Err("迁移已取消，原工作区保留。".into());}
        if stop.load(Ordering::Acquire){return Err("迁移复制已因其他文件失败而停止。".into());}
        let count=stream.read(buffer).map_err(|error|error.to_string())?;
        if count==0{break;}
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}",hash.finalize()))
}

fn reparse(path:&Path)->bool {
    fs::symlink_metadata(path).map(|metadata|{
        #[cfg(windows)] {use std::os::windows::fs::MetadataExt;metadata.file_attributes()&0x400!=0}
        #[cfg(not(windows))] {metadata.file_type().is_symlink()}
    }).unwrap_or(false)
}
pub fn safe_path(root:&Path,relative:&str)->Result<PathBuf,String> {
    let path=Path::new(relative);
    if relative.is_empty()||relative.contains(':')||relative.contains('\\')||path.is_absolute()||
        path.components().any(|part|!matches!(part,std::path::Component::Normal(_))) {return Err("资源路径越界。".into());}
    let mut current=root.to_path_buf();
    for part in path.components() {
        let name=part.as_os_str().to_string_lossy(); let stem=name.split('.').next().unwrap_or("").to_ascii_lowercase();
        if name.ends_with(' ')||name.ends_with('.')||["con","prn","aux","nul"].contains(&stem.as_str())||
            (stem.len()==4&&(stem.starts_with("com")||stem.starts_with("lpt"))&&stem.as_bytes()[3].is_ascii_digit()) {return Err("资源路径包含系统保留名称。".into());}
        current.push(part.as_os_str()); if reparse(&current){return Err("受管路径不能经过目录联接。".into());}
    } Ok(current)
}

impl Locator {
    pub fn validate_target(&self,root:&Path)->Result<(),String>{
        if !root.is_absolute(){return Err("工作区必须使用绝对路径。".into());}
        let root = normalized_target(root)?;
        if root.file_name().is_some_and(|v|v==".local")&&root.parent().is_some_and(|parent|parent.join("apps/desktop/Cargo.toml").is_file()){
            return Err("分发工作区不能复用开发仓库的 .local（本机资源）。".into());
        }
        if self.qualification.is_some(){
            let sandbox=canonical_path(Path::new("D:/项目开发/测试"))?;
            if !root.starts_with(&sandbox) || root == sandbox {return Err("验收工作区操作必须限定在测试目录的独立子目录内。".into());}
        }Ok(())
    }
    pub fn from_args(args:&[String])->Result<Self,String> {
        let mut locator=Self {path:PathBuf::from(std::env::var_os("LOCALAPPDATA").ok_or("无法定位系统应用数据目录")?).join("VoiceTerminal/workspace.json"),
            source_base:None,instance_tag:"distribution".into(),initial_root:None,headless:false,qualification:None};
        if let Some(index)=args.iter().position(|v|v=="--qualification-config") {
            if !cfg!(feature="qualification"){return Err("此分发构建不接受验收配置。".into());}
            let path=canonical_path(Path::new(args.get(index+1).ok_or("验收配置缺少路径")?))?;
            let qualification=load(&path)?;
            let registry=PathBuf::from(qualification["registry"].as_str().ok_or("验收配置缺少独立定位文件")?);
            let sandbox=canonical_path(Path::new("D:/项目开发/测试"))?;
            if !registry.is_absolute()||!registry.starts_with(&sandbox){return Err("验收定位记录必须在指定测试目录内。".into());}
            locator.path=registry;
            locator.source_base=qualification["source_base"].as_str().map(str::to_owned);
            locator.instance_tag=qualification["instance_tag"].as_str().unwrap_or("qualification").to_owned();
            locator.initial_root=qualification["workspace_root"].as_str().map(PathBuf::from);
            locator.headless=qualification["headless"].as_bool().unwrap_or(false);
            locator.qualification=Some(path);
            if let Some(root)=&locator.initial_root {if !root.starts_with(&sandbox){return Err("验收工作区必须在指定测试目录内。".into());}}
        }
        Ok(locator)
    }
    pub fn resolve(&self)->Result<PathBuf,String> {
        let record:Registry=serde_json::from_value(load(&self.path)?).map_err(|e|e.to_string())?;
        if record.schema!=1 {return Err("工作区定位记录格式不支持。".into());}
        let root=canonical_path(&record.workspace_root)?;
        self.validate_target(&root)?;
        if load(&root.join("portable.json"))?["workspace_id"].as_str()!=Some(&record.workspace_id) {return Err("工作区编号与定位记录不一致。".into());}
        Ok(root)
    }
    pub fn link(&self,root:&Path)->Result<(),String> {
        let root=canonical_path(root)?; let record=load(&root.join("portable.json"))?;
        self.validate_target(&root)?;
        let id=record["workspace_id"].as_str().ok_or("目标目录缺少工作区编号")?;
        atomic_json(&self.path,&json!({"schema":1,"workspace_id":id,"workspace_root":root,
            "fields":{"schema":"定位格式","workspace_id":"工作区编号","workspace_root":"用户所选工作区"}}))
    }
    pub fn child_args(&self)->Vec<String> {
        self.qualification.as_ref().map(|v|vec!["--qualification-config".into(),v.to_string_lossy().into_owned()]).unwrap_or_default()
    }
    pub fn handoff_root(&self,journal:&Path)->Result<PathBuf,String>{
        let record=load(journal)?;
        let target=canonical_path(Path::new(record["target_root"].as_str().ok_or("切换记录缺少目标")?))?;
        self.validate_target(&target)?;
        if canonical_path(journal)?!=canonical_path(&target.join("tmp/workspace-task.json"))?||record["registry_path"].as_str()!=Some(self.path.to_string_lossy().as_ref())||record["state"]!="starting"{return Err("内部工作区切换记录无效。".into());}
        let current=self.resolve()?;
        let source=Path::new(record["source_root"].as_str().ok_or("切换记录缺少来源")?);
        if current!=canonical_path(source)?{return Err("切换来源与当前定位不一致。".into());}
        if record["kind"]=="migrate"&&load(&target.join("portable.json"))?["workspace_id"]!=record["workspace_id"]{return Err("目标工作区编号无效。".into());}
        Ok(target)
    }
    pub fn ensure(&self,cancel:Arc<AtomicBool>,progress:&mut dyn FnMut(&str,u64,u64))->Result<PathBuf,String> {
        let root=match self.resolve() {Ok(root)=>root,Err(_)=>{
            let selection=if let Some(root)=&self.initial_root {Some(root.clone())}else{crate::native_ui::choose_folder("选择语音终端工作区（可使用中文文件夹）")?};
            let root=selection.ok_or("已取消工作区选择。")?;
            self.validate_target(&root)?;
            initialize(&root)?; self.link(&root)?; canonical_path(&root)?
        }};
        recover_install(&root)?;
        recover_base(&root)?;
        self.prepare(&root,cancel,progress)?;
        Ok(root)
    }
    pub fn prepare(&self,root:&Path,cancel:Arc<AtomicBool>,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String> {
        if crate::embedded::available() { return crate::embedded::prepare(root,cancel,progress); }
        let catalog:Catalog=serde_json::from_str(EMBEDDED_CATALOG).map_err(|e|e.to_string())?;
        if catalog.schema!=1{return Err("基础分发清单格式不支持。".into());}
        let base=self.source_base.as_deref().unwrap_or(&catalog.source_base);
        let needed= ["app","python","webview2"];
        for kind in needed {
            let relative=component_root(kind)?;
            let check=match kind {"app"=>"app/VoiceTerminalDesktop.exe","python"=>"runtimes/python/python.exe",_=>"runtimes/webview2/msedgewebview2.exe"};
            let manifest_name=if kind=="app"{"release.json"}else{"manifest.json"};
            if root.join(check).is_file() && root.join(relative).join(manifest_name).is_file(){
                // 大型组件只在安装及迁移时完整校验；启动检查布局与入口，资源库存单独校验。
                continue;
            }
            let package=catalog.packages.iter().find(|v|v.kind==kind).ok_or_else(||format!("分发清单缺少 {kind}（基础组件），请配置固定发布来源后重新组装。"))?;
            let archive=root.join("tmp/downloads").join(format!("{kind}.zip"));
            fs::create_dir_all(archive.parent().unwrap()).map_err(|e|e.to_string())?;
            let required=package.archive.size.saturating_add(package.unpacked_size);
            if available_space(root)?<required {return Err(format!("工作区空间不足，{kind}（基础组件）需要约 {required} 字节。"));}
            let resolve=|file:&DownloadFile|->Result<DownloadFile,String>{
                let mut file=file.clone(); if !file.url.contains("://") {
                    if base.is_empty(){return Err("未配置本体发布来源。".into());}
                    file.url=format!("{}/{}",base.trim_end_matches('/'),file.url.trim_start_matches('/'));
                } Ok(file)
            };
            if package.parts.is_empty(){
                let spec=resolve(&package.archive)?;
                download(&spec,&archive,cancel.clone(),&mut |current,total|progress(&format!("下载 {kind}（基础组件）"),current,total),0)?;
            }else{
                for (index,part) in package.parts.iter().enumerate(){
                    let path=archive.with_extension(format!("zip.part{index:03}"));
                    download(&resolve(part)?,&path,cancel.clone(),&mut |current,total|progress(&format!("下载 {kind}（基础组件）分片 {}",index+1),current,total),0)?;
                }
                let merged=archive.with_extension("merging"); let mut outgoing=fs::File::create(&merged).map_err(|e|e.to_string())?;
                for index in 0..package.parts.len(){
                    if cancel.load(Ordering::Acquire){return Err("任务已取消，分片已保留。".into());}
                    let mut incoming=fs::File::open(archive.with_extension(format!("zip.part{index:03}"))).map_err(|e|e.to_string())?;
                    std::io::copy(&mut incoming,&mut outgoing).map_err(|e|e.to_string())?;
                }
                outgoing.sync_all().map_err(|e|e.to_string())?;drop(outgoing);
                if file_hash(&merged)?!=package.archive.sha256||fs::metadata(&merged).map_err(|e|e.to_string())?.len()!=package.archive.size{return Err("基础包合并摘要不符。".into());}
                // 解包取消后可能留有已校验归档；Windows 重命名不会覆盖已有文件。
                if archive.exists(){remove_owned(&archive,root)?;}
                fs::rename(&merged,&archive).map_err(|e|e.to_string())?;
            }
            progress(&format!("释放 {kind}（基础组件）"),0,package.unpacked_size);
            let stage=root.join("tmp/bootstrap").join(kind);
            remove_owned(&stage,root)?;fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
            unpack(&archive,&stage,relative,package.unpacked_size,cancel.clone(),progress)?;
            let incoming=stage.join(relative);
            verify_tree_progress(&incoming,if kind=="app"{"release.json"}else{"manifest.json"},Some(cancel.clone()),&mut|current,total|progress("校验基础组件",current,total))?;
            if cancel.load(Ordering::Acquire){return Err("基础安装已取消，已下载归档保留以便重试。".into());}
            progress("启用基础组件",1,1);
            let target=root.join(relative);let old=root.join("tmp/bootstrap-old").join(kind);
            remove_owned(&old,root)?;fs::create_dir_all(old.parent().unwrap()).map_err(|e|e.to_string())?;
            let journal=root.join("tmp/bootstrap-transaction.json");
            atomic_json(&journal,&json!({"schema":1,"kind":kind,"state":"prepared","original":target.exists()}))?;
            if target.exists(){fs::rename(&target,&old).map_err(|e|e.to_string())?;}
            fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;
            if let Err(error)=fs::rename(&incoming,&target){if old.exists(){let _=fs::rename(&old,&target);}return Err(error.to_string());}
            atomic_json(&journal,&json!({"schema":1,"kind":kind,"state":"committed","original":old.exists()}))?;
            remove_owned(&old,root)?;remove_owned(&stage,root)?;
            fs::remove_file(journal).map_err(|e|e.to_string())?;
            let _=fs::remove_file(&archive);
            for index in 0..package.parts.len(){let _=fs::remove_file(archive.with_extension(format!("zip.part{index:03}")));}
        }
        Ok(())
    }
}

fn normalized_target(path:&Path)->Result<PathBuf,String>{
    if !path.is_absolute() || path.components().any(|v|matches!(v,std::path::Component::ParentDir)){return Err("路径必须是无上级跳转的绝对路径。".into());}
    let mut parent=path.to_path_buf();let mut missing=Vec::new();
    while !parent.exists(){missing.push(parent.file_name().ok_or("路径无效")?.to_os_string());parent.pop();}
    if reparse(&parent){return Err("工作区路径不能经过外部目录联接。".into());}
    let mut result=canonical_path(&parent)?;
    for name in missing.into_iter().rev(){result.push(name);}Ok(result)
}

pub fn recover_base(root:&Path)->Result<(),String>{
    let journal=root.join("tmp/bootstrap-transaction.json");if !journal.is_file(){return Ok(());}
    let record=load(&journal)?;let kind=record["kind"].as_str().ok_or("基础安装恢复记录无效")?;
    let relative=component_root(kind)?;let target=root.join(relative);let old=root.join("tmp/bootstrap-old").join(kind);
    if record["state"]!="committed"{
        if old.exists(){remove_owned(&target,root)?;fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(&old,&target).map_err(|e|e.to_string())?;}
        else if record["original"]==false{remove_owned(&target,root)?;}
    }
    remove_owned(&old,root)?;remove_owned(&root.join("tmp/bootstrap").join(kind),root)?;
    fs::remove_file(journal).map_err(|e|e.to_string())?;Ok(())
}

const OWNED_ITEMS:&[&str]=&["app","runtimes","models","data","cache","backups","releases","portable.json","VoiceTerminal.exe","Verify-Package.ps1","使用与环境说明.md"];

pub fn prepare_task(source:&Path,target:&Path,kind:&str,parent:u32,locator:&Locator)->Result<PathBuf,String>{
    locator.validate_target(target)?; let source=canonical_path(source)?;
    fs::create_dir_all(target).map_err(|e|e.to_string())?;let target=canonical_path(target)?;
    if source==target||source.starts_with(&target)||target.starts_with(&source){return Err("迁移或切换目标须与当前工作区互不包含。".into());}
    if kind=="migrate"&&fs::read_dir(&target).map_err(|e|e.to_string())?.next().is_some(){return Err("迁移目标必须为空。".into());}
    initialize(&target)?;
    if kind=="migrate"{
        let old=load(&source.join("portable.json"))?;
        atomic_json(&target.join("portable.json"),&old)?;
    }
    let helper=target.join("tmp/workspace-helper.exe");fs::create_dir_all(helper.parent().unwrap()).map_err(|e|e.to_string())?;
    fs::copy(source.join("VoiceTerminal.exe"),&helper).map_err(|e|format!("工作区缺少分发启动入口：{e}"))?;
    let journal=target.join("tmp/workspace-task.json");
    if !["migrate","link"].contains(&kind){return Err("工作区操作类型无效。".into());}
    let record=json!({
        "schema":1,
        "job_id":new_id(),
        "kind":kind,
        "state":"prepared",
        "parent_pid":parent,
        "helper_pid":0,
        "source_root":source,
        "target_root":target,
        "workspace_id":load(&source.join("portable.json"))?["workspace_id"],
        "previous_locator":load(&locator.path).unwrap_or(Value::Null),
        "registry_path":locator.path,
        "fields":{
            "schema":"工作区事务记录格式",
            "job_id":"本次工作区操作的唯一编号",
            "kind":"操作类型：link（链接）或 migrate（迁移）",
            "state":"事务阶段",
            "parent_pid":"提交任务前需要退出的原桌面进程编号",
            "helper_pid":"执行恢复或迁移的辅助进程编号",
            "helper_create_time":"辅助进程创建时间（Windows FILETIME）",
            "helper_executable":"辅助进程可执行文件的完整路径",
            "target_pid":"目标桌面进程编号",
            "target_create_time":"目标桌面进程创建时间（Windows FILETIME）",
            "target_executable":"目标桌面可执行文件的完整路径",
            "source_root":"原工作区根目录",
            "target_root":"新工作区根目录",
            "workspace_id":"迁移前后保持一致的工作区编号",
            "previous_locator":"操作前的工作区定位记录快照",
            "registry_path":"工作区定位记录文件路径",
            "expected_core":"目标工作区验收是否要求核心服务就绪",
            "cleanup_started":"是否已经开始清理原工作区",
            "cleanup_finished":"是否已经完成清理原工作区",
            "fields":"本记录英文字段的中文说明"
        }
    });
    atomic_json(&journal,&record)?;
    if kind=="migrate"{atomic_json(&source.join("data/state/workspace-task.json"),&record)?;}
    Ok(journal)
}

fn wait_parent(pid:u32)->Result<(),String>{
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::{System::Threading::{OpenProcess,WaitForSingleObject},Foundation::CloseHandle};
        let handle=OpenProcess(0x00100000,0,pid);if handle.is_null(){return Ok(());}
        let result=WaitForSingleObject(handle,30000);CloseHandle(handle);
        if result!=0{return Err("原桌面进程尚未退出，迁移保持原位置。".into());}
    }
    Ok(())
}
#[derive(Clone, Copy)]
enum CopyEvent {
    Bytes(u64), // bytes（工作线程已复制并摘要的字节数）。
    WorkerDone, // worker_done（一个复制工作线程已退出）。
}

fn ensure_directory_no_reparse(root:&Path,directory:&Path)->Result<(),String>{
    if reparse(root)||!root.is_dir(){return Err("迁移目标根目录无效或是重解析链接。".into());}
    let relative=directory.strip_prefix(root).map_err(|_|"迁移目标目录越出工作区。")?;
    let mut current=root.to_path_buf();
    for component in relative.components(){
        let std::path::Component::Normal(name)=component else{return Err("迁移目标父目录包含无效路径。".into());};
        current.push(name);
        if reparse(&current){return Err("迁移目标父目录不能经过重解析链接。".into());}
        match fs::create_dir(&current){
            Ok(())=>{},
            Err(error) if error.kind()==std::io::ErrorKind::AlreadyExists=>{},
            Err(error)=>return Err(error.to_string()),
        }
        if reparse(&current)||!current.is_dir(){return Err("迁移目标父目录不是安全目录。".into());}
    }
    Ok(())
}

fn create_copy_staging(target:&Path)->Result<(PathBuf,String),String>{
    let temporary_root=target.join("tmp");
    ensure_directory_no_reparse(target,&temporary_root)?;
    let target_canonical=canonical_path(target)?;
    let temporary_canonical=canonical_path(&temporary_root)?;
    if reparse(&temporary_root)||!temporary_canonical.starts_with(&target_canonical){
        return Err("迁移暂存父目录越出工作区或经过重解析链接。".into());
    }
    for _ in 0..8{
        let token=new_id();
        let name=format!("migration-copy-{token}");
        let staging=safe_path(target,&format!("tmp/{name}"))?;
        match fs::create_dir(&staging){
            Ok(())=>return Ok((staging,token)),
            Err(error) if error.kind()==std::io::ErrorKind::AlreadyExists=>continue,
            Err(error)=>return Err(error.to_string()),
        }
    }
    Err("无法创建本轮独占迁移暂存目录。".into())
}

fn verify_copy_staging(target:&Path,staging:&Path,token:&str)->Result<(),String>{
    let expected_name=format!("migration-copy-{token}");
    if staging.parent()!=Some(target.join("tmp").as_path())||
        staging.file_name().map(|name|name.to_string_lossy().as_ref()!=expected_name).unwrap_or(true)||
        reparse(target)||reparse(&target.join("tmp"))||reparse(staging)||!staging.is_dir(){
        return Err("迁移暂存目录所有权或路径无法验证。".into());
    }
    Ok(())
}

fn cleanup_copy_staging(target:&Path,staging:&Path,token:&str,file_count:usize)->Result<(),String>{
    let expected_name=format!("migration-copy-{token}");
    let temporary_root=target.join("tmp");
    if staging.parent()!=Some(temporary_root.as_path())||
        staging.file_name().map(|name|name.to_string_lossy().as_ref()!=expected_name).unwrap_or(true){
        return Err("迁移暂存目录不属于本轮任务，保留现有内容。".into());
    }
    if reparse(target)||reparse(&temporary_root)||reparse(staging){
        return Err("迁移暂存目录包含重解析链接，保留现有内容。".into());
    }
    if !staging.exists(){return Ok(());}
    let target_canonical=canonical_path(target)?;
    let temporary_canonical=canonical_path(&temporary_root)?;
    let staging_canonical=canonical_path(staging)?;
    if staging_canonical.parent()!=Some(temporary_canonical.as_path())||!staging_canonical.starts_with(&target_canonical){
        return Err("迁移暂存目录规范化路径越出目标工作区，保留现有内容。".into());
    }
    let mut owned_files=Vec::new();
    for entry in fs::read_dir(staging).map_err(|error|error.to_string())?{
        let path=entry.map_err(|error|error.to_string())?.path();
        if reparse(&path)||!path.is_file(){return Err("迁移暂存目录含有未确认项目，保留现有内容。".into());}
        let name=path.file_name().and_then(|value|value.to_str()).ok_or("迁移暂存文件名无法验证。")?;
        let Some(index_text)=name.strip_suffix(".part") else{return Err("迁移暂存目录含有未确认文件，保留现有内容。".into());};
        let index=index_text.parse::<usize>().map_err(|_|"迁移暂存文件编号无效。")?;
        if index>=file_count||format!("{index:08}.part")!=name{return Err("迁移暂存文件不属于本轮索引，保留现有内容。".into());}
        owned_files.push(path);
    }
    for path in owned_files{fs::remove_file(path).map_err(|error|error.to_string())?;}
    fs::remove_dir(staging).map_err(|error|error.to_string())
}

fn record_copy_failure(failure:&Mutex<Option<String>>,stop:&AtomicBool,message:String){
    let mut stored=failure.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    if stored.is_none(){*stored=Some(message);}
    stop.store(true,Ordering::Release);
}

fn is_copy_staging_path(relative:&str)->bool{
    let Some(rest)=relative.strip_prefix("tmp/migration-copy-") else{return false;};
    let token=rest.split('/').next().unwrap_or("");
    let mut parts=token.split('-');
    let time=parts.next().unwrap_or("");
    let process=parts.next().unwrap_or("");
    !time.is_empty()&&!process.is_empty()&&parts.next().is_none()&&
        time.bytes().all(|value|value.is_ascii_hexdigit())&&process.bytes().all(|value|value.is_ascii_hexdigit())
}

fn copy_one_file(index:usize,path:&Path,source:&Path,target:&Path,staging:&Path,token:&str,
    cancel:&AtomicBool,stop:&AtomicBool,buffer:&mut [u8],events:&mpsc::Sender<CopyEvent>)->Result<(),String>{
    if cancel.load(Ordering::Acquire){return Err("迁移已取消，原工作区保留。".into());}
    if stop.load(Ordering::Acquire){return Err("迁移复制已因其他文件失败而停止。".into());}
    verify_copy_staging(target,staging,token)?;
    let relative=path.strip_prefix(source).map_err(|error|error.to_string())?;
    let relative_text=relative.to_string_lossy().replace('\\',"/");
    let destination=safe_path(target,&relative_text)?;
    let parent=destination.parent().ok_or("迁移目标文件缺少父目录。")?;
    ensure_directory_no_reparse(target,parent)?;
    if reparse(&destination){return Err("迁移目标文件不能是重解析链接。".into());}
    let incoming=staging.join(format!("{index:08}.part"));
    if reparse(&incoming){return Err("迁移临时文件不能是重解析链接。".into());}
    let mut input=fs::File::open(path).map_err(|error|error.to_string())?;
    let mut output=fs::OpenOptions::new().write(true).create_new(true).open(&incoming).map_err(|error|error.to_string())?;
    let mut expected=Sha256::new();
    loop{
        if cancel.load(Ordering::Acquire){return Err("迁移已取消，原工作区保留。".into());}
        if stop.load(Ordering::Acquire){return Err("迁移复制已因其他文件失败而停止。".into());}
        let count=input.read(buffer).map_err(|error|error.to_string())?;
        if count==0{break;}
        output.write_all(&buffer[..count]).map_err(|error|error.to_string())?;
        expected.update(&buffer[..count]);
        events.send(CopyEvent::Bytes(count as u64)).map_err(|_|"迁移进度通道已关闭。")?;
    }
    output.sync_all().map_err(|error|error.to_string())?;
    drop(output);
    if cancel.load(Ordering::Acquire){return Err("迁移已取消，原工作区保留。".into());}
    if stop.load(Ordering::Acquire){return Err("迁移复制已因其他文件失败而停止。".into());}
    let expected=format!("{:x}",expected.finalize());
    if file_hash_for_copy(&incoming,buffer,cancel,stop)?!=expected{return Err("迁移文件校验失败。".into());}
    if cancel.load(Ordering::Acquire){return Err("迁移已取消，原工作区保留。".into());}
    if stop.load(Ordering::Acquire){return Err("迁移复制已因其他文件失败而停止。".into());}
    verify_copy_staging(target,staging,token)?;
    let destination=safe_path(target,&relative_text)?;
    let parent=destination.parent().ok_or("迁移目标文件缺少父目录。")?;
    ensure_directory_no_reparse(target,parent)?;
    if reparse(&destination){return Err("迁移目标文件不能是重解析链接。".into());}
    if destination.exists(){remove_owned(&destination,target)?;}
    fs::rename(incoming,destination).map_err(|error|error.to_string())
}

fn copy_files(source:&Path,target:&Path,cancel:Arc<AtomicBool>,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    fn collect(root:&Path,path:&Path,result:&mut Vec<PathBuf>)->Result<(),String>{
        let relative=path.strip_prefix(root).map_err(|e|e.to_string())?.to_string_lossy().replace('\\',"/");
        if relative.starts_with("data/state/application")||relative.starts_with("cache/webview2")||relative.starts_with("tmp/process")||is_copy_staging_path(&relative)||["data/state/startup-result.json","data/state/workspace-ready.json","data/state/workspace-task.json","tmp/workspace-task.json","tmp/workspace-helper.exe"].contains(&relative.as_str()){return Ok(());}
        if relative=="data/dsh/node_modules"&&reparse(path){
            let runtime=root.join("runtimes/dsh");let metadata=load(&runtime.join("runtime.json"))?;
            let modules=metadata["modules"].as_str().ok_or("代理运行清单缺少受管依赖路径。")?;
            let expected=safe_path(&runtime,modules)?;
            if canonical_path(path)?==canonical_path(&expected)?{return Ok(());}
            return Err("代理依赖链接不属于当前运行环境，停止迁移以保留内容。".into());
        }
        if reparse(path){return Err(format!("迁移遇到非受管链接：{relative}"));}
        if path.is_dir(){for entry in fs::read_dir(path).map_err(|e|e.to_string())?{collect(root,&entry.map_err(|e|e.to_string())?.path(),result)?;}}
        else if path.is_file(){result.push(path.to_path_buf());}Ok(())
    }
    let mut paths=vec![];
    for relative in OWNED_ITEMS{let path=source.join(relative);if path.exists(){collect(source,&path,&mut paths)?;}}
    if source.join("tmp").is_dir(){collect(source,&source.join("tmp"),&mut paths)?;}
    let total=paths.iter().try_fold(0u64,|sum,path|fs::metadata(path).map(|v|sum+v.len()).map_err(|e|e.to_string()))?;
    if available_space(target)?<total{return Err("迁移目标空间不足。".into());}
    if paths.is_empty(){return Ok(());}
    if cancel.load(Ordering::Acquire){return Err("迁移已取消，原工作区保留。".into());}
    let (staging,token)=create_copy_staging(target)?;
    let paths=paths;
    let worker_count=paths.len().min(MIGRATION_COPY_WORKERS);
    let next_index=AtomicUsize::new(0);
    let stop=AtomicBool::new(false);
    let failure=Mutex::new(None::<String>);
    let (events_tx,events_rx)=mpsc::channel();
    let mut completed=0u64;
    let mut pending_progress=0u64;
    let mut last_progress=Instant::now();
    thread::scope(|scope|{
        let mut workers=Vec::with_capacity(worker_count);
        for _ in 0..worker_count{
            let events=events_tx.clone();
            let paths=&paths;
            let next_index=&next_index;
            let stop=&stop;
            let failure=&failure;
            let source=source;
            let target=target;
            let staging=&staging;
            let token=&token;
            let cancel=&cancel;
            workers.push(scope.spawn(move||{
                let outcome=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{
                    let mut buffer=vec![0;MIGRATION_COPY_BUFFER];
                    loop{
                        if cancel.load(Ordering::Acquire){
                            record_copy_failure(failure,stop,"迁移已取消，原工作区保留。".into());
                            break;
                        }
                        if stop.load(Ordering::Acquire){break;}
                        let index=next_index.fetch_add(1,Ordering::Relaxed);
                        if index>=paths.len(){break;}
                        if let Err(error)=copy_one_file(index,&paths[index],source,target,staging,token,cancel,stop,&mut buffer,&events){
                            record_copy_failure(failure,stop,error);
                            break;
                        }
                    }
                }));
                if outcome.is_err(){record_copy_failure(failure,stop,"迁移复制工作线程异常退出。".into());}
                let _=events.send(CopyEvent::WorkerDone);
            }));
        }
        drop(events_tx);
        let mut finished=0usize;
        while finished<worker_count{
            if cancel.load(Ordering::Acquire){record_copy_failure(&failure,&stop,"迁移已取消，原工作区保留。".into());}
            match events_rx.recv_timeout(Duration::from_millis(100)){
                Ok(CopyEvent::Bytes(count))=>{
                    completed=completed.saturating_add(count).min(total);
                    pending_progress=pending_progress.saturating_add(count);
                },
                Ok(CopyEvent::WorkerDone)=>finished+=1,
                Err(mpsc::RecvTimeoutError::Timeout)=>{},
                Err(mpsc::RecvTimeoutError::Disconnected)=>break,
            }
            if pending_progress>0&&(pending_progress>=MIGRATION_COPY_BUFFER as u64||last_progress.elapsed()>=Duration::from_millis(100)||finished==worker_count){
                progress("复制并校验工作区",completed,total);
                pending_progress=0;
                last_progress=Instant::now();
            }
        }
        for worker in workers{if worker.join().is_err(){record_copy_failure(&failure,&stop,"迁移复制工作线程异常退出。".into());}}
    });
    if pending_progress>0{progress("复制并校验工作区",completed,total);}
    let result=failure.lock().unwrap_or_else(std::sync::PoisonError::into_inner).clone();
    let cleanup=cleanup_copy_staging(target,&staging,&token,paths.len());
    match (result,cleanup){
        (Some(error),Ok(()))=>Err(error),
        (Some(error),Err(cleanup_error))=>Err(format!("{error}；迁移暂存目录未清理以保留未确认内容：{cleanup_error}")),
        (None,Err(error))=>Err(error),
        (None,Ok(()))=>Ok(()),
    }
}

fn rewrite_managed_paths(source:&Path,target:&Path)->Result<(),String>{
    fn rewrite(value:&mut Value,old:&str,new:&str,key:&str){
        match value{
            Value::Object(values)=>for(key,value)in values{rewrite(value,old,new,key);},
            Value::Array(values)=>for value in values{rewrite(value,old,new,key);},
            Value::String(text) if ["path","root","cwd","workspace","workspace_root","model_dir","home_dir","directory","work_dir","voice_path","dsh_path","manifest_path","source_path","source_ref","archive_path","keywords_file"].contains(&key)=>{
                if text.get(..old.len()).map(|v|v.eq_ignore_ascii_case(old)).unwrap_or(false)&&text.as_bytes().get(old.len()).map(|v|*v==b'/'||*v==b'\\').unwrap_or(true){*text=format!("{new}{}",&text[old.len()..]);}
            },_=>{}
        }
    }
    let old=source.to_string_lossy();let new=target.to_string_lossy();
    for relative in ["data/config/product-settings.json","data/config/model-registry.json","data/config/wake-tasks.json","data/config/continuity-state.json","data/config/continuity-handoff.json","data/history/pending-transfer.json","data/dsh/storages/voice-terminal-wake-tasks.json","data/dsh/session-mappings.json"]{
        let path=target.join(relative);if path.is_file(){let mut value=load(&path)?;rewrite(&mut value,&old,&new,"");atomic_json(&path,&value)?;}
    }Ok(())
}

fn rewrite_extended_paths(source:&Path,target:&Path)->Result<(),String>{
    // 已知 JSON（配置文档）路径由原生白名单重建；不替换用户会话文本。
    rewrite_managed_paths(source,target)
}

fn start_desktop(root:&Path,locator:&Locator)->Result<std::process::Child,String>{start_candidate(root,locator,None)}
fn start_candidate(root:&Path,locator:&Locator,journal:Option<&Path>)->Result<std::process::Child,String>{
    use std::os::windows::process::CommandExt;
    let context=LaunchContext::portable(root)?;
    let mut command=std::process::Command::new(root.join("app/VoiceTerminalDesktop.exe"));
    context.configure_command(&mut command);
    command.args(locator.child_args()).current_dir(root.join("app")).creation_flags(0x08000000);
    if let Some(journal)=journal{command.arg("--workspace-handoff").arg(journal);}
    command.spawn().map_err(|e|format!("目标桌面启动失败：{e}"))
}

// Process identity（进程身份）同时核对创建时间和可执行文件，恢复时不按复用的 PID 杀进程。
#[cfg(windows)]
fn process_identity(pid:u32)->Result<Option<(u64,PathBuf)>,String>{
    use windows_sys::Win32::{Foundation::{CloseHandle,FILETIME},System::Threading::{OpenProcess,GetProcessTimes,QueryFullProcessImageNameW,GetExitCodeProcess}};
    unsafe {
        let handle=OpenProcess(0x1000,0,pid);if handle.is_null(){return Ok(None);}
        let result=(||{
            let mut code=0;if GetExitCodeProcess(handle,&mut code)==0{return Err(std::io::Error::last_os_error().to_string());}if code!=259{return Ok(None);}
            let mut created:FILETIME=std::mem::zeroed();let mut exited:FILETIME=std::mem::zeroed();let mut kernel:FILETIME=std::mem::zeroed();let mut user:FILETIME=std::mem::zeroed();
            if GetProcessTimes(handle,&mut created,&mut exited,&mut kernel,&mut user)==0{return Err(std::io::Error::last_os_error().to_string());}
            let mut name=vec![0u16;32768];let mut count=name.len() as u32;
            if QueryFullProcessImageNameW(handle,0,name.as_mut_ptr(),&mut count)==0{return Err(std::io::Error::last_os_error().to_string());}
            Ok(Some((((created.dwHighDateTime as u64)<<32)|created.dwLowDateTime as u64,PathBuf::from(String::from_utf16_lossy(&name[..count as usize])))))
        })();CloseHandle(handle);result
    }
}
#[cfg(not(windows))]
fn process_identity(_pid:u32)->Result<Option<(u64,PathBuf)>,String>{Ok(None)}

fn recorded_process(record:&Value,prefix:&str)->Result<Option<u32>,String>{
    let pid=record[format!("{prefix}_pid")].as_u64().unwrap_or(0) as u32;if pid==0{return Ok(None);}
    let Some((created,path))=process_identity(pid)? else{return Ok(None);};
    let expected=record[format!("{prefix}_executable")].as_str().ok_or("事务缺少进程文件身份，拒绝操作未知进程。")?;
    let expected_time=record[format!("{prefix}_create_time")].as_u64().ok_or("事务缺少进程创建时间，拒绝操作未知进程。")?;
    if created!=expected_time||!path.to_string_lossy().eq_ignore_ascii_case(expected){return Ok(None);}Ok(Some(pid))
}
fn remember_process(record:&mut Value,prefix:&str,pid:u32)->Result<(),String>{
    let (created,path)=process_identity(pid)?.ok_or("新启动进程已退出。")?;
    record[format!("{prefix}_pid")]=json!(pid);record[format!("{prefix}_create_time")]=json!(created);record[format!("{prefix}_executable")]=json!(path);Ok(())
}
fn stop_recorded_target(record:&Value)->Result<(),String>{
    let Some(pid)=recorded_process(record,"target")? else{return Ok(());};
    #[cfg(windows)] unsafe{
        use windows_sys::Win32::{System::Threading::{OpenProcess,TerminateProcess,WaitForSingleObject},Foundation::CloseHandle};
        let handle=OpenProcess(0x1000|0x00100000|1,0,pid);if handle.is_null(){return Err("无法停止中断事务的目标桌面。".into());}
        // 再次核对身份，避免查询和取得终止句柄之间的 PID 复用。
        if recorded_process(record,"target")?==Some(pid){
            if TerminateProcess(handle,1)==0{CloseHandle(handle);return Err(std::io::Error::last_os_error().to_string());}
            let ended=WaitForSingleObject(handle,30000);CloseHandle(handle);if ended!=0{return Err("中断事务的目标桌面尚未退出。".into());}
        }else{CloseHandle(handle);}
    }Ok(())
}
fn wait_ready(root:&Path,pid:u32,id:&Value,expected_core:bool)->Result<(),String>{
    let deadline=std::time::Instant::now()+Duration::from_secs(300);
    loop{
        if process_identity(pid)?.is_none(){return Err("目标桌面在验收前退出。".into());}
        let ready=load(&root.join("data/state/workspace-ready.json")).ok().map(|v|v["pid"].as_u64()==Some(pid as u64)&&v["workspace_id"]==*id).unwrap_or(false);
        let core=load(&root.join("data/state/startup-result.json")).ok().map(|v|v["core_available"]==true).unwrap_or(false);
        if ready&&(!expected_core||core){return Ok(());}
        if std::time::Instant::now()>=deadline{return Err("目标工作区启动校验超时。".into());}
        std::thread::sleep(Duration::from_millis(150));
    }
}

pub fn run_task(journal:&Path,locator:&Locator,cancel:Arc<AtomicBool>,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    let mut record=load(journal)?;
    if record["schema"]!=1||record["registry_path"].as_str()!=Some(locator.path.to_string_lossy().as_ref()){return Err("迁移记录与当前定位不匹配。".into());}
    if ["completed","failed"].contains(&record["state"].as_str().unwrap_or("")){return Ok(());}
    let source=normalized_target(Path::new(record["source_root"].as_str().ok_or("迁移记录缺少原目录")?))?;
    let target=canonical_path(Path::new(record["target_root"].as_str().ok_or("迁移记录缺少目标目录")?))?;
    if canonical_path(journal)?!=canonical_path(&target.join("tmp/workspace-task.json"))?{return Err("迁移记录位置无效。".into());}
    locator.validate_target(&source)?;locator.validate_target(&target)?;
    if source==target||source.starts_with(&target)||target.starts_with(&source) {return Err("迁移目录互相包含。".into());}
    let migration=record["kind"]=="migrate";
    let committed=record["state"]=="committed";
    if migration&&load(&target.join("portable.json"))?["workspace_id"]!=record["workspace_id"]{return Err("目标工作区编号变化。".into());}
    if (!committed||source.join("portable.json").exists())&&load(&source.join("portable.json"))?["workspace_id"]!=record["workspace_id"]{return Err("原工作区编号变化，停止迁移。".into());}
    remember_process(&mut record,"helper",std::process::id())?;atomic_json(journal,&record)?;
    if !committed{wait_parent(record["parent_pid"].as_u64().unwrap_or(0) as u32)?;}
    let mut target_process:Option<std::process::Child>=None;
    let result=(||{
        if !committed {
            // 中断可能发生在目标已启动或定位已写入之后；先停止已登记的候选，再从原工作区恢复事务。
            stop_recorded_target(&record)?;
            if record["previous_locator"].is_object(){atomic_json(&locator.path,&record["previous_locator"])?;}
            record["state"]=json!("copying");atomic_json(journal,&record)?;
            if migration{copy_files(&source,&target,cancel.clone(),progress)?;rewrite_managed_paths(&source,&target)?;}
            locator.prepare(&target,cancel.clone(),progress)?;
            if migration{rewrite_extended_paths(&source,&target)?;}
            let ready=target.join("data/state/workspace-ready.json");let startup=target.join("data/state/startup-result.json");
            let _=fs::remove_file(&ready);let _=fs::remove_file(&startup);
            record["state"]=json!("starting");atomic_json(journal,&record)?;
            progress("启动目标工作区并校验",0,0);
            target_process=Some(start_candidate(&target,locator,Some(journal))?);
            let process=target_process.as_mut().unwrap();remember_process(&mut record,"target",process.id())?;
            let expected_core=load(&source.join("data/state/startup-result.json")).ok().and_then(|v|v["core_available"].as_bool()).unwrap_or(false);
            record["expected_core"]=json!(migration&&expected_core);atomic_json(journal,&record)?;
            let target_id=load(&target.join("portable.json"))?["workspace_id"].clone();
            wait_ready(&target,process.id(),&target_id,migration&&expected_core)?;
            // 目标就绪之后才切换定位；中断前原定位始终有效。
            locator.link(&target)?;
            record["state"]=json!("committed");atomic_json(journal,&record)?;
        }else{
            // 已切换定位、尚未清理完的事务在重启后必须先恢复目标桌面，再继续清理。
            if locator.resolve()?!=target{return Err("已提交事务的定位记录发生变化，停止清理。".into());}
            let pid=match recorded_process(&record,"target")?{Some(pid)=>pid,None=>{
                let _=fs::remove_file(target.join("data/state/workspace-ready.json"));
                target_process=Some(start_desktop(&target,locator)?);
                let pid=target_process.as_ref().unwrap().id();remember_process(&mut record,"target",pid)?;atomic_json(journal,&record)?;pid
            }};
            let target_id=load(&target.join("portable.json"))?["workspace_id"].clone();
            progress("启动目标工作区并校验",0,0);
            wait_ready(&target,pid,&target_id,record["expected_core"]==true)?;
        }
        if migration{
            progress("目标已启动，清理原工作区",0,0);
            if source.join("portable.json").exists(){
                if load(&source.join("portable.json"))?["workspace_id"]!=record["workspace_id"]{return Err("原工作区编号变化，停止清理。".into());}
                record["cleanup_started"]=json!(true);atomic_json(journal,&record)?;
                for relative in OWNED_ITEMS.iter().filter(|v|**v!="portable.json"){remove_owned(&source.join(relative),&source)?;}
                remove_owned(&source.join("tmp"),&source)?;
                record["cleanup_finished"]=json!(true);atomic_json(journal,&record)?;
                fs::remove_file(source.join("portable.json")).map_err(|e|e.to_string())?;
            }else if record["cleanup_finished"]!=true{return Err("原工作区布局丢失，停止清理并保留剩余内容。".into());}
            if source.is_dir()&&fs::read_dir(&source).map_err(|e|e.to_string())?.next().is_none(){fs::remove_dir(&source).map_err(|e|e.to_string())?;}
        }
        record["state"]=json!("completed");atomic_json(journal,&record)?;Ok(())
    })();
    if result.is_err()&&record["state"]!="committed" {
        if let Some(process)=target_process.as_mut(){let _=process.kill();let _=process.wait();}
        if record["previous_locator"].is_object(){let _=atomic_json(&locator.path,&record["previous_locator"]);}
        // 中断后的再次启动以原位置为权威；完整副本保留用于重新尝试，避免删除未知用户文件。
        record["state"]=json!("failed");let _=atomic_json(journal,&record);
        let _=start_desktop(&source,locator);
    }result
}

pub fn pending_task(root:&Path)->Result<Option<PathBuf>,String>{
    for relative in ["tmp/workspace-task.json","data/state/workspace-task.json"]{
        let marker=root.join(relative);
        if !marker.is_file(){continue;}
        let reference=load(&marker)?;
        let target=Path::new(reference["target_root"].as_str().ok_or("迁移恢复记录缺少目标")?);
        let journal=target.join("tmp/workspace-task.json");
        let record=load(&journal)?;
        if ["completed","failed"].contains(&record["state"].as_str().unwrap_or("")){continue;}
        if recorded_process(&record,"helper")?.is_some_and(|pid|pid!=std::process::id()){return Err("工作区事务正在运行，请等待完成。".into());}
        return Ok(Some(journal));
    }
    Ok(None)
}

#[cfg(test)]
mod migration_copy_tests{
    use super::*;

    #[test]
    fn parallel_copy_commits_verified_files_and_reports_progress_on_caller_thread(){
        let root=std::env::temp_dir().join(format!("voice-parallel-copy-{}",new_id()));
        let source=root.join("source");
        let target=root.join("target");
        fs::create_dir_all(source.join("app/config")).unwrap();
        fs::create_dir_all(source.join("tmp/migration-copy-123abc-7")).unwrap();
        fs::create_dir_all(target.join("tmp")).unwrap();
        fs::write(source.join("tmp/migration-copy-123abc-7/00000000.part"),b"stale staging data").unwrap();
        let mut expected=Vec::new();
        for index in 0..32{
            let path=source.join(format!("app/config/{index:03}.bin"));
            let contents=(0..8192).map(|byte|((byte*31+index*97)&255) as u8).collect::<Vec<_>>();
            fs::write(&path,&contents).unwrap();
            expected.push((path.strip_prefix(&source).unwrap().to_path_buf(),contents));
        }
        for (name,contents) in [("same.wav",b"wave payload".as_slice()),("same.mp3",b"mpeg payload".as_slice())]{
            let path=source.join("app/config").join(name);
            fs::write(&path,contents).unwrap();
            expected.push((path.strip_prefix(&source).unwrap().to_path_buf(),contents.to_vec()));
        }
        let caller=thread::current().id();
        let mut progress_calls=0usize;
        let mut progress=|phase:&str,current:u64,total:u64|{
            assert_eq!(thread::current().id(),caller);
            assert_eq!(phase,"复制并校验工作区");
            assert!(current<=total);
            progress_calls+=1;
        };

        copy_files(&source,&target,Arc::new(AtomicBool::new(false)),&mut progress).unwrap();

        assert!(progress_calls>0);
        for (relative,contents) in expected{
            assert_eq!(fs::read(target.join(relative)).unwrap(),contents);
        }
        assert!(!target.join("tmp/migration-copy-123abc-7").exists());
        assert!(fs::read_dir(target.join("tmp")).unwrap().all(|entry|{
            !entry.unwrap().file_name().to_string_lossy().starts_with("migration-copy-")
        }));
        fs::remove_dir_all(root).unwrap();
    }
}

pub fn initialize(root:&Path)->Result<(),String> {
    if root.as_os_str().to_string_lossy().starts_with("\\\\"){return Err("工作区须位于本地磁盘。".into());}
    if reparse(root){return Err("工作区根目录不能是联接。".into());}
    fs::create_dir_all(root).map_err(|e|e.to_string())?;
    if root.join("portable.json").exists(){
        let mut value=load(&root.join("portable.json"))?;
        if value["schema"]!=2||value["app"]!="app"||value["data_dir"]!="data"{return Err("目标不是兼容工作区。".into());}
        if value["workspace_id"].as_str().is_none(){value["workspace_id"]=json!(new_id());atomic_json(&root.join("portable.json"),&value)?;}
        return Ok(());
    }
    if fs::read_dir(root).map_err(|e|e.to_string())?.next().is_some(){return Err("新工作区必须为空；已有工作区须包含兼容布局配置。".into());}
    for relative in ["data/state","data/config","models","cache/logs","tmp/process","backups","releases"]{
        fs::create_dir_all(root.join(relative)).map_err(|e|e.to_string())?;
    }
    atomic_json(&root.join("portable.json"),&json!({"schema":2,"workspace_id":new_id(),"app":"app","python":"runtimes/python/python.exe","models":"models","dsh":"runtimes/dsh","webview2":null,"data_dir":"data",
        "fields":{"schema":"固定布局格式","workspace_id":"工作区编号","app":"程序","python":"后端解释器","models":"模型","dsh":"代理宿主","webview2":"网页引擎","data_dir":"用户数据"}}))
}
pub fn new_id()->String {format!("{:x}-{:x}",SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos(),std::process::id())}
fn component_root(kind:&str)->Result<&'static str,String>{match kind{"app"=>Ok("app"),"python"=>Ok("runtimes/python"),"webview2"=>Ok("runtimes/webview2"),_=>Err("未知基础组件。".into())}}

pub fn available_space(root:&Path)->Result<u64,String>{
    #[cfg(windows)] {
        use std::os::windows::ffi::OsStrExt;
        unsafe extern "system" {fn GetDiskFreeSpaceExW(path:*const u16,available:*mut u64,total:*mut u64,free:*mut u64)->i32;}
        let name:Vec<_>=root.as_os_str().encode_wide().chain(Some(0)).collect();let mut available=0;
        if unsafe{GetDiskFreeSpaceExW(name.as_ptr(),&mut available,std::ptr::null_mut(),std::ptr::null_mut())}==0{return Err(std::io::Error::last_os_error().to_string());}Ok(available)
    }
    #[cfg(not(windows))] {let _=root;Ok(u64::MAX)}
}
pub fn remove_owned(path:&Path,root:&Path)->Result<(),String>{
    if !path.starts_with(root)||path==root{return Err("清理路径越出工作区。".into());}
    if reparse(path){return fs::remove_dir(path).map_err(|e|e.to_string());}
    if path.is_dir(){
        for entry in fs::read_dir(path).map_err(|e|e.to_string())?{remove_owned(&entry.map_err(|e|e.to_string())?.path(),root)?;}
        fs::remove_dir(path).map_err(|e|e.to_string())?;
    }else if path.exists(){fs::remove_file(path).map_err(|e|e.to_string())?;}
    Ok(())
}
pub fn verify_tree(root:&Path,manifest_name:&str)->Result<(),String>{
    verify_tree_progress(root,manifest_name,None,&mut|_,_|{})
}
fn verify_tree_progress(root:&Path,manifest_name:&str,cancel:Option<Arc<AtomicBool>>,progress:&mut dyn FnMut(u64,u64))->Result<(),String>{
    let value=load(&root.join(manifest_name))?;
    let records=value["files"].as_array().ok_or("组件清单缺少文件集合")?;
    let total=records.iter().try_fold(0u64,|sum,item|sum.checked_add(item["size"].as_u64().ok_or("清单大小无效")?).ok_or("清单大小溢出"))?;
    let mut completed=0u64;
    progress(completed,total);
    let mut expected=std::collections::HashSet::new();
    for item in records{
        if cancel.as_ref().is_some_and(|value|value.load(Ordering::Acquire)){return Err("已取消基础组件校验，已下载归档保留。".into());}
        let relative=item["path"].as_str().ok_or("清单路径无效")?;
        if !expected.insert(relative.to_lowercase()){return Err("组件清单包含重复路径。".into());}
        let path=safe_path(root,item["path"].as_str().ok_or("清单路径无效")?)?;
        if fs::metadata(&path).map_err(|e|e.to_string())?.len()!=item["size"].as_u64().ok_or("清单大小无效")?||
            file_hash(&path)?!=item["sha256"].as_str().ok_or("清单摘要无效")?{return Err(format!("组件文件校验失败：{}",path.display()));}
        completed+=item["size"].as_u64().unwrap();progress(completed,total);
    }
    fn actual(root:&Path,path:&Path,out:&mut std::collections::HashSet<String>)->Result<(),String>{
        for entry in fs::read_dir(path).map_err(|e|e.to_string())?{
            let path=entry.map_err(|e|e.to_string())?.path();if reparse(&path){return Err("组件含链接。".into());}
            if path.is_dir(){actual(root,&path,out)?;}else{let relative=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/").to_lowercase();if !["manifest.json","release.json"].contains(&relative.as_str()){out.insert(relative);}}
        }Ok(())
    }
    let mut present=std::collections::HashSet::new();actual(root,root,&mut present)?;
    if expected!=present{return Err("组件文件集合与发布清单不一致。".into());}Ok(())
}
fn unpack(archive:&Path,target:&Path,prefix:&str,total:u64,cancel:Arc<AtomicBool>,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    let mut source=zip::ZipArchive::new(fs::File::open(archive).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    let mut seen=std::collections::HashSet::new();let mut completed=0u64;
    for index in 0..source.len(){
        if cancel.load(Ordering::Acquire){return Err("已取消解包。".into());}
        let mut entry=source.by_index(index).map_err(|e|e.to_string())?;
        let relative=entry.name().trim_end_matches('/').to_string();
        if relative!=prefix&&!relative.starts_with(&format!("{prefix}/")){return Err("基础归档包含组件目录之外的文件。".into());}
        if !seen.insert(relative.to_lowercase())||entry.unix_mode().map(|v|v&0o170000==0o120000).unwrap_or(false){return Err("归档包含重复路径或链接。".into());}
        let destination=safe_path(target,&relative)?;
        if entry.is_dir(){fs::create_dir_all(&destination).map_err(|e|e.to_string())?;}else{
            fs::create_dir_all(destination.parent().unwrap()).map_err(|e|e.to_string())?;
            let mut outgoing=fs::OpenOptions::new().write(true).create_new(true).open(&destination).map_err(|e|e.to_string())?;
            let mut buffer=vec![0;1024*1024];loop{if cancel.load(Ordering::Acquire){return Err("已取消解包，未启用暂存内容。".into());}let count=entry.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}outgoing.write_all(&buffer[..count]).map_err(|e|e.to_string())?;completed+=count as u64;progress("释放基础组件",completed,total);}
        }
        progress("释放基础组件",completed,total);
    }Ok(())
}

pub fn recover_install(root:&Path)->Result<(),String>{
    let journal=root.join("tmp/update-transaction.json");if !journal.is_file(){return Ok(());}
    let record=load(&journal)?;let committed=record["state"]=="committed";
    let rows=record["items"].as_array().ok_or("更新恢复记录无效")?;
    for row in rows {
        let relative=row["path"].as_str().ok_or("恢复路径缺失")?;
        if !(relative=="app"||relative.starts_with("models/")||relative.starts_with("runtimes/")||["models","portable.json","VoiceTerminal.exe","app/release.json","models/manifest.json"].contains(&relative)) {return Err("更新恢复路径不在受管范围。".into());}
        safe_path(root,relative)?;
    }
    if !committed {for row in rows.iter().rev(){
        let relative=row["path"].as_str().unwrap();let target=root.join(relative);let old=root.join("tmp/publish/old").join(relative);let incoming=root.join("tmp/publish/new").join(relative);
        if old.exists(){remove_owned(&target,root)?;fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(old,target).map_err(|e|e.to_string())?;}
        else if row["original"]==false&&target.exists()&&!incoming.exists(){remove_owned(&target,root)?;}
    }}
    remove_owned(&root.join("tmp/publish"),root)?;fs::remove_file(journal).map_err(|e|e.to_string())?;Ok(())
}
