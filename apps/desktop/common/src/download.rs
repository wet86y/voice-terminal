//! Native download（原生下载）：静态 C ABI（C 调用接口）与固定摘要。
use std::{ffi::{CString, c_void}, path::{Path,PathBuf}, sync::{mpsc,Arc,Mutex,Condvar, atomic::{AtomicBool,AtomicUsize, Ordering}},time::Duration};
use serde::{Serialize, Deserialize};
use base64::Engine;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DownloadFile {
    pub url: String, // url（固定原始来源地址）。
    pub size: u64, // size（预期字节数）。
    #[serde(default)] pub sha256: String, // sha256（十六进制摘要）。
    #[serde(default)] pub sha512: String, // sha512（十六进制或 Base64 编码摘要）。
    #[serde(default)] pub path: String, // path（安装相对文件名）。
}

type Callback = unsafe extern "C" fn(*mut c_void, i32, u64, u64) -> i32;
type CancelCheck=unsafe extern "C" fn(*const c_void)->i32; // CancelCheck（独立原子信号读取接口）。
unsafe extern "C" fn cancel_check(context:*const c_void)->i32{
    i32::from(unsafe{&*(context as *const AtomicBool)}.load(Ordering::Acquire))
}
const MAX_DOWNLOADS:usize=4; // MAX_DOWNLOADS（全局并行传输上限）：所有组件共享四个下载通道。
std::thread_local!{static RESOURCE_ATTEMPT:std::cell::Cell<Option<u32>>=const{std::cell::Cell::new(None)};}
/// current_attempt（资源处理轮次）：统一来源回退与任务恢复，避免嵌套重试。
pub fn current_attempt()->Option<u32>{RESOURCE_ATTEMPT.with(|value|value.get())}
pub fn with_attempt<T>(attempt:Option<u32>,operation:impl FnOnce()->T)->T{
    struct Restore(Option<u32>);
    impl Drop for Restore{fn drop(&mut self){RESOURCE_ATTEMPT.with(|value|value.set(self.0));}}
    let _restore=Restore(RESOURCE_ATTEMPT.with(|value|value.replace(attempt)));operation()
}
pub const BATCH_DOWNLOAD_PROGRESS_PREFIX:&str="批量下载进度："; // BATCH_DOWNLOAD_PROGRESS_PREFIX（批量下载阶段标记）。
pub const BATCH_VERIFY_PROGRESS_PREFIX:&str="批量校验进度："; // BATCH_VERIFY_PROGRESS_PREFIX（批量校验阶段标记）。
#[derive(PartialEq)]
enum DownloadErrorKind { Source, Local, Cancelled, Permanent } // DownloadErrorKind（来源、本地、取消及不可恢复错误）。
struct DownloadError { kind:DownloadErrorKind,message:String } // DownloadError（分类传输错误）。
impl From<String> for DownloadError{fn from(message:String)->Self{Self{kind:DownloadErrorKind::Permanent,message}}}
impl From<&str> for DownloadError{fn from(message:&str)->Self{message.to_owned().into()}}

