//! Workspace（工作区）：本地身份、已发布本体初始化与可恢复迁移。
use std::{fs, io::{Read, Write}, path::{Path, PathBuf}, sync::{mpsc, Arc, Mutex, atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering}}, thread, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use crate::{canonical_path, LaunchContext};

const MIGRATION_COPY_WORKERS: usize = 4; // MIGRATION_COPY_WORKERS（迁移文件复制的有界工作线程数）。
const MIGRATION_COPY_BUFFER: usize = 1024 * 1024; // MIGRATION_COPY_BUFFER（每个复制线程复用的缓冲区字节数）。
const VALIDATION_WORKERS: usize = 4; // VALIDATION_WORKERS（组件摘要校验的有界线程数）。
const VALIDATION_BUFFER: usize = 1024 * 1024; // VALIDATION_BUFFER（每个校验线程复用的读取缓冲区）。

#[derive(Clone, Debug)]
pub struct Locator {
    pub local_root: Option<PathBuf>, // local_root（直接工作区）：无需外部定位记录。
    pub path: PathBuf, // path（本地事务身份；历史事务兼容原标识，不读取定位文件）。
    pub source_base: Option<String>, // source_base（验收发布源覆盖）。
    pub instance_tag: String, // instance_tag（隔离实例标识）。
    pub initial_root: Option<PathBuf>, // initial_root（仅验收配置允许的自动目录选择）。
    pub headless: bool, // headless（验收无窗口入口）。
    pub qualification: Option<PathBuf>, // qualification（验收配置位置）。
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
    let path=relative_path(relative)?;
    let mut current=root.to_path_buf();
    for part in path.components() {
        current.push(part.as_os_str()); if reparse(&current){return Err("受管路径不能经过目录联接。".into());}
    } Ok(current)
}
// relative_path（清单相对路径）：集中保留路径规则，目录遍历另行检查每个重解析点。
pub(crate) fn relative_path(relative:&str)->Result<&Path,String>{
    let path=Path::new(relative);
    if relative.is_empty()||relative.contains(':')||relative.contains('\\')||path.is_absolute()||
        path.components().any(|part|!matches!(part,std::path::Component::Normal(_))) {return Err("资源路径越界。".into());}
    for part in path.components() {
        let name=part.as_os_str().to_string_lossy(); let stem=name.split('.').next().unwrap_or("").to_ascii_lowercase();
        if name.ends_with(' ')||name.ends_with('.')||["con","prn","aux","nul"].contains(&stem.as_str())||
            (stem.len()==4&&(stem.starts_with("com")||stem.starts_with("lpt"))&&stem.as_bytes()[3].is_ascii_digit()) {return Err("资源路径包含系统保留名称。".into());}
    } Ok(path)
}