/// target_lock（目标互斥）：跨进程协调同一路径，等待锁不占网络通道，也不污染模型目录。
fn target_lock(target:&Path,cancel:&AtomicBool,callback:&mut dyn FnMut(&str,u64,u64))->Result<std::fs::File,String>{
    use sha2::{Digest,Sha256};
    let absolute=std::path::absolute(target).map_err(|_|"下载目标路径无效")?;
    let directory=std::env::temp_dir().join("voice-terminal-download-locks");
    std::fs::create_dir_all(&directory).map_err(|_|"下载协调目录不可写")?;
    let key=format!("{:x}",Sha256::digest(absolute.to_string_lossy().replace('\\',"/").to_lowercase().as_bytes()));
    loop{
        if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
        let mut options=std::fs::OpenOptions::new();options.read(true).write(true).create(true).truncate(false);
        #[cfg(windows)] {use std::os::windows::fs::OpenOptionsExt;options.share_mode(0);}
        match options.open(directory.join(&key)){
            Ok(file)=>return Ok(file),
            Err(error) if matches!(error.raw_os_error(),Some(32|33))=>{callback("等待同一下载文件释放",0,0);std::thread::sleep(Duration::from_millis(100));},
            Err(_)=>return Err("下载协调文件不可写，请检查目录权限。".into()),
        }
    }
}
static DOWNLOAD_SLOTS:Mutex<usize>=Mutex::new(0);
static DOWNLOAD_CHANGED:Condvar=Condvar::new();
pub(crate) struct DownloadSlot; // DownloadSlot（下载通道租约）：失败和取消也归还通道。
impl Drop for DownloadSlot{fn drop(&mut self){if let Ok(mut count)=DOWNLOAD_SLOTS.lock(){*count=count.saturating_sub(1);DOWNLOAD_CHANGED.notify_one();}}}
pub(crate) fn acquire_slot(cancel:&AtomicBool,callback:&mut dyn FnMut(&str,u64,u64))->Result<DownloadSlot,String>{
    let mut count=DOWNLOAD_SLOTS.lock().map_err(|_|"下载通道状态不可用。")?;
    while *count>=MAX_DOWNLOADS{
        if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
        drop(count);callback("等待下载通道",0,0);
        count=DOWNLOAD_SLOTS.lock().map_err(|_|"下载通道状态不可用。")?;
        if *count>=MAX_DOWNLOADS{count=DOWNLOAD_CHANGED.wait_timeout(count,Duration::from_millis(100)).map_err(|_|"下载通道状态不可用。")?.0;}
    }
    if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
    *count+=1;Ok(DownloadSlot)
}
#[cfg(windows)]
unsafe extern "C" {
    fn vt_download(url: *const i8, target: *const u16, size: u64, expected: *const i8,
        sha512: i32, probe_limit: u64, callback: Callback, context: *mut c_void,
        cancel_check:CancelCheck,cancel_context:*const c_void,error: *mut i8, error_size: usize) -> i32;
}
struct Progress<'a> {
    cancel: Arc<AtomicBool>, // cancel（取消信号）。
    callback: &'a mut dyn FnMut(&str,u64, u64), // callback（阶段与字节进度回调）。
}
unsafe extern "C" fn progress(context: *mut c_void, phase:i32, current: u64, total: u64) -> i32 {
    let state = unsafe { &mut *(context as *mut Progress<'_>) };
    (state.callback)(match phase{2=>"校验下载文件",3=>"提交已校验下载缓存",4=>"校验现有下载缓存",_=>"下载固定资源"},current, total);
    i32::from(state.cancel.load(Ordering::Acquire))
}

pub fn download(file: &DownloadFile, target: &Path, cancel: Arc<AtomicBool>,
    callback: &mut dyn FnMut(u64, u64), probe_limit: u64) -> Result<u64, String> {
    download_with_progress(file,target,cancel,&mut|_,done,total|callback(done,total),probe_limit)
}
/// download_with_progress（分阶段下载）：网络、缓存完整摘要和启用阶段均有进度与取消。
pub fn download_with_progress(file:&DownloadFile,target:&Path,cancel:Arc<AtomicBool>,callback:&mut dyn FnMut(&str,u64,u64),probe_limit:u64)->Result<u64,String>{
    let _target=target_lock(target,&cancel,callback)?;
    let _slot=acquire_slot(&cancel,callback)?;
    download_candidates(file,target,cancel,callback,probe_limit,&crate::download_sources::candidates(&file.url))
}
/// download_candidates（有序回退）：大小与摘要决定断点身份，来源失败不改写内容锁。
fn download_candidates(file:&DownloadFile,target:&Path,cancel:Arc<AtomicBool>,callback:&mut dyn FnMut(&str,u64,u64),probe_limit:u64,sources:&[String])->Result<u64,String>{
    let mut errors=vec![];
    let selected=current_attempt().map(|attempt|attempt.saturating_sub(1) as usize%sources.len().max(1));
    for (index,source) in sources.iter().enumerate() {
        if selected.is_some_and(|selected|selected!=index){continue;}
        if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
        let host=crate::download_sources::host(source);
        callback(&format!("下载来源：{host}"),0,file.size);
        let mut candidate=file.clone();candidate.url=source.clone();
        match download_one(&candidate,target,cancel.clone(),callback,probe_limit) {
            Ok(value)=>return Ok(value),
            Err(error)=>{
                if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
                if error.kind!=DownloadErrorKind::Source{return Err(error.message);}
                callback(&format!("来源 {host} 失败，{}：{}",if selected.is_some(){"本轮结束，等待任务恢复"}else{"尝试备用来源"},error.message),0,file.size);
                errors.push(format!("{host}：{}",error.message));
            }
        }
    }
    Err(format!("{}：{}",if selected.is_some(){"本轮下载来源失败"}else{"所有下载来源均失败"},errors.join("；")))
}
fn download_one(file:&DownloadFile,target:&Path,cancel:Arc<AtomicBool>,callback:&mut dyn FnMut(&str,u64,u64),probe_limit:u64)->Result<u64,DownloadError>{
    if !(file.url.starts_with("https://") || (cfg!(feature="qualification") && file.url.starts_with("http://127.0.0.1:"))) {
        return Err("资源来源必须为 HTTPS（加密传输），验收配置允许本机测试源。".into());
    }
    let (expected, algorithm) = if !file.sha512.is_empty() {
        let hash = if file.sha512.len() == 128 { file.sha512.to_lowercase() } else {
            base64::engine::general_purpose::STANDARD.decode(&file.sha512)
                .map_err(|_| "SHA-512（文件摘要）编码无效")?.iter().map(|v| format!("{v:02x}")).collect()
        }; (hash, 1)
    } else { (file.sha256.to_lowercase(), 0) };
    if probe_limit == 0 && (expected.len() != if algorithm == 1 {128} else {64} || !expected.bytes().all(|v| v.is_ascii_hexdigit())) {
        return Err("资源缺少可信文件摘要。".into());
    }
    let url = CString::new(file.url.as_str()).map_err(|_| "来源地址无效")?;
    let hash = CString::new(expected).map_err(|_| "摘要无效")?;
    #[cfg(windows)] {
        use std::os::windows::ffi::OsStrExt;
        let destination: Vec<_> = target.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut message = [0i8; 1024];
        let mut state = Progress {cancel, callback};
        let result = unsafe { vt_download(url.as_ptr(), destination.as_ptr(), file.size, hash.as_ptr(), algorithm,
            probe_limit.min(65536), progress, &mut state as *mut _ as *mut c_void, cancel_check, Arc::as_ptr(&state.cancel) as *const c_void, message.as_mut_ptr(), message.len()) };
        if result < 0 {
            let bytes: Vec<_> = message.iter().take_while(|v| **v != 0).map(|v| *v as u8).collect();
            return Err(DownloadError{kind:match result{-2=>DownloadErrorKind::Local,-3=>DownloadErrorKind::Cancelled,-4=>DownloadErrorKind::Permanent,_=>DownloadErrorKind::Source},message:String::from_utf8_lossy(&bytes).to_string()});
        }
        Ok(result as u64)
    }
    #[cfg(not(windows))] { let _=(target,cancel,callback,url,hash); Err("原生下载仅支持 Windows（操作系统）。".into()) }
}

/// download_batch（有界批量下载）：依赖与模型文件共用全局通道，回调只在调用线程执行。
pub fn download_batch(files:&[(DownloadFile,PathBuf)],cancel:Arc<AtomicBool>,callback:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    let attempt=current_attempt();
    let total=files.iter().try_fold(0u64,|sum,(file,_)|sum.checked_add(file.size).ok_or("下载大小溢出"))?;
    #[derive(Clone,Default)]
    struct FileProgress { downloaded:u64,verified:u64,transferred:bool,cached:bool } // FileProgress（单文件阶段计数）。
    let next=AtomicUsize::new(0);let checked=AtomicUsize::new(0);let stop=AtomicBool::new(false);
    let states=Mutex::new(vec![FileProgress::default();files.len()]);
    let source_status=Mutex::new(String::new()); // source_status（实际来源及切换原因）。
    let failure=Mutex::new(None::<String>);let (sender,receiver)=mpsc::channel();let count=files.len().min(MAX_DOWNLOADS);
    let transfer_cancel=Arc::new(AtomicBool::new(cancel.load(Ordering::Acquire)));
    let mut verification_started=false; // verification_started（整体已进入校验）：重下时明确说明重试。
    callback("准备固定资源下载清单",0,0);
    std::thread::scope(|scope|{
        let mut workers=vec![];
        for _ in 0..count{
            let sender=sender.clone();let next=&next;let checked=&checked;let stop=&stop;let failure=&failure;
            let cancel=cancel.clone();let transfer_cancel=transfer_cancel.clone();let states=&states;let source_status=&source_status;
            workers.push(scope.spawn(move||{
                let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||with_attempt(attempt,||{
                    loop{
                        if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
                        if stop.load(Ordering::Acquire){break;}
                        let index=next.fetch_add(1,Ordering::Relaxed);let Some((file,target))=files.get(index)else{break;};
                        download_with_progress(file,target,transfer_cancel.clone(),&mut|phase,current,_|{
                            if phase.starts_with("下载来源：")||phase.starts_with("来源 "){
                                if let Ok(mut status)=source_status.lock(){*status=phase.to_owned();}return;
                            }
                            if let Ok(mut rows)=states.lock(){let row=&mut rows[index];match phase{
                                "校验现有下载缓存"=>{row.cached=true;row.transferred=true;row.verified=current.min(file.size);},
                                "校验下载文件"=>{row.transferred=true;row.downloaded=file.size;row.verified=current.min(file.size);},
                                "提交已校验下载缓存"=>{row.transferred=true;row.verified=file.size;},
                                "下载固定资源"=>{row.cached=false;row.downloaded=current.min(file.size);row.transferred=current>=file.size;row.verified=0;},
                                _=>{},
                            }}
                        },0)?;
                        if let Ok(mut rows)=states.lock(){rows[index].transferred=true;rows[index].verified=file.size;}
                        checked.fetch_add(1,Ordering::Relaxed);
                    }Ok::<(),String>(())
                }))).unwrap_or_else(|_|Err("下载工作线程异常退出，断点已保留。".into()));
                if let Err(error)=result{if let Ok(mut failure)=failure.lock(){if failure.is_none(){*failure=Some(error);}}stop.store(true,Ordering::Release);}
                let _=sender.send(());
            }));
        }
        drop(sender);let mut finished=0;
        while finished<count{
            match receiver.recv_timeout(Duration::from_millis(100)){Ok(())=>finished+=1,Err(mpsc::RecvTimeoutError::Timeout)=>{},Err(mpsc::RecvTimeoutError::Disconnected)=>break}
            if cancel.load(Ordering::Acquire)||stop.load(Ordering::Acquire){transfer_cancel.store(true,Ordering::Release);}
            let completed=checked.load(Ordering::Relaxed);
            let rows=states.lock().unwrap();
            if rows.iter().all(|row|row.transferred){
                if !verification_started{
                    let network_total=rows.iter().zip(files).filter(|(row,_)|!row.cached).map(|(_, (file,_))|file.size).sum::<u64>();
                    if network_total>0{callback(&format!("{BATCH_DOWNLOAD_PROGRESS_PREFIX}下载完成"),network_total,network_total);}
                }
                verification_started=true;
                callback(&format!("{BATCH_VERIFY_PROGRESS_PREFIX}校验下载文件（{completed} / {} 个文件）",files.len()),rows.iter().map(|row|row.verified).sum::<u64>().min(total),total);
            }else{
                let downloaded=rows.iter().filter(|row|!row.cached).map(|row|row.downloaded).sum();
                let network_total=rows.iter().zip(files).filter(|(row,_)|!row.cached).map(|(_, (file,_))|file.size).sum();
                let source=source_status.lock().map(|value|value.clone()).unwrap_or_default();
                callback(&format!("{BATCH_DOWNLOAD_PROGRESS_PREFIX}{}（{completed} / {} 个文件） {source}",if verification_started{"校验未通过，重新下载"}else{"下载固定资源"},files.len()),downloaded,network_total);
            }
        }
        for worker in workers{if worker.join().is_err(){*failure.lock().unwrap()=Some("下载工作线程异常退出，断点已保留。".into());}}
    });
    if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
    if let Some(error)=failure.into_inner().map_err(|_|"下载状态不可用。")?{return Err(error);}
    callback(&format!("{BATCH_VERIFY_PROGRESS_PREFIX}摘要校验完成（{} / {} 个文件）",files.len(),files.len()),total,total);Ok(())
}