impl Locator {
    pub fn local(root:&Path)->Self {
        Self{local_root:Some(root.to_path_buf()),path:root.join("tmp/workspace-identity.json"),
            source_base:None,instance_tag:"product".into(),initial_root:Some(root.to_path_buf()),headless:false,qualification:None}
    }
    pub fn for_task(journal:&Path)->Result<Self,String>{
        let record=load(journal)?;
        let source=Path::new(record["source_root"].as_str().ok_or("工作区事务缺少原位置")?);
        let mut local=Self::local(source);
        // 旧事务只保留身份匹配，禁止再读取或写入系统定位文件。
        if let Some(path)=record["registry_path"].as_str(){local.path=PathBuf::from(path);}
        Ok(local)
    }
    pub fn validate_target(&self,root:&Path)->Result<(),String>{
        if !root.is_absolute(){return Err("工作区必须使用绝对路径。".into());}
        let root = normalized_target(root)?;
        if self.local_root.is_none()&&root.file_name().is_some_and(|v|v==".local")&&root.parent().is_some_and(|parent|parent.join("apps/desktop/Cargo.toml").is_file()){
            return Err("分发工作区不能复用开发仓库的 .local（本机资源）。".into());
        }
        if self.qualification.is_some(){
            let sandbox=canonical_path(Path::new("D:/项目开发/测试"))?;
            if !root.starts_with(&sandbox) || root == sandbox {return Err("验收工作区操作必须限定在测试目录的独立子目录内。".into());}
        }Ok(())
    }
    // 历史外部工具不再使用系统定位；明确拒绝，要求正式入口或工作区事务。
    pub fn from_args(_args:&[String])->Result<Self,String>{Err("工作区由正式入口位置确定；不再接受系统定位或分发渠道参数。".into())}
    pub fn resolve(&self)->Result<PathBuf,String>{canonical_path(self.local_root.as_deref().ok_or("正式程序缺少本地工作区身份")?)}
    pub fn link(&self,root:&Path)->Result<(),String>{self.validate_target(root)?;normalize_layout(root)}
    pub fn child_args(&self)->Vec<String> {
        self.qualification.as_ref().map(|v|vec!["--qualification-config".into(),v.to_string_lossy().into_owned()]).unwrap_or_default()
    }
    pub fn handoff_root(&self,journal:&Path)->Result<PathBuf,String>{
        let record=load(journal)?;
        let target=canonical_path(Path::new(record["target_root"].as_str().ok_or("切换记录缺少目标")?))?;
        self.validate_target(&target)?;
        if canonical_path(journal)?!=canonical_path(&target.join("tmp/workspace-task.json"))?||record["registry_path"].as_str()!=Some(self.path.to_string_lossy().as_ref())||!["starting","committed"].contains(&record["state"].as_str().unwrap_or("")){return Err("内部工作区切换记录无效。".into());}
        let current=normalized_target(self.local_root.as_deref().ok_or("事务缺少本地工作区身份")?)?;
        let source=Path::new(record["source_root"].as_str().ok_or("切换记录缺少来源")?);
        if current!=normalized_target(source)?{return Err("切换来源与当前事务身份不一致。".into());}
        if record["kind"]=="migrate"&&load(&target.join("portable.json"))?["workspace_id"]!=record["workspace_id"]{return Err("目标工作区编号无效。".into());}
        Ok(target)
    }
    pub fn prepare(&self,root:&Path,cancel:Arc<AtomicBool>,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
        if root.join("VoiceTerminal.exe").is_file()&&root.join("app/VoiceTerminalDesktop.exe").is_file(){return Ok(());}
        crate::installation::copy_program(self.local_root.as_deref().ok_or("缺少当前已发布本体")?,root,cancel,progress)
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
    crate::installation::ensure_idle(&target)?;
    initialize(&target)?;
    if kind=="migrate"{
        let old=load(&source.join("portable.json"))?;
        atomic_json(&target.join("portable.json"),&old)?;
    }
    let helper=target.join("tmp/workspace-helper.exe");fs::create_dir_all(helper.parent().unwrap()).map_err(|e|e.to_string())?;
    fs::copy(source.join("VoiceTerminal.exe"),&helper).map_err(|e|format!("工作区缺少正式启动入口：{e}"))?;
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
        "previous_locator":Value::Null,
        "registry_path":locator.path,
        "fields":{
            "schema":"工作区事务记录格式",
            "job_id":"本次工作区操作的唯一编号",
            "kind":"操作类型：link（切换）或 migrate（迁移）",
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
            "previous_locator":"历史兼容字段；新事务不读取系统定位记录",
            "registry_path":"本地事务身份；历史事务保留原标识",
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
    command.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    command.args(locator.child_args()).current_dir(root.join("app")).creation_flags(0x08000000);
    if let Some(journal)=journal{command.arg("--workspace-handoff").arg(journal);}
    command.spawn().map_err(|e|format!("目标桌面启动失败：{e}"))
}

// Process identity（进程身份）同时核对创建时间和可执行文件，恢复时不按复用的 PID 杀进程。
#[cfg(windows)]
pub fn process_identity(pid:u32)->Result<Option<(u64,PathBuf)>,String>{
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
pub fn process_identity(_pid:u32)->Result<Option<(u64,PathBuf)>,String>{Ok(None)}

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
        let ready=load(&root.join("data/state/workspace-ready.json")).ok().map(|v|v["pid"].as_u64()==Some(pid as u64)&&v["workspace_id"]==*id&&v["ready"]==true).unwrap_or(false);
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
            let expected_core=false; // 只认可当前进程的真实管理页或产品页，不读取上次模型启动结果。
            record["expected_core"]=json!(migration&&expected_core);atomic_json(journal,&record)?;
            let target_id=load(&target.join("portable.json"))?["workspace_id"].clone();
            wait_ready(&target,process.id(),&target_id,migration&&expected_core)?;
            // 目标真实页面就绪后更新快捷方式；不更新系统定位记录。
            locator.link(&target)?;
            crate::shortcuts::create(&target,if migration{Some(source.as_path())}else{None})?;
            record["state"]=json!("committed");atomic_json(journal,&record)?;
        }else{
            // 已提交、尚未清理完的事务先恢复目标桌面，再继续清理。
            let pid=match recorded_process(&record,"target")?{Some(pid)=>pid,None=>{
                let _=fs::remove_file(target.join("data/state/workspace-ready.json"));
                target_process=Some(start_candidate(&target,locator,Some(journal))?);
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
        record["state"]=json!("completed");atomic_json(journal,&record)?;
        // 切换后原工作区仍可直接启动；目标随后搬迁也不能留下失效的定位引用。
        let marker=source.join("data/state/workspace-task.json");
        if load(&marker).ok().is_some_and(|reference|reference["job_id"]==record["job_id"]){fs::remove_file(marker).map_err(|e|e.to_string())?;}
        Ok(())
    })();
    if result.is_err()&&record["state"]!="committed" {
        if let Some(process)=target_process.as_mut(){let _=process.kill();let _=process.wait();}
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
        // 终态记录不再定位旧目标；整体文件夹搬迁后，历史路径可以不存在。
        if ["completed","failed"].contains(&reference["state"].as_str().unwrap_or("")){continue;}
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
    fn terminal_task_records_allow_direct_folder_relocation(){
        let base=std::env::temp_dir().join(format!("voice-relocated-{}",new_id()));let root=base.join("relocated workspace");initialize(&root).unwrap();
        for state in ["completed","failed"]{
            atomic_json(&root.join("tmp/workspace-task.json"),&json!({"state":state,"target_root":base.join("old missing path")})).unwrap();
            assert!(pending_task(&root).unwrap().is_none());
        }
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn switch_preparation_uses_formal_helper_and_never_creates_external_locator(){
        let base=std::env::temp_dir().join(format!("voice-workspace-{}",new_id()));let source=base.join("source");let target=base.join("target");
        initialize(&source).unwrap();fs::write(source.join("VoiceTerminal.exe"),b"formal entry").unwrap();
        let locator=Locator::local(&source);let journal=prepare_task(&source,&target,"link",0,&locator).unwrap();let record=load(&journal).unwrap();
        assert_eq!(fs::read(target.join("tmp/workspace-helper.exe")).unwrap(),b"formal entry");assert_eq!(record["registry_path"],json!(locator.path));assert!(record["previous_locator"].is_null());assert!(!locator.path.exists());
        assert_ne!(load(&source.join("portable.json")).unwrap()["workspace_id"],load(&target.join("portable.json")).unwrap()["workspace_id"]);fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn committed_handoff_remains_valid_after_source_cleanup(){
        let base=std::env::temp_dir().join(format!("voice-recovery-{}",new_id()));let source=base.join("removed source");let target=base.join("target");initialize(&target).unwrap();
        let id=load(&target.join("portable.json")).unwrap()["workspace_id"].clone();let locator=Locator::local(&source);let journal=target.join("tmp/workspace-task.json");
        atomic_json(&journal,&json!({"schema":1,"kind":"migrate","state":"committed","source_root":source,"target_root":target,"workspace_id":id,"registry_path":locator.path})).unwrap();
        assert_eq!(Locator::for_task(&journal).unwrap().handoff_root(&journal).unwrap(),canonical_path(&target).unwrap());fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn migration_preparation_preserves_identity_and_rejects_nonempty_target(){
        let base=std::env::temp_dir().join(format!("voice-migrate-{}",new_id()));let source=base.join("source");let target=base.join("target");initialize(&source).unwrap();fs::write(source.join("VoiceTerminal.exe"),b"formal entry").unwrap();
        let locator=Locator::local(&source);prepare_task(&source,&target,"migrate",0,&locator).unwrap();assert_eq!(load(&source.join("portable.json")).unwrap()["workspace_id"],load(&target.join("portable.json")).unwrap()["workspace_id"]);
        let occupied=base.join("occupied");fs::create_dir_all(&occupied).unwrap();fs::write(occupied.join("user.txt"),b"user data").unwrap();assert!(prepare_task(&source,&occupied,"migrate",0,&locator).is_err());assert_eq!(fs::read(occupied.join("user.txt")).unwrap(),b"user data");fs::remove_dir_all(base).unwrap();
    }

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

pub fn initialize(root:&Path)->Result<(),String> { initialize_layout(root,false) }
// initialize_guarded（持锁初始化）：仅允许本轮创建的空暂存区与互斥文件。
pub fn initialize_guarded(root:&Path,_guard:&crate::installation::WorkspaceGuard)->Result<(),String>{initialize_layout(root,true)}
fn initialize_layout(root:&Path,guarded:bool)->Result<(),String> {
    if root.as_os_str().to_string_lossy().starts_with("\\\\"){return Err("工作区须位于本地磁盘。".into());}
    if reparse(root){return Err("工作区根目录不能是联接。".into());}
    fs::create_dir_all(root).map_err(|e|e.to_string())?;
    if root.join("portable.json").exists(){
        return normalize_layout(root);
    }
    for entry in fs::read_dir(root).map_err(|e|e.to_string())?{
        let path=entry.map_err(|e|e.to_string())?.path();
        if !guarded||path!=root.join("tmp")||reparse(&path)||!path.is_dir(){return Err("新工作区必须为空；已有工作区须包含兼容布局配置。".into());}
        for item in fs::read_dir(path).map_err(|e|e.to_string())?{
            let item=item.map_err(|e|e.to_string())?.path();
            if item!=root.join("tmp/program.lock")||reparse(&item)||!item.is_file()||fs::metadata(item).map_err(|e|e.to_string())?.len()!=0{return Err("新工作区暂存区含有未确认内容。".into());}
        }
    }
    for relative in ["data/state","data/config","models","cache/logs","tmp/process","backups","releases"]{
        fs::create_dir_all(root.join(relative)).map_err(|e|e.to_string())?;
    }
    atomic_json(&root.join("portable.json"),&json!({"schema":2,"workspace_id":new_id(),"app":"app","python":"runtimes/python/python.exe","models":"models","dsh":"runtimes/dsh","webview2":null,"data_dir":"data",
        "fields":{"schema":"固定布局格式","workspace_id":"工作区编号","app":"程序","python":"后端解释器","models":"模型","dsh":"代理宿主","webview2":"网页引擎","data_dir":"用户数据"}}))
}
// normalize_layout（原位兼容转换）：保留编号、用户内容和旧私有引擎文件。
pub fn normalize_layout(root:&Path)->Result<(),String>{
    let path=safe_path(root,"portable.json")?;
    let mut value=load(&path)?;
    if value["schema"]!=2||value["app"]!="app"||value["data_dir"]!="data"||
       value["python"]!="runtimes/python/python.exe"||value["models"]!="models"||value["dsh"]!="runtimes/dsh"||
       !(value["webview2"].is_null()||value["webview2"]=="runtimes/webview2"){
        return Err("目标不是第二版固定布局工作区。".into());
    }
    let original=value.clone();
    if value["workspace_id"].as_str().is_none_or(|id|id.is_empty()){value["workspace_id"]=json!(new_id());}
    value["webview2"]=Value::Null;
    if !value["fields"].is_object(){value["fields"]=json!({});}
    value["fields"]["webview2"]=json!("使用系统 WebView2（网页运行引擎），旧私有文件保留");
    value["fields"]["workspace_id"]=json!("迁移及重装时保留的工作区编号");
    if value!=original{atomic_json(&path,&value)?;}Ok(())
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
    verify_tree_with_progress(root,manifest_name,&AtomicBool::new(false),&mut|_,_,_|{})
}
#[cfg(test)] // legacy_verify_tree（只读性能对照）：正式程序不包含旧的串行实现。
fn legacy_verify_tree(root:&Path,manifest_name:&str,cancel:Option<Arc<AtomicBool>>,progress:&mut dyn FnMut(u64,u64))->Result<(),String>{
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
fn validation_cancel(cancel:&AtomicBool)->Result<(),String>{
    if cancel.load(Ordering::Acquire){Err("组件校验已取消，原组件保持不变。".into())}else{Ok(())}
}
struct ValidationFile {
    path: PathBuf, // path（已经过目录链接和文件集合检查的实际文件）。
    size: u64, // size（清单中的完整文件字节数）。
    sha256: String, // sha256（必须逐字节匹配的文件摘要）。
}
pub(crate) fn metadata_reparse(metadata:&fs::Metadata)->bool{
    #[cfg(windows)] {use std::os::windows::fs::MetadataExt;metadata.file_attributes()&0x400!=0}
    #[cfg(not(windows))] {metadata.file_type().is_symlink()}
}
// collect_validation_files（单次目录核对）：每个目录和文件检查一次，避免重复查询共同父目录。
fn collect_validation_files(root:&Path,path:&Path,files:&mut std::collections::HashMap<String,(PathBuf,u64)>,
    cancel:&AtomicBool,total:usize,last_report:&mut Instant,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    for entry in fs::read_dir(path).map_err(|e|e.to_string())?{
        validation_cancel(cancel)?;
        let entry=entry.map_err(|e|e.to_string())?;let path=entry.path();
        let metadata=entry.metadata().map_err(|e|e.to_string())?;
        if metadata_reparse(&metadata){return Err("组件含链接。".into());}
        let relative=path.strip_prefix(root).map_err(|e|e.to_string())?.to_string_lossy().replace('\\',"/");
        relative_path(&relative)?;
        if metadata.is_dir(){collect_validation_files(root,&path,files,cancel,total,last_report,progress)?;}
        else if metadata.is_file(){
            let key=relative.to_lowercase();
            if !["manifest.json","release.json"].contains(&key.as_str())&&files.insert(key,(path,metadata.len())).is_some(){
                return Err("组件包含大小写冲突路径。".into());
            }
        }else{return Err("组件含非普通文件。".into());}
        if last_report.elapsed()>=Duration::from_millis(100){
            progress(&format!("核对组件文件清单（{} / {total} 个文件）",files.len()),0,0);*last_report=Instant::now();
        }
    }Ok(())
}
fn hash_validation_file(item:&ValidationFile,buffer:&mut [u8],cancel:&AtomicBool,stop:&AtomicBool,completed:&AtomicU64)->Result<String,String>{
    validation_cancel(cancel)?;
    let mut options=fs::OpenOptions::new();options.read(true);
    #[cfg(windows)] {use std::os::windows::fs::OpenOptionsExt;options.custom_flags(0x00200000);} // FILE_FLAG_OPEN_REPARSE_POINT（不跟随文件链接）。
    let mut stream=options.open(&item.path).map_err(|e|e.to_string())?;
    let metadata=stream.metadata().map_err(|e|e.to_string())?;
    if metadata_reparse(&metadata)||!metadata.is_file()||metadata.len()!=item.size{return Err(format!("组件文件校验失败：{}",item.path.display()));}
    let mut hash=Sha256::new();let mut read_bytes=0u64;
    loop{
        validation_cancel(cancel)?;
        if stop.load(Ordering::Acquire){return Ok(String::new());}
        let count=stream.read(buffer).map_err(|e|e.to_string())?;
        if count==0{break;}
        hash.update(&buffer[..count]);read_bytes+=count as u64;completed.fetch_add(count as u64,Ordering::Relaxed);
    }
    let digest=format!("{:x}",hash.finalize());
    if read_bytes!=item.size||(!item.sha256.is_empty()&&digest!=item.sha256){return Err(format!("组件文件校验失败：{}",item.path.display()));}
    Ok(digest)
}
/// verify_tree_with_progress（完整组件校验）：清单、文件集合及全部摘要均检查，取消与进度在调用线程处理。
pub fn verify_tree_with_progress(root:&Path,manifest_name:&str,cancel:&AtomicBool,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    validation_cancel(cancel)?;progress("读取组件文件清单",0,0);validation_cancel(cancel)?;
    if metadata_reparse(&fs::symlink_metadata(root).map_err(|e|e.to_string())?){return Err("组件目录不能是链接。".into());}
    let value=load(&safe_path(root,manifest_name)?)?;
    let records=value["files"].as_array().ok_or("组件清单缺少文件集合")?;
    verify_records_with_progress(root,records,true,cancel,progress)
}

/// verify_records_with_progress（固定文件内容校验）：模型允许清单外辅助文件，运行环境要求完整文件集合。
pub fn verify_records_with_progress(root:&Path,records:&[Value],exact:bool,cancel:&AtomicBool,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    let files=prepare_validation_files(root,records,exact,cancel,progress)?;
    hash_validation_files(&files,cancel,progress).map(|_|())
}

/// verify_tree_sizes（快速盘点）：检查文件集合、路径和字节数，不读取文件内容。
pub fn verify_tree_sizes(root:&Path,manifest_name:&str)->Result<(),String>{
    verify_tree_sizes_with_progress(root,manifest_name,&AtomicBool::new(false),&mut|_,_,_|{})
}
/// verify_tree_sizes_with_progress（快速组件盘点）：检查文件集合与大小，不读取文件内容。
pub fn verify_tree_sizes_with_progress(root:&Path,manifest_name:&str,cancel:&AtomicBool,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    validation_cancel(cancel)?;
    if metadata_reparse(&fs::symlink_metadata(root).map_err(|e|e.to_string())?){return Err("组件目录不能是链接。".into());}
    progress("核对组件文件清单",0,0);validation_cancel(cancel)?;
    let value=load(&safe_path(root,manifest_name)?)?;
    prepare_validation_files(root,value["files"].as_array().ok_or("组件清单缺少文件集合")?,true,cancel,progress).map(|_|())
}

fn prepare_validation_files(root:&Path,records:&[Value],exact:bool,cancel:&AtomicBool,progress:&mut dyn FnMut(&str,u64,u64))->Result<Vec<ValidationFile>,String>{
    validation_cancel(cancel)?;
    if metadata_reparse(&fs::symlink_metadata(root).map_err(|e|e.to_string())?){return Err("组件目录不能是链接。".into());}
    let mut expected=std::collections::HashSet::new();
    for item in records{
        validation_cancel(cancel)?;
        let relative=item["path"].as_str().ok_or("清单路径无效")?;relative_path(relative)?;
        if !expected.insert(relative.to_lowercase()){return Err("组件清单包含重复路径。".into());}
    }
    let mut present=std::collections::HashMap::new();let mut last_report=Instant::now();
    progress("核对组件文件清单",0,0);
    collect_validation_files(root,root,&mut present,cancel,records.len(),&mut last_report,progress)?;
    if (exact&&expected.len()!=present.len())||!expected.iter().all(|path|present.contains_key(path)){return Err("组件文件集合与发布清单不一致。".into());}
    let mut files=Vec::with_capacity(records.len());
    for item in records{
        validation_cancel(cancel)?;
        let (path,size)=present.remove(&item["path"].as_str().unwrap().to_lowercase()).unwrap();
        if size!=item["size"].as_u64().unwrap(){return Err(format!("组件文件校验失败：{}",path.display()));}
        let sha256=item["sha256"].as_str().ok_or("清单摘要无效")?;
        if sha256.len()!=64||!sha256.bytes().all(|byte|byte.is_ascii_hexdigit()){return Err("清单摘要无效".into());}
        files.push(ValidationFile{path,size,sha256:sha256.to_lowercase()});
    }
    Ok(files)
}

/// manifest_with_progress（生成完整文件摘要清单）：一次遍历、有界并行读取，并在调用线程报告进度。
pub fn manifest_with_progress(root:&Path,kind:&str,build:&str,cancel:&AtomicBool,progress:&mut dyn FnMut(&str,u64,u64))->Result<Value,String>{
    validation_cancel(cancel)?;
    if metadata_reparse(&fs::symlink_metadata(root).map_err(|e|e.to_string())?){return Err("组件目录不能是链接。".into());}
    let mut present=std::collections::HashMap::new();let mut last_report=Instant::now();
    progress("扫描组件文件清单",0,0);
    collect_validation_files(root,root,&mut present,cancel,0,&mut last_report,progress)?;
    let mut files:Vec<_>=present.into_values().map(|(path,size)|ValidationFile{path,size,sha256:String::new()}).collect();
    files.sort_by(|a,b|a.path.cmp(&b.path));
    let hashes=hash_validation_files(&files,cancel,progress)?;
    let records=files.iter().zip(hashes).map(|(file,sha256)|json!({"path":file.path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/"),"size":file.size,"sha256":sha256})).collect::<Vec<_>>();
    Ok(json!({"schema":1,"kind":kind,"build":build,"files":records,"fields":{"schema":"清单格式","kind":"资源类别","build":"版本标识","files":"文件列表","path":"相对路径","size":"字节数","sha256":"文件摘要"}}))
}

fn hash_validation_files(files:&[ValidationFile],cancel:&AtomicBool,progress:&mut dyn FnMut(&str,u64,u64))->Result<Vec<String>,String>{
    let total=files.iter().try_fold(0u64,|sum,item|sum.checked_add(item.size).ok_or("清单大小溢出"))?;
    let hashes=Mutex::new(vec![String::new();files.len()]);let mut last_report=Instant::now();
    let completed=AtomicU64::new(0);let checked=AtomicUsize::new(0);let next=AtomicUsize::new(0);let stop=AtomicBool::new(false);
    let failure=Mutex::new(None::<String>);let worker_count=files.len().min(VALIDATION_WORKERS);
    let (sender,receiver)=mpsc::channel();progress("校验组件文件内容",0,total);validation_cancel(cancel)?;
    thread::scope(|scope|{
        let mut workers=vec![];
        for _ in 0..worker_count{
            let sender=sender.clone();let next=&next;let stop=&stop;let completed=&completed;let checked=&checked;let failure=&failure;let hashes=&hashes;
            workers.push(scope.spawn(move||{
                let outcome=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{
                    let mut buffer=vec![0;VALIDATION_BUFFER];
                    loop{
                        validation_cancel(cancel)?;if stop.load(Ordering::Acquire){break;}
                        let index=next.fetch_add(1,Ordering::Relaxed);if index>=files.len(){break;}
                        let digest=hash_validation_file(&files[index],&mut buffer,cancel,stop,completed)?;
                        hashes.lock().map_err(|_|"组件摘要状态不可用。")?[index]=digest;checked.fetch_add(1,Ordering::Relaxed);
                    }Ok::<(),String>(())
                })).unwrap_or_else(|_|Err("组件校验工作线程异常退出。".into()));
                if let Err(error)=outcome{let mut record=failure.lock().unwrap();if record.is_none(){*record=Some(error);}stop.store(true,Ordering::Release);}
                let _=sender.send(());
            }));
        }
        drop(sender);let mut finished=0;
        while finished<worker_count{
            match receiver.recv_timeout(Duration::from_millis(100)){
                Ok(())=>finished+=1,
                Err(mpsc::RecvTimeoutError::Timeout)=>{},
                Err(mpsc::RecvTimeoutError::Disconnected)=>break,
            }
            if last_report.elapsed()>=Duration::from_millis(100)&&!stop.load(Ordering::Acquire){
                progress(&format!("校验组件文件内容（{} / {} 个文件）",checked.load(Ordering::Relaxed),files.len()),completed.load(Ordering::Relaxed).min(total),total);
                last_report=Instant::now();
            }
        }
        for worker in workers{if worker.join().is_err(){*failure.lock().unwrap()=Some("组件校验工作线程异常退出。".into());}}
    });
    validation_cancel(cancel)?;
    if let Some(error)=failure.into_inner().map_err(|_|"组件校验状态不可用。")?{return Err(error);}
    progress("组件文件校验完成",total,total);validation_cancel(cancel)?;
    hashes.into_inner().map_err(|_|"组件摘要状态不可用。".into())
}
pub fn recover_install(root:&Path)->Result<(),String>{
    let journal=root.join("tmp/update-transaction.json");if !journal.is_file(){return Ok(());}
    let record=load(&journal)?;let committed=record["state"]=="committed";
    let rows=record["items"].as_array().ok_or("更新恢复记录无效")?;
    for row in rows {
        let relative=row["path"].as_str().ok_or("恢复路径缺失")?;
        if !(relative=="app"||relative.starts_with("models/")||relative.starts_with("runtimes/")||["models","portable.json","VoiceTerminal.exe","VoiceTerminalUIDemo.exe","Verify-Package.ps1","使用与环境说明.md","app/release.json","models/manifest.json"].contains(&relative)) {return Err("更新恢复路径不在受管范围。".into());}
        safe_path(root,relative)?;
    }
    if !committed {for row in rows.iter().rev(){
        let relative=row["path"].as_str().unwrap();let target=root.join(relative);let old=root.join("tmp/publish/old").join(relative);let incoming=root.join("tmp/publish/new").join(relative);
        if old.exists(){remove_owned(&target,root)?;fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(old,target).map_err(|e|e.to_string())?;}
        else if row["original"]==false&&target.exists()&&!incoming.exists(){remove_owned(&target,root)?;}
    }}
    remove_owned(&root.join("tmp/publish"),root)?;fs::remove_file(journal).map_err(|e|e.to_string())?;Ok(())
}

/// file_hash_with_progress（单文件摘要）：大文件的每个读取块均响应取消。
pub fn file_hash_with_progress(path:&Path,cancel:&AtomicBool,progress:&mut dyn FnMut(&str,u64,u64))->Result<String,String>{
    let size=fs::metadata(path).map_err(|e|e.to_string())?.len();
    hash_validation_files(&[ValidationFile{path:path.to_path_buf(),size,sha256:String::new()}],cancel,progress).map(|mut hashes|hashes.remove(0))
}

#[cfg(test)]
mod component_validation_tests {
    use super::*;
    fn fixture()->PathBuf{
        let root=std::env::temp_dir().join(format!("voice-validation-{}",new_id()));
        fs::create_dir_all(root.join("中文 目录/nested")).unwrap();
        for index in 0..32{fs::write(root.join(format!("中文 目录/nested/{index:03}.bin")),vec![index as u8;4096]).unwrap();}
        fs::write(root.join("empty.bin"),b"").unwrap();
        atomic_json(&root.join("manifest.json"),&crate::resources::manifest(&root,"fixture","1").unwrap()).unwrap();root
    }
    #[test]
    fn complete_hashes_report_progress_on_caller_and_accept_empty_unicode_files(){
        let root=fixture();let caller=thread::current().id();let mut bytes=vec![];let mut phases=vec![];
        verify_tree_with_progress(&root,"manifest.json",&AtomicBool::new(false),&mut|phase,done,total|{
            assert_eq!(thread::current().id(),caller);assert!(done<=total);if total>0{bytes.push(done);}phases.push(phase.to_string());
        }).unwrap();
        assert_eq!(bytes.first(),Some(&0));assert_eq!(bytes.last(),Some(&(32*4096)));
        assert!(bytes.windows(2).all(|pair|pair[0]<=pair[1]));assert_eq!(phases.last().unwrap(),"组件文件校验完成");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn detects_same_size_corruption_missing_and_extra_files(){
        let root=fixture();let target=root.join("中文 目录/nested/004.bin");
        fs::write(&target,vec![99;4096]).unwrap();assert!(verify_tree(&root,"manifest.json").unwrap_err().contains("校验失败"));
        fs::write(&target,vec![4;4096]).unwrap();fs::write(root.join("extra.bin"),b"extra").unwrap();
        assert!(verify_tree(&root,"manifest.json").unwrap_err().contains("文件集合"));fs::remove_file(root.join("extra.bin")).unwrap();
        fs::remove_file(&target).unwrap();assert!(verify_tree(&root,"manifest.json").unwrap_err().contains("文件集合"));fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn rejects_duplicates_traversal_and_reserved_names(){
        let root=fixture();let original=load(&root.join("manifest.json")).unwrap();
        let mut value=original.clone();let duplicate=value["files"][0].clone();value["files"].as_array_mut().unwrap().push(duplicate);
        atomic_json(&root.join("manifest.json"),&value).unwrap();assert!(verify_tree(&root,"manifest.json").unwrap_err().contains("重复"));
        for name in ["../escape.bin","C:/escape.bin","中文 目录\\escape.bin","NUL.bin"]{
            let mut value=original.clone();value["files"][0]["path"]=json!(name);atomic_json(&root.join("manifest.json"),&value).unwrap();
            assert!(verify_tree(&root,"manifest.json").is_err(),"应拒绝 {name}（非法路径）");
        }fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn cancellation_during_inventory_and_before_hash_preserves_all_files(){
        let root=fixture();let original=file_hash(&root.join("manifest.json")).unwrap();
        for target in ["读取组件文件清单","核对组件文件清单","校验组件文件内容"]{
            let cancel=AtomicBool::new(false);
            let error=verify_tree_with_progress(&root,"manifest.json",&cancel,&mut|phase,_,_|{
                if phase==target{cancel.store(true,Ordering::Release);}
            }).unwrap_err();assert!(error.contains("取消"));assert_eq!(file_hash(&root.join("manifest.json")).unwrap(),original);
        }verify_tree(&root,"manifest.json").unwrap();fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn cancellation_interrupts_reading_inside_large_file(){
        let root=fixture();let path=root.join("large.bin");fs::write(&path,vec![7;32*1024*1024]).unwrap();
        let item=ValidationFile{path:path.clone(),size:32*1024*1024,sha256:file_hash(&path).unwrap()};
        let cancel=AtomicBool::new(false);let completed=AtomicU64::new(0);let stopped=AtomicBool::new(false);
        thread::scope(|scope|{
            let handle=scope.spawn(||{
                let mut buffer=vec![0;VALIDATION_BUFFER];hash_validation_file(&item,&mut buffer,&cancel,&stopped,&completed)
            });
            while completed.load(Ordering::Acquire)==0&&!handle.is_finished(){thread::yield_now();}
            cancel.store(true,Ordering::Release);assert!(handle.join().unwrap().unwrap_err().contains("取消"));
        });
        assert!(completed.load(Ordering::Acquire)<item.size);assert_eq!(file_hash(&path).unwrap(),item.sha256);fs::remove_dir_all(root).unwrap();
    }
    #[test]
    #[cfg(windows)]
    fn rejects_junction_without_touching_external_directory(){
        use std::os::windows::process::CommandExt;
        let root=fixture();let external=fixture();let linked=root.join("linked");
        let status=std::process::Command::new("cmd.exe").args(["/D","/C","mklink","/J"])
            .arg(&linked).arg(&external).creation_flags(0x08000000).stdout(std::process::Stdio::null()).status().unwrap();
        assert!(status.success());assert!(verify_tree(&root,"manifest.json").unwrap_err().contains("链接"));
        assert!(external.join("manifest.json").is_file());fs::remove_dir(&linked).unwrap();fs::remove_dir_all(root).unwrap();fs::remove_dir_all(external).unwrap();
    }
    #[test]
    #[ignore="手动只读验收：对指定真实组件目录测量完整摘要校验与取消延迟"]
    fn benchmark_existing_component_read_only(){
        let root=PathBuf::from(std::env::var_os("VOICE_VALIDATION_TEST_ROOT").expect("需要 VOICE_VALIDATION_TEST_ROOT（只读组件目录）"));
        let report=PathBuf::from(std::env::var_os("VOICE_VALIDATION_REPORT").expect("需要 VOICE_VALIDATION_REPORT（测试报告路径）"));
        let input=load(&root.join("manifest.json")).unwrap();let count=input["files"].as_array().unwrap().len();
        let total=input["files"].as_array().unwrap().iter().map(|row|row["size"].as_u64().unwrap()).sum::<u64>();
        let original=file_hash(&root.join("manifest.json")).unwrap();let mut runs=vec![];
        // 两轮反转执行顺序；旧实现最多运行 30 秒，完整结果与截断结果分别记录。
        for (round,order) in [(1,["legacy","optimized"]),(2,["optimized","legacy"])]{
            for mode in order{
                let started=Instant::now();let mut bytes=0;let mut calls=0;let mut phase=String::new();let mut report_clock=Instant::now();
                let signal=Arc::new(AtomicBool::new(false));
                let result=if mode=="legacy"{
                    legacy_verify_tree(&root,"manifest.json",Some(signal.clone()),&mut|done,_|{
                        bytes=done;calls+=1;if started.elapsed()>=Duration::from_secs(30){signal.store(true,Ordering::Release);}
                    })
                }else{
                    verify_tree_with_progress(&root,"manifest.json",&signal,&mut|current,done,total|{
                        bytes=done;calls+=1;
                        if phase!=current&&report_clock.elapsed()>=Duration::from_secs(5){println!("第 {round} 轮 {mode}（校验模式）：{current}，{done} / {total} 字节");report_clock=Instant::now();}phase=current.into();
                    })
                };
                let elapsed=started.elapsed().as_secs_f64();println!("第 {round} 轮 {mode}（校验模式）：{elapsed:.3} 秒，{bytes} / {total} 字节，完整完成={}",result.is_ok());
                if mode=="optimized"{assert!(result.is_ok(),"优化校验失败：{result:?}");}
                else if result.is_err(){assert!(signal.load(Ordering::Acquire),"旧校验异常：{result:?}");}
                runs.push(json!({"round":round,"mode":mode,"seconds":elapsed,"complete":result.is_ok(),"verified_bytes":bytes,"progress_calls":calls,"error":result.err()}));
            }
        }
        let cancel=AtomicBool::new(false);let started=Instant::now();let mut requested=None;
        let result=verify_tree_with_progress(&root,"manifest.json",&cancel,&mut|phase,_,_|{
            if phase.starts_with("校验组件文件内容（")&&requested.is_none(){requested=Some(Instant::now());cancel.store(true,Ordering::Release);}
        });assert!(result.unwrap_err().contains("取消"));assert!(requested.is_some());
        let cancel_latency=requested.unwrap().elapsed().as_secs_f64();assert!(cancel_latency<2.0);assert_eq!(file_hash(&root.join("manifest.json")).unwrap(),original);
        let cancelled_test_seconds=started.elapsed().as_secs_f64();
        let mut runtime_cancel_verified=false;
        if root.file_name().is_some_and(|name|name=="python")&&root.parent().is_some_and(|path|path.file_name().is_some_and(|name|name=="runtimes")){
            let workspace=root.parent().unwrap().parent().unwrap();let context=LaunchContext::portable(workspace).unwrap();
            let signal=Arc::new(AtomicBool::new(false));let mut activated=false;let mut phases=vec![];
            let result=crate::runtime::configure_with_activation(&context,"all",signal.clone(),&mut|phase,_,_|{
                phases.push(phase.to_string());if phase=="校验现有语音环境：校验组件文件内容"{signal.store(true,Ordering::Release);}
            },&mut|_,_|{activated=true;Ok(())});
            assert!(result.unwrap_err().contains("取消"));assert!(!activated);assert!(phases.iter().all(|phase|!phase.contains("下载")));
            assert_eq!(file_hash(&root.join("manifest.json")).unwrap(),original);runtime_cancel_verified=true;
        }
        atomic_json(&report,&json!({"success":true,"files":count,"total_bytes":total,"runs":runs,"cancellation_seconds":cancel_latency,"cancelled_test_seconds":cancelled_test_seconds,"runtime_cancel_verified":runtime_cancel_verified,"manifest_preserved":true,
            "fields":{"success":"两轮完整校验及取消验收通过","files":"实际组件文件数","total_bytes":"清单文件总字节数","runs":"两轮反转顺序的旧串行与优化校验","round":"测量轮次","mode":"legacy（旧串行）或 optimized（优化校验）","seconds":"该轮耗时秒数","complete":"是否完成全部内容及文件集合校验","verified_bytes":"该轮校验完的字节数；旧实现可能因30秒预算截断","progress_calls":"进度回调次数","error":"截断或校验错误","cancellation_seconds":"发出取消后到返回的秒数","cancelled_test_seconds":"含目录扫描在内的取消测试总秒数","runtime_cancel_verified":"真实配置入口取消后没有下载或启用环境","manifest_preserved":"原组件清单摘要不变"}})).unwrap();
    }
}