#[cfg(all(test, windows, feature = "qualification"))]
mod tests {
    use super::*;
    use std::{fs, io::{Read, Write}, net::TcpListener};

    #[test]
    fn concurrent_same_target_reuses_one_transfer_and_can_cancel_waiter(){
        let root=std::env::temp_dir().join(format!("voice-download-mutex-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let target=root.join("shared.bin");let held=target_lock(&target,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
        let cancel=AtomicBool::new(false);let mut waited=false;
        let error=target_lock(&target,&cancel,&mut|phase,_,_|{waited=phase.contains("等待");cancel.store(true,Ordering::Release);}).unwrap_err();
        assert!(waited&&error.contains("取消"));drop(held);
        let body=b"one verified shared download";let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
        let server=std::thread::spawn(move||{
            let(mut socket,_)=listener.accept().unwrap();let mut request=[0;4096];socket.read(&mut request).unwrap();
            write!(socket,"HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n",body.len(),body.len()-1,body.len()).unwrap();socket.write_all(body).unwrap();
        });
        let file=DownloadFile{url:format!("http://{address}/shared.bin"),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(body)),sha512:String::new(),path:"shared.bin".into()};
        std::thread::scope(|scope|{let first=scope.spawn(||download_with_progress(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},0));
            let second=scope.spawn(||download_with_progress(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},0));
            assert!(first.join().unwrap().is_ok());assert!(second.join().unwrap().is_ok());});
        server.join().unwrap();assert_eq!(fs::read(&target).unwrap(),body);fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn locked_local_promotion_does_not_retry_other_sources(){
        let root=std::env::temp_dir().join(format!("voice-download-local-error-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let target=root.join("same.bin");let part=target.with_extension("bin.part");let body=b"verified complete partial";
        let file=DownloadFile{url:"http://127.0.0.1:1/same.bin".into(),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(body)),sha512:String::new(),path:"same.bin".into()};
        fs::write(&part,body).unwrap();fs::write(target.with_extension("bin.part.identity"),format!("{}:sha256:{}",file.size,file.sha256)).unwrap();
        let held=fs::OpenOptions::new().read(true).share_mode(3).open(&part).unwrap();let mut sources=0;
        let error=download_candidates(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{if phase.starts_with("下载来源："){sources+=1;}},0,&[file.url.clone(),"http://127.0.0.1:2/same.bin".into()]).unwrap_err();
        assert_eq!(sources,1);assert!(!error.contains("所有下载来源"));assert!(part.exists());drop(held);fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn changed_identity_discards_stale_partial_and_preserves_old_target_until_verified(){
        let root=std::env::temp_dir().join(format!("voice-download-upgrade-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let target=root.join("upgrade.bin");let body=b"new verified component";let old=b"old working component";
        fs::write(&target,old).unwrap();fs::write(target.with_extension("bin.part"),b"stale partial from other version").unwrap();
        fs::write(target.with_extension("bin.part.identity"),"old size and digest").unwrap();
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
        let server=std::thread::spawn(move||{let(mut socket,_)=listener.accept().unwrap();let mut request=[0;4096];socket.read(&mut request).unwrap();
            write!(socket,"HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n",body.len(),body.len()-1,body.len()).unwrap();socket.write_all(body).unwrap();});
        let file=DownloadFile{url:format!("http://{address}/upgrade.bin"),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(body)),sha512:String::new(),path:"upgrade.bin".into()};
        let mut verified_before_replace=false;
        download_with_progress(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{if phase=="校验下载文件"{assert_eq!(fs::read(&target).unwrap(),old);verified_before_replace=true;}},0).unwrap();
        server.join().unwrap();assert!(verified_before_replace);assert_eq!(fs::read(&target).unwrap(),body);
        assert!(!target.with_extension("bin.part.identity").exists());fs::remove_dir_all(root).unwrap();
    }
    use sha2::{Digest, Sha256};
    use std::{os::windows::fs::OpenOptionsExt, time::Duration};

    #[test]
    fn mirror_bad_digest_falls_back_and_cancel_stops_before_next_source(){
        let root=std::env::temp_dir().join(format!("voice-mirror-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let body=b"verified resource";let expected=format!("{:x}",Sha256::digest(body));
        let mut sources=vec![];let mut servers=vec![];
        for payload in [b"incorrect content".to_vec(),body.to_vec()] {
            let server=TcpListener::bind("127.0.0.1:0").unwrap();sources.push(format!("http://{}/model.bin",server.local_addr().unwrap()));
            servers.push(std::thread::spawn(move||{let(mut socket,_)=server.accept().unwrap();let mut request=[0;4096];socket.read(&mut request).unwrap();
                write!(socket,"HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n",payload.len(),payload.len()-1,payload.len()).unwrap();socket.write_all(&payload).unwrap();}));
        }
        let file=DownloadFile{url:sources[1].clone(),size:body.len() as u64,sha256:expected,sha512:String::new(),path:"model.bin".into()};
        let target=root.join("download.bin");let mut phases=vec![];
        download_candidates(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|phases.push(phase.to_owned()),0,&sources).unwrap();
        for server in servers{server.join().unwrap();}
        assert_eq!(fs::read(&target).unwrap(),body);assert!(phases.iter().any(|phase|phase.contains("尝试备用来源")));
        let cancelled=Arc::new(AtomicBool::new(true));let mut called=false;
        assert!(download_candidates(&file,&target,cancelled,&mut|_,_,_|called=true,0,&sources).is_err());assert!(!called);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn managed_source_recovery_uses_one_source_per_round(){
        let root=std::env::temp_dir().join(format!("voice-source-round-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let body=b"verified recovery";let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
        let called=Arc::new(AtomicBool::new(false));let seen=called.clone();
        let server=std::thread::spawn(move||{let(mut socket,_)=listener.accept().unwrap();seen.store(true,Ordering::Release);
            let mut request=[0;4096];socket.read(&mut request).unwrap();
            write!(socket,"HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n",body.len(),body.len()-1,body.len()).unwrap();socket.write_all(body).unwrap();});
        let sources=vec!["http://127.0.0.1:1/unavailable".to_owned(),format!("http://{address}/model")];
        let file=DownloadFile{url:sources[0].clone(),path:"model.bin".into(),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(body)),sha512:String::new()};
        let target=root.join("model.bin");
        let first=with_attempt(Some(1),||download_candidates(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},0,&sources));
        assert!(first.unwrap_err().contains("本轮下载来源失败"));assert!(!called.load(Ordering::Acquire));assert_eq!(current_attempt(),None);
        with_attempt(Some(2),||download_candidates(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},0,&sources)).unwrap();
        server.join().unwrap();assert_eq!(fs::read(&target).unwrap(),body);fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cached_hash_reports_progress_and_cancels_without_touching_verified_file(){
        let root=std::env::temp_dir().join(format!("voice-download-摘要取消-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let body=vec![42u8;8*1024*1024];let target=root.join("缓存.bin");fs::write(&target,&body).unwrap();
        let file=DownloadFile{url:"http://127.0.0.1:1/cache.bin".into(),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(&body)),sha512:String::new(),path:"cache.bin".into()};
        let cancel=Arc::new(AtomicBool::new(false));let mut phases=vec![];let started=std::time::Instant::now();
        let result=download_with_progress(&file,&target,cancel.clone(),&mut|phase,done,_|{
            phases.push(phase.to_owned());if phase=="校验现有下载缓存"&&done>=1024*1024{cancel.store(true,Ordering::Release);}
        },0);
        assert!(result.unwrap_err().contains("任务已取消"));assert!(started.elapsed()<Duration::from_secs(2));
        assert!(phases.iter().any(|phase|phase=="校验现有下载缓存"));assert_eq!(fs::read(&target).unwrap(),body);
        download(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_|{},0).unwrap();fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn stalled_network_request_is_cancelled_and_all_handles_are_released(){
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
        let (arrived,accepted)=mpsc::channel();let (release,wait)=mpsc::channel();
        let server=std::thread::spawn(move||{let(mut socket,_)=listener.accept().unwrap();let mut request=[0;4096];socket.read(&mut request).unwrap();arrived.send(()).unwrap();let _=wait.recv_timeout(Duration::from_secs(3));});
        let root=std::env::temp_dir().join(format!("voice-download-网络停滞-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();let target=root.join("model.bin");
        let file=DownloadFile{url:format!("http://{address}/model.bin"),size:1024,sha256:format!("{:x}",Sha256::digest(vec![1u8;1024])),sha512:String::new(),path:"model.bin".into()};
        let cancel=Arc::new(AtomicBool::new(false));let flag=cancel.clone();
        let trigger=std::thread::spawn(move||{accepted.recv_timeout(Duration::from_secs(3)).unwrap();let start=std::time::Instant::now();flag.store(true,Ordering::Release);start});
        let result=download(&file,&target,cancel,&mut|_,_|{},0);let started=trigger.join().unwrap();
        assert!(started.elapsed()<Duration::from_secs(2));assert!(result.unwrap_err().contains("任务已取消"));
        release.send(()).unwrap();server.join().unwrap();assert!(!target.exists());fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn ignored_range_restarts_full_stream_and_verifies_large_file(){
        let body=Arc::new(vec![19u8;9*1024*1024]);let expected=format!("{:x}",Sha256::digest(&*body));
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();let content=body.clone();
        let server=std::thread::spawn(move||{let mut requests=vec![];for _ in 0..2{
            let(mut socket,_)=listener.accept().unwrap();let mut request=[0;4096];let length=socket.read(&mut request).unwrap();requests.push(String::from_utf8_lossy(&request[..length]).to_string());
            write!(socket,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",content.len()).unwrap();let _=socket.write_all(&content);
        }requests});
        let root=std::env::temp_dir().join(format!("voice-download-无范围-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();let target=root.join("large.bin");
        let file=DownloadFile{url:format!("http://{address}/large.bin"),size:body.len() as u64,sha256:expected.clone(),sha512:String::new(),path:"large.bin".into()};
        fs::write(target.with_extension("bin.part"),vec![99u8;1024*1024]).unwrap();fs::write(target.with_extension("bin.part.identity"),format!("{}:sha256:{}",file.size,expected)).unwrap();
        download(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_|{},0).unwrap();let requests=server.join().unwrap();
        assert!(requests[0].contains("Range: bytes=1048576-"));assert!(!requests[1].contains("Range:"));assert_eq!(crate::workspace::file_hash(&target).unwrap(),expected);
        assert!(!target.with_extension("bin.part").exists());fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn independent_batches_share_global_concurrency_and_report_on_caller(){
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
        let active=Arc::new(AtomicUsize::new(0));let peak=Arc::new(AtomicUsize::new(0));let load=active.clone();let maximum=peak.clone();
        let server=std::thread::spawn(move||{let mut workers=vec![];for _ in 0..12{
            let(mut socket,_)=listener.accept().unwrap();let load=load.clone();let maximum=maximum.clone();
            workers.push(std::thread::spawn(move||{let now=load.fetch_add(1,Ordering::AcqRel)+1;maximum.fetch_max(now,Ordering::AcqRel);
                let mut request=[0;4096];let length=socket.read(&mut request).unwrap();let request=String::from_utf8_lossy(&request[..length]);let index=request.split_whitespace().nth(1).unwrap().trim_start_matches('/').trim_end_matches(".bin").parse::<u8>().unwrap();
                std::thread::sleep(Duration::from_millis(75));let body=vec![index;256*1024];
                write!(socket,"HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n",body.len(),body.len()-1,body.len()).unwrap();socket.write_all(&body).unwrap();load.fetch_sub(1,Ordering::AcqRel);
            }));
        }for worker in workers{worker.join().unwrap();}});
        let root=std::env::temp_dir().join(format!("voice-download-并行-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let files=(0u8..12).map(|index|{let name=format!("{index}.bin");let body=vec![index;256*1024];
            (DownloadFile{url:format!("http://{address}/{name}"),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(&body)),sha512:String::new(),path:name.clone()},root.join(name))}).collect::<Vec<_>>();
        std::thread::scope(|scope|{let mut workers=vec![];for batch in files.chunks(6){workers.push(scope.spawn(move||{
            let caller=std::thread::current().id();let mut samples=vec![];
            download_batch(batch,Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{assert_eq!(std::thread::current().id(),caller);assert!(done<=total);samples.push((phase.to_owned(),done,total));}).unwrap();
            for prefix in [BATCH_DOWNLOAD_PROGRESS_PREFIX,BATCH_VERIFY_PROGRESS_PREFIX]{
                let stage=samples.iter().filter(|(phase,_,_)|phase.starts_with(prefix)).collect::<Vec<_>>();
                assert!(stage.windows(2).all(|pair|pair[0].1<=pair[1].1));assert_eq!(stage.last().unwrap().1,stage.last().unwrap().2);
            }
        }));}for worker in workers{worker.join().unwrap();}});server.join().unwrap();
        assert!((2..=MAX_DOWNLOADS).contains(&peak.load(Ordering::Acquire)));assert_eq!(active.load(Ordering::Acquire),0);
        // 服务端已关闭，全部文件仍能从完整缓存复用，不需要再次联网。
        download_batch(&files,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).unwrap();
        // AV（防病毒扫描）可能在任务句柄已归还后短暂借用缓存；夹具清理有界等待。
        for round in 0..20{match fs::remove_dir_all(&root){Ok(())=>break,Err(error) if round<19&&matches!(error.raw_os_error(),Some(32|33))=>std::thread::sleep(Duration::from_millis(50)),Err(error)=>panic!("夹具清理失败：{error}")}}
    }

    /// download_with_temporary_lock（短暂占用下载夹具）：覆盖分片和已有缓存的共享冲突。
    fn download_with_temporary_lock(lock_target: bool) { // lock_target（占用最终文件；否则占用下载分片）。
        let body = b"verified dependency wheel";
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = [0; 4096];socket.read(&mut request).unwrap();
            write!(socket, "HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n", body.len(), body.len() - 1, body.len()).unwrap();
            socket.write_all(body).unwrap();
        });
        let root = std::env::temp_dir().join(format!("voice-download-占用测试-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();let target = root.join("依赖 文件.whl");
        fs::write(&target, b"old incomplete cache").unwrap();
        let part = target.with_extension("whl.part"); // part（下载暂存文件）。
        let checkpoint = target.with_extension("whl.part.identity"); // checkpoint（固定大小及摘要断点）。
        let file = DownloadFile { url: format!("http://{address}/dependency.whl"), size: body.len() as u64,
            sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "dependency.whl".into() };
        let mut release = None; // release（模拟其他进程短暂占用后释放句柄）。
        let result = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |done, _| {
            if done == file.size && release.is_none() {
                // share_mode（共享模式）允许读写，但禁止其他句柄删除或重命名。
                let held = fs::OpenOptions::new().read(true).share_mode(3).open(if lock_target { &target } else { &part }).unwrap();
                release = Some(std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(350));drop(held); }));
            }
        }, 0);
        server.join().unwrap();release.unwrap().join().unwrap();
        let installed = fs::read(&target).ok(); // installed（最终缓存内容）。
        let clean = !part.exists() && !checkpoint.exists(); // clean（完成后断点已清理）。
        fs::remove_dir_all(&root).unwrap();
        assert!(result.is_ok(), "短暂文件占用不应中断依赖下载：{result:?}");
        assert_eq!(installed.as_deref(), Some(body.as_slice()));assert!(clean);
    }

    #[test]
    fn native_download_retries_temporary_file_locks_without_losing_verified_bytes() {
        // 不同下载同时遇到文件占用，仍须各自完成校验、替换与断点清理。
        std::thread::scope(|scope| {
            for lock_target in [false, true] {
                scope.spawn(move || download_with_temporary_lock(lock_target));
            }
        });
    }

    #[test]
    fn native_download_cancels_locked_promotion_and_resumes_without_network() {
        let body = b"completed dependency wheel";
        let root = std::env::temp_dir().join(format!("voice-download-断点测试-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();let target = root.join("dependency.whl");
        let part = target.with_extension("whl.part");let checkpoint = target.with_extension("whl.part.identity");
        let file = DownloadFile { url: "http://127.0.0.1:1/dependency.whl".into(), size: body.len() as u64,
            sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "dependency.whl".into() };
        fs::write(&part, body).unwrap();fs::write(&checkpoint, format!("{}:sha256:{}", file.size, file.sha256)).unwrap();
        fs::write(&target, b"keep previous cache").unwrap();
        let held = fs::OpenOptions::new().read(true).share_mode(3).open(&part).unwrap();
        let cancel = Arc::new(AtomicBool::new(false));let mut samples = 0;
        let result = download(&file, &target, cancel.clone(), &mut |_, _| {
            samples += 1;if samples >= 3 { cancel.store(true, Ordering::Release); }
        }, 0);
        let preserved = fs::read(&part).unwrap() == body && checkpoint.exists() && fs::read(&target).ok().as_deref() == Some(b"keep previous cache".as_slice()); // preserved（取消后保留完整断点与旧缓存）。
        drop(held);
        let resumed = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |_, _| {}, 0);
        let complete = fs::read(&target).unwrap() == body && !part.exists() && !checkpoint.exists();
        fs::remove_dir_all(&root).unwrap();
        assert!(result.unwrap_err().contains("任务已取消"));assert!(preserved);
        assert!(resumed.is_ok());assert!(complete);
    }

    #[test]
    fn native_download_bounds_file_lock_wait_and_preserves_previous_cache() {
        let body = b"verified replacement wheel";
        let root = std::env::temp_dir().join(format!("voice-download-超时测试-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();let target = root.join("dependency.whl");
        let part = target.with_extension("whl.part");let checkpoint = target.with_extension("whl.part.identity");
        let file = DownloadFile { url: "http://127.0.0.1:1/dependency.whl".into(), size: body.len() as u64,
            sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "dependency.whl".into() };
        fs::write(&part, body).unwrap();fs::write(&checkpoint, format!("{}:sha256:{}", file.size, file.sha256)).unwrap();
        fs::write(&target, b"keep previous cache").unwrap();
        let held = fs::OpenOptions::new().read(true).share_mode(3).open(&target).unwrap();
        let started = std::time::Instant::now(); // started（文件占用等待的开始时间）。
        let result = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |_, _| {}, 0);
        let elapsed = started.elapsed(); // elapsed（实际等待时长，避免无限重试）。
        let preserved = fs::read(&part).unwrap() == body && checkpoint.exists() && fs::read(&target).unwrap() == b"keep previous cache";
        drop(held);fs::remove_dir_all(&root).unwrap();
        // Windows（系统）对已存在的占用目标也可能返回访问拒绝，两种结果都要限时保留断点。
        let error = result.unwrap_err();assert!(error.contains("下载文件仍被其他进程占用") || error.contains("系统仍拒绝启用下载文件"), "{error}");assert!(error.contains("断点已保留"));
        assert!(elapsed >= Duration::from_secs(5) && elapsed < Duration::from_secs(10));assert!(preserved);
    }

    #[test]
    fn native_download_accepts_success_with_exact_range_and_rejects_wrong_range() {
        for (status, valid) in [(200, true), (206, true), (200, false)] {
            let body = b"fixed model file";
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let server = std::thread::spawn(move || {
                let (mut socket, _) = listener.accept().unwrap();
                let mut request = [0; 4096];socket.read(&mut request).unwrap();
                let last = if valid { body.len() - 1 } else { body.len() - 2 };
                write!(socket, "HTTP/1.1 {status} OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{last}/{}\r\nConnection: close\r\n\r\n", body.len(), body.len()).unwrap();
                socket.write_all(body).unwrap();
            });
            let root = std::env::temp_dir().join(format!("voice-download-{}", crate::workspace::new_id()));
            fs::create_dir_all(&root).unwrap();let target = root.join("model.bin");
            let file = DownloadFile { url: format!("http://{address}/model.bin"), size: body.len() as u64,
                sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "model.bin".into() };
            let mut samples = vec![];
            let result = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |done, total| samples.push((done, total)), 0);
            assert_eq!(result.is_ok(), valid);
            if valid { assert_eq!(fs::read(&target).unwrap(), body);assert_eq!(samples.last(), Some(&(file.size, file.size))); }
            server.join().unwrap();
            // 失败返回后也必须释放分片文件句柄，否则同一进程无法清理或重试下载。
            for path in [target.clone(), target.with_extension("bin.part"), target.with_extension("bin.part.identity")] {
                if let Err(error) = fs::remove_file(&path) {
                    assert_eq!(error.kind(), std::io::ErrorKind::NotFound, "下载测试文件未释放：{}：{error}", path.display());
                }
            }
            fs::remove_dir(&root).expect("下载测试的暂存目录未释放");
        }
    }
}
